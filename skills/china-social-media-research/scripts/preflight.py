#!/usr/bin/env python3
"""Offline dependency checks for the China social media research skill."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PLATFORMS = ("xhs", "douyin", "weibo", "bilibili", "douban", "maoyan")


def venv_python(root: Path) -> Path | None:
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    return next((path for path in candidates if path.is_file()), None)


def path_from_env(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def result(platform: str, checks: list[tuple[str, bool, str]]) -> dict[str, Any]:
    return {
        "platform": platform,
        "ready": all(ok for _, ok, _ in checks),
        "checks": [
            {"name": name, "ok": ok, "detail": detail}
            for name, ok, detail in checks
        ],
    }


def run_probe(
    command: list[str],
    label: str,
    success_detail: str = "passed",
) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, f"{label} check timed out"
    except OSError as error:
        return False, f"{label} check could not start: {type(error).__name__}"
    if completed.returncode != 0:
        return False, f"{label} check exited with code {completed.returncode}"
    return True, success_detail


def check_platform(platform: str) -> dict[str, Any]:
    if platform in {"xhs", "douyin"}:
        root = path_from_env("MEDIACRAWLER_ROOT")
        return result(
            platform,
            [
                ("MEDIACRAWLER_ROOT", root is not None, str(root or "not set")),
                (
                    "MediaCrawler main.py",
                    bool(root and (root / "main.py").is_file()),
                    str((root / "main.py") if root else "unavailable"),
                ),
                (
                    "MediaCrawler virtualenv",
                    bool(root and venv_python(root)),
                    str(venv_python(root) if root else "unavailable"),
                ),
            ],
        )

    if platform == "weibo":
        media_root = path_from_env("MEDIACRAWLER_ROOT")
        cli = path_from_env("WEIBO_CLI_BIN")
        media_ok = bool(
            media_root
            and (media_root / "main.py").is_file()
            and venv_python(media_root)
        )
        return result(
            platform,
            [
                (
                    "MediaCrawler search backend",
                    media_ok,
                    str(media_root or "MEDIACRAWLER_ROOT not set"),
                ),
                (
                    "weibo-cli comments/hot backend",
                    bool(cli and cli.is_file()),
                    str(cli or "WEIBO_CLI_BIN not set"),
                ),
            ],
        )

    if platform == "bilibili":
        root = path_from_env("BILIBILI_CRAWLER_ROOT")
        return result(
            platform,
            [
                ("BILIBILI_CRAWLER_ROOT", root is not None, str(root or "not set")),
                (
                    "search script",
                    bool(root and (root / "crawl" / "search.py").is_file()),
                    str((root / "crawl" / "search.py") if root else "unavailable"),
                ),
                (
                    "detail script",
                    bool(root and (root / "crawl" / "main.py").is_file()),
                    str((root / "crawl" / "main.py") if root else "unavailable"),
                ),
                (
                    "Bilibili virtualenv",
                    bool(root and venv_python(root)),
                    str(venv_python(root) if root else "unavailable"),
                ),
                (
                    "BILI_PSEUDONYM_SALT",
                    bool(os.getenv("BILI_PSEUDONYM_SALT", "").strip()),
                    "set" if os.getenv("BILI_PSEUDONYM_SALT", "").strip() else "not set",
                ),
            ],
        )

    if platform == "douban":
        configured_python = path_from_env("DOUBAN_PYTHON")
        media_root = path_from_env("MEDIACRAWLER_ROOT")
        python = (
            configured_python
            or (venv_python(media_root) if media_root else None)
            or Path(sys.executable)
        )
        python_ok = python.is_file()
        playwright_ok = False
        browser_ok = False
        detail = "Python interpreter unavailable"
        browser_detail = "Playwright unavailable"
        if python_ok:
            playwright_ok, detail = run_probe(
                [str(python), "-c", "import playwright"],
                "Playwright package",
                "installed",
            )
        if playwright_ok:
            browser_ok, browser_detail = run_probe(
                [
                    str(python),
                    "-c",
                    (
                        "from pathlib import Path; "
                        "from playwright.sync_api import sync_playwright; "
                        "p=sync_playwright().start(); e=p.chromium.executable_path; p.stop(); "
                        "raise SystemExit(0 if Path(e).is_file() else 2)"
                    ),
                ],
                "Playwright Chromium runtime",
                "installed",
            )
        return result(
            platform,
            [
                ("Python", python_ok, str(python)),
                ("Playwright Python package", playwright_ok, detail),
                ("Playwright Chromium runtime", browser_ok, browser_detail),
            ],
        )

    if platform == "maoyan":
        root = path_from_env("MAOYAN_CRAWLER_ROOT")
        python = venv_python(root) if root else None
        backend_script = root / "tag_pool_runner.py" if root else None
        wrapper = Path(__file__).with_name("maoyan_runner.py")
        compatibility_ok = False
        compatibility_detail = "backend unavailable"
        if python and backend_script and backend_script.is_file() and wrapper.is_file():
            compatibility_ok, compatibility_detail = run_probe(
                [
                    str(python),
                    str(wrapper),
                    "--backend-root",
                    str(root),
                    "--",
                    "--help",
                ],
                "Maoyan wrapper compatibility",
            )
        return result(
            platform,
            [
                ("MAOYAN_CRAWLER_ROOT", root is not None, str(root or "not set")),
                (
                    "tag-pool runner",
                    bool(root and (root / "tag_pool_runner.py").is_file()),
                    str((root / "tag_pool_runner.py") if root else "unavailable"),
                ),
                (
                    "Maoyan virtualenv",
                    bool(python),
                    str(python or "unavailable"),
                ),
                (
                    "Maoyan wrapper compatibility",
                    compatibility_ok,
                    compatibility_detail,
                ),
                (
                    "MAOYAN_PSEUDONYM_SALT",
                    bool(os.getenv("MAOYAN_PSEUDONYM_SALT", "").strip()),
                    "set" if os.getenv("MAOYAN_PSEUDONYM_SALT", "").strip() else "not set",
                ),
            ],
        )

    raise ValueError(f"unsupported platform: {platform}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check crawler dependencies without making network requests."
    )
    parser.add_argument("--platform", choices=("all", *PLATFORMS), default="all")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected = PLATFORMS if args.platform == "all" else (args.platform,)
    results = [check_platform(platform) for platform in selected]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for item in results:
            status = "READY" if item["ready"] else "NOT READY"
            print(f"[{status}] {item['platform']}")
            for check in item["checks"]:
                marker = "ok" if check["ok"] else "missing"
                print(f"  - {marker}: {check['name']} ({check['detail']})")

    return 0 if all(item["ready"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
