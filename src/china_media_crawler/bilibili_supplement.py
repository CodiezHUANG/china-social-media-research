"""Preview or execute missing-layer / retry supplement for Bilibili audits."""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from china_media_crawler.platforms.bilibili_content import collect_content


def failure_layer(item: dict[str, Any]) -> str | None:
    if item.get("layer") in {"detail", "comments", "danmaku"}:
        return str(item["layer"])
    stage = item.get("failure_stage")
    if stage in {"comments_page", "comments_extract"}:
        return "comments"
    if stage == "video_metadata":
        return "detail"
    if stage in {"video_parts", "danmaku_part"}:
        return "danmaku"
    source = str(item.get("source_run") or "")
    for layer in ("detail", "comments", "danmaku"):
        if source.endswith(layer) or f"/{layer}" in source:
            return layer
    return None


def load_targets(audit: Path, layers: set[str], retry_only: bool,
                 max_items: int) -> dict[str, list[str]]:
    audit = audit.expanduser().resolve()
    quality = audit / "quality_audit.csv"
    if not quality.is_file():
        raise ValueError("quality_audit.csv was not found in the audit directory")
    missing: dict[str, set[str]] = defaultdict(set)
    with quality.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            layer = str(row.get("layer") or "")
            if layer not in layers:
                continue
            present = str(row.get("present")).lower() == "true"
            complete_text = str(row.get("complete") or "").strip().lower()
            complete = present if not complete_text else complete_text == "true"
            needs_retry = str(row.get("needs_retry") or "").strip().lower() == "true"
            if present and complete and not needs_retry:
                continue
            bvid = str(row.get("bvid") or "")
            if bvid:
                missing[layer].add(bvid)
    retry: dict[str, set[str]] = defaultdict(set)
    retry_path = audit / "retry_queue.jsonl"
    if retry_path.is_file():
        for line in retry_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            layer = failure_layer(item)
            if layer in layers and item.get("bvid"):
                retry[layer].add(str(item["bvid"]))
    targets = retry if retry_only else {
        layer: missing.get(layer, set()) | retry.get(layer, set())
        for layer in layers
    }
    result = {}
    for layer in sorted(targets):
        bvids = sorted(targets[layer])[:max_items]
        if bvids:
            result[layer] = bvids
    return result


def supplement(audit: Path, output: Path, *, layers: set[str], retry_only: bool,
               max_items: int, comments_mode: str, comments_per_item: int,
               max_pages: int, danmaku_per_item: int, max_parts: int,
               session_mode: str, execute: bool) -> dict[str, Any]:
    if max_items < 1:
        raise ValueError("max-items must be positive")
    targets = load_targets(audit, layers, retry_only, max_items)
    if not targets:
        raise ValueError("no supplement targets were found")
    summary: dict[str, Any] = {
        "audit": str(Path(audit).expanduser().resolve()),
        "output": str(Path(output).expanduser().resolve()),
        "retry_only": retry_only,
        "targets": targets,
        "results": [],
    }
    if not execute:
        summary["status"] = "preview"
        return summary
    if not os.getenv("BILI_PSEUDONYM_SALT", "").strip():
        raise ValueError("BILI_PSEUDONYM_SALT is required for supplement execution")
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    failures = 0
    for layer, bvids in targets.items():
        layer_output = output / layer
        try:
            code = collect_content(
                bvids, layer_output, surface=layer,
                salt=os.getenv("BILI_PSEUDONYM_SALT", "").strip(),
                max_items=len(bvids), comments_per_item=comments_per_item,
                max_pages=max_pages, danmaku_per_item=danmaku_per_item,
                max_parts=max_parts, session_mode=session_mode,
                comments_mode=comments_mode if layer == "comments" else "first_page",
                precheck=True, on_item_error="continue",
            )
            entry = {"layer": layer, "bvids": bvids, "output": str(layer_output),
                     "returncode": code,
                     "status": "complete" if code == 0 else
                               "complete_with_failures" if code == 1 else "failed"}
            failures += 1 if code else 0
        except (OSError, ValueError) as error:
            entry = {"layer": layer, "bvids": bvids, "output": str(layer_output),
                     "returncode": 2, "status": "failed",
                     "error_type": type(error).__name__}
            failures += 1
        summary["results"].append(entry)
    summary["status"] = "complete" if failures == 0 else "complete_with_failures"
    (output / "supplement_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--layer", action="append", choices=("detail", "comments", "danmaku"),
                        default=[])
    parser.add_argument("--retry-only", action="store_true")
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--comments-mode", choices=("first_page", "deep"), default="first_page")
    parser.add_argument("--comments-per-item", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--danmaku-per-item", type=int, default=100)
    parser.add_argument("--max-parts", type=int, default=1)
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    layers = set(args.layer) if args.layer else {"detail", "comments", "danmaku"}
    try:
        summary = supplement(
            args.audit, args.output, layers=layers, retry_only=args.retry_only,
            max_items=args.max_items, comments_mode=args.comments_mode,
            comments_per_item=args.comments_per_item, max_pages=args.max_pages,
            danmaku_per_item=args.danmaku_per_item, max_parts=args.max_parts,
            session_mode=args.session, execute=args.execute,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"{summary['status']}: " + ", ".join(
            f"{layer}={len(bvids)}" for layer, bvids in summary["targets"].items()))
    return 0 if summary["status"] in {"preview", "complete"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

