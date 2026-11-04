"""Bounded execution and partial-result manifests."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from china_media_crawler.manifest import utc_now, write_manifest
from china_media_crawler.models import Plan, plan_dict
from china_media_crawler.privacy import child_environment

def validate_execution(plan: Plan) -> None:
    """Check execution requirements without reserving output or starting work."""
    missing = [name for name in plan.required_environment if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("required environment variable(s) are not set: " + ", ".join(missing))

    for step in plan.steps:
        if step.stdout_file and (
            Path(step.stdout_file).is_absolute()
            or len(Path(step.stdout_file).parts) != 1
            or step.stdout_file in {".", "..", "run_manifest.json"}
        ):
            raise ValueError("stdout_file must be a plain output filename")


def execute(plan: Plan) -> int:
    validate_execution(plan)
    output = Path(plan.output)
    # Reserve a new run atomically; an existing directory is never reused.
    output.mkdir(parents=True, exist_ok=False)

    manifest_path = output / "run_manifest.json"
    manifest = plan_dict(plan)
    manifest.update({"status": "running", "started_at": utc_now(), "results": []})
    write_manifest(manifest_path, manifest)

    try:
        for step in plan.steps:
            environment = child_environment(step)
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



