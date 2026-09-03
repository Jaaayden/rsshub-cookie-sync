#!/usr/bin/env python3
"""Microsoft Edge Native Messaging host for RSSHub Cookie Sync.

The host deliberately has a very small surface area:

* Native Messaging uses the Chromium little-endian, four-byte length frame.
* The only data accepted from the extension is a versioned provider payload.
* The cookie headers are passed to the server only as SSH stdin.  They are
  never put in an argv item, an environment variable, or a log message.
* The remote forced command returns one of the small, allow-listed statuses.

This module uses only Python 3.9's standard library so it can be copied to a
user's Application Support directory without installing a Python package.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Mapping, Optional, Sequence, TextIO, Tuple


HOST_NAME = "com.jayden.rsshub_cookie_sync"
DEFAULT_SERVER_PORT = 22
DEFAULT_SERVER_USER = "rsshub-sync"
DEFAULT_CONNECT_TIMEOUT = 15
MAX_CONNECT_TIMEOUT = 120

# ``ConnectTimeout`` only covers establishing the SSH connection.  The
# forced command on the server can validate candidates, probe the current
# login, recreate RSSHub, wait for its health check, run post-recreate
# probes, and (on failure) perform a rollback.  A short timeout derived from
# ConnectTimeout would interrupt that transaction while it is still safely
# progressing.  Reserve a fixed 15-minute budget for the remote apply and
# add only the bounded connection budget below, so a hung SSH process still
# has a deterministic upper bound.
DEFAULT_APPLY_TIMEOUT = 15 * 60
MAX_SSH_SUBPROCESS_TIMEOUT = DEFAULT_APPLY_TIMEOUT + MAX_CONNECT_TIMEOUT

DEFAULT_APP_SUPPORT_DIR = (
    Path.home() / "Library" / "Application Support" / "RSSHub Cookie Sync"
)
DEFAULT_CONFIG_PATH = DEFAULT_APP_SUPPORT_DIR / "config.json"
DEFAULT_IDENTITY_FILE = DEFAULT_APP_SUPPORT_DIR / "ssh" / "id_ed25519"
DEFAULT_KNOWN_HOSTS_FILE = DEFAULT_APP_SUPPORT_DIR / "ssh" / "known_hosts"

# The browser can legitimately send a fairly large Cookie header, but an
# unbounded Native Messaging frame would make the host an easy memory DoS.
# Keep the frame bounded independently of the cookie-header limit.  A single
# request may contain both provider headers, so the frame budget is larger
# than one header but still small enough for a browser-launched helper.
MAX_FRAME_BYTES = 1024 * 1024
MAX_COOKIE_BYTES = 128 * 1024

ALLOWED_PROVIDERS = frozenset(("zhihu", "weibo"))
ALLOWED_REMOTE_STATUSES = frozenset(
    ("unchanged", "candidate_saved", "promoted", "rejected_invalid", "retryable_error")
)

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")


class ProtocolError(ValueError):
    """An invalid Native Messaging frame or request payload."""


class ConfigurationError(ValueError):
    """A missing or unsafe local SSH configuration."""


@dataclass(frozen=True)
class HostConfig:
    """Validated values needed to make one SSH connection."""

    # The deployment-specific server endpoint is intentionally required.  A
    # missing value must never silently target another operator's server.
    server_host: str
    server_port: int = DEFAULT_SERVER_PORT
    server_user: str = DEFAULT_SERVER_USER
    # A proxy is opt-in.  ``None`` means that OpenSSH connects directly.
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    identity_file: Path = DEFAULT_IDENTITY_FILE
    known_hosts_file: Path = DEFAULT_KNOWN_HOSTS_FILE
    ssh_binary: str = "/usr/bin/ssh"
    nc_binary: str = "/usr/bin/nc"
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT


def _default_path(path: Path) -> Path:
    """Expand a user path and make it absolute without following symlinks."""

    return path.expanduser().absolute()


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ConfigurationError("invalid configuration value")
    return value


def _port(value: Any) -> int:
    # bool is an int subclass, but is never a valid port.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigurationError("invalid port")
    return value


def _config_path(value: Any, base_dir: Path) -> Path:
    path = Path(_string(value, "path")).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.absolute()


def _validate_endpoint(host: str, user: Optional[str] = None) -> None:
    # The endpoint is eventually placed in an SSH argv item and a ProxyCommand
    # option.  Keep it to a hostname/IP grammar; no shell metacharacters or
    # whitespace can enter the command.
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host) or host.startswith(".") or host.endswith("."):
        raise ConfigurationError("invalid endpoint")
    if user is not None and (not isinstance(user, str) or not _USER_RE.fullmatch(user)):
        raise ConfigurationError("invalid SSH user")


def _validate_binary(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or not path.startswith("/")
        or "\x00" in path
        or "\n" in path
        or "\r" in path
        or any(char.isspace() for char in path)
    ):
        raise ConfigurationError("invalid executable path")
    # The nc path is interpolated into OpenSSH's ProxyCommand string, which
    # OpenSSH executes through a shell.  Restrict both executable paths to a
    # shell-safe absolute path; the default on macOS is /usr/bin/nc.
    if not re.fullmatch(r"/[A-Za-z0-9._/+-]+", path):
        raise ConfigurationError("invalid executable path")
    return path


def _validate_file_argument(path: Path) -> Path:
    value = os.fspath(path)
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ConfigurationError("invalid SSH file path")
    return path


def _quote_ssh_config_value(value: str) -> str:
    """Quote one value consumed by OpenSSH's ``-o`` config parser.

    ``subprocess`` already keeps each argv item intact, but OpenSSH parses the
    text after ``-o`` again using ssh_config token rules.  Paths below
    ``~/Library/Application Support`` therefore need config-level quoting even
    though no shell is involved.  Escape the two characters that are special
    inside a double-quoted ssh_config token.
    """

    if not isinstance(value, str) or not value or any(
        character in value for character in ("\x00", "\n", "\r")
    ):
        raise ConfigurationError("invalid SSH option value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_from_mapping(raw: Mapping[str, Any], *, config_path: Optional[Path] = None) -> HostConfig:
    """Build a :class:`HostConfig` from the installer JSON shape.

    Unknown top-level/section keys are ignored for forwards compatibility, but
    all values used for the SSH command are strictly typed and validated.
    """

    base_dir = (config_path.parent if config_path else DEFAULT_APP_SUPPORT_DIR).absolute()
    server = raw.get("server")
    proxy = raw.get("proxy")
    ssh = raw.get("ssh", {})
    if not isinstance(server, dict) or not isinstance(ssh, dict):
        raise ConfigurationError("invalid configuration")

    server_host = _string(server.get("host"), "server host")
    server_user = _string(server.get("user", DEFAULT_SERVER_USER), "server user")
    server_port = _port(server.get("port", DEFAULT_SERVER_PORT))

    proxy_host: Optional[str]
    proxy_port: Optional[int]
    if proxy is None:
        proxy_host = None
        proxy_port = None
    elif isinstance(proxy, dict):
        # A configured proxy must contain both endpoint parts.  This avoids
        # accidentally treating a typo as a direct connection.
        proxy_host = _string(proxy.get("host"), "proxy host")
        proxy_port = _port(proxy.get("port"))
        proxy_type = proxy.get("type", "socks5")
        if proxy_type != "socks5":
            raise ConfigurationError("only SOCKS5 proxy is supported")
    else:
        raise ConfigurationError("invalid configuration")

    identity = ssh.get("identity_file", str(DEFAULT_IDENTITY_FILE))
    known_hosts = ssh.get("known_hosts_file", str(DEFAULT_KNOWN_HOSTS_FILE))
    ssh_binary = _validate_binary(_string(ssh.get("binary", "/usr/bin/ssh"), "ssh binary"))
    nc_binary = _validate_binary(_string(ssh.get("nc_binary", "/usr/bin/nc"), "nc binary"))
    timeout = ssh.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_CONNECT_TIMEOUT:
        raise ConfigurationError("invalid SSH timeout")

    _validate_endpoint(server_host, server_user)
    if proxy_host is not None:
        _validate_endpoint(proxy_host)
    return HostConfig(
        server_host=server_host,
        server_port=server_port,
        server_user=server_user,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        identity_file=_config_path(identity, base_dir),
        known_hosts_file=_config_path(known_hosts, base_dir),
        ssh_binary=ssh_binary,
        nc_binary=nc_binary,
        connect_timeout=timeout,
    )


def _safe_config_file(path: Path) -> None:
    """Ensure a local config is a user-owned, non-writable-by-others file."""

    try:
        info = path.lstat()
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigurationError("configuration file unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ConfigurationError("configuration file is not regular")
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ConfigurationError("configuration file permissions are unsafe")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> HostConfig:
    """Load and validate an installer-generated config file.

    A missing file is treated as an incomplete installation.  The host loop
    turns that configuration error into ``retryable_error`` until the
    installer has written a server endpoint, key, and known_hosts file.
    """

    path = _default_path(path)
    try:
        _safe_config_file(path)
    except ConfigurationError as exc:
        # ``Path.exists`` is false for a broken symlink.  Never treat one as a
        # missing config, otherwise a later replacement could be redirected to
        # an attacker-controlled location.
        if not os.path.lexists(str(path)):
            raise ConfigurationError("configuration file missing") from exc
        raise exc
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ConfigurationError("cannot read configuration") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("invalid configuration")
    if type(raw.get("schema_version", 1)) is not int or raw.get("schema_version", 1) != 1:
        raise ConfigurationError("unsupported configuration version")
    return config_from_mapping(raw, config_path=path)


def _safe_key_file(path: Path) -> None:
    """Validate a private key path before handing it to OpenSSH."""

    try:
        info = path.lstat()
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigurationError("SSH identity unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ConfigurationError("SSH identity is not regular")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigurationError("SSH identity permissions are unsafe")


def _safe_known_hosts_file(path: Path) -> None:
    try:
        info = path.lstat()
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigurationError("known_hosts unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ConfigurationError("known_hosts is not regular")
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ConfigurationError("known_hosts permissions are unsafe")


def validate_runtime_files(config: HostConfig) -> None:
    """Check the files used by SSH without reading either file's contents."""

    _safe_key_file(config.identity_file)
    _safe_known_hosts_file(config.known_hosts_file)


