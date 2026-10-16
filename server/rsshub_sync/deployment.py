"""deployment (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

from .config import RuntimeConfig
from .constants import MAX_INPUT_BYTES
from .docker import CommandRunner, DockerCompose
from .errors import SyncError
from .fsutil import atomic_write, file_lock, read_limited
from .service import SyncService
from .transport import HTTPTransport


def discover_rsshub_access_key(
    config: RuntimeConfig, runner: Optional[CommandRunner] = None,
) -> Optional[str]:
    """Read the selected service's effective Compose environment in memory.

    Compose resolves environment/env_file precedence and .env interpolation.
    Reading a previous container instead would miss changes that the next
    Cookie transaction applies when it recreates the service.
    """
    config.validate()
    docker = DockerCompose(config, runner=runner)
    limit = 2 * 1024 * 1024
    try:
        result = docker.runner.run(
            docker._compose() + ["config", "--format", "json"],
            timeout=30.0, capture=True, output_limit=limit,
        )
    except Exception:
        raise SyncError("cannot read RSSHub Compose configuration") from None
    if result.returncode != 0:
        raise SyncError("cannot read RSSHub Compose configuration")
    if len(result.stdout) >= limit:
        raise SyncError("RSSHub Compose configuration is too large")
    try:
        value = json.loads(result.stdout)
        service = value["services"][config.service]
        if not isinstance(service, dict):
            raise ValueError
        environment = service.get("environment", {})
        if not isinstance(environment, dict):
            raise ValueError
        key = environment.get("ACCESS_KEY")
        if key is not None and (
            not isinstance(key, str)
            or any(ord(char) < 32 or ord(char) == 127 for char in key)
        ):
            raise ValueError
    except (TypeError, KeyError, ValueError):
        # Do not propagate a JSON error's source text or Docker stderr: both
        # can contain credentials unrelated to this command as well.
        raise SyncError("RSSHub Compose access key configuration is invalid") from None
    return key or None


def refresh_rsshub_config(
    config_path: Path,
    *,
    base_url: Optional[str] = None,
    dry_run: bool = False,
    runner: Optional[CommandRunner] = None,
    transport: Optional[HTTPTransport] = None,
) -> Dict[str, Any]:
    """Validate current RSSHub settings before atomically refreshing config.

    This command never recreates containers or recovers Cookie transactions.
    If Compose changes have not been applied yet, the health check fails and
    the previous synchronizer configuration is retained.
    """
    config_path = Path(config_path)
    config = RuntimeConfig.from_file(config_path, require_file=True, require_deployment=True)
    config.validate()
    with file_lock(config.lock_file):
        config = RuntimeConfig.from_file(config_path, require_file=True, require_deployment=True)
        config.validate()
        original = read_limited(config_path, MAX_INPUT_BYTES)
        data = json.loads(original.decode("utf-8", "strict"))
        key = discover_rsshub_access_key(config, runner=runner)
        proposed = replace(
            config, rsshub_access_key=key,
            rsshub_base_url=base_url if base_url is not None else config.rsshub_base_url,
        )
        proposed.validate()
        health = SyncService(proposed, transport=transport)._health_probe()
        if not health.ok:
            status = str(health.status) if health.status is not None else "network error"
            raise SyncError(
                "RSSHub health check failed (" + status
                + "); apply RSSHub configuration before refreshing"
            )
        rsshub = data.setdefault("rsshub", {})
        changed = (
            rsshub.get("access_key") != key
            or rsshub.get("base_url") != proposed.rsshub_base_url
        )
        rsshub["access_key"] = key
        rsshub["base_url"] = proposed.rsshub_base_url
        if changed and not dry_run:
            atomic_write(
                config_path,
                (json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
                mode=0o600,
            )
        return {
            "configured": True, "changed": changed,
            "access_key_configured": bool(key), "health_status": health.status,
            "dry_run": dry_run,
        }


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
    detect_access_key: bool = False,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Atomically persist one validated deployment without exposing secrets.

    Existing non-deployment settings are preserved.  Installers explicitly
    discover the RSSHub access key from the selected Compose deployment.
    Changing an already configured target
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
    if detect_access_key:
        rsshub_mapping["access_key"] = discover_rsshub_access_key(validated, runner=runner)
    else:
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
