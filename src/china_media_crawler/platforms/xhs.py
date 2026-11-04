"""Independent Xiaohongshu search and detail collection through our own browser context."""
from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import os
import re
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from china_media_crawler import __version__, xhs_session
from china_media_crawler.authentication import get_session_provider
from china_media_crawler.configuration import clean_text, read_urls, validate_xhs_note_url
from china_media_crawler.http import CollectionError
from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Plan, Record, Step
from china_media_crawler.privacy import pseudonym


SEARCH_MARKERS = ("/api/sns/web/v1/search/notes", "/api/sns/web/v2/search/notes")
DETAIL_MARKER = "/api/sns/web/v1/feed"


def note_id_from_url(url: str) -> str | None:
    path = urlparse(url).path
    marker = "/explore/"
    if marker not in path:
        return None
    value = path.split(marker, 1)[1].split("/", 1)[0]
    return value if re.fullmatch(r"[0-9A-Za-z_-]+", value) else None


def close_safely(resource: Any) -> None:
    try:
        resource.close()
    except Exception:
        pass


def validation_error(code: str, stage: str, field: str | None = None) -> CollectionError:
    error = CollectionError(code)
    error.validation_code = code
    error.validation_stage = stage
    error.validation_field = field
    return error


def valid_note_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise validation_error("unexpected_identifier", "note_record", "note_id")
    text = str(value)
    if not re.fullmatch(r"[0-9A-Za-z_-]+", text):
        raise validation_error("unexpected_identifier", "note_record", "note_id")
    return text


def note_record(note: dict[str, Any], surface: str, captured_at: str,
                salt: str, query: str | None = None, page: int | None = None,
                identifier: str | None = None) -> Record:
    note_id = note.get("note_id") or identifier
    if not isinstance(note_id, str) or not note_id:
        raise validation_error("unexpected_note_schema", "note_record", "note_id")
    user = note.get("user")
    if not isinstance(user, dict):
        raise validation_error("unexpected_note_schema", "note_record", "user")
    user_id = user.get("user_id")
    if user_id is None:
        raise validation_error("unexpected_note_schema", "note_record", "user_id")
    title = note.get("display_title")
    desc = note.get("desc")
    if not isinstance(title, str) and not isinstance(desc, str):
        raise validation_error("unexpected_note_schema", "note_record", "content")
    interact = note.get("interact_info") if isinstance(note.get("interact_info"), dict) else {}
    engagement = {}
    for field, label in (("liked_count", "likes"), ("collected_count", "collects"),
                         ("comment_count", "comments"), ("shared_count", "shares")):
        value = interact.get(field)
        if type(value) is int:
            engagement[label] = value
    published = None
    timestamp = note.get("time")
    if type(timestamp) is int:
        seconds = timestamp / 1000 if timestamp > 10**12 else timestamp
        published = datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    platform_data: dict[str, Any] = {
        "note_id": note_id,
        "title": title if isinstance(title, str) else None,
        "note_type": note.get("type") if isinstance(note.get("type"), str) else None,
    }
    if query is not None:
        platform_data["query"] = query
    if page is not None:
        platform_data["page"] = page
    return Record(
        platform="xhs", surface=surface, object_id=valid_note_id(note_id),
        content=desc if isinstance(desc, str) and desc.strip() else title,
        published_at=published, captured_at=captured_at,
        author_pseudonym=pseudonym(str(user_id), salt), engagement=engagement,
        platform_data=platform_data,
    )


def search_payload(payload: Any, query: str, page: int, captured_at: str,
                   salt: str) -> tuple[list[Record], bool]:
    if not isinstance(payload, dict):
        raise validation_error("unexpected_search_schema", "search_payload", "payload")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise validation_error("unexpected_search_schema", "search_payload", "data")
    items = data.get("items")
    if not isinstance(items, list):
        raise validation_error("unexpected_search_schema", "search_payload", "items")
    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        note = item.get("note_card")
        if isinstance(note, dict):
            title_value = note.get("display_title")
            desc_value = note.get("desc")
            if (not isinstance(title_value, str) or not title_value.strip()) and (
                    not isinstance(desc_value, str) or not desc_value.strip()):
                continue
            identifier = item.get("id") if isinstance(item.get("id"), str) else None
            records.append(note_record(note, "search", captured_at, salt, query, page,
                                       identifier))
    return records, bool(data.get("has_more"))


