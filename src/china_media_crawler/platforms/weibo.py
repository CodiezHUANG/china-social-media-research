"""Independent Weibo hot-list, keyword-search, and first-level comment samples."""
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
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from china_media_crawler import __version__, weibo_session
from china_media_crawler.authentication import get_session_provider
from china_media_crawler.configuration import clean_text, read_ids
from china_media_crawler.http import CollectionError, fetch_json
from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Plan, Record, Step
from china_media_crawler.privacy import pseudonym
from china_media_crawler.text import plain_text

HOT_URL = "https://weibo.com/ajax/side/hotSearch"
COMMENTS_URL = "https://m.weibo.cn/comments/hotflow"
SEARCH_URL = "https://m.weibo.cn/api/container/getIndex"
VISITOR_HOME = "https://m.weibo.cn/"
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def close_safely(resource: Any) -> None:
    try:
        resource.close()
    except Exception:
        pass


def visitor_cookie_header(timeout_seconds: int = 20, playwright_factory: Callable | None = None) -> str:
    """Obtain the site's own anonymous visitor cookies in a bounded browser.

    No user login, credential file, CAPTCHA handling or retry is involved. The
    returned header is held in memory only and is scoped to m.weibo.cn.
    """
    if not 5 <= timeout_seconds <= 60:
        raise ValueError("visitor session timeout must be between 5 and 60 seconds")
    if playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise CollectionError("missing_playwright") from None
        playwright_factory = sync_playwright
    with playwright_factory() as driver, ExitStack() as cleanup:
        browser = driver.chromium.launch(headless=True)
        cleanup.callback(close_safely, browser)
        context = browser.new_context(locale="zh-CN", user_agent=MOBILE_USER_AGENT)
        cleanup.callback(close_safely, context)
        page = context.new_page()
        response = page.goto(VISITOR_HOME, wait_until="domcontentloaded", timeout=30_000)
        if response is None or response.status >= 400:
            raise CollectionError("visitor_navigation_failed")
        deadline = time.monotonic() + timeout_seconds
        cookies: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            if page.is_closed():
                raise CollectionError("visitor_session_cancelled")
            cookies = context.cookies()
            if any(cookie.get("name") in {"SUB", "SUBP"} and cookie.get("value")
                   for cookie in cookies):
                break
            page.wait_for_timeout(500)
        else:
            raise CollectionError("visitor_session_unavailable")
        return cookie_header_from(cookies)


def cookie_header_from(cookies: list[dict[str, Any]]) -> str:
    now = time.time()
    pairs = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise CollectionError("visitor_session_invalid")
        name, value = cookie.get("name"), cookie.get("value")
        domain = str(cookie.get("domain", "")).lower().lstrip(".")
        if not isinstance(name, str) or not isinstance(value, str):
            raise CollectionError("visitor_session_invalid")
        if not (domain == "weibo.cn" or domain.endswith(".weibo.cn")):
            continue
        expires = cookie.get("expires", -1)
        if type(expires) not in (int, float) or not (expires == -1 or expires > now):
            continue
        if (not COOKIE_NAME.fullmatch(name) or not value.isascii()
                or any(ord(char) < 32 or char in ';\x7f' for char in value)):
            raise CollectionError("visitor_session_invalid")
        pairs.append(name + "=" + value)
    if not any(pair.startswith(("SUB=", "SUBP=")) for pair in pairs):
        raise CollectionError("visitor_session_unavailable")
    return "; ".join(pairs)


class PublicClient:
    def __init__(self, fetch: Callable | None = None, sleep: Callable = time.sleep,
                 cookie_header: str | None = None, min_delay: float = 2.5,
                 max_delay: float = 5.0) -> None:
        self.fetch = fetch or fetch_json
        self.sleep = sleep
        self.cookie_header = cookie_header
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.requests = 0

    def get(self, url: str, *, mobile: bool = False) -> Any:
        if self.requests:
            self.sleep(random.uniform(self.min_delay, self.max_delay))
        self.requests += 1
        referer = "https://m.weibo.cn/" if mobile else "https://weibo.com/"
        if mobile:
            if self.cookie_header:
                return self.fetch(url, referer=referer, cookie_header=self.cookie_header,
                                  credential_host="m.weibo.cn", user_agent=MOBILE_USER_AGENT)
            return self.fetch(url, referer=referer, user_agent=MOBILE_USER_AGENT)
        return self.fetch(url, referer=referer)


