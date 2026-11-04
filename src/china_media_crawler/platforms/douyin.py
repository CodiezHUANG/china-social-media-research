"""Independent Douyin search (stdlib HTTP) and detail (Playwright) collection."""
from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import os
import random
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from china_media_crawler import __version__, douyin_session
from china_media_crawler.authentication import get_session_provider
from china_media_crawler.configuration import clean_text, read_ids
from china_media_crawler.http import CollectionError, fetch_json
from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Plan, Record, Step
from china_media_crawler.privacy import pseudonym


SEARCH_URL = "https://www.douyin.com/aweme/v1/web/general/search/single/"
DETAIL_MARKER = "/aweme/v1/web/aweme/detail/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
COMMON_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "version_code": "190600",
    "version_name": "19.6.0",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "cookie_enabled": "true",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "130.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "8",
    "device_memory": "8",
    "engine_version": "130.0.0.0",
    "platform": "PC",
    "screen_width": "1920",
    "screen_height": "1080",
    "effective_type": "4g",
    "round_trip_time": "50",
}


def numeric_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CollectionError("unexpected_identifier")
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 30:
        raise CollectionError("unexpected_identifier")
    if int(text) == 0:
        raise CollectionError("unexpected_identifier")
    return str(int(text))


def close_safely(resource: Any) -> None:
    try:
        resource.close()
    except Exception:
        pass


def search_records(payload: Any, query: str, page: int, offset: int,
                   captured_at: str, salt: str) -> tuple[list[Record], bool, str]:
    if not isinstance(payload, dict):
        raise CollectionError("unexpected_schema")
    status = payload.get("status_code")
    if status is not None and (type(status) is not int or status != 0):
        raise CollectionError("api_error", api_code=status if type(status) is int else None)
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) > 50:
        raise CollectionError("unexpected_search_schema")
    records = []
    for item in rows:
        if not isinstance(item, dict):
            raise CollectionError("unexpected_search_schema")
        aweme = item.get("aweme_info")
        if not isinstance(aweme, dict):
            mix = item.get("aweme_mix_info")
            mix_items = mix.get("mix_items") if isinstance(mix, dict) else None
            aweme = mix_items[0] if isinstance(mix_items, list) and mix_items else None
        if not isinstance(aweme, dict):
            continue
        desc = aweme.get("desc")
        if not isinstance(desc, str):
            raise CollectionError("unexpected_search_schema")
        author = aweme.get("author")
        if not isinstance(author, dict):
            raise CollectionError("unexpected_search_schema")
        author_id = author.get("uid") or author.get("sec_uid")
        if author_id is None:
            raise CollectionError("unexpected_search_schema")
        created = aweme.get("create_time")
        if created is not None and type(created) is not int:
            raise CollectionError("unexpected_timestamp")
        published_at = (datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                        if created is not None else None)
        statistics = aweme.get("statistics") if isinstance(aweme.get("statistics"), dict) else {}
        engagement = {}
        for field, label in (("digg_count", "likes"), ("comment_count", "comments"),
                             ("share_count", "shares"), ("collect_count", "collects")):
            if type(statistics.get(field)) is int:
                engagement[label] = statistics[field]
        video = aweme.get("video") if isinstance(aweme.get("video"), dict) else {}
        records.append(Record(
            platform="douyin", surface="search", object_id=numeric_id(aweme.get("aweme_id")),
            content=desc, published_at=published_at, captured_at=captured_at,
            author_pseudonym=pseudonym(str(author_id), salt), engagement=engagement,
            platform_data={
                "query": query, "page": page, "offset": offset,
                "aweme_type": aweme.get("aweme_type") if type(aweme.get("aweme_type")) is int else None,
                "duration": video.get("duration") if type(video.get("duration")) is int else None,
                "is_top": bool(aweme.get("is_top")),
            },
        ))
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    next_id = extra.get("logid") if isinstance(extra.get("logid"), str) else ""
    return records, bool(payload.get("has_more")), next_id


def detail_record(payload: Any, aweme_id: str, captured_at: str, salt: str) -> Record:
    detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        raise CollectionError("unexpected_detail_schema")
    desc = detail.get("desc")
    author = detail.get("author")
    if not isinstance(desc, str) or not isinstance(author, dict):
        raise CollectionError("unexpected_detail_schema")
    author_id = author.get("uid") or author.get("sec_uid")
    if author_id is None:
        raise CollectionError("unexpected_detail_schema")
    created = detail.get("create_time")
    if created is not None and type(created) is not int:
        raise CollectionError("unexpected_timestamp")
    published_at = (datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                    if created is not None else None)
    statistics = detail.get("statistics") if isinstance(detail.get("statistics"), dict) else {}
    engagement = {}
    for field, label in (("digg_count", "likes"), ("comment_count", "comments"),
                         ("share_count", "shares"), ("collect_count", "collects")):
        if type(statistics.get(field)) is int:
            engagement[label] = statistics[field]
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    return Record(
        platform="douyin", surface="detail", object_id=numeric_id(detail.get("aweme_id") or aweme_id),
        content=desc, published_at=published_at, captured_at=captured_at,
        author_pseudonym=pseudonym(str(author_id), salt), engagement=engagement,
        platform_data={
            "aweme_type": detail.get("aweme_type") if type(detail.get("aweme_type")) is int else None,
            "duration": video.get("duration") if type(video.get("duration")) is int else None,
        },
    )


