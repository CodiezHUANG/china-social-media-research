"""Bounded collection from Bilibili's public video search endpoint."""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import build_opener

from china_media_crawler import __version__
from china_media_crawler.http import (
    CollectionError, fetch_json as transport_fetch_json,
)
from china_media_crawler.configuration import clean_text
from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Plan, Record, Step
from china_media_crawler.privacy import pseudonym
from china_media_crawler.text import plain_text
from china_media_crawler.authentication import get_session_provider
from china_media_crawler.bilibili_session import cookie_header as session_cookie


SEARCH_ENDPOINT = "https://api.bilibili.com/x/web-interface/search/type"
PAGE_SIZE = 20
BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}\Z")
ORDERS = ("totalrank", "click", "pubdate")


def build_url(query: str, page: int, order: str) -> str:
    return SEARCH_ENDPOINT + "?" + urlencode({
        "search_type": "video", "keyword": query, "page": page,
        "page_size": PAGE_SIZE, "order": order,
    })


def fetch_json(url: str, *, cookie_header: str | None = None) -> dict[str, Any]:
    return transport_fetch_json(
        url, referer="https://search.bilibili.com/", opener_factory=build_opener,
        cookie_header=cookie_header, credential_host="api.bilibili.com",
    )


def search_rows(payload: dict[str, Any], page: int) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise CollectionError("unexpected_schema")
    code = payload["code"]
    if code != 0:
        raise CollectionError("api_error", api_code=code)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CollectionError("unexpected_schema")
    if data.get("v_voucher") or payload.get("v_voucher"):
        raise CollectionError("access_challenge")
    if "page" in data and (type(data["page"]) is not int or data["page"] != page):
        raise CollectionError("unexpected_page")
    rows = data.get("result")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CollectionError("unexpected_schema")
    if len(rows) > PAGE_SIZE:
        raise CollectionError("unexpected_page_size")
    page_count = data.get("numPages")
    if page_count is not None and (type(page_count) is not int or page_count < 0):
        raise CollectionError("unexpected_schema")
    if page_count == 0 and rows:
        raise CollectionError("unexpected_page")
    return rows, page_count


def normalize_record(
    row: dict[str, Any], query: str, order: str, page: int,
    position: int, captured_at: str, salt: str,
) -> Record:
    if row.get("type") not in (None, "video"):
        raise CollectionError("unexpected_result_type")
    bvid = row.get("bvid")
    title = row.get("title")
    if not isinstance(bvid, str) or not BVID_PATTERN.fullmatch(bvid):
        raise CollectionError("unexpected_video_id")
    if not isinstance(title, str) or not plain_text(title):
        raise CollectionError("unexpected_video_title")
    description = row.get("description", "")
    if not isinstance(description, str):
        raise CollectionError("unexpected_schema")
    published_at = None
    pubdate = row.get("pubdate")
    if pubdate is not None:
        if type(pubdate) is not int or pubdate < 0:
            raise CollectionError("unexpected_publication_time")
        try:
            published_at = datetime.fromtimestamp(pubdate, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError) as error:
            raise CollectionError("unexpected_publication_time") from error
    mid = row.get("mid")
    if mid is not None and (
        isinstance(mid, bool) or not isinstance(mid, (int, str))
        or not str(mid).isdigit()
    ):
        raise CollectionError("unexpected_author_id")
    return Record(
        platform="bilibili", surface="search", object_id=bvid,
        content=plain_text(title), published_at=published_at, captured_at=captured_at,
        author_pseudonym=pseudonym(str(mid), salt) if mid is not None else None,
        engagement={
            name: row.get(source)
            for name, source in (
                ("views", "play"), ("favorites", "favorites"),
                ("comments", "review"), ("danmaku", "video_review"),
            )
            if type(row.get(source)) in {int, str}
        },
        platform_data={
            "title": plain_text(title), "description": plain_text(description),
            "url": f"https://www.bilibili.com/video/{bvid}",
            "duration": row.get("duration") if isinstance(row.get("duration"), str) else None,
            "query": query, "search_order": order, "search_page": page,
            "search_position": position,
        },
    )


def validate_limits(max_items: int, max_pages: int, min_delay: float, max_delay: float) -> None:
    if not 1 <= max_items <= 200 or not 1 <= max_pages <= 5:
        raise ValueError("items must be 1..200 and pages must be 1..5")
    if (
        not math.isfinite(min_delay) or not math.isfinite(max_delay)
        or not 2.5 <= min_delay <= max_delay <= 60
    ):
        raise ValueError("delays must satisfy 2.5 <= min-delay <= max-delay <= 60")


