"""state (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .constants import ALLOWED_BOOTSTRAP_STATES, ALLOWED_CANDIDATE_STATES, ALLOWED_ERROR_CODES, ALLOWED_PROBE_STATES, MAX_INPUT_BYTES, PROVIDERS
from .errors import SyncError
from .fsutil import atomic_write, read_limited


def _default_provider_state() -> Dict[str, Any]:
    return {
        "route_probe": "unknown",
        "route_probe_at": None,
        "route_failures": 0,
        "route_error": None,
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
    if isinstance(value, str) and re.fullmatch(r"route_(http_[0-9]{3}|network_error|invalid_feed|too_large|ok)", value):
        return value
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
            if item.get("route_probe") in ALLOWED_PROBE_STATES:
                target["route_probe"] = item["route_probe"]
            target["route_error"] = _safe_error_code(item.get("route_error"))
            if item.get("candidate_validation") in ALLOWED_CANDIDATE_STATES:
                target["candidate_validation"] = item.get("candidate_validation")
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
                "route_probe_at",
            ):
                if key in item and (item[key] is None or type(item[key]) is int):
                    target[key] = item[key]
            for key in ("live_hash", "candidate_hash"):
                if key in item and (item[key] is None or (isinstance(item[key], str) and re.fullmatch(r"[0-9a-f]{16}", item[key]))):
                    target[key] = item[key]
            for key in ("auth_failures", "transient_failures", "route_failures"):
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
