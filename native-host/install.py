#!/usr/bin/env python3
"""Install the RSSHub Cookie Sync Native Messaging host for Microsoft Edge.

The installer is intentionally local-only.  It never contacts the server and
never runs ``ssh-keyscan``: the fixed server host key must be supplied by the
operator through ``--known-hosts-source`` (or placed in ``~/.ssh/known_hosts``
out of band) before the host can connect with StrictHostKeyChecking enabled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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
        DEFAULT_EXTENSION_ID,
        HOST_NAME,
        ConfigurationError,
        HostConfig,
        load_config,
    )
except ImportError:  # pragma: no cover - supports direct path execution
    from .native_host import (  # type: ignore
        DEFAULT_APP_SUPPORT_DIR,
        DEFAULT_CONNECT_TIMEOUT,
        MAX_CONNECT_TIMEOUT,
        DEFAULT_SERVER_PORT,
        DEFAULT_SERVER_USER,
        DEFAULT_EXTENSION_ID,
        HOST_NAME,
        ConfigurationError,
        HostConfig,
        load_config,
    )


DEFAULT_EDGE_MANIFEST_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Microsoft Edge"
    / "NativeMessagingHosts"
)
DEFAULT_SSH_BINARY = "/usr/bin/ssh"
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


def _identity_path(value: Optional[Path], *, home: Optional[Path] = None) -> Path:
    """Return a safe private-key path directly below the user's ``~/.ssh``.

    The installer may create this directory and the default key, or reuse an
    existing key selected with ``--identity-file``.  Keeping the path to one
    normal file in ``~/.ssh`` prevents a typo from making the Native Host read
    an unrelated private key elsewhere on disk.
    """

    root = _safe_path((home or Path.home()) / ".ssh")
    if os.path.lexists(str(root)) and root.is_symlink():
        raise InstallError("~/.ssh 是符号链接，无法安全安装")
    candidate = _safe_path(value or root / "rsshub-cookie-sync")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise InstallError("--identity-file 必须位于 ~/.ssh 目录内") from exc
    if len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
        raise InstallError("--identity-file 必须是 ~/.ssh 下的文件名")
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", relative.name):
        raise InstallError("--identity-file 文件名不合法")
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            raise InstallError("--identity-file 不能是符号链接")
    return candidate


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
        try:
            info = key_path.lstat()
        except OSError as exc:
            raise InstallError("SSH 密钥不可读") from exc
        if not stat.S_ISREG(info.st_mode):
            raise InstallError("SSH 密钥路径不是普通文件")
        if info.st_uid != os.getuid():
            raise InstallError("SSH 密钥必须由当前用户拥有")
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


def _ensure_identity(key_path: Path, *, generate: bool) -> None:
    """Validate/reuse an existing key, or create the default one."""

    _ensure_dir(key_path.parent, 0o700)
    if key_path.exists() or key_path.is_symlink():
        if key_path.is_symlink():
            raise InstallError("SSH 密钥不能是符号链接")
        try:
            info = key_path.lstat()
        except OSError as exc:
            raise InstallError("SSH 密钥不可读") from exc
        if not stat.S_ISREG(info.st_mode):
            raise InstallError("SSH 密钥路径不是普通文件")
        if info.st_uid != os.getuid():
            raise InstallError("SSH 密钥必须由当前用户拥有")
        os.chmod(key_path, 0o600)
        return
    if not generate:
        raise InstallError("未找到 SSH 密钥；请移除 --no-generate-key 或先准备 --identity-file")
    _generate_identity(key_path)


def _ensure_known_hosts(
    destination: Path,
    source: Optional[Path],
    *,
    server_host: Optional[str],
    server_port: int,
) -> bool:
    _ensure_dir(destination.parent, 0o700)
    if source is not None:
        if server_host is None:
            raise InstallError("提供 known_hosts 源文件时必须同时提供服务器地址")
        source_payload = _read_known_hosts_source(
            source, server_host=server_host, server_port=server_port
        )
        # The default destination is the user's normal ~/.ssh/known_hosts.
        # Never replace that shared file just because the installer received a
        # one-entry source file; merge the verified entry and preserve all
        # existing host keys.
        if destination.exists():
            _assert_not_symlink(destination)
            if not destination.is_file():
                raise InstallError("known_hosts 目标不是普通文件")
            try:
                destination_info = destination.lstat()
            except OSError as exc:
                raise InstallError("known_hosts 目标不可读") from exc
            if destination_info.st_uid != os.getuid():
                raise InstallError("known_hosts 目标必须由当前用户拥有")
            try:
                existing = destination.read_bytes()
            except OSError as exc:
                raise InstallError("known_hosts 目标不可读") from exc
            if b"\x00" in existing or len(existing) > 1024 * 1024:
                raise InstallError("known_hosts 目标不合法")
            separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
            merged = existing + separator + source_payload
        else:
            merged = source_payload
        _validate_known_hosts_entry(merged, server_host=server_host, server_port=server_port)
        _atomic_write_bytes(destination, merged, 0o600)
        return True
    if destination.exists():
        _assert_not_symlink(destination)
        if not destination.is_file():
            raise InstallError("known_hosts 目标不是普通文件")
        try:
            info = destination.lstat()
        except OSError as exc:
            raise InstallError("known_hosts 目标不可读") from exc
        if info.st_uid != os.getuid():
            raise InstallError("known_hosts 目标必须由当前用户拥有")
        os.chmod(destination, 0o600)
        if destination.stat().st_size == 0:
            return False
        if server_host is None:
            # A fresh zero-argument install has no endpoint to verify yet.
            # Keep the user's shared known_hosts intact and let Options set
            # the endpoint later; the first update will still use strict host
            # key checking.
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


def _native_launcher(python_executable: Optional[Path], host_path: Path) -> bytes:
    """Create a launcher that does not depend on Edge's GUI process PATH."""

    raw_python = python_executable or Path(sys.executable)
    try:
        interpreter = raw_python.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InstallError("无法确定当前 Python 解释器") from exc
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise InstallError("当前 Python 解释器不可执行")
    command = " ".join((shlex.quote(str(interpreter)), shlex.quote(str(host_path))))
    return f'#!/bin/sh\nexec {command} "$@"\n'.encode("utf-8")