def numeric_id(value: Any, *, allow_zero: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CollectionError("unexpected_identifier")
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 30:
        raise CollectionError("unexpected_identifier")
    if not allow_zero and int(text) == 0:
        raise CollectionError("unexpected_identifier")
    return str(int(text))


def response_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or type(payload.get("ok")) is not int:
        raise CollectionError("unexpected_schema")
    if payload["ok"] != 1:
        raise CollectionError("api_error", api_code=payload["ok"])
    if not isinstance(payload.get("data"), dict):
        raise CollectionError("unexpected_schema")
    return payload["data"]


def hot_records(payload: Any, captured_at: str) -> list[Record]:
    rows = response_data(payload).get("realtime")
    if not isinstance(rows, list) or len(rows) > 200:
        raise CollectionError("unexpected_hot_schema")
    records = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("word"), str) or not row["word"].strip():
            raise CollectionError("unexpected_hot_schema")
        word = row["word"].strip()
        records.append(Record(
            platform="weibo", surface="hot", object_id=hashlib.sha256(word.encode()).hexdigest()[:24],
            content=word, published_at=None, captured_at=captured_at,
            engagement={"heat": row["num"]} if type(row.get("num")) is int else {},
            platform_data={"rank": index, "source_rank": row.get("rank")
                           if type(row.get("rank")) is int else None,
                           "is_ad": row.get("is_ad") in (1, True),
                           "label": row.get("label_name") if isinstance(row.get("label_name"), str) else None},
        ))
    return records


def weibo_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise CollectionError("unexpected_timestamp")
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc).isoformat()
    except ValueError as error:
        raise CollectionError("unexpected_timestamp") from error


def comment_records(payload: Any, post_id: str, captured_at: str, salt: str) -> tuple:
    data = response_data(payload)
    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) > 200:
        raise CollectionError("unexpected_comment_schema")
    cursor = numeric_id(data.get("max_id"), allow_zero=True)
    cursor_type = data.get("max_id_type", 0)
    if type(cursor_type) is not int or cursor_type not in range(4):
        raise CollectionError("unexpected_cursor_type")
    records = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise CollectionError("unexpected_comment_schema")
        user = row.get("user")
        if not isinstance(user, dict):
            raise CollectionError("unexpected_comment_schema")
        records.append(Record(
            platform="weibo", surface="comments", object_id=numeric_id(row.get("id")),
            content=plain_text(row["text"]), published_at=weibo_timestamp(row.get("created_at")),
            captured_at=captured_at,
            author_pseudonym=pseudonym(numeric_id(user.get("id")), salt),
            engagement={"likes": row["like_count"]} if type(row.get("like_count")) is int else {},
            platform_data={"post_id": post_id, "level": 1},
        ))
    if not rows and cursor != "0":
        raise CollectionError("unexpected_empty_comment_page")
    return records, cursor, cursor_type


