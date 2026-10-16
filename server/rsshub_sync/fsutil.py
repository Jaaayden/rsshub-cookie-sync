"""fsutil (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import tempfile
from pathlib import Path
from typing import Iterator

from .errors import SyncError


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync after an atomic rename."""

    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def ensure_directory(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise SyncError("cannot secure storage directory") from exc


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write bytes durably and replace *path* atomically.

    The destination is never removed before the replacement is ready.  This
    is important for the live env file: Compose must not observe a missing or
    half-written file while a timer and an upload overlap.
    """

    if b"\x00" in data:
        raise SyncError("storage data contains NUL")
    ensure_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(str(temporary_path), str(path))
        _fsync_directory(path.parent)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def secure_copy(src: Path, dst: Path, mode: int = 0o600) -> None:
    try:
        data = src.read_bytes()
    except OSError as exc:
        raise SyncError("cannot read transaction backup") from exc
    atomic_write(dst, data, mode=mode)


def secure_remove(path: Path) -> None:
    """Best-effort scrub and unlink of a temporary secret-bearing file."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SyncError("cannot inspect temporary file") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SyncError("temporary file is not regular")
    try:
        with path.open("r+b") as handle:
            remaining = info.st_size
            zeroes = b"\x00" * 65536
            while remaining:
                chunk = zeroes if remaining >= len(zeroes) else zeroes[:remaining]
                handle.write(chunk)
                remaining -= len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
        _fsync_directory(path.parent)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SyncError("cannot remove temporary secret file") from exc


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Serialise apply, monitor, rollback, and state writes."""

    ensure_directory(path.parent)
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SyncError("cannot open lock") from exc
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise SyncError("cannot acquire lock") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def read_limited(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SyncError("cannot read secure file") from exc
    if len(data) > limit:
        raise SyncError("secure file is too large")
    return data
