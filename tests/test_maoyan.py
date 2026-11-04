from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from china_media_crawler.platforms import maoyan
from china_media_crawler.http import CollectionError
from china_media_crawler.cli import build_parser
from china_media_crawler.cli import prepare_collection_plan
from china_media_crawler.planning import build_plan
from china_media_crawler.preflight import check_platform
from china_media_crawler.platforms import get_adapter


def row(identifier: int = 1) -> dict:
    return {"id": identifier, "movieId": 123, "userId": 98765,
            "nickName": "private-synthetic-name", "content": "Synthetic review",
            "time": "2026-10-29 12:00:00", "cityName": "Example city", "score": 4.5,
            "approve": 3}


def page(*rows: dict, total: int = 100) -> dict:
    return {"cmts": list(rows), "total": total}


class MaoyanTests(unittest.TestCase):
    def collect(self, *payloads: dict, **kwargs) -> tuple:
        client = maoyan.PublicClient(Mock(side_effect=payloads), Mock())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            code = maoyan.collect_reviews("123", output, salt="test-salt", client=client, **kwargs)
            records = [json.loads(line) for line in (output / "records.jsonl").read_text().splitlines()]
            manifest = json.loads((output / "maoyan_manifest.json").read_text())
        return code, records, manifest, client

    def test_privacy_timestamp_and_rating(self) -> None:
        code, records, manifest, _ = self.collect(page(row(), total=1))
        self.assertEqual(code, 0)
        self.assertEqual(records[0]["published_at"], "2026-10-29T04:00:00+00:00")
        self.assertEqual(records[0]["platform_data"]["rating"], 4.5)
        self.assertEqual(records[0]["object_id"], "123:1")
        for secret in ("98765", "private-synthetic-name", "test-salt"):
            self.assertNotIn(secret, json.dumps(records) + json.dumps(manifest))

    def test_exact_global_cap_stops_before_next_pool(self) -> None:
        code, records, manifest, client = self.collect(page(row(1), row(2)), tags="0,1", max_items=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(client.requests, 1)
        self.assertEqual(manifest["termination_reason"], "item_limit")

    def test_cross_pool_duplicates_are_not_double_counted(self) -> None:
        code, records, manifest, _ = self.collect(page(row(1), total=1),
                                                 page(row(1), row(2), total=2), tags="0,1")
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 2)
        self.assertEqual(manifest["duplicate_count"], 1)
        self.assertEqual(len(manifest["pools"]), 2)

    def test_page_offsets_are_zero_and_fifteen(self) -> None:
        code, records, manifest, client = self.collect(page(row(1)), page(row(2)), max_pages=2)
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 2)
        offsets = [parse_qs(urlparse(call.args[0]).query)["offset"][0]
                   for call in client.fetch.call_args_list]
        self.assertEqual(offsets, ["0", "15"])
        self.assertEqual(manifest["pools"][0]["termination_reason"], "page_limit")
        client.sleep.assert_called_once()

    def test_one_page_really_fetches_one_page(self) -> None:
        code, records, _, client = self.collect(page(row()), max_pages=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(client.requests, 1)

    def test_page_budget_is_not_presented_as_complete_view_exhaustion(self) -> None:
        code, _, manifest, _ = self.collect(page(row()), max_pages=1, max_items=100)
        self.assertEqual(code, 0)
        self.assertEqual(manifest["termination_reason"], "page_limit")

    def test_failed_second_page_records_tag_offset_and_partial_counts(self) -> None:
        code, records, manifest, _ = self.collect(page(row()), {"code": -403}, tags="1")
        self.assertEqual(code, 2)
        self.assertEqual(manifest["failure_stage"], "page_validation")
        self.assertEqual(manifest["failed_tag"], "1")
        self.assertEqual(manifest["failed_page"], 2)
        self.assertEqual(manifest["failed_offset"], 15)
        self.assertEqual(manifest["partial_pool"]["accepted"], len(records))
        self.assertEqual(manifest["partial_pool"]["termination_reason"], "failed")

    def test_request_error_stops_before_other_tag(self) -> None:
        client = maoyan.PublicClient(Mock(side_effect=CollectionError("connection_failed")), Mock())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.assertEqual(maoyan.collect_reviews("123", output, tags="0,1", salt="s", client=client), 2)
            manifest = json.loads((output / "maoyan_manifest.json").read_text())
        self.assertEqual(manifest["failure_stage"], "request")
        self.assertEqual(manifest["failed_offset"], 0)
        self.assertEqual(client.requests, 1)

    def test_interrupted_write_keeps_saved_prefix_and_duplicate_count(self) -> None:
        client = maoyan.PublicClient(Mock(return_value=page(row(1), row(1), row(2))), Mock())
        original = maoyan.Record.to_dict
        count = 0
        def fail_second(record):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("synthetic interruption")
            return original(record)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(maoyan.Record, "to_dict", fail_second), self.assertRaises(OSError):
                maoyan.collect_reviews("123", output, salt="s", client=client)
            manifest = json.loads((output / "maoyan_manifest.json").read_text())
            self.assertEqual(len((output / "records.jsonl").read_text().splitlines()), 1)
        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(manifest["duplicate_count"], 1)
        self.assertEqual(manifest["partial_pool"]["accepted"], 1)
        self.assertEqual(manifest["failure_stage"], "output_write")

    def test_boolean_budgets_fail_before_any_output_or_request(self) -> None:
        for limits in ({"max_items": True}, {"max_pages": True}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "run"
                client = maoyan.PublicClient(Mock(), Mock())
                with self.assertRaises(ValueError):
                    maoyan.collect_reviews("123", output, salt="s", client=client, **limits)
                self.assertFalse(output.exists())
                self.assertEqual(client.requests, 0)

    def test_all_view_end_is_distinct_from_budget_stop(self) -> None:
        code, _, manifest, _ = self.collect(page(row(1), total=1), page(row(2), total=1), tags="0,1")
        self.assertEqual(code, 0)
        self.assertEqual(manifest["termination_reason"], "selected_views_end")

    def test_partial_output_survives_api_error(self) -> None:
        code, records, manifest, client = self.collect(page(row()), {"code": -403})
        self.assertEqual(code, 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(manifest["api_code"], -403)
        self.assertEqual(len(manifest["partial_pool"]["pages"]), 1)
        self.assertEqual(client.requests, 2)

    def test_missing_cmts_is_not_empty_success(self) -> None:
        code, _, manifest, _ = self.collect({"total": 0})
        self.assertEqual(code, 2)
        self.assertEqual(manifest["error_kind"], "unexpected_review_schema")

    def test_empty_list_is_a_valid_end(self) -> None:
        code, records, manifest, client = self.collect(page())
        self.assertEqual(code, 0)
        self.assertFalse(records)
        self.assertEqual(manifest["pools"][0]["termination_reason"], "empty_page")
        self.assertEqual(client.requests, 1)

    def test_wrong_movie_and_invalid_rating_fail(self) -> None:
        for item in ({**row(), "movieId": 456}, {**row(), "score": float("nan")},
                     {**row(), "score": 6}, {**row(), "score": True}):
            with self.subTest(item=item):
                code, records, manifest, _ = self.collect(page(item))
                self.assertEqual(code, 2)
                self.assertFalse(records)
                self.assertEqual(manifest["status"], "failed")

    def test_composite_fingerprint_is_cross_pool_stable(self) -> None:
        item = row()
        del item["id"]
        code, records, manifest, _ = self.collect(page(item, total=1), page(item, total=1), tags="0,1")
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(manifest["duplicate_count"], 1)
        self.assertEqual(records[0]["platform_data"]["identifier_source"], "composite_fingerprint")

    def test_nickname_fallback_does_not_merge_distinct_authors(self) -> None:
        first, second = row(), row()
        for item in (first, second):
            del item["id"]
            del item["userId"]
        second["nickName"] = "another-synthetic-name"
        code, records, _, _ = self.collect(page(first, second, total=2))
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["author_pseudonym"], records[1]["author_pseudonym"])

    def test_tags_are_deduplicated_and_bounded(self) -> None:
        self.assertEqual(maoyan.parse_tags("0,1,0"), ["0", "1"])
        for value in ("", "0,,1", "-1", ",".join(str(i) for i in range(11))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                maoyan.parse_tags(value)

    def test_bad_time_does_not_get_guessed(self) -> None:
        code, _, manifest, _ = self.collect(page({**row(), "time": "10-29"}))
        self.assertEqual(code, 2)
        self.assertEqual(manifest["error_kind"], "unexpected_timestamp")

    def test_missing_salt_leaves_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                maoyan.collect_reviews("123", output, salt="")
            self.assertFalse(output.exists())

    def test_plan_and_preflight_need_no_old_repo(self) -> None:
        args = build_parser().parse_args([
            "plan", "--platform", "maoyan", "--item-id", "123",
            "--tags", "0,1", "--max-items", "7", "--output", "unused",
        ])
        with patch.dict(os.environ, {"MAOYAN_PSEUDONYM_SALT": "test-value"}, clear=True):
            plan = build_plan(args)
            check = check_platform("maoyan", "reviews")
        self.assertFalse(hasattr(plan, "implementation"))
        self.assertEqual(plan.required_environment, ["MAOYAN_PSEUDONYM_SALT"])
        self.assertEqual(plan.limits["reviews"], 7)
        self.assertTrue(check["ready"])
        self.assertNotIn("test-value", str(plan) + str(check))

    def test_plan_records_purpose_and_tag_note(self) -> None:
        args = build_parser().parse_args([
            "plan", "--platform", "maoyan", "--item-id", "123", "--output", "unused",
            "--purpose", "  Synthetic reception pilot  ",
        ])
        plan = prepare_collection_plan(args)
        self.assertEqual(plan.purpose, "Synthetic reception pilot")
        self.assertEqual(plan.notes, [maoyan.TAG_NOTE])
        self.assertIn("assigns none", maoyan.TAG_NOTE)

    def test_plan_without_purpose_records_none(self) -> None:
        args = build_parser().parse_args([
            "plan", "--platform", "maoyan", "--item-id", "123", "--output", "unused",
        ])
        self.assertIsNone(prepare_collection_plan(args).purpose)

    def test_purpose_must_be_one_bounded_line(self) -> None:
        for value in ("", "   ", "first\nsecond", "x" * 501):
            args = build_parser().parse_args([
                "plan", "--platform", "maoyan", "--item-id", "123", "--output", "unused",
                "--purpose", value,
            ])
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                prepare_collection_plan(args)

    def test_auto_is(self) -> None:
        self.assertEqual(get_adapter("maoyan", "reviews").backend, 'maoyan-public')
    def test_second_run_cannot_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            maoyan.collect_reviews("123", output, salt="s", client=maoyan.PublicClient(
                Mock(return_value=page(total=0)), Mock(),
            ))
            original = (output / "maoyan_manifest.json").read_bytes()
            with self.assertRaises(ValueError):
                maoyan.collect_reviews("123", output, salt="s")
            self.assertEqual(original, (output / "maoyan_manifest.json").read_bytes())

    def test_explicit_start_time_is_preferred(self) -> None:
        item = row()
        item["time"] = "10-29"
        item["startTime"] = "2026-10-29 12:00:00"
        code, records, _, _ = self.collect(page(item, total=1))
        self.assertEqual(code, 0)
        self.assertEqual(records[0]["published_at"], "2026-10-29T04:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