def search_records(payload: Any, query: str, page: int, captured_at: str,
                   salt: str) -> tuple[list[Record], int | None]:
    data = response_data(payload)
    cards = data.get("cards")
    if not isinstance(cards, list) or len(cards) > 200:
        raise CollectionError("unexpected_search_schema")
    info = data.get("cardlistInfo")
    next_page = None
    if info is not None:
        if not isinstance(info, dict):
            raise CollectionError("unexpected_search_schema")
        next_page = info.get("page")
        if next_page is not None:
            if type(next_page) is not int or next_page < 0:
                raise CollectionError("unexpected_search_page")
            if next_page == 0:
                next_page = None
    mblogs = []
    for card in cards:
        if not isinstance(card, dict):
            raise CollectionError("unexpected_search_schema")
        candidates = [card]
        group = card.get("card_group")
        if group is not None:
            if not isinstance(group, list):
                raise CollectionError("unexpected_search_schema")
            candidates.extend(group)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise CollectionError("unexpected_search_schema")
            mblog = candidate.get("mblog")
            if mblog is not None:
                mblogs.append(mblog)
    if len(mblogs) > 200:
        raise CollectionError("unexpected_search_schema")
    records = []
    for rank, mblog in enumerate(mblogs, 1):
        if not isinstance(mblog, dict) or not isinstance(mblog.get("text"), str):
            raise CollectionError("unexpected_search_schema")
        user = mblog.get("user")
        if not isinstance(user, dict):
            raise CollectionError("unexpected_search_schema")
        engagement = {}
        for field, label in (("reposts_count", "reposts"), ("comments_count", "comments"),
                             ("attitudes_count", "likes")):
            if type(mblog.get(field)) is int:
                engagement[label] = mblog[field]
        records.append(Record(
            platform="weibo", surface="search", object_id=numeric_id(mblog.get("id")),
            content=plain_text(mblog["text"]), published_at=weibo_timestamp(mblog.get("created_at")),
            captured_at=captured_at,
            author_pseudonym=pseudonym(numeric_id(user.get("id")), salt),
            engagement=engagement,
            platform_data={"query": query, "page": page, "rank": rank,
                           "is_retweeted": isinstance(mblog.get("retweeted_status"), dict)},
        ))
    return records, next_page


