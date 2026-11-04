"""Bilibili session profile and fixed-host, path-scoped API credentials."""
from __future__ import annotations
if __package__ in {None, ""}:
    import sys
    from pathlib import Path
    sys.path[0] = str(Path(__file__).resolve().parents[1])

import re
import time
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlparse
from china_media_crawler.browser_session import BrowserSession, BILIBILI, state_path, session_main
from china_media_crawler.http import CollectionError


def session_path():
    return state_path(BILIBILI)


def store():
    return BrowserSession(BILIBILI, session_path)


def collection_session(mode, cookie):
    if mode not in {"auto", "required", "off"}:
        raise ValueError("unsupported session mode")
    if mode == "off":
        return "anonymous", None
    if cookie:
        parsed = SimpleCookie()
        try:
            if any(c in cookie for c in '\r\n\x00'):
                raise CookieError()
            parsed.load(cookie)
        except CookieError:
            raise CollectionError("cookie_invalid") from None
        state = {"cookies": [{"name": k, "value": v.value, "domain": ".bilibili.com", "path": "/",
                              "expires": -1, "secure": True} for k, v in parsed.items()], "origins": []}
        source = "cookie"
    else:
        state = store().load()
        source = "saved"
    if state is None:
        if mode == "required":
            raise CollectionError("login_required")
        return "anonymous", None
    if not store().authenticated(state["cookies"]):
        raise CollectionError("session_expired")
    header = cookie_header(state, "https://api.bilibili.com/x/web-interface/view")
    if not header or not any(pair.startswith("SESSDATA=") for pair in header.split("; ")):
        raise CollectionError("session_scope_invalid")
    return source, state


def cookie_header(state, url):
    if state is None:
        return None
    try:
        parsed = urlparse(url)
        allowed = (parsed.scheme == "https" and parsed.hostname == "api.bilibili.com"
                   and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        allowed = False
    if not allowed:
        raise CollectionError("credential_destination_rejected")
    host, path, now = parsed.hostname, parsed.path or "/", time.time()
    pairs = []
    for cookie in state["cookies"]:
        name, value = cookie["name"], cookie["value"]
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or not value.isascii() or any(ord(c) < 32 or c in ';\x7f' for c in value)):
            raise CollectionError("cookie_invalid")
        domain = cookie["domain"].lower()
        matches = (host == domain.lstrip(".") or host.endswith(domain)) if domain.startswith(".") else host == domain
        expires = cookie.get("expires", -1)
        cookie_path = cookie.get("path", "/")
        if not isinstance(cookie_path, str) or not cookie_path.startswith("/"):
            raise CollectionError("session_invalid")
        path_matches = path == cookie_path or (path.startswith(cookie_path) and
                         (cookie_path.endswith("/") or path[len(cookie_path):].startswith("/")))
        if matches and path_matches and (expires == -1 or expires > now):
            pairs.append(name + "=" + value)
    return "; ".join(pairs) or None


def main(argv=None):
    return session_main(BILIBILI, store, argv)


if __name__ == "__main__":
    raise SystemExit(main())
