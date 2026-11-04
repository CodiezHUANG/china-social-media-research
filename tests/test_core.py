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
from china_media_crawler.platforms import capabilities
from china_media_crawler.platforms.douban_reviews import normalize_record
from china_media_crawler.runner import execute


class CoreTests(unittest.TestCase):
    def test_catalog_is_available_without_backend_configuration(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            entries = capabilities()
        self.assertEqual(len(entries), 14)
        self.assertEqual({e["platform"] for e in entries}, {
            "xhs", "douyin", "weibo", "bilibili", "douban", "maoyan",
        })
        self.assertTrue(all(e["backend"] for e in entries))
        self.assertTrue(all("implementation" not in e for e in entries))

    def test_plan_previews_without_writing_or_starting_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), patch(
                "china_media_crawler.runner.subprocess.run"
            ) as run:
                code = main([
                    "plan", "--platform", "douban", "--item-id", "123456",
                    "--output", str(output),
                ])
            plan = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(plan["backend"], "douban-public")
            self.assertNotIn("implementation", plan)
            self.assertEqual(plan["schema_version"], "0.2")
            self.assertFalse(output.exists())
            run.assert_not_called()

    def test_plan_rejects_execute_flag_before_any_process(self) -> None:
        with patch("china_media_crawler.runner.subprocess.run") as run:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main([
                    "plan", "--platform", "douban", "--item-id", "123456",
                    "--output", "unused", "--execute",
                ])
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_script_entry_from_unrelated_directory(self) -> None:
        script = ROOT / "skills/china-social-media-research/scripts/collect.py"
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run([
                sys.executable, "-B", str(script), "--platform", "douban",
                "--item-id", "123456", "--output", "run",
            ], cwd=temporary, capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("implementation", json.loads(completed.stdout))
            self.assertFalse((Path(temporary) / "run").exists())

    def test_record_omits_author_and_preserves_platform_fields(self) -> None:
        row = {
            "content": "Example review", "timestamp": "2026-01-01 12:00:00",
            "author": "private-example-author", "votes": "3", "rating": 4,
        }
        record = normalize_record(row, "123456", "reviews", 1, utc_now(), "test-salt")
        payload = record.to_dict()
        self.assertNotIn("private-example-author", json.dumps(payload))
        self.assertEqual(payload["schema_version"], "0.2")
        self.assertEqual(payload["engagement"]["votes"], 3)
        self.assertEqual(payload["published_at"], "2026-01-01T04:00:00+00:00")
        self.assertEqual(payload["platform_data"]["rating"], 4)
        self.assertTrue(payload["author_pseudonym"].startswith("u_"))

    def test_missing_credential_leaves_no_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            plan = Plan(utc_now(), "douban", "reviews", str(output), {}, ["TEST_SECRET"], [])
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "TEST_SECRET"):
                    execute(plan)
            self.assertFalse(output.exists())

    def test_existing_empty_output_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan = Plan(utc_now(), "douban", "reviews", temporary, {}, [], [])
            with self.assertRaises(FileExistsError):
                execute(plan)

    def test_stdout_path_cannot_escape_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            step = Step("bad path", ["unused"], temporary, stdout_file="../outside.jsonl")
            plan = Plan(utc_now(), "weibo", "hot", str(output), {}, [], [step])
            with self.assertRaisesRegex(ValueError, "plain output filename"):
                execute(plan)
            self.assertFalse(output.exists())

    def test_run_manifest_redacts_sensitive_url_query(self) -> None:
        url = ("https://www.xiaohongshu.com/explore/note"
               "?xsec_token=synthetic-secret&xsec_source=pc_search")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            step = Step("detail", ["collect", "--item-url", url], temporary)
            plan = Plan(utc_now(), "xhs", "detail", str(output), {}, [], [step])
            with patch("china_media_crawler.runner.subprocess.run") as run:
                run.return_value.returncode = 0
                self.assertEqual(execute(plan), 0)
            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            saved = json.dumps(manifest)
            self.assertNotIn("synthetic-secret", saved)
            self.assertIn("xsec_token=REDACTED", saved)
            self.assertIn("synthetic-secret", plan.steps[0].command[2])

    def test_run_manifest_records_purpose_and_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            step = Step("reviews", ["collect"], temporary)
            plan = Plan(utc_now(), "maoyan", "reviews", str(output), {}, [], [step],
                        purpose="Synthetic reception pilot", notes=["synthetic note"])
            with patch("china_media_crawler.runner.subprocess.run") as run:
                run.return_value.returncode = 0
                self.assertEqual(execute(plan), 0)
            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["purpose"], "Synthetic reception pilot")
            self.assertEqual(manifest["notes"], ["synthetic note"])

    def test_backend_failure_prevents_following_step_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            steps = [Step("first", ["first"], temporary), Step("next", ["next"], temporary)]
            plan = Plan(utc_now(), "weibo", "hot", str(output), {}, [], steps)
            with patch("china_media_crawler.runner.subprocess.run") as run:
                run.return_value.returncode = 7
                self.assertEqual(execute(plan), 7)
            self.assertEqual(run.call_count, 1)
            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["results"][0]["returncode"], 7)


if __name__ == "__main__":
    unittest.main()