def collect_search(
    query: str, output: Path, *, max_items: int = 20, max_pages: int = 5,
    order: str = "totalrank", salt: str, min_delay: float = 2.5, max_delay: float = 5.0,
    fetch: Callable[[str], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    session_mode: str = "off",
) -> int:
    query = clean_text(query, "query")
    validate_limits(max_items, max_pages, min_delay, max_delay)
    if order not in ORDERS:
        raise ValueError("unsupported search order")
    if not salt.strip():
        raise ValueError("BILI_PSEUDONYM_SALT is required")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = {
        "records": output / "records.jsonl", "frame": output / "bv_candidates.txt",
        "manifest": output / "bilibili_manifest.json",
    }
    if any(path.exists() for path in files.values()):
        raise ValueError("refusing to overwrite an existing Bilibili search run")
    if any(path.name != "run_manifest.json" for path in output.iterdir()):
        raise ValueError("output contains unrelated files; choose a new run directory")
    manifest: dict[str, Any] = {
        "schema_version": "0.2", "tool_version": __version__, "platform": "bilibili",
        "surface": "search", "endpoint": SEARCH_ENDPOINT,
        "query": query, "search_order": order, "status": "running",
        "started_at": utc_now(), "limits": {"items": max_items, "pages": max_pages},
        "page_size": PAGE_SIZE, "cookie_used": False, "session_source": "pending", "pseudonymized": True,
        "record_count": 0, "duplicate_count": 0, "excluded_count": 0, "pages": [],
    }
    write_manifest(files["manifest"], manifest)
    seen: set[str] = set()
    current_page = 1
    stage, requests = "session_load", 0
    try:
        source, state = get_session_provider("bilibili").collection_state(
            session_mode, os.getenv("BILIBILI_COOKIE", "").strip())
        manifest.update(session_source=source, cookie_used=source != "anonymous")
        fetch = fetch or (lambda url: fetch_json(url, cookie_header=session_cookie(state, url)))
        write_manifest(files["manifest"], manifest)
        with files["records"].open("x", encoding="utf-8") as records:
            with files["frame"].open("x", encoding="utf-8") as frame:
                reason = "page_limit"
                for current_page in range(1, max_pages + 1):
                    if current_page > 1:
                        sleep(random.uniform(min_delay, max_delay))
                    stage = "search_page"
                    requests += 1
                    payload = fetch(build_url(query, current_page, order))
                    rows, total_pages = search_rows(payload, current_page)
                    captured_at = utc_now()
                    accepted = duplicates = 0
                    # Validate the full page before exporting any of this page's records.
                    page_records = []
                    excluded = []
                    for index, row in enumerate(rows, start=1):
                        try:
                            if row.get("type") == "ketang":
                                excluded.append({
                                    "position": index, "type": "ketang",
                                    "reason": "outside_video_sampling_frame",
                                })
                                continue
                            page_records.append(normalize_record(
                                row, query, order, current_page, index, captured_at, salt,
                            ))
                        except CollectionError as error:
                            error.record_position = index
                            error.record_fields = sorted(row)
                            raise
                    for record in page_records:
                        if record.object_id in seen:
                            duplicates += 1
                            continue
                        if len(seen) >= max_items:
                            break
                        records.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                        records.flush()
                        frame.write(record.object_id + "\n")
                        frame.flush()
                        seen.add(record.object_id)
                        accepted += 1
                    manifest["pages"].append({
                        "page": current_page, "http_status": 200, "returned": len(rows),
                        "accepted": accepted, "duplicates": duplicates,
                        "eligible": len(page_records), "excluded": excluded,
                        "not_exported": len(page_records) - accepted - duplicates,
                        "reported_pages": total_pages, "captured_at": captured_at,
                    })
                    manifest["record_count"] = len(seen)
                    manifest["duplicate_count"] += duplicates
                    manifest["excluded_count"] += len(excluded)
                    write_manifest(files["manifest"], manifest)
                    if len(seen) >= max_items:
                        reason = "item_limit"
                        break
                    if not rows:
                        reason = "empty_page"
                        break
                    if total_pages is not None and current_page >= total_pages:
                        reason = "reported_end"
                        break
        manifest.update(status="complete", finished_at=utc_now(), termination_reason=reason,
                        http_request_count=requests)
        write_manifest(files["manifest"], manifest)
        return 0
    except BaseException as error:
        manifest.update(
            status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
            finished_at=utc_now(), record_count=len(seen), failed_page=current_page,
            error_type=type(error).__name__,
            failure_stage=stage, http_request_count=requests,
        )
        if isinstance(error, CollectionError):
            manifest.update(
                error_kind=error.kind, http_status=error.http_status, api_code=error.api_code,
                failed_record_position=error.record_position,
                failed_record_fields=error.record_fields,
                transport_error_type=error.transport_error_type,
            )
            if error.kind in {"login_required", "session_invalid", "session_expired", "session_scope_invalid", "cookie_invalid", "access_challenge"} or error.api_code == -101:
                manifest.update(login_action_required=True, next_action="cmc login --platform bilibili")
        write_manifest(files["manifest"], manifest)
        if isinstance(error, CollectionError):
            return 2
        raise


def search_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    if surface != "search" or len(args.query) != 1:
        raise ValueError("Bilibili search requires exactly one --query")
    query = clean_text(args.query[0], "query")
    script = Path(__file__).resolve()
    return Plan(
        created_at=utc_now(), platform="bilibili", surface="search", output=str(output),
        limits={"items": args.max_items, "pages": args.max_pages},
        required_environment=["BILI_PSEUDONYM_SALT"],
        steps=[Step(
            "Bilibili search", [
                sys.executable, str(script), "--query", query, "--output", str(output),
                "--max-items", str(args.max_items), "--max-pages", str(args.max_pages),
                "--session", getattr(args, "session", "auto"),
            ], str(script.parent), pass_environment=["BILI_PSEUDONYM_SALT"] + (
                ["BILIBILI_COOKIE"] if getattr(args, "session", "auto") != "off" else []),
        )],
        timeout_seconds=args.timeout_seconds,
        backend="bilibili-public-search",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--order", choices=ORDERS, default="totalrank")
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    args = parser.parse_args(argv)
    try:
        return collect_search(
            args.query, Path(args.output), max_items=args.max_items, max_pages=args.max_pages,
            order=args.order, salt=os.getenv("BILI_PSEUDONYM_SALT", ""),
            session_mode=args.session,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