def collect_weibo(
    output: Path, *, surface: str, ids: list[str] | None = None,
    queries: list[str] | None = None, max_items: int = 20,
    comments_per_item: int = 20, max_pages: int = 5, salt: str = "", client: PublicClient | None = None,
    visitor_session: bool = False, session_mode: str = "auto",
) -> int:
    if surface not in {"hot", "comments", "search"} or not 1 <= max_items <= 200:
        raise ValueError("invalid Weibo surface or limits")
    page_limit = 50 if surface in {"comments", "search"} else 5
    if not 1 <= max_pages <= page_limit:
        raise ValueError(f"max pages must be between 1 and {page_limit}")
    if session_mode not in {"auto", "required", "off"}:
        raise ValueError("unsupported session mode")
    if session_mode != "auto" and surface == "hot":
        raise ValueError("Weibo sessions are only valid for comments and search")
    if visitor_session and surface == "hot":
        raise ValueError("visitor session is only valid for Weibo comments and search")
    comment_limit = 1000 if surface == "comments" else 100
    if not 1 <= comments_per_item <= comment_limit:
        raise ValueError(f"comments-per-item must be 1..{comment_limit}")
    if surface == "comments" and (not salt.strip() or not ids):
        raise ValueError("comments require public post IDs and WEIBO_PSEUDONYM_SALT")
    if surface == "search" and (not salt.strip() or not queries):
        raise ValueError("search requires at least one query and WEIBO_PSEUDONYM_SALT")
    ids = list(dict.fromkeys(numeric_id(value) for value in (ids or [])))[:max_items]
    queries = list(dict.fromkeys(clean_text(value, "query") for value in (queries or [])))[:10]
    client = client or PublicClient(
        min_delay=8.0 if surface == "search" else 2.5,
        max_delay=12.0 if surface == "search" else 5.0,
    )
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(path.name != "run_manifest.json" for path in output.iterdir()):
        raise ValueError("choose a new output directory")
    manifest_path = output / "weibo_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "0.2", "tool_version": __version__, "platform": "weibo",
        "surface": surface, "status": "running", "cookie_used": False,
        "visitor_session": bool(visitor_session), "session_source": "pending",
        "started_at": utc_now(), "record_count": 0, "objects": [],
        "limits": {"items": max_items, "comments_per_item": comments_per_item,
                   "pages": max_pages, "queries": len(queries)},
    }
    write_manifest(manifest_path, manifest)
    progress = None
    try:
        if surface in {"comments", "search"}:
            source, state = get_session_provider("weibo").collection_state(
                session_mode, os.getenv("WEIBO_COOKIE", "").strip())
            manifest.update(session_source=source, cookie_used=source in {"saved", "cookie"})
            if state is not None:
                manifest["visitor_session"] = False
                endpoint = SEARCH_URL if surface == "search" else COMMENTS_URL
                client.cookie_header = weibo_session.cookie_header(state, endpoint)
            elif visitor_session:
                manifest["visitor_session"] = True
                client.cookie_header = visitor_cookie_header()
                manifest["session_source"] = "visitor"
        else:
            manifest.update(session_source="anonymous")
        write_manifest(manifest_path, manifest)
        with (output / "records.jsonl").open("x", encoding="utf-8") as stream:
            def emit(record: Record) -> None:
                stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                stream.flush()
                manifest["record_count"] += 1

            if surface == "hot":
                records = hot_records(client.get(HOT_URL), utc_now())
                seen: set[str] = set()
                for record in records:
                    if record.object_id in seen:
                        continue
                    if len(seen) >= max_items:
                        break
                    emit(record)
                    seen.add(record.object_id)
                manifest["returned"] = len(records)
                manifest["termination_reason"] = "item_limit" if len(seen) >= max_items else "snapshot_end"
            elif surface == "comments":
                for post_id in ids:
                    progress = {"post_id": post_id, "pages": [], "records": 0, "termination_reason": "page_limit"}
                    seen, cursors = set(), {"0"}
                    cursor, cursor_type = "0", 0
                    for page in range(1, max_pages + 1):
                        params = {"id": post_id, "mid": post_id, "max_id": cursor}
                        if cursor != "0":
                            params["max_id_type"] = cursor_type
                        records, next_cursor, cursor_type = comment_records(
                            client.get(COMMENTS_URL + "?" + urlencode(params), mobile=True),
                            post_id, utc_now(), salt,
                        )
                        accepted = duplicates = 0
                        for record in records:
                            if record.object_id in seen:
                                duplicates += 1
                                continue
                            if len(seen) >= comments_per_item:
                                break
                            emit(record)
                            seen.add(record.object_id)
                            accepted += 1
                        progress["records"] = len(seen)
                        progress["pages"].append({"page": page, "returned": len(records),
                                                  "accepted": accepted, "duplicates": duplicates})
                        if len(seen) >= comments_per_item:
                            progress["termination_reason"] = "item_limit"
                            break
                        if next_cursor == "0":
                            progress["termination_reason"] = "cursor_end"
                            break
                        if next_cursor in cursors:
                            raise CollectionError("repeated_cursor")
                        cursors.add(next_cursor)
                        cursor = next_cursor
                        manifest["http_request_count"] = client.requests
                        write_manifest(manifest_path, manifest)
                    manifest["objects"].append(progress)
                    progress = None
                    write_manifest(manifest_path, manifest)
            else:
                for query in queries:
                    progress = {"query": query, "pages": [], "records": 0, "returned": 0,
                                "termination_reason": "page_limit"}
                    seen: set[str] = set()
                    visited: set[int] = set()
                    page = 1
                    while page <= max_pages:
                        visited.add(page)
                        params = {"containerid": "100103type=1&q=" + query,
                                  "page_type": "searchall", "page": page}
                        records, next_page = search_records(
                            client.get(SEARCH_URL + "?" + urlencode(params), mobile=True),
                            query, page, utc_now(), salt,
                        )
                        accepted = duplicates = 0
                        for record in records:
                            if record.object_id in seen:
                                duplicates += 1
                                continue
                            if len(seen) >= max_items:
                                break
                            emit(record)
                            seen.add(record.object_id)
                            accepted += 1
                        progress["records"] = len(seen)
                        progress["returned"] += len(records)
                        progress["pages"].append({
                            "page": page, "returned": len(records), "accepted": accepted,
                            "duplicates": duplicates, "next_page": next_page,
                        })
                        if len(seen) >= max_items:
                            progress["termination_reason"] = "item_limit"
                            break
                        if next_page is None or (accepted == 0 and records):
                            progress["termination_reason"] = "page_end"
                            break
                        if next_page <= page or next_page in visited:
                            raise CollectionError("repeated_search_page")
                        if next_page > max_pages:
                            progress["termination_reason"] = "page_limit"
                            break
                        page = next_page
                        manifest["http_request_count"] = client.requests
                        write_manifest(manifest_path, manifest)
                    manifest["objects"].append(progress)
                    progress = None
                    write_manifest(manifest_path, manifest)
        manifest.update(status="complete", finished_at=utc_now(), http_request_count=client.requests)
        write_manifest(manifest_path, manifest)
        return 0
    except BaseException as error:
        manifest.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                        finished_at=utc_now(), http_request_count=client.requests,
                        error_type=type(error).__name__)
        if progress is not None:
            manifest["partial_object"] = progress
        if isinstance(error, CollectionError):
            manifest.update(error_kind=error.kind, api_code=error.api_code, http_status=error.http_status,
                            transport_error_type=error.transport_error_type)
            for attribute in ("retry_after", "rate_limit_limit",
                              "rate_limit_remaining", "rate_limit_reset"):
                value = getattr(error, attribute, None)
                if value is not None:
                    manifest[attribute] = value
            if error.kind == "unexpected_redirect":
                manifest.update(redirect_host=error.redirect_host,
                                redirect_path_class=error.redirect_path_class)
        write_manifest(manifest_path, manifest)
        if isinstance(error, CollectionError):
            return 2
        raise


