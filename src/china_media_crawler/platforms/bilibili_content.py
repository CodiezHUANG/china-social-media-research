"""video metadata, first-level comments, and separate XML danmaku sampling."""
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
import sys
import time
import zlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from china_media_crawler import __version__
from china_media_crawler.configuration import read_ids
from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Plan, Record, Step
from china_media_crawler.privacy import pseudonym
from china_media_crawler.authentication import get_session_provider
from china_media_crawler.bilibili_session import cookie_header as session_cookie
from china_media_crawler.http import (
    CollectionError, MAX_RESPONSE_BYTES, RejectRedirect, connection_error,
    fetch_json as transport_fetch_json, request_headers,
)
from china_media_crawler.platforms.bilibili_search import BVID_PATTERN
from china_media_crawler.text import plain_text

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
REPLY_URL = "https://api.bilibili.com/x/v2/reply"
DANMAKU_URL = "https://api.bilibili.com/x/v1/dm/list.so"


def positive_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CollectionError("unexpected_identifier")
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 30 or int(text) <= 0:
        raise CollectionError("unexpected_identifier")
    return str(int(value))


def timestamp(value: Any) -> str:
    if type(value) is not int or value < 0:
        raise CollectionError("unexpected_timestamp")
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as error:
        raise CollectionError("unexpected_timestamp") from error


def api_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise CollectionError("unexpected_schema")
    if payload["code"] != 0:
        raise CollectionError("api_error", api_code=payload["code"])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CollectionError("unexpected_schema")
    if payload.get("v_voucher") or data.get("v_voucher"):
        raise CollectionError("access_challenge")
    return data


def video_data(payload: Any, bvid: str) -> dict[str, Any]:
    data = api_data(payload)
    if data.get("bvid") != bvid or not isinstance(data.get("title"), str):
        raise CollectionError("unexpected_video_schema")
    positive_id(data.get("aid"))
    if not isinstance(data.get("owner"), dict):
        raise CollectionError("unexpected_video_schema")
    positive_id(data["owner"].get("mid"))
    return data


def video_record(data: dict[str, Any], salt: str) -> Record:
    if not isinstance(data.get("desc", ""), str):
        raise CollectionError("unexpected_video_schema")
    stats = data.get("stat")
    if not isinstance(stats, dict):
        raise CollectionError("unexpected_video_schema")
    return Record(
        platform="bilibili", surface="detail", object_id=data["bvid"],
        content=plain_text(data["title"]), published_at=timestamp(data.get("pubdate")),
        captured_at=utc_now(), author_pseudonym=pseudonym(positive_id(data["owner"]["mid"]), salt),
        engagement={key: value for key, value in stats.items()
                    if key in {"view", "danmaku", "reply", "favorite", "coin", "share", "like"}
                    and type(value) is int},
        platform_data={"bvid": data["bvid"], "aid": positive_id(data["aid"]),
                       "description": plain_text(data.get("desc", "")),
                       "duration": data.get("duration") if type(data.get("duration")) is int else None,
                       "url": "https://www.bilibili.com/video/" + data["bvid"]},
    )


def comment_page(payload: Any, requested_page: int) -> tuple[list[dict[str, Any]], int]:
    data = api_data(payload)
    pagination = data.get("page")
    rows = data.get("replies")

    def fail(kind: str) -> None:
        error = CollectionError(kind)
        error.pagination_diagnostic = {
            "requested_page": requested_page,
            **{key: pagination.get(key) if isinstance(pagination, dict)
               and type(pagination.get(key)) is int else None for key in ("num", "size", "count")},
            "returned_rows": len(rows) if isinstance(rows, list) else None,
        }
        raise error

    if (isinstance(pagination, dict)
            and all(type(pagination.get(key)) is int and pagination[key] == 0 for key in ("num", "size", "count"))
            and rows is None):
        fail("comment_pagination_unavailable")
    if (not isinstance(pagination, dict) or type(pagination.get("count")) is not int
            or pagination["count"] < 0 or type(pagination.get("num")) is not int
            or pagination["num"] != requested_page
            or ("size" in pagination and (type(pagination["size"]) is not int or pagination["size"] != 20))):
        fail("unexpected_comment_page")
    if rows is None:
        if pagination["count"] > (requested_page - 1) * 20:
            fail("comment_pagination_unavailable")
        rows = []
    if (not isinstance(rows, list) or len(rows) > 20
            or any(not isinstance(row, dict) for row in rows)):
        raise CollectionError("unexpected_comment_schema")
    if not rows and pagination["count"] > (requested_page - 1) * 20:
        raise CollectionError("unexpected_empty_comment_page")
    return rows, pagination["count"]


