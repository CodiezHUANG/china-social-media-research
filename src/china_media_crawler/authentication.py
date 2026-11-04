"""Platform-scoped authentication contracts and lazy session dispatch."""
from __future__ import annotations

import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import Protocol

from china_media_crawler.configuration import PLATFORM_SURFACES
from china_media_crawler.privacy import SAFE_INHERITED_ENVIRONMENT


def auth_capability(platform: str, surface: str) -> dict:
    if platform not in PLATFORM_SURFACES or surface not in PLATFORM_SURFACES[platform]:
        raise ValueError("unsupported authentication capability")
    if platform == "douban":
        return {"requirement": "session_optional", "implemented_modes": ["anonymous", "cookie", "saved"],
                "session_provider": "douban", "session_scope": "douban", "cookie_environment": "DOUBAN_COOKIE"}
    if platform == "bilibili":
        return {"requirement": "session_optional",
                "implemented_modes": ["anonymous", "cookie", "saved"],
                "session_provider": "bilibili", "session_scope": "bilibili",
                "cookie_environment": "BILIBILI_COOKIE"}
    if platform == "weibo":
        result = {"requirement": "anonymous", "implemented_modes": ["anonymous"],
                  "session_provider": None, "session_scope": "weibo"}
        if surface in {"comments", "search"}:
            result.update({
                "requirement": "session_optional",
                "implemented_modes": ["anonymous", "cookie", "saved"],
                "session_provider": "weibo",
                "cookie_environment": "WEIBO_COOKIE",
                "visitor_session": "optional",
                "visitor_session_browser": "playwright",
            })
        return result
    if platform == "douyin":
        return {
            "requirement": "session_required",
            "implemented_modes": ["cookie", "saved"],
            "session_provider": "douyin",
            "session_scope": "douyin",
            "cookie_environment": "DOUYIN_COOKIE",
            "ms_token": "browser_login_state",
        }
    if platform == "xhs":
        return {
            "requirement": "session_required",
            "implemented_modes": ["cookie", "saved"],
            "session_provider": "xhs",
            "session_scope": "xhs",
            "cookie_environment": "XHS_COOKIE",
        }
    return {"requirement": "anonymous", "implemented_modes": ["anonymous"],
            "session_provider": None, "session_scope": platform}


class SessionProvider(Protocol):
    platform: str

    def command(self, action: str, timeout: int,
                login_url: str | None = None) -> list[str]: ...

    def collection_state(self, mode: str, cookie: str) -> tuple[str, dict | None]: ...


class BrowserSessionProvider:
    platform: str

    def command(self, action: str, timeout: int, login_url: str | None = None) -> list[str]:
        if action not in {"login", "status", "clear"} or not 15 <= timeout <= 1800:
            raise ValueError("invalid session action or timeout")
        if login_url and self.platform != "xhs":
            raise ValueError("custom login URL is only supported for Xiaohongshu")
        python = sys.executable
        if self.platform == "douban":
            planner = import_module("china_media_crawler.platforms.douban_plan")
            python, _ = planner.selected_python()
        command = [str(python), "-B", str(Path(__file__).with_name(self.platform + "_session.py")),
                   "--action", action, "--timeout-seconds", str(timeout)]
        if login_url:
            command += ["--login-url", login_url]
        return command

    def collection_state(self, mode: str, cookie: str) -> tuple[str, dict | None]:
        module = import_module("china_media_crawler." + self.platform + "_session")
        return module.collection_session(mode, cookie)


class DoubanSessionProvider(BrowserSessionProvider):
    platform = "douban"


class BilibiliSessionProvider(BrowserSessionProvider):
    platform = "bilibili"


class WeiboSessionProvider(BrowserSessionProvider):
    platform = "weibo"


class DouyinSessionProvider(BrowserSessionProvider):
    platform = "douyin"


class XhsSessionProvider(BrowserSessionProvider):
    platform = "xhs"


def get_session_provider(platform: str) -> SessionProvider:
    if platform not in PLATFORM_SURFACES:
        raise ValueError("unsupported platform")
    if platform == "douban":
        return DoubanSessionProvider()
    if platform == "bilibili":
        return BilibiliSessionProvider()
    if platform == "weibo":
        return WeiboSessionProvider()
    if platform == "douyin":
        return DouyinSessionProvider()
    if platform == "xhs":
        return XhsSessionProvider()
    raise ValueError(f"session provider is not implemented for {platform}")


def run_session(platform: str, action: str, timeout: int,
                login_url: str | None = None) -> int:
    command = get_session_provider(platform).command(action, timeout, login_url)
    environment = {k: v for k in SAFE_INHERITED_ENVIRONMENT if (v := os.getenv(k)) is not None}
    return subprocess.run(command, env=environment, check=False, timeout=timeout + 60).returncode
