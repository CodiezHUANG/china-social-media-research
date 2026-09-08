#!/usr/bin/env python3
"""Bounded Playwright collector for one public Douban movie subject.

The adapter uses ordinary browser automation. It does not alter automation
signals, solve challenges, rotate accounts, or retry around access controls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse


EXTRACT_COMMENTS = r"""() => {
  const ratingMap = {allstar10: 1, allstar20: 2, allstar30: 3, allstar40: 4, allstar50: 5};
  return Array.from(document.querySelectorAll('div.comment')).map((comment) => {
    const author = comment.querySelector('span.comment-info a[href*="people"]');
    const star = comment.querySelector('span.comment-info span[class*="allstar"]');
    const time = comment.querySelector('a.comment-time');
    const location = comment.querySelector('span.comment-location');
    const votes = comment.querySelector('span.votes.vote-count');
    const content = comment.querySelector('span.short');
    let rating = null;
    const classes = star ? star.className.split(/\s+/) : [];
    for (const name of classes) {
      if (Object.prototype.hasOwnProperty.call(ratingMap, name)) rating = ratingMap[name];
    }
    return {
      author: author ? author.textContent.trim() : '',
      rating,
      timestamp: time ? (time.getAttribute('title') || time.textContent.trim()) : '',
      ip_location: location ? location.textContent.trim() : '',
      votes: votes ? votes.textContent.trim() : '0',
      content: content ? content.textContent.trim() : ''
    };
  });
}"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_cookie_header(value: str) -> list[dict[str, str]]:
    cookies: list[dict[str, str]] = []
    for pair in value.split(";"):
        if "=" not in pair:
            continue
        name, cookie_value = pair.strip().split("=", 1)
        if name:
            cookies.append(
                {
                    "name": name.strip(),
                    "value": cookie_value.strip(),
                    "domain": ".douban.com",
                    "path": "/",
                }
            )
    return cookies


def pseudonym(author: str, salt: str) -> str | None:
    if not author or not salt:
        return None
    digest = hashlib.sha256(f"{salt}:{author}".encode("utf-8")).hexdigest()
    return f"u_{digest[:16]}"


def challenge_reason(html: str) -> str | None:
    markers = (
        "禁止访问",
        "异常请求",
        "验证码",
        "登录豆瓣",
        "请先登录",
        "captcha",
        "sec.douban.com",
    )
    lowered = html.lower()
    return next((marker for marker in markers if marker.lower() in lowered), None)


def validate_page(
    page: Any,
    response: Any,
    subject_id: str,
    *,
    comments: bool,
) -> str:
    if response is None:
        raise RuntimeError("navigation returned no HTTP response")
    if response.status >= 400:
        raise RuntimeError(f"unexpected HTTP status: {response.status}")

    parsed = urlparse(page.url)
    expected_path = f"/subject/{subject_id}/comments" if comments else f"/subject/{subject_id}"
    if parsed.hostname != "movie.douban.com" or parsed.path.rstrip("/") != expected_path:
        raise RuntimeError(f"unexpected redirect: {page.url}")

    html = page.content()
    challenge = challenge_reason(html)
    if challenge:
        raise RuntimeError(f"access or login challenge: {challenge}")
    if not page.title().strip():
        raise RuntimeError("unexpected HTML: page title is empty")
    return html


def validate_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise RuntimeError("unexpected Douban comment schema")
    return value


def close_safely(resource: Any) -> None:
    try:
        resource.close()
    except Exception:
        pass