def first_page_comments(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    data = api_data(payload)
    rows = data.get("replies")
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    cursor = data.get("cursor") if isinstance(data.get("cursor"), dict) else {}
    total = page.get("count")
    if total is None:
        total = cursor.get("all_count", cursor.get("count"))
    if rows is None:
        if total == 0:
            rows = []
        else:
            raise CollectionError("unexpected_comment_schema")
    if (not isinstance(rows, list) or len(rows) > 20
            or any(not isinstance(row, dict) for row in rows)):
        raise CollectionError("unexpected_comment_schema")
    if not rows and isinstance(total, int) and total > 0:
        raise CollectionError("unexpected_empty_comment_page")
    return rows, total if isinstance(total, int) else None


def comment_record(row: dict[str, Any], bvid: str, page: int, salt: str,
                   *, level: int = 1, parent_id: str | None = None) -> Record:
    if level == 1 and (row.get("root", 0) not in (0, "0")
                       or row.get("parent", 0) not in (0, "0")):
        raise CollectionError("unexpected_nested_comment")
    content, member = row.get("content"), row.get("member")
    if (not isinstance(content, dict) or not isinstance(content.get("message"), str)
            or not isinstance(member, dict)):
        raise CollectionError("unexpected_comment_schema")
    if level == 2 and parent_id is None:
        parent_id = positive_id(row.get("root"))
    return Record(
        platform="bilibili", surface="comments", object_id=positive_id(row.get("rpid")),
        content=content["message"], published_at=timestamp(row.get("ctime")),
        captured_at=utc_now(), author_pseudonym=pseudonym(positive_id(member.get("mid")), salt),
        engagement={key: row[key] for key in ("like", "rcount") if type(row.get(key)) is int},
        platform_data={"bvid": bvid, "page": page, "level": level,
                       "parent_id": parent_id},
    )


def decode_xml(body: bytes, encoding: str = "identity") -> bytes:
    if len(body) > MAX_RESPONSE_BYTES:
        raise CollectionError("response_too_large")
    encoding = encoding.strip().lower()
    if encoding in {"", "identity"}:
        return body
    if encoding not in {"deflate", "gzip"}:
        raise CollectionError("unexpected_xml_encoding")
    try:
        # Some HTTP servers label raw DEFLATE as "deflate" without a zlib header.
        wrapped = (len(body) >= 2 and body[0] & 15 == 8
                   and (body[0] * 256 + body[1]) % 31 == 0)
        window = 31 if encoding == "gzip" else (zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
        decoder = zlib.decompressobj(window)
        decoded = decoder.decompress(body, MAX_RESPONSE_BYTES + 1)
        if len(decoded) > MAX_RESPONSE_BYTES or decoder.unconsumed_tail:
            raise CollectionError("response_too_large")
        if not decoder.eof or decoder.unused_data:
            raise CollectionError("unexpected_xml_encoding")
        return decoded
    except zlib.error as error:
        raise CollectionError("unexpected_xml_encoding") from error


def fetch_xml(url: str, *, cookie_header: str | None = None,
              referer: str = "https://www.bilibili.com/") -> bytes:
    request = Request(url, headers=request_headers(url, {
        "User-Agent": f"ChinaMediaCrawler/{__version__} (research collection)",
        "Accept": "application/xml,text/xml", "Referer": referer,
        "Accept-Encoding": "identity",
    }, cookie_header, "api.bilibili.com"))
    try:
        with build_opener(ProxyHandler({}), RejectRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                raise CollectionError("http_error", http_status=response.status)
            body = response.read(MAX_RESPONSE_BYTES + 1)
            encoding = response.headers.get("Content-Encoding", "identity")
    except HTTPError as error:
        error.close()
        raise CollectionError("http_error", http_status=error.code) from error
    except (URLError, OSError, TimeoutError) as error:
        raise connection_error(error) from error
    return decode_xml(body, encoding)


class NoDtdTreeBuilder(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        # Parser-level rejection also covers UTF-16/other XML encodings.
        raise CollectionError("unsafe_xml")


def danmaku_records(body: bytes, bvid: str, cid: str, part: int, salt: str) -> list[Record]:
    if len(body) > MAX_RESPONSE_BYTES or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise CollectionError("unsafe_xml")
    try:
        root = ET.fromstring(body, parser=ET.XMLParser(target=NoDtdTreeBuilder()))
    except (ET.ParseError, ValueError) as error:
        raise CollectionError("unexpected_xml") from error
    if root.tag != "i":
        raise CollectionError("unexpected_xml")
    chatid = root.findtext("chatid")
    if chatid is None or positive_id(chatid) != cid:
        raise CollectionError("unexpected_danmaku_cid")
    records = []
    for element in root.findall("d"):
        fields = element.get("p", "").split(",")
        if len(fields) < 8 or element.text is None:
            raise CollectionError("unexpected_danmaku_schema")
        try:
            progress = float(fields[0])
            posted = int(fields[4])
        except ValueError as error:
            raise CollectionError("unexpected_danmaku_schema") from error
        if not math.isfinite(progress) or progress < 0:
            raise CollectionError("unexpected_danmaku_schema")
        records.append(Record(
            platform="bilibili", surface="danmaku", object_id=cid + ":" + positive_id(fields[7]),
            content=element.text, published_at=timestamp(posted), captured_at=utc_now(),
            author_pseudonym=pseudonym(fields[6], salt),
            platform_data={"bvid": bvid, "cid": cid, "part": part,
                           "progress_seconds": progress, "mode": fields[1]},
        ))
    return records


class PublicClient:
    def __init__(self, json_fetch: Callable | None = None, xml_fetch: Callable | None = None,
                 sleep: Callable = time.sleep, min_delay: float = 5.0,
                 max_delay: float = 8.0) -> None:
        self.state = None
        self.referer = "https://www.bilibili.com/"
        self.json_fetch = json_fetch or (lambda url: transport_fetch_json(
            url, referer=self.referer, opener_factory=build_opener,
            cookie_header=session_cookie(self.state, url), credential_host="api.bilibili.com"))
        self.xml_fetch = xml_fetch or (lambda url: fetch_xml(
            url, referer=self.referer, cookie_header=session_cookie(self.state, url)))
        self.sleep = sleep
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.requests = 0

    def get(self, url: str, *, xml: bool = False) -> Any:
        if self.requests:
            self.sleep(random.uniform(self.min_delay, self.max_delay))
        self.requests += 1
        return (self.xml_fetch if xml else self.json_fetch)(url)

    def probe(self, url: str) -> Any:
        return self.json_fetch(url)


def classify_failure(error: BaseException) -> str:
    if isinstance(error, KeyboardInterrupt):
        return "cancelled"
    if isinstance(error, CollectionError):
        if error.kind in {"comment_pagination_unavailable",
                          "unexpected_empty_comment_page", "repeated_cursor"}:
            return "pagination_unavailable"
        if error.kind == "api_error":
            if error.api_code in {-352, 412, 429, 401, 403}:
                return "rate_limited"
            if error.api_code == 62002:
                return "platform_invisible"
            return "schema_changed"
        if error.kind == "http_error":
            if error.http_status in {401, 403, 412, 429}:
                return "rate_limited"
            return "schema_changed"
        if error.kind in {"connection_failed", "precheck_failed"}:
            return "env_blocked"
        if error.kind in {
            "login_required", "session_invalid", "session_expired",
            "session_scope_invalid", "cookie_invalid", "access_challenge",
        }:
            return "access_restricted"
    return "schema_changed"


def connectivity_precheck(client: PublicClient, bvid: str) -> dict[str, Any]:
    attempts = []
    for index in range(3):
        item = {"attempt": index + 1, "http_status": None, "api_code": None,
                "success": False, "error": ""}
        try:
            payload = client.probe(VIEW_URL + "?" + urlencode({"bvid": bvid}))
            item["api_code"] = payload.get("code") if isinstance(payload, dict) else None
            data = payload.get("data") if isinstance(payload, dict) else None
            item["success"] = (
                item["api_code"] == 0 and isinstance(data, dict) and data.get("bvid") == bvid
            ) or item["api_code"] == 62002
        except Exception as error:
            item["error"] = type(error).__name__
        attempts.append(item)
        if index < 2:
            time.sleep(2)
    rate = sum(1 for item in attempts if item["success"]) / 3
    result = {"attempts": attempts, "success_rate": rate,
              "status": "pass" if rate >= 2 / 3 else "stop"}
    if result["status"] != "pass":
        error = CollectionError("precheck_failed")
        error.precheck = result
        raise error
    return result


def collect_content(
    ids: list[str], output: Path, *, surface: str, salt: str, max_items: int = 20,
    comments_per_item: int = 20, max_pages: int = 5, danmaku_per_item: int = 100,
    max_parts: int = 1, client: PublicClient | None = None,
    session_mode: str = "off", comments_mode: str = "first_page",
    precheck: bool = True, on_item_error: str = "stop",
) -> int:
    if surface not in {"detail", "comments", "danmaku"}:
        raise ValueError("unsupported content surface")
    if comments_mode not in {"first_page", "deep"}:
        raise ValueError("comments mode must be first_page or deep")
    if on_item_error not in {"stop", "continue"}:
        raise ValueError("on-item-error must be stop or continue")
    if surface != "comments" and comments_mode != "first_page":
        raise ValueError("comments mode is only valid for comments")
    if not 1 <= max_items <= 200 or not 1 <= max_pages <= 5:
        raise ValueError("invalid item/page limit")
    if not 0 <= comments_per_item <= 100 or not 1 <= danmaku_per_item <= 1000:
        raise ValueError("invalid comment/danmaku limit")
    if not 1 <= max_parts <= 5 or (surface == "comments" and comments_per_item == 0):
        raise ValueError("invalid part/comment limit")
    if not salt.strip() or not ids or any(not BVID_PATTERN.fullmatch(value) for value in ids):
        raise ValueError("valid BV IDs and BILI_PSEUDONYM_SALT are required")
    ids = list(dict.fromkeys(ids))[:max_items]
    effective_pages = 1 if (surface == "comments" and comments_mode == "first_page") else max_pages
    client = client or PublicClient()
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(path.name != "run_manifest.json" for path in output.iterdir()):
        raise ValueError("choose a new output directory")
    manifest_path = output / f"bilibili_{surface}_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "0.2", "tool_version": __version__, "platform": "bilibili",
        "surface": surface, "status": "running",
        "started_at": utc_now(), "cookie_used": False, "session_source": "pending", "record_count": 0, "objects": [],
        "comments_mode": comments_mode,
        "limits": {"items": max_items, "pages": effective_pages, "comments_per_item": comments_per_item,
                   "danmaku_per_item": danmaku_per_item, "parts_per_item": max_parts},
    }
    write_manifest(manifest_path, manifest)
    current = None
    accounting = None
    precheck_result = None
    stage, failed_page, failed_part, failed_cid = "session_load", None, None, None
    try:
        source, client.state = get_session_provider("bilibili").collection_state(
            session_mode, os.getenv("BILIBILI_COOKIE", "").strip())
        manifest.update(session_source=source, cookie_used=source != "anonymous")
        write_manifest(manifest_path, manifest)
        client.referer = "https://www.bilibili.com/video/" + ids[0]
        if precheck:
            stage = "precheck"
            precheck_result = connectivity_precheck(client, ids[0])
            manifest["precheck"] = precheck_result
            write_manifest(manifest_path, manifest)
        with (output / "records.jsonl").open("x", encoding="utf-8") as stream:
            def emit(record: Record) -> None:
                stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                stream.flush()
                manifest["record_count"] += 1
                if accounting is not None:
                    accounting["records"] += 1

            failures = 0
            for current in ids:
                accounting = None
                seen: set[str] = set()
                stage, failed_page, failed_part, failed_cid = "video_metadata", None, None, None
                client.referer = "https://www.bilibili.com/video/" + current
                try:
                    data = video_data(client.get(VIEW_URL + "?" + urlencode({"bvid": current})), current)
                except CollectionError as error:
                    if on_item_error != "continue":
                        raise
                    failures += 1
                    manifest["objects"].append({
                        "bvid": current, "records": 0, "pages": [],
                        "termination_reason": "failed",
                        "failure_class": classify_failure(error),
                        "error_kind": error.kind,
                        "failure_stage": stage,
                    })
                    manifest["http_request_count"] = client.requests
                    write_manifest(manifest_path, manifest)
                    continue
                accounting: dict[str, Any] = {"bvid": current, "records": 0, "pages": [],
                                              "termination_reason": "complete"}
                if surface == "detail":
                    record = video_record(data, salt)
                    stage = "output_write"
                    emit(record)
                    accounting["records"] = 1
                elif surface == "comments" and comments_mode == "first_page":
                    accounting["termination_reason"] = "first_page"
                    stage, failed_page = "comments_page", 1
                    rows, total = first_page_comments(client.get(
                        REPLY_URL + "?" + urlencode({
                            "type": 1, "oid": positive_id(data["aid"]), "pn": 1,
                            "ps": 20, "sort": 2, "nohot": 1,
                        })))
                    main_accepted = nested_accepted = nested_returned = 0
                    for row in rows[:comments_per_item]:
                        main_record = comment_record(row, current, 1, salt, level=1)
                        if main_record.object_id not in seen:
                            stage = "output_write"
                            emit(main_record)
                            seen.add(main_record.object_id)
                            main_accepted += 1
                        replies = row.get("replies")
                        if isinstance(replies, list):
                            nested_returned += len(replies)
                            for sub in replies:
                                if nested_accepted >= comments_per_item * 5:
                                    break
                                if not isinstance(sub, dict):
                                    raise CollectionError("unexpected_comment_schema")
                                nested_record = comment_record(
                                    sub, current, 1, salt, level=2,
                                    parent_id=positive_id(row.get("rpid")))
                                if nested_record.object_id in seen:
                                    continue
                                stage = "output_write"
                                emit(nested_record)
                                seen.add(nested_record.object_id)
                                nested_accepted += 1
                    accounting["records"] = len(seen)
                    accounting["nested_reply_count"] = nested_accepted
                    accounting["pages"].append({
                        "page": 1, "returned": len(rows),
                        "accepted": main_accepted + nested_accepted,
                        "main_accepted": main_accepted,
                        "nested_returned": nested_returned,
                        "nested_accepted": nested_accepted,
                        "reported_total": total,
                    })
                elif surface == "comments":
                    accounting["termination_reason"] = "page_limit"
                    for page in range(1, effective_pages + 1):
                        stage, failed_page = "comments_page", page
                        try:
                            rows, total = comment_page(client.get(REPLY_URL + "?" + urlencode({
                                "type": 1, "oid": positive_id(data["aid"]), "pn": page,
                                "ps": 20, "sort": 0,
                            })), page)
                        except CollectionError as error:
                            if on_item_error != "continue":
                                raise
                            failures += 1
                            accounting.update(
                                termination_reason="failed",
                                failure_class=classify_failure(error),
                                error_kind=error.kind,
                                failure_stage=stage,
                                failed_page=failed_page,
                            )
                            if hasattr(error, "pagination_diagnostic"):
                                accounting["pagination_diagnostic"] = error.pagination_diagnostic
                            break
                        normalized = [comment_record(row, current, page, salt) for row in rows]
                        accepted = 0
                        for record in normalized:
                            if record.object_id in seen:
                                continue
                            if len(seen) >= comments_per_item:
                                break
                            stage = "output_write"
                            emit(record)
                            seen.add(record.object_id)
                            accepted += 1
                        accounting["records"] = len(seen)
                        accounting["pages"].append({"page": page, "returned": len(rows),
                                                    "accepted": accepted, "reported_total": total})
                        if len(seen) >= comments_per_item:
                            accounting["termination_reason"] = "item_limit"
                            break
                        if not rows or page * 20 >= total:
                            accounting["termination_reason"] = "reported_end"
                            break
                else:
                    stage = "video_parts"
                    parts = data.get("pages")
                    if not isinstance(parts, list) or not parts:
                        raise CollectionError("unexpected_video_parts")
                    selected = []
                    selected_cids: set[str] = set()
                    for index, part in enumerate(parts[:max_parts], 1):
                        if (not isinstance(part, dict) or type(part.get("page")) is not int
                                or part["page"] != index):
                            raise CollectionError("unexpected_video_parts")
                        cid = positive_id(part.get("cid"))
                        if cid in selected_cids:
                            raise CollectionError("unexpected_video_parts")
                        selected_cids.add(cid)
                        selected.append((index, cid))
                    accounting["termination_reason"] = "part_limit" if len(parts) > max_parts else "complete"
                    for index, cid in selected:
                        stage, failed_part, failed_cid = "danmaku_part", index, cid
                        try:
                            body = client.get(DANMAKU_URL + "?" + urlencode({"oid": cid}), xml=True)
                            records = danmaku_records(body, current, cid, index, salt)
                        except CollectionError as error:
                            if on_item_error != "continue":
                                raise
                            failures += 1
                            accounting.update(
                                termination_reason="failed",
                                failure_class=classify_failure(error),
                                error_kind=error.kind,
                                failure_stage=stage,
                                failed_part=failed_part,
                                failed_cid=failed_cid,
                            )
                            break
                        accepted = 0
                        for record in records:
                            if record.object_id in seen:
                                continue
                            if len(seen) >= danmaku_per_item:
                                break
                            stage = "output_write"
                            emit(record)
                            seen.add(record.object_id)
                            accepted += 1
                        accounting["records"] = len(seen)
                        accounting["pages"].append({"part": index, "cid": cid,
                                                    "returned": len(records), "accepted": accepted})
                        if len(seen) >= danmaku_per_item:
                            accounting["termination_reason"] = "item_limit"
                            break
                manifest["objects"].append(accounting)
                manifest["http_request_count"] = client.requests
                write_manifest(manifest_path, manifest)
        status = "complete_with_failures" if failures else "complete"
        manifest.update(status=status, finished_at=utc_now(), failed_object_count=failures)
        write_manifest(manifest_path, manifest)
        return 1 if failures else 0
    except BaseException as error:
        manifest.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                        finished_at=utc_now(), failed_object=current,
                        error_type=type(error).__name__, http_request_count=client.requests,
                        failure_stage=stage, failure_class=classify_failure(error))
        if precheck_result is not None:
            manifest["precheck"] = precheck_result
        if hasattr(error, "precheck"):
            manifest["precheck"] = error.precheck
        for key, value in (("failed_page", failed_page), ("failed_part", failed_part), ("failed_cid", failed_cid)):
            if value is not None:
                manifest[key] = value
        if isinstance(error, CollectionError):
            manifest.update(error_kind=error.kind, http_status=error.http_status, api_code=error.api_code)
            manifest["transport_error_type"] = error.transport_error_type
            if hasattr(error, "pagination_diagnostic"):
                manifest["pagination_diagnostic"] = error.pagination_diagnostic
            if error.kind in {"login_required", "session_invalid", "session_expired", "session_scope_invalid", "cookie_invalid", "access_challenge"} or error.api_code == -101:
                manifest.update(login_action_required=True, next_action="cmc login --platform bilibili")
        if accounting is not None:
            accounting["termination_reason"] = manifest["status"]
            manifest["partial_object"] = accounting
        write_manifest(manifest_path, manifest)
        if isinstance(error, CollectionError):
            return 2
        raise


def content_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    ids = read_ids(args)
    if not ids or any(not BVID_PATTERN.fullmatch(value) for value in ids):
        raise ValueError("Bilibili content requires valid --item-id or --input-file BV IDs")
    command = [sys.executable, str(Path(__file__).resolve()), "--surface", surface,
               "--output", str(output), "--max-items", str(args.max_items),
               "--comments-per-item", str(args.comments_per_item), "--max-pages", str(args.max_pages),
               "--danmaku-per-item", str(args.danmaku_per_item), "--max-parts", str(args.max_parts),
               "--session", getattr(args, "session", "auto"),
               "--on-item-error", getattr(args, "on_item_error", "stop")]
    if surface == "comments":
        command += ["--comments-mode", getattr(args, "comments_mode", "first_page")]
    for bvid in ids[:args.max_items]:
        command += ["--item-id", bvid]
    pages = 1 if (surface == "comments"
                  and getattr(args, "comments_mode", "first_page") == "first_page") else args.max_pages
    return Plan(
        created_at=utc_now(), platform="bilibili", surface=surface, output=str(output),
        limits={"items": args.max_items, "comments_per_item": args.comments_per_item,
                "pages": pages, "danmaku_per_item": args.danmaku_per_item,
                "parts_per_item": args.max_parts},
        required_environment=["BILI_PSEUDONYM_SALT"], timeout_seconds=args.timeout_seconds,
        steps=[Step("Bilibili " + surface, command, str(Path(__file__).parent),
                    pass_environment=["BILI_PSEUDONYM_SALT"] + (
                        ["BILIBILI_COOKIE"] if getattr(args, "session", "auto") != "off" else []))],
        backend="bilibili-public-content",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("detail", "comments", "danmaku"), required=True)
    parser.add_argument("--item-id", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--comments-per-item", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--danmaku-per-item", type=int, default=100)
    parser.add_argument("--max-parts", type=int, default=1)
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    parser.add_argument("--comments-mode", choices=("first_page", "deep"), default="first_page")
    parser.add_argument("--on-item-error", choices=("stop", "continue"), default="stop")
    args = parser.parse_args(argv)
    try:
        return collect_content(args.item_id, Path(args.output), surface=args.surface,
                               salt=os.getenv("BILI_PSEUDONYM_SALT", ""), max_items=args.max_items,
                               comments_per_item=args.comments_per_item, max_pages=args.max_pages,
                               danmaku_per_item=args.danmaku_per_item, max_parts=args.max_parts,
                               session_mode=args.session, comments_mode=args.comments_mode,
                               on_item_error=args.on_item_error)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


