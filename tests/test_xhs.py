from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_media_crawler import xhs_session
from china_media_crawler.cli import build_collection_parser
from china_media_crawler.models import plan_dict
from china_media_crawler.planning import build_plan
from china_media_crawler.platforms import get_adapter
from china_media_crawler.platforms import xhs


def note(identifier="66aabbccddeeff0011223344", title="Synthetic title",
         desc="Synthetic note body", user="98765"):
    return {
        "note_id": identifier,
        "display_title": title,
        "desc": desc,
        "type": "normal",
        "time": 1700000000000,
        "user": {"user_id": user, "nickname": "private-author"},
        "interact_info": {"liked_count": 4, "collected_count": 3,
                          "comment_count": 2, "shared_count": 1},
    }


def search_payload(*notes, has_more=1):
    return {"data": {"items": [{"note_card": value} for value in notes],
                     "has_more": has_more}}


def detail_payload(value=None):
    return {"data": {"items": [{"note_card": value or note()}]}}


def state():
    return {
        "cookies": [{"name": "web_session", "value": "synthetic-session",
                     "domain": ".xiaohongshu.com", "path": "/"}],
        "origins": [],
    }


class XhsTests(unittest.TestCase):
    def test_note_normalization_and_privacy(self):
        record = xhs.note_record(note(), "search", "2026-10-30T00:00:00+00:00",
                                 "synthetic-salt", "电影", 1).to_dict()
        self.assertEqual(record["object_id"], "66aabbccddeeff0011223344")
        self.assertEqual(record["content"], "Synthetic note body")
        self.assertEqual(record["published_at"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(record["engagement"], {"likes": 4, "collects": 3,
                                                "comments": 2, "shares": 1})
        self.assertEqual(record["platform_data"]["title"], "Synthetic title")
        saved = json.dumps(record)
        self.assertNotIn("98765", saved)
        self.assertNotIn("private-author", saved)

    def test_search_and_detail_payloads(self):
        records, has_more = xhs.search_payload(
            search_payload(note()), "电影", 1, "2026-10-30T00:00:00+00:00", "salt")
        self.assertEqual(len(records), 1)
        self.assertTrue(has_more)
        detail = xhs.detail_payload(detail_payload(), "2026-10-30T00:00:00+00:00", "salt")
        self.assertEqual(detail.surface, "detail")

    def test_plan_search_and_detail(self):
        output = Path(tempfile.mkdtemp()) / "run"
        search_args = build_collection_parser().parse_args([
            "--platform", "xhs", "--surface", "search", "--query", "电影",
            "--output", str(output)])
        search_plan = build_plan(search_args)
        self.assertEqual(search_plan.backend, "xhs")
        self.assertFalse(hasattr(search_plan, "implementation"))
        self.assertIn("XHS_PSEUDONYM_SALT", search_plan.required_environment)
        self.assertIn("--headless", search_plan.steps[0].command)

        url = ("https://www.xiaohongshu.com/explore/66aabbccddeeff0011223344"
               "?xsec_token=synthetic&xsec_source=pc_search")
        detail_args = build_collection_parser().parse_args([
            "--platform", "xhs", "--surface", "detail", "--item-url", url,
            "--output", str(output / "detail")])
        detail_plan = build_plan(detail_args)
        self.assertEqual(detail_plan.backend, "xhs")
        self.assertIn(url, detail_plan.steps[0].command)
        preview = json.dumps(plan_dict(detail_plan))
        self.assertNotIn("xsec_token=synthetic", preview)
        self.assertIn("xsec_token=REDACTED", preview)
        self.assertIn("xsec_source=pc_search", preview)

    def test_default(self):
        self.assertEqual(get_adapter("xhs", "search").backend, 'xhs')
        self.assertEqual(get_adapter("xhs", "detail").backend, 'xhs')
    def test_cookie_header_is_scoped(self):
        header = xhs_session.cookie_header(state(), "https://www.xiaohongshu.com/")
        self.assertIn("web_session=synthetic-session", header)
        with self.assertRaises(xhs.CollectionError):
            xhs_session.cookie_header(state(), "https://example.com/")

    def test_guest_cookie_does_not_save_rednote_login(self):
        store = xhs_session.store()
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.url = "https://www.rednote.com/"
        page.is_closed.return_value = False
        page.goto.return_value.status = 200
        context.cookies.return_value = [{
            "name": "web_session", "value": "synthetic-guest",
            "domain": ".rednote.com", "path": "/", "expires": -1,
        }]

        def evaluate(script):
            if "document.body.innerText" in script:
                return "登录后查看搜索结果"
            return False

        page.evaluate.side_effect = evaluate
        with patch("china_media_crawler.browser_session.time.monotonic",
                   side_effect=[0, 0, 16]), \
             patch.object(store, "save") as save:
            with self.assertRaises(xhs.CollectionError) as caught:
                store.login(15, lambda: driver)
        self.assertEqual(caught.exception.kind, "login_timeout")
        save.assert_not_called()

    def test_rednote_login_url_is_allowed_and_foreign_hosts_are_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = xhs_session.main([
                "--action", "status", "--login-url", "https://www.rednote.com/",
            ])
        self.assertEqual(code, 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            xhs_session.main([
                "--action", "status", "--login-url", "https://example.com/",
            ])
        self.assertEqual(caught.exception.code, 2)

    def test_missing_login_fails_before_browser(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("anonymous", None)
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.dict(os.environ, {"XHS_PSEUDONYM_SALT": "salt"}):
            code = xhs.collect_xhs(output, surface="search", queries=["电影"],
                                   salt="salt", session_mode="auto")
        self.assertEqual(code, 2)
        manifest = json.loads((output / "xhs_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["error_kind"], "login_required")
        self.assertFalse((output / "records.jsonl").exists())

    def test_validation_errors_carry_safe_code_stage_and_field(self):
        with self.assertRaises(xhs.CollectionError) as caught:
            xhs.search_payload({"data": {"items": "invalid"}}, "电影", 1,
                               "2026-10-30T00:00:00+00:00", "salt")
        self.assertEqual(caught.exception.kind, "unexpected_search_schema")
        self.assertEqual(caught.exception.validation_code, "unexpected_search_schema")
        self.assertEqual(caught.exception.validation_stage, "search_payload")
        self.assertEqual(caught.exception.validation_field, "items")

        broken = note()
        broken["user"] = None
        with self.assertRaises(xhs.CollectionError) as caught:
            xhs.note_record(broken, "search", "2026-10-30T00:00:00+00:00", "salt")
        self.assertEqual(caught.exception.validation_code, "unexpected_note_schema")
        self.assertEqual(caught.exception.validation_stage, "note_record")
        self.assertEqual(caught.exception.validation_field, "user")

    def test_validation_fields_are_written_to_failed_manifest(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        response = Mock()
        response.url = "https://www.xiaohongshu.com/api/sns/web/v1/search/notes"
        response.status = 200
        response.json.return_value = {"data": {"items": "invalid"}}
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.xiaohongshu.com/search_result?keyword=test"
        callbacks = {}
        page.on.side_effect = lambda event, callback: callbacks.setdefault("response", callback)
        page.goto.side_effect = lambda *args, **kwargs: callbacks["response"](response)
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.object(xhs.xhs_session, "preferred_host",
                          return_value="https://www.xiaohongshu.com"), \
             patch("playwright.sync_api.sync_playwright", return_value=driver):
            code = xhs.collect_xhs(
                output, surface="search", queries=["电影"], salt="test-salt",
                session_mode="required",
            )
        self.assertEqual(code, 2)
        manifest = json.loads((output / "xhs_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error_kind"], "unexpected_search_schema")
        self.assertEqual(manifest["validation_code"], "unexpected_search_schema")
        self.assertEqual(manifest["validation_stage"], "search_payload")
        self.assertEqual(manifest["validation_field"], "items")

    def test_response_payload_rejects_http_and_api_errors(self):
        response = Mock()
        response.status = 429
        response.url = "https://www.xiaohongshu.com/api/sns/web/v1/search/notes"
        with self.assertRaises(xhs.CollectionError) as caught:
            xhs.response_payload(response)
        self.assertEqual(caught.exception.kind, "http_error")
        self.assertEqual(caught.exception.http_status, 429)

        response.status = 200
        response.json.return_value = {"success": False, "code": 300012,
                                      "data": {"items": []}}
        with self.assertRaises(xhs.CollectionError) as caught:
            xhs.response_payload(response)
        self.assertEqual(caught.exception.kind, "api_error")
        self.assertEqual(caught.exception.api_code, 300012)

    def test_http_429_is_failed_not_empty_complete(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        response = Mock()
        response.url = "https://www.xiaohongshu.com/api/sns/web/v1/search/notes"
        response.status = 429
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.xiaohongshu.com/search_result?keyword=test"
        callbacks = {}
        page.on.side_effect = lambda event, callback: callbacks.setdefault("response", callback)
        page.goto.side_effect = lambda *args, **kwargs: callbacks["response"](response)
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.object(xhs.xhs_session, "preferred_host",
                          return_value="https://www.xiaohongshu.com"), \
             patch("playwright.sync_api.sync_playwright", return_value=driver):
            code = xhs.collect_xhs(
                output, surface="search", queries=["电影"], salt="test-salt",
                session_mode="required",
            )
        self.assertEqual(code, 2)
        manifest = json.loads((output / "xhs_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error_kind"], "http_error")
        self.assertEqual(manifest["http_status"], 429)
        self.assertEqual(manifest["record_count"], 0)

    def test_api_error_is_failed_with_safe_code(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        response = Mock()
        response.url = "https://www.xiaohongshu.com/api/sns/web/v1/search/notes"
        response.status = 200
        response.json.return_value = {"success": False, "code": 300012,
                                      "data": {"items": []}}
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.xiaohongshu.com/search_result?keyword=test"
        callbacks = {}
        page.on.side_effect = lambda event, callback: callbacks.setdefault("response", callback)
        page.goto.side_effect = lambda *args, **kwargs: callbacks["response"](response)
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.object(xhs.xhs_session, "preferred_host",
                          return_value="https://www.xiaohongshu.com"), \
             patch("playwright.sync_api.sync_playwright", return_value=driver):
            code = xhs.collect_xhs(
                output, surface="search", queries=["电影"], salt="test-salt",
                session_mode="required",
            )
        self.assertEqual(code, 2)
        manifest = json.loads((output / "xhs_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error_kind"], "api_error")
        self.assertEqual(manifest["api_code"], 300012)
        self.assertEqual(manifest["record_count"], 0)

    def test_next_page_timeout_is_failed_not_complete(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        response = Mock()
        response.url = "https://www.xiaohongshu.com/api/sns/web/v1/search/notes"
        response.status = 200
        response.json.return_value = search_payload(note(), has_more=1)
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.xiaohongshu.com/search_result?keyword=test"
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.object(xhs.xhs_session, "preferred_host",
                          return_value="https://www.xiaohongshu.com"), \
             patch.object(xhs, "wait_for_response", side_effect=[response, None]), \
             patch("playwright.sync_api.sync_playwright", return_value=driver):
            code = xhs.collect_xhs(
                output, surface="search", queries=["电影"], salt="test-salt",
                session_mode="required", max_pages=5,
            )
        self.assertEqual(code, 2)
        manifest = json.loads((output / "xhs_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error_kind"], "search_response_timeout")
        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(manifest["partial_object"]["termination_reason"], "response_timeout")
        self.assertEqual(len(manifest["partial_object"]["pages"]), 1)

    def test_detail_manifest_url_strips_token(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        response = Mock()
        response.url = "https://www.xiaohongshu.com/api/sns/web/v1/feed"
        response.status = 200
        response.json.return_value = detail_payload(note())
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.xiaohongshu.com/explore/66aabbccddeeff0011223344"
        callbacks = {}
        page.on.side_effect = lambda event, callback: callbacks.setdefault("response", callback)
        page.goto.side_effect = lambda *args, **kwargs: callbacks["response"](response)
        url = ("https://www.xiaohongshu.com/explore/66aabbccddeeff0011223344"
               "?xsec_token=synthetic-token&xsec_source=pc_search")
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.object(xhs.xhs_session, "preferred_host",
                          return_value="https://www.xiaohongshu.com"), \
             patch("playwright.sync_api.sync_playwright", return_value=driver):
            code = xhs.collect_xhs(
                output, surface="detail", urls=[url], salt="test-salt",
                session_mode="required",
            )
        self.assertEqual(code, 0)
        raw = (output / "xhs_manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("synthetic-token", raw)
        manifest = json.loads(raw)
        self.assertEqual(manifest["objects"][0]["url"],
                         "https://www.xiaohongshu.com/explore/66aabbccddeeff0011223344")
        self.assertEqual(manifest["http_request_count"], 1)

    def test_detail_api_response_must_match_requested_note(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        response = Mock()
        response.url = "https://www.xiaohongshu.com/api/sns/web/v1/feed"
        response.status = 200
        response.json.return_value = detail_payload(note("77aabbccddeeff0011223344"))
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.xiaohongshu.com/explore/66aabbccddeeff0011223344"
        callbacks = {}
        page.on.side_effect = lambda event, callback: callbacks.setdefault("response", callback)
        page.goto.side_effect = lambda *args, **kwargs: callbacks["response"](response)
        url = ("https://www.xiaohongshu.com/explore/66aabbccddeeff0011223344"
               "?xsec_token=synthetic-token&xsec_source=pc_search")
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.object(xhs.xhs_session, "preferred_host",
                          return_value="https://www.xiaohongshu.com"), \
             patch("playwright.sync_api.sync_playwright", return_value=driver):
            code = xhs.collect_xhs(
                output, surface="detail", urls=[url], salt="test-salt",
                session_mode="required",
            )
        self.assertEqual(code, 2)
        manifest = json.loads((output / "xhs_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["error_kind"], "unexpected_detail_target")
        self.assertEqual(manifest["validation_stage"], "detail_payload")
        self.assertEqual(manifest["record_count"], 0)
        self.assertEqual(manifest["http_request_count"], 1)
        self.assertEqual((output / "records.jsonl").read_text(encoding="utf-8"), "")

    def test_detail_state_fallback_uses_requested_note_and_counts(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.xiaohongshu.com/explore/66aabbccddeeff0011223344"
        page.evaluate.return_value = {
            "note_id": "66aabbccddeeff0011223344", "title": "State title",
            "desc": "State body", "type": "normal", "time": 1700000000000,
            "user_id": "98765", "liked_count": 1, "collected_count": 0,
            "comment_count": 0, "shared_count": 0,
        }
        url = ("https://www.xiaohongshu.com/explore/66aabbccddeeff0011223344"
               "?xsec_token=synthetic-token&xsec_source=pc_search")
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.object(xhs.xhs_session, "preferred_host",
                          return_value="https://www.xiaohongshu.com"), \
             patch.object(xhs, "wait_for_response", return_value=None), \
             patch("playwright.sync_api.sync_playwright", return_value=driver):
            code = xhs.collect_xhs(
                output, surface="detail", urls=[url], salt="test-salt",
                session_mode="required",
            )
        self.assertEqual(code, 0)
        manifest = json.loads((output / "xhs_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["http_request_count"], 1)
        self.assertEqual(manifest["objects"][0]["note_id"],
                         "66aabbccddeeff0011223344")
        self.assertEqual(page.evaluate.call_args.args[1],
                         "66aabbccddeeff0011223344")

    def test_detail_response_is_not_used_as_search_response(self):
        output = Path(tempfile.mkdtemp()) / "run"
        provider = Mock()
        provider.collection_state.return_value = ("saved", state())
        detail = Mock()
        detail.url = "https://www.xiaohongshu.com/api/sns/web/v1/feed"
        detail.status = 200
        detail.json.return_value = detail_payload(note(
            "77aabbccddeeff0011223344", "Detail title", "Detail body"))
        search = Mock()
        search.url = "https://www.xiaohongshu.com/api/sns/web/v1/search/notes"
        search.status = 200
        search.json.return_value = search_payload(note(
            "66aabbccddeeff0011223344", "Search title", "Search body"), has_more=0)
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.xiaohongshu.com/search_result?keyword=test"
        callbacks = {}
        page.on.side_effect = lambda event, callback: callbacks.setdefault("response", callback)
        page.goto.side_effect = lambda *args, **kwargs: (
            callbacks["response"](detail), callbacks["response"](search))
        with patch.object(xhs, "get_session_provider", return_value=provider), \
             patch.object(xhs.xhs_session, "preferred_host",
                          return_value="https://www.xiaohongshu.com"), \
             patch("playwright.sync_api.sync_playwright", return_value=driver):
            code = xhs.collect_xhs(
                output, surface="search", queries=["电影"], salt="test-salt",
                session_mode="required",
            )
        self.assertEqual(code, 0)
        records = [json.loads(line) for line in
                   (output / "records.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["object_id"], "66aabbccddeeff0011223344")
        self.assertEqual(records[0]["content"], "Search body")
        manifest = json.loads((output / "xhs_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["http_request_count"], 1)


if __name__ == "__main__":
    unittest.main()






