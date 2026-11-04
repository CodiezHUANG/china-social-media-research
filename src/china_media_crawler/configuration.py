from __future__ import annotations

import argparse
from pathlib import Path

from urllib.parse import parse_qs, urlparse

PLATFORM_SURFACES = {
    "xhs": {"search", "detail"},
    "douyin": {"search", "detail"},
    "weibo": {"search", "comments", "hot"},
    "bilibili": {"search", "detail", "comments", "danmaku"},
    "douban": {"reviews", "wantsee"},
    "maoyan": {"reviews"},
}

DEFAULT_SURFACE = {
    "xhs": "search",
    "douyin": "search",
    "weibo": "search",
    "bilibili": "search",
    "douban": "reviews",
    "maoyan": "reviews",
}

def clean_text(value: str, label: str) -> str:
    value = value.strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a non-empty single-line value")
    return value


def require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise ValueError(f"{label} was not found: {path}")
    return path


def read_ids(args: argparse.Namespace) -> list[str]:
    ids = [clean_text(value, "item ID") for value in args.item_id]
    if args.input_file:
        source = Path(args.input_file).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"input file was not found: {source}")
        for line in source.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                ids.append(clean_text(value, "item ID"))
    return list(dict.fromkeys(ids))


def read_urls(args: argparse.Namespace) -> list[str]:
    urls = [clean_text(value, "item URL") for value in args.item_url]
    if args.input_file:
        source = Path(args.input_file).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"input file was not found: {source}")
        for line in source.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                urls.append(clean_text(value, "item URL"))
    return list(dict.fromkeys(urls))


def validate_xhs_note_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    params = parse_qs(parsed.query)
    if (
        parsed.scheme not in {"http", "https"}
        or not any(host == domain or host.endswith("." + domain)
                   for domain in ("xiaohongshu.com", "rednote.com"))
        or "/explore/" not in parsed.path
        or not params.get("xsec_token")
        or not params.get("xsec_source")
    ):
        raise ValueError(
            "Xiaohongshu detail requires a full note URL containing "
            "xsec_token and xsec_source; use --item-url, not a bare ID"
        )
    return value
