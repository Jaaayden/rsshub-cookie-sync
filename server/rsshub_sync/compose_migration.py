"""compose migration (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .clock import Clock, now_seconds
from .compose_text import _collapse_migrated_environment, _compose_service_block, _extract_rsshub_environment, _infer_service_child_indent, _inspect_env_file, _validate_migrated_compose_text
from .config import RuntimeConfig, _managed_env_reference, _normalise_managed_env_reference, _validate_compose_service
from .constants import DEFAULT_MANAGED_ENV_PATH, DEFAULT_SERVICE
from .docker import CommandRunner, DockerCompose
from .envfile import read_env_file, render_env
from .errors import SyncError
from .fsutil import _fsync_directory, atomic_write, read_limited, secure_copy, secure_remove
from .service import SyncService
from .transport import HTTPTransport


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
