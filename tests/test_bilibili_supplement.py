from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from china_media_crawler.bilibili_supplement import load_targets, supplement
from china_media_crawler.cli import main


class BilibiliSupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit = self.root / "audit"
        self.audit.mkdir()
        (self.audit / "quality_audit.csv").write_text(
            "bvid,layer,present,record_count,unique_count,main_count,nested_count,source_runs,failure_class,failure_stage,failed_page\n"
            "BV1000000001,detail,False,0,0,0,0,,,,\n"
            "BV1000000001,comments,True,2,2,1,1,run/comments,,,\n"
            "BV1000000001,danmaku,False,0,0,0,0,,,,\n"
            "BV1000000002,comments,False,0,0,0,0,,,,\n",
            encoding="utf-8",
        )
        (self.audit / "retry_queue.jsonl").write_text(json.dumps({
            "bvid": "BV1000000003", "failure_class": "env_blocked",
            "failure_stage": None, "source_run": "run/danmaku",
        }) + "\n", encoding="utf-8")

    def test_targets_missing_and_retry(self):
        targets = load_targets(self.audit, {"detail", "comments", "danmaku"}, False, 20)
        self.assertEqual(targets["detail"], ["BV1000000001"])
        self.assertEqual(targets["comments"], ["BV1000000002"])
        self.assertEqual(targets["danmaku"], ["BV1000000001", "BV1000000003"])
        retry = load_targets(self.audit, {"detail", "comments", "danmaku"}, True, 20)
        self.assertEqual(retry, {"danmaku": ["BV1000000003"]})

    def test_partial_layer_is_a_supplement_target_even_when_present(self):
        audit = self.root / "partial-audit"
        audit.mkdir()
        (audit / "quality_audit.csv").write_text(
            "bvid,layer,present,complete,needs_retry,record_count,unique_count,"
            "main_count,nested_count,source_runs,failure_class,failure_stage,failed_page\n"
            "BV1000000009,comments,True,False,True,2,2,1,1,run/comments,"
            "pagination_unavailable,comments_page,3\n",
            encoding="utf-8",
        )
        (audit / "retry_queue.jsonl").write_text(json.dumps({
            "bvid": "BV1000000009", "layer": "comments",
            "failure_class": "pagination_unavailable",
            "failure_stage": "comments_page", "failed_page": 3,
        }) + "\n", encoding="utf-8")
        targets = load_targets(audit, {"comments"}, False, 20)
        self.assertEqual(targets, {"comments": ["BV1000000009"]})
        retry = load_targets(audit, {"comments"}, True, 20)
        self.assertEqual(retry, {"comments": ["BV1000000009"]})

    def test_preview_does_not_create_output(self):
        output = self.root / "out"
        summary = supplement(
            self.audit, output, layers={"danmaku"}, retry_only=True, max_items=20,
            comments_mode="first_page", comments_per_item=20, max_pages=5,
            danmaku_per_item=100, max_parts=1, session_mode="auto", execute=False,
        )
        self.assertEqual(summary["status"], "preview")
        self.assertEqual(summary["targets"], {"danmaku": ["BV1000000003"]})
        self.assertFalse(output.exists())

    def test_execute_calls_collector_and_writes_manifest(self):
        output = self.root / "out"
        with patch.dict(os.environ, {"BILI_PSEUDONYM_SALT": "synthetic"}), \
             patch("china_media_crawler.bilibili_supplement.collect_content",
                   return_value=0) as run:
            summary = supplement(
                self.audit, output, layers={"danmaku"}, retry_only=True, max_items=20,
                comments_mode="first_page", comments_per_item=20, max_pages=5,
                danmaku_per_item=100, max_parts=1, session_mode="required", execute=True,
            )
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["results"][0]["layer"], "danmaku")
        self.assertEqual(summary["results"][0]["bvids"], ["BV1000000003"])
        run.assert_called_once()
        self.assertTrue((output / "supplement_manifest.json").is_file())

    def test_execute_requires_salt_before_output(self):
        output = self.root / "out"
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                supplement(
                    self.audit, output, layers={"danmaku"}, retry_only=True, max_items=20,
                    comments_mode="first_page", comments_per_item=20, max_pages=5,
                    danmaku_per_item=100, max_parts=1, session_mode="auto", execute=True,
                )
        self.assertFalse(output.exists())

    def test_cli_preview_summary(self):
        output = self.root / "out"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["bili-supplement", "--audit", str(self.audit),
                                   "--output", str(output), "--layer", "danmaku",
                                   "--retry-only", "--json"]), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "preview")
        self.assertEqual(payload["targets"], {"danmaku": ["BV1000000003"]})
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
