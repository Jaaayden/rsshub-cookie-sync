"""service (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional, Tuple

from .clock import Clock
from .compose_text import _validate_migrated_compose_text
from .config import RuntimeConfig, _managed_env_reference
from .constants import ALLOWED_BOOTSTRAP_STATES, COOKIE_KEYS, MAX_COOKIE_BYTES, MAX_FEED_BYTES, MAX_INPUT_BYTES, PROVIDERS
from .docker import CommandRunner, DockerCompose
from .envfile import parse_env, read_env_file, render_env
from .errors import InvalidInput, ProbeError, SyncError, TransactionError
from .fsutil import atomic_write, ensure_directory, file_lock, read_limited, secure_copy, secure_remove
from .notify import BarkNotifier, _append_query
from .probes import ProbeResult, ProviderProber
from .state import _safe_error_code, load_state, save_state
from .transport import HTTPTransport
from .validation import sha256_prefix, twitter_token, validate_cookie_header, validate_provider, validate_provider_value


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
            validate_provider_value(provider, value)
        except (UnicodeDecodeError, InvalidInput, SyncError):
            return None
        return value

    @staticmethod
    def _mark_direct(item: MutableMapping[str, Any]) -> None:
        # Transport success is not evidence of upstream authentication.
        item.update(last_probe="unknown", last_probe_at=None, last_full_probe_at=None,
                    last_success_at=None, auth_failures=0, transient_failures=0,
                    last_error=None)

    def _save_candidate(self, provider: str, cookie: str) -> None:
        validate_provider_value(provider, cookie)
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
        """Track whether at least one provider is configured; providers are independent."""

        present = [bool(live_values.get(COOKIE_KEYS[provider], "")) for provider in PROVIDERS]
        status = "seeded" if any(present) else "unseeded"
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
        item.get("last_probe")
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
            if not self.docker.config_quiet() or not self.docker.recreate() or not self.docker.wait_healthy():
                raise TransactionError("transaction recovery recreate failed")
            health = self._health_probe()
            if not health.ok:
                raise TransactionError("transaction recovery health check failed")
        self._remove_transaction_files()

    def _promote(self, updates: Mapping[str, str], state: MutableMapping[str, Any], reason: str) -> None:
        if not updates:
            return
        for provider, cookie in updates.items():
            validate_provider(provider)
            validate_provider_value(provider, cookie)
        old_data, old_values = self._read_live()
        if "twitter" in updates and "," in old_values.get("TWITTER_AUTH_TOKEN", ""):
            raise InvalidInput("Twitter token pools cannot be automatically managed")
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
                if self.config.sync_mode == "direct":
                    continue
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
                if self.config.sync_mode == "direct":
                    self._mark_direct(item)
                self._safe_notification(
                    state,
                    "promoted:" + provider,
                    "RSSHub Cookie 已自动更新",
                    (provider + " 凭证已直接同步，RSSHub 健康检查通过；未验证上游登录态。")
                    if self.config.sync_mode == "direct" else provider + " 登录态已验证并完成切换。",
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
        if not isinstance(payload, dict) or set(payload) not in ({"version", "providers"}, {"version", "providers", "diagnostics"}) or ("diagnostics" in payload and payload["diagnostics"] is not True):
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
                cookie_values[provider] = (twitter_token(item["cookieHeader"], header=True)
                                           if provider == "twitter" else validate_cookie_header(item["cookieHeader"]))
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
            reasons: Dict[str, str] = {}
            to_promote: Dict[str, str] = {}
            for provider in invalid_providers:
                results[provider] = "rejected_invalid"
            for provider, cookie in cookie_values.items():
                if provider == "twitter" and "," in live_values.get("TWITTER_AUTH_TOKEN", ""):
                    state["providers"][provider]["last_error"] = "twitter_token_pool_unsupported"
                    reasons[provider] = "twitter_token_pool_unsupported"
                    results[provider] = "rejected_invalid"
                    continue
                if self.config.sync_mode == "direct":
                    current = live_values.get(COOKIE_KEYS[provider], "")
                    if current == cookie:
                        try:
                            self._remove_candidate(provider)
                        except SyncError:
                            results[provider] = "retryable_error"
                            reasons[provider] = "server_error"
                            continue
                        item = state["providers"][provider]
                        self._clear_candidate_metadata(item)
                        self._mark_direct(item)
                        item["live_hash"] = sha256_prefix(cookie)
                        results[provider] = "unchanged"
                    else:
                        to_promote[provider] = cookie
                    continue
                result = self.prober.probe(provider, cookie, full=True)
                reasons[provider] = _safe_error_code(result.reason) or "upstream_temporary_failure"
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
                    response = {"status": status}
                    if payload.get("diagnostics") and status in ("retryable_error", "rejected_invalid"):
                        failed = next(p for p, value in results.items() if value == status)
                        response["reason"] = reasons.get(failed, "candidate_invalid" if status == "rejected_invalid" else "server_error")
                    if payload.get("diagnostics") and self.config.sync_mode == "direct" and status in ("promoted", "unchanged"):
                        response["reason"] = "direct_sync"
                    return response
            return {"status": "retryable_error"}

    def _maybe_full_probe(self, provider: str, cookie: str, item: Mapping[str, Any]) -> Tuple[ProbeResult, bool]:
        current = self.clock.now()
        last_full = item.get("last_full_probe_at")
        full = not isinstance(last_full, (int, float)) or current - int(last_full) >= self.config.moments_interval
        return self.prober.probe(provider, cookie, full=full), full

    def _monitor_route(self, state: Dict[str, Any], provider: str) -> None:
        item = state["providers"][provider]
        route = self.config.rsshub_routes[provider]
        url = _append_query(self.config.rsshub_base_url.rstrip("/") + route, self.config.rsshub_access_key)
        error = None
        try:
            response = self.transport.request(url, timeout=self.config.provider_timeout,
                                              max_body_bytes=MAX_FEED_BYTES)
            if response.status != 200:
                error = "route_http_" + str(response.status)
            elif len(response.body) > MAX_FEED_BYTES:
                error = "route_too_large"
            else:
                # Reject declarations, including UTF-16/32 encodings, before parsing.
                scan = response.body.replace(b"\x00", b"").upper()
                if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
                    error = "route_invalid_feed"
                else:
                    root = ET.fromstring(response.body)
                    if not ((root.tag == "rss" and root.find("channel") is not None)
                            or root.tag == "{http://www.w3.org/2005/Atom}feed"):
                        error = "route_invalid_feed"
        except (ET.ParseError, ValueError, LookupError):
            error = "route_invalid_feed"
        except (ProbeError, OSError, TimeoutError):
            error = "route_network_error"
        previous_failures = item["route_failures"]
        item["route_probe_at"] = self.clock.now()
        item["route_probe"] = "transient" if error else "ok"
        item["route_error"] = error
        item["route_failures"] = previous_failures + 1 if error else 0
        if error and item["route_failures"] >= 2:
            self._safe_notification(state, "route_failed:" + provider, "RSSHub 订阅路由异常",
                                    provider + " 订阅路由连续检查失败（" + error + "），请检查抓取情况；凭证保持不变。")
        elif not error and previous_failures >= 2:
            self._safe_notification(state, "route_recovered:" + provider, "RSSHub 订阅路由已恢复",
                                    provider + " 已恢复返回有效订阅；结果可能来自 RSSHub 缓存。")

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
                if self.config.sync_mode == "direct":
                    self._mark_direct(item)
                    item["live_hash"] = sha256_prefix(cookie) if cookie else None
                    if provider == "twitter" and "," in cookie:
                        item["last_error"] = "twitter_token_pool_unsupported"
                    if cookie and provider in self.config.rsshub_routes and health.ok:
                        self._monitor_route(state, provider)
                    elif not cookie or provider not in self.config.rsshub_routes:
                        item.update(route_probe="unknown", route_probe_at=None, route_failures=0, route_error=None)
                    continue
                if provider == "twitter" and (not cookie or "," in cookie):
                    item["last_probe"] = "unknown"
                    item["last_error"] = "twitter_token_pool_unsupported" if cookie else "twitter_not_configured"
                    item["auth_failures"] = 0
                    item["transient_failures"] = 0
                    continue
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
        checks the process health endpoint and each configured provider.
        Existing Twitter token pools are preserved but not managed. A fresh
        installation has no provider Cookies yet; it is marked ``unseeded``
        after the Compose/RSSHub checks and can receive its first valid Cookie
        through ``apply``. Providers can be configured independently.

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
                self._set_bootstrap_status(state, values)
            else:
                for provider, cookie in cookies.items():
                    if not cookie:
                        continue
                    if provider == "twitter" and "," in cookie:
                        state["providers"][provider]["last_error"] = "twitter_token_pool_unsupported"
                        continue
                    if self.config.sync_mode == "direct":
                        item = state["providers"][provider]
                        self._mark_direct(item)
                        item["live_hash"] = sha256_prefix(cookie)
                        continue
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
                self._set_bootstrap_status(state, values)
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
                    "route_probe",
                    "route_probe_at",
                    "route_failures",
                    "route_error",
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
            "sync_mode": self.config.sync_mode,
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
