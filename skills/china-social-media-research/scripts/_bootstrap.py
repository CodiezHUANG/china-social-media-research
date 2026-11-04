"""Resolve the core package for plugin entry points."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_MISSING = """\
Cannot import china_media_crawler.

This skill directory is not self-contained. Use one of these installation routes:
  1. From a full checkout, run: python -m pip install -e ".[browser]"
  2. Keep the full checkout available and set CMC_ROOT to its root directory.
  3. Install the china-media-crawler package in this Python environment.

A copied skill subdirectory alone does not include the core package.
"""


def bootstrap() -> None:
    configured = os.environ.get("CMC_ROOT", "").strip()
    if configured:
        source = Path(configured).expanduser().resolve() / "src"
        if not (source / "china_media_crawler").is_dir():
            raise SystemExit(
                "CMC_ROOT must point to a checkout containing "
                "src/china_media_crawler.\n"
            )
        sys.path.insert(0, str(source))
        return

    source = Path(__file__).resolve().parents[3] / "src"
    if (source / "china_media_crawler").is_dir():
        sys.path.insert(0, str(source))
        return

    try:
        import china_media_crawler
    except ModuleNotFoundError as error:
        if error.name == "china_media_crawler" or (
            error.name and error.name.startswith("china_media_crawler.")
        ):
            raise SystemExit(_MISSING) from None
        raise
