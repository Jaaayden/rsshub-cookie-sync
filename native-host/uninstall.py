#!/usr/bin/env python3
"""Remove the RSSHub Cookie Sync Native Messaging host from this Mac.

Only exact files owned by this installer are considered.  The script never
recursively deletes an arbitrary directory, and it keeps SSH keys by default.
The installed copy is intentionally self-contained: ``install.py`` bundles
this file beside ``native_host.py`` so it remains usable after the source
checkout is removed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

try:
    from native_host import DEFAULT_APP_SUPPORT_DIR, HOST_NAME
except ImportError:  # pragma: no cover - supports package execution
    from .native_host import DEFAULT_APP_SUPPORT_DIR, HOST_NAME  # type: ignore


DEFAULT_EDGE_MANIFEST_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Microsoft Edge"
    / "NativeMessagingHosts"
)
DEDICATED_IDENTITY_NAME = "rsshub-cookie-sync"
DEFAULT_UNINSTALL_DIR = (
    Path.home() / "Library" / "Application Support" / "rsshub-cookie-sync"
)


def _safe_path(path: Path) -> Path:
    return path.expanduser().absolute()


def _managed_directory(path: Path, *, allowed_root: Path, label: str) -> Path:
    candidate = _safe_path(path)
    root = _safe_path(allowed_root)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} 必须位于当前用户目录内") from exc
    if not relative.parts:
        raise RuntimeError(f"{label} 不能是当前用户目录本身")
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            raise RuntimeError(f"{label} 包含符号链接")
    return candidate


def _remove_regular(path: Path) -> bool:
    """Remove one managed regular file, refusing symlink surprises."""

    if not os.path.lexists(str(path)):
        return False
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"拒绝删除不安全路径：{path}")
    path.unlink()
    return True


def _remove_empty_dirs(paths: Iterable[Path]) -> None:
    for directory in sorted(set(paths), key=lambda item: len(item.parts), reverse=True):
        if not directory.exists() or directory.is_symlink() or not directory.is_dir():
            continue
        try:
            directory.rmdir()
        except OSError:
            # A user may have placed unrelated files in the directory.  Leave
            # it intact; uninstalling this host must not remove those files.
            pass


def _ask_purge_key() -> bool:
    """Ask only when a real terminal is attached; otherwise preserve keys."""

    try:
        if not sys.stdin.isatty():
            return False
        print(
            "是否同时删除项目专用 SSH 密钥 ~/.ssh/rsshub-cookie-sync？[y/N] ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        answer = sys.stdin.readline().strip().lower()
    except (AttributeError, OSError):
        return False
    return answer in {"y", "yes"}


def _default_uninstall_dir_for_root(trusted_root: Path) -> Path:
    """Map the fixed user path into a test/custom home without string hacks."""

    home = _safe_path(Path.home())
    try:
        relative = _safe_path(DEFAULT_UNINSTALL_DIR).relative_to(home)
    except ValueError as exc:  # pragma: no cover - constant sanity check
        raise RuntimeError("卸载器目录配置不安全") from exc
    return _safe_path(trusted_root / relative)


def uninstall(
    *,
    app_support_dir: Path = DEFAULT_APP_SUPPORT_DIR,
    edge_manifest_dir: Path = DEFAULT_EDGE_MANIFEST_DIR,
    uninstall_dir: Optional[Path] = None,
    purge_key: bool = False,
    prompt_for_key: bool = False,
    allowed_root: Optional[Path] = None,
) -> list[Path]:
    """Remove the Native Host while preserving SSH material by default.

    ``purge_key`` is deliberately explicit for unattended operation.  The
    optional ``prompt_for_key`` is used by the CLI when it has a TTY; library
    callers remain non-interactive unless they opt in.  Only this project's
    exact dedicated key and the old Application Support key are eligible for
    removal.  A user's normal ``~/.ssh/id_ed25519`` is never touched.
    """

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
    uninstall_dir = _managed_directory(
        uninstall_dir or _default_uninstall_dir_for_root(trusted_root),
        allowed_root=trusted_root,
        label="卸载器目录",
    )
    config_path = app_support_dir / "config.json"

    removed: list[Path] = []
    manifest_path = edge_manifest_dir / f"{HOST_NAME}.json"
    for path in (
        manifest_path,
        app_support_dir / "native_host",
        app_support_dir / "native_host.py",
        config_path,
    ):
        if _remove_regular(path):
            removed.append(path)

    should_purge = purge_key or (prompt_for_key and _ask_purge_key())
    if should_purge:
        home_ssh = trusted_root / ".ssh"
        project_key_files = (
            home_ssh / DEDICATED_IDENTITY_NAME,
            home_ssh / f"{DEDICATED_IDENTITY_NAME}.pub",
            # These are the only key names created by the pre-1.1 installer;
            # they live inside its private Application Support directory and
            # are included only when the user explicitly requests purge.
            app_support_dir / "ssh" / "id_ed25519",
            app_support_dir / "ssh" / "id_ed25519.pub",
        )
        for path in project_key_files:
            if _remove_regular(path):
                removed.append(path)

    # The fixed uninstall entry point is self-contained and can remove its
    # own files after the main host has stopped being referenced by Edge.
    # Keep the directory if unrelated files are present.
    for path in (
        uninstall_dir / "uninstall.sh",
        uninstall_dir / "uninstall.py",
        uninstall_dir / "native_host.py",
    ):
        if _remove_regular(path):
            removed.append(path)

    _remove_empty_dirs(
        (
            app_support_dir / "ssh",
            app_support_dir,
            edge_manifest_dir,
            uninstall_dir,
        )
    )
    return removed


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="卸载 RSSHub Cookie Sync Edge Native Messaging host")
    parser.add_argument("--app-support-dir", type=Path, default=DEFAULT_APP_SUPPORT_DIR)
    parser.add_argument("--edge-manifest-dir", type=Path, default=DEFAULT_EDGE_MANIFEST_DIR)
    parser.add_argument(
        "--uninstall-dir",
        type=Path,
        default=DEFAULT_UNINSTALL_DIR,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--purge-key",
        action="store_true",
        help="同时删除项目专用 SSH 密钥；默认保留密钥",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        removed = uninstall(
            app_support_dir=args.app_support_dir,
            edge_manifest_dir=args.edge_manifest_dir,
            uninstall_dir=args.uninstall_dir,
            purge_key=args.purge_key,
            prompt_for_key=not args.purge_key,
        )
    except (OSError, RuntimeError) as exc:
        print(f"卸载失败：{exc}", file=sys.stderr)
        return 2
    if removed:
        print(f"已移除 {len(removed)} 个 Native host 文件。")
    else:
        print("未找到已安装的 Native host 文件。")
    home_ssh = _safe_path(Path.home() / ".ssh")
    app_ssh = _safe_path(args.app_support_dir) / "ssh"
    project_key_paths = {
        home_ssh / DEDICATED_IDENTITY_NAME,
        home_ssh / f"{DEDICATED_IDENTITY_NAME}.pub",
        app_ssh / "id_ed25519",
        app_ssh / "id_ed25519.pub",
    }
    if any(path in project_key_paths for path in removed):
        print("项目专用 SSH 密钥已删除；~/.ssh/known_hosts 保留。")
    else:
        print("项目专用 SSH 密钥已保留；~/.ssh/known_hosts 保留。")
    print("请打开 edge://extensions，手动移除 RSSHub Cookie Sync 扩展。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
