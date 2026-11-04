from __future__ import annotations

import copy
import gzip
import io
import json
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from china_media_crawler.platforms import bilibili_content as content
from china_media_crawler.platforms.bilibili_search import CollectionError
from china_media_crawler.cli import build_parser
from china_media_crawler.planning import build_plan

BVID = "BV1000000001"


def video() -> dict:
    return {"code": 0, "data": {
        "bvid": BVID, "aid": 123, "title": "Example video", "desc": "Synthetic description",
        "owner": {"mid": 321, "name": "private-synthetic-author"}, "pubdate": 1704067200,
        "duration": 120, "stat": {"view": 40, "like": 2},
        "pages": [{"cid": 456, "page": 1}, {"cid": 457, "page": 2}],
    }}


def reply(identifier: int = 1) -> dict:
    return {"rpid": identifier, "root": 0, "parent": 0, "ctime": 1704067200,
            "member": {"mid": 987, "uname": "private-commenter"},
            "content": {"message": "Synthetic comment"}, "like": 1,
            "replies": [{"content": {"message": "nested-must-not-export"}}]}


def nested(identifier: int = 11, parent: int = 1) -> dict:
    return {"rpid": identifier, "root": parent, "parent": parent, "ctime": 1704067201,
            "member": {"mid": 988, "uname": "private-nested-commenter"},
            "content": {"message": "Synthetic nested comment"}, "like": 0}


def comments(page: int = 1, rows: list | None = None, count: int = 1) -> dict:
    return {"code": 0, "data": {"page": {"num": page, "count": count},
                                  "replies": rows}}


def xml(cid: int = 456, n: int = 3) -> bytes:
    items = ''.join(f'<d p="1.5,1,25,16777215,1704067200,0,private-author-hash,{i}">Text {i}</d>'
                    for i in range(1, n + 1))
    return f'<i><chatid>{cid}</chatid>{items}</i>'.encode()


