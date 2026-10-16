"""envfile (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping, Tuple

from .constants import KEY_RE, MAX_INPUT_BYTES
from .errors import SyncError
from .fsutil import read_limited
from .validation import validate_env_secret


def parse_env(data: bytes) -> Dict[str, str]:
    """Parse the deliberately simple raw env-file format used by Compose."""

    if b"\x00" in data or len(data) > MAX_INPUT_BYTES:
        raise SyncError("env file is invalid")
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SyncError("env file is not UTF-8") from exc
    result: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise SyncError("env file contains an invalid line")
        key, value = line.split("=", 1)
        if not KEY_RE.fullmatch(key):
            raise SyncError("env file contains an invalid key")
        if "\r" in value or "\n" in value:
            raise SyncError("env file contains a newline in a value")
        result[key] = value
    return result


def render_env(data: bytes, updates: Mapping[str, str]) -> bytes:
    """Replace or append env keys while preserving comments and other keys."""

    parse_env(data)  # Validate before writing a candidate live configuration.
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SyncError("env file is not UTF-8") from exc
    for key in updates:
        if not KEY_RE.fullmatch(key):
            raise SyncError("invalid env key")
        validate_env_secret(key, updates[key])
    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    out: list[str] = []
    for raw_line in lines:
        ending = "\n" if raw_line.endswith("\n") else ""
        line = raw_line[:-1] if ending else raw_line
        if line.endswith("\r"):
            line = line[:-1]
            ending = "\r\n"
        candidate = line[7:] if line.startswith("export ") else line
        key = candidate.split("=", 1)[0] if "=" in candidate else ""
        if key in updates:
            if key in seen:
                continue
            out.append(key + "=" + updates[key] + (ending or "\n"))
            seen.add(key)
        else:
            out.append(raw_line)
    for key, value in updates.items():
        if key not in seen:
            out.append(key + "=" + value + "\n")
    result = "".join(out).encode("utf-8")
    if len(result) > MAX_INPUT_BYTES:
        raise SyncError("env file is too large")
    return result


def read_env_file(path: Path, missing_ok: bool = True) -> Tuple[bytes, Dict[str, str]]:
    try:
        data = read_limited(path, MAX_INPUT_BYTES)
    except FileNotFoundError:
        if missing_ok:
            return b"", {}
        raise SyncError("live env file is missing") from None
    return data, parse_env(data)
