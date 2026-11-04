from __future__ import annotations

import argparse
import json
from pathlib import Path

from china_media_crawler import __version__
from china_media_crawler.configuration import PLATFORM_SURFACES
from china_media_crawler.models import plan_dict
from china_media_crawler.planning import build_plan
from china_media_crawler.runner import execute

MAX_PURPOSE_CHARS = 500


def _purpose(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or len(text) > MAX_PURPOSE_CHARS or any(ord(char) < 32 for char in text):
        raise ValueError(
            f"--purpose must be a non-empty single line of at most {MAX_PURPOSE_CHARS} characters"
        )
    return text

def build_collection_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=add_help)
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
    parser.add_argument("--comments-mode", choices=("first_page", "deep"), default="first_page",
                        help="Bilibili comments coverage: first page plus nested replies, or deep pages")
    parser.add_argument("--on-item-error", choices=("stop", "continue"), default="stop",
                        help="Bilibili content policy after a per-video failure")
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--danmaku-per-item", type=int, default=100)
    parser.add_argument("--max-parts", type=int, default=1)
    parser.add_argument("--tags", default="0", help="Maoyan tag pools, comma separated")
    parser.add_argument("--session", choices=("auto", "required", "off"), default="auto",
                        help="saved-session reuse for all platforms")
    parser.add_argument("--visitor-session", action="store_true",
                        help="use an anonymous visitor browser session for Weibo comments/search")
    parser.add_argument(
        "--timeout-seconds", type=int, default=1800, help="per-step wall-clock limit"
    )
    parser.add_argument(
        "--purpose",
        help="research purpose recorded in the plan and run manifest "
             f"(single line, at most {MAX_PURPOSE_CHARS} characters)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the live collection; default is preview",
    )
    return parser

def collection_main(argv: list[str] | None = None) -> int:
    parser = build_collection_parser()
    return run_collection(parser, parser.parse_args(argv))


def prepare_collection_plan(args: argparse.Namespace):
    """Apply the same authentication rules to single and workflow tasks."""
    mode = args.session
    plan = build_plan(args)
    plan.purpose = _purpose(getattr(args, "purpose", None))
    if getattr(args, "comments_mode", "first_page") == "deep" and not (
        plan.platform == "bilibili" and plan.surface == "comments"
    ):
        raise ValueError("--comments-mode deep is only supported for Bilibili comments")
    if getattr(args, "on_item_error", "stop") == "continue" and not (
        plan.platform == "bilibili" and plan.surface in {"detail", "comments", "danmaku"}
    ):
        raise ValueError("--on-item-error continue is only supported for Bilibili content")
    if mode != "auto" and not (
        plan.platform in {"douban", "bilibili", "douyin", "xhs"}
        or (plan.platform == "weibo" and plan.surface in {"comments", "search"})
    ):
        raise ValueError("this route does not support saved sessions")
    if getattr(args, "visitor_session", False):
        if not (plan.platform == "weibo" and plan.surface in {"comments", "search"}):
            raise ValueError("--visitor-session is only supported for Weibo comments/search")
    return plan


