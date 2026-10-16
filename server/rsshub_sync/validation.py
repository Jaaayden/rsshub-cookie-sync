"""validation (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any, Dict, Iterable, Tuple

from .constants import COOKIE_KEYS, COOKIE_NAME_RE, HASH_CHARS, MAX_COOKIE_BYTES, MAX_INPUT_BYTES, PROVIDERS
from .errors import InvalidInput


def sha256_prefix(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()[:HASH_CHARS]


def twitter_token(value: Any, *, header: bool = False) -> str:
    validate_cookie_shape(value)
    if header:
        if not value.startswith("auth_token="):
            raise InvalidInput("invalid Twitter auth token")
        value = value[len("auth_token="):]
    if len(value) > 4085 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise InvalidInput("invalid Twitter auth token")
    return value


def validate_provider_value(provider: str, value: Any) -> str:
    return twitter_token(value) if provider == "twitter" else validate_cookie_header(value)


def validate_env_secret(key: str, value: str) -> None:
    if key == "TWITTER_AUTH_TOKEN":
        # Empty optional credentials and existing pools must survive migration
        # verbatim. Automatic updates validate single tokens before this layer.
        if not value:
            return
        if "," in value:
            validate_cookie_shape(value)
        else:
            twitter_token(value)
    elif key in COOKIE_KEYS.values():
        validate_cookie_header(value)


def validate_cookie_header(value: Any) -> str:
    """Validate a browser Cookie request header without normalising secrets."""

    validate_cookie_shape(value)
    assert isinstance(value, str)
    pairs = []
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise InvalidInput("cookieHeader contains an invalid cookie pair")
        name, cookie_value = part.split("=", 1)
        name = name.strip()
        if not name or not COOKIE_NAME_RE.fullmatch(name):
            raise InvalidInput("cookieHeader contains an invalid cookie name")
        pairs.append((name, cookie_value))
    if not pairs:
        raise InvalidInput("cookieHeader contains no cookies")
    return value


def validate_cookie_shape(value: Any) -> str:
    """Validate only bounded transport-safe input.

    The native host may send one valid provider and one malformed provider in
    the same request.  Shape validation therefore happens while decoding the
    envelope, while cookie-pair validation is performed per provider by
    ``apply`` so the valid provider can still be accepted independently.
    """

    if not isinstance(value, str):
        raise InvalidInput("cookieHeader must be a string")
    try:
        raw = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise InvalidInput("cookieHeader is not valid UTF-8") from exc
    if not raw or len(raw) > MAX_COOKIE_BYTES:
        raise InvalidInput("cookieHeader has an invalid length")
    if any(byte in raw for byte in (0, 10, 13)):
        raise InvalidInput("cookieHeader contains a forbidden control character")
    if any(byte < 32 or byte == 127 for byte in raw):
        raise InvalidInput("cookieHeader contains a forbidden control character")
    return value


def validate_provider(provider: Any) -> str:
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise InvalidInput("unknown provider")
    return provider


def build_manual_update_request(provider: Any, cookie_header: Any) -> Dict[str, Any]:
    """Build one normal apply request from a hidden interactive Cookie input."""

    provider_name = validate_provider(provider)
    if provider_name == "twitter":
        token = twitter_token(cookie_header, header=isinstance(cookie_header, str) and cookie_header.startswith("auth_token="))
        cookie = "auth_token=" + token
    else:
        cookie = validate_cookie_header(cookie_header)
    return {
        "version": 1,
        "providers": {provider_name: {"cookieHeader": cookie}},
    }


def _reject_duplicate_json_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput("request contains duplicate JSON keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise InvalidInput("request contains an invalid JSON number")


def strict_json_from_stdin(stream: Any = None, max_bytes: int = MAX_INPUT_BYTES) -> Dict[str, Any]:
    """Read exactly one strict JSON object from stdin, bounded in memory."""

    stream = stream or sys.stdin.buffer
    try:
        raw = stream.read(max_bytes + 1)
    except Exception as exc:
        raise InvalidInput("cannot read request") from exc
    if len(raw) > max_bytes:
        raise InvalidInput("request is too large")
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidInput, RecursionError) as exc:
        raise InvalidInput("request is not valid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidInput("request must be a JSON object")
    if set(value) not in ({"version", "providers"}, {"version", "providers", "diagnostics"}) or ("diagnostics" in value and value["diagnostics"] is not True):
        raise InvalidInput("request has unexpected fields")
    if type(value.get("version")) is not int or value.get("version") != 1:
        raise InvalidInput("unsupported request version")
    providers = value.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise InvalidInput("providers must be a non-empty object")
    for provider, item in providers.items():
        validate_provider(provider)
        if not isinstance(item, dict) or set(item) != {"cookieHeader"}:
            raise InvalidInput("provider payload has unexpected fields")
        validate_cookie_shape(item["cookieHeader"])
    return value
