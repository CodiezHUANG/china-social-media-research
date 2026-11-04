"""Platform collector registry."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Callable, Protocol

from china_media_crawler.configuration import PLATFORM_SURFACES
from china_media_crawler.models import Plan


class PlatformAdapter(Protocol):
    backend: str

    def build_plan(self, args: argparse.Namespace, output: Path, surface: str) -> Plan: ...


@dataclass(frozen=True)
class Adapter:
    module: str
    planner: str
    backend: str

    def build_plan(self, args: argparse.Namespace, output: Path, surface: str) -> Plan:
        function: Callable[..., Plan] = getattr(import_module(self.module), self.planner)
        return function(args, output, surface)


def get_adapter(platform: str, surface: str) -> PlatformAdapter:
    if platform not in PLATFORM_SURFACES or surface not in PLATFORM_SURFACES[platform]:
        raise ValueError(f"unsupported platform/surface: {platform}/{surface}")
    prefix = "china_media_crawler.platforms."
    if platform == "maoyan":
        return Adapter(prefix + "maoyan", "maoyan_plan", "maoyan-public")
    if platform == "weibo":
        return Adapter(prefix + "weibo", "weibo_plan", "weibo-public")
    if platform == "douyin":
        return Adapter(prefix + "douyin", "douyin_plan", "douyin")
    if platform == "xhs":
        return Adapter(prefix + "xhs", "xhs_plan", "xhs")
    if platform == "bilibili" and surface in {"detail", "comments", "danmaku"}:
        return Adapter(prefix + "bilibili_content", "content_plan", "bilibili-public-content")
    if platform == "bilibili":
        return Adapter(prefix + "bilibili_search", "search_plan", "bilibili-public-search")
    if platform == "douban":
        return Adapter(prefix + "douban_plan", "douban_plan", "douban-public")
    raise ValueError(f"adapter is not implemented for {platform}")


def capabilities() -> list[dict[str, object]]:
    from china_media_crawler.authentication import auth_capability

    rows = []
    for platform, surfaces in PLATFORM_SURFACES.items():
        for surface in sorted(surfaces):
            adapter = get_adapter(platform, surface)
            rows.append({
                "platform": platform,
                "surface": surface,
                "backend": adapter.backend,
                "authentication": auth_capability(platform, surface),
            })
    return rows