def detail_payload(payload: Any, captured_at: str, salt: str,
                   expected_id: str | None = None) -> Record:
    if not isinstance(payload, dict):
        raise validation_error("unexpected_detail_schema", "detail_payload", "payload")
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise validation_error("unexpected_detail_schema", "detail_payload", "data.items")
    item = None
    note = None
    for candidate in items:
        if not isinstance(candidate, dict):
            continue
        candidate_note = candidate.get("note_card")
        if not isinstance(candidate_note, dict):
            continue
        identifiers = {
            candidate.get("id") if isinstance(candidate.get("id"), str) else None,
            candidate_note.get("note_id") if isinstance(candidate_note.get("note_id"), str) else None,
        }
        if expected_id is None or expected_id in identifiers:
            item = candidate
            note = candidate_note
            break
    if item is None or note is None:
        if expected_id is not None:
            raise validation_error("unexpected_detail_target", "detail_payload", "note_id")
        raise validation_error("unexpected_detail_schema", "detail_payload", "note_card")
    identifier = item.get("id") if isinstance(item.get("id"), str) else None
    record = note_record(note, "detail", captured_at, salt, identifier=identifier)
    if expected_id is not None and record.object_id != expected_id:
        raise validation_error("unexpected_detail_target", "detail_payload", "note_id")
    return record


def state_note_record(note: Any, captured_at: str, salt: str) -> Record:
    if not isinstance(note, dict):
        raise validation_error("unexpected_detail_schema", "state_note_record", "note")
    normalized = {
        "note_id": note.get("note_id"),
        "display_title": note.get("title"),
        "desc": note.get("desc"),
        "type": note.get("type"),
        "time": note.get("time"),
        "user": {"user_id": note.get("user_id")},
        "interact_info": {
            "liked_count": note.get("liked_count"),
            "collected_count": note.get("collected_count"),
            "comment_count": note.get("comment_count"),
            "shared_count": note.get("shared_count"),
        },
    }
    return note_record(normalized, "detail", captured_at, salt)


def page_state_note(page: Any, expected_id: str | None = None) -> Any:
    return page.evaluate("""(expectedId) => {
      const noteState = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.note;
      const map = noteState && noteState.noteDetailMap;
      if (!map) return null;
      const key = expectedId || Object.keys(map)[0];
      let note = key && map[key] && map[key].note;
      if (!note && expectedId) {
        for (const candidate of Object.values(map)) {
          if (candidate && candidate.note && candidate.note.noteId === expectedId) {
            note = candidate.note;
            break;
          }
        }
      }
      if (!note) return null;
      const interact = note.interactInfo || {};
      const user = note.user || {};
      return {
        note_id: note.noteId,
        title: note.title,
        desc: note.desc,
        type: note.type,
        time: note.time,
        user_id: user.userId,
        liked_count: interact.likedCount,
        collected_count: interact.collectedCount,
        comment_count: interact.commentCount,
        shared_count: interact.shareCount
      };
    }""", expected_id)


def response_payload(response: Any) -> dict[str, Any]:
    status = getattr(response, "status", None)
    if status != 200:
        raise CollectionError("http_error", http_status=status)
    try:
        payload = response.json()
    except Exception:
        raise CollectionError("unexpected_response") from None
    if not isinstance(payload, dict):
        raise CollectionError("unexpected_response")
    success = payload.get("success")
    code = payload.get("code")
    if success is False or (code is not None and code != 0):
        raise CollectionError("api_error", api_code=code if type(code) is int else None)
    return payload


def wait_for_response(responses: list[Any], count: int, page: Any,
                      timeout: float = 20.0) -> Any | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(responses) >= count:
            return responses[count - 1]
        page.wait_for_timeout(500)
    return None


