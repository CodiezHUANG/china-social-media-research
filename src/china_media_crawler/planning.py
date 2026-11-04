from __future__ import annotations

import argparse
from pathlib import Path

from china_media_crawler.configuration import DEFAULT_SURFACE, PLATFORM_SURFACES
from china_media_crawler.models import Plan

def build_plan(args: argparse.Namespace) -> Plan:
    surface = args.surface or DEFAULT_SURFACE[args.platform]
    if surface not in PLATFORM_SURFACES[args.platform]:
        allowed = ", ".join(sorted(PLATFORM_SURFACES[args.platform]))
        raise ValueError(f"surface {surface!r} is invalid for {args.platform}; choose {allowed}")
    if not 1 <= args.max_items <= 200:
        raise ValueError("--max-items must be between 1 and 200")
    comment_limit = 1000 if (args.platform == "weibo" and surface == "comments") else 100
    if not 0 <= args.comments_per_item <= comment_limit:
        raise ValueError(f"--comments-per-item must be between 0 and {comment_limit}")
    page_limit = 50 if (args.platform == "weibo" and surface in {"comments", "search"}) else 5
    if not 1 <= args.max_pages <= page_limit:
        raise ValueError(f"--max-pages must be between 1 and {page_limit}")
    if not 1 <= args.danmaku_per_item <= 1000 or not 1 <= args.max_parts <= 5:
        raise ValueError("--danmaku-per-item must be 1..1000 and --max-parts must be 1..5")
    if not 30 <= args.timeout_seconds <= 7200:
        raise ValueError("--timeout-seconds must be between 30 and 7200")
    if surface == "comments" and args.comments_per_item == 0:
        raise ValueError("--comments-per-item must be positive for a comments collection")
    if args.item_url and not (args.platform == "xhs" and surface == "detail"):
        raise ValueError("--item-url is only valid for Xiaohongshu detail collection")

    output = Path(args.output).expanduser().resolve()
    from china_media_crawler.platforms import get_adapter

    adapter = get_adapter(args.platform, surface)
    plan = adapter.build_plan(args, output, surface)
    plan.backend = adapter.backend
    from china_media_crawler.authentication import auth_capability

    plan.authentication = auth_capability(args.platform, surface)
    return plan
