from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from china_media_crawler.configuration import (
    read_ids, require_file,
)
from china_media_crawler.manifest import utc_now
from china_media_crawler.models import Plan, Step

def selected_python() -> tuple[Path, list[str]]:
    configured_python = os.getenv("DOUBAN_PYTHON", "").strip()
    if configured_python:
        python = require_file(
            Path(configured_python).expanduser().resolve(), "DOUBAN_PYTHON"
        )
        required = ["DOUBAN_PYTHON"]
    else:
        python = Path(sys.executable)
        required = []
    return python, required


def douban_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    ids = read_ids(args)
    if len(ids) != 1:
        raise ValueError("Douban collection requires exactly one --item-id subject ID")
    if not re.fullmatch(r"\d+", ids[0]):
        raise ValueError("Douban subject ID must contain digits only")
    adapter = require_file(Path(__file__).with_name("douban_reviews.py"), "bundled Douban adapter")
    python, required = selected_python()
    command = [
        str(python),
        str(adapter),
        "--subject-id",
        ids[0],
        "--surface",
        surface,
        "--max-pages",
        str(args.max_pages),
        "--max-items",
        str(args.max_items),
        "--session",
        args.session,
        "--output",
        str(output),
        "--min-delay",
        "2.5",
        "--max-delay",
        "5.0",
    ]
    return Plan(
        created_at=utc_now(),
        platform="douban",
        surface=surface,
        output=str(output),
        limits={"pages": args.max_pages, "records": args.max_items, "comments_per_page": 20},
        required_environment=required,
        steps=[
            Step(
                "Douban bounded HTTP collection",
                command,
                str(adapter.parent),
                pass_environment=["DOUBAN_COOKIE", "DOUBAN_PSEUDONYM_SALT"],
            )
        ],
        timeout_seconds=args.timeout_seconds,
    )

