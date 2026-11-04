"""Independent bounded Maoyan short-review collection from public tag views."""
from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from china_media_crawler import __version__
from china_media_crawler.configuration import read_ids
from china_media_crawler.http import CollectionError, fetch_json
from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Plan, Record, Step
from china_media_crawler.privacy import pseudonym

PAGE_WIDTH = 15
LOCAL_TIME = timezone(timedelta(hours=8))
ENDPOINT = "https://m.maoyan.com/mmdb/comments/movie/"


def numeric_id(value: Any, *, zero: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CollectionError("unexpected_identifier")
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 30:
        raise CollectionError("unexpected_identifier")
    if not zero and int(text) == 0:
        raise CollectionError("unexpected_identifier")
    return str(int(text))


def parse_tags(value: str) -> list[str]:
    try:
        tags = list(dict.fromkeys(numeric_id(part.strip(), zero=True) for part in value.split(",")))
    except CollectionError as error:
        raise ValueError("tags must be comma-separated nonnegative ASCII integers") from error
    if not 1 <= len(tags) <= 10:
        raise ValueError("select between 1 and 10 unique tag views")
    return tags


def build_url(movie_id: str, tag: str, page: int) -> str:
    return ENDPOINT + numeric_id(movie_id) + ".json?" + urlencode({
        "_v_": "yes", "offset": (page - 1) * PAGE_WIDTH, "tag": numeric_id(tag, zero=True),
    })


def review_page(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(payload, dict):
        raise CollectionError("unexpected_schema")
    if "code" in payload and (type(payload["code"]) is not int or payload["code"] != 0):
        raise CollectionError("api_error", api_code=payload["code"] if type(payload["code"]) is int else None)
    rows = payload.get("cmts")
    if not isinstance(rows, list) or len(rows) > PAGE_WIDTH or any(not isinstance(r, dict) for r in rows):
        raise CollectionError("unexpected_review_schema")
    total = payload.get("total")
    if total is not None and (type(total) is not int or total < 0):
        raise CollectionError("unexpected_total")
    return rows, total


def normalize_review(
    row: dict[str, Any], movie_id: str, tag: str, page: int, position: int,
    captured_at: str, salt: str,
) -> Record:
    if "movieId" in row and numeric_id(row["movieId"]) != movie_id:
        raise CollectionError("unexpected_movie")
    content = row.get("content")
    if not isinstance(content, str) or not content.strip():
        raise CollectionError("unexpected_review_text")
    platform_time = row.get("startTime") or row.get("time")
    if not isinstance(platform_time, str):
        raise CollectionError("unexpected_timestamp")
    try:
        published = datetime.strptime(platform_time, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=LOCAL_TIME,
        ).astimezone(timezone.utc).isoformat()
    except ValueError as error:
        raise CollectionError("unexpected_timestamp") from error
    score = row.get("score")
    if score is not None and (
        type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 5
    ):
        raise CollectionError("unexpected_rating")
    city = row.get("cityName", "")
    if not isinstance(city, str):
        raise CollectionError("unexpected_city")
    author_key = "unavailable"
    author = None
    if row.get("userId") is not None:
        author_key = "user_id"
        author = pseudonym("maoyan:user:" + numeric_id(row["userId"]), salt)
    elif row.get("nickName"):
        if not isinstance(row["nickName"], str):
            raise CollectionError("unexpected_author")
        author_key = "nickname"
        author = pseudonym("maoyan:nick:" + row["nickName"], salt)
    identifier_source = "platform_id"
    if row.get("id") is not None:
        identifier = numeric_id(row["id"])
    else:
        identifier_source = "composite_fingerprint"
        material = json.dumps([content.strip(), published, city, score, author], ensure_ascii=False)
        identifier = "h_" + hashlib.sha256(material.encode()).hexdigest()[:24]
    return Record(
        platform="maoyan", surface="reviews", object_id=movie_id + ":" + identifier,
        content=content.strip(), published_at=published, captured_at=captured_at,
        author_pseudonym=author,
        engagement={"likes": row["approve"]} if type(row.get("approve")) is int else {},
        platform_data={"movie_id": movie_id, "tag": tag, "page": page,
                       "offset": (page - 1) * PAGE_WIDTH, "position": position,
                       "rating": score, "city": city, "platform_time": platform_time,
                       "author_key_source": author_key, "identifier_source": identifier_source},
    )


class PublicClient:
    def __init__(self, fetch: Callable | None = None, sleep: Callable = time.sleep) -> None:
        self.fetch = fetch or fetch_json
        self.sleep = sleep
        self.requests = 0

    def get(self, url: str) -> Any:
        if self.requests:
            self.sleep(random.uniform(2.5, 5.0))
        self.requests += 1
        return self.fetch(url, referer="https://m.maoyan.com/")


def collect_reviews(
    movie_id: str, output: Path, *, tags: str = "0", max_items: int = 20,
    max_pages: int = 5, salt: str, client: PublicClient | None = None,
) -> int:
    try:
        movie_id = numeric_id(movie_id)
    except CollectionError as error:
        raise ValueError("movie ID must be positive ASCII digits") from error
    selected_tags = parse_tags(tags)
    if (type(max_items) is not int or type(max_pages) is not int
            or not 1 <= max_items <= 200 or not 1 <= max_pages <= 5 or not salt.strip()):
        raise ValueError("items must be 1..200, pages 1..5, and MAOYAN_PSEUDONYM_SALT is required")
    client = client or PublicClient()
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(path.name != "run_manifest.json" for path in output.iterdir()):
        raise ValueError("choose a new output directory")
    manifest_path = output / "maoyan_manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "0.2", "tool_version": __version__, "platform": "maoyan",
        "surface": "reviews", "movie_id": movie_id,
        "status": "running", "started_at": utc_now(), "cookie_used": False,
        "record_count": 0, "duplicate_count": 0, "pools": [], "page_width": PAGE_WIDTH,
        "limits": {"reviews": max_items, "pages_per_tag": max_pages, "tags": selected_tags},
    }
    write_manifest(manifest_path, manifest)
    seen: set[str] = set()
    progress = None
    tag, page, stage = None, None, "output_write"
    try:
        with (output / "records.jsonl").open("x", encoding="utf-8") as stream:
            for tag in selected_tags:
                progress = {"tag": tag, "pages": [], "accepted": 0, "termination_reason": "page_limit"}
                for page in range(1, max_pages + 1):
                    stage = "request"
                    payload = client.get(build_url(movie_id, tag, page))
                    stage = "page_validation"
                    rows, total = review_page(payload)
                    captured = utc_now()
                    normalized = []
                    stage = "normalization"
                    for index, row in enumerate(rows, 1):
                        try:
                            normalized.append(normalize_review(
                                row, movie_id, tag, page, index, captured, salt,
                            ))
                        except CollectionError as error:
                            error.record_position = index
                            error.record_fields = sorted(row)
                            raise
                    accepted = duplicates = 0
                    for record in normalized:
                        if record.object_id in seen:
                            duplicates += 1
                            manifest["duplicate_count"] += 1
                            continue
                        if len(seen) >= max_items:
                            break
                        stage = "output_write"
                        stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                        stream.flush()
                        seen.add(record.object_id)
                        accepted += 1
                        progress["accepted"] += 1
                        manifest["record_count"] = len(seen)
                    progress["pages"].append({"page": page, "offset": (page - 1) * PAGE_WIDTH,
                                               "returned": len(rows), "accepted": accepted,
                                               "duplicates": duplicates, "reported_total": total})
                    if len(seen) >= max_items:
                        progress["termination_reason"] = "item_limit"
                        break
                    if not rows:
                        progress["termination_reason"] = "empty_page"
                        break
                    if total is not None and (page - 1) * PAGE_WIDTH + len(rows) >= total:
                        progress["termination_reason"] = "reported_end"
                        break
                    write_manifest(manifest_path, manifest)
                manifest["pools"].append(progress)
                progress = None
                write_manifest(manifest_path, manifest)
                if len(seen) >= max_items:
                    break
        reason = ("item_limit" if len(seen) >= max_items else "page_limit"
                  if any(pool["termination_reason"] == "page_limit" for pool in manifest["pools"])
                  else "selected_views_end")
        manifest.update(status="complete", finished_at=utc_now(), http_request_count=client.requests,
                        termination_reason=reason)
        write_manifest(manifest_path, manifest)
        return 0
    except BaseException as error:
        manifest.update(status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                        finished_at=utc_now(), http_request_count=client.requests,
                        error_type=type(error).__name__, failure_stage=stage)
        if tag is not None:
            manifest["failed_tag"] = tag
        if page is not None:
            manifest.update(failed_page=page, failed_offset=(page - 1) * PAGE_WIDTH)
        if progress is not None:
            progress["termination_reason"] = manifest["status"]
            manifest["partial_pool"] = progress
        if isinstance(error, CollectionError):
            manifest.update(error_kind=error.kind, http_status=error.http_status, api_code=error.api_code,
                            transport_error_type=error.transport_error_type,
                            failed_record_position=error.record_position,
                            failed_record_fields=error.record_fields)
        write_manifest(manifest_path, manifest)
        if isinstance(error, CollectionError):
            return 2
        raise


TAG_NOTE = (
    "Maoyan tag values are site view identifiers. The endpoint returns no view labels and "
    "this tool assigns none; confirm what each selected view shows on the movie page before "
    "interpreting results. Views can overlap, so reported totals must not be added."
)


def maoyan_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    ids = read_ids(args)
    if surface != "reviews" or len(ids) != 1:
        raise ValueError("Maoyan requires exactly one movie ID")
    try:
        movie_id = numeric_id(ids[0])
    except CollectionError as error:
        raise ValueError("movie ID must be positive ASCII digits") from error
    tags = parse_tags(args.tags)
    script = Path(__file__).resolve()
    command = [sys.executable, str(script), "--movie-id", movie_id, "--tags", ",".join(tags),
               "--max-items", str(args.max_items), "--max-pages", str(args.max_pages),
               "--output", str(output)]
    return Plan(created_at=utc_now(), platform="maoyan", surface="reviews", output=str(output),
                limits={"reviews": args.max_items, "pages_per_tag": args.max_pages, "tag_count": len(tags)},
                required_environment=["MAOYAN_PSEUDONYM_SALT"],
                steps=[Step("Maoyan reviews", command, str(script.parent),
                            pass_environment=["MAOYAN_PSEUDONYM_SALT"])],
                timeout_seconds=args.timeout_seconds, backend="maoyan-public",
                notes=[TAG_NOTE])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movie-id", required=True)
    parser.add_argument("--tags", default="0")
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        return collect_reviews(args.movie_id, Path(args.output), tags=args.tags, max_items=args.max_items,
                               max_pages=args.max_pages, salt=os.getenv("MAOYAN_PSEUDONYM_SALT", ""))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
