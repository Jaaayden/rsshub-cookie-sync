#!/usr/bin/env python3
"""RSSHub cookie synchronisation service.

This module intentionally uses only Python's standard library.  It is used on
the RSSHub host by a short-lived ``apply`` command and a systemd timer running
``monitor``.  Cookie values are kept in root-readable files only; state and
logs contain hashes and fixed error categories, never the values themselves.

The module is also deliberately dependency-injection friendly.  Tests (and a
future deployment wrapper) can provide an HTTP transport, command runner,
clock, and notifier without changing the state machine.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import getpass
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


LOG = logging.getLogger("rsshub-cookie-sync")

PROVIDERS: Tuple[str, ...] = ("zhihu", "weibo")
COOKIE_KEYS: Mapping[str, str] = {
    "zhihu": "ZHIHU_COOKIES",
    "weibo": "WEIBO_COOKIES",
}
MAX_INPUT_BYTES = 512 * 1024
MAX_COOKIE_BYTES = 256 * 1024
MAX_HTTP_BODY_BYTES = 128 * 1024
HASH_CHARS = 16

# These non-operational library defaults deliberately point at an
# unconfigured application directory rather than at any conventional RSSHub
# deployment.  Runtime CLI commands require a complete config.json, and the
# installer writes the operator's explicit target before invoking Docker.
DEFAULT_UNCONFIGURED_DIR = Path("/var/lib/rsshub-cookie-sync/unconfigured")
DEFAULT_COMPOSE_FILE = DEFAULT_UNCONFIGURED_DIR / "docker-compose.yml"
DEFAULT_LIVE_ENV = DEFAULT_UNCONFIGURED_DIR / "secrets/rsshub.env"
DEFAULT_CANDIDATE_DIR = DEFAULT_UNCONFIGURED_DIR / "secrets/candidates"
DEFAULT_STATE_FILE = Path("/var/lib/rsshub-cookie-sync/state.json")
# Keep the lock inode in persistent state.  A systemd oneshot service may
# remove RuntimeDirectory contents as soon as it exits; that would allow a
# concurrent apply process to lock a newly-created inode and bypass the
# monitor's lock.
DEFAULT_LOCK_FILE = Path("/var/lib/rsshub-cookie-sync/lock")
DEFAULT_CONFIG_FILE = Path("/etc/rsshub-cookie-sync/config.json")
DEFAULT_PROJECT = "rsshub"
DEFAULT_SERVICE = "rsshub"
# Compose resolves ``env_file`` entries relative to the Compose file.  Keep
# the default explicit so validation/migration can be used for a different
# service or a different deployment directory without embedding the old
# ``rsshub`` service name in their parsing logic.
DEFAULT_MANAGED_ENV_PATH = "./secrets/rsshub.env"

# The service receives login cookies and therefore must not become an SSRF
# primitive if its root-only configuration is accidentally edited.
ALLOWED_ZHIHU_HOST = "www.zhihu.com"
ALLOWED_WEIBO_HOST = "m.weibo.cn"
ALLOWED_RSSHUB_HOSTS = {"127.0.0.1", "localhost", "::1"}

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-fA-F]{12,128}$")
# These values are passed as individual Docker arguments, but validating them
# still prevents accidental option-like/ambiguous Compose names and keeps the
# migration parser's target unambiguous.  Project names follow Compose's
# documented lower-case form; service names may also contain dots.
COMPOSE_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
COMPOSE_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ALLOWED_PROBE_STATES = {"unknown", "ok", "auth_failed", "transient"}
ALLOWED_CANDIDATE_STATES = {None, "ok", "rejected_invalid", "retryable_error"}
ALLOWED_BOOTSTRAP_STATES = {"unknown", "seeded", "unseeded"}
ALLOWED_ERROR_CODES = {
    None,
    "invalid_cookie",
    "live_cookie_missing",
    "http_401",
    "http_403",
    "http_429",
    "http_432",
    "network_error",
    "moments_invalid_json",
    "moments_unauthorized",
    "profile_unauthorized",
    "profile_missing",
    "config_invalid_json",
    "config_unauthorized",
    "config_not_ok",
    "config_logged_out",
    "rsshub_health_network_error",
    "promotion_failed_rolled_back",
    "promotion_failed_rollback_failed",
    "post_probe_transient",
}


class SyncError(Exception):
    """Expected operational error with a safe, non-secret message."""


class InvalidInput(SyncError):
    pass


class ProbeError(SyncError):
    pass


class TransactionError(SyncError):
    pass


def now_seconds() -> int:
    return int(time.time())


class Clock:
    """Clock abstraction used by the state machine and tests."""

    def now(self) -> int:
        return now_seconds()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


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


def sha256_prefix(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()[:HASH_CHARS]


def validate_cookie_header(value: Any) -> str:
    """Validate a browser Cookie request header without normalising secrets."""

    validate_cookie_shape(value)
    assert isinstance(value, str)
    pairs = []
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise InvalidInput("cookieHeader contains an invalid cookie pair")
        name, cookie_value = part.split("=", 1)
        name = name.strip()
        if not name or not COOKIE_NAME_RE.fullmatch(name):
            raise InvalidInput("cookieHeader contains an invalid cookie name")
        pairs.append((name, cookie_value))
    if not pairs:
        raise InvalidInput("cookieHeader contains no cookies")
    return value


def validate_cookie_shape(value: Any) -> str:
    """Validate only bounded transport-safe input.

    The native host may send one valid provider and one malformed provider in
    the same request.  Shape validation therefore happens while decoding the
    envelope, while cookie-pair validation is performed per provider by
    ``apply`` so the valid provider can still be accepted independently.
    """

    if not isinstance(value, str):
        raise InvalidInput("cookieHeader must be a string")
    try:
        raw = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise InvalidInput("cookieHeader is not valid UTF-8") from exc
    if not raw or len(raw) > MAX_COOKIE_BYTES:
        raise InvalidInput("cookieHeader has an invalid length")
    if any(byte in raw for byte in (0, 10, 13)):
        raise InvalidInput("cookieHeader contains a forbidden control character")
    if any(byte < 32 or byte == 127 for byte in raw):
        raise InvalidInput("cookieHeader contains a forbidden control character")
    return value


def validate_provider(provider: Any) -> str:
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise InvalidInput("unknown provider")
    return provider


def build_manual_update_request(provider: Any, cookie_header: Any) -> Dict[str, Any]:
    """Build one normal apply request from a hidden interactive Cookie input."""

    provider_name = validate_provider(provider)
    cookie = validate_cookie_header(cookie_header)
    return {
        "version": 1,
        "providers": {provider_name: {"cookieHeader": cookie}},
    }


def _reject_duplicate_json_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput("request contains duplicate JSON keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise InvalidInput("request contains an invalid JSON number")


def strict_json_from_stdin(stream: Any = None, max_bytes: int = MAX_INPUT_BYTES) -> Dict[str, Any]:
    """Read exactly one strict JSON object from stdin, bounded in memory."""

    stream = stream or sys.stdin.buffer
    try:
        raw = stream.read(max_bytes + 1)
    except Exception as exc:
        raise InvalidInput("cannot read request") from exc
    if len(raw) > max_bytes:
        raise InvalidInput("request is too large")
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidInput, RecursionError) as exc:
        raise InvalidInput("request is not valid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidInput("request must be a JSON object")
    if set(value) != {"version", "providers"}:
        raise InvalidInput("request has unexpected fields")
    if type(value.get("version")) is not int or value.get("version") != 1:
        raise InvalidInput("unsupported request version")
    providers = value.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise InvalidInput("providers must be a non-empty object")
    for provider, item in providers.items():
        validate_provider(provider)
        if not isinstance(item, dict) or set(item) != {"cookieHeader"}:
            raise InvalidInput("provider payload has unexpected fields")
        validate_cookie_shape(item["cookieHeader"])
    return value


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
        validate_cookie_header(updates[key]) if key in COOKIE_KEYS.values() else None
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
        raise SyncError("live env file is missing")
    return data, parse_env(data)


def _default_provider_state() -> Dict[str, Any]:
    return {
        "live_hash": None,
        "candidate_hash": None,
        "candidate_received_at": None,
        "candidate_validated_at": None,
        "candidate_validation": None,
        "last_probe": "unknown",
        "last_probe_at": None,
        "last_success_at": None,
        "last_full_probe_at": None,
        "auth_failures": 0,
        "transient_failures": 0,
        "last_error": None,
    }


def _safe_error_code(value: Any) -> Optional[str]:
    if value in ALLOWED_ERROR_CODES:
        return value
    if isinstance(value, str) and re.fullmatch(r"http_[0-9]{3}", value):
        return value
    if isinstance(value, str) and re.fullmatch(r"rsshub_health_http_[0-9]{3}", value):
        return value
    return None


def default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": None,
        # ``unseeded`` is a valid first-install state: RSSHub itself has
        # passed bootstrap, but provider Cookie values have not been supplied
        # yet.  Keep this separate from provider probe state so a missing
        # Cookie is not mistaken for a failed login during installation.
        "bootstrap": {"status": "unknown"},
        "providers": {provider: _default_provider_state() for provider in PROVIDERS},
        "compose": {
            "last_probe": "unknown",
            "last_probe_at": None,
            "last_recreate_at": None,
            "last_error": None,
        },
        "notifications": {},
        "transaction": None,
    }


def _merge_state(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SyncError("state file has an unsupported format")
    state = default_state()
    if "updated_at" in value and (value["updated_at"] is None or type(value["updated_at"]) is int):
        state["updated_at"] = value["updated_at"]
    bootstrap = value.get("bootstrap")
    if isinstance(bootstrap, dict) and bootstrap.get("status") in ALLOWED_BOOTSTRAP_STATES:
        state["bootstrap"]["status"] = bootstrap["status"]
    if "transaction" in value and isinstance(value["transaction"], dict):
        # The marker is intentionally not exposed through status.  Preserve
        # only its shape so a crash-recovery check can still decide what to do.
        state["transaction"] = {"phase": str(value["transaction"].get("phase", ""))[:32]}
    if isinstance(value.get("notifications"), dict):
        state["notifications"] = {
            str(k): int(v)
            for k, v in value["notifications"].items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
    compose = value.get("compose")
    if isinstance(compose, dict):
        if compose.get("last_probe") in ALLOWED_PROBE_STATES:
            state["compose"]["last_probe"] = compose["last_probe"]
        for key in ("last_probe_at", "last_recreate_at"):
            if key in compose and (compose[key] is None or type(compose[key]) is int):
                state["compose"][key] = compose[key]
        safe_compose_error = _safe_error_code(compose.get("last_error"))
        if safe_compose_error is not None:
            state["compose"]["last_error"] = safe_compose_error
    providers = value.get("providers")
    if isinstance(providers, dict):
        for provider in PROVIDERS:
            item = providers.get(provider)
            if not isinstance(item, dict):
                continue
            target = state["providers"][provider]
            if item.get("candidate_validation") in ALLOWED_CANDIDATE_STATES:
                target["candidate_validation"] = item["candidate_validation"]
            if item.get("last_probe") in ALLOWED_PROBE_STATES:
                target["last_probe"] = item["last_probe"]
            safe_provider_error = _safe_error_code(item.get("last_error"))
            if safe_provider_error is not None:
                target["last_error"] = safe_provider_error
            for key in (
                "candidate_received_at",
                "candidate_validated_at",
                "last_probe_at",
                "last_success_at",
                "last_full_probe_at",
            ):
                if key in item and (item[key] is None or type(item[key]) is int):
                    target[key] = item[key]
            for key in ("live_hash", "candidate_hash"):
                if key in item and (item[key] is None or (isinstance(item[key], str) and re.fullmatch(r"[0-9a-f]{16}", item[key]))):
                    target[key] = item[key]
            for key in ("auth_failures", "transient_failures"):
                try:
                    target[key] = max(0, int(item.get(key, target[key])))
                except (TypeError, ValueError):
                    target[key] = 0
    return state


def load_state(path: Path) -> Dict[str, Any]:
    try:
        data = read_limited(path, MAX_INPUT_BYTES)
    except FileNotFoundError:
        return default_state()
    try:
        value = json.loads(data.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncError("state file is corrupt") from exc
    return _merge_state(value)


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    safe = json.loads(json.dumps(state, ensure_ascii=True, separators=(",", ":")))
    # A state file must never accidentally grow into a secret store.
    if len(json.dumps(safe, ensure_ascii=True).encode("utf-8")) > MAX_INPUT_BYTES:
        raise SyncError("state file is too large")
    safe["version"] = 1
    atomic_write(
        path,
        (json.dumps(safe, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        mode=0o600,
    )


def _validate_compose_project(value: Any) -> str:
    if not isinstance(value, str) or not COMPOSE_PROJECT_RE.fullmatch(value):
        raise SyncError("invalid compose project")
    return value


def _validate_compose_service(value: Any) -> str:
    if not isinstance(value, str) or not COMPOSE_SERVICE_RE.fullmatch(value):
        raise SyncError("invalid compose service")
    return value


def _normalise_managed_env_reference(value: Any) -> str:
    """Return a safe, Compose-relative ``env_file`` reference.

    Compose resolves an ``env_file`` path relative to the Compose file, so an
    absolute path or a traversal component would either be surprising or
    allow a configuration typo to make the migration write one file while
    RSSHub reads another.  Accept both ``secrets/rsshub.env`` and the more
    explicit ``./secrets/rsshub.env`` forms, but always emit the latter.
    """

    if isinstance(value, Path):
        value = value.as_posix()
    if not isinstance(value, str):
        raise SyncError("invalid managed env path")
    reference = value.strip()
    if not reference or any(ord(char) < 32 or ord(char) == 127 for char in reference):
        raise SyncError("invalid managed env path")
    if "\\" in reference or reference.startswith("/"):
        raise SyncError("invalid managed env path")
    if reference.startswith("./"):
        relative = reference[2:]
    else:
        relative = reference
    if not relative or relative == ".":
        raise SyncError("invalid managed env path")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SyncError("invalid managed env path")
    return "./" + "/".join(parts)


def _managed_env_reference(
    compose_file: Path,
    live_env: Path,
    managed_env_path: Optional[Any] = None,
) -> str:
    """Resolve the managed live env file to a Compose-relative reference.

    The live env is intentionally confined to the Compose deployment tree.
    This preserves the invariant that the file updated by the synchronizer
    is exactly the file consumed by the configured service.  A caller may
    provide the explicit Compose reference (useful when validating text); it
    must still resolve to ``live_env``.
    """

    compose_path = Path(compose_file)
    live_path = Path(live_env)
    compose_parent = compose_path.parent.resolve()
    resolved_live = live_path.resolve()
    try:
        relative = resolved_live.relative_to(compose_parent)
    except ValueError as exc:
        raise SyncError("live env must be inside compose directory") from exc
    if not relative.parts:
        raise SyncError("invalid managed env path")
    derived = _normalise_managed_env_reference(relative.as_posix())
    if managed_env_path is None:
        return derived
    if isinstance(managed_env_path, (Path, str)) and os.path.isabs(os.fspath(managed_env_path)):
        explicit_path = Path(managed_env_path).resolve()
        if explicit_path != resolved_live:
            raise SyncError("managed env path does not match live env")
        return derived
    reference = _normalise_managed_env_reference(managed_env_path)
    target = (compose_parent / reference[2:]).resolve()
    if target != resolved_live:
        raise SyncError("managed env path does not match live env")
    return reference


@dataclass(frozen=True)
class RuntimeConfig:
    compose_file: Path = DEFAULT_COMPOSE_FILE
    live_env: Path = DEFAULT_LIVE_ENV
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR
    state_file: Path = DEFAULT_STATE_FILE
    lock_file: Path = DEFAULT_LOCK_FILE
    config_file: Path = DEFAULT_CONFIG_FILE
    project: str = DEFAULT_PROJECT
    service: str = DEFAULT_SERVICE
    provider_timeout: float = 20.0
    health_timeout: float = 90.0
    health_poll_seconds: float = 1.0
    notification_cooldown: int = 6 * 60 * 60
    moments_interval: int = 60 * 60
    rsshub_base_url: str = "http://127.0.0.1:1200"
    rsshub_health_path: str = "/healthz"
    rsshub_access_key: Optional[str] = None
    zhihu_me_url: str = "https://www.zhihu.com/api/v4/me?include=is_realname"
    zhihu_moments_url: str = "https://www.zhihu.com/api/v3/moments?desktop=true&limit=1"
    weibo_config_url: str = "https://m.weibo.cn/api/config"
    bark_base_url: Optional[str] = None
    bark_device_key: Optional[str] = None
    bark_push_url: Optional[str] = None

    @classmethod
    def from_file(
        cls,
        path: Path = DEFAULT_CONFIG_FILE,
        *,
        require_file: bool = False,
        require_deployment: bool = False,
        **overrides: Any,
    ) -> "RuntimeConfig":
        path = Path(path)
        values: Dict[str, Any] = {"config_file": path}
        if require_file and not path.exists():
            raise SyncError("configuration is missing")
        if path.exists():
            try:
                info = path.lstat()
            except OSError as exc:
                raise SyncError("configuration is invalid") from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise SyncError("configuration permissions are unsafe")
            try:
                data = json.loads(read_limited(path, MAX_INPUT_BYTES).decode("utf-8", "strict"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SyncError("configuration is invalid") from exc
            if not isinstance(data, dict):
                raise SyncError("configuration is invalid")
            deployment = data.get("deployment", {})
            if deployment is None:
                deployment = {}
            if not isinstance(deployment, dict):
                raise SyncError("configuration is invalid")
            required_deployment_fields = {
                "compose_file",
                "live_env",
                "candidate_dir",
                "state_file",
                "lock_file",
                "project",
                "service",
            }
            if require_deployment and not required_deployment_fields.issubset(deployment):
                raise SyncError("configuration deployment is incomplete")
            for field in (
                "compose_file",
                "live_env",
                "candidate_dir",
                "state_file",
                "lock_file",
            ):
                configured = deployment.get(field)
                if configured is None:
                    continue
                if not isinstance(configured, str) or not configured.strip():
                    raise SyncError("configuration is invalid")
                values[field] = Path(configured)
            for field in ("project", "service"):
                configured = deployment.get(field)
                if configured is None:
                    continue
                if not isinstance(configured, str):
                    raise SyncError("configuration is invalid")
                values[field] = configured
            for section_name in ("rsshub", "providers", "bark", "timeouts"):
                if section_name in data and not isinstance(data[section_name], dict):
                    raise SyncError("configuration is invalid")
            rsshub = data.get("rsshub", {})
            providers = data.get("providers", {})
            for provider_name in PROVIDERS:
                if provider_name in providers and not isinstance(providers[provider_name], dict):
                    raise SyncError("configuration is invalid")
            zhihu = providers.get("zhihu", {})
            weibo = providers.get("weibo", {})
            bark = data.get("bark", {})
            timeouts = data.get("timeouts", {})
            values.update(
                {
                    "rsshub_base_url": rsshub.get("base_url", cls.rsshub_base_url),
                    "rsshub_health_path": rsshub.get("health_path", cls.rsshub_health_path),
                    "rsshub_access_key": rsshub.get("access_key"),
                    "zhihu_me_url": zhihu.get("me_url", cls.zhihu_me_url),
                    "zhihu_moments_url": zhihu.get("moments_url", cls.zhihu_moments_url),
                    "weibo_config_url": weibo.get("config_url", cls.weibo_config_url),
                    "provider_timeout": float(timeouts.get("provider_seconds", cls.provider_timeout)),
                    "health_timeout": float(timeouts.get("health_seconds", cls.health_timeout)),
                    "health_poll_seconds": float(timeouts.get("health_poll_seconds", cls.health_poll_seconds)),
                    "notification_cooldown": int(data.get("notification_cooldown", cls.notification_cooldown)),
                    "moments_interval": int(data.get("moments_interval_seconds", cls.moments_interval)),
                }
            )
            # The preferred form stores the Bark key in the JSON body.  A full
            # user-provided Bark URL is accepted for migration, but is never
            # emitted in status or logs and is converted in memory below.
            values["bark_base_url"] = bark.get("base_url")
            values["bark_device_key"] = bark.get("device_key")
            values["bark_push_url"] = bark.get("push_url") or bark.get("url")
        # ``None`` is the sentinel used by CLI options whose default is
        # intentionally unset.  It must not replace a deployment value read
        # above (nor turn a Path/string field into ``None``).
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)

    def provider_url(self, provider: str, full: bool) -> str:
        if provider == "zhihu":
            return self.zhihu_moments_url if full else self.zhihu_me_url
        if provider == "weibo":
            return self.weibo_config_url
        raise InvalidInput("unknown provider")

    def validate(self) -> None:
        _validate_compose_project(self.project)
        _validate_compose_service(self.service)
        for path in (
            self.compose_file,
            self.live_env,
            self.candidate_dir,
            self.state_file,
            self.lock_file,
        ):
            if (
                not isinstance(path, Path)
                or not path.is_absolute()
                or any(char in os.fspath(path) for char in ("\x00", "\r", "\n"))
            ):
                raise SyncError("configuration contains invalid deployment paths")
        # ``env_file`` references are relative to the Compose file.  Resolve
        # this once during validation so every migration/bootstrap path shares
        # the same deployment invariant.
        _managed_env_reference(self.compose_file, self.live_env)
        rsshub = urlsplit(self.rsshub_base_url)
        try:
            rsshub_port = rsshub.port
        except ValueError as exc:
            raise SyncError("configuration contains an invalid RSSHub URL") from exc
        if (
            rsshub.scheme != "http"
            or rsshub.hostname not in ALLOWED_RSSHUB_HOSTS
            or rsshub.username
            or rsshub.password
            or rsshub.path not in ("", "/")
            or rsshub.query
            or rsshub.fragment
            or (rsshub_port is not None and not 1 <= rsshub_port <= 65535)
        ):
            raise SyncError("configuration contains an invalid RSSHub URL")
        zhihu_me = urlsplit(self.zhihu_me_url)
        zhihu_moments = urlsplit(self.zhihu_moments_url)
        if (
            zhihu_me.scheme != "https"
            or zhihu_me.hostname != ALLOWED_ZHIHU_HOST
            or zhihu_me.path != "/api/v4/me"
            or zhihu_me.query != "include=is_realname"
            or zhihu_moments.scheme != "https"
            or zhihu_moments.hostname != ALLOWED_ZHIHU_HOST
            or zhihu_moments.path != "/api/v3/moments"
            or set(parse_qsl(zhihu_moments.query, keep_blank_values=True)) != {("desktop", "true"), ("limit", "1")}
        ):
            raise SyncError("configuration contains an invalid Zhihu URL")
        weibo = urlsplit(self.weibo_config_url)
        if weibo.scheme != "https" or weibo.hostname != ALLOWED_WEIBO_HOST or weibo.path != "/api/config" or weibo.query:
            raise SyncError("configuration contains an invalid Weibo URL")
        if self.rsshub_health_path != "/healthz" or any(
            not isinstance(value, str) or any(ord(char) < 32 for char in value)
            for value in (self.rsshub_health_path, self.rsshub_access_key or "")
        ):
            raise SyncError("configuration contains an invalid health setting")
        if self.rsshub_access_key is not None and not isinstance(self.rsshub_access_key, str):
            raise SyncError("configuration contains an invalid access key")
        numeric_ranges = (
            (self.provider_timeout, 0.1, 300.0),
            (self.health_timeout, 1.0, 1800.0),
            (self.health_poll_seconds, 0.0, 60.0),
            (self.notification_cooldown, 0.0, 31.0 * 24 * 60 * 60),
            (self.moments_interval, 0.0, 7.0 * 24 * 60 * 60),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
            for value, minimum, maximum in numeric_ranges
        ):
            raise SyncError("configuration contains an invalid timeout")
        if self.bark_base_url is not None:
            bark = urlsplit(self.bark_base_url)
            if (
                bark.scheme != "https"
                or bark.hostname != "api.day.app"
                or bark.path not in ("", "/")
                or bark.username
                or bark.password
                or bark.query
                or bark.fragment
            ):
                raise SyncError("configuration contains an invalid Bark URL")
        if self.bark_push_url is not None:
            bark = urlsplit(self.bark_push_url)
            if bark.scheme != "https" or bark.hostname != "api.day.app" or bark.username or bark.password:
                raise SyncError("configuration contains an invalid Bark URL")
        for value in (self.bark_device_key,):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value) > 512
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise SyncError("configuration contains an invalid Bark key")


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes


class HTTPTransport:
    """Small HTTP GET/POST transport with bounded response reads."""

    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(
            self,
            request: Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> Any:
            # Provider and notification endpoints are fixed; following a
            # redirect could send a Cookie or Bark key to another host.
            raise ProbeError("redirect rejected")

    def __init__(self) -> None:
        # An explicit empty ProxyHandler disables urllib's environment proxy
        # discovery.  Provider cookies, Bark device keys, and localhost
        # health requests must never be sent through HTTP(S)_PROXY/ALL_PROXY.
        self._opener = build_opener(ProxyHandler({}), self._NoRedirect())

    def request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 20.0,
    ) -> HTTPResponse:
        request = Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HTTPResponse(int(response.getcode()), response.read(MAX_HTTP_BODY_BYTES + 1))
        except HTTPError as exc:
            try:
                response_body = exc.read(MAX_HTTP_BODY_BYTES + 1)
            except Exception:
                response_body = b""
            return HTTPResponse(int(exc.code), response_body)
        except (URLError, TimeoutError, OSError) as exc:
            raise ProbeError("network error") from exc


@dataclass(frozen=True)
class ProbeResult:
    kind: str  # ok, auth_failed, transient
    status: Optional[int]
    reason: str

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def _response_json(response: HTTPResponse) -> Optional[Any]:
    if len(response.body) > MAX_HTTP_BODY_BYTES:
        return None
    try:
        return json.loads(response.body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _classify_status(status: int) -> str:
    if status == 401:
        return "auth_failed"
    # A 403 from these providers is often a WAF/rate-limit response.  Do not
    # rotate a valid login based on it; monitor it as a transient failure.
    if status in (403, 429, 432) or status >= 500:
        return "transient"
    if status < 200 or status >= 300:
        return "transient"
    return "ok"


def _looks_like_auth_error(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    candidates = [payload.get("code"), payload.get("error_code")]
    error = payload.get("error")
    if isinstance(error, dict):
        candidates.append(error.get("code"))
        candidates.append(error.get("status"))
    for candidate in candidates:
        if candidate in (401, "401", -401, "-401", 10001, "10001"):
            return True
    return False


def _valid_weibo_uid(value: Any) -> bool:
    """Accept only a positive integer UID or its canonical decimal string."""

    if type(value) is int:
        return value > 0
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,31}", value) is not None


class ProviderProber:
    def __init__(self, config: RuntimeConfig, transport: Optional[HTTPTransport] = None) -> None:
        self.config = config
        self.transport = transport or HTTPTransport()

    @staticmethod
    def _headers(cookie: str, provider: str) -> Dict[str, str]:
        common = {
            "Cookie": cookie,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        }
        if provider == "weibo":
            common.update(
                {
                    "Referer": "https://m.weibo.cn/",
                    "MWeibo-Pwa": "1",
                    "X-Requested-With": "XMLHttpRequest",
                }
            )
        return common

    def _get(self, provider: str, url: str, cookie: str) -> Tuple[Optional[HTTPResponse], ProbeResult]:
        try:
            response = self.transport.request(
                url,
                method="GET",
                headers=self._headers(cookie, provider),
                timeout=self.config.provider_timeout,
            )
        except ProbeError:
            return None, ProbeResult("transient", None, "network_error")
        if response.status == 401:
            return response, ProbeResult("auth_failed", response.status, "http_401")
        if response.status in (403, 429, 432) or response.status >= 500:
            return response, ProbeResult("transient", response.status, "http_" + str(response.status))
        if response.status < 200 or response.status >= 300:
            return response, ProbeResult("transient", response.status, "http_" + str(response.status))
        return response, ProbeResult("ok", response.status, "http_ok")

    def probe_zhihu(self, cookie: str, full: bool = False) -> ProbeResult:
        try:
            validate_cookie_header(cookie)
        except InvalidInput:
            return ProbeResult("auth_failed", None, "invalid_cookie")
        response, result = self._get("zhihu", self.config.zhihu_me_url, cookie)
        if result.kind != "ok" or response is None:
            return result
        payload = _response_json(response)
        if not isinstance(payload, dict) or not payload.get("name"):
            if _looks_like_auth_error(payload):
                return ProbeResult("auth_failed", response.status, "profile_unauthorized")
            return ProbeResult("transient", response.status, "profile_missing")
        if not full:
            return ProbeResult("ok", response.status, "profile_ok")
        response, result = self._get("zhihu", self.config.zhihu_moments_url, cookie)
        if result.kind != "ok" or response is None:
            return result
        payload = _response_json(response)
        if payload is None:
            return ProbeResult("transient", response.status, "moments_invalid_json")
        if _looks_like_auth_error(payload):
            return ProbeResult("auth_failed", response.status, "moments_unauthorized")
        return ProbeResult("ok", response.status, "moments_ok")

    def probe_weibo(self, cookie: str) -> ProbeResult:
        try:
            validate_cookie_header(cookie)
        except InvalidInput:
            return ProbeResult("auth_failed", None, "invalid_cookie")
        response, result = self._get("weibo", self.config.weibo_config_url, cookie)
        if result.kind != "ok" or response is None:
            return result
        payload = _response_json(response)
        if not isinstance(payload, dict):
            return ProbeResult("transient", response.status, "config_invalid_json")
        ok_value = payload.get("ok")
        if type(ok_value) is not int or ok_value != 1:
            if _looks_like_auth_error(payload):
                return ProbeResult("auth_failed", response.status, "config_unauthorized")
            return ProbeResult("auth_failed", response.status, "config_not_ok")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        login = data.get("login") if isinstance(data, dict) else None
        user = data.get("user") if isinstance(data, dict) else None
        uid = data.get("uid") if isinstance(data, dict) else None
        if not _valid_weibo_uid(uid) and isinstance(user, dict):
            uid = user.get("uid") or user.get("id")
        if login is not True or not _valid_weibo_uid(uid):
            return ProbeResult("auth_failed", response.status, "config_logged_out")
        return ProbeResult("ok", response.status, "config_ok")

    def probe(self, provider: str, cookie: str, full: bool = False) -> ProbeResult:
        validate_provider(provider)
        return self.probe_zhihu(cookie, full=full) if provider == "zhihu" else self.probe_weibo(cookie)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    """Subprocess runner which never writes command output to logs."""

    def run(self, args: Sequence[str], timeout: float, capture: bool = False) -> CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                env={
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "LC_ALL": "C",
                },
                timeout=timeout,
                check=False,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(124, "", "")
        except OSError:
            return CommandResult(127, "", "")
        # Even capture=True is only used for container IDs/status, not config
        # output.  Bound it before it reaches Python state.
        return CommandResult(
            int(completed.returncode),
            (completed.stdout or "")[:1024],
            (completed.stderr or "")[:1024],
        )


class DockerCompose:
    def __init__(
        self,
        config: RuntimeConfig,
        runner: Optional[CommandRunner] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner()
        self.clock = clock or Clock()

    def _compose(self) -> list[str]:
        _validate_compose_project(self.config.project)
        _validate_compose_service(self.config.service)
        return [
            "docker",
            "compose",
            "-p",
            self.config.project,
            "-f",
            str(self.config.compose_file),
        ]

    def config_quiet(self) -> bool:
        result = self.runner.run(self._compose() + ["config", "--quiet"], timeout=30.0)
        return result.returncode == 0

    def recreate(self) -> bool:
        result = self.runner.run(
            self._compose()
            + ["up", "-d", "--no-deps", "--force-recreate", "--pull", "never", self.config.service],
            timeout=180.0,
        )
        return result.returncode == 0

    def _container_id(self) -> Optional[str]:
        result = self.runner.run(
            self._compose() + ["ps", "-q", self.config.service], timeout=15.0, capture=True
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        return value if CONTAINER_ID_RE.fullmatch(value) else None

    def _inspect(self, container_id: str, field: str) -> Optional[str]:
        if not CONTAINER_ID_RE.fullmatch(container_id):
            return None
        result = self.runner.run(
            ["docker", "inspect", "--format=" + field, container_id],
            timeout=15.0,
            capture=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()[:64]

    def wait_healthy(self) -> bool:
        deadline = self.clock.now() + max(0.0, self.config.health_timeout)
        while self.clock.now() <= deadline:
            container_id = self._container_id()
            if container_id:
                status = self._inspect(container_id, "{{.State.Status}}")
                health = self._inspect(container_id, "{{if .State.Health}}{{.State.Health.Status}}{{end}}")
                if status == "running" and health in ("healthy", ""):
                    return True
                if status in ("exited", "dead", "created") or health == "unhealthy":
                    return False
            self.clock.sleep(min(self.config.health_poll_seconds, 1.0))
        return False


class BarkNotifier:
    def __init__(self, config: RuntimeConfig, transport: Optional[HTTPTransport] = None) -> None:
        self.config = config
        self.transport = transport or HTTPTransport()

    def _endpoint_and_key(self) -> Tuple[Optional[str], Optional[str]]:
        if self.config.bark_base_url and self.config.bark_device_key:
            parts = urlsplit(self.config.bark_base_url)
            if parts.scheme != "https" or parts.hostname != "api.day.app" or parts.path not in ("", "/"):
                return None, None
            return "https://api.day.app/push", self.config.bark_device_key
        # Migration-friendly support for a complete https://api.day.app/<key>/
        # URL.  It is parsed in memory and the key is put in the POST body.
        value = self.config.bark_push_url
        if value:
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                return None, None
            path_parts = [item for item in parts.path.split("/") if item]
            if not path_parts:
                return None, None
            key = path_parts[-1]
            if parts.scheme != "https" or parts.hostname != "api.day.app":
                return None, None
            return "https://api.day.app/push", key
        return None, None

    def enabled(self) -> bool:
        endpoint, key = self._endpoint_and_key()
        return bool(endpoint and key)

    def send(self, title: str, body: str) -> bool:
        endpoint, key = self._endpoint_and_key()
        if not endpoint or not key:
            return False
        payload = json.dumps(
            {
                "device_key": key,
                "title": title[:120],
                "body": body[:1000],
                "level": "active",
                "group": "rsshub-cookie-sync",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            response = self.transport.request(
                endpoint,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=payload,
                timeout=15.0,
            )
        except ProbeError:
            return False
        return 200 <= response.status < 300


def _append_query(url: str, key: Optional[str]) -> str:
    if not key:
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("key", key))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class SyncService:
    def __init__(
        self,
        config: RuntimeConfig,
        transport: Optional[HTTPTransport] = None,
        runner: Optional[CommandRunner] = None,
        notifier: Optional[BarkNotifier] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        config.validate()
        self.config = config
        self.clock = clock or Clock()
        self.transport = transport or HTTPTransport()
        self.prober = ProviderProber(config, self.transport)
        self.docker = DockerCompose(config, runner=runner, clock=self.clock)
        self.notifier = notifier or BarkNotifier(config, self.transport)
        self.prev_env = config.live_env.with_name(config.live_env.name + ".prev")
        self.transaction_file = config.live_env.with_name(config.live_env.name + ".txn.json")

    def _prepare_storage(self) -> None:
        ensure_directory(self.config.live_env.parent)
        ensure_directory(self.config.candidate_dir)
        ensure_directory(self.config.state_file.parent)
        ensure_directory(self.config.lock_file.parent)
        try:
            os.chmod(self.config.live_env.parent, 0o700)
            os.chmod(self.config.candidate_dir, 0o700)
        except OSError as exc:
            raise SyncError("cannot secure secret directory") from exc

    def _candidate_path(self, provider: str) -> Path:
        validate_provider(provider)
        return self.config.candidate_dir / (provider + ".cookie")

    def _read_candidate(self, provider: str) -> Optional[str]:
        path = self._candidate_path(provider)
        try:
            data = read_limited(path, MAX_COOKIE_BYTES)
        except FileNotFoundError:
            return None
        try:
            value = data.decode("utf-8", "strict")
            validate_cookie_header(value)
        except (UnicodeDecodeError, InvalidInput, SyncError):
            return None
        return value

    def _save_candidate(self, provider: str, cookie: str) -> None:
        validate_cookie_header(cookie)
        atomic_write(self._candidate_path(provider), cookie.encode("utf-8"), mode=0o600)

    def _remove_candidate(self, provider: str) -> None:
        # Candidate files contain bearer credentials.  Always scrub them
        # before unlinking so a stale session cannot remain recoverable in a
        # deleted inode.  ``secure_remove`` also rejects symlinks and other
        # non-regular paths instead of following or deleting them.
        secure_remove(self._candidate_path(provider))

    @staticmethod
    def _clear_candidate_metadata(item: MutableMapping[str, Any]) -> None:
        """Forget all metadata for a candidate that has been discarded."""

        item["candidate_hash"] = None
        item["candidate_received_at"] = None
        item["candidate_validated_at"] = None
        item["candidate_validation"] = None

    def _read_live(self) -> Tuple[bytes, Dict[str, str]]:
        return read_env_file(self.config.live_env, missing_ok=True)

    @staticmethod
    def _set_bootstrap_status(state: MutableMapping[str, Any], live_values: Mapping[str, str]) -> str:
        """Record whether both provider Cookies have been seeded.

        An empty/absent pair is the expected state immediately after a fresh
        RSSHub install.  A partial pair remains ``unknown`` so bootstrap and
        operators cannot accidentally treat an incomplete configuration as a
        healthy seeded installation.
        """

        present = [bool(live_values.get(COOKIE_KEYS[provider], "")) for provider in PROVIDERS]
        if all(present):
            status = "seeded"
        elif not any(present):
            status = "unseeded"
        else:
            status = "unknown"
        bootstrap = state.setdefault("bootstrap", {})
        bootstrap["status"] = status
        return status

    def _safe_notification(self, state: MutableMapping[str, Any], event: str, title: str, body: str) -> bool:
        notifications = state.setdefault("notifications", {})
        last = notifications.get(event)
        current = self.clock.now()
        if isinstance(last, (int, float)) and current - int(last) < self.config.notification_cooldown:
            return False
        sent = self.notifier.send(title, body) if self.notifier else False
        # Record attempted event to de-duplicate even when Bark is temporarily
        # unavailable; a later recovery event communicates the useful state.
        notifications[event] = current
        return sent

    def _set_probe_state(self, state: MutableMapping[str, Any], provider: str, result: ProbeResult, full: bool = False) -> None:
        item = state["providers"][provider]
        previous_kind = item.get("last_probe")
        item["last_probe"] = result.kind
        item["last_probe_at"] = self.clock.now()
        item["last_error"] = None if result.kind == "ok" else result.reason
        if full:
            item["last_full_probe_at"] = self.clock.now()
        if result.kind == "ok":
            item["auth_failures"] = 0
            item["transient_failures"] = 0
            item["last_success_at"] = self.clock.now()
        elif result.kind == "auth_failed":
            item["auth_failures"] = int(item.get("auth_failures") or 0) + 1
            item["transient_failures"] = 0
        else:
            item["transient_failures"] = int(item.get("transient_failures") or 0) + 1
            item["auth_failures"] = 0

    def _health_probe(self) -> ProbeResult:
        url = self.config.rsshub_base_url.rstrip("/") + "/" + self.config.rsshub_health_path.lstrip("/")
        url = _append_query(url, self.config.rsshub_access_key)
        try:
            response = self.transport.request(url, timeout=10.0)
        except ProbeError:
            return ProbeResult("transient", None, "rsshub_health_network_error")
        if response.status == 200:
            return ProbeResult("ok", response.status, "rsshub_health_ok")
        return ProbeResult("transient", response.status, "rsshub_health_http_" + str(response.status))

    def _transaction_value(self, provider_updates: Mapping[str, str], old_env: bytes, phase: str) -> Dict[str, Any]:
        return {
            "version": 1,
            "phase": phase,
            "providers": sorted(provider_updates),
            "old_hashes": {p: sha256_prefix(parse_env(old_env).get(COOKIE_KEYS[p], "")) for p in provider_updates},
            "new_hashes": {p: sha256_prefix(v) for p, v in provider_updates.items()},
            "started_at": self.clock.now(),
        }

    def _save_transaction(self, value: Mapping[str, Any]) -> None:
        atomic_write(
            self.transaction_file,
            (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            mode=0o600,
        )

    def _load_transaction(self) -> Optional[Dict[str, Any]]:
        try:
            data = read_limited(self.transaction_file, MAX_INPUT_BYTES)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(data.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransactionError("transaction marker is corrupt") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise TransactionError("transaction marker is invalid")
        return value

    def _remove_transaction_files(self) -> None:
        for path in (self.transaction_file, self.prev_env):
            try:
                if path == self.prev_env:
                    secure_remove(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise TransactionError("cannot clean transaction files") from exc

    def recover_transaction(self) -> None:
        marker = self._load_transaction()
        if marker is None:
            # The backup is written immediately before the marker.  If the
            # process died in that tiny window there is no live replacement to
            # recover, so this is a safe orphan to remove rather than retaining
            # a second copy of the old Cookie indefinitely.
            if self.prev_env.exists():
                secure_remove(self.prev_env)
            return
        phase = str(marker.get("phase", ""))
        if phase in ("committed", "rollback_complete"):
            self._remove_transaction_files()
            return
        if not self.prev_env.exists():
            raise TransactionError("transaction backup is missing")
        # The marker exists before the live replacement, so restoring the
        # backup is safe even if the process was killed during preparation.
        secure_copy(self.prev_env, self.config.live_env, mode=0o600)
        if phase in ("config_validated", "recreated", "healthy", "rolling_back", "rollback_failed"):
            if self.docker.config_quiet() and self.docker.recreate() and self.docker.wait_healthy():
                health = self._health_probe()
                if not health.ok:
                    raise TransactionError("transaction recovery health check failed")
        self._remove_transaction_files()

    def _promote(self, updates: Mapping[str, str], state: MutableMapping[str, Any], reason: str) -> None:
        if not updates:
            return
        for provider, cookie in updates.items():
            validate_provider(provider)
            validate_cookie_header(cookie)
        old_data, old_values = self._read_live()
        effective_updates = {
            provider: cookie
            for provider, cookie in updates.items()
            if old_values.get(COOKIE_KEYS[provider], "") != cookie
        }
        if not effective_updates:
            return
        updates = effective_updates
        if not old_data:
            # An empty live file is valid for a first installation only when
            # Compose already declares the file; use an empty base otherwise.
            old_data = b""
        backup = self._transaction_value(updates, old_data, "prepared")
        atomic_write(self.prev_env, old_data, mode=0o600)
        self._save_transaction(backup)
        try:
            new_data = render_env(old_data, {COOKIE_KEYS[p]: v for p, v in updates.items()})
            atomic_write(self.config.live_env, new_data, mode=0o600)
            backup["phase"] = "live_replaced"
            self._save_transaction(backup)
            if not self.docker.config_quiet():
                raise TransactionError("compose config validation failed")
            backup["phase"] = "config_validated"
            self._save_transaction(backup)
            if not self.docker.recreate():
                raise TransactionError("rsshub recreate failed")
            backup["phase"] = "recreated"
            self._save_transaction(backup)
            if not self.docker.wait_healthy():
                raise TransactionError("rsshub container is not healthy")
            backup["phase"] = "healthy"
            self._save_transaction(backup)
            health = self._health_probe()
            if not health.ok:
                raise TransactionError("rsshub health check failed")
            post_transient: Dict[str, ProbeResult] = {}
            for provider, cookie in updates.items():
                result = self.prober.probe(provider, cookie, full=True)
                if result.kind == "auth_failed":
                    # A candidate that was valid before recreation should not
                    # silently become the live configuration if it is now
                    # clearly rejected.  Roll back the complete transaction.
                    raise TransactionError("post-recreate provider auth check failed")
                if result.kind == "transient":
                    # A temporary 429/432/5xx from one provider must not undo
                    # a successfully installed candidate for another provider.
                    # Keep the new env, record the transient status, and let
                    # the monitor retry it on its normal cadence.
                    post_transient[provider] = result
            backup["phase"] = "committed"
            self._save_transaction(backup)
            for provider in updates:
                self._remove_candidate(provider)
                item = state["providers"][provider]
                item["live_hash"] = sha256_prefix(updates[provider])
                self._clear_candidate_metadata(item)
                item["last_probe"] = "transient" if provider in post_transient else "ok"
                item["last_probe_at"] = self.clock.now()
                if provider in post_transient:
                    item["last_success_at"] = item.get("last_success_at")
                    item["auth_failures"] = 0
                    item["transient_failures"] = int(item.get("transient_failures") or 0) + 1
                    item["last_error"] = post_transient[provider].reason
                else:
                    item["last_success_at"] = self.clock.now()
                    item["auth_failures"] = 0
                    item["transient_failures"] = 0
                    item["last_error"] = None
                self._safe_notification(
                    state,
                    "promoted:" + provider,
                    "RSSHub Cookie 已自动更新",
                    provider + " 登录态已验证并完成切换。",
                )
            state["compose"]["last_recreate_at"] = self.clock.now()
            state["compose"]["last_probe"] = "ok"
            state["compose"]["last_probe_at"] = self.clock.now()
            state["compose"]["last_error"] = None
            # Keep the installation marker in sync after a successful
            # transaction.  The first provider promotion from an empty env is
            # intentionally still ``unseeded`` until the second provider is
            # supplied.
            _, live_values = self._read_live()
            self._set_bootstrap_status(state, live_values)
            self._remove_transaction_files()
        except Exception as exc:
            # Keep the original exception category safe; rollback itself is
            # attempted before the caller is told that promotion failed.
            try:
                backup["phase"] = "rolling_back"
                self._save_transaction(backup)
                secure_copy(self.prev_env, self.config.live_env, mode=0o600)
                if not self.docker.config_quiet() or not self.docker.recreate() or not self.docker.wait_healthy():
                    raise TransactionError("rollback recreate failed")
                health = self._health_probe()
                if not health.ok:
                    raise TransactionError("rollback health check failed")
                backup["phase"] = "rollback_complete"
                self._save_transaction(backup)
                self._remove_transaction_files()
                state["compose"]["last_error"] = "promotion_failed_rolled_back"
                for provider in updates:
                    state["providers"][provider]["last_error"] = "promotion_failed_rolled_back"
                self._safe_notification(
                    state,
                    "rollback",
                    "RSSHub Cookie 更新失败，已回滚",
                    "新配置未通过重建或登录态检查，服务已恢复旧配置。",
                )
            except Exception:
                # Leave marker + backup in place.  The next monitor/apply run
                # will retry recovery while the operator gets a safe alert.
                state["compose"]["last_error"] = "promotion_failed_rollback_failed"
                self._safe_notification(
                    state,
                    "rollback_failed",
                    "RSSHub Cookie 回滚失败，需要人工处理",
                    "自动更新事务无法恢复旧配置，请检查服务器上的服务状态。",
                )
            raise TransactionError("promotion failed and rollback was attempted") from exc

    def apply(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"version", "providers"}:
            raise InvalidInput("request has unexpected fields")
        # Reuse the same validation rules for callers that already decoded JSON.
        providers = payload.get("providers")
        if type(payload.get("version")) is not int or payload.get("version") != 1 or not isinstance(providers, dict) or not providers:
            raise InvalidInput("invalid request")
        cookie_values: Dict[str, str] = {}
        invalid_providers: set[str] = set()
        for provider, item in providers.items():
            validate_provider(provider)
            if not isinstance(item, dict) or set(item) != {"cookieHeader"}:
                raise InvalidInput("invalid provider payload")
            try:
                cookie_values[provider] = validate_cookie_header(item["cookieHeader"])
            except InvalidInput:
                # Keep independent provider updates independent: a malformed
                # Weibo value must not prevent a valid Zhihu candidate from
                # being validated and stored.
                invalid_providers.add(provider)

        with file_lock(self.config.lock_file):
            self._prepare_storage()
            self.recover_transaction()
            state = load_state(self.config.state_file)
            live_data, live_values = self._read_live()
            results: Dict[str, str] = {}
            to_promote: Dict[str, str] = {}
            for provider in invalid_providers:
                results[provider] = "rejected_invalid"
            for provider, cookie in cookie_values.items():
                result = self.prober.probe(provider, cookie, full=True)
                if result.kind == "auth_failed":
                    results[provider] = "rejected_invalid"
                    continue
                if result.kind != "ok":
                    results[provider] = "retryable_error"
                    # A failed upload was never written as the candidate.  Do
                    # not invalidate metadata for an older, verified file;
                    # otherwise one bad or rate-limited upload could silently
                    # remove the only available recovery path.
                    continue
                key = COOKIE_KEYS[provider]
                current = live_values.get(key, "")
                if current == cookie:
                    item = state["providers"][provider]
                    try:
                        # The upload confirms the current live session is
                        # still valid.  Discard any older candidate before
                        # clearing its metadata, otherwise a later live
                        # failure could promote that stale session.
                        self._remove_candidate(provider)
                    except SyncError:
                        # Do not claim the stale candidate was removed.  Keep
                        # its metadata and mark the operation retryable so a
                        # subsequent upload can retry the secure cleanup.
                        item["candidate_validation"] = "retryable_error"
                        item["live_hash"] = sha256_prefix(cookie)
                        results[provider] = "retryable_error"
                        continue
                    self._clear_candidate_metadata(item)
                    results[provider] = "unchanged"
                    item["live_hash"] = sha256_prefix(cookie)
                    continue
                candidate = self._read_candidate(provider)
                if candidate == cookie:
                    results[provider] = "unchanged"
                else:
                    self._save_candidate(provider, cookie)
                    results[provider] = "candidate_saved"
                item = state["providers"][provider]
                item["candidate_hash"] = sha256_prefix(cookie)
                item["candidate_received_at"] = self.clock.now()
                item["candidate_validated_at"] = self.clock.now()
                item["candidate_validation"] = "ok"
                item["live_hash"] = sha256_prefix(current) if current else None

                # A fresh valid candidate repairs a known failed live login
                # immediately.  If the state is not yet at the two-failure
                # threshold, one direct live check still enables immediate
                # repair after the Edge sends a replacement.
                live_needs_repair = int(item.get("auth_failures") or 0) >= 2
                if not live_needs_repair and current:
                    live_result = self.prober.probe(provider, current, full=False)
                    live_needs_repair = live_result.kind == "auth_failed"
                if live_needs_repair or not current:
                    to_promote[provider] = cookie
            if to_promote:
                self._promote(to_promote, state, reason="fresh_candidate")
                for provider in to_promote:
                    results[provider] = "promoted"
            state["updated_at"] = self.clock.now()
            save_state(self.config.state_file, state)
            # Native Messaging deliberately accepts only {"status": ...}.
            # The Edge extension sends one provider per request; multi-provider
            # callers get a deterministic aggregate status using the same
            # vocabulary rather than an unbounded per-provider response.
            for status in ("promoted", "candidate_saved", "unchanged", "rejected_invalid", "retryable_error"):
                if status in results.values():
                    return {"status": status}
            return {"status": "retryable_error"}

    def _maybe_full_probe(self, provider: str, cookie: str, item: Mapping[str, Any]) -> Tuple[ProbeResult, bool]:
        current = self.clock.now()
        last_full = item.get("last_full_probe_at")
        full = not isinstance(last_full, (int, float)) or current - int(last_full) >= self.config.moments_interval
        return self.prober.probe(provider, cookie, full=full), full

    def monitor(self) -> Dict[str, Any]:
        with file_lock(self.config.lock_file):
            self._prepare_storage()
            self.recover_transaction()
            state = load_state(self.config.state_file)
            previous_health_kind = state["compose"].get("last_probe")
            health = self._health_probe()
            state["compose"]["last_probe"] = health.kind
            state["compose"]["last_probe_at"] = self.clock.now()
            if health.ok:
                state["compose"]["last_error"] = None
                if previous_health_kind in ("auth_failed", "transient"):
                    self._safe_notification(
                        state,
                        "recovered:rsshub",
                        "RSSHub 服务已恢复",
                        "RSSHub 本机健康检查已恢复正常。",
                    )
            else:
                state["compose"]["last_error"] = health.reason
                self._safe_notification(
                    state,
                    "rsshub_unhealthy",
                    "RSSHub 健康检查失败",
                    "RSSHub 进程或本机健康端点不可用，请检查服务状态。",
                )

            _, live_values = self._read_live()
            self._set_bootstrap_status(state, live_values)
            to_promote: Dict[str, str] = {}
            for provider in PROVIDERS:
                item = state["providers"][provider]
                cookie = live_values.get(COOKIE_KEYS[provider], "")
                if not cookie:
                    result = ProbeResult("auth_failed", None, "live_cookie_missing")
                    full = False
                else:
                    result, full = self._maybe_full_probe(provider, cookie, item)
                previous_failures = int(item.get("auth_failures") or 0)
                previous_transient = int(item.get("transient_failures") or 0)
                previous_kind = item.get("last_probe")
                self._set_probe_state(state, provider, result, full=full)
                item["live_hash"] = sha256_prefix(cookie) if cookie else None

                if result.kind == "ok":
                    if previous_kind in ("auth_failed", "transient") and (previous_failures or previous_transient):
                        self._safe_notification(
                            state,
                            "recovered:" + provider,
                            "RSSHub 上游已恢复",
                            provider + " 登录态探针已恢复正常。",
                        )
                    continue
                if result.kind == "auth_failed" and int(item.get("auth_failures") or 0) >= 2:
                    candidate = self._read_candidate(provider)
                    # A candidate whose secure cleanup previously failed is
                    # deliberately quarantined in metadata.  Do not promote
                    # it during a later outage: the live-equivalent upload
                    # must first retry and complete candidate removal.
                    if candidate and item.get("candidate_validation") == "ok":
                        candidate_result = self.prober.probe(provider, candidate, full=True)
                        if candidate_result.ok:
                            to_promote[provider] = candidate
                        elif candidate_result.kind == "auth_failed":
                            item["candidate_validation"] = "rejected_invalid"
                            self._safe_notification(
                                state,
                                "auth_no_candidate:" + provider,
                                "RSSHub 登录态失效，需要重新登录",
                                provider + " 当前 Cookie 与候选 Cookie 均不可用，请在 Edge 重新登录。",
                            )
                        else:
                            # A temporary failure does not contradict the last
                            # successful validation.  Keep the candidate in
                            # the retry queue so the next monitor cycle can
                            # promote it after the upstream recovers.
                            pass
                    else:
                        self._safe_notification(
                            state,
                            "auth_no_candidate:" + provider,
                            "RSSHub 登录态失效，需要重新登录",
                            provider + " 没有可用候选 Cookie，请在 Edge 重新登录。",
                        )
                elif result.kind == "transient" and int(item.get("transient_failures") or 0) >= 4:
                    self._safe_notification(
                        state,
                        "transient:" + provider,
                        "RSSHub 上游连接持续异常",
                        provider + " 连续多次探针失败，暂不更换 Cookie。",
                    )

            if to_promote:
                try:
                    self._promote(to_promote, state, reason="monitor_auth_failure")
                except TransactionError:
                    # _promote records a safe notification and leaves the
                    # candidate intact for the next monitor cycle.
                    pass
            state["updated_at"] = self.clock.now()
            save_state(self.config.state_file, state)
            return self.public_status(state)

    def bootstrap(self) -> Dict[str, Any]:
        """Validate the migrated live configuration before enabling the timer.

        This is intentionally a separate, explicit installation step.  It
        recreates only the RSSHub service (never ``pull``/``down``/Redis), then
        checks the process health endpoint.  Existing installations with both
        provider Cookies also run both provider login probes.  A fresh
        installation has no provider Cookies yet; it is marked ``unseeded``
        after the Compose/RSSHub checks and can receive its first valid Cookie
        through ``apply``.  A partial pair is rejected instead of being
        treated as a fresh installation.

        A failed bootstrap leaves the existing env file untouched and the
        caller must not enable the monitor timer.
        """

        with file_lock(self.config.lock_file):
            self._prepare_storage()
            self.recover_transaction()
            try:
                compose_text = read_limited(self.config.compose_file, 2 * 1024 * 1024).decode("utf-8", "strict")
            except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
                raise SyncError("compose file cannot be read") from exc
            managed_reference = _managed_env_reference(self.config.compose_file, self.config.live_env)
            _validate_migrated_compose_text(compose_text, self.config.service, managed_reference)
            state = load_state(self.config.state_file)
            _, values = self._read_live()
            cookies = {provider: values.get(COOKIE_KEYS[provider], "") for provider in PROVIDERS}
            missing = [provider for provider, cookie in cookies.items() if not cookie]
            if missing and len(missing) != len(PROVIDERS):
                raise SyncError("live provider cookie is missing")
            if not self.docker.config_quiet():
                raise SyncError("compose config validation failed")
            if not self.docker.recreate() or not self.docker.wait_healthy():
                raise SyncError("rsshub bootstrap recreate failed")
            health = self._health_probe()
            if not health.ok:
                raise SyncError("rsshub bootstrap health check failed")
            unseeded = len(missing) == len(PROVIDERS)
            if unseeded:
                # Do not probe provider endpoints with an empty Cookie.  The
                # first valid candidate is promoted by apply() because its
                # corresponding live value is absent.
                self._set_bootstrap_status(state, cookies)
            else:
                for provider, cookie in cookies.items():
                    result = self.prober.probe(provider, cookie, full=True)
                    if not result.ok:
                        raise SyncError("rsshub bootstrap provider check failed")
                    item = state["providers"][provider]
                    item["live_hash"] = sha256_prefix(cookie)
                    item["last_probe"] = "ok"
                    item["last_probe_at"] = self.clock.now()
                    item["last_full_probe_at"] = self.clock.now()
                    item["last_success_at"] = self.clock.now()
                    item["auth_failures"] = 0
                    item["transient_failures"] = 0
                    item["last_error"] = None
                self._set_bootstrap_status(state, cookies)
            state["compose"]["last_probe"] = "ok"
            state["compose"]["last_probe_at"] = self.clock.now()
            state["compose"]["last_recreate_at"] = self.clock.now()
            state["compose"]["last_error"] = None
            state["updated_at"] = self.clock.now()
            save_state(self.config.state_file, state)
            result: Dict[str, Any] = {"version": 1, "bootstrapped": True}
            if unseeded:
                result["unseeded"] = True
            return result

    def public_status(self, state: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if state is None:
            state = load_state(self.config.state_file)
        # State is already constructed without secrets.  Selectively copy it
        # to ensure a future internal field cannot leak through status.
        providers: Dict[str, Any] = {}
        for provider in PROVIDERS:
            source = state.get("providers", {}).get(provider, {}) if isinstance(state, dict) else {}
            providers[provider] = {
                key: source.get(key)
                for key in (
                    "candidate_received_at",
                    "candidate_validated_at",
                    "candidate_validation",
                    "last_probe",
                    "last_probe_at",
                    "last_success_at",
                    "last_full_probe_at",
                    "auth_failures",
                    "transient_failures",
                    "last_error",
                )
            }
        compose_source = state.get("compose", {}) if isinstance(state, dict) else {}
        bootstrap_source = state.get("bootstrap", {}) if isinstance(state, dict) else {}
        bootstrap_status = (
            bootstrap_source.get("status")
            if isinstance(bootstrap_source, dict)
            and bootstrap_source.get("status") in ALLOWED_BOOTSTRAP_STATES
            else "unknown"
        )
        return {
            "version": 1,
            "updated_at": state.get("updated_at") if isinstance(state, dict) else None,
            "bootstrap": {"status": bootstrap_status},
            "providers": providers,
            "compose": {
                key: compose_source.get(key)
                for key in ("last_probe", "last_probe_at", "last_recreate_at", "last_error")
            },
            "transaction_pending": bool(self.transaction_file.exists() or self.prev_env.exists()),
        }

    def status(self) -> Dict[str, Any]:
        with file_lock(self.config.lock_file):
            self._prepare_storage()
            return self.public_status()

    def notify_test(self) -> bool:
        return bool(self.notifier and self.notifier.send("RSSHub Cookie 同步测试", "Bark 通知配置正常。"))


_COMPOSE_SECRET_KEYS = frozenset(set(COOKIE_KEYS.values()) | {"TWITTER_AUTH_TOKEN"})
_COMPOSE_KEY_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*:(?P<value>.*)$")


def _compose_code_line(raw: str) -> Optional[Tuple[str, int, str]]:
    """Return ``(line, indent, stripped)`` for a non-comment YAML line."""

    line = raw.rstrip("\r\n")
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    prefix = line[: len(line) - len(line.lstrip(" "))]
    if "\t" in prefix:
        raise SyncError("compose indentation uses tabs")
    return line, len(prefix), stripped


def _compose_mapping(line: str) -> Optional[Tuple[int, str, str]]:
    match = _COMPOSE_KEY_RE.match(line)
    if not match:
        return None
    return len(match.group("indent")), match.group("key"), match.group("value")


def _compose_scalar(value: str) -> str:
    value = value.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        end = value.rfind(quote)
        if end < 1 or (value[end + 1 :].strip() and not value[end + 1 :].lstrip().startswith("#")):
            raise SyncError("compose scalar quoting is ambiguous")
        value = value[1:end]
    elif " #" in value:
        # YAML treats a # preceded by whitespace as a comment delimiter.  A
        # cookie value containing a literal '#' without preceding whitespace
        # remains untouched.
        value = value.split(" #", 1)[0].rstrip()
    return value


def _compose_service_block(
    lines: Sequence[str], service_name: str = DEFAULT_SERVICE
) -> Tuple[int, int, int, int]:
    """Locate the unique configured service mapping block."""

    _validate_compose_service(service_name)

    services: Optional[Tuple[int, int]] = None
    for index, raw in enumerate(lines):
        item = _compose_code_line(raw)
        if item is None:
            continue
        line, indent, _ = item
        mapping = _compose_mapping(line)
        if mapping and indent == 0 and mapping[1] == "services":
            if services is not None:
                raise SyncError("compose has duplicate services mappings")
            services = (index, indent)
    if services is None:
        raise SyncError("compose services mapping was not found")
    services_index, services_indent = services
    services_end = len(lines)
    for index in range(services_index + 1, len(lines)):
        item = _compose_code_line(lines[index])
        if item is not None and item[1] <= services_indent:
            services_end = index
            break

    matches: list[Tuple[int, int]] = []
    for index in range(services_index + 1, services_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and mapping[1] == service_name and item[1] > services_indent:
            matches.append((index, item[1]))
    if len(matches) != 1:
        raise SyncError("configured service was not found uniquely in compose file")
    service_index, service_indent = matches[0]
    service_end = len(lines)
    for index in range(service_index + 1, services_end):
        item = _compose_code_line(lines[index])
        if item is not None and item[1] <= service_indent:
            service_end = index
            break
    return service_index, service_end, service_indent, services_end


def _infer_service_child_indent(
    lines: Sequence[str], service_index: int, service_end: int, service_indent: int
) -> int:
    """Infer the actual indentation of direct service child keys."""

    first: Optional[Tuple[int, int, str]] = None
    for index in range(service_index + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is not None:
            first = (index, item[1], item[0])
            break
    if first is None:
        raise SyncError("configured service has no child keys")
    _, child_indent, first_line = first
    if child_indent <= service_indent or _compose_mapping(first_line) is None:
        raise SyncError("configured service child indentation is ambiguous")
    for index in range(first[0] + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and service_indent < item[1] < child_indent:
            raise SyncError("configured service child indentation is inconsistent")
    return child_indent


def _compose_node_end(lines: Sequence[str], start: int, end: int, parent_indent: int) -> int:
    for index in range(start + 1, end):
        item = _compose_code_line(lines[index])
        if item is not None and item[1] <= parent_indent:
            return index
    return end


def _secret_assignment(line: str) -> Optional[Tuple[str, str]]:
    """Parse one direct ``environment`` mapping/list assignment."""

    mapping = _compose_mapping(line)
    if mapping and mapping[1] in _COMPOSE_SECRET_KEYS:
        return mapping[1], _compose_scalar(mapping[2])
    stripped = line.strip()
    # YAML permits quoted mapping keys.  Support only the three exact managed
    # names; do not turn this conservative editor into a general YAML parser.
    for key in _COMPOSE_SECRET_KEYS:
        for quote in ("'", '"'):
            prefix = f"{quote}{key}{quote}"
            if stripped.startswith(prefix):
                remainder = stripped[len(prefix) :].lstrip()
                if remainder.startswith(":"):
                    return key, _compose_scalar(remainder[1:])
    if stripped.startswith("-"):
        # Compose also accepts the complete KEY=value list scalar in quotes,
        # for example `- "ZHIHU_COOKIES=z_c0=..."`.
        raw_item = stripped[1:].strip()
        whole_item_quoted = raw_item.startswith(("'", '"'))
        item = _compose_scalar(raw_item)
        for key in _COMPOSE_SECRET_KEYS:
            if item.startswith(key + "="):
                value = item[len(key) + 1 :]
                return key, value if whole_item_quoted else _compose_scalar(value)
    return None


def _looks_like_unparsed_secret_assignment(line: str) -> bool:
    """Fail closed when a direct environment entry starts with a secret key."""

    candidate = line.strip()
    if candidate.startswith("-"):
        candidate = candidate[1:].strip()
    candidate = candidate.lstrip("'\"")
    for key in _COMPOSE_SECRET_KEYS:
        if candidate.startswith(key):
            suffix = candidate[len(key) : len(key) + 1]
            if not suffix or suffix in {"=", ":", " ", "\t", "'", '"'}:
                return True
    return False


def _extract_rsshub_environment(
    lines: Sequence[str],
    service_index: int,
    service_end: int,
    child_indent: int,
    managed_env_path: str = DEFAULT_MANAGED_ENV_PATH,
) -> Tuple[Dict[str, str], Dict[int, str]]:
    """Extract only secrets in the configured service's environment."""

    managed_reference = _normalise_managed_env_reference(managed_env_path)

    environment_nodes: list[Tuple[int, int, str]] = []
    for index in range(service_index + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and item[1] == child_indent and mapping[1] == "environment":
            environment_nodes.append((index, item[1], mapping[2]))
    if len(environment_nodes) > 1:
        raise SyncError("configured service has duplicate environment mappings")
    if not environment_nodes:
        return {}, {}
    environment_index, environment_indent, value = environment_nodes[0]
    # ``{}`` is the valid Compose representation we write when all entries in
    # the original mapping were migrated.  Treat it as an empty mapping so
    # validation can prove the final document has no inline secrets.
    if value.strip() == "{}":
        return {}, {}
    if value.strip() and not value.lstrip().startswith("#"):
        raise SyncError("configured service environment mapping is not a block")
    environment_end = _compose_node_end(lines, environment_index, service_end, environment_indent)
    code: list[Tuple[int, int, str]] = []
    for index in range(environment_index + 1, environment_end):
        item = _compose_code_line(lines[index])
        if item is not None:
            code.append((index, item[1], item[0]))
    if not code:
        return {}, {}
    entry_indent = min(item[1] for item in code)
    if entry_indent <= environment_indent or any(indent != entry_indent for _, indent, _ in code):
        raise SyncError("configured service environment indentation is ambiguous")
    extracted: Dict[str, str] = {}
    replacements: Dict[int, str] = {}
    for index, _, line in code:
        assignment = _secret_assignment(line)
        if assignment is None:
            if _looks_like_unparsed_secret_assignment(line):
                raise SyncError("configured service secret environment entry is ambiguous")
            if _compose_mapping(line) is None and not line.strip().startswith("-"):
                raise SyncError("configured service environment entry is ambiguous")
            continue
        key, value = assignment
        if key in extracted:
            raise SyncError("configured service environment contains duplicate secret")
        if "$" in value or "{" in value:
            raise SyncError("compose secret uses interpolation; migrate it manually")
        if key in COOKIE_KEYS.values():
            validate_cookie_header(value)
        extracted[key] = value
        replacements[index] = (" " * entry_indent) + "# migrated to " + managed_reference[2:] + "\n"
    return extracted, replacements


def _collapse_migrated_environment(
    lines: list[str],
    service_index: int,
    service_end: int,
    child_indent: int,
    migrated_lines: Mapping[int, str],
) -> bool:
    """Make a secret-only environment block an explicit empty mapping.

    Leaving the original ``environment:`` key with only migration comments
    gives YAML a null value, which Docker Compose rejects for some versions.
    Collapse it to ``environment: {}`` only when every code line in that block
    was migrated; unrelated environment entries must remain untouched.
    """

    environment_nodes: list[Tuple[int, str]] = []
    for index in range(service_index + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and item[1] == child_indent and mapping[1] == "environment":
            environment_nodes.append((index, lines[index]))
    if not environment_nodes:
        return False
    if len(environment_nodes) > 1:
        raise SyncError("configured service has duplicate environment mappings")

    environment_index, environment_line = environment_nodes[0]
    environment_end = _compose_node_end(lines, environment_index, service_end, child_indent)
    code_indices = [
        index
        for index in range(environment_index + 1, environment_end)
        if _compose_code_line(lines[index]) is not None
    ]
    if not code_indices or any(index not in migrated_lines for index in code_indices):
        return False

    line_without_newline = environment_line.rstrip("\r\n")
    mapping = _compose_mapping(line_without_newline)
    if mapping is None:  # pragma: no cover - guarded by the scan above
        raise SyncError("rsshub environment mapping is ambiguous")
    newline = environment_line[len(line_without_newline) :] or "\n"
    inline_comment = mapping[2] if mapping[2].lstrip().startswith("#") else ""
    lines[environment_index] = (
        line_without_newline[: mapping[0]] + "environment: {}" + inline_comment + newline
    )
    return True


def _inspect_env_file(
    lines: Sequence[str],
    service_index: int,
    service_end: int,
    child_indent: int,
    managed_env_path: str = DEFAULT_MANAGED_ENV_PATH,
) -> Dict[str, Any]:
    """Inspect the configured service's managed raw env-file entry."""

    managed_reference = _normalise_managed_env_reference(managed_env_path)

    nodes: list[Tuple[int, str]] = []
    for index in range(service_index + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and item[1] == child_indent and mapping[1] == "env_file":
            nodes.append((index, mapping[2]))
    if len(nodes) > 1:
        raise SyncError("configured service has duplicate env_file mappings")
    if not nodes:
        return {"present": False, "managed": False, "append_at": service_index + 1, "seq_indent": child_indent + 2}
    env_index, value = nodes[0]
    if value.strip() and not value.lstrip().startswith("#"):
        raise SyncError("configured service env_file mapping is not a block")
    env_end = _compose_node_end(lines, env_index, service_end, child_indent)
    code: list[Tuple[int, int, str]] = []
    for index in range(env_index + 1, env_end):
        item = _compose_code_line(lines[index])
        if item is not None:
            code.append((index, item[1], item[0]))
    if not code:
        return {"present": True, "managed": False, "append_at": env_end, "seq_indent": child_indent + 2}
    seq_indent = code[0][1]
    if seq_indent <= child_indent or not code[0][2].strip().startswith("-"):
        raise SyncError("configured service env_file list indentation is ambiguous")
    item_starts: list[Tuple[int, int, str]] = []
    for index, indent, line in code:
        stripped = line.strip()
        if indent < seq_indent:
            raise SyncError("configured service env_file list indentation is inconsistent")
        if indent == seq_indent:
            if not stripped.startswith("-"):
                raise SyncError("configured service env_file contains a non-list entry")
            item_starts.append((index, indent, stripped[1:].strip()))
    if not item_starts:
        raise SyncError("configured service env_file list is invalid")
    target_items = 0
    short_target = False
    for position, (item_index, _, item_value) in enumerate(item_starts):
        next_index = item_starts[position + 1][0] if position + 1 < len(item_starts) else env_end
        fields: Dict[str, str] = {}
        if item_value:
            pseudo = " " * (seq_indent + 2) + item_value
            mapping = _compose_mapping(pseudo)
            if mapping is None:
                if _compose_scalar(item_value) == managed_reference:
                    short_target = True
                continue
            fields[mapping[1]] = _compose_scalar(mapping[2])
        field_indent: Optional[int] = None
        for index in range(item_index + 1, next_index):
            item = _compose_code_line(lines[index])
            if item is None:
                continue
            if item[1] <= seq_indent:
                raise SyncError("configured service env_file item indentation is inconsistent")
            mapping = _compose_mapping(item[0])
            if mapping is None:
                raise SyncError("configured service env_file item is ambiguous")
            if field_indent is None:
                field_indent = item[1]
            elif field_indent != item[1]:
                raise SyncError("configured service env_file field indentation is inconsistent")
            if mapping[1] in fields:
                raise SyncError("configured service env_file item contains duplicate fields")
            fields[mapping[1]] = _compose_scalar(mapping[2])
        if fields.get("path") == managed_reference:
            target_items += 1
            if fields.get("format") != "raw":
                raise SyncError("configured service env_file managed entry is not format raw")
    if short_target:
        raise SyncError("configured service env_file target lacks format raw")
    if target_items > 1:
        raise SyncError("configured service env_file has duplicate managed entries")
    return {"present": True, "managed": target_items == 1, "append_at": env_end, "seq_indent": seq_indent}


def _ensure_raw_env_file(
    lines: list[str],
    service_index: int,
    service_end: int,
    child_indent: int,
    managed_env_path: str = DEFAULT_MANAGED_ENV_PATH,
) -> bool:
    managed_reference = _normalise_managed_env_reference(managed_env_path)
    info = _inspect_env_file(lines, service_index, service_end, child_indent, managed_reference)
    if info["managed"]:
        return False
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    sequence_indent = " " * int(info["seq_indent"])
    child = " " * child_indent
    managed = [
        sequence_indent + "- path: " + managed_reference + newline,
        sequence_indent + "  format: raw" + newline,
    ]
    if not info["present"]:
        lines[service_index + 1 : service_index + 1] = [child + "env_file:" + newline, *managed]
    else:
        lines[int(info["append_at"]) : int(info["append_at"])] = managed
    return True


def _validate_migrated_compose_text(
    text: str,
    service_name: str = DEFAULT_SERVICE,
    managed_env_path: str = DEFAULT_MANAGED_ENV_PATH,
) -> None:
    """Prove Compose text has no inline secrets and raw env wiring."""

    lines = text.splitlines(keepends=True)
    managed_reference = _normalise_managed_env_reference(managed_env_path)
    service_index, service_end, service_indent, _ = _compose_service_block(lines, service_name)
    child_indent = _infer_service_child_indent(lines, service_index, service_end, service_indent)
    extracted, _ = _extract_rsshub_environment(lines, service_index, service_end, child_indent)
    if extracted:
        raise SyncError("configured service still contains inline secrets")
    if not _inspect_env_file(lines, service_index, service_end, child_indent, managed_reference)["managed"]:
        raise SyncError("configured service does not reference managed env file")


def migrate_compose_file(
    compose_file: Path,
    live_env: Path,
    service_name: str = DEFAULT_SERVICE,
    managed_env_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """Migrate secret values out of a conventional RSSHub Compose file.

    This intentionally handles the two Compose forms commonly used by RSSHub
    (environment mapping and ``- KEY=value`` list).  It refuses ambiguous YAML
    rather than guessing, and it does not print any extracted value.
    """

    _validate_compose_service(service_name)
    managed_reference = _managed_env_reference(compose_file, live_env, managed_env_path)
    migration_marker = compose_file.with_name(compose_file.name + ".pre-cookie-sync.txn.json")
    try:
        original = read_limited(compose_file, 2 * 1024 * 1024)
        text = original.decode("utf-8", "strict")
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise SyncError("compose file cannot be read") from exc
    if migration_marker.exists():
        # A previous invocation may have stopped after writing one side of the
        # transaction.  Never trust a marker alone: validate both its shape
        # and the current Compose document before continuing.
        marker = _load_migration_marker(
            compose_file,
            live_env,
            service_name=service_name,
            managed_env_path=managed_reference,
        )
        if marker is None or marker.get("phase") not in {
            "prepared",
            "env_replaced",
            "compose_replaced",
            "finalizing",
        }:
            raise SyncError("migration marker has an invalid phase")
        _validate_migrated_compose_text(text, service_name, managed_reference)
        return {"migrated": [], "compose_changed": False, "migration_pending": True}
    backup = compose_file.with_name(compose_file.name + ".pre-cookie-sync")
    live_backup = live_env.with_name(live_env.name + ".pre-cookie-sync")
    if backup.exists() or live_backup.exists():
        raise SyncError("orphaned migration backup requires manual review")
    lines = text.splitlines(keepends=True)
    service_index, service_end, service_indent, _ = _compose_service_block(lines, service_name)
    child_indent = _infer_service_child_indent(lines, service_index, service_end, service_indent)
    extracted, replacements = _extract_rsshub_environment(
        lines,
        service_index,
        service_end,
        child_indent,
        managed_reference,
    )
    out = list(lines)
    collapsed_environment = bool(replacements) and _collapse_migrated_environment(
        out, service_index, service_end, child_indent, replacements
    )
    for index, replacement in replacements.items():
        out[index] = replacement
    changed = bool(replacements) or collapsed_environment
    existing_data, _ = read_env_file(live_env, missing_ok=True)
    if not extracted:
        # Live values alone are not evidence that RSSHub actually receives
        # them.  Accept an already-manual migration only with managed wiring.
        # A fresh installation has neither provider value nor env file yet;
        # it is still valid and will receive an atomically-created empty raw
        # env below.  This also keeps a finalized empty migration idempotent.
        env_info = _inspect_env_file(out, service_index, service_end, child_indent, managed_reference)
        if env_info["managed"] and live_env.exists():
            _validate_migrated_compose_text(text, service_name, managed_reference)
            return {
                "migrated": [],
                "compose_changed": False,
                "already_migrated": True,
                "migration_pending": False,
            }
        # No inline secrets is not an error: wire an empty (or existing
        # non-provider) env file so Compose can safely mount it in raw mode.
    env_data = render_env(existing_data, extracted) if existing_data else render_env(b"", extracted)
    # Add/append the managed raw env file using the service's real child
    # indentation.  An unrelated existing env_file is not sufficient.
    if _ensure_raw_env_file(out, service_index, service_end, child_indent, managed_reference):
        changed = True
    _validate_migrated_compose_text("".join(out), service_name, managed_reference)

    # Preserve the original compose as a recoverable .pre-cookie-sync file,
    # then atomically replace it only after the secret file is durable.
    if not backup.exists():
        atomic_write(backup, original, mode=0o600)
    live_existed = live_env.exists()
    if live_existed and not live_backup.exists():
        atomic_write(live_backup, existing_data, mode=0o600)
    marker = {
        "version": 1,
        "phase": "prepared",
        "compose_backup": str(backup),
        "live_backup": str(live_backup),
        "live_existed": live_existed,
        "service": service_name,
        "managed_env_path": managed_reference,
        "started_at": now_seconds(),
    }
    atomic_write(
        migration_marker,
        (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        mode=0o600,
    )
    atomic_write(live_env, env_data, mode=0o600)
    marker["phase"] = "env_replaced"
    atomic_write(
        migration_marker,
        (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        mode=0o600,
    )
    if changed:
        atomic_write(compose_file, "".join(out).encode("utf-8"), mode=0o600)
    marker["phase"] = "compose_replaced"
    atomic_write(
        migration_marker,
        (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        mode=0o600,
    )
    return {"migrated": sorted(extracted), "compose_changed": changed, "migration_pending": True}


def _migration_paths(compose_file: Path, live_env: Path) -> Tuple[Path, Path, Path]:
    return (
        compose_file.with_name(compose_file.name + ".pre-cookie-sync"),
        live_env.with_name(live_env.name + ".pre-cookie-sync"),
        compose_file.with_name(compose_file.name + ".pre-cookie-sync.txn.json"),
    )


def _load_migration_marker(
    compose_file: Path,
    live_env: Path,
    service_name: str = DEFAULT_SERVICE,
    managed_env_path: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    _, _, marker_path = _migration_paths(compose_file, live_env)
    try:
        data = read_limited(marker_path, 64 * 1024)
    except FileNotFoundError:
        return None
    try:
        value = json.loads(data.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncError("migration marker is corrupt") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or type(value.get("live_existed")) is not bool:
        raise SyncError("migration marker is invalid")
    _validate_compose_service(service_name)
    expected_reference = (
        _normalise_managed_env_reference(managed_env_path)
        if managed_env_path is not None
        else _managed_env_reference(compose_file, live_env)
    )
    marker_service = value.get("service")
    if marker_service is not None:
        if not isinstance(marker_service, str) or marker_service != service_name:
            raise SyncError("migration marker targets another service")
    marker_reference = value.get("managed_env_path")
    if marker_reference is not None:
        try:
            normalised_marker_reference = _normalise_managed_env_reference(marker_reference)
        except SyncError as exc:
            raise SyncError("migration marker is invalid") from exc
        if normalised_marker_reference != expected_reference:
            raise SyncError("migration marker targets another env file")
    return value


def _remove_migration_files(compose_file: Path, live_env: Path) -> None:
    """Scrub fixed migration backups, then remove their transaction marker."""

    compose_backup, live_backup, marker_path = _migration_paths(compose_file, live_env)
    if compose_backup.exists():
        secure_remove(compose_backup)
    if live_backup.exists():
        secure_remove(live_backup)
    try:
        marker_path.unlink()
        _fsync_directory(marker_path.parent)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SyncError("cannot clean migration transaction") from exc


def finalize_migration(
    compose_file: Path,
    live_env: Path,
    service_name: str = DEFAULT_SERVICE,
    managed_env_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """Delete migration backups only after bootstrap has passed."""

    _validate_compose_service(service_name)
    managed_reference = _managed_env_reference(compose_file, live_env, managed_env_path)
    marker = _load_migration_marker(
        compose_file,
        live_env,
        service_name=service_name,
        managed_env_path=managed_reference,
    )
    if marker is None:
        return {"finalized": False}
    if marker.get("phase") not in ("compose_replaced", "finalizing"):
        raise SyncError("migration is not ready to finalize")
    try:
        current_compose = read_limited(compose_file, 2 * 1024 * 1024).decode("utf-8", "strict")
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise SyncError("compose file cannot be read") from exc
    _validate_migrated_compose_text(current_compose, service_name, managed_reference)
    _, _, marker_path = _migration_paths(compose_file, live_env)
    # Mark finalizing before deleting either backup.  A power loss leaves this
    # marker behind, so the next explicit finalize is idempotent and can keep
    # scrubbing the remaining old-secret files.
    if marker.get("phase") != "finalizing":
        marker["phase"] = "finalizing"
        atomic_write(
            marker_path,
            (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            mode=0o600,
        )
    _remove_migration_files(compose_file, live_env)
    return {"finalized": True}


def rollback_migration(
    compose_file: Path,
    live_env: Path,
    config: Optional[RuntimeConfig] = None,
    runner: Optional[CommandRunner] = None,
    clock: Optional[Clock] = None,
    transport: Optional[HTTPTransport] = None,
    service_name: str = DEFAULT_SERVICE,
    managed_env_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """Restore pre-migration Compose/env and recreate the original service."""

    effective_service = config.service if config is not None else service_name
    _validate_compose_service(effective_service)
    managed_reference = _managed_env_reference(compose_file, live_env, managed_env_path)
    marker = _load_migration_marker(
        compose_file,
        live_env,
        service_name=effective_service,
        managed_env_path=managed_reference,
    )
    if marker is None:
        return {"rolled_back": False}
    compose_backup, live_backup, _ = _migration_paths(compose_file, live_env)
    if marker.get("phase") == "rollback_complete":
        _remove_migration_files(compose_file, live_env)
        return {"finalized": True, "rolled_back": True}
    if not compose_backup.exists():
        raise SyncError("compose migration backup is missing")
    if marker.get("live_existed") and not live_backup.exists():
        raise SyncError("env migration backup is missing")
    atomic_write(compose_file, compose_backup.read_bytes(), mode=0o600)
    if marker.get("live_existed"):
        secure_copy(live_backup, live_env, mode=0o600)
    else:
        secure_remove(live_env)
    config = config or RuntimeConfig(
        compose_file=compose_file,
        live_env=live_env,
        service=effective_service,
    )
    docker = DockerCompose(config, runner=runner, clock=clock)
    if not docker.config_quiet() or not docker.recreate() or not docker.wait_healthy():
        raise SyncError("migration rollback recreate failed")
    service = SyncService(config, transport=transport, runner=runner, clock=clock)
    if not service._health_probe().ok:
        raise SyncError("migration rollback health check failed")
    marker["phase"] = "rollback_complete"
    _, _, marker_path = _migration_paths(compose_file, live_env)
    atomic_write(
        marker_path,
        (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        mode=0o600,
    )
    _remove_migration_files(compose_file, live_env)
    return {"finalized": True, "rolled_back": True}


def _parse_bark_input(value: str) -> str:
    value = value.strip()
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InvalidInput("Bark key is invalid")
    if value.startswith("https://"):
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or parts.hostname != "api.day.app"
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise InvalidInput("Bark URL is invalid")
        segments = [segment for segment in parts.path.split("/") if segment]
        if len(segments) != 1:
            raise InvalidInput("Bark URL is invalid")
        value = segments[0]
    if not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", value):
        raise InvalidInput("Bark key is invalid")
    return value


def configure_bark_from_stdin(stream: Any, config_path: Path) -> Dict[str, Any]:
    """Set Bark's key from stdin without exposing it through argv/env/logs."""

    try:
        # One line is enough and lets a non-TTY SSH pipe finish without
        # waiting for EOF.  The value is never echoed or placed in argv/env.
        raw = stream.readline(4097)
    except Exception as exc:
        raise InvalidInput("cannot read Bark key") from exc
    if len(raw) > 4096:
        raise InvalidInput("Bark key is too large")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise InvalidInput("Bark key is invalid") from exc
    key = _parse_bark_input(text)
    ensure_directory(config_path.parent, mode=0o700)
    data: Dict[str, Any] = {}
    if config_path.exists():
        # Enforce the same owner/mode/symlink boundary as runtime reads before
        # accepting and rewriting a file that may already contain a Bark key.
        RuntimeConfig.from_file(config_path)
        try:
            loaded = json.loads(read_limited(config_path, MAX_INPUT_BYTES).decode("utf-8", "strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncError("configuration is invalid") from exc
        if not isinstance(loaded, dict):
            raise SyncError("configuration is invalid")
        data = loaded
    bark = data.get("bark") if isinstance(data.get("bark"), dict) else {}
    bark["base_url"] = "https://api.day.app"
    bark["device_key"] = key
    bark.pop("url", None)
    bark.pop("push_url", None)
    data["bark"] = bark
    atomic_write(
        config_path,
        (json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        mode=0o600,
    )
    return {"configured": True}


def configure_deployment(
    config_path: Path,
    *,
    compose_file: Path,
    live_env: Path,
    candidate_dir: Path,
    state_file: Path,
    lock_file: Path,
    project: str,
    service: str,
    rsshub_base_url: str,
    replace: bool = False,
) -> Dict[str, Any]:
    """Atomically persist one validated deployment without exposing secrets.

    Existing non-deployment settings (including the optional Bark key and an
    RSSHub access key) are preserved.  Changing an already configured target
    requires an explicit ``replace`` flag so a routine upgrade cannot silently
    start operating on another Compose project.
    """

    config_path = Path(config_path)
    proposed = {
        "compose_file": os.fspath(Path(compose_file)),
        "live_env": os.fspath(Path(live_env)),
        "candidate_dir": os.fspath(Path(candidate_dir)),
        "state_file": os.fspath(Path(state_file)),
        "lock_file": os.fspath(Path(lock_file)),
        "project": project,
        "service": service,
    }
    # Reuse the runtime validator before any write.  This checks absolute
    # paths, Compose names, the env-file containment rule, and the loopback
    # RSSHub endpoint restriction.
    validated = RuntimeConfig.from_file(
        config_path,
        compose_file=Path(compose_file),
        live_env=Path(live_env),
        candidate_dir=Path(candidate_dir),
        state_file=Path(state_file),
        lock_file=Path(lock_file),
        project=project,
        service=service,
        rsshub_base_url=rsshub_base_url,
    )
    validated.validate()

    data: Dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(read_limited(config_path, MAX_INPUT_BYTES).decode("utf-8", "strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncError("configuration is invalid") from exc
        if not isinstance(loaded, dict):
            raise SyncError("configuration is invalid")
        data = loaded

    existing = data.get("deployment")
    if existing is not None and not isinstance(existing, dict):
        raise SyncError("configuration is invalid")
    existing_mapping: Dict[str, Any] = dict(existing or {})
    differs = any(
        key in existing_mapping and existing_mapping.get(key) != value
        for key, value in proposed.items()
    )
    existing_rsshub = data.get("rsshub")
    if existing_rsshub is not None and not isinstance(existing_rsshub, dict):
        raise SyncError("configuration is invalid")
    # Once a deployment has been saved, its loopback probe endpoint is part of
    # the target identity as well.  Requiring the same explicit replacement
    # flag prevents a typo during a routine upgrade from silently monitoring a
    # different local RSSHub instance.
    if existing_mapping and isinstance(existing_rsshub, dict):
        saved_base_url = existing_rsshub.get("base_url")
        if saved_base_url is not None and saved_base_url != rsshub_base_url:
            differs = True
    if differs and not replace:
        raise SyncError("deployment target differs; explicit replacement is required")

    existing_mapping.update(proposed)
    data["deployment"] = existing_mapping
    rsshub_mapping: Dict[str, Any] = dict(existing_rsshub or {})
    rsshub_mapping["base_url"] = rsshub_base_url
    rsshub_mapping.setdefault("health_path", "/healthz")
    rsshub_mapping.setdefault("access_key", None)
    data["rsshub"] = rsshub_mapping

    encoded = (
        json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    changed = True
    if config_path.exists():
        try:
            changed = read_limited(config_path, MAX_INPUT_BYTES) != encoded
        except OSError as exc:
            raise SyncError("configuration is invalid") from exc
    if changed:
        atomic_write(config_path, encoded, mode=0o600)
    else:
        try:
            os.chmod(config_path, 0o600)
        except OSError as exc:
            raise SyncError("cannot secure configuration") from exc
    return {"configured": True, "changed": changed}


def install_cli(args: argparse.Namespace) -> int:
    # Migration changes both the Compose file and live secret env.  Serialize
    # it with apply/monitor so the standalone CLI cannot race either one.
    config = make_config(args)
    config.validate()
    managed_reference = _managed_env_reference(config.compose_file, config.live_env)
    with file_lock(config.lock_file):
        result = migrate_compose_file(
            config.compose_file,
            config.live_env,
            service_name=config.service,
            managed_env_path=managed_reference,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    else:
        print("migrated: " + ",".join(result["migrated"]))
    return 0


def make_config(args: argparse.Namespace) -> RuntimeConfig:
    overrides: Dict[str, Any] = {}
    for field in ("compose_file", "live_env", "candidate_dir", "state_file", "lock_file"):
        value = getattr(args, field, None)
        if value is not None:
            overrides[field] = Path(value)
    for field in ("project", "service"):
        value = getattr(args, field, None)
        if value is not None:
            overrides[field] = value
    return RuntimeConfig.from_file(
        Path(args.config),
        require_file=True,
        require_deployment=True,
        **overrides,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RSSHub Cookie 自动同步与失效自愈")
    parser.add_argument(
        "command",
        choices=(
            "apply",
            "manual-update",
            "monitor",
            "status",
            "notify-test",
            "migrate-compose",
            "bootstrap",
            "finalize-migration",
            "rollback-migration",
            "configure-bark",
            "configure-deployment",
        ),
    )
    parser.add_argument("--config", default=os.environ.get("RSSHUB_COOKIE_SYNC_CONFIG", str(DEFAULT_CONFIG_FILE)))
    # Deployment values come from config.json.  Keep these options unset by
    # default so a config file is not silently overridden by machine-specific
    # defaults; an explicit CLI option remains a deliberate one-shot override.
    parser.add_argument("--compose-file", default=None)
    parser.add_argument("--live-env", default=None)
    parser.add_argument("--candidate-dir", default=None)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--lock-file", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--service", default=None)
    parser.add_argument("--rsshub-base-url", default=None)
    parser.add_argument("--replace-deployment", action="store_true")
    parser.add_argument("--provider", choices=PROVIDERS, help="manual-update 的目标服务")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出安全状态")
    return parser


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "migrate-compose":
            return install_cli(args)
        if args.command == "configure-bark":
            if stdin is None and sys.stdin.isatty():
                # Interactive terminals echo normal stdin.  getpass keeps the
                # Device Key out of scrollback while the non-interactive path
                # remains pipe-friendly for controlled provisioning systems.
                value = getpass.getpass("Bark Device Key 或完整 URL（不会回显）：")
                bark_stream: Any = io.BytesIO((value + "\n").encode("utf-8"))
            else:
                bark_stream = stdin or sys.stdin.buffer
            result = configure_bark_from_stdin(bark_stream, Path(args.config))
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if args.command == "configure-deployment":
            required = {
                "compose_file": args.compose_file,
                "live_env": args.live_env,
                "candidate_dir": args.candidate_dir,
                "state_file": args.state_file,
                "lock_file": args.lock_file,
                "project": args.project,
                "service": args.service,
                "rsshub_base_url": args.rsshub_base_url,
            }
            if any(value is None for value in required.values()):
                raise InvalidInput("configure-deployment requires all deployment options")
            result = configure_deployment(
                Path(args.config),
                compose_file=Path(args.compose_file),
                live_env=Path(args.live_env),
                candidate_dir=Path(args.candidate_dir),
                state_file=Path(args.state_file),
                lock_file=Path(args.lock_file),
                project=args.project,
                service=args.service,
                rsshub_base_url=args.rsshub_base_url,
                replace=args.replace_deployment,
            )
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        config = make_config(args)
        service = SyncService(config)
        if args.command == "manual-update":
            if args.provider is None:
                raise InvalidInput("manual-update requires --provider")
            if stdin is not None or not sys.stdin.isatty():
                raise InvalidInput("manual-update requires an interactive terminal")
            cookie_header = getpass.getpass(
                f"{args.provider} Cookie（粘贴后按回车，内容不会回显）："
            )
            result = service.apply(build_manual_update_request(args.provider, cookie_header))
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            if result.get("status") == "retryable_error":
                return 75
            return 0
        if args.command == "apply":
            payload = strict_json_from_stdin(stdin)
            result = service.apply(payload)
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            if result.get("status") == "retryable_error":
                return 75
            return 0
        if args.command == "monitor":
            result = service.monitor()
            if args.json:
                print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if args.command == "bootstrap":
            result = service.bootstrap()
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if args.command == "finalize-migration":
            with file_lock(config.lock_file):
                result = finalize_migration(
                    config.compose_file,
                    config.live_env,
                    service_name=config.service,
                )
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if args.command == "rollback-migration":
            with file_lock(config.lock_file):
                result = rollback_migration(
                    config.compose_file,
                    config.live_env,
                    config=config,
                    service_name=config.service,
                )
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if args.command == "status":
            result = service.status()
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
        if args.command == "notify-test":
            if not service.notify_test():
                print("notification failed", file=sys.stderr)
                return 1
            print("notification sent")
            return 0
        parser.error("unknown command")
    except (InvalidInput, SyncError) as exc:
        # Messages are fixed categories from this module; never forward
        # provider bodies, subprocess stderr, or request headers.
        LOG.error("%s", str(exc))
        return 1
    except Exception:
        LOG.error("unexpected operational error")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
