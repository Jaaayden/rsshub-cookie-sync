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
        build_ssh_argv,
        config_from_mapping,
        load_config,
        load_config_for_migration,
        validate_runtime_files,
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
        build_ssh_argv,
        config_from_mapping,
        load_config,
        load_config_for_migration,
        validate_runtime_files,
    )


DEFAULT_EDGE_MANIFEST_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Microsoft Edge"
    / "NativeMessagingHosts"
)
DEFAULT_UNINSTALL_DIR = (
    Path.home() / "Library" / "Application Support" / "rsshub-cookie-sync"
)
DEFAULT_SSH_BINARY = "/usr/bin/ssh"
DEDICATED_IDENTITY_NAME = "rsshub-cookie-sync"
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
    # The browser-facing host relies on the forced-command account installed
    # by the server component.  Accepting another syntactically valid user
    # here would create a configuration that either fails later or, worse,
    # targets an unrestricted account such as root.
    if value != DEFAULT_SERVER_USER:
        raise InstallError(f"--server-user 固定为 {DEFAULT_SERVER_USER}")
    return DEFAULT_SERVER_USER


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
    """Return the one project-dedicated identity below ``~/.ssh``.

    A generic identity such as ``id_ed25519`` may already be authorised for
    root on one or more machines.  The installer must never adopt such a key,
    even when an older config points at it.  Keeping one fixed filename also
    makes a reinstall distinguish the project key from a legacy/shared key.
    """

    root = _safe_path((home or Path.home()) / ".ssh")
    if os.path.lexists(str(root)) and root.is_symlink():
        raise InstallError("~/.ssh 是符号链接，无法安全安装")
    candidate = _safe_path(value or root / DEDICATED_IDENTITY_NAME)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise InstallError("--identity-file 必须位于 ~/.ssh 目录内") from exc
    if len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
        raise InstallError("--identity-file 必须是 ~/.ssh 下的文件名")
    if relative.name != DEDICATED_IDENTITY_NAME:
        raise InstallError(
            f"SSH 私钥固定使用 ~/.ssh/{DEDICATED_IDENTITY_NAME}；"
            "不会复用 id_ed25519 等通用登录密钥"
        )
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            raise InstallError("--identity-file 不能是符号链接")
    return candidate


def prepare_dedicated_identity(
    *,
    allowed_root: Optional[Path] = None,
    no_generate_key: bool = False,
) -> Path:
    """Create or validate the dedicated key without changing Host config.

    This is the first half of a safe legacy-key migration.  The operator can
    provision the returned public key while the old Native Host configuration
    remains byte-for-byte unchanged, then explicitly activate it later.
    """

    trusted_root = _safe_path(allowed_root or Path.home())
    identity = _identity_path(None, home=trusted_root)
    _ensure_identity(identity, generate=not no_generate_key)
    return identity


def _read_safe_known_hosts(path: Path, *, label: str) -> bytes:
    """Read a trusted known_hosts file without interpreting its host keys.

    OpenSSH owns the known_hosts grammar and matching rules.  In particular,
    hashed hosts, CA markers, RSA/ECDSA keys, aliases, and non-ASCII comments
    are all valid inputs that an installer must not second-guess.  We only
    enforce the local file boundary needed before it is handed to OpenSSH.
    """

    _assert_not_symlink(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallError(f"{label}不可读") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or info.st_size > 1024 * 1024
    ):
        raise InstallError(f"{label}不合法")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise InstallError(f"{label}不可读") from exc
    if b"\x00" in payload:
        raise InstallError(f"{label}包含 NUL")
    return payload


def _read_known_hosts_source(source: Path) -> bytes:
    return _read_safe_known_hosts(source, label="known_hosts 源文件")


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


