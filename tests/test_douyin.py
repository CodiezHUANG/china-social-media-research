from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_media_crawler import douyin_session
from china_media_crawler.cli import build_collection_parser
from china_media_crawler.http import CollectionError
from china_media_crawler.planning import build_plan
from china_media_crawler.platforms import get_adapter
from china_media_crawler.platforms import douyin


def aweme(identifier="123", desc="Synthetic video", created=1700000000):
    return {
        "aweme_id": identifier,
        "desc": desc,
        "create_time": created,
        "author": {"uid": "98765", "nickname": "private-author"},
        "statistics": {"digg_count": 4, "comment_count": 3,
                       "share_count": 2, "collect_count": 1},
        "video": {"duration": 12000},
        "aweme_type": 0,
    }


def search_payload(*items, has_more=1, logid="log-1"):
    return {
        "status_code": 0,
        "data": [{"aweme_info": item} for item in items],
        "has_more": has_more,
        "extra": {"logid": logid},
    }


def state():
    return {
        "cookies": [{"name": "sessionid", "value": "synthetic-session",
                     "domain": ".douyin.com", "path": "/"}],
        "origins": [{"origin": "https://www.douyin.com",
                     "localStorage": [{"name": "xmst", "value": "synthetic-ms-token"}]}],
    }


class DouyinTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "run"

    def provider(self):
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        return provider

    def run_search(self, payloads, max_items=20):
        with patch.object(douyin, "get_session_provider", return_value=self.provider()), \
             patch.object(douyin.douyin_session, "cookie_header", return_value="sessionid=secret"), \
             patch.object(douyin.douyin_session, "ms_token", return_value="ms-token"), \
             patch.object(douyin, "fetch_search", side_effect=payloads) as fetch, \
             patch.dict(os.environ, {"DOUYIN_PSEUDONYM_SALT": "synthetic-salt",
                                     "DOUYIN_MS_TOKEN": ""}):
            code = douyin.collect_douyin(
                self.output, surface="search", queries=["synthetic"], max_items=max_items,
                max_pages=2, salt="synthetic-salt", session_mode="required")
        return code, fetch

    def manifest(self):
        return json.loads((self.output / "douyin_manifest.json").read_text(encoding="utf-8"))

    def records(self):
        return [json.loads(line) for line in
                (self.output / "records.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_search_record_normalization_and_privacy(self):
        records, has_more, next_id = douyin.search_records(
            search_payload(aweme()), "synthetic", 1, 0,
            "2026-10-30T00:00:00+00:00", "synthetic-salt")
        self.assertEqual(len(records), 1)
        record = records[0].to_dict()
        self.assertEqual(record["object_id"], "123")
        self.assertEqual(record["content"], "Synthetic video")
        self.assertEqual(record["published_at"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(record["engagement"], {"likes": 4, "comments": 3,
                                                "shares": 2, "collects": 1})
        self.assertEqual(record["platform_data"]["offset"], 0)
        self.assertTrue(has_more)
        self.assertEqual(next_id, "log-1")
        saved = json.dumps(record)
        self.assertNotIn("98765", saved)
        self.assertNotIn("private-author", saved)

    def test_search_pagination_and_manifest(self):
        code, fetch = self.run_search([
            search_payload(aweme("1"), has_more=1, logid="log-1"),
            search_payload(aweme("2"), has_more=0, logid="log-2"),
        ])
        self.assertEqual(code, 0)
        manifest = self.manifest()
        self.assertEqual(manifest["transport"], "http")
        self.assertEqual(manifest["record_count"], 2)
        self.assertEqual(manifest["session_source"], "saved")
        self.assertTrue(manifest["ms_token_used"])
        self.assertEqual(manifest["objects"][0]["pages"][1]["offset"], 15)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "has_more_end")
        self.assertIn("offset=15", fetch.call_args_list[1].args[0])

    def test_search_item_limit_stops_before_next_page(self):
        code, fetch = self.run_search([search_payload(aweme("1"), aweme("2"))], max_items=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.manifest()["objects"][0]["termination_reason"], "item_limit")
        self.assertEqual(fetch.call_count, 1)

    def test_missing_login_and_ms_token_are_explicit(self):
        provider = Mock()
        provider.collection_state.return_value = ("anonymous", None)
        with patch.object(douyin, "get_session_provider", return_value=provider), \
             patch.dict(os.environ, {"DOUYIN_PSEUDONYM_SALT": "synthetic-salt"}):
            code = douyin.collect_douyin(
                self.output, surface="search", queries=["synthetic"], salt="synthetic-salt",
                session_mode="auto")
        self.assertEqual(code, 2)
        self.assertEqual(self.manifest()["error_kind"], "login_required")
        self.assertFalse((self.output / "records.jsonl").exists())

        output = Path(self.temporary.name) / "missing-token"
        with patch.object(douyin, "get_session_provider", return_value=self.provider()), \
             patch.object(douyin.douyin_session, "ms_token", return_value=None), \
             patch.dict(os.environ, {"DOUYIN_PSEUDONYM_SALT": "synthetic-salt",
                                     "DOUYIN_MS_TOKEN": ""}):
            code = douyin.collect_douyin(
                output, surface="search", queries=["synthetic"], salt="synthetic-salt",
                session_mode="required")
        self.assertEqual(code, 2)
        manifest = json.loads((output / "douyin_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["error_kind"], "missing_ms_token")

    def test_detail_record(self):
        payload = {"aweme_detail": aweme()}
        record = douyin.detail_record(payload, "123", "2026-10-30T00:00:00+00:00",
                                      "synthetic-salt").to_dict()
        self.assertEqual(record["surface"], "detail")
        self.assertEqual(record["object_id"], "123")
        self.assertEqual(record["platform_data"]["duration"], 12000)

    def test_plan_search_and_detail(self):
        search_args = build_collection_parser().parse_args([
            "--platform", "douyin", "--surface", "search", "--query", "synthetic",
            "--output", str(self.output)])
        search_plan = build_plan(search_args)
        self.assertEqual(search_plan.backend, "douyin")
        self.assertFalse(hasattr(search_plan, "implementation"))
        self.assertIn("DOUYIN_PSEUDONYM_SALT", search_plan.required_environment)
        self.assertIn("synthetic", search_plan.steps[0].command)

        detail_args = build_collection_parser().parse_args([
            "--platform", "douyin", "--surface", "detail", "--item-id", "123",
            "--output", str(self.output / "detail")])
        detail_plan = build_plan(detail_args)
        self.assertIn("--headless", detail_plan.steps[0].command)

    def test_default(self):
        self.assertEqual(get_adapter("douyin", "search").backend, 'douyin')
        self.assertEqual(get_adapter("douyin", "detail").backend, 'douyin')
    def test_cookie_header_and_ms_token_are_scoped(self):
        payload = state()
        payload["cookies"].append({"name": "", "value": "ignored",
                                   "domain": ".douyin.com", "path": "/"})
        header = douyin_session.cookie_header(payload, "https://www.douyin.com/")
        self.assertIn("sessionid=synthetic-session", header)
        self.assertNotIn("ignored", header)
        self.assertEqual(douyin_session.ms_token(payload), "synthetic-ms-token")
        with self.assertRaises(CollectionError):
            douyin_session.cookie_header(payload, "https://example.com/")


if __name__ == "__main__":
    unittest.main()
