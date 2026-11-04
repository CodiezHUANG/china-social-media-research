"""Preview and sequential execution of explicitly selected collection tasks."""
from __future__ import annotations

import argparse
import json
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Plan
from china_media_crawler.runner import execute, validate_execution

MAX_FILE_BYTES = 256 * 1024
MAX_TASKS = 24
STRING_FIELDS = {
    "platform", "surface", "input_file", "tags", "session",
    "comments_mode", "on_item_error", "purpose",
}
INTEGER_FIELDS = {
    "max_items", "comments_per_item", "max_pages", "danmaku_per_item",
    "max_parts", "timeout_seconds",
}
LIST_FIELDS = {"query", "item_id", "item_url"}
BOOLEAN_FIELDS = {"visitor_session"}
TASK_FIELDS = {"id"} | STRING_FIELDS | INTEGER_FIELDS | LIST_FIELDS | BOOLEAN_FIELDS
RESERVED_IDS = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


@dataclass
class WorkflowTask:
    id: str
    plan: Plan


@dataclass
class Workflow:
    created_at: str
    output: str
    on_error: str
    tasks: list[WorkflowTask]
    schema_version: str = "0.2"


def workflow_dict(workflow: Workflow) -> dict:
    return asdict(workflow)


class TaskParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Invalid task values may contain credentials; never echo parser input.
        raise ValueError("invalid task option or choice; see WORKFLOWS.md")


def _unique_keys(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("task strings must be non-empty and at most 4096 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("task strings must be single-line values without control characters")
    return value


def _task_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value):
        raise ValueError("task id must be 1..48 lowercase ASCII letters, digits, hyphens or underscores; start with a letter")
    if value in RESERVED_IDS:
        raise ValueError("task id is a reserved device name")
    return value


def _plain_path(path: Path) -> None:
    """Do not traverse linked output ancestors (including Windows junctions)."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("workflow output must not traverse links or junctions")


def load_workflow(source: str | Path, output: str | Path, *, on_error: str = "stop",
                  ) -> Workflow:
    if on_error not in {"stop", "continue"}:
        raise ValueError("on_error must be stop or continue")
    source = Path(source).expanduser().resolve()
    with source.open("rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("workflow file exceeds 256 KiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("workflow must contain valid UTF-8 JSON") from None
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "tasks"}:
        raise ValueError("workflow must contain only schema_version and tasks")
    if payload["schema_version"] != "0.2":
        raise ValueError("unsupported workflow schema_version; expected 0.2")
    tasks = payload["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError("workflow must contain 1..24 tasks")
    root = Path(output).expanduser().absolute()
    _plain_path(root)
    root = root.resolve()
    from china_media_crawler.cli import build_collection_parser, prepare_collection_plan

    parser = TaskParser(parents=[build_collection_parser(add_help=False)], add_help=False)
    selected = []
    used = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) - TASK_FIELDS:
            raise ValueError("unknown task field; only documented collection options are supported")
        identity = _task_id(task.get("id"))
        if identity in used:
            raise ValueError("duplicate task id")
        used.add(identity)
        argv = ["--output", str(root / identity)]
        for field, value in task.items():
            if field == "id":
                continue
            flag = "--" + field.replace("_", "-")
            if field in BOOLEAN_FIELDS:
                if type(value) is not bool:
                    raise ValueError(f"{identity}: {field} must be a boolean")
                if value:
                    argv.append(flag)
            elif field in INTEGER_FIELDS:
                if type(value) is not int:
                    raise ValueError(f"{identity}: {field} must be an integer")
                argv.append(f"{flag}={value}")
            elif field in LIST_FIELDS:
                if not isinstance(value, list) or not 1 <= len(value) <= 200:
                    raise ValueError(f"{identity}: {field} must contain 1..200 strings")
                argv.extend(f"{flag}={_text(item)}" for item in value)
            else:
                value = _text(value)
                if field == "input_file":
                    value = str((source.parent / Path(value).expanduser()).resolve())
                argv.append(f"{flag}={value}")
        try:
            plan = prepare_collection_plan(parser.parse_args(argv))
        except (OSError, ValueError):
            # Existing planners may include a private URL or query in errors.
            raise ValueError(f"{identity}: collection options could not be planned; check route and inputs") from None
        selected.append(WorkflowTask(identity, plan))
    return Workflow(utc_now(), str(root), on_error, selected)


def execute_workflow(workflow: Workflow) -> int:
    """Precheck all plans, then run in order; never retry a failed platform."""
    if workflow.on_error not in {"stop", "continue"} or not 1 <= len(workflow.tasks) <= MAX_TASKS:
        raise ValueError("invalid workflow policy or task count")
    root = Path(workflow.output)
    _plain_path(root)
    identities = set()
    for task in workflow.tasks:
        identity = _task_id(task.id)
        if identity in identities or Path(task.plan.output) != root / identity:
            raise ValueError("task ids must be unique and output paths must remain inside the workflow")
        identities.add(identity)
        validate_execution(task.plan)
    # All plans must be valid before any output directory or process is created.
    root.mkdir(parents=True, exist_ok=False)
    path = root / "workflow_manifest.json"
    manifest = workflow_dict(workflow)
    manifest.update(status="running", started_at=utc_now())
    for entry in manifest["tasks"]:
        entry["status"] = "pending"
    write_manifest(path, manifest)
    failed_platforms = set()
    stop_reason = None
    cancelled = False
    active = None
    try:
        for task, entry in zip(workflow.tasks, manifest["tasks"]):
            if stop_reason or task.plan.platform in failed_platforms:
                entry.update(status="not_attempted", reason=stop_reason or "platform_failed",
                             finished_at=utc_now())
                write_manifest(path, manifest)
                continue
            active = entry
            entry.update(status="running", started_at=utc_now())
            write_manifest(path, manifest)
            try:
                code = execute(task.plan)
            except (OSError, ValueError) as error:
                code = 2
                entry["error_type"] = type(error).__name__
            entry.update(status="complete" if code == 0 else "timed_out" if code == 124 else
                         "cancelled" if code == 130 else "failed",
                         returncode=code, finished_at=utc_now())
            if code:
                failed_platforms.add(task.plan.platform)
                if code == 130:
                    cancelled = True
                    stop_reason = "workflow_cancelled"
                elif workflow.on_error == "stop":
                    stop_reason = "previous_task_failed"
            write_manifest(path, manifest)
            active = None
    except BaseException as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        if active is not None:
            active.update(status="cancelled" if cancelled else "failed",
                          error_type=type(error).__name__, finished_at=utc_now())
        for entry in manifest["tasks"]:
            if entry["status"] == "pending":
                entry.update(status="not_attempted", reason="workflow_cancelled" if cancelled else
                             "workflow_error", finished_at=utc_now())
        manifest.update(status="cancelled" if cancelled else "failed", finished_at=utc_now())
        write_manifest(path, manifest)
        if cancelled:
            return 130
        raise
    manifest.update(status="cancelled" if cancelled else "failed" if failed_platforms else "complete",
                    finished_at=utc_now())
    write_manifest(path, manifest)
    return 130 if cancelled else 1 if failed_platforms else 0


