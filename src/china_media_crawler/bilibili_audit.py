"""Build per-video canonical layers and a quality audit from Bilibili runs."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_files(run: Path) -> tuple[Path | None, Path | None]:
    run = run.expanduser().resolve()
    if run.is_file():
        return run, None
    records = run / "records.jsonl"
    manifests = sorted(run.glob("bilibili_*_manifest.json"))
    return (records if records.is_file() else None), (manifests[0] if manifests else None)


def clean_record(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise ValueError("invalid Bilibili record")
    data = payload.get("platform_data")
    if not isinstance(data, dict) or not isinstance(data.get("bvid"), str):
        raise ValueError("Bilibili record is missing platform_data.bvid")
    record = dict(payload)
    for key in ("author", "user", "cookie", "salt"):
        record.pop(key, None)
    return record


def failure_class(manifest: dict[str, Any]) -> str | None:
    if manifest.get("failure_class"):
        return str(manifest["failure_class"])
    kind = manifest.get("error_kind")
    if kind in {"comment_pagination_unavailable", "unexpected_empty_comment_page",
                "repeated_cursor"}:
        return "pagination_unavailable"
    if kind == "connection_failed":
        return "env_blocked"
    if kind == "precheck_failed":
        return "env_blocked"
    if kind == "api_error" and manifest.get("api_code") in {-352, 412, 429, 401, 403}:
        return "rate_limited"
    if kind == "http_error" and manifest.get("http_status") in {401, 403, 412, 429}:
        return "rate_limited"
    if kind == "api_error" and manifest.get("api_code") == 62002:
        return "platform_invisible"
    if kind:
        return "schema_changed"
    return None


def failure_layer(manifest: dict[str, Any]) -> str | None:
    stage = manifest.get("failure_stage")
    if stage in {"comments_page", "comments_extract"}:
        return "comments"
    if stage == "video_metadata":
        return "detail"
    if stage in {"video_parts", "danmaku_part"}:
        return "danmaku"
    return None


def object_failure(bvid: str, item: dict[str, Any], source: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if (item.get("termination_reason") != "failed" and not item.get("failure_class")
            and not item.get("error_kind")):
        return None
    stage = item.get("failure_stage")
    stored_class = item.get("failure_class")
    derived_class = failure_class({
        "error_kind": item.get("error_kind"),
        "api_code": item.get("api_code"),
        "http_status": item.get("http_status"),
    })
    selected_class = stored_class if stored_class not in {None, "schema_changed"} else (derived_class or stored_class)
    return {
        "bvid": bvid, "layer": failure_layer({"failure_stage": stage}),
        "failure_class": selected_class or "unknown",
        "failure_stage": stage, "failed_page": item.get("failed_page"),
        "pagination_diagnostic": item.get("pagination_diagnostic"),
        "source_run": source,
    }


def audit_runs(runs: list[Path], output: Path) -> dict[str, Any]:
    if not runs:
        raise ValueError("at least one --run is required")
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("refusing to overwrite an existing audit directory")
    output.mkdir(parents=True)

    # bvid -> layer -> object_id -> record
    grouped: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    sources: dict[str, dict[str, set[str]]] = {}
    run_summaries = []
    failures = []
    for raw_run in runs:
        run = raw_run.expanduser().resolve()
        records_path, manifest_path = run_files(run)
        if records_path is None:
            raise ValueError(f"Bilibili records.jsonl was not found under {run}")
        manifest = {}
        if manifest_path is not None:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = run.name if run.parent.name == "runs" else f"{run.parent.name}/{run.name}"
        surface = str(manifest.get("surface") or "")
        count = 0
        for number, line in enumerate(records_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                raise ValueError(f"invalid JSON in {records_path.name} at line {number}") from None
            record = clean_record(payload)
            if record.get("platform") != "bilibili":
                continue
            bvid = record["platform_data"]["bvid"]
            layer = str(record.get("surface") or surface)
            if layer not in {"detail", "comments", "danmaku"}:
                raise ValueError(f"unexpected Bilibili surface at line {number}")
            object_id = str(record.get("object_id") or "")
            if not object_id:
                raise ValueError(f"missing object_id at line {number}")
            grouped.setdefault(bvid, {}).setdefault(layer, {})[object_id] = record
            sources.setdefault(bvid, {}).setdefault(layer, set()).add(source)
            count += 1
        failed = manifest.get("failed_object")
        cls = failure_class(manifest) if manifest.get("status") == "failed" else None
        if failed and cls:
            failures.append({
                "bvid": failed,
                "layer": failure_layer(manifest) or (surface if surface in {"detail", "comments", "danmaku"} else None),
                "failure_class": cls,
                "failure_stage": manifest.get("failure_stage"),
                "failed_page": manifest.get("failed_page"),
                "pagination_diagnostic": manifest.get("pagination_diagnostic"),
                "source_run": source,
            })
        object_failure_count = 0
        for item in manifest.get("objects", []):
            if not isinstance(item, dict):
                continue
            failure = object_failure(str(item.get("bvid") or ""), item, source)
            if failure and failure["bvid"]:
                object_failure_count += 1
                failures.append(failure)
        run_summaries.append({
            "source_run": source,
            "surface": surface or None,
            "status": manifest.get("status"),
            "record_count": count,
            "comments_mode": manifest.get("comments_mode"),
            "session_source": manifest.get("session_source"),
            "failure_class": cls,
            "failed_object_count": object_failure_count,
        })

    audit_rows = []
    for bvid in sorted(grouped):
        video_dir = output / "videos" / bvid
        video_dir.mkdir(parents=True, exist_ok=True)
        layers = grouped[bvid]
        for layer in ("detail", "comments", "danmaku"):
            records = list(layers.get(layer, {}).values())
            layer_failures = [
                failure for failure in failures
                if failure["bvid"] == bvid and failure.get("layer") == layer
            ]
            present = bool(records)
            complete = present and not layer_failures
            needs_retry = bool(layer_failures)
            source_runs = sorted(sources.get(bvid, {}).get(layer, set()))
            if layer == "detail" and records:
                latest = max(records, key=lambda item: str(item.get("captured_at") or ""))
                canonical = {
                    "bvid": bvid,
                    "title": latest.get("content"),
                    "published_at": latest.get("published_at"),
                    "captured_at": latest.get("captured_at"),
                    "author_pseudonym": latest.get("author_pseudonym"),
                    "engagement": latest.get("engagement"),
                    "platform_data": latest.get("platform_data"),
                    "source_runs": source_runs,
                }
                (video_dir / "video_info.json").write_text(
                    json.dumps(canonical, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            elif layer == "comments" and records:
                records = sorted(records, key=lambda item: (
                    str(item.get("published_at") or ""), str(item.get("object_id") or "")))
                main = sum(1 for item in records
                           if item.get("platform_data", {}).get("level") == 1)
                nested = sum(1 for item in records
                             if item.get("platform_data", {}).get("level") == 2)
                canonical = {
                    "bvid": bvid, "main_count": main, "nested_count": nested,
                    "source_runs": source_runs, "comments": records,
                }
                (video_dir / "comments.json").write_text(
                    json.dumps(canonical, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            elif layer == "danmaku" and records:
                records = sorted(records, key=lambda item: (
                    item.get("platform_data", {}).get("progress_seconds", 0),
                    str(item.get("object_id") or "")))
                canonical = {"bvid": bvid, "count": len(records),
                             "source_runs": source_runs, "danmaku": records}
                (video_dir / "danmaku.json").write_text(
                    json.dumps(canonical, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            row = {
                "bvid": bvid, "layer": layer, "present": present,
                "complete": complete, "needs_retry": needs_retry,
                "record_count": len(records), "unique_count": len(records),
                "main_count": sum(1 for item in records
                                  if item.get("platform_data", {}).get("level") == 1),
                "nested_count": sum(1 for item in records
                                    if item.get("platform_data", {}).get("level") == 2),
                "source_runs": ";".join(source_runs),
            }
            if layer_failures:
                failure = layer_failures[0]
                row.update(
                    failure_class=failure["failure_class"],
                    failure_stage=failure.get("failure_stage") or "",
                    failed_page=(failure.get("failed_page")
                                 if failure.get("failed_page") is not None else ""),
                )
                if failure.get("pagination_diagnostic") is not None:
                    row["pagination_diagnostic"] = json.dumps(
                        failure["pagination_diagnostic"], ensure_ascii=False, sort_keys=True)
            audit_rows.append(row)

    row_index = {(row["bvid"], row["layer"]): row for row in audit_rows}
    for failure in failures:
        layer = failure.get("layer") or "run"
        key = (failure["bvid"], layer)
        if key not in row_index:
            row = {
                "bvid": failure["bvid"], "layer": layer,
                "present": False, "complete": False, "needs_retry": True,
                "record_count": 0, "unique_count": 0,
                "main_count": 0, "nested_count": 0,
                "source_runs": failure["source_run"],
            }
            audit_rows.append(row)
            row_index[key] = row
        row = row_index[key]
        row["complete"] = False
        row["needs_retry"] = True
        row["failure_class"] = failure["failure_class"]
        row["failure_stage"] = failure.get("failure_stage") or ""
        row["failed_page"] = (failure.get("failed_page")
                              if failure.get("failed_page") is not None else "")
        if failure.get("pagination_diagnostic") is not None:
            row["pagination_diagnostic"] = json.dumps(
                failure["pagination_diagnostic"], ensure_ascii=False, sort_keys=True)
        if failure.get("source_run") and failure["source_run"] not in row.get("source_runs", "").split(";"):
            row["source_runs"] = ";".join(filter(None, [row.get("source_runs", ""), failure["source_run"]]))
    fields = ["bvid", "layer", "present", "complete", "needs_retry",
              "record_count", "unique_count", "main_count", "nested_count",
              "source_runs", "failure_class", "failure_stage", "failed_page",
              "pagination_diagnostic"]
    with (output / "quality_audit.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in audit_rows:
            writer.writerow({key: row.get(key, "") for key in fields})

    retry = [item for item in failures if item["failure_class"] in {
        "rate_limited", "env_blocked", "pagination_unavailable"}]
    (output / "retry_queue.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in retry),
        encoding="utf-8",
    )
    summary = {
        "generated_at": utc_now(),
        "output": str(output),
        "runs": run_summaries,
        "bvid_count": len(grouped),
        "layer_counts": dict(Counter(row["layer"] for row in audit_rows if row["present"])),
        "retry_count": len(retry),
        "failure_count": len(failures),
        "incomplete_count": sum(1 for row in audit_rows if not row["complete"]),
        "note": "Pseudonyms may use different per-run salts; do not compare authors across runs. "
                "Comments are bounded samples, not complete comment populations.",
    }
    (output / "audit.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", default=[], type=Path, required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = audit_runs(args.run, args.output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Audited {summary['bvid_count']} BVs; retry queue {summary['retry_count']}; output {summary['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())











