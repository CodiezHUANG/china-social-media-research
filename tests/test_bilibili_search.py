from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from china_media_crawler.cli import main
from china_media_crawler import http as transport
from china_media_crawler.platforms import get_adapter
from china_media_crawler.platforms import bilibili_search as search
from china_media_crawler.preflight import check_platform


def fixture() -> dict:
    return json.loads(
        (ROOT / "tests/fixtures/bilibili_search_success.json").read_text(encoding="utf-8")
    )


def page(number: int, *bvids: str, pages: int = 5) -> dict:
    template = fixture()["data"]["result"][0]
    return {"code": 0, "data": {
        "page": number, "numPages": pages,
        "result": [{**template, "bvid": bvid} for bvid in bvids],
    }}


class FakeResponse(io.BytesIO):
    status = 200


class BilibiliHttpTests(unittest.TestCase):
    def test_request_is_anonymous_and_disables_proxy_inheritance(self) -> None:
        opener = Mock()
        opener.open.return_value = FakeResponse(json.dumps(fixture()).encode("utf-8"))
        with patch.object(search, "build_opener", return_value=opener) as build:
            payload = search.fetch_json(search.build_url("example & 中文", 1, "totalrank"))
        request = opener.open.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertTrue(headers["user-agent"].startswith("ChinaMediaCrawler/"))
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(payload["code"], 0)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        self.assertEqual(parse_qs(urlparse(request.full_url).query)["keyword"], ["example & 中文"])

    def test_redirect_is_rejected(self) -> None:
        with self.assertRaises(search.CollectionError) as raised:
            transport.RejectRedirect().redirect_request(None, None, 302, None, None, "elsewhere")
        self.assertEqual(raised.exception.kind, "unexpected_redirect")

    def test_http_429_is_not_retried(self) -> None:
        opener = Mock()
        opener.open.side_effect = HTTPError("example", 429, "slow down", {}, None)
        with patch.object(search, "build_opener", return_value=opener):
            with self.assertRaises(search.CollectionError) as raised:
                search.fetch_json("https://api.bilibili.com/example")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(opener.open.call_count, 1)

    def test_connection_failure_is_not_retried(self) -> None:
        opener = Mock()
        opener.open.side_effect = URLError("private connection detail")
        with patch.object(search, "build_opener", return_value=opener):
            with self.assertRaises(search.CollectionError) as raised:
                search.fetch_json("https://api.bilibili.com/example")
        self.assertEqual(str(raised.exception), "connection_failed")
        self.assertEqual(opener.open.call_count, 1)

    def test_html_response_is_not_a_successful_empty_result(self) -> None:
        opener = Mock()
        opener.open.return_value = FakeResponse(b"<html>login</html>")
        with patch.object(search, "build_opener", return_value=opener):
            with self.assertRaises(search.CollectionError) as raised:
                search.fetch_json("https://api.bilibili.com/example")
        self.assertEqual(raised.exception.kind, "unexpected_response")

    def test_oversize_response_is_rejected(self) -> None:
        opener = Mock()
        opener.open.return_value = FakeResponse(b"x" * (transport.MAX_RESPONSE_BYTES + 1))
        with patch.object(search, "build_opener", return_value=opener):
            with self.assertRaises(search.CollectionError) as raised:
                search.fetch_json("https://api.bilibili.com/example")
        self.assertEqual(raised.exception.kind, "response_too_large")