def weibo_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    ids = read_ids(args) if surface == "comments" else []
    if surface == "comments" and not ids:
        raise ValueError("Weibo comments require --item-id or --input-file")
    queries = []
    if surface == "search":
        queries = list(dict.fromkeys(
            clean_text(value, "query") for value in getattr(args, "query", [])))[:10]
        if not queries:
            raise ValueError("Weibo search requires --query")
    try:
        ids = [numeric_id(value) for value in ids][:args.max_items]
    except CollectionError as error:
        raise ValueError("Weibo post IDs must be positive ASCII digits") from error
    script = Path(__file__).resolve()
    command = [sys.executable, str(script), "--surface", surface, "--output", str(output),
               "--max-items", str(args.max_items), "--comments-per-item", str(max(1, args.comments_per_item)),
               "--max-pages", str(args.max_pages)]
    if getattr(args, "visitor_session", False):
        if surface not in {"comments", "search"}:
            raise ValueError("--visitor-session is only valid for Weibo comments and search")
        command.append("--visitor-session")
    if surface in {"comments", "search"}:
        command += ["--session", getattr(args, "session", "auto")]
    for query in queries:
        command += ["--query", query]
    for post_id in ids:
        command += ["--item-id", post_id]
    required = ["WEIBO_PSEUDONYM_SALT"] if surface in {"comments", "search"} else []
    pass_environment = required + (["WEIBO_COOKIE"] if surface in {"comments", "search"} else [])
    return Plan(created_at=utc_now(), platform="weibo", surface=surface, output=str(output),
                limits={"items": args.max_items, "comments_per_item": args.comments_per_item,
                        "pages": args.max_pages, "queries": len(queries)},
                required_environment=required,
                steps=[Step("Weibo " + surface, command, str(script.parent),
                            pass_environment=pass_environment)], timeout_seconds=args.timeout_seconds,
                backend="weibo-public")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("hot", "comments", "search"), required=True)
    parser.add_argument("--item-id", action="append", default=[])
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--comments-per-item", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--visitor-session", action="store_true")
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    args = parser.parse_args(argv)
    try:
        return collect_weibo(Path(args.output), surface=args.surface, ids=args.item_id,
                             queries=args.query, max_items=args.max_items,
                             comments_per_item=args.comments_per_item, max_pages=args.max_pages,
                             salt=os.getenv("WEIBO_PSEUDONYM_SALT", ""),
                             visitor_session=args.visitor_session, session_mode=args.session)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
