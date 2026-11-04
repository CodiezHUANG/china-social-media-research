"""Douban adapter for the shared private browser-session lifecycle."""
from __future__ import annotations
if __package__ in {None, ""}:
    import sys
    from pathlib import Path
    sys.path[0] = str(Path(__file__).resolve().parents[1])

import re
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlparse
from china_media_crawler.browser_session import BrowserSession, DOUBAN, state_path, session_main
from china_media_crawler.http import CollectionError


def session_path():
    return state_path(DOUBAN)


def store():
    return BrowserSession(DOUBAN, session_path)


def load_session():
    return store().load()


def save_session(value):
    return store().save(value)


def clear_session():
    return store().clear()


def auth_cookie_present(cookies):
    return store().authenticated(cookies)


def login_session(timeout, playwright_factory=None):
    return store().login(timeout, playwright_factory)


def cookie_header(state, url):
    if state is None:
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        allowed = (
            parsed.scheme == "https"
            and (host == "douban.com" or host.endswith(".douban.com"))
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise CollectionError("credential_destination_rejected")
    path, now = parsed.path or "/", time.time()
    pairs = []
    for cookie in state["cookies"]:
        name, value = cookie["name"], cookie["value"]
        if (not isinstance(name, str) or not isinstance(value, str)
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or not value.isascii()
                or any(ord(char) < 32 or char in ';\x7f' for char in value)):
            raise CollectionError("cookie_invalid")
        domain = str(cookie.get("domain", "")).lower()
        matches = (host == domain.lstrip(".") or host.endswith(domain)
                   if domain.startswith(".") else host == domain)
        expires = cookie.get("expires", -1)
        cookie_path = cookie.get("path", "/")
        if not isinstance(cookie_path, str) or not cookie_path.startswith("/"):
            raise CollectionError("session_invalid")
        path_matches = path == cookie_path or (
            path.startswith(cookie_path)
            and (cookie_path.endswith("/") or path[len(cookie_path):].startswith("/"))
        )
        if matches and path_matches and (expires == -1 or expires > now):
            pairs.append(name + "=" + value)
    return "; ".join(pairs) or None


def parse_cookie_state(value):
    parsed = SimpleCookie()
    try:
        if any(char in value for char in "\r\n\x00"):
            raise CookieError()
        parsed.load(value)
    except CookieError:
        raise CollectionError("cookie_invalid") from None
    return {
        "cookies": [
            {"name": name, "value": morsel.value, "domain": ".douban.com",
             "path": "/", "expires": -1, "secure": True}
            for name, morsel in parsed.items()
        ],
        "origins": [],
    }


def collection_session(mode, cookie):
    if mode not in {"auto", "required", "off"}:
        raise ValueError("unsupported session mode")
    if mode == "off":
        return "anonymous", None
    if cookie:
        state = parse_cookie_state(cookie)
        source = "cookie"
    else:
        state = load_session()
        source = "saved"
    if state is None:
        if mode == "required":
            raise CollectionError("login_required")
        return "anonymous", None
    if not auth_cookie_present(state["cookies"]):
        raise CollectionError("session_expired")
    header = cookie_header(state, "https://movie.douban.com/subject/1/comments")
    if not header or not any(pair.startswith("dbcl2=") for pair in header.split("; ")):
        raise CollectionError("session_scope_invalid")
    return source, state


def main(argv=None):
    return session_main(DOUBAN, store, argv)


if __name__ == "__main__":
    raise SystemExit(main())