def run_collection(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        plan = prepare_collection_plan(args)
        if not args.execute:
            print(json.dumps(plan_dict(plan), ensure_ascii=False, indent=2))
            return 0
        return execute(plan)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmc", description="Bounded Chinese media research collection."
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    collection = build_collection_parser(add_help=False)
    commands.add_parser("collect", parents=[collection], help="preview; add --execute to collect")
    commands.add_parser("plan", parents=[collection], help="preview only")
    workflow = commands.add_parser("workflow", help="preview or execute selected tasks sequentially")
    workflow.add_argument("--file", required=True, help="UTF-8 JSON task file")
    workflow.add_argument("--output", required=True, help="new workflow output directory")
    workflow.add_argument("--on-error", choices=("stop", "continue"), default="stop")

    workflow.add_argument("--execute", action="store_true")
    audit = commands.add_parser("bili-audit", help="build per-video Bilibili canonical layers and audit")
    audit.add_argument("--run", action="append", default=[], type=Path, required=True)
    audit.add_argument("--output", required=True, type=Path)
    audit.add_argument("--json", action="store_true")
    supplement = commands.add_parser(
        "bili-supplement", help="preview or execute missing-layer/retry Bilibili supplement")
    supplement.add_argument("--audit", required=True, type=Path)
    supplement.add_argument("--output", required=True, type=Path)
    supplement.add_argument("--layer", action="append",
                            choices=("detail", "comments", "danmaku"), default=[])
    supplement.add_argument("--retry-only", action="store_true")
    supplement.add_argument("--max-items", type=int, default=20)
    supplement.add_argument("--comments-mode", choices=("first_page", "deep"), default="first_page")
    supplement.add_argument("--comments-per-item", type=int, default=20)
    supplement.add_argument("--max-pages", type=int, default=5)
    supplement.add_argument("--danmaku-per-item", type=int, default=100)
    supplement.add_argument("--max-parts", type=int, default=1)
    supplement.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    supplement.add_argument("--execute", action="store_true")
    supplement.add_argument("--json", action="store_true")
    check = commands.add_parser("preflight", help="check dependencies offline")
    check.add_argument("--platform", choices=("all", *PLATFORM_SURFACES), action="append")
    check.add_argument("--surface")
    check.add_argument("--json", action="store_true")
    check.add_argument("--session", choices=("auto", "required", "off"), default="auto")
    check.add_argument("--visitor-session", action="store_true")
    catalog = commands.add_parser("platforms", help="show capabilities")
    catalog.add_argument("--json", action="store_true")
    catalog.add_argument("--platform", choices=tuple(PLATFORM_SURFACES), action="append")
    login = commands.add_parser("login", help="manage the selected platform's session provider")
    login.add_argument("--platform", required=True, choices=tuple(PLATFORM_SURFACES))
    login.add_argument("--timeout-seconds", type=int, default=300)
    login.add_argument("--login-url", help="HTTPS Rednote login page for Xiaohongshu")
    actions = login.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="inspect saved-state presence offline")
    actions.add_argument("--clear", action="store_true", help="delete only the local saved platform state")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "bili-audit":
        from china_media_crawler.bilibili_audit import audit_runs

        try:
            summary = audit_runs(args.run, args.output)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"Audited {summary['bvid_count']} BVs; retry queue {summary['retry_count']}; output {summary['output']}")
        return 0
    if args.command == "bili-supplement":
        from china_media_crawler.bilibili_supplement import supplement

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
    if args.command == "workflow":
        from china_media_crawler.workflow import execute_workflow, load_workflow, workflow_dict

        try:
            workflow = load_workflow(args.file, args.output, on_error=args.on_error)
            if not args.execute:
                print(json.dumps(workflow_dict(workflow), ensure_ascii=False, indent=2))
                return 0
            return execute_workflow(workflow)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if args.command == "login":
        import subprocess
        from china_media_crawler.authentication import run_session

        if not 15 <= args.timeout_seconds <= 1800:
            parser.error("timeout must be between 15 and 1800 seconds")
        try:
            return run_session(args.platform, "status" if args.status else "clear" if args.clear else "login",
                               args.timeout_seconds, args.login_url)
        except ValueError as error:
            parser.error(str(error))
        except (OSError, subprocess.TimeoutExpired):
            parser.error("session operation could not complete; check the selected platform's Python and browser")
    if args.command == "preflight":
        from china_media_crawler.preflight import main as preflight_main

        forwarded = []
        for platform in args.platform or []:
            forwarded += ["--platform", platform]
        if args.surface:
            forwarded += ["--surface", args.surface]
        forwarded += ["--session", args.session]
        if args.json:
            forwarded.append("--json")
        if args.visitor_session:
            forwarded.append("--visitor-session")
        return preflight_main(forwarded)
    if args.command == "platforms":
        from china_media_crawler.platforms import capabilities

        catalog = capabilities()
        if args.platform:
            catalog = [entry for entry in catalog if entry["platform"] in args.platform]
        if args.json:
            print(json.dumps(catalog, ensure_ascii=False, indent=2))
        else:
            for entry in catalog:
                print(
                    f"{entry['platform']}/{entry['surface']}: "
                    f"{entry['backend']}"
                )
        return 0
    if args.command == "plan" and args.execute:
        parser.error("plan is preview-only; use collect --execute for a live run")
    return run_collection(parser, args)
