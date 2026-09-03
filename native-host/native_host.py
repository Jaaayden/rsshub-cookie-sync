#!/usr/bin/env python3
"""Microsoft Edge Native Messaging host for RSSHub Cookie Sync.

The host deliberately has a very small surface area:

* Native Messaging uses the Chromium little-endian, four-byte length frame.
* Synchronisation accepts only the versioned provider payload; the separate
  get-config/set-config control messages are handled locally and never reach
  the server.
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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Mapping, Optional, Sequence, TextIO, Tuple


HOST_NAME = "com.jayden.rsshub_cookie_sync"
DEFAULT_SERVER_PORT = 22
DEFAULT_SERVER_USER = "rsshub-sync"
DEFAULT_CONNECT_TIMEOUT = 15
MAX_CONNECT_TIMEOUT = 120
DEFAULT_EXTENSION_ID = "ohpnejcdmchhchkamammonikfbmfpiam"

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
# Keep the SSH material in the user's normal SSH directory.  The private key
# is never read by the extension; this path is only handed to OpenSSH by the
# Native Messaging host.  Operators may point the installer at another file
# directly below ``~/.ssh`` when reusing an existing key.
DEFAULT_SSH_DIR = Path.home() / ".ssh"
DEFAULT_IDENTITY_FILE = DEFAULT_SSH_DIR / "rsshub-cookie-sync"
DEFAULT_KNOWN_HOSTS_FILE = DEFAULT_SSH_DIR / "known_hosts"

# The browser can legitimately send a fairly large Cookie header, but an
# unbounded Native Messaging frame would make the host an easy memory DoS.
# Keep the frame bounded independently of the cookie-header limit.  A single
# request may contain both provider headers, so the frame budget is larger
# than one header but still small enough for a browser-launched helper.
MAX_FRAME_BYTES = 1024 * 1024
MAX_COOKIE_BYTES = 128 * 1024
MAX_IDENTITIES = 256

ALLOWED_PROVIDERS = frozenset(("zhihu", "weibo"))
ALLOWED_REMOTE_STATUSES = frozenset(
    ("unchanged", "candidate_saved", "promoted", "rejected_invalid", "retryable_error")
)
ALLOWED_CONTROL_STATUSES = frozenset(
    ("config", "config_saved", "config_error", "rejected_invalid")
)
# A control-protocol sentinel, not a real filename.  Reserve and exclude it
# from ~/.ssh scanning so a user file called "legacy" can never be confused
# with the private key kept by a v1.0 installation.
LEGACY_IDENTITY_NAME = "__rsshub_cookie_sync_legacy__"

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_IDENTITY_NAME_RE = re.compile(r"^[A-Za-z0-9._+\-]+$")
_EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")


class ProtocolError(ValueError):
    """An invalid Native Messaging frame or request payload."""


class ConfigurationError(ValueError):
    """A missing or unsafe local SSH configuration."""


@dataclass(frozen=True)
class HostConfig:
    """Validated values needed to make one SSH connection."""

    # The deployment-specific server endpoint is intentionally optional.  A
    # fresh zero-argument install leaves it unset until the Edge Options page
    # supplies the operator's own endpoint; a missing value must never
    # silently target another operator's server.
    server_host: Optional[str] = None
    server_port: int = DEFAULT_SERVER_PORT
    server_user: str = DEFAULT_SERVER_USER
    identity_file: Path = DEFAULT_IDENTITY_FILE
    known_hosts_file: Path = DEFAULT_KNOWN_HOSTS_FILE
    ssh_binary: str = "/usr/bin/ssh"
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT


def _runtime_default_config_path(
    module_file: Optional[Path] = None,
    fallback: Optional[Path] = None,
) -> Path:
    """Use the config installed beside the host, including custom locations."""

    sibling = Path(module_file or __file__).resolve().with_name("config.json")
    if os.path.lexists(str(sibling)):
        return sibling
    return fallback or DEFAULT_CONFIG_PATH


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
    # The endpoint is placed in an SSH argv item.  Keep it to a hostname/IP
    # grammar; no shell metacharacters or whitespace can enter the command.
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host) or host.startswith(".") or host.endswith("."):
        raise ConfigurationError("invalid endpoint")
    if user is not None and (not isinstance(user, str) or not _USER_RE.fullmatch(user)):
        raise ConfigurationError("invalid SSH user")


def _validate_server_user(user: Any) -> str:
    """Require the dedicated forced-command account for every SSH session.

    The server-side installer provisions ``rsshub-sync`` with a forced
    command and no interactive shell.  A user value from an old or manually
    edited config must therefore never be allowed to select ``root`` (or any
    other account), even if it otherwise matches the normal SSH username
    grammar.
    """

    if user != DEFAULT_SERVER_USER:
        raise ConfigurationError("unsupported SSH user")
    return DEFAULT_SERVER_USER


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
    text after ``-o`` again using ssh_config token rules.  Paths containing
    spaces therefore need config-level quoting even though no shell is
    involved.  Escape the two characters that are special inside a
    double-quoted ssh_config token.
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
    server = raw.get("server", {})
    ssh = raw.get("ssh", {})
    if server is None:
        server = {}
    if not isinstance(server, dict) or not isinstance(ssh, dict):
        raise ConfigurationError("invalid configuration")

    # v1.0 wrote a ``proxy`` section.  It is deliberately ignored on read so
    # an upgrade remains usable; every configuration written by this module
    # omits it and therefore permanently migrates the installation to direct
    # SSH.  No proxy-related value is ever passed to OpenSSH.

    raw_host = server.get("host")
    if raw_host is None or raw_host == "":
        server_host: Optional[str] = None
    else:
        server_host = _string(raw_host, "server host")
    server_user = _validate_server_user(
        _string(server.get("user", DEFAULT_SERVER_USER), "server user")
    )
    server_port = _port(server.get("port", DEFAULT_SERVER_PORT))

    identity = ssh.get("identity_file", str(DEFAULT_IDENTITY_FILE))
    known_hosts = ssh.get("known_hosts_file", str(DEFAULT_KNOWN_HOSTS_FILE))
    ssh_binary = _validate_binary(_string(ssh.get("binary", "/usr/bin/ssh"), "ssh binary"))
    timeout = ssh.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_CONNECT_TIMEOUT:
        raise ConfigurationError("invalid SSH timeout")

    if server_host is not None:
        _validate_endpoint(server_host, server_user)
    return HostConfig(
        server_host=server_host,
        server_port=server_port,
        server_user=server_user,
        identity_file=_config_path(identity, base_dir),
        known_hosts_file=_config_path(known_hosts, base_dir),
        ssh_binary=ssh_binary,
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


def _read_config_mapping(path: Path) -> Dict[str, Any]:
    """Read one trusted config file without exposing its contents."""

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
    return raw


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> HostConfig:
    """Load and validate an installer-generated config file.

    A missing file is treated as an incomplete installation.  The host loop
    turns that configuration error into ``retryable_error`` until the
    installer has written a server endpoint, key, and known_hosts file.
    """

    path = _default_path(path)
    raw = _read_config_mapping(path)
    return config_from_mapping(raw, config_path=path)


def load_config_for_migration(path: Path = DEFAULT_CONFIG_PATH) -> HostConfig:
    """Load legacy metadata while replacing its SSH user with the safe one.

    Runtime code must use :func:`load_config`, which rejects every account
    except ``rsshub-sync``.  The installer and a user-initiated set-config
    request may use this narrow migration path so an old custom-user config
    can be repaired without ever placing that legacy username in SSH argv.
    """

    path = _default_path(path)
    raw = _read_config_mapping(path)
    server = raw.get("server")
    if not isinstance(server, dict):
        raise ConfigurationError("invalid server configuration")
    migrated = dict(raw)
    migrated_server = dict(server)
    migrated_server["user"] = DEFAULT_SERVER_USER
    migrated["server"] = migrated_server
    return config_from_mapping(migrated, config_path=path)


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


def _has_safe_ed25519_public_key(identity: Path) -> bool:
    """Confirm that an identity has a safe Ed25519 public-key sidecar.

    The server provisioning command intentionally accepts only Ed25519.  The
    Options page must therefore not offer RSA/ECDSA identities that can never
    be installed on ``rsshub-sync``.  This check inspects only the public
    sidecar and never returns key material.
    """

    public_key = identity.with_name(f"{identity.name}.pub")
    try:
        info = public_key.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return False
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            return False
        if not 1 <= info.st_size <= 8192:
            return False
        payload = public_key.read_bytes()
    except (OSError, ValueError):
        return False
    if b"\x00" in payload or b"\r" in payload:
        return False
    lines = payload.strip().split(b"\n")
    if len(lines) != 1:
        return False
    fields = lines[0].split()
    return len(fields) >= 2 and fields[0] == b"ssh-ed25519"


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
    """Check the private key and known_hosts files used by SSH."""

    _safe_key_file(config.identity_file)
    _safe_known_hosts_file(config.known_hosts_file)


def build_ssh_argv(config: HostConfig) -> Tuple[str, ...]:
    """Return a fully explicit, shell-free SSH command.

    The server account is expected to use an ``authorized_keys`` forced
    command.  No remote command is appended here, so the forced command is the
    only server-side operation available to this host.
    """

    _port(config.server_port)
    if config.server_host is None:
        raise ConfigurationError("server endpoint is not configured")
    _validate_server_user(config.server_user)
    _validate_endpoint(config.server_host, config.server_user)
    if not isinstance(config.connect_timeout, int) or isinstance(config.connect_timeout, bool):
        raise ConfigurationError("invalid SSH timeout")
    if not 1 <= config.connect_timeout <= MAX_CONNECT_TIMEOUT:
        raise ConfigurationError("invalid SSH timeout")
    _validate_binary(config.ssh_binary)
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


def _parse_json_object(raw: bytes) -> Dict[str, Any]:
    """Decode one control message with duplicate-key protection."""

    try:
        message = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ProtocolError, RecursionError) as exc:
        raise ProtocolError("invalid control JSON") from exc
    if not isinstance(message, dict):
        raise ProtocolError("control request must be an object")
    return message


def _validate_identity_name(value: Any) -> str:
    """Validate the public identity selector, never a filesystem path."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value in {".", ".."}
        or not _IDENTITY_NAME_RE.fullmatch(value)
    ):
        raise ProtocolError("invalid identity name")
    return value


