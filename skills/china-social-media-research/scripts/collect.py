#!/usr/bin/env python3
"""Preview or execute one bounded public-data collection.

The script is intentionally an orchestration layer. With the exception of the
small bundled Douban adapter, crawler engines are installed separately and
located through environment variables containing filesystem paths.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


PLATFORM_SURFACES = {
    "xhs": {"search", "detail"},
    "douyin": {"search", "detail"},
    "weibo": {"search", "comments", "hot"},
    "bilibili": {"search", "detail"},
    "douban": {"reviews", "wantsee"},
    "maoyan": {"reviews"},
}

DEFAULT_SURFACE = {
    "xhs": "search",
    "douyin": "search",
    "weibo": "search",
    "bilibili": "search",
    "douban": "reviews",
    "maoyan": "reviews",
}

SAFE_INHERITED_ENVIRONMENT = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PROGRAMDATA",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: str, label: str) -> str:
    value = value.strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a non-empty single-line value")
    return value


def configured_path(name: str) -> Path:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; run preflight.py first")
    return Path(value).expanduser().resolve()


def backend_python(root: Path, backend: str) -> Path:
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(f"{backend} virtualenv interpreter was not found under {root}")


def require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise ValueError(f"{label} was not found: {path}")
    return path


def read_ids(args: argparse.Namespace) -> list[str]:
    ids = [clean_text(value, "item ID") for value in args.item_id]
    if args.input_file:
        source = Path(args.input_file).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"input file was not found: {source}")
        for line in source.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                ids.append(clean_text(value, "item ID"))
    return list(dict.fromkeys(ids))


def read_urls(args: argparse.Namespace) -> list[str]:
    urls = [clean_text(value, "item URL") for value in args.item_url]
    if args.input_file:
        source = Path(args.input_file).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"input file was not found: {source}")
        for line in source.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                urls.append(clean_text(value, "item URL"))
    return list(dict.fromkeys(urls))


def validate_xhs_note_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    params = parse_qs(parsed.query)
    if (
        parsed.scheme not in {"http", "https"}
        or not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
        or "/explore/" not in parsed.path
        or not params.get("xsec_token")
        or not params.get("xsec_source")
    ):
        raise ValueError(
            "Xiaohongshu detail requires a full note URL containing "
            "xsec_token and xsec_source; use --item-url, not a bare ID"
        )
    return value


@dataclass
class Step:
    label: str
    command: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    pass_environment: list[str] = field(default_factory=list)
    stdout_file: str | None = None


@dataclass
class Plan:
    created_at: str
    platform: str
    surface: str
    output: str
    limits: dict[str, int]
    required_environment: list[str]
    steps: list[Step]
    timeout_seconds: int = 1800
    visible_browser: bool = False
    visible_browser_approved: bool = False
    live_network_action: bool = True


def media_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    root = configured_path("MEDIACRAWLER_ROOT")
    python = backend_python(root, "MediaCrawler")
    main_script = require_file(root / "main.py", "MediaCrawler main.py")
    code = {"xhs": "xhs", "douyin": "dy", "weibo": "wb"}[args.platform]
    command = [
        str(python),
        str(main_script),
        "--platform",
        code,
        "--lt",
        args.login,
        "--type",
        surface,
        "--headless",
        "yes" if args.headless else "no",
    ]

    if surface == "search":
        if not args.query:
            raise ValueError("at least one --query is required for search")
        queries = [clean_text(value, "query") for value in args.query]
        command += ["--keywords", ",".join(queries)]
    else:
        if args.platform == "xhs":
            if args.item_id:
                raise ValueError("Xiaohongshu detail accepts --item-url, not --item-id")
            targets = [validate_xhs_note_url(value) for value in read_urls(args)]
            if not targets:
                raise ValueError("--item-url or --input-file is required for Xiaohongshu detail")
        else:
            targets = read_ids(args)
            if not targets:
                raise ValueError("--item-id or --input-file is required for detail")
        command += ["--specified_id", ",".join(targets[: args.max_items])]

    command += [
        "--crawler_max_notes_count",
        str(args.max_items),
        "--get_comment",
        "yes" if args.comments_per_item else "no",
        "--get_sub_comment",
        "no",
        "--max_comments_count_singlenotes",
        str(args.comments_per_item),
        "--max_concurrency_num",
        "1",
        "--enable_ip_proxy",
        "no",
        "--save_data_option",
        "jsonl",
        "--save_data_path",
        str(output),
    ]
    required = ["MEDIACRAWLER_ROOT"]
    if args.login == "cookie":
        required.append("MEDIACRAWLER_COOKIES")
    return Plan(
        created_at=utc_now(),
        platform=args.platform,
        surface=surface,
        output=str(output),
        limits={"items": args.max_items, "comments_per_item": args.comments_per_item},
        required_environment=required,
        steps=[
            Step(
                "MediaCrawler collection",
                command,
                str(root),
                pass_environment=["MEDIACRAWLER_COOKIES"] if args.login == "cookie" else [],
            )
        ],
        timeout_seconds=args.timeout_seconds,
        visible_browser=not args.headless,
        visible_browser_approved=args.allow_visible_browser,
    )


def weibo_cli_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    cli = require_file(configured_path("WEIBO_CLI_BIN"), "weibo-cli binary")
    base = [str(cli)]
    steps: list[Step] = []
    if surface == "hot":
        command = base + [
            "hot",
            "-n",
            str(args.max_items),
            "-o",
            "jsonl",
            "--rate",
            "3s",
            "--retries",
            "0",
            "--timeout",
            "15s",
        ]
        steps.append(
            Step(
                "Weibo hot snapshot",
                command,
                str(cli.parent),
                stdout_file="weibo_hot.jsonl",
            )
        )
    else:
        ids = read_ids(args)
        if not ids:
            raise ValueError("--item-id or --input-file is required for Weibo comments")
        for index, item_id in enumerate(ids[: args.max_items], start=1):
            command = base + [
                "comments",
                item_id,
                "-n",
                str(args.comments_per_item),
                "-o",
                "jsonl",
                "--rate",
                "3s",
                "--retries",
                "0",
                "--timeout",
                "15s",
            ]
            steps.append(
                Step(
                    f"Weibo comments {index}",
                    command,
                    str(cli.parent),
                    stdout_file=f"comments_{index:03d}.jsonl",
                )
            )
    return Plan(
        created_at=utc_now(),
        platform="weibo",
        surface=surface,
        output=str(output),
        limits={"items": args.max_items, "comments_per_item": args.comments_per_item},
        required_environment=["WEIBO_CLI_BIN"],
        steps=steps,
        timeout_seconds=args.timeout_seconds,
    )


def bilibili_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    root = configured_path("BILIBILI_CRAWLER_ROOT")
    python = backend_python(root, "Bilibili crawler")
    crawl_dir = root / "crawl"
    if surface == "search":
        if len(args.query) != 1:
            raise ValueError("Bilibili search requires exactly one --query")
        script = require_file(crawl_dir / "search.py", "Bilibili search script")
        captured_at = utc_now()
        candidates_path = output / "bv_candidates.txt"
        raw_path = output / "search_result_raw.jsonl"
        failed_path = output / "failed_search.jsonl"
        env = {
            "BILI_KEYWORD": clean_text(args.query[0], "query"),
            "BILI_SEARCH_PAGES": str(max(1, math.ceil(args.max_items / 20))),
            "BILI_MAX_VIDEOS": str(args.max_items),
            "BILI_CAPTURE_TIME": captured_at,
            "BILI_RESULT_FILE": str(candidates_path),
            "BILI_SEARCH_RAW_FILE": str(raw_path),
            "BILI_FAILED_SEARCH_FILE": str(failed_path),
        }
        frame_script = require_file(
            Path(__file__).with_name("bilibili_search_frame.py"),
            "bundled Bilibili search-frame normalizer",
        )
        steps = [
            Step(
                "Bilibili search",
                [str(python), str(script)],
                str(root),
                env,
                pass_environment=["BILIBILI_COOKIE", "BILI_PSEUDONYM_SALT"],
            ),
            Step(
                "Bilibili search-frame validation",
                [
                    str(python),
                    str(frame_script),
                    "--candidates",
                    str(candidates_path),
                    "--raw",
                    str(raw_path),
                    "--failed",
                    str(failed_path),
                    "--max-items",
                    str(args.max_items),
                ],
                str(frame_script.parent),
            ),
        ]
        required = ["BILIBILI_CRAWLER_ROOT", "BILI_PSEUDONYM_SALT"]
    else:
        ids = read_ids(args)
        if not ids:
            raise ValueError("--input-file or --item-id is required for Bilibili detail")
        if not args.input_file:
            raise ValueError(
                "Bilibili detail requires --input-file so the approved frame is auditable"
            )
        source = Path(args.input_file).expanduser().resolve()
        script = require_file(crawl_dir / "main.py", "Bilibili detail script")
        env = {
            "BILI_RESULT_FILE": str(source),
            "BILI_OUTPUT_DIR": str(output),
            "BILI_MAX_VIDEOS": str(min(args.max_items, len(ids))),
            "BILI_MAX_WORKERS": "1",
            "BILI_SKIP_DANMAKU": "0",
        }
        steps = [
            Step(
                "Bilibili detail, comments, and danmaku",
                [str(python), str(script)],
                str(root),
                env,
                pass_environment=["BILIBILI_COOKIE", "BILI_PSEUDONYM_SALT"],
            )
        ]
        required = ["BILIBILI_CRAWLER_ROOT", "BILI_PSEUDONYM_SALT"]

    return Plan(
        created_at=utc_now(),
        platform="bilibili",
        surface=surface,
        output=str(output),
        limits={"items": args.max_items, "comments_per_item": args.comments_per_item},
        required_environment=required,
        steps=steps,
        timeout_seconds=args.timeout_seconds,
    )


def douban_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    ids = read_ids(args)
    if len(ids) != 1:
        raise ValueError("Douban collection requires exactly one --item-id subject ID")
    if not re.fullmatch(r"\d+", ids[0]):
        raise ValueError("Douban subject ID must contain digits only")
    adapter = require_file(Path(__file__).with_name("douban_reviews.py"), "bundled Douban adapter")
    configured_python = os.getenv("DOUBAN_PYTHON", "").strip()
    media_root_value = os.getenv("MEDIACRAWLER_ROOT", "").strip()
    if configured_python:
        python = require_file(
            Path(configured_python).expanduser().resolve(), "DOUBAN_PYTHON"
        )
        required = ["DOUBAN_PYTHON"]
    elif media_root_value:
        python = backend_python(
            Path(media_root_value).expanduser().resolve(), "MediaCrawler for Douban"
        )
        required = ["MEDIACRAWLER_ROOT"]
    else:
        python = Path(sys.executable)
        required = []
    command = [
        str(python),
        str(adapter),
        "--subject-id",
        ids[0],
        "--surface",
        surface,
        "--max-pages",
        str(args.max_pages),
        "--output",
        str(output),
        "--min-delay",
        "2.5",
        "--max-delay",
        "5.0",
    ]
    if args.headless:
        command.append("--headless")
    return Plan(
        created_at=utc_now(),
        platform="douban",
        surface=surface,
        output=str(output),
        limits={"pages": args.max_pages, "comments_per_item": 20},
        required_environment=required,
        steps=[
            Step(
                "Douban bounded Playwright collection",
                command,
                str(adapter.parent),
                pass_environment=["DOUBAN_COOKIE", "DOUBAN_PSEUDONYM_SALT"],
            )
        ],
        timeout_seconds=args.timeout_seconds,
        visible_browser=not args.headless,
        visible_browser_approved=args.allow_visible_browser,
    )


def maoyan_plan(args: argparse.Namespace, output: Path, surface: str) -> Plan:
    ids = read_ids(args)
    if len(ids) != 1:
        raise ValueError("Maoyan collection requires exactly one --item-id movie ID")
    if not re.fullmatch(r"\d+", ids[0]):
        raise ValueError("Maoyan movie ID must contain digits only")
    tags = [value.strip() for value in args.tags.split(",") if value.strip()]
    if not tags or any(not re.fullmatch(r"\d+", value) for value in tags):
        raise ValueError("--tags must be a comma-separated list of non-negative integers")
    root = configured_path("MAOYAN_CRAWLER_ROOT")
    python = backend_python(root, "Maoyan crawler")
    require_file(root / "tag_pool_runner.py", "Maoyan tag-pool runner")
    runner = require_file(
        Path(__file__).with_name("maoyan_runner.py"),
        "bundled Maoyan runner",
    )
    ceiling = args.max_pages * 15
    command = [
        str(python),
        str(runner),
        "--backend-root",
        str(root),
        "--",
        "--movie-id",
        ids[0],
        "--output-dir",
        str(output),
        "--tags",
        ",".join(tags),
        "--ceiling",
        str(ceiling),
        "--min-delay",
        "2.5",
        "--max-delay",
        "5.0",
    ]
    return Plan(
        created_at=utc_now(),
        platform="maoyan",
        surface=surface,
        output=str(output),
        limits={"pages_per_tag": args.max_pages, "tag_count": len(tags)},
        required_environment=["MAOYAN_CRAWLER_ROOT", "MAOYAN_PSEUDONYM_SALT"],
        steps=[
            Step(
                "Maoyan tag-pool reviews",
                command,
                str(runner.parent),
                pass_environment=["MAOYAN_PSEUDONYM_SALT"],
            )
        ],
        timeout_seconds=args.timeout_seconds,
    )


def build_plan(args: argparse.Namespace) -> Plan:
    surface = args.surface or DEFAULT_SURFACE[args.platform]
    if surface not in PLATFORM_SURFACES[args.platform]:
        allowed = ", ".join(sorted(PLATFORM_SURFACES[args.platform]))
        raise ValueError(f"surface {surface!r} is invalid for {args.platform}; choose {allowed}")
    if not 1 <= args.max_items <= 200:
        raise ValueError("--max-items must be between 1 and 200")
    if not 0 <= args.comments_per_item <= 100:
        raise ValueError("--comments-per-item must be between 0 and 100")
    if not 1 <= args.max_pages <= 5:
        raise ValueError("--max-pages must be between 1 and 5")
    if not 30 <= args.timeout_seconds <= 7200:
        raise ValueError("--timeout-seconds must be between 30 and 7200")
    if surface == "comments" and args.comments_per_item == 0:
        raise ValueError("--comments-per-item must be positive for a comments collection")
    if args.item_url and not (args.platform == "xhs" and surface == "detail"):
        raise ValueError("--item-url is only valid for Xiaohongshu detail collection")

    output = Path(args.output).expanduser().resolve()
    if args.platform in {"xhs", "douyin"} or (args.platform == "weibo" and surface == "search"):
        return media_plan(args, output, surface)
    if args.platform == "weibo":
        return weibo_cli_plan(args, output, surface)
    if args.platform == "bilibili":
        return bilibili_plan(args, output, surface)
    if args.platform == "douban":
        return douban_plan(args, output, surface)
    if args.platform == "maoyan":
        return maoyan_plan(args, output, surface)
    raise ValueError(f"unsupported platform: {args.platform}")


def plan_dict(plan: Plan) -> dict[str, Any]:
    return {
        **asdict(plan),
        "steps": [asdict(step) for step in plan.steps],
    }


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def child_environment(plan: Plan, step: Step) -> dict[str, str]:
    inherited_names = SAFE_INHERITED_ENVIRONMENT | set(step.pass_environment)
    environment = {
        name: value
        for name in inherited_names
        if (value := os.getenv(name)) is not None
    }
    environment.update(step.env)
    return environment


def scrub_maoyan_checkpoint(output: Path) -> None:
    checkpoint = output / "checkpoint.json"
    if not checkpoint.is_file():
        return
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    if payload.pop("pseudonym_salt", None) is not None:
        write_manifest(checkpoint, payload)


def execute(plan: Plan) -> int:
    if plan.visible_browser and not plan.visible_browser_approved:
        raise ValueError(
            "this plan launches a visible browser; preview it, then add "
            "--allow-visible-browser before --execute"
        )
    output = Path(plan.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty; choose a new run path: {output}")
    output.mkdir(parents=True, exist_ok=True)

    missing = [name for name in plan.required_environment if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("required environment variable(s) are not set: " + ", ".join(missing))

    manifest_path = output / "run_manifest.json"
    manifest = plan_dict(plan)
    manifest.update({"status": "running", "started_at": utc_now(), "results": []})
    write_manifest(manifest_path, manifest)

    try:
        for step in plan.steps:
            environment = child_environment(plan, step)
            stdout_handle = None
            try:
                if step.stdout_file:
                    stdout_path = output / step.stdout_file
                    stdout_handle = stdout_path.open("xb")
                completed = subprocess.run(
                    step.command,
                    cwd=step.cwd,
                    env=environment,
                    stdout=stdout_handle,
                    check=False,
                    timeout=plan.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                manifest["results"].append(
                    {
                        "label": step.label,
                        "status": "timed_out",
                        "timeout_seconds": plan.timeout_seconds,
                        "finished_at": utc_now(),
                    }
                )
                manifest["status"] = "timed_out"
                manifest["finished_at"] = utc_now()
                write_manifest(manifest_path, manifest)
                return 124
            finally:
                if stdout_handle:
                    stdout_handle.close()
                if plan.platform == "maoyan":
                    scrub_maoyan_checkpoint(output)
            manifest["results"].append(
                {"label": step.label, "returncode": completed.returncode, "finished_at": utc_now()}
            )
            write_manifest(manifest_path, manifest)
            if completed.returncode != 0:
                manifest["status"] = "failed"
                manifest["finished_at"] = utc_now()
                write_manifest(manifest_path, manifest)
                return completed.returncode
    except BaseException as error:
        manifest["status"] = "failed"
        manifest["finished_at"] = utc_now()
        manifest["error_type"] = type(error).__name__
        write_manifest(manifest_path, manifest)
        raise

    manifest["status"] = "complete"
    manifest["finished_at"] = utc_now()
    write_manifest(manifest_path, manifest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=tuple(PLATFORM_SURFACES))
    parser.add_argument(
        "--surface",
        help="platform surface; defaults to the platform's search/review surface",
    )
    parser.add_argument(
        "--query", action="append", default=[], help="repeat for multiple search queries"
    )
    parser.add_argument(
        "--item-id", action="append", default=[], help="repeat for multiple public IDs"
    )
    parser.add_argument(
        "--item-url",
        action="append",
        default=[],
        help="repeat for Xiaohongshu note URLs",
    )
    parser.add_argument(
        "--input-file",
        help="UTF-8 file with one approved public ID, or XHS note URL, per line",
    )
    parser.add_argument(
        "--output", required=True, help="new run-specific output directory"
    )
    parser.add_argument("--max-items", type=int, default=20)
    parser.add_argument("--comments-per-item", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--tags", default="0", help="Maoyan tag pools, comma separated")
    parser.add_argument("--login", choices=("qrcode", "cookie"), default="qrcode")
    parser.add_argument(
        "--headless", action="store_true", help="run browser-based backends headlessly"
    )
    parser.add_argument(
        "--allow-visible-browser",
        action="store_true",
        help="explicitly approve a visible browser for this run",
    )
    parser.add_argument(
        "--timeout-seconds", type=int, default=1800, help="per-step wall-clock limit"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the live collection; default is preview",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args)
        if not args.execute:
            print(json.dumps(plan_dict(plan), ensure_ascii=False, indent=2))
            return 0
        return execute(plan)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
