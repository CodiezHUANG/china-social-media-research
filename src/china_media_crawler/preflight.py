#!/usr/bin/env python3
"""Offline dependency checks for collectors."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PLATFORMS = ("xhs", "douyin", "weibo", "bilibili", "douban", "maoyan")


def result(platform: str, checks: list[tuple[str, bool, str]]) -> dict[str, Any]:
    return {
        "platform": platform,
        "ready": all(ok for _, ok, _ in checks),
        "checks": [
            {"name": name, "ok": ok, "detail": detail}
            for name, ok, detail in checks
        ],
    }


def run_probe(command: list[str], label: str,
              success_detail: str = "passed") -> tuple[bool, str]:
    try:
        completed = subprocess.run(command, capture_output=True, check=False, timeout=10)
    except subprocess.TimeoutExpired:
        return False, f"{label} check timed out"
    except OSError as error:
        return False, f"{label} check could not start: {type(error).__name__}"
    if completed.returncode != 0:
        return False, f"{label} check exited with code {completed.returncode}"
    return True, success_detail


def playwright_checks() -> list[tuple[str, bool, str]]:
    python = Path(sys.executable)
    package_ok, package_detail = run_probe(
        [str(python), "-c", "import playwright"], "Playwright package", "installed")
    browser_ok = False
    browser_detail = "Playwright unavailable"
    if package_ok:
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
    return [
        ("Playwright Python package", package_ok, package_detail),
        ("Playwright Chromium runtime", browser_ok, browser_detail),
    ]


SALT_PURPOSE = (
    "private value that replaces author IDs with stable pseudonyms; "
    "set it in this process environment, never in chat or on a command line"
)


def _salt_detail(present: bool) -> str:
    return "set" if present else f"not set: {SALT_PURPOSE}"


def _check_dependencies(platform: str, surface: str) -> dict[str, Any]:
    if platform == "bilibili":
        salt = bool(os.getenv("BILI_PSEUDONYM_SALT", "").strip())
        return result(platform, [
            ("Python standard library", True, "no external crawler or browser required"),
            ("BILI_PSEUDONYM_SALT", salt, _salt_detail(salt)),
        ])
    if platform == "maoyan":
        salt = bool(os.getenv("MAOYAN_PSEUDONYM_SALT", "").strip())
        return result(platform, [
            ("Python standard library", True, "no external crawler or browser required"),
            ("MAOYAN_PSEUDONYM_SALT", salt, _salt_detail(salt)),
        ])
    if platform == "douban":
        return result(platform, [
            ("Python standard library", True,
             "HTTP collection; Playwright is only needed for manual login"),
        ])
    if platform == "weibo":
        checks = [("Python standard library", True,
                   "hot uses HTTP; comments/search may use Playwright visitor session")]
        if surface in {"comments", "search"}:
            salt = bool(os.getenv("WEIBO_PSEUDONYM_SALT", "").strip())
            checks.append(("WEIBO_PSEUDONYM_SALT", salt, _salt_detail(salt)))
        return result(platform, checks)
    if platform == "douyin":
        salt = bool(os.getenv("DOUYIN_PSEUDONYM_SALT", "").strip())
        checks = [
            ("Python standard library", True, "search uses HTTP"),
            ("DOUYIN_PSEUDONYM_SALT", salt, _salt_detail(salt)),
        ]
        if surface == "detail":
            checks.extend(playwright_checks())
        return result(platform, checks)
    if platform == "xhs":
        checks = playwright_checks()
        salt = bool(os.getenv("XHS_PSEUDONYM_SALT", "").strip())
        checks.append(("XHS_PSEUDONYM_SALT", salt, _salt_detail(salt)))
        return result(platform, checks)
    raise ValueError(f"unsupported platform: {platform}")


def check_platform(platform: str, surface: str | None = None, *,
                   session: str = "auto", visitor_session: bool = False) -> dict[str, Any]:
    from china_media_crawler.authentication import auth_capability
    from china_media_crawler.configuration import DEFAULT_SURFACE
    from china_media_crawler.platforms import get_adapter

    surface = surface or DEFAULT_SURFACE.get(platform)
    adapter = get_adapter(platform, surface)
    authentication = auth_capability(platform, surface)
    item = _check_dependencies(platform, surface)
    if session not in {"auto", "required", "off"}:
        raise ValueError("unsupported session mode")
    if session != "auto" and not authentication.get("session_provider"):
        raise ValueError("this route does not support saved sessions")
    if session == "required":
        from china_media_crawler.authentication import get_session_provider
        from china_media_crawler.http import CollectionError

        cookie_name = authentication.get("cookie_environment", "")
        try:
            source, _ = get_session_provider(platform).collection_state(
                "required", os.getenv(cookie_name, "").strip())
            check = {"name": "required session", "ok": True,
                     "detail": source + "; online validity not checked"}
        except (CollectionError, OSError, ValueError) as error:
            check = {"name": "required session", "ok": False,
                     "detail": error.kind if isinstance(error, CollectionError) else "session_unavailable"}
        item["checks"].append(check)
        item["ready"] = item["ready"] and check["ok"]
    if visitor_session:
        if not (platform == "weibo" and surface in {"comments", "search"}):
            raise ValueError("visitor session requires Weibo comments or search")
        item["checks"].extend([
            {"name": name, "ok": ok, "detail": detail}
            for name, ok, detail in playwright_checks()
        ])
        item["ready"] = item["ready"] and all(check["ok"] for check in item["checks"])
        item["visitor_session"] = True
    item.update(surface=surface, backend=adapter.backend,
                authentication=authentication, online_validated=False)
    return item


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("all", *PLATFORMS), action="append")
    parser.add_argument("--surface")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    parser.add_argument("--visitor-session", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    requested = list(dict.fromkeys(args.platform or ["all"]))
    if "all" in requested and len(requested) != 1:
        raise SystemExit("--platform all cannot be combined with named platforms")
    selected = list(PLATFORMS) if requested == ["all"] else requested
    if (len(selected) != 1 or requested == ["all"]) and args.surface is not None:
        raise SystemExit("--surface requires one explicit --platform")
    if args.visitor_session and not (
        selected == ["weibo"] and args.surface in {"comments", "search"}
    ):
        raise SystemExit(
            "--visitor-session requires --platform weibo --surface comments|search"
        )
    try:
        results = [check_platform(platform, args.surface, session=args.session,
                                  visitor_session=args.visitor_session)
                   for platform in selected]
    except ValueError as error:
        raise SystemExit(str(error)) from error

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
