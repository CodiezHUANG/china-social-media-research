#!/usr/bin/env python3
"""Validate and cap a Bilibili search sampling frame."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def load_raw(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"Bilibili raw search output was not created: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from error
        if not isinstance(record, dict) or not str(record.get("bvid") or "").strip():
            raise ValueError(f"unexpected Bilibili search schema at {path}:{line_number}")
        records.append(record)
    return records


def normalize(candidates: Path, raw: Path, max_items: int) -> int:
    if not candidates.is_file():
        raise ValueError(f"Bilibili candidate file was not created: {candidates}")

    candidate_ids = [
        line.strip()
        for line in candidates.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    raw_records = load_raw(raw)

    raw_by_id: dict[str, dict[str, Any]] = {}
    for record in raw_records:
        bvid = str(record["bvid"]).strip()
        raw_by_id.setdefault(bvid, record)

    selected: list[str] = []
    for bvid in candidate_ids:
        if bvid not in selected:
            selected.append(bvid)
        if len(selected) >= max_items:
            break

    selected_records = [raw_by_id[bvid] for bvid in selected if bvid in raw_by_id]
    if len(selected_records) != len(selected):
        missing = sorted(set(selected) - set(raw_by_id))
        raise ValueError("raw search metadata is missing for candidate(s): " + ", ".join(missing))

    atomic_write_text(candidates, "".join(f"{bvid}\n" for bvid in selected))
    atomic_write_text(
        raw,
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in selected_records),
    )
    return len(selected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--failed", required=True)
    parser.add_argument("--max-items", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.max_items <= 200:
        raise SystemExit("max-items must be between 1 and 200")

    candidates = Path(args.candidates).expanduser().resolve()
    raw = Path(args.raw).expanduser().resolve()
    failed = Path(args.failed).expanduser().resolve()
    try:
        count = normalize(candidates, raw, args.max_items)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    if failed.is_file() and failed.stat().st_size:
        print(
            f"Bilibili search returned {count} candidate(s), but at least one page failed; "
            f"inspect {failed}",
            file=sys.stderr,
        )
        return 3

    print(f"Validated Bilibili sampling frame: {count} unique candidate(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