def validate_control_request(raw: bytes) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Validate the small, local-only get/set configuration protocol.

    Synchronisation payloads are intentionally handled by :func:`validate_request`
    and are not accepted here.  Keeping the two exact shapes separate prevents
    a future control field from accidentally crossing the server wire.
    """

    message = _parse_json_object(raw)
    version = message.get("version")
    if type(version) is not int or version != 1:
        raise ProtocolError("unsupported control version")
    action = message.get("action")
    if action == "get-config":
        if set(message) != {"version", "action"}:
            raise ProtocolError("invalid get-config request")
        return action, None
    if action != "set-config":
        raise ProtocolError("unknown control action")
    if set(message) != {"version", "action", "server", "identityName"}:
        raise ProtocolError("invalid set-config request")
    server = message.get("server")
    if not isinstance(server, dict) or set(server) != {"host", "port", "user"}:
        raise ProtocolError("invalid server configuration")
    host = server.get("host")
    user = server.get("user")
    port = server.get("port")
    if not isinstance(host, str) or not host:
        raise ProtocolError("invalid server host")
    if not isinstance(user, str) or not user:
        raise ProtocolError("invalid server user")
    # The server installer grants this one account a forced command.  Letting
    # a browser message select root or an ordinary shell account would bypass
    # that server-side capability boundary.
    if user != DEFAULT_SERVER_USER:
        raise ProtocolError("invalid server user")
    try:
        _validate_endpoint(host, user)
        _port(port)
    except (ConfigurationError, TypeError) as exc:
        raise ProtocolError("invalid server configuration") from exc
    identity_name = _validate_identity_name(message.get("identityName"))
    return action, {"host": host, "port": port, "user": user, "identityName": identity_name}


def _is_direct_ssh_identity(path: Path) -> Optional[str]:
    """Return a safe one-level name when ``path`` is directly under ~/.ssh."""

    try:
        relative = _default_path(path).relative_to(_default_path(DEFAULT_SSH_DIR))
    except ValueError:
        return None
    if len(relative.parts) != 1:
        return None
    name = relative.name
    if name in {"", ".", ".."} or not _IDENTITY_NAME_RE.fullmatch(name):
        return None
    return name


def _legacy_identity_path(config: HostConfig) -> Optional[Path]:
    """Return the current non-~/.ssh identity when it is safe to use."""

    path = _default_path(config.identity_file)
    if _is_direct_ssh_identity(path) is not None:
        return None
    try:
        _safe_key_file(path)
    except ConfigurationError:
        return None
    if not _has_safe_ed25519_public_key(path):
        return None
    return path


def _identity_looks_private(path: Path) -> bool:
    """Recognise selectable identities without reading private-key bytes."""

    # _safe_key_file() has already established that `path` is a protected
    # regular file.  The paired public key is sufficient to filter algorithm
    # support; OpenSSH remains the authority on the private file itself.
    return _has_safe_ed25519_public_key(path)


def scan_ssh_identities() -> Tuple[Mapping[str, Any], ...]:
    """List safe private keys directly below ~/.ssh for the Options page.

    Only a filename and a legacy marker leave this function.  It never returns
    a path, fingerprint, key material, or an exception string.
    """

    root = _default_path(DEFAULT_SSH_DIR)
    try:
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return ()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            return ()
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except (OSError, ValueError):
        return ()

    identities = []
    ignored = {
        "config",
        "known_hosts",
        "known_hosts.old",
        "authorized_keys",
        LEGACY_IDENTITY_NAME,
    }
    for path in entries:
        name = path.name
        if name in ignored or name.endswith(".pub") or not _IDENTITY_NAME_RE.fullmatch(name):
            continue
        try:
            _safe_key_file(path)
        except ConfigurationError:
            continue
        if _identity_looks_private(path):
            identities.append({"name": name, "legacy": False})
    return tuple(identities[:MAX_IDENTITIES])


def _identity_name_for_config(config: HostConfig) -> Optional[str]:
    direct = _is_direct_ssh_identity(config.identity_file)
    if direct is not None:
        try:
            _safe_key_file(_default_path(config.identity_file))
        except ConfigurationError:
            return None
        if not _has_safe_ed25519_public_key(_default_path(config.identity_file)):
            return None
        return direct
    if _legacy_identity_path(config) is not None:
        return LEGACY_IDENTITY_NAME
    return None


def _public_config(config: HostConfig) -> Mapping[str, Any]:
    """Build the only configuration shape exposed to Edge."""

    identity_name = _identity_name_for_config(config)
    identities = list(scan_ssh_identities())
    if identity_name == LEGACY_IDENTITY_NAME:
        identities.append({"name": LEGACY_IDENTITY_NAME, "legacy": True})
    elif identity_name and not any(item["name"] == identity_name for item in identities):
        # A valid current key can be in a format that the directory scanner
        # cannot classify.  Show only its one-level name, never its path.
        identities.insert(0, {"name": identity_name, "legacy": False})
    elif identity_name is None and identities:
        # If the configured key was removed, keep the Options page usable so
        # the operator can select another safe key and repair the config.  This
        # is only a suggested form selection; nothing is persisted until the
        # user explicitly presses Save.
        preferred = next(
            (item for item in identities if item["name"] == "rsshub-cookie-sync"),
            identities[0],
        )
        identity_name = preferred["name"]
    identities.sort(key=lambda item: (item["name"] != "rsshub-cookie-sync", item["name"]))
    if len(identities) > MAX_IDENTITIES:
        # The selected identity is always retained, including the legacy
        # marker, even on a machine with an unusually large ~/.ssh directory.
        selected = next((item for item in identities if item["name"] == identity_name), None)
        identities = [item for item in identities if item["name"] != identity_name]
        identities = identities[: max(0, MAX_IDENTITIES - 1)]
        if selected is not None:
            identities.append(selected)
        identities.sort(key=lambda item: (item["name"] != "rsshub-cookie-sync", item["name"]))
    return {
        "server": {
            "host": config.server_host,
            "port": config.server_port,
            "user": config.server_user,
        },
        "identityName": identity_name,
        "identities": identities,
    }


def _safe_config_parent(path: Path) -> Path:
    parent = _default_path(path).parent
    try:
        info = parent.lstat()
    except (OSError, ValueError, UnicodeError) as exc:
        raise ConfigurationError("configuration directory unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ConfigurationError("configuration directory is not regular")
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ConfigurationError("configuration directory permissions are unsafe")
    return parent


def _atomic_write_config(path: Path, raw: Mapping[str, Any]) -> None:
    """Replace config atomically with mode 0600 and no proxy section."""

    path = _default_path(path)
    parent = _safe_config_parent(path)
    if os.path.lexists(str(path)):
        _safe_config_file(path)
    # Validate and normalise before writing.  In particular this drops an old
    # non-empty proxy section during the first successful Options update.
    config = config_from_mapping(dict(raw), config_path=path)
    normalized: Dict[str, Any] = {
        "schema_version": 1,
        "host_name": HOST_NAME,
        "server": {
            "host": config.server_host,
            "port": config.server_port,
            "user": config.server_user,
        },
        "ssh": {
            "binary": config.ssh_binary,
            "identity_file": str(config.identity_file),
            "known_hosts_file": str(config.known_hosts_file),
            "connect_timeout": config.connect_timeout,
        },
    }
    payload = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd = -1
    temporary: Optional[Path] = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except (OSError, ValueError) as exc:
        raise ConfigurationError("cannot write configuration") from exc
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _resolve_identity_name(name: str, current: HostConfig) -> Path:
    if name == LEGACY_IDENTITY_NAME:
        path = _legacy_identity_path(current)
        if path is None:
            raise ConfigurationError("legacy SSH identity unavailable")
        return path
    if name in {"config", "known_hosts", "known_hosts.old", "authorized_keys"}:
        raise ConfigurationError("SSH identity unavailable")
    # Do not let a browser message choose an arbitrary path.  The selector is
    # a single filename below ~/.ssh, and the existing file still has to pass
    # ownership, regular-file, and 0600-or-stricter checks.
    try:
        path = _default_path(DEFAULT_SSH_DIR / name)
        relative = path.relative_to(_default_path(DEFAULT_SSH_DIR))
    except (ValueError, OSError) as exc:
        raise ConfigurationError("SSH identity unavailable") from exc
    if len(relative.parts) != 1 or relative.name != name:
        raise ConfigurationError("SSH identity unavailable")
    _safe_key_file(path)
    if not _identity_looks_private(path):
        raise ConfigurationError("SSH identity unavailable")
    return path


def _mapping_for_update(current: HostConfig, update: Mapping[str, Any], identity: Path) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "host_name": HOST_NAME,
        "server": {
            "host": update["host"],
            "port": update["port"],
            "user": update["user"],
        },
        "ssh": {
            "binary": current.ssh_binary,
            "identity_file": str(identity),
            "known_hosts_file": str(current.known_hosts_file),
            "connect_timeout": current.connect_timeout,
        },
    }


def process_control_request(raw: bytes, config_path: Path = DEFAULT_CONFIG_PATH) -> Mapping[str, Any]:
    """Handle get/set locally; this function never invokes SSH."""

    try:
        action, update = validate_control_request(raw)
    except ProtocolError:
        return {"status": "rejected_invalid"}

    try:
        current = (
            load_config(config_path)
            if action == "get-config"
            else load_config_for_migration(config_path)
        )
        if action == "get-config":
            public = _public_config(current)
            if public["identityName"] is None:
                return {"status": "config_error"}
            return {"status": "config", **public}
        assert update is not None
        identity = _resolve_identity_name(str(update["identityName"]), current)
        _atomic_write_config(config_path, _mapping_for_update(current, update, identity))
        updated = load_config(config_path)
        public = _public_config(updated)
        if public["identityName"] is None:
            return {"status": "config_error"}
        return {"status": "config_saved", **public}
    except (ConfigurationError, OSError, ValueError, TypeError, KeyError):
        # Never return local paths, exception text, or configuration contents
        # to the extension.  The fixed status tells the UI to show a generic
        # actionable error.
        return {"status": "config_error"}


def _looks_like_control_request(raw: bytes) -> bool:
    """Select the control response channel without accepting loose aliases."""

    try:
        message = _parse_json_object(raw)
    except ProtocolError:
        return False
    return "action" in message


def write_control_response(stream: BinaryIO, response: Mapping[str, Any]) -> None:
    """Write a strictly shaped local configuration response."""

    status = response.get("status")
    if status not in ALLOWED_CONTROL_STATUSES:
        response = {"status": "config_error"}
    elif status in {"rejected_invalid", "config_error"}:
        response = {"status": status}
    else:
        # Rebuild from the approved public fields so a future caller cannot
        # accidentally put a path, private key, Cookie, or raw exception in
        # a response.  Values are validated once more at the boundary.
        server = response.get("server")
        identities = response.get("identities")
        identity_name = response.get("identityName")
        try:
            if not isinstance(server, dict) or set(server) != {"host", "port", "user"}:
                raise ValueError
            host = server.get("host")
            user = _validate_server_user(server.get("user"))
            if host is not None:
                _validate_endpoint(host, user)
            _port(server.get("port"))
            if not isinstance(identity_name, str):
                raise ValueError
            if identity_name != LEGACY_IDENTITY_NAME:
                _validate_identity_name(identity_name)
            if not isinstance(identities, (list, tuple)):
                raise ValueError
            clean_identities = []
            for item in identities:
                if not isinstance(item, dict) or set(item) != {"name", "legacy"}:
                    raise ValueError
                name = item.get("name")
                if name != LEGACY_IDENTITY_NAME:
                    _validate_identity_name(name)
                if type(item.get("legacy")) is not bool:
                    raise ValueError
                clean_identities.append({"name": name, "legacy": item["legacy"]})
            response = {
                "status": status,
                "server": {
                    "host": host,
                    "port": server["port"],
                    "user": user,
                },
                "identityName": identity_name,
                "identities": clean_identities,
            }
        except (ConfigurationError, ProtocolError, TypeError, ValueError):
            response = {"status": "config_error"}
    payload = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_FRAME_BYTES:
        payload = b'{"status":"config_error"}'
    try:
        stream.write(encode_frame(payload))
        flush = getattr(stream, "flush", None)
        if callable(flush):
            flush()
    except (BrokenPipeError, OSError):
        raise


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

        if _looks_like_control_request(frame):
            response = process_control_request(frame, config_path)
            if response.get("status") == "config_saved":
                # A long-lived connectNative session must use the new endpoint
                # for the very next synchronisation request.  sendNativeMessage
                # launches a fresh process, but reloading here keeps both APIs
                # correct and makes the invariant explicit.
                try:
                    config = load_config(config_path)
                except ConfigurationError:
                    config = None
            try:
                write_control_response(out_stream, response)
            except (BrokenPipeError, OSError):
                _safe_stderr(err_stream, "write_failed")
                return 2
            continue

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
        default=_runtime_default_config_path(),
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