def build_config(
    *,
    identity_file: Path,
    known_hosts_file: Path,
    server_host: Optional[str] = None,
    server_port: int = DEFAULT_SERVER_PORT,
    server_user: str = DEFAULT_SERVER_USER,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    ssh_binary: str = DEFAULT_SSH_BINARY,
) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "host_name": HOST_NAME,
        "server": {
            "host": (
                _validate_endpoint(server_host, label="--server-host")
                if server_host is not None
                else None
            ),
            "port": _validate_port(server_port, "--server-port"),
            "user": _validate_user(server_user),
        },
        "ssh": {
            "binary": _validate_binary(ssh_binary, "--ssh-binary"),
            "identity_file": str(identity_file),
            "known_hosts_file": str(known_hosts_file),
            "connect_timeout": _validate_timeout(connect_timeout),
        },
    }


def install(
    *,
    extension_id: str = DEFAULT_EXTENSION_ID,
    app_support_dir: Path = DEFAULT_APP_SUPPORT_DIR,
    edge_manifest_dir: Path = DEFAULT_EDGE_MANIFEST_DIR,
    known_hosts_source: Optional[Path] = None,
    server_host: Optional[str] = None,
    server_port: Optional[int] = None,
    server_user: Optional[str] = None,
    identity_file: Optional[Path] = None,
    connect_timeout: Optional[int] = None,
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
    launcher_path = app_support_dir / "native_host"
    config_path = app_support_dir / "config.json"
    manifest_path = edge_manifest_dir / f"{HOST_NAME}.json"

    # Reinstallation is intentionally non-destructive.  If an older version
    # already has a valid server or an Application Support identity, retain it
    # unless the operator explicitly supplies a replacement.  This also makes
    # the old non-empty proxy section harmless: native_host ignores it and
    # the next successful write below emits a direct-SSH-only config.
    existing: Optional[HostConfig] = None
    if os.path.lexists(str(config_path)):
        try:
            existing = load_config(config_path)
        except ConfigurationError as exc:
            raise InstallError("已有配置文件不合法；为避免清空部署，请先修复或备份它") from exc

    effective_host = server_host if server_host is not None else (existing.server_host if existing else None)
    effective_port = server_port if server_port is not None else (existing.server_port if existing else DEFAULT_SERVER_PORT)
    effective_user = server_user if server_user is not None else (existing.server_user if existing else DEFAULT_SERVER_USER)
    effective_timeout = connect_timeout if connect_timeout is not None else (existing.connect_timeout if existing else DEFAULT_CONNECT_TIMEOUT)
    if effective_host is not None:
        _validate_endpoint(effective_host, label="--server-host")
    _validate_port(effective_port, "--server-port")
    _validate_user(effective_user)
    _validate_timeout(effective_timeout)

    if identity_file is not None:
        selected_identity = _identity_path(identity_file, home=trusted_root)
    elif existing is not None:
        existing_identity = _safe_path(existing.identity_file)
        ssh_root = _safe_path(trusted_root / ".ssh")
        try:
            relative = existing_identity.relative_to(ssh_root)
        except ValueError:
            try:
                existing_identity.relative_to(app_support_dir)
            except ValueError as exc:
                raise InstallError("已有 SSH 密钥不在 ~/.ssh 或旧版 Application Support 目录内") from exc
            # A legacy key remains usable and is intentionally not copied or
            # exposed to Edge; its public selector is simply ``legacy``.
            selected_identity = existing_identity
        else:
            if len(relative.parts) != 1:
                raise InstallError("已有 SSH 密钥路径不安全")
            selected_identity = _identity_path(existing_identity, home=trusted_root)
    else:
        selected_identity = _identity_path(None, home=trusted_root)

    if existing is not None and known_hosts_source is None:
        # Keep an older Application Support known_hosts file when it is the
        # only trust store for the existing deployment.  Fresh installs use
        # the normal ~/.ssh/known_hosts path.
        existing_known_hosts = _safe_path(existing.known_hosts_file)
        standard_known_hosts = _safe_path(trusted_root / ".ssh" / "known_hosts")
        if existing_known_hosts != standard_known_hosts:
            try:
                existing_known_hosts.relative_to(app_support_dir)
            except ValueError:
                known_hosts_file = standard_known_hosts
            else:
                known_hosts_file = existing_known_hosts
        else:
            known_hosts_file = standard_known_hosts
    else:
        known_hosts_file = _safe_path(trusted_root / ".ssh" / "known_hosts")

    _copy_file_atomic(source_host, host_path, 0o700)
    _atomic_write_bytes(launcher_path, _native_launcher(None, host_path), 0o700)
    _ensure_identity(selected_identity, generate=not no_generate_key)
    known_hosts_ready = _ensure_known_hosts(
        known_hosts_file,
        known_hosts_source,
        server_host=effective_host,
        server_port=effective_port,
    )

    config = build_config(
        identity_file=selected_identity,
        known_hosts_file=known_hosts_file,
        server_host=effective_host,
        server_port=effective_port,
        server_user=effective_user,
        connect_timeout=effective_timeout,
    )
    _atomic_write_bytes(
        config_path,
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        0o600,
    )
    manifest = build_manifest(launcher_path, extension_id)
    _atomic_write_bytes(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        0o600,
    )
    return {
        "app_support_dir": app_support_dir,
        "host_path": host_path,
        "launcher_path": launcher_path,
        "config_path": config_path,
        "identity_file": selected_identity,
        "known_hosts_file": known_hosts_file,
        "manifest_path": manifest_path,
        "known_hosts_ready": known_hosts_ready,
        "server_configured": effective_host is not None,
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
    parser.add_argument(
        "--extension-id",
        default=DEFAULT_EXTENSION_ID,
        help=f"扩展 ID（默认：{DEFAULT_EXTENSION_ID}；仅在使用自定义扩展时指定）",
    )
    parser.add_argument("--app-support-dir", type=Path, default=DEFAULT_APP_SUPPORT_DIR)
    parser.add_argument("--edge-manifest-dir", type=Path, default=DEFAULT_EDGE_MANIFEST_DIR)
    parser.add_argument(
        "--known-hosts-source",
        type=Path,
        help="预先获取并核对过的 SSH known_hosts 文件；不会自动 ssh-keyscan",
    )
    parser.add_argument("--server-host", help="服务器地址；不填则保留已有配置，首次安装可稍后在扩展设置中填写")
    parser.add_argument("--server-port", type=int, default=None)
    parser.add_argument("--server-user", default=None)
    parser.add_argument(
        "--identity-file",
        type=Path,
        default=None,
        help="~/.ssh 下要复用或创建的私钥文件（默认：~/.ssh/rsshub-cookie-sync）",
    )
    parser.add_argument("--connect-timeout", type=int, default=None)
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
            identity_file=args.identity_file,
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
    if not paths["server_configured"]:
        print(
            "提示：尚未配置服务器。安装扩展后打开“设置”，填写服务器地址、端口并选择密钥。",
            file=sys.stderr,
        )
    elif not paths["known_hosts_ready"]:
        print(
            f"警告：{paths['known_hosts_file']} 目前为空；请先写入已核对的服务器主机公钥，"
            "StrictHostKeyChecking=yes 才会允许连接。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
