"""Project-owned private storage and manual browser login shared by profiles."""
from __future__ import annotations
import argparse
import json
import math
import os
import stat
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from china_media_crawler.http import CollectionError


@dataclass(frozen=True)
class BrowserProfile:
    platform: str
    label: str
    domain: str
    home: str
    auth_cookie: str
    login_marker: tuple[str, str] | None = None
    login_selector: str | None = None
    require_cookie_change: bool = False
    domains: tuple[str, ...] = ()
    blocked_login_hosts: tuple[str, ...] = ()
    login_wall_markers: tuple[str, ...] = ()
    login_state_script: str | None = None


DOUBAN = BrowserProfile("douban", "豆瓣", "douban.com", "https://movie.douban.com/", "dbcl2",
                        blocked_login_hosts=("sec.douban.com",))
BILIBILI = BrowserProfile("bilibili", "B站", "bilibili.com", "https://www.bilibili.com/", "SESSDATA")
WEIBO = BrowserProfile("weibo", "微博", "weibo.cn", "https://m.weibo.cn/", "SUB",
                       login_marker=("MLOGIN", "1"))
DOUYIN = BrowserProfile("douyin", "抖音", "douyin.com", "https://www.douyin.com/", "sessionid")
XHS = BrowserProfile(
    "xhs", "小红书", "xiaohongshu.com", "https://www.xiaohongshu.com/", "web_session",
    require_cookie_change=True, domains=("rednote.com",),
    login_wall_markers=("登录后查看", "扫码登录"),
    login_state_script="""() => {
      const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
      const i = u && u.userInfo && u.userInfo._value;
      return !!(i && i.guest === false);
    }""",
)


def state_path(profile):
    base = Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else Path.home() / ".local" / "state"
    return base / "ChinaMediaCrawler" / "auth" / profile.platform / "storage_state.json"


