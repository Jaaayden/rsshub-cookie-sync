#!/usr/bin/env python3
"""RSSHub cookie synchronisation service.

This module intentionally uses only Python's standard library.  It is used on
the RSSHub host by a short-lived ``apply`` command and a systemd timer running
``monitor``.  Cookie values are kept in root-readable files only; state and
logs contain hashes and fixed error categories, never the values themselves.

The module is also deliberately dependency-injection friendly.  Tests (and a
future deployment wrapper) can provide an HTTP transport, command runner,
clock, and notifier without changing the state machine.

The implementation lives in the ``rsshub_sync`` package next to this file.
This script is the stable entry point used by the wrappers, the systemd unit
and the forced SSH command, and it re-exports the package API under its
historical module name.
"""

from __future__ import annotations

import getpass  # noqa: F401  (patched by tests through this module)
import subprocess  # noqa: F401
import sys  # noqa: F401

from rsshub_sync.constants import (  # noqa: F401
    ALLOWED_BOOTSTRAP_STATES,
    ALLOWED_CANDIDATE_STATES,
    ALLOWED_ERROR_CODES,
    ALLOWED_PROBE_STATES,
    ALLOWED_RSSHUB_HOSTS,
    ALLOWED_WEIBO_HOST,
    ALLOWED_ZHIHU_HOST,
    COMPOSE_PROJECT_RE,
    COMPOSE_SERVICE_RE,
    CONTAINER_ID_RE,
    COOKIE_KEYS,
    COOKIE_NAME_RE,
    DEFAULT_CANDIDATE_DIR,
    DEFAULT_COMPOSE_FILE,
    DEFAULT_CONFIG_FILE,
    DEFAULT_LIVE_ENV,
    DEFAULT_LOCK_FILE,
    DEFAULT_MANAGED_ENV_PATH,
    DEFAULT_PROJECT,
    DEFAULT_SERVICE,
    DEFAULT_STATE_FILE,
    DEFAULT_UNCONFIGURED_DIR,
    HASH_CHARS,
    KEY_RE,
    LOG,
    MAX_COOKIE_BYTES,
    MAX_FEED_BYTES,
    MAX_HTTP_BODY_BYTES,
    MAX_INPUT_BYTES,
    PROVIDERS,
)
from rsshub_sync.errors import (  # noqa: F401
    InvalidInput,
    ProbeError,
    SyncError,
    TransactionError,
)
from rsshub_sync.clock import (  # noqa: F401
    Clock,
    now_seconds,
)
from rsshub_sync.fsutil import (  # noqa: F401
    atomic_write,
    ensure_directory,
    file_lock,
    read_limited,
    secure_copy,
    secure_remove,
)
from rsshub_sync.validation import (  # noqa: F401
    build_manual_update_request,
    sha256_prefix,
    strict_json_from_stdin,
    twitter_token,
    validate_cookie_header,
    validate_cookie_shape,
    validate_env_secret,
    validate_provider,
    validate_provider_value,
)
from rsshub_sync.envfile import (  # noqa: F401
    parse_env,
    read_env_file,
    render_env,
)
from rsshub_sync.state import (  # noqa: F401
    _merge_state,
    default_state,
    load_state,
    save_state,
)
from rsshub_sync.config import (  # noqa: F401
    RuntimeConfig,
)
from rsshub_sync.transport import (  # noqa: F401
    HTTPResponse,
    HTTPTransport,
)
from rsshub_sync.probes import (  # noqa: F401
    ProbeResult,
    ProviderProber,
)
from rsshub_sync.docker import (  # noqa: F401
    CommandResult,
    CommandRunner,
    DockerCompose,
)
from rsshub_sync.notify import (  # noqa: F401
    BarkNotifier,
)
from rsshub_sync.service import (  # noqa: F401
    SyncService,
)
from rsshub_sync.compose_migration import (  # noqa: F401
    finalize_migration,
    migrate_compose_file,
    rollback_migration,
)
from rsshub_sync.deployment import (  # noqa: F401
    configure_deployment,
    discover_rsshub_access_key,
    refresh_rsshub_config,
)
from rsshub_sync.cli import (  # noqa: F401
    build_parser,
    configure_bark_from_stdin,
    install_cli,
    main,
    make_config,
)


if __name__ == "__main__":
    raise SystemExit(main())
