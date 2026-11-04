from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from china_media_crawler.bilibili_audit import audit_runs
from china_media_crawler.cli import main

BVID = "BV1000000001"


def record(surface: str, object_id: str, content: str, **platform) -> dict:
    return {
        "platform": "bilibili", "surface": surface, "object_id": object_id,
        "content": content, "published_at": "2026-10-30T00:00:00+00:00",
        "captured_at": "2026-10-30T00:00:01+00:00",
        "author_pseudonym": "u_example", "engagement": {}, "platform_data": platform,
        "schema_version": "0.2", "author": "raw-author",
    }


class BilibiliAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / "runs"
        self.comments = self.runs / "comments"
        self.detail = self.runs / "detail"
        self.danmaku = self.runs / "danmaku"
        self.failed = self.runs / "failed"
        for path in (self.comments, self.detail, self.danmaku, self.failed):
            path.mkdir(parents=True)
        (self.comments / "records.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in [
                record("comments", "1", "main", bvid=BVID, page=1, level=1, parent_id=None),
                record("comments", "2", "nested", bvid=BVID, page=1, level=2, parent_id="1"),
            ]) + "\n", encoding="utf-8")
        (self.comments / "bilibili_comments_manifest.json").write_text(json.dumps({
            "platform": "bilibili", "surface": "comments", "status": "complete",
            "record_count": 2, "comments_mode": "first_page",
        }), encoding="utf-8")
        (self.detail / "records.jsonl").write_text(
            json.dumps(record("detail", BVID, "title", bvid=BVID, aid="1"), ensure_ascii=False) + "\n",
            encoding="utf-8")
        (self.detail / "bilibili_detail_manifest.json").write_text(json.dumps({
            "platform": "bilibili", "surface": "detail", "status": "complete", "record_count": 1,
        }), encoding="utf-8")
        (self.danmaku / "records.jsonl").write_text(
            json.dumps(record("danmaku", "d1", "danmaku", bvid=BVID, progress_seconds=1.5),
                       ensure_ascii=False) + "\n", encoding="utf-8")
        (self.danmaku / "bilibili_danmaku_manifest.json").write_text(json.dumps({
            "platform": "bilibili", "surface": "danmaku", "status": "complete", "record_count": 1,
        }), encoding="utf-8")
        (self.failed / "records.jsonl").write_text(
            json.dumps(record("comments", "3", "partial", bvid="BV1000000002", level=1),
                       ensure_ascii=False) + "\n", encoding="utf-8")
        (self.failed / "bilibili_comments_manifest.json").write_text(json.dumps({
            "platform": "bilibili", "surface": "comments", "status": "failed",
            "failed_object": "BV1000000002", "failure_class": "rate_limited",
            "failure_stage": "comments_page", "failed_page": 2,
        }), encoding="utf-8")

    def test_canonical_layers_audit_and_retry_queue(self):
        output = self.root / "audit"
        summary = audit_runs([self.comments, self.detail, self.danmaku, self.failed], output)
        self.assertEqual(summary["bvid_count"], 2)
        self.assertEqual(summary["retry_count"], 1)
        video = output / "videos" / BVID
        self.assertTrue((video / "video_info.json").is_file())
        self.assertTrue((video / "comments.json").is_file())
        self.assertTrue((video / "danmaku.json").is_file())
        comments = json.loads((video / "comments.json").read_text(encoding="utf-8"))
        self.assertEqual(comments["main_count"], 1)
        self.assertEqual(comments["nested_count"], 1)
        self.assertNotIn("raw-author", (video / "comments.json").read_text(encoding="utf-8"))
        retry = [json.loads(line) for line in (output / "retry_queue.jsonl").read_text().splitlines()]
        self.assertEqual(retry[0]["bvid"], "BV1000000002")
        self.assertEqual(retry[0]["failure_class"], "rate_limited")
        audit = (output / "quality_audit.csv").read_text(encoding="utf-8-sig")
        self.assertIn("comments", audit)
        self.assertIn("rate_limited", audit)

    def test_partial_object_failure_is_audited_and_retried(self):
        partial = self.runs / "partial"
        partial.mkdir()
        bvid = "BV1000000004"
        (partial / "records.jsonl").write_text(
            json.dumps(record("comments", "4", "partial", bvid=bvid, level=1),
                       ensure_ascii=False) + "\n", encoding="utf-8")
        (partial / "bilibili_comments_manifest.json").write_text(json.dumps({
            "platform": "bilibili", "surface": "comments",
            "status": "complete_with_failures", "failed_object_count": 1,
            "objects": [{
                "bvid": bvid, "records": 1, "pages": [{"page": 1}],
                "termination_reason": "failed",
                "failure_class": "pagination_unavailable",
                "error_kind": "comment_pagination_unavailable",
                "failure_stage": "comments_page", "failed_page": 3,
                "pagination_diagnostic": {"requested_page": 3, "count": 72,
                                          "returned_rows": None},
            }],
        }), encoding="utf-8")
        output = self.root / "audit-partial"
        summary = audit_runs([partial], output)
        self.assertEqual(summary["failure_count"], 1)
        self.assertEqual(summary["retry_count"], 1)
        audit = (output / "quality_audit.csv").read_text(encoding="utf-8-sig")
        self.assertIn("pagination_unavailable", audit)
        self.assertIn(bvid, audit)
        self.assertIn("False", audit)
        self.assertIn("True", audit)
        retry = [json.loads(line) for line in
                 (output / "retry_queue.jsonl").read_text().splitlines()]
        self.assertEqual(retry[0]["bvid"], bvid)
        self.assertEqual(retry[0]["failure_class"], "pagination_unavailable")
        self.assertEqual(retry[0]["pagination_diagnostic"]["count"], 72)

    def test_refuses_to_overwrite_audit_output(self):
        output = self.root / "audit"
        output.mkdir()
        with self.assertRaises(ValueError):
            audit_runs([self.comments], output)

    def test_cli_audit_summary(self):
        output = self.root / "audit"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["bili-audit", "--run", str(self.comments),
                                   "--run", str(self.failed), "--output", str(output), "--json"]), 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["bvid_count"], 2)
        self.assertEqual(summary["retry_count"], 1)


if __name__ == "__main__":
    unittest.main()