def collect_xhs(
    output: Path, *, surface: str, queries: list[str] | None = None,
    urls: list[str] | None = None, max_items: int = 20, max_pages: int = 5,
    salt: str = "", session_mode: str = "auto", headless: bool = True,
) -> int:
    if surface not in {"search", "detail"} or not 1 <= max_items <= 200:
        raise ValueError("invalid Xiaohongshu surface or limits")
    if not 1 <= max_pages <= 50:
        raise ValueError("max pages must be between 1 and 50")
    if not salt.strip():
        raise ValueError("XHS_PSEUDONYM_SALT is required")
    queries = list(dict.fromkeys(clean_text(value, "query") for value in (queries or [])))[:10]
    urls = list(dict.fromkeys(validate_xhs_note_url(value) for value in (urls or [])))[:max_items]
    if surface == "search" and not queries:
        raise ValueError("Xiaohongshu search requires --query")
    if surface == "detail" and not urls:
        raise ValueError("Xiaohongshu detail requires --item-url or --input-file")

    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(path.name != "run_manifest.json" for path in output.iterdir()):
        raise ValueError("choose a new output directory")
    manifest_path = output / "xhs_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "0.2", "tool_version": __version__, "platform": "xhs",
        "surface": surface, "transport": "playwright",
        "status": "running", "cookie_used": False, "session_source": "pending",
        "started_at": utc_now(), "record_count": 0, "http_request_count": 0,
        "objects": [],
        "limits": {"items": max_items, "pages": max_pages, "queries": len(queries)},
    }
    write_manifest(manifest_path, manifest)
    progress = None
    try:
        source, state = get_session_provider("xhs").collection_state(
            session_mode, os.getenv("XHS_COOKIE", "").strip())
        if state is None:
            raise CollectionError("login_required")
        manifest.update(session_source=source, cookie_used=source in {"saved", "cookie"})
        base_url = xhs_session.preferred_host(state)
        write_manifest(manifest_path, manifest)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise CollectionError("missing_playwright") from None
        with sync_playwright() as driver, ExitStack() as cleanup:
            browser = driver.chromium.launch(headless=headless)
            cleanup.callback(close_safely, browser)
            context = browser.new_context(locale="zh-CN", storage_state=state)
            cleanup.callback(close_safely, context)
            page = context.new_page()
            search_responses: list[Any] = []
            detail_responses: list[Any] = []
            page.on("response", lambda response: (
                search_responses.append(response)
                if any(marker in response.url for marker in SEARCH_MARKERS)
                else detail_responses.append(response)
                if DETAIL_MARKER in response.url else None
            ))
            with (output / "records.jsonl").open("x", encoding="utf-8") as stream:
                def emit(record: Record) -> None:
                    stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                    stream.flush()
                    manifest["record_count"] += 1

                if surface == "search":
                    for query in queries:
                        progress = {"query": query, "pages": [], "records": 0,
                                    "termination_reason": "page_limit"}
                        search_responses.clear()
                        page.goto(
                            base_url + "/search_result?"
                            + "keyword=" + quote(query) + "&source=web_search_result_notes",
                            wait_until="domcontentloaded", timeout=30_000,
                        )
                        if "website-login" in page.url or "/login" in page.url:
                            raise CollectionError("access_or_login_challenge")
                        seen: set[str] = set()
                        for page_number in range(1, max_pages + 1):
                            response = wait_for_response(search_responses, page_number, page)
                            if response is None:
                                if page_number == 1:
                                    raise CollectionError("search_response_missing")
                                progress["termination_reason"] = "response_timeout"
                                raise CollectionError("search_response_timeout")
                            manifest["http_request_count"] += 1
                            payload = response_payload(response)
                            records, has_more = search_payload(
                                payload, query, page_number, utc_now(), salt)
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
                            progress["pages"].append({
                                "page": page_number, "returned": len(records),
                                "accepted": accepted, "duplicates": duplicates,
                            })
                            if len(seen) >= max_items:
                                progress["termination_reason"] = "item_limit"
                                break
                            if not has_more:
                                progress["termination_reason"] = "has_more_end"
                                break
                            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(1500)
                            manifest["http_request_count"] = page_number
                            write_manifest(manifest_path, manifest)
                        manifest["objects"].append(progress)
                        progress = None
                        write_manifest(manifest_path, manifest)
                else:
                    for url in urls:
                        parsed_url = urlparse(url)
                        safe_url = parsed_url._replace(query="", fragment="").geturl()
                        expected_id = note_id_from_url(url)
                        progress = {"url": safe_url, "records": 0,
                                    "termination_reason": "item_limit", "note_id": expected_id}
                        detail_responses.clear()
                        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        if "website-login" in page.url or "/login" in page.url:
                            raise CollectionError("access_or_login_challenge")
                        manifest["http_request_count"] += 1
                        response = wait_for_response(detail_responses, 1, page)
                        if response is not None:
                            record = detail_payload(
                                response_payload(response), utc_now(), salt,
                                expected_id=expected_id,
                            )
                        else:
                            state_note = page_state_note(page, expected_id)
                            if state_note is None:
                                raise CollectionError("detail_response_missing")
                            record = state_note_record(state_note, utc_now(), salt)
                            if expected_id is not None and record.object_id != expected_id:
                                raise validation_error(
                                    "unexpected_detail_target", "page_state_note", "note_id")
                        emit(record)
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
                        finished_at=utc_now(), error_type=type(error).__name__)
        if progress is not None:
            manifest["partial_object"] = progress
        if isinstance(error, CollectionError):
            manifest.update(error_kind=error.kind, http_status=error.http_status,
                            api_code=error.api_code)
            for attribute in ("validation_code", "validation_stage", "validation_field"):
                if hasattr(error, attribute):
                    manifest[attribute] = getattr(error, attribute)
            if error.kind in {"login_required", "session_expired", "session_scope_invalid",
                              "access_or_login_challenge"}:
                manifest["login_action_required"] = True
                manifest["next_action"] = "cmc login --platform xhs"
        write_manifest(manifest_path, manifest)
        if isinstance(error, CollectionError):
            return 2
        raise


