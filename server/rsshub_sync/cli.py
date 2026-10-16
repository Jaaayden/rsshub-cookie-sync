"""cli (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import argparse
import getpass
import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from urllib.parse import urlsplit

from .compose_migration import finalize_migration, migrate_compose_file, rollback_migration
from .config import RuntimeConfig, _managed_env_reference
from .constants import DEFAULT_CONFIG_FILE, LOG, MAX_INPUT_BYTES, PROVIDERS
from .deployment import configure_deployment, refresh_rsshub_config
from .errors import InvalidInput, SyncError
from .fsutil import atomic_write, ensure_directory, file_lock, read_limited
from .service import SyncService
from .validation import build_manual_update_request, strict_json_from_stdin


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
            "refresh-rsshub-config",
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
    parser.add_argument("--detect-rsshub-access-key", action="store_true",
                        help="安装时从 Compose 自动读取 RSSHub 访问密钥")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览 refresh-rsshub-config，不保存配置")
    parser.add_argument("--provider", choices=PROVIDERS, help="manual-update 的目标服务")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出安全状态")
    return parser


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.dry_run and args.command != "refresh-rsshub-config":
            raise InvalidInput("--dry-run is only supported by refresh-rsshub-config")
        if args.detect_rsshub_access_key and args.command != "configure-deployment":
            raise InvalidInput("--detect-rsshub-access-key is only supported by configure-deployment")
        if args.command == "refresh-rsshub-config":
            result = refresh_rsshub_config(
                Path(args.config), base_url=args.rsshub_base_url, dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
            return 0
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
                detect_access_key=args.detect_rsshub_access_key,
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
                f"{args.provider} {'Auth Token' if args.provider == 'twitter' else 'Cookie'}"
                "（粘贴后按回车，内容不会回显）："
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
