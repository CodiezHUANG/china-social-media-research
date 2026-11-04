#!/usr/bin/env python3
"""Bounded stdlib HTTP collector for one public Douban movie subject.

Collection uses ordinary HTTP requests and server-rendered HTML. Manual login
is still user-operated and may use the shared browser-session window; the
collection path itself does not launch a browser.
"""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from china_media_crawler import douban_session
from china_media_crawler.authentication import get_session_provider
from china_media_crawler.http import CollectionError, fetch_html
from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Record
from china_media_crawler.privacy import pseudonym


DOUBAN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DOUBAN_TIMEZONE = timezone(timedelta(hours=8))
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
CHALLENGE_MARKERS = (
    "sec.douban.com",
    "accounts.douban.com/passport/login",
    "登录豆瓣",
    "请先登录",
    "异常请求",
    "验证码",
)


class DoubanCommentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.comment_depth: int | None = None
        self.current: dict[str, Any] | None = None
        self.captures: list[tuple[str, int]] = []
        self.rows: list[dict[str, Any]] = []
        self.saw_comments_container = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = values.get("class", "").split()
        if tag == "div" and values.get("id") == "comments":
            self.saw_comments_container = True
        if tag in VOID_TAGS:
            return
        self.depth += 1
        if self.comment_depth is None:
            if tag == "div" and "comment-item" in classes:
                self.comment_depth = self.depth
                self.current = {}
                self.captures = []
            return
        if self.current is None:
            return
        if tag == "a" and "comment-time" in classes:
            title = values.get("title", "").strip()
            if title:
                self.current["timestamp"] = title
            else:
                self.captures.append(("timestamp", self.depth))
                self.current.setdefault("timestamp", "")
        elif tag == "span" and "short" in classes:
            self.captures.append(("content", self.depth))
            self.current.setdefault("content", "")
        elif tag == "span" and "comment-location" in classes:
            self.captures.append(("ip_location", self.depth))
            self.current.setdefault("ip_location", "")
        elif tag == "span" and "votes" in classes and "vote-count" in classes:
            self.captures.append(("votes", self.depth))
            self.current.setdefault("votes", "")
        elif tag == "a" and "/people/" in values.get("href", ""):
            if "author" not in self.current:
                self.captures.append(("author", self.depth))
                self.current["author"] = ""
        elif tag == "span":
            for name in classes:
                if name.startswith("allstar") and name[7:].isdigit():
                    value = int(name[7:])
                    self.current["rating"] = int(value / 10) if value % 10 == 0 else value / 10
                    break

    def handle_data(self, data: str) -> None:
        if self.comment_depth is None or self.current is None:
            return
        for field, _ in self.captures:
            self.current[field] = str(self.current.get(field) or "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        if self.comment_depth is None:
            self.depth -= 1
            return
        self.captures = [(field, depth) for field, depth in self.captures
                         if depth != self.depth]
        if self.depth == self.comment_depth:
            if self.current is not None:
                self.rows.append(self.current)
            self.current = None
            self.comment_depth = None
        self.depth -= 1


def parse_comments(html: str) -> tuple[list[dict[str, Any]], bool]:
    parser = DoubanCommentParser()
    parser.feed(html)
    return parser.rows, parser.saw_comments_container


def validate_html(html: str) -> str:
    lowered = html.lower()
    if any(marker.lower() in lowered for marker in CHALLENGE_MARKERS):
        raise CollectionError("access_or_login_challenge")
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if title is None or not title.group(1).strip():
        raise CollectionError("unexpected_html")
    return re.sub(r"\s+", " ", title.group(1)).strip()


def validate_rows(value: Any) -> list[dict[str, Any]]:
    if (not isinstance(value, list) or len(value) > 20
            or any(not isinstance(row, dict) or not isinstance(row.get("content"), str)
                   for row in value)):
        raise CollectionError("unexpected_comment_schema")
    return value


def normalize_timestamp(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=DOUBAN_TIMEZONE)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return text


def normalize_votes(value: Any) -> int | None:
    if type(value) is int:
        return value
    text = str(value or "").strip().replace(",", "")
    return int(text) if text.isdigit() else None


def normalize_record(
    row: dict[str, Any], subject_id: str, surface: str, page: int,
    captured_at: str, salt: str,
) -> Record:
    content = str(row.get("content") or "").strip()
    timestamp = str(row.get("timestamp") or "").strip()
    published_at = normalize_timestamp(timestamp)
    digest = hashlib.sha256(
        f"{content}\x1f{published_at or timestamp}".encode("utf-8")
    ).hexdigest()[:24]
    votes = normalize_votes(row.get("votes"))
    return Record(
        platform="douban",
        surface=surface,
        object_id=f"{subject_id}:{digest}",
        content=content,
        published_at=published_at,
        captured_at=captured_at,
        author_pseudonym=pseudonym(str(row.get("author") or ""), salt),
        engagement={"votes": votes} if votes is not None else {},
        platform_data={
            "subject_id": subject_id,
            "page": page,
            "rating": row.get("rating"),
            "ip_location": str(row.get("ip_location") or "").strip() or None,
        },
    )


def failure_details(error: BaseException, stage: str) -> dict[str, Any]:
    details: dict[str, Any] = {"error_type": type(error).__name__, "failure_stage": stage}
    if isinstance(error, CollectionError):
        details["error_kind"] = error.kind
        if error.http_status is not None:
            details["http_status"] = error.http_status
        for attribute in ("redirect_host", "redirect_path_class",
                          "transport_error_type", "retry_after",
                          "rate_limit_limit", "rate_limit_remaining",
                          "rate_limit_reset"):
            value = getattr(error, attribute, None)
            if value is not None:
                details[attribute] = value
    elif isinstance(error, KeyboardInterrupt):
        details["error_kind"] = "cancelled"
    elif isinstance(error, TimeoutError):
        details["error_kind"] = "navigation_timeout"
    else:
        details["error_kind"] = "collection_failed"
    return details


def build_url(subject_id: str, surface: str, page: int) -> str:
    params: dict[str, str | int] = {
        "start": (page - 1) * 20,
        "limit": 20,
        "status": "F" if surface == "wantsee" else "P",
    }
    if surface == "reviews":
        params["sort"] = "new_score"
    return f"https://movie.douban.com/subject/{subject_id}/comments?{urlencode(params)}"


def fetch_page(url: str, cookie_header: str | None) -> tuple[int, str]:
    try:
        return fetch_html(
            url,
            referer="https://movie.douban.com/",
            cookie_header=cookie_header,
            credential_host="movie.douban.com",
            user_agent=DOUBAN_USER_AGENT,
        )
    except CollectionError as error:
        if error.kind == "unexpected_redirect":
            failure = CollectionError("access_or_login_challenge",
                                      http_status=error.http_status)
            for attribute in ("redirect_host", "redirect_path_class",
                              "transport_error_type", "retry_after",
                              "rate_limit_limit", "rate_limit_remaining",
                              "rate_limit_reset"):
                setattr(failure, attribute, getattr(error, attribute, None))
            raise failure from None
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--surface", choices=("reviews", "wantsee"), default="reviews")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--max-items", type=int, default=20, help="global unique review export cap")
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-delay", type=float, default=2.5)
    parser.add_argument("--max-delay", type=float, default=5.0)
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"\d+", args.subject_id):
        raise SystemExit("subject ID must contain digits only")
    if not 1 <= args.max_pages <= 5:
        raise SystemExit("max pages must be between 1 and 5")
    if not 1 <= args.max_items <= 200:
        raise SystemExit("max items must be between 1 and 200")
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
    subject_url = f"https://movie.douban.com/subject/{args.subject_id}/"
    manifest: dict[str, Any] = {
        "schema_version": "0.2",
        "platform": "douban",
        "transport": "http",
        "status": "running",
        "subject_id": args.subject_id,
        "surface": args.surface,
        "max_pages": args.max_pages,
        "max_items": args.max_items,
        "started_at": utc_now(),
        "cookie_used": False,
        "session_source": "pending",
        "pseudonymized": bool(salt),
        "pages": [],
        "record_count": 0,
        "duplicate_count": 0,
    }
    write_manifest(manifest_path, manifest)

    seen: set[tuple[str, str]] = set()
    stage = "session_load"
    try:
        session_source, saved_state = get_session_provider("douban").collection_state(
            args.session, cookie_value)
        manifest["session_source"] = session_source
        header = None
        if saved_state is not None:
            header = douban_session.cookie_header(saved_state, subject_url)
            if not header or not any(pair.startswith("dbcl2=") for pair in header.split("; ")):
                raise CollectionError("session_scope_invalid")
            manifest["cookie_used"] = True
        write_manifest(manifest_path, manifest)

        stage = "subject_page"
        status, html = fetch_page(subject_url, header)
        manifest["subject_page_title"] = validate_html(html)
        manifest["subject_page_url"] = subject_url
        write_manifest(manifest_path, manifest)

        stage = "output_write"
        manifest["termination_reason"] = "page_budget"
        with data_path.open("x", encoding="utf-8") as output_file:
            for page_number in range(1, args.max_pages + 1):
                if page_number > 1:
                    time.sleep(random.uniform(args.min_delay, args.max_delay))
                captured_at = utc_now()
                stage = "comments_page"
                status, html = fetch_page(
                    build_url(args.subject_id, args.surface, page_number), header)
                validate_html(html)

                stage = "comments_extract"
                rows, saw_container = parse_comments(html)
                if not rows and not saw_container:
                    raise CollectionError("missing_comments_container")
                rows = validate_rows(rows)
                accepted = 0
                for row in rows:
                    content = str(row.get("content") or "").strip()
                    timestamp = str(row.get("timestamp") or "").strip()
                    if not content:
                        continue
                    key = (content, normalize_timestamp(timestamp) or timestamp)
                    if key in seen:
                        manifest["duplicate_count"] += 1
                        continue
                    seen.add(key)
                    stage = "comments_extract"
                    record = normalize_record(
                        row, args.subject_id, args.surface, page_number, captured_at, salt,
                    ).to_dict()
                    stage = "output_write"
                    output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    output_file.flush()
                    accepted += 1
                    manifest["record_count"] += 1
                    if manifest["record_count"] >= args.max_items:
                        manifest["termination_reason"] = "max_items"
                        break
                manifest["pages"].append({
                    "page": page_number,
                    "http_status": status,
                    "returned": len(rows),
                    "accepted": accepted,
                    "captured_at": captured_at,
                })
                write_manifest(manifest_path, manifest)
                if manifest["record_count"] >= args.max_items:
                    break
                if not rows:
                    manifest["termination_reason"] = "empty_page"
                    break

        manifest.update({"status": "complete", "finished_at": utc_now()})
        write_manifest(manifest_path, manifest)
        return 0
    except BaseException as error:
        manifest.update({
            "status": "cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
            "finished_at": utc_now(),
            **failure_details(error, stage),
        })
        manifest.pop("termination_reason", None)
        if isinstance(error, CollectionError) and error.kind in {
            "access_or_login_challenge", "login_required", "session_invalid",
            "session_expired", "session_scope_invalid",
        }:
            manifest["login_action_required"] = True
            manifest["next_action"] = "cmc login --platform douban"
            print("采集已停止。请运行 cmc login --platform douban，在专用浏览器中确认登录与正常访问后再启动新一轮采集。",
                  file=sys.stderr)
        write_manifest(manifest_path, manifest)
        if isinstance(error, CollectionError):
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