class BilibiliSchemaTests(unittest.TestCase):
    def test_fixture_is_normalized_without_raw_author_identity(self) -> None:
        record = search.normalize_record(
            fixture()["data"]["result"][0], "example", "totalrank", 1, 1,
            "2026-01-01T00:00:00+00:00", "test-salt",
        ).to_dict()
        self.assertEqual(record["content"], "An example & a film")
        self.assertEqual(record["published_at"], "2024-01-01T00:00:00+00:00")
        self.assertEqual(record["engagement"]["views"], 120)
        self.assertEqual(record["platform_data"]["description"], "Synthetic description")
        self.assertTrue(record["author_pseudonym"].startswith("u_"))
        self.assertNotIn("synthetic-author-not-for-export", json.dumps(record))
        self.assertNotIn("123456", json.dumps(record))

    def test_api_and_voucher_errors_stop(self) -> None:
        for payload in ({"code": -352}, {"code": 0, "data": {"v_voucher": "redacted"}}):
            with self.subTest(payload=payload), self.assertRaises(search.CollectionError):
                search.search_rows(payload, 1)

    def test_missing_result_and_wrong_page_stop(self) -> None:
        for payload in (
            {"code": 0, "data": {}},
            {"code": 0, "data": {"result": [], "page": 2}},
        ):
            with self.subTest(payload=payload), self.assertRaises(search.CollectionError):
                search.search_rows(payload, 1)

    def test_invalid_video_does_not_become_a_silent_skip(self) -> None:
        row = fixture()["data"]["result"][0]
        row["bvid"] = "not-a-bv"
        with self.assertRaises(search.CollectionError):
            search.normalize_record(row, "q", "totalrank", 1, 1, "time", "salt")


