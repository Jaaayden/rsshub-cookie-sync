#!/usr/bin/env python3
"""Remove the RSSHub Cookie Sync Native Messaging host from this Mac.

Only the files owned by this installer are considered.  The script never
recursively deletes an arbitrary directory, and an SSH key outside the
Application Support directory (if a user edited ``config.json``) is left
untouched.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

try:
    from native_host import DEFAULT_APP_SUPPORT_DIR, HOST_NAME
    from install import DEFAULT_EDGE_MANIFEST_DIR
except ImportError:  # pragma: no cover - supports package execution
    from .native_host import DEFAULT_APP_SUPPORT_DIR, HOST_NAME  # type: ignore
    from .install import DEFAULT_EDGE_MANIFEST_DIR  # type: ignore


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


def uninstall(
    *,
    app_support_dir: Path = DEFAULT_APP_SUPPORT_DIR,
    edge_manifest_dir: Path = DEFAULT_EDGE_MANIFEST_DIR,
    allowed_root: Optional[Path] = None,
) -> list[Path]:
    """Remove the host, manifest, config, and installer-generated SSH files."""

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
    config_path = app_support_dir / "config.json"

    removed: list[Path] = []
    manifest_path = edge_manifest_dir / f"{HOST_NAME}.json"
    for path in (manifest_path, app_support_dir / "native_host.py", config_path):
        if _remove_regular(path):
            removed.append(path)

    # Remove only known filenames under the managed ssh directory.  Do not
    # read config.json to discover paths: config is editable and must never be
    # able to redirect uninstall to an arbitrary file in Application Support.
    ssh_dir = app_support_dir / "ssh"
    managed_ssh_files = {
        ssh_dir / "id_ed25519",
        ssh_dir / "id_ed25519.pub",
        ssh_dir / "known_hosts",
    }
    for path in sorted(managed_ssh_files):
        if _remove_regular(path):
            removed.append(path)

    _remove_empty_dirs((ssh_dir, app_support_dir, edge_manifest_dir))
    return removed


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="卸载 RSSHub Cookie Sync Edge Native Messaging host")
    parser.add_argument("--app-support-dir", type=Path, default=DEFAULT_APP_SUPPORT_DIR)
    parser.add_argument("--edge-manifest-dir", type=Path, default=DEFAULT_EDGE_MANIFEST_DIR)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        removed = uninstall(
            app_support_dir=args.app_support_dir,
            edge_manifest_dir=args.edge_manifest_dir,
        )
    except (OSError, RuntimeError) as exc:
        print(f"卸载失败：{exc}", file=sys.stderr)
        return 2
    if removed:
        print(f"已移除 {len(removed)} 个 Native host 文件。")
    else:
        print("未找到已安装的 Native host 文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