def _validate_ed25519_public_sidecar(key_path: Path) -> None:
    """Require the public half accepted by the server provisioner."""

    public_path = Path(f"{key_path}.pub")
    _assert_not_symlink(public_path)
    try:
        info = public_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise InstallError("SSH 公钥必须是当前用户拥有的普通文件")
        if info.st_mode & 0o022 or not 1 <= info.st_size <= 8192:
            raise InstallError("SSH 公钥权限或大小不安全")
        payload = public_path.read_bytes()
    except FileNotFoundError as exc:
        raise InstallError("缺少对应的 .pub 公钥文件") from exc
    except OSError as exc:
        raise InstallError("SSH 公钥不可读") from exc
    _validate_ed25519_public_payload(payload)


def _public_key_identity(payload: bytes) -> tuple[bytes, bytes]:
    """Validate one public-key line and return its type/blob for comparison."""

    if not isinstance(payload, bytes) or b"\x00" in payload or b"\r" in payload:
        raise InstallError("SSH 公钥格式不合法")
    lines = payload.strip().split(b"\n")
    fields = lines[0].split() if len(lines) == 1 else []
    if len(fields) < 2 or fields[0] != b"ssh-ed25519":
        raise InstallError("只支持 Ed25519 SSH 密钥")
    if not re.fullmatch(rb"[A-Za-z0-9+/]+={0,2}", fields[1]):
        raise InstallError("SSH 公钥格式不合法")
    return fields[0], fields[1]


def _validate_ed25519_public_payload(payload: bytes) -> None:
    """Validate one public-key line without echoing its contents."""

    _public_key_identity(payload)