class BrowserSession:
    def __init__(self, profile: BrowserProfile, location: Callable | None = None):
        self.profile = profile
        self.location = location or (lambda: state_path(profile))

    def checked_path(self):
        path = Path(os.path.abspath(self.location().expanduser()))
        checkout = Path(__file__).absolute().parents[2]
        if path == checkout or checkout in path.parents:
            raise CollectionError("session_location_not_private")
        for item in reversed((path, *path.parents)):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise CollectionError("session_location_not_private")
        return path

    def domain_allowed(self, host):
        host = host.lower()
        return any(host == domain or host.endswith("." + domain)
                   for domain in (self.profile.domain, *self.profile.domains))

    def scoped_state(self, value):
        if not isinstance(value, dict) or not isinstance(value.get("cookies"), list) or not isinstance(value.get("origins"), list):
            raise CollectionError("session_invalid")
        cookies, origins = [], []
        for cookie in value["cookies"]:
            if not isinstance(cookie, dict) or not all(isinstance(cookie.get(k), str) for k in ("domain", "name", "value")):
                raise CollectionError("session_invalid")
            if self.domain_allowed(cookie["domain"].lstrip(".")):
                expiry = cookie.get("expires", -1)
                if type(expiry) not in (int, float) or not math.isfinite(expiry):
                    raise CollectionError("session_invalid")
                cookies.append(cookie)
        for origin in value["origins"]:
            if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str) or not isinstance(origin.get("localStorage"), list):
                raise CollectionError("session_invalid")
            parsed = urlparse(origin["origin"])
            if parsed.scheme == "https" and self.domain_allowed(parsed.hostname or ""):
                origins.append(origin)
        return {"cookies": cookies, "origins": origins}

    def load(self):
        path = self.checked_path()
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise CollectionError("session_invalid")
            return self.scoped_state(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            raise CollectionError("session_invalid") from None

    def save(self, value):
        body = json.dumps(self.scoped_state(value), ensure_ascii=False).encode("utf-8")
        if len(body) > 2 * 1024 * 1024:
            raise CollectionError("session_invalid")
        path = self.checked_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".storage_state-", suffix=".tmp", dir=path.parent)
            with os.fdopen(fd, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def clear(self):
        try:
            self.checked_path().unlink()
            return True
        except FileNotFoundError:
            return False

    def authenticated(self, cookies):
        now = time.time()
        def valid(name, value=None):
            return any(
                c.get("name") == name
                and (value is None or str(c.get("value")) == value)
                and c.get("value")
                and self.domain_allowed(str(c.get("domain", "")).lstrip("."))
                and (c.get("expires", -1) == -1 or c.get("expires", 0) > now)
                for c in cookies
            )
        if not valid(self.profile.auth_cookie):
            return False
        if self.profile.login_marker is not None:
            name, value = self.profile.login_marker
            return valid(name, value)
        return True

    def login(self, timeout, playwright_factory=None, login_url=None):
        self.checked_path()
        if playwright_factory is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise CollectionError("missing_playwright") from None
            playwright_factory = sync_playwright
        target = login_url or self.profile.home
        with playwright_factory() as driver, ExitStack() as cleanup:
            browser = driver.chromium.launch(headless=False)
            cleanup.callback(browser.close)
            context = browser.new_context(locale="zh-CN")
            cleanup.callback(context.close)
            page = context.new_page()
            print(f"请在专用浏览器中正常登录{self.profile.label}，完成后回到 {target}。", flush=True)
            response = page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            if response is None or response.status >= 400:
                raise CollectionError("login_navigation_failed")
            page.wait_for_timeout(2000)
            initial_auth_values = {
                cookie.get("value") for cookie in context.cookies()
                if cookie.get("name") == self.profile.auth_cookie
            }
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise CollectionError("login_cancelled")
                cookies = context.cookies()
                parsed_page = urlparse(page.url)
                host = parsed_page.hostname or ""
                logged_in = (
                    self.domain_allowed(host)
                    and host not in self.profile.blocked_login_hosts
                    and "/website-login" not in parsed_page.path
                    and "/login" not in parsed_page.path
                    and self.authenticated(cookies)
                )
                if self.profile.require_cookie_change:
                    current_values = {
                        cookie.get("value") for cookie in cookies
                        if cookie.get("name") == self.profile.auth_cookie
                    }
                    logged_in = logged_in and bool(current_values - initial_auth_values)
                if logged_in and self.profile.login_selector:
                    try:
                        logged_in = page.locator(self.profile.login_selector).count() > 0
                    except Exception:
                        logged_in = False
                if logged_in and self.profile.login_wall_markers:
                    try:
                        body_text = page.evaluate("() => document.body.innerText")
                        logged_in = not any(
                            marker in body_text
                            for marker in self.profile.login_wall_markers
                        )
                    except Exception:
                        logged_in = False
                if logged_in and self.profile.login_state_script:
                    try:
                        logged_in = bool(page.evaluate(self.profile.login_state_script))
                    except Exception:
                        logged_in = False
                if logged_in:
                    self.save(context.storage_state())
                    print(f"{self.profile.label}会话已保存；有效性由后续采集确认。", flush=True)
                    return
                page.wait_for_timeout(1000)
            raise CollectionError("login_timeout")


def session_main(profile, factory, argv=None):
    parser = argparse.ArgumentParser(description=f"Manage private {profile.platform} browser session")
    parser.add_argument("--action", choices=("login", "status", "clear"), default="login")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    if profile.platform == "xhs":
        parser.add_argument("--login-url", help="HTTPS Xiaohongshu or Rednote login page")
    args = parser.parse_args(argv)
    if not 15 <= args.timeout_seconds <= 1800:
        parser.error("timeout must be between 15 and 1800 seconds")
    try:
        store = factory()
        login_url = getattr(args, "login_url", None)
        if login_url:
            parsed = urlparse(login_url)
            host = (parsed.hostname or "").lower()
            if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                    or parsed.port not in (None, 443)
                    or not store.domain_allowed(host)
                    or host in profile.blocked_login_hosts):
                parser.error("login URL must be an HTTPS Xiaohongshu or Rednote page")
        if args.action == "status":
            print(json.dumps({"platform": profile.platform, "saved": store.load() is not None, "validity": "not_checked_online"}))
        elif args.action == "clear":
            print(json.dumps({"platform": profile.platform, "cleared": store.clear()}))
        else:
            store.login(args.timeout_seconds, login_url=login_url)
        return 0
    except KeyboardInterrupt:
        print("login_cancelled")
        return 130
    except Exception as error:
        print(error.kind if isinstance(error, CollectionError) else "session_operation_failed")
        return 1