class BilibiliCollectionTests(unittest.TestCase):
    def read_manifest(self, output: Path) -> dict:
        return json.loads((output / "bilibili_manifest.json").read_text(encoding="utf-8"))

    def test_exact_item_cap_and_frame_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            fetch, sleep = Mock(return_value=fixture()), Mock()
            code = search.collect_search(
                "example", output, max_items=1, salt="test-salt", fetch=fetch, sleep=sleep,
            )
            records = (output / "records.jsonl").read_text(encoding="utf-8").splitlines()
            frame = (output / "bv_candidates.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(code, 0)
            self.assertEqual(len(records), 1)
            self.assertEqual(frame, [json.loads(records[0])["object_id"]])
            self.assertEqual(self.read_manifest(output)["termination_reason"], "item_limit")
            self.assertNotIn("test-salt", (output / "bilibili_manifest.json").read_text())
            self.assertEqual(fetch.call_count, 1)
            sleep.assert_not_called()

    def test_cross_page_duplicates_and_page_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            fetch = Mock(side_effect=[
                page(1, "BV1000000001", "BV1000000002"),
                page(2, "BV1000000002", "BV1000000003"),
            ])
            sleep = Mock()
            search.collect_search(
                "q", output, max_pages=2, max_items=20, salt="s", fetch=fetch, sleep=sleep,
            )
            manifest = self.read_manifest(output)
            self.assertEqual(manifest["record_count"], 3)
            self.assertEqual(manifest["duplicate_count"], 1)
            self.assertEqual(manifest["termination_reason"], "page_limit")
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once()
            self.assertGreaterEqual(sleep.call_args.args[0], 2.5)

    def test_api_failure_preserves_partial_output_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            fetch = Mock(side_effect=[page(1, "BV1000000001"), {"code": -412}])
            code = search.collect_search("q", output, salt="s", fetch=fetch, sleep=Mock())
            manifest = self.read_manifest(output)
            self.assertEqual(code, 2)
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["record_count"], 1)
            self.assertEqual(manifest["api_code"], -412)
            self.assertEqual(manifest["failed_page"], 2)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual((output / "bv_candidates.txt").read_text().strip(), "BV1000000001")

    def test_valid_empty_page_is_complete_and_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            search.collect_search("q", output, salt="s", fetch=Mock(return_value=page(1)))
            manifest = self.read_manifest(output)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["termination_reason"], "empty_page")
            self.assertEqual(manifest["record_count"], 0)

    def test_reported_last_page_prevents_extra_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            fetch = Mock(return_value=page(1, "BV1000000001", pages=1))
            search.collect_search("q", output, salt="s", fetch=fetch)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(self.read_manifest(output)["termination_reason"], "reported_end")

    def test_bad_record_fails_whole_page_before_export(self) -> None:
        payload = copy.deepcopy(fixture())
        payload["data"]["result"][1]["title"] = None
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            self.assertEqual(search.collect_search(
                "q", output, salt="s", fetch=Mock(return_value=payload),
            ), 2)
            self.assertEqual((output / "records.jsonl").read_text(), "")
            manifest = self.read_manifest(output)
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failed_record_position"], 2)
            self.assertIn("bvid", manifest["failed_record_fields"])
            self.assertNotIn("synthetic-author-not-for-export", json.dumps(manifest))

    def test_second_run_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            search.collect_search("q", output, salt="s", fetch=Mock(return_value=page(1)))
            before = (output / "bilibili_manifest.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "overwrite"):
                search.collect_search("q", output, salt="s", fetch=Mock())
            self.assertEqual((output / "bilibili_manifest.json").read_bytes(), before)

    def test_missing_salt_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaisesRegex(ValueError, "SALT"):
                search.collect_search("q", output, salt="", fetch=Mock())
            self.assertFalse(output.exists())

    def test_course_cards_are_excluded_and_original_positions_preserved(self) -> None:
        payload = fixture()
        payload["data"]["result"].insert(0, {
            "type": "ketang", "bvid": "", "title": "Synthetic course",
        })
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            code = search.collect_search(
                "q", output, max_items=1, salt="s", fetch=Mock(return_value=payload),
            )
            self.assertEqual(code, 0)
            record = json.loads((output / "records.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["platform_data"]["search_position"], 2)
            manifest = self.read_manifest(output)
            self.assertEqual(manifest["excluded_count"], 1)
            self.assertEqual(manifest["pages"][0]["eligible"], 2)
            self.assertEqual(manifest["pages"][0]["not_exported"], 1)
            self.assertEqual(manifest["pages"][0]["excluded"][0]["type"], "ketang")
            self.assertNotIn("Synthetic course", json.dumps(manifest))

    def test_unknown_card_types_still_fail_even_with_valid_bv(self) -> None:
        payload = fixture()
        payload["data"]["result"][0]["type"] = "unknown-new-card"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            self.assertEqual(search.collect_search(
                "q", output, salt="s", fetch=Mock(return_value=payload),
            ), 2)
            self.assertEqual(self.read_manifest(output)["error_kind"], "unexpected_result_type")

    def test_video_with_missing_bv_is_not_treated_as_course(self) -> None:
        payload = fixture()
        payload["data"]["result"][0].update(type="video", bvid="")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            self.assertEqual(search.collect_search(
                "q", output, salt="s", fetch=Mock(return_value=payload),
            ), 2)
            self.assertEqual(self.read_manifest(output)["error_kind"], "unexpected_video_id")

    def test_course_only_page_does_not_mean_end_of_video_results(self) -> None:
        course_page = page(1)
        course_page["data"]["result"] = [{"type": "ketang", "bvid": ""}]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            fetch = Mock(side_effect=[course_page, page(2, "BV1000000001")])
            self.assertEqual(search.collect_search(
                "q", output, salt="s", max_items=1, fetch=fetch, sleep=Mock(),
            ), 0)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.read_manifest(output)["record_count"], 1)


class BilibiliIntegrationTests(unittest.TestCase):
    def test_preview_requires_no_external_crawler_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            buffer = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(buffer):
                code = main([
                    "plan", "--platform", "bilibili",
                    "--query", "example", "--max-items", "1", "--output", str(output),
                ])
            plan = json.loads(buffer.getvalue())
            self.assertEqual(code, 0)
            self.assertNotIn("implementation", plan)
            self.assertEqual(plan["required_environment"], ["BILI_PSEUDONYM_SALT"])
            self.assertNotIn("BILIBILI_COOKIE", plan["required_environment"])
            self.assertIn("--session", plan["steps"][0]["command"])
            self.assertFalse(output.exists())

    def test_preflight_needs_only_salt_and_python(self) -> None:
        with patch.dict(os.environ, {"BILI_PSEUDONYM_SALT": "test-value"}, clear=True):
            result = check_platform("bilibili", "search")
        self.assertTrue(result["ready"])
        self.assertNotIn("test-value", str(result))

    def test_auto_selects_validated_routes(self) -> None:
        self.assertEqual(get_adapter("bilibili", "search").backend, 'bilibili-public-search')
        self.assertEqual(get_adapter("bilibili", "detail").backend, 'bilibili-public-content')
        self.assertEqual(get_adapter("bilibili", "comments").backend, 'bilibili-public-content')

if __name__ == "__main__":
    unittest.main()
