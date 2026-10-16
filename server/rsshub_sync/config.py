"""config (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlsplit

from .constants import ALLOWED_RSSHUB_HOSTS, ALLOWED_WEIBO_HOST, ALLOWED_ZHIHU_HOST, COMPOSE_PROJECT_RE, COMPOSE_SERVICE_RE, DEFAULT_CANDIDATE_DIR, DEFAULT_COMPOSE_FILE, DEFAULT_CONFIG_FILE, DEFAULT_LIVE_ENV, DEFAULT_LOCK_FILE, DEFAULT_PROJECT, DEFAULT_SERVICE, DEFAULT_STATE_FILE, MAX_INPUT_BYTES, PROVIDERS
from .errors import InvalidInput, SyncError
from .fsutil import read_limited


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
    sync_mode: str = "direct"
    rsshub_routes: Dict[str, str] = field(default_factory=dict)
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
                    "sync_mode": data.get("sync_mode", cls.sync_mode),
                    "rsshub_routes": {name: providers[name]["rsshub_route"] for name in PROVIDERS if providers.get(name, {}).get("rsshub_route") is not None},
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
        if provider == "twitter":
            return "https://api.x.com/1.1/account/settings.json"
        raise InvalidInput("unknown provider")

    def validate(self) -> None:
        if self.sync_mode not in ("direct", "verified"):
            raise SyncError("sync_mode must be direct or verified")
        if not isinstance(self.rsshub_routes, dict):
            raise SyncError("invalid RSSHub routes")
        for provider, route in self.rsshub_routes.items():
            if (provider not in PROVIDERS or not isinstance(route, str)
                    or not route.startswith("/" + provider + "/")
                    or len(route) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in route)
                    or "\\" in route or urlsplit(route).query or urlsplit(route).fragment
                    or any(part in (".", "..") for part in route.split("/"))):
                raise SyncError("RSSHub route must be a provider path without query or fragment")
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
