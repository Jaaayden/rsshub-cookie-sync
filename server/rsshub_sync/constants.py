"""constants (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Mapping, Tuple


LOG = logging.getLogger("rsshub-cookie-sync")

PROVIDERS: Tuple[str, ...] = ("zhihu", "weibo", "twitter")
COOKIE_KEYS: Mapping[str, str] = {
    "zhihu": "ZHIHU_COOKIES",
    "weibo": "WEIBO_COOKIES",
    "twitter": "TWITTER_AUTH_TOKEN",
}
MAX_INPUT_BYTES = 512 * 1024
MAX_COOKIE_BYTES = 256 * 1024
MAX_HTTP_BODY_BYTES = 128 * 1024
MAX_FEED_BYTES = 2 * 1024 * 1024
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
    "twitter_csrf_missing",
    "twitter_invalid_response",
    "twitter_auth_failed",
    "twitter_not_configured",
    "twitter_token_pool_unsupported",
    "direct_sync",
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
