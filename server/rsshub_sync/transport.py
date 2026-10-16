"""transport (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .constants import MAX_HTTP_BODY_BYTES
from .errors import ProbeError


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    set_cookies: Tuple[str, ...] = ()


class HTTPTransport:
    """Small HTTP GET/POST transport with bounded response reads."""

    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(
            self,
            request: Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> Any:
            # Provider and notification endpoints are fixed; following a
            # redirect could send a Cookie or Bark key to another host.
            raise ProbeError("redirect rejected")

    def __init__(self) -> None:
        # An explicit empty ProxyHandler disables urllib's environment proxy
        # discovery.  Provider cookies, Bark device keys, and localhost
        # health requests must never be sent through HTTP(S)_PROXY/ALL_PROXY.
        self._opener = build_opener(ProxyHandler({}), self._NoRedirect())

    def request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 20.0,
        max_body_bytes: int = MAX_HTTP_BODY_BYTES,
    ) -> HTTPResponse:
        request = Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HTTPResponse(
                    int(response.getcode()),
                    response.read(max_body_bytes + 1),
                    tuple(response.headers.get_all("Set-Cookie", [])),
                )
        except HTTPError as exc:
            try:
                response_body = exc.read(max_body_bytes + 1)
            except Exception:
                response_body = b""
            return HTTPResponse(int(exc.code), response_body, tuple(exc.headers.get_all("Set-Cookie", [])))
        except (URLError, TimeoutError, OSError) as exc:
            raise ProbeError("network error") from exc