def fetch_search(url: str, cookie_header: str) -> Any:
    return fetch_json(
        url,
        referer="https://www.douyin.com/",
        cookie_header=cookie_header,
        credential_host="www.douyin.com",
        user_agent=USER_AGENT,
    )


def collect_douyin(
    output: Path, *, surface: str, ids: list[str] | None = None,
    queries: list[str] | None = None, max_items: int = 20, max_pages: int = 5,
    salt: str = "", session_mode: str = "auto", headless: bool = True,
) -> int:
    if surface not in {"search", "detail"} or not 1 <= max_items <= 200:
        raise ValueError("invalid Douyin surface or limits")
    if not 1 <= max_pages <= 50:
        raise ValueError("max pages must be between 1 and 50")
    if not salt.strip():
        raise ValueError("DOUYIN_PSEUDONYM_SALT is required")
    queries = list(dict.fromkeys(clean_text(value, "query") for value in (queries or [])))[:10]
    ids = list(dict.fromkeys(numeric_id(value) for value in (ids or [])))[:max_items]
    if surface == "search" and not queries:
        raise ValueError("Douyin search requires --query")
    if surface == "detail" and not ids:
        raise ValueError("Douyin detail requires --item-id or --input-file")

    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(path.name != "run_manifest.json" for path in output.iterdir()):
        raise ValueError("choose a new output directory")
    manifest_path = output / "douyin_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "0.2", "tool_version": __version__, "platform": "douyin",
        "surface": surface, "transport": "http" if surface == "search" else "playwright",
        "status": "running", "cookie_used": False, "session_source": "pending",
        "started_at": utc_now(), "record_count": 0, "objects": [],
        "limits": {"items": max_items, "pages": max_pages, "queries": len(queries)},
    }
    write_manifest(manifest_path, manifest)
    progress = None
    try:
        source, state = get_session_provider("douyin").collection_state(
            session_mode, os.getenv("DOUYIN_COOKIE", "").strip())
        if state is None:
            raise CollectionError("login_required")
        manifest.update(session_source=source, cookie_used=source in {"saved", "cookie"})
        if surface == "search":
            ms_token = os.getenv("DOUYIN_MS_TOKEN", "").strip() or douyin_session.ms_token(state)
            if not ms_token:
                raise CollectionError("missing_ms_token")
            cookie_header = douyin_session.cookie_header(state, SEARCH_URL)
            if not cookie_header:
                raise CollectionError("session_scope_invalid")
            manifest["ms_token_used"] = True
            with (output / "records.jsonl").open("x", encoding="utf-8") as stream:
                def emit(record: Record) -> None:
                    stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                    stream.flush()
                    manifest["record_count"] += 1

                for query in queries:
                    progress = {"query": query, "pages": [], "records": 0,
                                "termination_reason": "page_limit"}
                    seen: set[str] = set()
                    offset, search_id, page = 0, "", 1
                    webid = str(random.randint(10**18, 10**19 - 1))
                    while page <= max_pages:
                        params = dict(COMMON_PARAMS)
                        params.update({
                            "search_channel": "aweme_general", "enable_history": "1",
                            "keyword": query, "search_source": "tab_search",
                            "query_correct_type": "1", "is_filter_search": "0",
                            "from_group_id": "7378810571505847586", "offset": str(offset),
                            "count": "15", "need_filter_settings": "1",
                            "list_type": "multi", "search_id": search_id,
                            "msToken": ms_token, "webid": webid,
                        })
                        payload = fetch_search(SEARCH_URL + "?" + urlencode(params), cookie_header)
                        records, has_more, next_id = search_records(
                            payload, query, page, offset, utc_now(), salt)
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
                        progress["pages"].append({"page": page, "offset": offset,
                                                  "returned": len(records),
                                                  "accepted": accepted, "duplicates": duplicates})
                        if len(seen) >= max_items:
                            progress["termination_reason"] = "item_limit"
                            break
                        if not records:
                            progress["termination_reason"] = "empty_page"
                            break
                        if not has_more:
                            progress["termination_reason"] = "has_more_end"
                            break
                        search_id = next_id
                        offset += 15
                        page += 1
                        manifest["http_request_count"] = page - 1
                        write_manifest(manifest_path, manifest)
                    manifest["objects"].append(progress)
                    progress = None
                    write_manifest(manifest_path, manifest)
        else:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise CollectionError("missing_playwright") from None
            manifest["session_source"] = source
            write_manifest(manifest_path, manifest)
            with sync_playwright() as driver, ExitStack() as cleanup:
                browser = driver.chromium.launch(headless=headless)
                cleanup.callback(close_safely, browser)
                context = browser.new_context(locale="zh-CN", storage_state=state)
                cleanup.callback(close_safely, context)
                page = context.new_page()
                for aweme_id in ids:
                    progress = {"aweme_id": aweme_id, "records": 0,
                                "termination_reason": "item_limit"}
                    responses: list[Any] = []
                    page.on("response", lambda response: responses.append(response)
                            if DETAIL_MARKER in response.url else None)
                    page.goto(f"https://www.douyin.com/video/{aweme_id}",
                              wait_until="domcontentloaded", timeout=30_000)
                    deadline = time.monotonic() + 20
                    payload = None
                    while time.monotonic() < deadline:
                        for response in responses:
                            try:
                                candidate = response.json()
                            except Exception:
                                continue
                            detail = candidate.get("aweme_detail") if isinstance(candidate, dict) else None
                            if (isinstance(detail, dict)
                                    and str(detail.get("aweme_id")) == aweme_id):
                                payload = candidate
                                break
                        if payload is not None:
                            break
                        page.wait_for_timeout(500)
                    if payload is None:
                        raise CollectionError("detail_response_missing")
                    record = detail_record(payload, aweme_id, utc_now(), salt)
                    with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                    manifest["record_count"] += 1
                    progress["records"] = 1
                    manifest["objects"].append(progress)
                    progress = None
                    write_manifest(manifest_path, manifest)
        manifest.update(status="complete", finished_at=utc_now(),
                        http_request_count=manifest.get("http_request_count", 0))
        write_manifest(manifest_path, manifest)
        return 0
    except BaseException as error:
        manifest.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                        finished_at=utc_now(),
                        error_type=type(error).__name__)
        if progress is not None:
            manifest["partial_object"] = progress
        if isinstance(error, CollectionError):
            manifest.update(error_kind=error.kind, api_code=error.api_code,
                            http_status=error.http_status,
                            transport_error_type=error.transport_error_type)
            if error.kind in {"login_required", "session_expired", "session_scope_invalid",
                              "missing_ms_token"}:
                manifest["login_action_required"] = True
                manifest["next_action"] = "cmc login --platform douyin"
        write_manifest(manifest_path, manifest)
        if isinstance(error, CollectionError):
            return 2
        raise