def build_url(subject_id: str, surface: str, page: int) -> str:
    params: dict[str, str | int] = {
        "start": (page - 1) * 20,
        "limit": 20,
        "status": "F" if surface == "wantsee" else "P",
    }
    if surface == "reviews":
        params["sort"] = "new_score"
    return f"https://movie.douban.com/subject/{subject_id}/comments?{urlencode(params)}"


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--surface", choices=("reviews", "wantsee"), default="reviews")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-delay", type=float, default=2.5)
    parser.add_argument("--max-delay", type=float, default=5.0)
    parser.add_argument("--headless", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"\d+", args.subject_id):
        raise SystemExit("subject ID must contain digits only")
    if not 1 <= args.max_pages <= 5:
        raise SystemExit("max pages must be between 1 and 5")
    if args.min_delay < 2.5 or args.max_delay < args.min_delay:
        raise SystemExit("delays must satisfy 2.5 <= min-delay <= max-delay")

    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    data_path = output / f"douban_{args.surface}.jsonl"
    manifest_path = output / "douban_manifest.json"
    if data_path.exists() or manifest_path.exists():
        raise SystemExit("refusing to overwrite an existing Douban run")

    cookie_value = os.getenv("DOUBAN_COOKIE", "").strip()
    salt = os.getenv("DOUBAN_PSEUDONYM_SALT", "").strip()
    manifest: dict[str, Any] = {
        "status": "running",
        "subject_id": args.subject_id,
        "surface": args.surface,
        "max_pages": args.max_pages,
        "started_at": utc_now(),
        "cookie_used": bool(cookie_value),
        "pseudonymized": bool(salt),
        "pages": [],
        "record_count": 0,
    }
    write_manifest(manifest_path, manifest)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        manifest.update(
            {
                "status": "failed",
                "finished_at": utc_now(),
                "error_type": "missing_playwright",
            }
        )
        write_manifest(manifest_path, manifest)
        raise SystemExit("Playwright is not installed; run preflight.py") from error

    seen: set[tuple[str, str]] = set()
    try:
        with sync_playwright() as playwright, ExitStack() as cleanup:
            browser = playwright.chromium.launch(headless=args.headless)
            cleanup.callback(close_safely, browser)
            context = browser.new_context(locale="zh-CN")
            cleanup.callback(close_safely, context)
            if cookie_value:
                context.add_cookies(parse_cookie_header(cookie_value))
            page = context.new_page()

            subject_url = f"https://movie.douban.com/subject/{args.subject_id}/"
            response = page.goto(subject_url, wait_until="domcontentloaded", timeout=30_000)
            validate_page(page, response, args.subject_id, comments=False)
            manifest["subject_page_title"] = page.title()
            manifest["subject_page_url"] = page.url
            write_manifest(manifest_path, manifest)

            with data_path.open("x", encoding="utf-8") as output_file:
                for page_number in range(1, args.max_pages + 1):
                    if page_number > 1:
                        time.sleep(random.uniform(args.min_delay, args.max_delay))
                    captured_at = utc_now()
                    response = page.goto(
                        build_url(args.subject_id, args.surface, page_number),
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    validate_page(page, response, args.subject_id, comments=True)

                    rows = validate_rows(page.evaluate(EXTRACT_COMMENTS))
                    accepted = 0
                    for row in rows:
                        content = str(row.get("content") or "").strip()
                        timestamp = str(row.get("timestamp") or "").strip()
                        if not content:
                            continue
                        key = (content, timestamp)
                        if key in seen:
                            continue
                        seen.add(key)
                        record = {
                            "platform": "douban",
                            "subject_id": args.subject_id,
                            "surface": args.surface,
                            "page": page_number,
                            "content": content,
                            "timestamp": timestamp or None,
                            "rating": row.get("rating"),
                            "votes": row.get("votes"),
                            "ip_location": str(row.get("ip_location") or "").strip() or None,
                            "user_pseudonym": pseudonym(str(row.get("author") or ""), salt),
                            "captured_at": captured_at,
                        }
                        output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                        output_file.flush()
                        accepted += 1
                    manifest["pages"].append(
                        {
                            "page": page_number,
                            "http_status": response.status if response else None,
                            "returned": len(rows),
                            "accepted": accepted,
                            "captured_at": captured_at,
                        }
                    )
                    manifest["record_count"] += accepted
                    write_manifest(manifest_path, manifest)
                    if not rows:
                        break

        manifest.update({"status": "complete", "finished_at": utc_now()})
        write_manifest(manifest_path, manifest)
        return 0
    except BaseException as error:
        manifest.update(
            {
                "status": "failed",
                "finished_at": utc_now(),
                "error_type": type(error).__name__,
            }
        )
        write_manifest(manifest_path, manifest)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