def build_ssh_argv(config: HostConfig, *, use_proxy: bool = True) -> Tuple[str, ...]:
    """Return a fully explicit, shell-free SSH command.

    The server account is expected to use an ``authorized_keys`` forced
    command.  No remote command is appended here, so the forced command is the
    only server-side operation available to this host.
    """

    _port(config.server_port)
    _validate_endpoint(config.server_host, config.server_user)
    if (config.proxy_host is None) != (config.proxy_port is None):
        raise ConfigurationError("proxy host and port must be configured together")
    proxy_configured = config.proxy_host is not None
    if proxy_configured:
        # The paired-None check above makes this safe for the Optional fields.
        _port(config.proxy_port)
        _validate_endpoint(config.proxy_host)
    if not isinstance(config.connect_timeout, int) or isinstance(config.connect_timeout, bool):
        raise ConfigurationError("invalid SSH timeout")
    if not 1 <= config.connect_timeout <= MAX_CONNECT_TIMEOUT:
        raise ConfigurationError("invalid SSH timeout")
    _validate_binary(config.ssh_binary)
    _validate_binary(config.nc_binary)
    _validate_file_argument(config.identity_file)
    _validate_file_argument(config.known_hosts_file)

    arguments = [
        config.ssh_binary,
        "-F",
        "/dev/null",
        "-T",
        "-p",
        str(config.server_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "-o",
        "CheckHostIP=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "RequestTTY=no",
        "-o",
        f"UserKnownHostsFile={_quote_ssh_config_value(os.fspath(config.known_hosts_file))}",
        "-o",
        f"IdentityFile={_quote_ssh_config_value(os.fspath(config.identity_file))}",
        "-o",
        f"ConnectTimeout={config.connect_timeout}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if use_proxy and proxy_configured:
        proxy_command = (
            f"{config.nc_binary} -x {config.proxy_host}:{config.proxy_port} -X 5 %h %p"
        )
        arguments.extend(("-o", f"ProxyCommand={proxy_command}"))
    arguments.append(f"{config.server_user}@{config.server_host}")
    return tuple(arguments)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO, *, max_bytes: int = MAX_FRAME_BYTES) -> Optional[bytes]:
    """Read one Native Messaging payload, returning ``None`` at clean EOF."""

    prefix = _read_exact(stream, 4)
    if not prefix:
        return None
    if len(prefix) != 4:
        raise ProtocolError("truncated frame length")
    (length,) = struct.unpack("<I", prefix)
    if length == 0 or length > max_bytes:
        raise ProtocolError("frame too large")
    payload = _read_exact(stream, length)
    if len(payload) != length:
        raise ProtocolError("truncated frame")
    return payload


def encode_frame(payload: bytes) -> bytes:
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_FRAME_BYTES:
        raise ProtocolError("invalid frame payload")
    return struct.pack("<I", len(payload)) + payload


def write_status(stream: BinaryIO, status: str) -> None:
    """Write the intentionally minimal response understood by the extension."""

    if status not in ALLOWED_REMOTE_STATUSES:
        status = "retryable_error"
    payload = json.dumps({"status": status}, separators=(",", ":")).encode("ascii")
    stream.write(encode_frame(payload))
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ProtocolError("invalid JSON number")


def _validate_cookie_header(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError("cookie header is missing")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        # Lone UTF-16 surrogates can be constructed by a malformed extension
        # payload.  Turn them into a normal validation result instead of
        # allowing an encoding exception to escape the Native host loop.
        raise ProtocolError("cookie header is not valid UTF-8") from exc
    if encoded_length > MAX_COOKIE_BYTES:
        raise ProtocolError("cookie header is too large")
    if any(ord(char) < 0x20 or char == "\x7f" for char in value):
        # This includes NUL, CR and LF.  Rejecting all ASCII controls also
        # avoids terminal/log injection if a future caller accidentally logs a
        # validation error.
        raise ProtocolError("cookie header contains a control character")

    # A browser Cookie header is a semicolon-separated list.  Values may be
    # empty and names may repeat (different cookie paths can produce that), so
    # this validates syntax without normalizing or deduplicating the secret.
    parts = [part.strip() for part in value.split(";")]
    if not parts or any("=" not in part for part in parts):
        raise ProtocolError("cookie header is malformed")
    for part in parts:
        name, _ = part.split("=", 1)
        if not name or not _COOKIE_NAME_RE.fullmatch(name):
            raise ProtocolError("cookie name is malformed")
    return value


def validate_request(raw: bytes) -> Dict[str, Dict[str, str]]:
    """Validate a request and return only the canonical provider payload."""

    try:
        text = raw.decode("utf-8")
        message = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ProtocolError, RecursionError) as exc:
        raise ProtocolError("invalid request JSON") from exc
    if not isinstance(message, dict):
        raise ProtocolError("request must be an object")
    if set(message) != {"version", "providers"}:
        raise ProtocolError("unknown request field")
    version = message.get("version")
    if type(version) is not int or version != 1:
        raise ProtocolError("unsupported request version")
    providers = message.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ProtocolError("providers must be a non-empty object")

    canonical: Dict[str, Dict[str, str]] = {}
    for provider, details in providers.items():
        if provider not in ALLOWED_PROVIDERS:
            raise ProtocolError("unknown provider")
        if not isinstance(details, dict):
            raise ProtocolError("provider details must be an object")
        if set(details) != {"cookieHeader"}:
            raise ProtocolError("invalid provider details")
        canonical[provider] = {"cookieHeader": _validate_cookie_header(details["cookieHeader"])}
    return canonical


def _request_bytes(providers: Mapping[str, Mapping[str, str]]) -> bytes:
    # Re-serialize the validated subset.  This prevents unknown JSON fields
    # from reaching the forced command and keeps the wire shape deterministic.
    message = {"version": 1, "providers": providers}
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_remote_status(output: bytes) -> Optional[str]:
    """Extract only an allow-listed status from the forced command output."""

    if not isinstance(output, bytes) or not output or len(output) > 64 * 1024:
        return None
    try:
        value = json.loads(
            output.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ProtocolError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    if set(value) != {"status"}:
        return None
    status = value.get("status")
    if isinstance(status, str) and status in ALLOWED_REMOTE_STATUSES:
        return status
    return None


def send_to_server(
    providers: Mapping[str, Mapping[str, str]],
    config: HostConfig,
    *,
    runner: Any = subprocess.run,
) -> str:
    """Send one validated request to the forced SSH command.

    ``runner`` is injectable for tests.  The production call supplies a fixed,
    minimal child environment, and no cookie is ever copied into that mapping;
    the cookie payload is supplied only via ``input``.
    """

    try:
        validate_runtime_files(config)
        payload = _request_bytes(providers)
        # Do not inherit proxy variables, SSH agent handles, locale hooks, or
        # any unrelated browser environment.  In particular, cookie headers
        # are never represented in this mapping; they exist only in ``input``.
        child_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LC_ALL": "C",
        }
        runner_kwargs = {
            "input": payload,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": child_env,
            "shell": False,
            "check": False,
            # The SSH connection can consume the configured ConnectTimeout
            # before the remote forced command starts.  Keep that grace
            # separate from the 15-minute apply transaction budget and cap
            # the resulting process timeout by construction.
            "timeout": DEFAULT_APPLY_TIMEOUT + config.connect_timeout,
        }
        result = runner(
            list(build_ssh_argv(config)),
            **runner_kwargs,
        )
    except (
        ConfigurationError,
        OSError,
        ProtocolError,
        UnicodeError,
        RecursionError,
        subprocess.SubprocessError,
        TimeoutError,
    ):
        return "retryable_error"

    returncode = getattr(result, "returncode", 1)
    if returncode != 0:
        return "retryable_error"
    status = parse_remote_status(getattr(result, "stdout", b""))
    return status or "retryable_error"


def process_request(raw: bytes, config: HostConfig, *, runner: Any = subprocess.run) -> str:
    try:
        providers = validate_request(raw)
    except ProtocolError:
        return "rejected_invalid"
    return send_to_server(providers, config, runner=runner)


def _safe_stderr(stderr: TextIO, code: str) -> None:
    """Emit a fixed diagnostic, never exception text or command output."""

    if code not in {
        "invalid_frame",
        "invalid_request",
        "configuration",
        "ssh_failed",
        "write_failed",
    }:
        code = "internal_error"
    try:
        stderr.write(f"rsshub-cookie-sync: {code}\n")
        stderr.flush()
    except (AttributeError, OSError):
        pass


def run_host(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    stdin: Optional[BinaryIO] = None,
    stdout: Optional[BinaryIO] = None,
    stderr: Optional[TextIO] = None,
    runner: Any = subprocess.run,
) -> int:
    """Run the Native Messaging loop until Edge closes the port."""

    in_stream = stdin if stdin is not None else sys.stdin.buffer
    out_stream = stdout if stdout is not None else sys.stdout.buffer
    err_stream = stderr if stderr is not None else sys.stderr
    try:
        config = load_config(config_path)
    except ConfigurationError:
        config = None
        _safe_stderr(err_stream, "configuration")

    while True:
        try:
            frame = read_frame(in_stream)
        except (OSError, ProtocolError):
            try:
                write_status(out_stream, "rejected_invalid")
            except (BrokenPipeError, OSError):
                _safe_stderr(err_stream, "write_failed")
            _safe_stderr(err_stream, "invalid_frame")
            return 2
        if frame is None:
            return 0

        if config is None:
            status = "retryable_error"
        else:
            status = process_request(frame, config, runner=runner)
        try:
            write_status(out_stream, status)
        except (BrokenPipeError, OSError):
            _safe_stderr(err_stream, "write_failed")
            return 2


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RSSHub Cookie Sync Edge Native Messaging host")
    # Chromium launches a Native Messaging host with the calling extension's
    # origin as the first positional argument.  The manifest's
    # ``allowed_origins`` remains the authorization boundary; accepting the
    # bounded browser-supplied argument here prevents argparse from exiting
    # before the Native Messaging frame can be read.
    parser.add_argument("origin", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="path to the local host configuration JSON",
    )
    args = parser.parse_args(argv)
    if args.origin is not None and not _EXTENSION_ORIGIN_RE.fullmatch(args.origin):
        parser.error("invalid extension origin")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    return run_host(config_path=args.config)


if __name__ == "__main__":
    raise SystemExit(main())