def douyin_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    ids = read_ids(args) if surface == "detail" else []
    queries = []
    if surface == "search":
        queries = list(dict.fromkeys(
            clean_text(value, "query") for value in getattr(args, "query", [])))[:10]
        if not queries:
            raise ValueError("Douyin search requires --query")
    try:
        ids = [numeric_id(value) for value in ids][:args.max_items]
    except CollectionError as error:
        raise ValueError("Douyin IDs must be positive ASCII digits") from error
    if surface == "detail" and not ids:
        raise ValueError("Douyin detail requires --item-id or --input-file")
    script = Path(__file__).resolve()
    command = [sys.executable, str(script), "--surface", surface, "--output", str(output),
               "--max-items", str(args.max_items), "--max-pages", str(args.max_pages),
               "--session", getattr(args, "session", "auto")]
    if surface == "detail":
        command.append("--headless")
    for query in queries:
        command += ["--query", query]
    for aweme_id in ids:
        command += ["--item-id", aweme_id]
    required = ["DOUYIN_PSEUDONYM_SALT"]
    pass_environment = required + ["DOUYIN_COOKIE", "DOUYIN_MS_TOKEN"]
    return Plan(created_at=utc_now(), platform="douyin", surface=surface, output=str(output),
                limits={"items": args.max_items, "pages": args.max_pages, "queries": len(queries)},
                required_environment=required,
                steps=[Step("Douyin " + surface, command, str(script.parent),
                            pass_environment=pass_environment)],
                timeout_seconds=args.timeout_seconds, backend="douyin")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("search", "detail"), required=True)
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--item-id", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)
    try:
        return collect_douyin(
            Path(args.output), surface=args.surface, ids=args.item_id, queries=args.query,
            max_items=args.max_items, max_pages=args.max_pages,
            salt=os.getenv("DOUYIN_PSEUDONYM_SALT", ""),
            session_mode=args.session, headless=args.headless,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
