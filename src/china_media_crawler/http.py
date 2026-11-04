"""Bounded JSON transport with optional explicitly host-scoped credentials."""
from __future__ import annotations

import json
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.parse import urlparse

from china_media_crawler import __version__

MAX_RESPONSE_BYTES = 2 * 1024 * 1024

class CollectionError(RuntimeError):
    def __init__(
        self, kind: str, *, http_status: int | None = None, api_code: int | None = None,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.http_status = http_status
        self.api_code = api_code
        self.record_position: int | None = None
        self.record_fields: list[str] | None = None
        self.transport_error_type: str | None = None
        self.redirect_host: str | None = None
        self.redirect_path_class: str | None = None


_RATE_LIMIT_HEADERS = {
    "retry_after": ("retry-after",),
    "rate_limit_limit": ("x-ratelimit-limit", "ratelimit-limit"),
    "rate_limit_remaining": ("x-ratelimit-remaining", "ratelimit-remaining"),
    "rate_limit_reset": ("x-ratelimit-reset", "ratelimit-reset"),
}


def safe_header(headers: Any, names: tuple[str, ...]) -> str | None:
    if headers is None or not hasattr(headers, "items"):
        return None
    values = {str(key).lower(): value for key, value in headers.items()}
    for name in names:
        value = values.get(name)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if (value and len(value) <= 128
                and not any(ord(character) < 32 or ord(character) == 127
                            for character in value)):
            return value
    return None


def rate_limit_metadata(headers: Any) -> dict[str, str]:
    result = {}
    for attribute, names in _RATE_LIMIT_HEADERS.items():
        value = safe_header(headers, names)
        if value is not None:
            result[attribute] = value
    return result


def connection_error(error: BaseException) -> CollectionError:
    failure = CollectionError("connection_failed")
    failure.transport_error_type = type(getattr(error, "reason", error)).__name__
    return failure


def http_failure(error: HTTPError) -> CollectionError:
    failure = CollectionError("http_error", http_status=error.code)
    for attribute, value in rate_limit_metadata(error.headers).items():
        setattr(failure, attribute, value)
    return failure


def redirect_diagnostic(target: str | None) -> tuple[str | None, str]:
    if not target:
        return None, "unknown"
    try:
        parsed = urlparse(target)
        host = parsed.hostname
    except ValueError:
        return None, "invalid"
    if host:
        host = host.lower()
        if len(host) > 253 or any(ord(character) < 32 for character in host):
            host = None
    path = (parsed.path or "").lower()
    if not path or path == "/":
        category = "root"
    elif "visitor" in path:
        category = "visitor"
    elif "passport" in path or "login" in path or "signin" in path:
        category = "login"
    elif "sorry" in path or "verify" in path or "captcha" in path:
        category = "challenge"
    else:
        category = "other"
    return host, category


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Any, response: Any, code: int, msg: Any = None,
                         headers: Any = None, newurl: str | None = None) -> Any:
        target = newurl
        if not target and headers is not None:
            target = headers.get("Location") if hasattr(headers, "get") else None
        failure = CollectionError("unexpected_redirect", http_status=code)
        failure.redirect_host, failure.redirect_path_class = redirect_diagnostic(target)
        raise failure


def request_headers(url: str, headers: dict[str, str], cookie_header: str | None = None,
                    credential_host: str | None = None) -> dict[str, str]:
    headers = dict(headers)
    if cookie_header:
        try:
            parsed = urlparse(url)
            allowed = (credential_host and parsed.scheme == "https" and parsed.hostname == credential_host
                       and parsed.port in (None, 443) and not parsed.username and not parsed.password)
        except ValueError:
            allowed = False
        if not allowed:
            raise CollectionError("credential_destination_rejected")
        if not cookie_header.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in cookie_header):
            raise CollectionError("cookie_invalid")
        headers["Cookie"] = cookie_header
    return headers


def fetch_html(url: str, *, referer: str, opener_factory: Any = build_opener,
               cookie_header: str | None = None, credential_host: str | None = None,
               user_agent: str | None = None) -> tuple[int, str]:
    """One bounded HTML request with no redirects or retries."""
    request = Request(url, headers=request_headers(url, {
        "User-Agent": user_agent or f"ChinaMediaCrawler/{__version__} (research collection)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
    }, cookie_header, credential_host))
    opener = opener_factory(ProxyHandler({}), RejectRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            status = response.status
            if status != 200:
                raise CollectionError("http_error", http_status=status)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        failure = http_failure(error)
        error.close()
        raise failure from error
    except (URLError, TimeoutError, socket.timeout, OSError) as error:
        raise connection_error(error) from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise CollectionError("response_too_large")
    try:
        return status, body.decode("utf-8")
    except UnicodeError as error:
        raise CollectionError("unexpected_response") from error


def fetch_json(url: str, *, referer: str, opener_factory: Any = build_opener,
               cookie_header: str | None = None, credential_host: str | None = None,
               user_agent: str | None = None) -> dict[str, Any]:
    """One bounded request; optional host-scoped credentials, no retries or redirects."""
    request = Request(url, headers=request_headers(url, {
        "User-Agent": user_agent or f"ChinaMediaCrawler/{__version__} (research collection)",
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
    }, cookie_header, credential_host))
    opener = opener_factory(ProxyHandler({}), RejectRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            status = response.status
            if status != 200:
                raise CollectionError("http_error", http_status=status)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        failure = http_failure(error)
        error.close()
        raise failure from error
    except (URLError, TimeoutError, socket.timeout, OSError) as error:
        raise connection_error(error) from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise CollectionError("response_too_large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise CollectionError("unexpected_response") from error
    if not isinstance(payload, dict):
        raise CollectionError("unexpected_schema")
    return payload