def _derive_dedicated_public_key(key_path: Path) -> bytes:
    """Derive an Ed25519 public key from the private key itself."""

    try:
        result = subprocess.run(
            ["/usr/bin/ssh-keygen", "-y", "-f", str(key_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("无法验证专用 SSH 私钥") from exc
    if getattr(result, "returncode", 1) != 0:
        raise InstallError("专用 SSH 私钥无法无人值守解锁")
    payload = getattr(result, "stdout", b"")
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= 8192:
        raise InstallError("专用 SSH 私钥派生出的公钥不合法")
    _validate_ed25519_public_payload(payload)
    return payload.strip() + b"\n"


def _validate_dedicated_identity_pair(key_path: Path) -> None:
    """Use the private key as authority and compare an existing .pub sidecar."""

    derived = _derive_dedicated_public_key(key_path)
    public_path = Path(f"{key_path}.pub")
    if not os.path.lexists(str(public_path)):
        _atomic_write_bytes(public_path, derived, 0o644)
        _validate_ed25519_public_sidecar(key_path)
        return
    _validate_ed25519_public_sidecar(key_path)
    try:
        sidecar = public_path.read_bytes()
    except OSError as exc:
        raise InstallError("SSH 公钥不可读") from exc
    if _public_key_identity(sidecar) != _public_key_identity(derived):
        raise InstallError("SSH 公钥与专用私钥不匹配；当前文件未修改")


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
        public_path = Path(f"{key_path}.pub")
        if key_path.name == DEDICATED_IDENTITY_NAME:
            _validate_dedicated_identity_pair(key_path)
        else:
            _validate_ed25519_public_sidecar(key_path)
        return
    if not generate:
        raise InstallError("未找到 SSH 密钥；请移除 --no-generate-key 或先准备 --identity-file")
    _generate_identity(key_path)
    _validate_ed25519_public_sidecar(key_path)


def _ensure_known_hosts(
    destination: Path,
    source: Optional[Path],
    *,
    server_host: Optional[str],
    server_port: int,
) -> bool:
    _ensure_dir(destination.parent, 0o700)
    if source is not None:
        source_payload = _read_known_hosts_source(source)
        # The default destination is the user's normal ~/.ssh/known_hosts.
        # Never replace that shared file just because the installer received a
        # one-entry source file; merge the verified entry and preserve all
        # existing host keys.
        if destination.exists():
            existing = _read_safe_known_hosts(destination, label="known_hosts 目标")
            separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
            merged = existing + separator + source_payload
        else:
            merged = source_payload
        if b"\x00" in merged or len(merged) > 1024 * 1024:
            raise InstallError("known_hosts 目标不合法")
        _atomic_write_bytes(destination, merged, 0o600)
        return bool(merged.strip())
    if destination.exists():
        payload = _read_safe_known_hosts(destination, label="known_hosts 目标")
        os.chmod(destination, 0o600)
        if not payload:
            return False
        if server_host is None:
            # A fresh zero-argument install has no endpoint to verify yet.
            # Keep the user's shared known_hosts intact and let Options set
            # the endpoint later; the first update will still use strict host
            # key checking.
            return False
        # The presence of bytes is only a UI hint.  Whether this endpoint has
        # a matching key is deliberately left to OpenSSH at connection time.
        return bool(payload.strip())
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
        # Keep the stable command discovered by ``command -v python3`` (for
        # example /opt/homebrew/bin/python3) instead of resolving it to a
        # versioned Homebrew Cellar path that disappears on upgrade.
        interpreter = raw_python.expanduser().absolute()
    except (OSError, RuntimeError) as exc:
        raise InstallError("无法确定当前 Python 解释器") from exc
    if not interpreter.is_absolute() or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise InstallError("当前 Python 解释器不可执行")
    command = " ".join((shlex.quote(str(interpreter)), shlex.quote(str(host_path))))
    return f'#!/bin/sh\nexec {command} "$@"\n'.encode("utf-8")


def _uninstall_launcher(
    python_executable: Optional[Path],
    uninstall_python: Path,
    *,
    app_support_dir: Path,
    edge_manifest_dir: Path,
) -> bytes:
    """Create a self-contained uninstall entry point.

    The installed launcher passes the actual installation directories rather
    than relying on the caller's current directory or on the source checkout.
    All paths are shell-quoted as data; the user may still append
    ``--purge-key`` or other future options through ``"$@"``.
    """

    raw_python = python_executable or Path(sys.executable)
    try:
        # See _native_launcher: retain a stable Homebrew shim rather than a
        # versioned Cellar path.
        interpreter = raw_python.expanduser().absolute()
    except (OSError, RuntimeError) as exc:
        raise InstallError("无法确定当前 Python 解释器") from exc
    if not interpreter.is_absolute() or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise InstallError("当前 Python 解释器不可执行")
    command = " ".join(
        shlex.quote(str(value))
        for value in (
            interpreter,
            uninstall_python,
            "--app-support-dir",
            app_support_dir,
            "--edge-manifest-dir",
            edge_manifest_dir,
        )
    )
    return f'#!/bin/sh\nset -eu\nexec {command} "$@"\n'.encode("utf-8")


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


def _verify_dedicated_identity(
    config: HostConfig,
    *,
    runner: Any = None,
) -> None:
    """Require an auth-only SSH round trip before activating a new key.

    The deliberately empty provider request contains no Cookie.  A correctly
    provisioned forced command rejects it quickly with a non-255 status;
    OpenSSH reserves 255 for transport/authentication failure.  No server
    response or stderr is exposed by the installer.
    """

    validate_runtime_files(config)
    invoke = runner or subprocess.run
    try:
        result = invoke(
            build_ssh_argv(config),
            input=b'{"version":1,"providers":{}}\n',
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
            shell=False,
            check=False,
            timeout=min(60, config.connect_timeout + 15),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("专用 SSH 密钥认证探测失败；当前配置未修改") from exc
    if getattr(result, "returncode", 255) == 255:
        raise InstallError(
            "专用 SSH 探测失败；请确认公钥已授权且 ~/.ssh/known_hosts 已加入经核对的主机密钥，"
            "当前配置未修改"
        )


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
    activate_dedicated_key: bool = False,
    activation_runner: Any = None,
    source_host: Optional[Path] = None,
    allowed_root: Optional[Path] = None,
) -> Mapping[str, Any]:
    """Install local files and return their paths.

    Normal installation has no network side effects.  Explicit dedicated-key
    activation performs one Cookie-free SSH authentication probe before any
    installed Host or config file is replaced.  ``activation_runner`` keeps
    that probe injectable in tests.
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
    # The installed manifest must launch the protocol host itself.  Using
    # ``Path(__file__)`` here would copy this installer into Application
    # Support, which would leave Edge invoking install.py as a Native host.
    source_host = source_host or Path(__file__).with_name("native_host.py").resolve()
    source_uninstall = Path(__file__).with_name("uninstall.py").resolve()
    host_path = app_support_dir / "native_host.py"
    launcher_path = app_support_dir / "native_host"
    config_path = app_support_dir / "config.json"
    manifest_path = edge_manifest_dir / f"{HOST_NAME}.json"
    home_root = _safe_path(Path.home())
    try:
        uninstall_relative = _safe_path(DEFAULT_UNINSTALL_DIR).relative_to(home_root)
    except ValueError as exc:  # pragma: no cover - constant sanity check
        raise InstallError("卸载器目录配置不安全") from exc
    uninstall_dir = _managed_directory(
        trusted_root / uninstall_relative,
        allowed_root=trusted_root,
        label="卸载器目录",
    )
    uninstall_script = uninstall_dir / "uninstall.sh"
    uninstall_python = uninstall_dir / "uninstall.py"
    uninstall_host = uninstall_dir / "native_host.py"

    # Ordinary reinstallation is intentionally non-destructive.  It only
    # reuses the exact project identity already stored in the config.  A
    # generic key (notably id_ed25519) may also unlock root elsewhere, so it is
    # never retained or replaced silently.  Migration is an explicit second
    # step after the dedicated public key has been provisioned on the server.
    existing: Optional[HostConfig] = None
    if os.path.lexists(str(config_path)):
        try:
            existing = load_config(config_path)
        except ConfigurationError as exc:
            if not activate_dedicated_key:
                raise InstallError(
                    "已有配置需要迁移；普通重装不会改写它。先运行 "
                    "install.py --prepare-dedicated-key，授权专用公钥后再运行 "
                    "install.py --activate-dedicated-key"
                ) from exc
            try:
                existing = load_config_for_migration(config_path)
            except ConfigurationError as migration_exc:
                raise InstallError("已有配置文件不合法；为避免清空部署，请先修复或备份它") from migration_exc

    if activate_dedicated_key and existing is None:
        raise InstallError("没有需要迁移的已有配置；全新安装请直接运行 install.py")

    effective_host = server_host if server_host is not None else (existing.server_host if existing else None)
    effective_port = server_port if server_port is not None else (existing.server_port if existing else DEFAULT_SERVER_PORT)
    effective_user = server_user if server_user is not None else (existing.server_user if existing else DEFAULT_SERVER_USER)
    effective_timeout = connect_timeout if connect_timeout is not None else (existing.connect_timeout if existing else DEFAULT_CONNECT_TIMEOUT)
    if effective_host is not None:
        _validate_endpoint(effective_host, label="--server-host")
    _validate_port(effective_port, "--server-port")
    _validate_user(effective_user)
    _validate_timeout(effective_timeout)

    dedicated_identity = _identity_path(identity_file, home=trusted_root)
    if existing is not None and not activate_dedicated_key:
        existing_identity = _safe_path(existing.identity_file)
        if existing_identity != dedicated_identity:
            raise InstallError(
                "检测到旧版或通用 SSH 密钥；普通重装不会继续复用或自动替换它。先运行 "
                "install.py --prepare-dedicated-key，授权 ~/.ssh/rsshub-cookie-sync.pub 后再运行 "
                "install.py --activate-dedicated-key"
            )
    selected_identity = dedicated_identity

    # There is exactly one trust-store location.  Older Application Support
    # known_hosts files are never selected again; users may copy a verified
    # entry into this standard file themselves (or provide
    # ``--known-hosts-source``).  If the file is absent, _ensure_known_hosts
    # creates an empty one and the later SSH connection fails closed until a
    # host key is added.
    known_hosts_file = _safe_path(trusted_root / ".ssh" / "known_hosts")

    # Finish every key/trust/config validation before replacing the running
    # Host or launcher.  A rejected legacy RSA key or malformed known_hosts
    # must leave the previously installed executable untouched.
    # Never regenerate a configured or soon-to-be-activated identity.  A new
    # private key at the same path would not match the public key currently on
    # the server and would turn a routine reinstall into an outage.
    generate_identity = existing is None and not no_generate_key
    _ensure_identity(selected_identity, generate=generate_identity)
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
    if activate_dedicated_key:
        if effective_host is None:
            raise InstallError("激活专用密钥前必须配置服务器地址")
        runtime_config = config_from_mapping(config, config_path=config_path)
        _verify_dedicated_identity(runtime_config, runner=activation_runner)
    launcher_payload = _native_launcher(None, host_path)
    uninstall_launcher_payload = _uninstall_launcher(
        None,
        uninstall_python,
        app_support_dir=app_support_dir,
        edge_manifest_dir=edge_manifest_dir,
    )
    manifest = build_manifest(launcher_path, extension_id)
    if not source_uninstall.is_file() or source_uninstall.is_symlink():
        raise InstallError("卸载器源文件不存在或不安全")

    _ensure_dir(app_support_dir, 0o700)
    _ensure_dir(edge_manifest_dir, 0o700)
    _ensure_dir(uninstall_dir, 0o700)
    _copy_file_atomic(source_host, host_path, 0o700)
    _atomic_write_bytes(launcher_path, launcher_payload, 0o700)
    _copy_file_atomic(source_uninstall, uninstall_python, 0o700)
    _copy_file_atomic(source_host, uninstall_host, 0o700)
    _atomic_write_bytes(uninstall_script, uninstall_launcher_payload, 0o700)
    _atomic_write_bytes(
        config_path,
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        0o600,
    )
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
        "uninstall_dir": uninstall_dir,
        "uninstall_script": uninstall_script,
        "uninstall_python": uninstall_python,
        "uninstall_host": uninstall_host,
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
    # Retain the exact-value flag for compatibility with older install
    # commands, but keep it out of normal help because the account is not a
    # deployment choice.
    parser.add_argument("--server-user", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--identity-file",
        type=Path,
        default=None,
        help="兼容参数；只接受 ~/.ssh/rsshub-cookie-sync，不允许通用登录密钥",
    )
    parser.add_argument("--connect-timeout", type=int, default=None)
    parser.add_argument(
        "--no-generate-key",
        action="store_true",
        help="不调用 ssh-keygen，要求目标密钥已经存在",
    )
    migration = parser.add_mutually_exclusive_group()
    migration.add_argument(
        "--prepare-dedicated-key",
        action="store_true",
        help="只创建或检查 ~/.ssh/rsshub-cookie-sync，不修改当前 Native Host 配置",
    )
    migration.add_argument(
        "--activate-dedicated-key",
        action="store_true",
        help="确认公钥已授权后，将已有配置显式迁移到专用密钥",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        if args.prepare_dedicated_key:
            identity = prepare_dedicated_identity(no_generate_key=args.no_generate_key)
            print(f"专用 SSH 密钥已准备：{identity}", file=sys.stderr)
            public_key = _public_key(identity)
            if public_key:
                print(
                    "请把下面的公钥加入服务器 rsshub-sync 账号（不含引号）：",
                    file=sys.stderr,
                )
                print(public_key)
            print(
                "公钥授权并验证后，再运行 install.py --activate-dedicated-key",
                file=sys.stderr,
            )
            return 0
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
            activate_dedicated_key=args.activate_dedicated_key,
        )
    except (InstallError, OSError) as exc:
        # Exception text contains only local paths and fixed setup errors; no
        # Cookie ever enters this script.  Keep the output concise.
        print(f"安装失败：{exc}", file=sys.stderr)
        return 2

    print(f"已安装 Native Messaging host：{paths['host_path']}", file=sys.stderr)
    print(f"Edge manifest：{paths['manifest_path']}", file=sys.stderr)
    public_key = _public_key(paths["identity_file"])
    if public_key:
        print(
            "请把下面的公钥加入服务器 rsshub-sync 账号（不含引号）：",
            file=sys.stderr,
        )
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
