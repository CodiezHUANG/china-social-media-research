from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from china_media_crawler.cli import main
from china_media_crawler.manifest import utc_now
from china_media_crawler.models import Plan, Step
from china_media_crawler.workflow import (
    MAX_FILE_BYTES, Workflow, WorkflowTask, execute_workflow, load_workflow,
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "tasks.json"
        self.output = self.folder / "output"

    def load(self, tasks, **kwargs):
        self.source.write_text(json.dumps({"schema_version": "0.2", "tasks": tasks}), encoding="utf-8")
        return load_workflow(self.source, self.output, **kwargs)

    def task(self, identity="movie", **kwargs):
        return {"id": identity, "platform": "maoyan", "item_id": ["123456"], **kwargs}

    def execution(self):
        tasks = []
        for identity, platform in [("first", "maoyan"), ("second", "bilibili"), ("third", "maoyan")]:
            plan = Plan(utc_now(), platform, "reviews", str(self.output / identity), {}, [], [])
            tasks.append(WorkflowTask(identity, plan))
        return Workflow(utc_now(), str(self.output), "stop", tasks)

    def result(self):
        return json.loads((self.output / "workflow_manifest.json").read_text(encoding="utf-8"))

    def test_cli_preview_only_selected_platforms_without_output_or_process(self):
        self.load([self.task(), {"id": "hot", "platform": "weibo", "surface": "hot"}])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch("china_media_crawler.workflow.execute") as run:
            code = main(["workflow", "--file", str(self.source), "--output", str(self.output)])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual([entry["plan"]["platform"] for entry in payload["tasks"]], ["maoyan", "weibo"])
        self.assertEqual(payload["tasks"][0]["plan"]["authentication"]["implemented_modes"], ["anonymous"])
        self.assertFalse(self.output.exists())
        run.assert_not_called()


    def test_relative_input_file_is_relative_to_task_file(self):
        (self.folder / "ids.txt").write_text("123456\n", encoding="utf-8")
        workflow = self.load([{"id": "movie", "platform": "maoyan", "input_file": "ids.txt"}])
        self.assertIn("123456", workflow.tasks[0].plan.steps[0].command)

    def test_invalid_ids_cannot_escape_or_collide_on_windows(self):
        for identity in ("../outside", "a/b", "a\\b", "A", "con", "lpt1", "a.", "", "é", "a" * 49):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.load([self.task(identity)])
        self.assertFalse(self.output.exists())

    def test_duplicate_task_ids_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate task id"):
            self.load([self.task(), self.task()])

    def test_task_purpose_is_recorded_and_validated(self):
        workflow = self.load([self.task(purpose="Synthetic reception pilot")])
        self.assertEqual(workflow.tasks[0].plan.purpose, "Synthetic reception pilot")
        with self.assertRaises(ValueError):
            self.load([self.task(purpose="first\nsecond")])

    def test_unknown_and_execution_fields_rejected(self):
        for field in ("command", "env", "output", "execute", "headless"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unknown task field"):
                self.load([self.task(**{field: "forbidden"})])

    def test_json_option_types_and_limits_rejected(self):
        for options in ({"max_items": True}, {"max_pages": 6}, {"max_items": 0}, {"visitor_session": "true"},
                        {"query": "text"}, {"item_id": [3]}, {"query": []}, {"platform": "--help"},
                        {"item_id": ["1\n2"]}, {"query": ["x" * 4097]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.load([self.task(**options)])

    def test_authentication_rules_match_single_collection(self):
        for task in (self.task(session="required"),):
            with self.subTest(task=task), self.assertRaises(ValueError):
                self.load([task])

    def test_bounded_file_and_task_count(self):
        for tasks in ([], [self.task(f"t{i}") for i in range(25)]):
            with self.assertRaises(ValueError):
                self.load(tasks)
        self.source.write_bytes(b" " * (MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "256 KiB"):
            load_workflow(self.source, self.output)

    def test_duplicate_keys_malformed_json_and_schema_rejected(self):
        for raw in ('{"schema_version":"0.2","schema_version":"0.2","tasks":[]}',
                    '{"schema_version":"0.1","tasks":[]}', '{"schema_version":"0.2","tasks":[],"env":{}}',
                    '{"tasks":', '[1]', '\xff'):
            self.source.write_bytes(raw.encode("latin-1"))
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load_workflow(self.source, self.output)

    def test_invalid_planner_errors_do_not_echo_private_input(self):
        with self.assertRaises(ValueError) as raised:
            self.load([self.task(item_id=["SECRET_VALUE"])])
        self.assertNotIn("SECRET_VALUE", str(raised.exception))

    def test_missing_last_task_requirement_prevents_all_execution_and_outputs(self):
        workflow = self.execution()
        workflow.tasks[-1].plan.required_environment = ["TEST_WORKFLOW_SECRET"]
        with patch.dict(os.environ, {}, clear=True), patch("china_media_crawler.workflow.execute") as run:
            with self.assertRaisesRegex(ValueError, "TEST_WORKFLOW_SECRET"):
                execute_workflow(workflow)
        run.assert_not_called()
        self.assertFalse(self.output.exists())


    def test_success_order_and_separate_child_manifests_using_real_runner(self):
        workflow = self.execution()
        for task in workflow.tasks:
            task.plan.steps = [Step(task.id, ["unused"], str(self.folder))]
        with patch("china_media_crawler.runner.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertEqual(execute_workflow(workflow), 0)
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["unused"] * 3)
        self.assertEqual(self.result()["status"], "complete")
        self.assertEqual([entry["id"] for entry in self.result()["tasks"]], ["first", "second", "third"])
        for task in workflow.tasks:
            child = json.loads((Path(task.plan.output) / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(child["status"], "complete")

    def test_default_stop_preserves_partial_output_and_marks_remaining(self):
        workflow = self.execution()
        def fail(plan):
            Path(plan.output).mkdir()
            (Path(plan.output) / "records.jsonl").write_text("partial", encoding="utf-8")
            return 2
        with patch("china_media_crawler.workflow.execute", side_effect=fail) as run:
            self.assertEqual(execute_workflow(workflow), 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual([e["status"] for e in self.result()["tasks"]], ["failed", "not_attempted", "not_attempted"])
        self.assertEqual((self.output / "first" / "records.jsonl").read_text(), "partial")

    def test_continue_runs_other_platform_but_skips_failed_platform(self):
        workflow = self.execution()
        workflow.on_error = "continue"
        with patch("china_media_crawler.workflow.execute", side_effect=[2, 0]) as run:
            self.assertEqual(execute_workflow(workflow), 1)
        self.assertEqual([call.args[0].platform for call in run.call_args_list], ["maoyan", "bilibili"])
        entries = self.result()["tasks"]
        self.assertEqual([e["status"] for e in entries], ["failed", "complete", "not_attempted"])
        self.assertEqual(entries[2]["reason"], "platform_failed")

    def test_timeout_and_sanitized_launch_error_are_not_success(self):
        workflow = self.execution()
        workflow.on_error = "continue"
        with patch("china_media_crawler.workflow.execute", side_effect=[124, OSError("PRIVATE_TOKEN")]):
            self.assertEqual(execute_workflow(workflow), 1)
        payload = self.result()
        self.assertEqual([e["status"] for e in payload["tasks"]], ["timed_out", "failed", "not_attempted"])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(payload))
        self.assertEqual(payload["tasks"][1]["error_type"], "OSError")

    def test_keyboard_interrupt_records_cancellation_and_stops_even_in_continue_mode(self):
        workflow = self.execution()
        workflow.on_error = "continue"
        with patch("china_media_crawler.workflow.execute", side_effect=KeyboardInterrupt) as run:
            self.assertEqual(execute_workflow(workflow), 130)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.result()["status"], "cancelled")
        self.assertEqual([e["status"] for e in self.result()["tasks"]], ["cancelled", "not_attempted", "not_attempted"])

    def test_child_cancellation_return_code_stops_all_platforms(self):
        workflow = self.execution()
        workflow.on_error = "continue"
        with patch("china_media_crawler.workflow.execute", return_value=130) as run:
            self.assertEqual(execute_workflow(workflow), 130)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.result()["status"], "cancelled")

    def test_unexpected_exception_records_failure_and_reraises(self):
        workflow = self.execution()
        with patch("china_media_crawler.workflow.execute", side_effect=RuntimeError("PRIVATE")):
            with self.assertRaises(RuntimeError):
                execute_workflow(workflow)
        self.assertEqual(self.result()["status"], "failed")
        self.assertNotIn("PRIVATE", json.dumps(self.result()))
        self.assertEqual(self.result()["tasks"][1]["reason"], "workflow_error")

    def test_existing_output_never_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with patch("china_media_crawler.workflow.execute") as run:
            with self.assertRaises(FileExistsError):
                execute_workflow(self.execution())
        run.assert_not_called()
        self.assertEqual(marker.read_text(), "keep")

    def test_tampered_task_output_rejected_before_output(self):
        workflow = self.execution()
        workflow.tasks[-1].plan.output = str(self.folder / "outside")
        with self.assertRaisesRegex(ValueError, "inside the workflow"):
            execute_workflow(workflow)
        self.assertFalse(self.output.exists())

    def test_real_runner_filters_credentials_for_each_selected_platform(self):
        workflow = self.execution()
        workflow.tasks = workflow.tasks[:2]
        workflow.tasks[0].plan.steps = [Step("first", ["first"], str(self.folder), pass_environment=["MAOYAN_PSEUDONYM_SALT"])]
        workflow.tasks[1].plan.steps = [Step("second", ["second"], str(self.folder), pass_environment=["BILIBILI_COOKIE"])]
        with patch.dict(os.environ, {"MAOYAN_PSEUDONYM_SALT": "SALT", "BILIBILI_COOKIE": "COOKIE", "XHS_COOKIE": "UNRELATED"}), patch("china_media_crawler.runner.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertEqual(execute_workflow(workflow), 0)
        first, second = [call.kwargs["env"] for call in run.call_args_list]
        self.assertIn("MAOYAN_PSEUDONYM_SALT", first)
        self.assertNotIn("BILIBILI_COOKIE", first)
        self.assertIn("BILIBILI_COOKIE", second)
        self.assertNotIn("MAOYAN_PSEUDONYM_SALT", second)
        self.assertNotIn("XHS_COOKIE", first)
        self.assertNotIn("XHS_COOKIE", second)

    def test_visitor_session_boolean_is_forwarded_to_weibo(self):
        workflow = self.load([{
            "id": "weibo-comments", "platform": "weibo", "surface": "comments",
            "item_id": ["123"], "visitor_session": True,
        }])
        self.assertIn("--visitor-session", workflow.tasks[0].plan.steps[0].command)

    def test_bilibili_comments_mode_is_forwarded_and_scoped(self):
        workflow = self.load([{
            "id": "video-comments", "platform": "bilibili", "surface": "comments",
            "item_id": ["BV1000000001"], "comments_mode": "deep",
        }])
        command = workflow.tasks[0].plan.steps[0].command
        self.assertIn("--comments-mode", command)
        self.assertIn("deep", command)
        with self.assertRaises(ValueError):
            self.load([self.task(comments_mode="deep")])


if __name__ == "__main__":
    unittest.main()