def xhs_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    queries = []
    if surface == "search":
        queries = list(dict.fromkeys(
            clean_text(value, "query") for value in getattr(args, "query", [])))[:10]
        if not queries:
            raise ValueError("Xiaohongshu search requires --query")
        urls = []
    else:
        if getattr(args, "item_id", []):
            raise ValueError("Xiaohongshu detail accepts --item-url, not --item-id")
        urls = list(dict.fromkeys(validate_xhs_note_url(value) for value in read_urls(args)))[:args.max_items]
        if not urls:
            raise ValueError("Xiaohongshu detail requires --item-url or --input-file")
    script = Path(__file__).resolve()
    command = [sys.executable, str(script), "--surface", surface, "--output", str(output),
               "--max-items", str(args.max_items), "--max-pages", str(args.max_pages),
               "--session", getattr(args, "session", "auto"), "--headless"]
    for query in queries:
        command += ["--query", query]
    for url in urls:
        command += ["--item-url", url]
    required = ["XHS_PSEUDONYM_SALT"]
    pass_environment = required + ["XHS_COOKIE"]
    return Plan(created_at=utc_now(), platform="xhs", surface=surface, output=str(output),
                limits={"items": args.max_items, "pages": args.max_pages, "queries": len(queries)},
                required_environment=required,
                steps=[Step("Xiaohongshu " + surface, command, str(script.parent),
                            pass_environment=pass_environment)],
                timeout_seconds=args.timeout_seconds, backend="xhs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("search", "detail"), required=True)
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--item-url", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)
    try:
        return collect_xhs(
            Path(args.output), surface=args.surface, queries=args.query, urls=args.item_url,
            max_items=args.max_items, max_pages=args.max_pages,
            salt=os.getenv("XHS_PSEUDONYM_SALT", ""), session_mode=args.session,
            headless=args.headless,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())









