#!/usr/bin/env python3
"""Install the RSSHub Cookie Sync Native Messaging host for Microsoft Edge.

The installer is intentionally local-only.  It never contacts the server and
never runs ``ssh-keyscan``: the fixed server host key must be supplied by the
operator through ``--known-hosts-source`` (or placed in the destination file
out of band) before the host can connect with StrictHostKeyChecking enabled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from native_host import (
        DEFAULT_APP_SUPPORT_DIR,
        DEFAULT_CONNECT_TIMEOUT,
        MAX_CONNECT_TIMEOUT,
        DEFAULT_SERVER_PORT,
        DEFAULT_SERVER_USER,
        HOST_NAME,
    )
except ImportError:  # pragma: no cover - supports direct path execution
    from .native_host import (  # type: ignore
        DEFAULT_APP_SUPPORT_DIR,
        DEFAULT_CONNECT_TIMEOUT,
        MAX_CONNECT_TIMEOUT,
        DEFAULT_SERVER_PORT,
        DEFAULT_SERVER_USER,
        HOST_NAME,
    )


DEFAULT_EDGE_MANIFEST_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Microsoft Edge"
    / "NativeMessagingHosts"
)
DEFAULT_SSH_BINARY = "/usr/bin/ssh"
DEFAULT_NC_BINARY = "/usr/bin/nc"
EXTENSION_ID_LENGTH = 32


class InstallError(RuntimeError):
    """A safe, user-actionable installation error."""


def _safe_path(path: Path) -> Path:
    return path.expanduser().absolute()


def _managed_directory(path: Path, *, allowed_root: Path, label: str) -> Path:
    """Constrain installer-managed directories below one trusted user root.

    The installer chmods its leaf directories.  Rejecting `/`, the home
    directory itself, paths outside that root, and symlinked existing
    components prevents a mistyped customization from changing a broad system
    directory or redirecting writes elsewhere.
    """

    candidate = _safe_path(path)
    root = _safe_path(allowed_root)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise InstallError(f"{label} 必须位于当前用户目录内") from exc
    if not relative.parts:
        raise InstallError(f"{label} 不能是当前用户目录本身")
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            raise InstallError(f"{label} 包含符号链接")
    return candidate


def _assert_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise InstallError("拒绝操作符号链接路径")


def _ensure_dir(path: Path, mode: int = 0o700) -> None:
    _assert_not_symlink(path)
    path.mkdir(parents=True, exist_ok=True)
    _assert_not_symlink(path)
    os.chmod(path, mode)


def _atomic_write_bytes(path: Path, payload: bytes, mode: int) -> None:
    _ensure_dir(path.parent)
    _assert_not_symlink(path)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_file_atomic(source: Path, destination: Path, mode: int) -> None:
    if not source.is_file() or source.is_symlink():
        raise InstallError("Native host 源文件不存在或不安全")
    _ensure_dir(destination.parent)
    _assert_not_symlink(destination)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, mode)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_extension_id(value: str) -> str:
    # Chromium extension IDs use the alphabet a-p.  Requiring the exact
    # length prevents accidentally granting access to a broad or malformed
    # origin in the Native Messaging manifest.
    if len(value) != EXTENSION_ID_LENGTH or any(char < "a" or char > "p" for char in value):
        raise InstallError("--extension-id 必须是 32 位小写 a-p 扩展 ID")
    return value


def _validate_endpoint(value: str, *, label: str) -> str:
    if not value or len(value) > 253 or any(
        not (char.isalnum() or char in ".-") for char in value
    ) or not value.isascii():
        raise InstallError(f"{label} 不合法")
    if value.startswith(".") or value.endswith("."):
        raise InstallError(f"{label} 不合法")
    return value


def _validate_user(value: str) -> str:
    if (
        not value
        or len(value) > 64
        or not value.isascii()
        or not (value[0].isalpha() or value[0] == "_")
        or any(not (char.isalnum() or char in "_.-") for char in value)
    ):
        raise InstallError("--server-user 不合法")
    return value


def _validate_port(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise InstallError(f"{label} 不合法")
    return value


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CONNECT_TIMEOUT:
        raise InstallError(f"--connect-timeout 必须是 1 到 {MAX_CONNECT_TIMEOUT} 秒")
    return value


def _validate_binary(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9._/+-]+", value):
        raise InstallError(f"{label} 不合法")
    return value


def _validate_known_hosts_entry(payload: bytes, *, server_host: str, server_port: int) -> None:
    """Require an exact, unhashed entry for the configured SSH endpoint.

    OpenSSH emits the bare host for the default port and ``[host]:port`` for a
    non-default port.  Accept both spellings for port 22, but never accept a
    wildcard, negated pattern, or hashed host as the only match.  The
    operator still must verify the key fingerprint out of band; this check
    prevents accidentally installing a file for a different host.
    """

    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InstallError("known_hosts 只能包含 ASCII 主机条目") from exc
    expected = {f"[{server_host}]:{server_port}"}
    if server_port == 22:
        expected.add(server_host)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        host_index = 1 if fields and fields[0].startswith("@") else 0
        if len(fields) < host_index + 3:
            continue
        host_patterns = fields[host_index].split(",")
        key_type = fields[host_index + 1]
        if key_type == "ssh-ed25519" and any(pattern in expected for pattern in host_patterns):
            return
    raise InstallError("known_hosts 未包含目标服务器的精确 Ed25519 host:port 条目")


def _read_known_hosts_source(source: Path, *, server_host: str, server_port: int) -> bytes:
    _assert_not_symlink(source)
    try:
        info = source.stat()
    except OSError as exc:
        raise InstallError("known_hosts 源文件不可读") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise InstallError("known_hosts 源文件不合法")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise InstallError("known_hosts 源文件不可读") from exc
    if b"\x00" in payload:
        raise InstallError("known_hosts 源文件包含 NUL")
    _validate_known_hosts_entry(payload, server_host=server_host, server_port=server_port)
    return payload


def _generate_identity(key_path: Path) -> None:
    """Generate an Ed25519 key with the system ssh-keygen, never a shell."""

    _ensure_dir(key_path.parent, 0o700)
    _assert_not_symlink(key_path)
    if key_path.exists():
        if not key_path.is_file():
            raise InstallError("SSH 密钥路径不是普通文件")
        os.chmod(key_path, 0o600)
        return
    old_umask = os.umask(0o077)
    try:
        try:
            result = subprocess.run(
                [
                    "/usr/bin/ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-f",
                    str(key_path),
                    "-N",
                    "",
                    "-C",
                    "rsshub-cookie-sync",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError("无法调用 /usr/bin/ssh-keygen") from exc
    finally:
        os.umask(old_umask)
    if getattr(result, "returncode", 1) != 0 or not key_path.is_file():
        raise InstallError("Ed25519 密钥生成失败")
    os.chmod(key_path, 0o600)


def _ensure_known_hosts(
    destination: Path,
    source: Optional[Path],
    *,
    server_host: str,
    server_port: int,
) -> bool:
    _ensure_dir(destination.parent, 0o700)
    if source is not None:
        _atomic_write_bytes(
            destination,
            _read_known_hosts_source(source, server_host=server_host, server_port=server_port),
            0o600,
        )
        return True
    if destination.exists():
        _assert_not_symlink(destination)
        if not destination.is_file():
            raise InstallError("known_hosts 目标不是普通文件")
        os.chmod(destination, 0o600)
        if destination.stat().st_size == 0:
            return False
        try:
            payload = destination.read_bytes()
        except OSError as exc:
            raise InstallError("known_hosts 目标不可读") from exc
        _validate_known_hosts_entry(payload, server_host=server_host, server_port=server_port)
        return True
    # An empty file is deliberately created instead of silently trusting a
    # key learned over the network.  StrictHostKeyChecking=yes will keep the
    # connection disabled until the operator provisions the expected key.
    _atomic_write_bytes(destination, b"", 0o600)
    return False


def build_manifest(host_path: Path, extension_id: str) -> Mapping[str, Any]:
    return {
        "name": HOST_NAME,
        "description": "RSSHub Cookie Sync Native Messaging host",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{_validate_extension_id(extension_id)}/"],
    }


def build_config(
    *,
    identity_file: Path,
    known_hosts_file: Path,
    server_host: str,
    server_port: int = DEFAULT_SERVER_PORT,
    server_user: str = DEFAULT_SERVER_USER,
    proxy_host: Optional[str] = None,
    proxy_port: Optional[int] = None,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    ssh_binary: str = DEFAULT_SSH_BINARY,
    nc_binary: str = DEFAULT_NC_BINARY,
) -> Mapping[str, Any]:
    if (proxy_host is None) != (proxy_port is None):
        raise InstallError("--proxy-host 与 --proxy-port 必须同时提供")
    return {
        "schema_version": 1,
        "host_name": HOST_NAME,
        "server": {
            "host": _validate_endpoint(server_host, label="--server-host"),
            "port": _validate_port(server_port, "--server-port"),
            "user": _validate_user(server_user),
        },
        "proxy": (
            None
            if proxy_host is None
            else {
                "type": "socks5",
                "host": _validate_endpoint(proxy_host, label="--proxy-host"),
                "port": _validate_port(proxy_port, "--proxy-port"),
            }
        ),
        "ssh": {
            "binary": _validate_binary(ssh_binary, "--ssh-binary"),
            "nc_binary": _validate_binary(nc_binary, "--nc-binary"),
            "identity_file": str(identity_file),
            "known_hosts_file": str(known_hosts_file),
            "connect_timeout": _validate_timeout(connect_timeout),
        },
    }


def install(
    *,
    extension_id: str,
    app_support_dir: Path = DEFAULT_APP_SUPPORT_DIR,
    edge_manifest_dir: Path = DEFAULT_EDGE_MANIFEST_DIR,
    known_hosts_source: Optional[Path] = None,
    server_host: str,
    server_port: int = DEFAULT_SERVER_PORT,
    server_user: str = DEFAULT_SERVER_USER,
    proxy_host: Optional[str] = None,
    proxy_port: Optional[int] = None,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    no_generate_key: bool = False,
    source_host: Optional[Path] = None,
    allowed_root: Optional[Path] = None,
) -> Mapping[str, Any]:
    """Install local files and return their paths.

    This function has no network side effects.  It is separate from CLI
    parsing so tests can run it with a temporary directory and a mocked
    ``ssh-keygen``.
    """

    extension_id = _validate_extension_id(extension_id)
    # Validate all endpoint options before creating or replacing any local
    # files.  This keeps a typo from leaving a half-installed host behind.
    _validate_endpoint(server_host, label="--server-host")
    _validate_port(server_port, "--server-port")
    _validate_user(server_user)
    if (proxy_host is None) != (proxy_port is None):
        raise InstallError("--proxy-host 与 --proxy-port 必须同时提供")
    if proxy_host is not None:
        _validate_endpoint(proxy_host, label="--proxy-host")
        _validate_port(proxy_port, "--proxy-port")
    _validate_timeout(connect_timeout)
    trusted_root = _safe_path(allowed_root or Path.home())
    app_support_dir = _managed_directory(
        app_support_dir,
        allowed_root=trusted_root,
        label="--app-support-dir",
    )
    edge_manifest_dir = _managed_directory(
        edge_manifest_dir,
        allowed_root=trusted_root,
        label="--edge-manifest-dir",
    )
    _ensure_dir(app_support_dir, 0o700)
    _ensure_dir(edge_manifest_dir, 0o700)

    # The installed manifest must launch the protocol host itself.  Using
    # ``Path(__file__)`` here would copy this installer into Application
    # Support, which would leave Edge invoking install.py as a Native host.
    source_host = source_host or Path(__file__).with_name("native_host.py").resolve()
    host_path = app_support_dir / "native_host.py"
    config_path = app_support_dir / "config.json"
    ssh_dir = app_support_dir / "ssh"
    identity_file = ssh_dir / "id_ed25519"
    known_hosts_file = ssh_dir / "known_hosts"
    manifest_path = edge_manifest_dir / f"{HOST_NAME}.json"

    _copy_file_atomic(source_host, host_path, 0o700)
    if no_generate_key:
        if not identity_file.is_file() or identity_file.is_symlink():
            raise InstallError("未找到专用 SSH 密钥；请移除 --no-generate-key 或先准备密钥")
        os.chmod(identity_file, 0o600)
    else:
        _generate_identity(identity_file)
    known_hosts_ready = _ensure_known_hosts(
        known_hosts_file,
        known_hosts_source,
        server_host=server_host,
        server_port=server_port,
    )

    config = build_config(
        identity_file=identity_file,
        known_hosts_file=known_hosts_file,
        server_host=server_host,
        server_port=server_port,
        server_user=server_user,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        connect_timeout=connect_timeout,
    )
    _atomic_write_bytes(
        config_path,
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        0o600,
    )
    manifest = build_manifest(host_path, extension_id)
    _atomic_write_bytes(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        0o600,
    )
    return {
        "app_support_dir": app_support_dir,
        "host_path": host_path,
        "config_path": config_path,
        "identity_file": identity_file,
        "known_hosts_file": known_hosts_file,
        "manifest_path": manifest_path,
        "known_hosts_ready": known_hosts_ready,
    }


def _public_key(identity_file: Path) -> str:
    public_file = Path(f"{identity_file}.pub")
    if not public_file.is_file() or public_file.is_symlink():
        return ""
    try:
        # Public key material is not secret, but cap output and remove all
        # accidental line breaks from the CLI presentation.
        value = public_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    return value[:8192]


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="安装 RSSHub Cookie Sync Edge Native Messaging host")
    parser.add_argument("--extension-id", required=True, help="固定的 32 位 Edge 扩展 ID")
    parser.add_argument("--app-support-dir", type=Path, default=DEFAULT_APP_SUPPORT_DIR)
    parser.add_argument("--edge-manifest-dir", type=Path, default=DEFAULT_EDGE_MANIFEST_DIR)
    parser.add_argument(
        "--known-hosts-source",
        type=Path,
        help="预先获取并核对过的 SSH known_hosts 文件；不会自动 ssh-keyscan",
    )
    parser.add_argument("--server-host", required=True)
    parser.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
    parser.add_argument("--server-user", default=DEFAULT_SERVER_USER)
    parser.add_argument(
        "--proxy-host",
        help="可选的本机 SOCKS5 代理主机；必须与 --proxy-port 一起提供",
    )
    parser.add_argument(
        "--proxy-port",
        type=int,
        help="可选的本机 SOCKS5 代理端口；必须与 --proxy-host 一起提供",
    )
    parser.add_argument("--connect-timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument(
        "--no-generate-key",
        action="store_true",
        help="不调用 ssh-keygen，要求目标密钥已经存在",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        paths = install(
            extension_id=args.extension_id,
            app_support_dir=args.app_support_dir,
            edge_manifest_dir=args.edge_manifest_dir,
            known_hosts_source=args.known_hosts_source,
            server_host=args.server_host,
            server_port=args.server_port,
            server_user=args.server_user,
            proxy_host=args.proxy_host,
            proxy_port=args.proxy_port,
            connect_timeout=args.connect_timeout,
            no_generate_key=args.no_generate_key,
        )
    except (InstallError, OSError) as exc:
        # Exception text contains only local paths and fixed setup errors; no
        # Cookie ever enters this script.  Keep the output concise.
        print(f"安装失败：{exc}", file=sys.stderr)
        return 2

    print(f"已安装 Native Messaging host：{paths['host_path']}")
    print(f"Edge manifest：{paths['manifest_path']}")
    public_key = _public_key(paths["identity_file"])
    if public_key:
        print("请把下面的公钥加入服务器 rsshub-sync 账号（不含引号）：")
        print(public_key)
    if not paths["known_hosts_ready"]:
        print(
            f"警告：{paths['known_hosts_file']} 目前为空；请先写入已核对的服务器主机公钥，"
            "StrictHostKeyChecking=yes 才会允许连接。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
