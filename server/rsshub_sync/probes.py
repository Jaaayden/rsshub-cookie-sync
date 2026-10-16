"""probes (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Any, Dict, Optional, Tuple

from .config import RuntimeConfig
from .constants import MAX_HTTP_BODY_BYTES
from .errors import InvalidInput, ProbeError
from .transport import HTTPResponse, HTTPTransport
from .validation import twitter_token, validate_cookie_header, validate_provider


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

    def probe_twitter(self, token: str) -> ProbeResult:
        try:
            token = twitter_token(token)
        except InvalidInput:
            return ProbeResult("auth_failed", None, "invalid_cookie")
        response, result = self._get("twitter", "https://x.com/", "auth_token=" + token)
        if response is None or result.kind != "ok":
            return result
        csrf_values = set()
        for raw in response.set_cookies:
            if len(raw) > 16384:
                continue
            try:
                cookies = SimpleCookie()
                cookies.load(raw)
                morsel = cookies.get("ct0")
                if morsel and morsel["domain"].lstrip(".").lower() in ("", "x.com") and morsel["path"] in ("", "/"):
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", morsel.value):
                        csrf_values.add(morsel.value)
            except CookieError:
                continue
        if len(csrf_values) != 1:
            return ProbeResult("transient", response.status, "twitter_csrf_missing")
        csrf = next(iter(csrf_values))
        # Public X web-client bearer identifier, also used by RSSHub's web API.
        bearer = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
        headers = self._headers("auth_token=" + token + "; ct0=" + csrf, "twitter")
        headers.update({"Authorization": bearer, "x-csrf-token": csrf,
                        "x-twitter-auth-type": "OAuth2Session", "x-twitter-active-user": "yes",
                        "Referer": "https://x.com/"})
        try:
            response = self.transport.request("https://api.x.com/1.1/account/settings.json",
                                              headers=headers, timeout=self.config.provider_timeout)
        except ProbeError:
            return ProbeResult("transient", None, "network_error")
        kind = _classify_status(response.status)
        if kind != "ok":
            return ProbeResult(kind, response.status, "http_" + str(response.status))
        payload = _response_json(response)
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and any(isinstance(e, dict) and type(e.get("code")) is int and e["code"] in (32, 89) for e in errors):
                return ProbeResult("auth_failed", response.status, "twitter_auth_failed")
            name = payload.get("screen_name")
            if not errors and isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
                return ProbeResult("ok", response.status, "profile_ok")
        return ProbeResult("transient", response.status, "twitter_invalid_response")

    def probe(self, provider: str, cookie: str, full: bool = False) -> ProbeResult:
        validate_provider(provider)
        if provider == "twitter":
            return self.probe_twitter(cookie)
        return self.probe_zhihu(cookie, full=full) if provider == "zhihu" else self.probe_weibo(cookie)