class ContentTests(unittest.TestCase):
    def client(self, *payloads: dict, bodies: list[bytes] | None = None) -> content.PublicClient:
        return content.PublicClient(Mock(side_effect=payloads), Mock(side_effect=bodies), Mock())

    def run_sample(self, surface: str, client: content.PublicClient, **kwargs) -> tuple:
        kwargs.setdefault("precheck", False)
        if surface == "comments":
            kwargs.setdefault("comments_mode", "deep")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            code = content.collect_content([BVID], output, surface=surface, salt="test-salt",
                                           client=client, **kwargs)
            rows = [json.loads(line) for line in (output / "records.jsonl").read_text().splitlines()]
            manifest = json.loads((output / f"bilibili_{surface}_manifest.json").read_text())
            return code, rows, manifest

    def test_video_metadata_has_no_raw_identity(self) -> None:
        client = self.client(video())
        code, rows, manifest = self.run_sample("detail", client)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["engagement"]["view"], 40)
        self.assertNotIn("private-synthetic-author", json.dumps(rows))
        self.assertNotIn('"mid"', json.dumps(rows))
        self.assertEqual(manifest["http_request_count"], 1)
        client.sleep.assert_not_called()

    def test_comments_are_first_level_only_and_capped(self) -> None:
        client = self.client(video(), comments(rows=[reply(1), reply(2)], count=2))
        code, rows, manifest = self.run_sample("comments", client, comments_per_item=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("nested-must-not-export", json.dumps(rows))
        self.assertNotIn("private-commenter", json.dumps(rows))
        self.assertEqual(manifest["objects"][0]["termination_reason"], "item_limit")
        client.sleep.assert_called_once()

    def test_valid_zero_comments_is_not_a_failure(self) -> None:
        code, rows, manifest = self.run_sample("comments", self.client(video(), comments(count=0)))
        self.assertEqual(code, 0)
        self.assertEqual(rows, [])
        self.assertEqual(manifest["objects"][0]["termination_reason"], "reported_end")

    def test_missing_comments_despite_positive_count_is_pagination_unavailable(self) -> None:
        code, _, manifest = self.run_sample("comments", self.client(video(), comments(count=20)))
        self.assertEqual(code, 2)
        self.assertEqual(manifest["error_kind"], "comment_pagination_unavailable")

    def test_nested_top_level_row_fails(self) -> None:
        row = reply()
        row["root"] = 2
        code, rows, manifest = self.run_sample(
            "comments", self.client(video(), comments(rows=[row])),
        )
        self.assertEqual(code, 2)
        self.assertFalse(rows)
        self.assertEqual(manifest["error_kind"], "unexpected_nested_comment")

    def test_comment_page_budget_and_deduplication(self) -> None:
        client = self.client(video(), comments(1, [reply(1), reply(2)], 100),
                             comments(2, [reply(2), reply(3)], 100))
        code, rows, manifest = self.run_sample("comments", client, max_pages=2)
        self.assertEqual(code, 0)
        self.assertEqual([row["object_id"] for row in rows], ["1", "2", "3"])
        self.assertEqual(manifest["objects"][0]["termination_reason"], "page_limit")

    def test_comment_failure_preserves_previous_page(self) -> None:
        client = self.client(video(), comments(1, [reply()], 100), {"code": -352})
        code, rows, manifest = self.run_sample("comments", client)
        self.assertEqual(code, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(len(manifest["partial_object"]["pages"]), 1)
        self.assertEqual(client.requests, 3)

    def test_danmaku_is_separate_and_bounded(self) -> None:
        client = self.client(video(), bodies=[xml()])
        code, rows, manifest = self.run_sample("danmaku", client, danmaku_per_item=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform_data"]["progress_seconds"], 1.5)
        self.assertNotIn("private-author-hash", json.dumps(rows))
        self.assertEqual(manifest["objects"][0]["termination_reason"], "item_limit")
        self.assertEqual(client.requests, 2)
        self.assertEqual(client.json_fetch.call_count, 1)

    def test_default_danmaku_samples_first_part_only(self) -> None:
        code, rows, manifest = self.run_sample("danmaku", self.client(video(), bodies=[xml()]))
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "part_limit")

    def test_danmaku_cap_is_shared_across_parts(self) -> None:
        client = self.client(video(), bodies=[xml(456, 2), xml(457, 2)])
        code, rows, manifest = self.run_sample("danmaku", client, max_parts=2, danmaku_per_item=3)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(manifest["objects"][0]["pages"]), 2)

    def test_empty_danmaku_is_valid_but_wrong_cid_is_not(self) -> None:
        self.assertEqual(content.danmaku_records(xml(n=0), BVID, "456", 1, "s"), [])
        with self.assertRaises(CollectionError):
            content.danmaku_records(xml(457), BVID, "456", 1, "s")

    def test_html_and_entity_xml_fail(self) -> None:
        for body in (b'<html>challenge</html>', b'<!DOCTYPE i [<!ENTITY a "x">]><i/>'):
            with self.subTest(body=body), self.assertRaises(CollectionError):
                content.danmaku_records(body, BVID, "456", 1, "s")

    def test_metadata_must_match_requested_bv(self) -> None:
        payload = video()
        payload["data"]["bvid"] = "BV1000000002"
        code, rows, manifest = self.run_sample("detail", self.client(payload))
        self.assertEqual(code, 2)
        self.assertFalse(rows)
        self.assertEqual(manifest["error_kind"], "unexpected_video_schema")

    def test_backend_failure_stops_before_second_video(self) -> None:
        client = self.client({"code": -412})
        with tempfile.TemporaryDirectory() as temporary:
            code = content.collect_content([BVID, "BV1000000002"], Path(temporary),
                                           surface="detail", salt="s", client=client,
                                           precheck=False)
        self.assertEqual(code, 2)
        self.assertEqual(client.requests, 1)

    def test_plans_freeze_ids_and_do_not_require_old_backend(self) -> None:
        for surface in ("detail", "comments", "danmaku"):
            args = build_parser().parse_args([
                "plan", "--platform", "bilibili", "--surface", surface,
                "--item-id", BVID, "--output", "unused",
            ])
            plan = build_plan(args)
            self.assertFalse(hasattr(plan, "implementation"))
            self.assertEqual(plan.required_environment, ["BILI_PSEUDONYM_SALT"])
            self.assertIn(BVID, plan.steps[0].command)
            self.assertNotIn("BILIBILI_COOKIE", plan.required_environment)
            self.assertIn("--session", plan.steps[0].command)

    def test_danmaku_http_error_does_not_retry(self) -> None:
        opener = Mock()
        opener.open.side_effect = HTTPError("example", 429, "limited", {}, None)
        with patch.object(content, "build_opener", return_value=opener):
            with self.assertRaises(CollectionError) as raised:
                content.fetch_xml("https://comment.bilibili.com/456.xml")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(opener.open.call_count, 1)

    def test_xml_failure_preserves_completed_part(self) -> None:
        client = self.client(video(), bodies=[xml(456), b"<html>challenge</html>"])
        code, rows, manifest = self.run_sample("danmaku", client, max_parts=2)
        self.assertEqual(code, 2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(manifest["record_count"], 3)
        self.assertEqual(manifest["error_kind"], "unexpected_xml")

    def test_invalid_limits_fail_before_network_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            client = self.client(video())
            with self.assertRaises(ValueError):
                content.collect_content([BVID], output, surface="danmaku", salt="s",
                                        max_parts=6, client=client)
            self.assertEqual(client.requests, 0)
            self.assertFalse(output.exists())

    def test_xml_deflate_and_gzip_are_decoded(self) -> None:
        body = xml()
        for encoding, wire in (("identity", body), ("deflate", zlib.compress(body)),
                               ("gzip", gzip.compress(body))):
            with self.subTest(encoding=encoding):
                self.assertEqual(content.decode_xml(wire, encoding), body)

    def test_xml_decompression_has_a_decoded_size_limit(self) -> None:
        wire = zlib.compress(b"x" * (content.MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(CollectionError) as raised:
            content.decode_xml(wire, "deflate")
        self.assertEqual(raised.exception.kind, "response_too_large")

    def test_raw_http_deflate_is_decoded_with_the_same_limit(self) -> None:
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        wire = compressor.compress(xml()) + compressor.flush()
        self.assertEqual(content.decode_xml(wire, "deflate"), xml())
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        bomb = compressor.compress(b"x" * (content.MAX_RESPONSE_BYTES + 1)) + compressor.flush()
        with self.assertRaises(CollectionError) as raised:
            content.decode_xml(bomb, "deflate")
        self.assertEqual(raised.exception.kind, "response_too_large")

    def test_truncated_and_unknown_encodings_fail(self) -> None:
        for body, encoding in ((zlib.compress(xml())[:-2], "deflate"), (xml(), "br"),
                               (zlib.compress(xml()) + b"trailing", "deflate")):
            with self.subTest(encoding=encoding), self.assertRaises(CollectionError):
                content.decode_xml(body, encoding)

    def test_xml_transport_honors_content_encoding(self) -> None:
        response = io.BytesIO(zlib.compress(xml()))
        response.status = 200
        response.headers = {"Content-Encoding": "deflate"}
        opener = Mock()
        opener.open.return_value = response
        with patch.object(content, "build_opener", return_value=opener):
            self.assertEqual(content.fetch_xml(content.DANMAKU_URL + "?oid=456"), xml())
        self.assertEqual(opener.open.call_count, 1)

    def test_transport_diagnostics_include_type_but_not_message(self) -> None:
        opener = Mock()
        opener.open.side_effect = URLError(ConnectionResetError("private network message"))
        with patch.object(content, "build_opener", return_value=opener):
            with self.assertRaises(CollectionError) as raised:
                content.fetch_xml(content.DANMAKU_URL + "?oid=456")
        self.assertEqual(raised.exception.transport_error_type, "ConnectionResetError")
        self.assertNotIn("private network message", str(raised.exception))

    def test_danmaku_uses_one_fixed_api_endpoint(self) -> None:
        client = self.client(video(), bodies=[xml()])
        self.run_sample("danmaku", client)
        url = client.xml_fetch.call_args.args[0]
        self.assertEqual(url, content.DANMAKU_URL + "?oid=456")
        self.assertNotIn("comment.bilibili.com", url)

    def test_invalid_unicode_and_oversized_identifiers_fail_cleanly(self) -> None:
        for value in ("²", "１２３", "1" * 100, True, 0, "-1"):
            with self.subTest(value=value), self.assertRaises(CollectionError) as caught:
                content.positive_id(value)
            self.assertEqual(caught.exception.kind, "unexpected_identifier")

    def test_comment_pagination_rejects_boolean_page_and_changed_page_size(self) -> None:
        for override in ({"num": True}, {"size": 10}, {"size": True}):
            payload = comments(rows=[reply()])
            payload["data"]["page"].update(override)
            code, rows, manifest = self.run_sample("comments", self.client(video(), payload))
            self.assertEqual(code, 2)
            self.assertEqual(rows, [])
            self.assertEqual(manifest["error_kind"], "unexpected_comment_page")
            self.assertEqual(manifest["failed_page"], 1)

    def test_utf16_dtd_is_rejected_before_entity_expansion(self) -> None:
        text = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE i [<!ENTITY x "hidden">]><i><chatid>456</chatid></i>'
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding), self.assertRaises(CollectionError) as caught:
                content.danmaku_records(text.encode(encoding), BVID, "456", 1, "s")
            self.assertEqual(caught.exception.kind, "unsafe_xml")

    def test_valid_utf16_xml_remains_supported(self) -> None:
        text = '<?xml version="1.0" encoding="UTF-16"?>' + xml().decode()
        self.assertEqual(len(content.danmaku_records(text.encode("utf-16"), BVID, "456", 1, "s")), 3)

    def test_duplicate_cids_and_boolean_part_numbers_stop_before_xml_request(self) -> None:
        for change in ("duplicate", "boolean"):
            payload = video()
            if change == "duplicate":
                payload["data"]["pages"][1]["cid"] = 456
            else:
                payload["data"]["pages"][0]["page"] = True
            client = self.client(payload)
            code, rows, manifest = self.run_sample("danmaku", client, max_parts=2)
            self.assertEqual(code, 2)
            self.assertEqual(rows, [])
            self.assertEqual(manifest["error_kind"], "unexpected_video_parts")
            self.assertEqual(manifest["failure_stage"], "video_parts")
            client.xml_fetch.assert_not_called()

    def test_later_comment_failure_reports_page_and_partial_status(self) -> None:
        client = self.client(video(), comments(1, [reply()], 100), {"code": -352})
        code, rows, manifest = self.run_sample("comments", client)
        self.assertEqual(code, 2)
        self.assertEqual(manifest["failed_page"], 2)
        self.assertEqual(manifest["failure_stage"], "comments_page")
        self.assertEqual(manifest["partial_object"]["records"], len(rows))
        self.assertEqual(manifest["partial_object"]["termination_reason"], "failed")
        self.assertEqual(client.requests, 3)

    def test_later_part_failure_reports_cid_and_keeps_completed_part(self) -> None:
        client = self.client(video(), bodies=[xml(), b'<html>challenge</html>'])
        code, rows, manifest = self.run_sample("danmaku", client, max_parts=2)
        self.assertEqual(code, 2)
        self.assertEqual(manifest["failed_part"], 2)
        self.assertEqual(manifest["failed_cid"], "457")
        self.assertEqual(manifest["failure_stage"], "danmaku_part")
        self.assertEqual(manifest["partial_object"]["records"], len(rows))
        self.assertEqual(manifest["partial_object"]["termination_reason"], "failed")

    def test_write_failure_keeps_exact_completed_record_count(self) -> None:
        original = content.Record.to_dict
        calls = 0
        def fail_second(record):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic write interruption")
            return original(record)
        client = self.client(video(), comments(rows=[reply(1), reply(2)], count=2))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with patch.object(content.Record, "to_dict", fail_second), self.assertRaises(OSError):
                content.collect_content([BVID], output, surface="comments", salt="s",
                                        client=client, comments_mode="deep", precheck=False)
            manifest = json.loads((output / "bilibili_comments_manifest.json").read_text())
            self.assertEqual(manifest["record_count"], 1)
            self.assertEqual(manifest["partial_object"]["records"], 1)
            self.assertEqual(manifest["failure_stage"], "output_write")
            self.assertEqual(len((output / "records.jsonl").read_text().splitlines()), 1)

    def test_zeroed_pagination_placeholder_is_not_a_successful_end(self) -> None:
        placeholder = {"code": 0, "data": {"page": {"num": 0, "size": 0, "count": 0}, "replies": None}}
        client = self.client(video(), comments(1, [reply()], 100), placeholder)
        code, rows, manifest = self.run_sample("comments", client)
        self.assertEqual(code, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(manifest["error_kind"], "comment_pagination_unavailable")
        self.assertEqual(manifest["failed_page"], 2)
        self.assertEqual(manifest["pagination_diagnostic"], {
            "requested_page": 2, "num": 0, "size": 0, "count": 0, "returned_rows": None})
        self.assertEqual(manifest["partial_object"]["termination_reason"], "failed")
        self.assertEqual(client.requests, 3)

    def test_first_page_exports_nested_replies_and_marks_level(self) -> None:
        main = reply(1)
        main["replies"] = [nested(11, 1)]
        client = self.client(video(), comments(rows=[main], count=1))
        code, rows, manifest = self.run_sample(
            "comments", client, comments_mode="first_page",
        )
        self.assertEqual(code, 0)
        self.assertEqual([row["object_id"] for row in rows], ["1", "11"])
        self.assertEqual([row["platform_data"]["level"] for row in rows], [1, 2])
        self.assertEqual(rows[1]["platform_data"]["parent_id"], "1")
        self.assertEqual(rows[1]["content"], "Synthetic nested comment")
        self.assertNotIn("private-nested-commenter", json.dumps(rows))
        self.assertEqual(manifest["comments_mode"], "first_page")
        self.assertEqual(manifest["limits"]["pages"], 1)
        self.assertEqual(manifest["objects"][0]["nested_reply_count"], 1)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "first_page")
        self.assertEqual(client.requests, 2)

    def test_first_page_ignores_deep_page_budget(self) -> None:
        main = reply(1)
        main["replies"] = []
        client = self.client(video(), comments(rows=[main], count=100))
        code, rows, manifest = self.run_sample(
            "comments", client, comments_mode="first_page", max_pages=5,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(client.requests, 2)
        self.assertEqual(manifest["limits"]["pages"], 1)
        self.assertEqual(len(manifest["objects"][0]["pages"]), 1)

    def test_rate_limit_has_failure_class(self) -> None:
        client = self.client(video(), {"code": -352})
        code, _, manifest = self.run_sample("comments", client, comments_mode="deep")
        self.assertEqual(code, 2)
        self.assertEqual(manifest["failure_class"], "rate_limited")
        self.assertEqual(manifest["api_code"], -352)

    def test_precheck_failure_is_env_blocked(self) -> None:
        client = self.client({"code": -352}, {"code": -352}, {"code": -352})
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            code = content.collect_content(
                [BVID], output, surface="detail", salt="s", client=client, precheck=True,
            )
            manifest = json.loads((output / "bilibili_detail_manifest.json").read_text())
        self.assertEqual(code, 2)
        self.assertEqual(manifest["failure_class"], "env_blocked")
        self.assertEqual(manifest["failure_stage"], "precheck")
        self.assertEqual(manifest["precheck"]["status"], "stop")
        self.assertEqual(client.requests, 0)

    def test_null_replies_with_nonzero_count_is_pagination_unavailable(self) -> None:
        placeholder = {"code": 0, "data": {"page": {"num": 3, "size": 20, "count": 69},
                                            "replies": None}}
        client = self.client(video(), comments(1, [reply()], 100),
                             comments(2, [reply(2)], 100), placeholder)
        code, rows, manifest = self.run_sample(
            "comments", client, comments_mode="deep", max_pages=3,
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(manifest["error_kind"], "comment_pagination_unavailable")
        self.assertEqual(manifest["failed_page"], 3)
        self.assertEqual(manifest["pagination_diagnostic"]["returned_rows"], None)
        self.assertEqual(manifest["pagination_diagnostic"]["count"], 69)

    def test_on_item_error_continue_records_failure_and_next_bv(self) -> None:
        second = video()
        second["data"]["bvid"] = "BV1000000002"
        second["data"]["aid"] = 124
        client = self.client({"code": -352}, second, comments(rows=[reply()], count=1))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            code = content.collect_content(
                [BVID, "BV1000000002"], output, surface="comments", salt="s",
                comments_mode="deep", on_item_error="continue", precheck=False, client=client,
            )
            manifest = json.loads((output / "bilibili_comments_manifest.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(manifest["status"], "complete_with_failures")
        self.assertEqual(manifest["failed_object_count"], 1)
        self.assertEqual(len(manifest["objects"]), 2)
        self.assertEqual(manifest["objects"][0]["failure_class"], "rate_limited")
        self.assertEqual(manifest["objects"][1]["bvid"], "BV1000000002")
        self.assertEqual(manifest["objects"][1]["records"], 1)
        self.assertEqual(manifest["record_count"], 1)

    def test_continue_pagination_failure_records_class_and_diagnostic(self) -> None:
        placeholder = {"code": 0, "data": {"page": {"num": 3, "size": 20, "count": 69},
                                            "replies": None}}
        client = self.client(video(), comments(1, [reply()], 100),
                             comments(2, [reply(2)], 100), placeholder)
        code, rows, manifest = self.run_sample(
            "comments", client, comments_mode="deep", max_pages=3,
            on_item_error="continue",
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(manifest["status"], "complete_with_failures")
        failed = manifest["objects"][0]
        self.assertEqual(failed["failure_class"], "pagination_unavailable")
        self.assertEqual(failed["failed_page"], 3)
        self.assertEqual(failed["pagination_diagnostic"]["count"], 69)


if __name__ == "__main__":
    unittest.main()
