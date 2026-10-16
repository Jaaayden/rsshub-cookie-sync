"""notify (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import json
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import RuntimeConfig
from .errors import ProbeError
from .transport import HTTPTransport


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
