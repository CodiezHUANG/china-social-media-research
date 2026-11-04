from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from china_media_crawler.cli import build_parser
from china_media_crawler.http import CollectionError, RejectRedirect
from china_media_crawler.platforms import get_adapter
from china_media_crawler.platforms import weibo
from china_media_crawler.planning import build_plan
from china_media_crawler.preflight import check_platform


def hot() -> dict:
    return {"ok": 1, "data": {"realtime": [
        {"word": "Synthetic topic", "num": 10, "rank": 1},
        {"word": "Synthetic topic", "num": 10, "rank": 1},
        {"word": "Second topic", "num": 5, "is_ad": 1},
    ]}}


def row(identifier: str = "1") -> dict:
    return {"id": identifier, "text": "A <b>synthetic</b> &amp; comment",
            "created_at": "Thu Oct 29 12:00:00 +0800 2026", "like_count": 2,
            "user": {"id": 98765, "screen_name": "not-for-export"},
            "comments": [{"text": "nested-do-not-export"}]}


def comments(*rows: dict, cursor: int = 0) -> dict:
    return {"ok": 1, "data": {"data": list(rows), "max_id": cursor, "max_id_type": 0}}


def search(*rows: dict, page: int | None = 2) -> dict:
    return {"ok": 1, "data": {
        "cards": [{"mblog": row} for row in rows],
        "cardlistInfo": {"page": page} if page is not None else {},
    }}


def search_group(*rows: dict, page: int | None = 2) -> dict:
    return {"ok": 1, "data": {
        "cards": [{"card_type": 11, "card_group": [{"mblog": row} for row in rows]}],
        "cardlistInfo": {"page": page} if page is not None else {},
    }}


class WeiboTests(unittest.TestCase):
    def collect(self, surface: str, *payloads: dict, **limits) -> tuple:
        client = weibo.PublicClient(Mock(side_effect=payloads), Mock())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            options = dict(limits)
            if surface in {"comments", "search"}:
                options.setdefault("session_mode", "off")
            code = weibo.collect_weibo(
                output, surface=surface, ids=["123"] if surface == "comments" else [],
                queries=["synthetic"] if surface == "search" else [], salt="test-salt",
                client=client, **options,
            )
            records = [json.loads(line) for line in (output / "records.jsonl").read_text().splitlines()]
            manifest = json.loads((output / "weibo_manifest.json").read_text())
        return code, records, manifest, client

    def test_hot_cap_dedup_and_rank_preservation(self) -> None:
        code, rows, manifest, client = self.collect("hot", hot(), max_items=2)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["platform_data"]["rank"], 3)
        self.assertTrue(rows[1]["platform_data"]["is_ad"])
        self.assertIsNone(rows[0]["published_at"])
        self.assertIsNone(rows[0]["author_pseudonym"])
        self.assertEqual(client.requests, 1)
        self.assertNotIn("test-salt", json.dumps(manifest))

    def test_empty_hot_snapshot(self) -> None:
        code, rows, manifest, _ = self.collect("hot", {"ok": 1, "data": {"realtime": []}})
        self.assertEqual(code, 0)
        self.assertFalse(rows)
        self.assertEqual(manifest["termination_reason"], "snapshot_end")

    def test_html_comments_utc_and_privacy(self) -> None:
        code, rows, _, _ = self.collect("comments", comments(row()))
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["content"], "A synthetic & comment")
        self.assertEqual(rows[0]["published_at"], "2026-10-29T04:00:00+00:00")
        for private in ("98765", "not-for-export", "nested-do-not-export"):
            self.assertNotIn(private, json.dumps(rows))

    def test_comment_cap(self) -> None:
        code, rows, manifest, _ = self.collect("comments", comments(row("1"), row("2")),
                                             comments_per_item=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "item_limit")

    def test_cursor_pagination_deduplication(self) -> None:
        code, rows, manifest, client = self.collect(
            "comments", comments(row("1"), cursor=2), comments(row("1"), row("2")),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 2)
        self.assertIn("max_id=2", client.fetch.call_args.args[0])
        self.assertEqual(manifest["objects"][0]["pages"][1]["duplicates"], 1)
        client.sleep.assert_called_once()

    def test_page_budget(self) -> None:
        code, rows, manifest, client = self.collect("comments", comments(row(), cursor=2), max_pages=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(client.requests, 1)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "page_limit")

    def test_repeated_cursor_stops(self) -> None:
        code, rows, manifest, client = self.collect(
            "comments", comments(row("1"), cursor=2), comments(row("2"), cursor=2),
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(manifest["error_kind"], "repeated_cursor")
        self.assertEqual(client.requests, 2)

    def test_non_success_preserves_partial_results(self) -> None:
        code, rows, manifest, client = self.collect("comments", comments(row(), cursor=2), {"ok": -100})
        self.assertEqual(code, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(manifest["api_code"], -100)
        self.assertEqual(manifest["partial_object"]["records"], 1)
        self.assertEqual(client.requests, 2)

    def test_redirect_diagnostic_is_sanitized(self) -> None:
        target = ("https://passport.weibo.com/visitor/genvisitor"
                  "?token=synthetic-secret")
        with self.assertRaises(CollectionError) as caught:
            RejectRedirect().redirect_request(None, None, 302, None, None, target)
        self.assertEqual(caught.exception.redirect_host, "passport.weibo.com")
        self.assertEqual(caught.exception.redirect_path_class, "visitor")
        self.assertNotIn("synthetic-secret", str(caught.exception))

        code, rows, manifest, _ = self.collect("hot", caught.exception)
        self.assertEqual(code, 2)
        self.assertFalse(rows)
        self.assertEqual(manifest["error_kind"], "unexpected_redirect")
        self.assertEqual(manifest["http_status"], 302)
        self.assertEqual(manifest["redirect_host"], "passport.weibo.com")
        self.assertEqual(manifest["redirect_path_class"], "visitor")
        self.assertNotIn("synthetic-secret", json.dumps(manifest))

    def test_genuine_empty_comments(self) -> None:
        code, rows, manifest, _ = self.collect("comments", comments())
        self.assertEqual(code, 0)
        self.assertFalse(rows)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "cursor_end")

    def test_missing_data_is_not_empty_success(self) -> None:
        for payload in ({"ok": 1}, {"ok": 1, "data": {}}, {"ok": True, "data": {}}):
            with self.subTest(payload=payload):
                code, rows, manifest, _ = self.collect("comments", payload)
                self.assertEqual(code, 2)
                self.assertFalse(rows)
                self.assertEqual(manifest["status"], "failed")

    def test_empty_page_with_nonzero_cursor_is_error(self) -> None:
        code, _, manifest, _ = self.collect("comments", comments(cursor=1))
        self.assertEqual(code, 2)
        self.assertEqual(manifest["error_kind"], "unexpected_empty_comment_page")

    def test_malformed_timestamp(self) -> None:
        reply = row()
        reply["created_at"] = "unknown local time"
        code, _, manifest, _ = self.collect("comments", comments(reply))
        self.assertEqual(code, 2)
        self.assertEqual(manifest["error_kind"], "unexpected_timestamp")

    def test_preflight_and_plans_do_not_need_weibo_cli(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(check_platform("weibo", "hot")["ready"])
            self.assertFalse(check_platform("weibo", "comments")["ready"])
            args = build_parser().parse_args([
                "plan", "--platform", "weibo", "--surface", "hot",
                "--output", "unused",
            ])
            plan = build_plan(args)
            self.assertEqual(plan.required_environment, [])
            self.assertEqual(plan.steps[0].pass_environment, [])

    def test_invalid_post_id_is_a_planning_error(self) -> None:
        args = build_parser().parse_args([
            "plan", "--platform", "weibo", "--surface", "comments",
            "--item-id", "not-an-id", "--output", "unused",
        ])
        with self.assertRaises(ValueError):
            build_plan(args)

    def test_all_weibo_surfaces_default_use_platform_collector(self) -> None:
        for surface in ("hot", "comments", "search"):
            with self.subTest(surface=surface):
                self.assertEqual(get_adapter("weibo", surface).backend, 'weibo-public')
    def test_search_records_pagination_and_privacy(self) -> None:
        first = row("1")
        first.update({"reposts_count": 4, "comments_count": 3, "attitudes_count": 9,
                      "retweeted_status": {"id": "ignored"}})
        code, rows, manifest, client = self.collect(
            "search", search(first, page=2), search(row("2"), page=None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["content"], "A synthetic & comment")
        self.assertEqual(rows[0]["published_at"], "2026-10-29T04:00:00+00:00")
        self.assertEqual(rows[0]["platform_data"]["query"], "synthetic")
        self.assertEqual(rows[0]["platform_data"]["page"], 1)
        self.assertEqual(rows[0]["platform_data"]["rank"], 1)
        self.assertTrue(rows[0]["platform_data"]["is_retweeted"])
        self.assertEqual(rows[0]["engagement"], {"reposts": 4, "comments": 3, "likes": 9})
        self.assertIn("page=2", client.fetch.call_args_list[1].args[0])
        self.assertEqual(manifest["objects"][0]["termination_reason"], "page_end")
        self.assertEqual(manifest["objects"][0]["pages"][1]["page"], 2)
        for private in ("98765", "not-for-export", "nested-do-not-export"):
            self.assertNotIn(private, json.dumps(rows))

    def test_search_nested_card_groups_and_empty_page_continuation(self) -> None:
        code, rows, manifest, client = self.collect(
            "search", search(row("1"), page=2), search_group(row("2"), page=3),
            search_group(row("3"), page=None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(manifest["objects"][0]["pages"][1]["accepted"], 1)
        self.assertEqual(manifest["objects"][0]["pages"][2]["accepted"], 1)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "page_end")
        self.assertEqual(client.requests, 3)
        code, rows, manifest, client = self.collect(
            "search", search(row("1"), page=2), search(page=3), search(row("2"), page=None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(manifest["objects"][0]["pages"][1]["returned"], 0)
        self.assertEqual(client.requests, 3)

    def test_search_item_limit_and_page_limit(self) -> None:
        code, rows, manifest, client = self.collect(
            "search", search(row("1"), row("2"), page=2), max_items=1,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "item_limit")
        self.assertEqual(client.requests, 1)
        code, rows, manifest, client = self.collect("search", search(row("1"), page=2), max_pages=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "page_limit")
        self.assertEqual(client.requests, 1)

    def test_search_empty_and_schema_failure(self) -> None:
        code, rows, manifest, _ = self.collect("search", search(page=None))
        self.assertEqual(code, 0)
        self.assertFalse(rows)
        self.assertEqual(manifest["objects"][0]["termination_reason"], "page_end")
        code, rows, manifest, _ = self.collect("search", {"ok": 1, "data": {}})
        self.assertEqual(code, 2)
        self.assertFalse(rows)
        self.assertEqual(manifest["error_kind"], "unexpected_search_schema")

    def test_search_requires_query_and_salt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                weibo.collect_weibo(output, surface="search", queries=["synthetic"], salt="")
            self.assertFalse(output.exists())
            with self.assertRaises(ValueError):
                weibo.collect_weibo(output, surface="search", queries=[], salt="test-salt")
            self.assertFalse(output.exists())

    def test_search_plan_and_visitor_session(self) -> None:
        args = build_parser().parse_args([
            "plan", "--platform", "weibo", "--surface", "search",
            "--query", "synthetic", "--visitor-session", "--output", "unused",
        ])
        plan = build_plan(args)
        self.assertIn("--query", plan.steps[0].command)
        self.assertIn("synthetic", plan.steps[0].command)
        self.assertIn("--visitor-session", plan.steps[0].command)
        self.assertIn("WEIBO_PSEUDONYM_SALT", plan.required_environment)
        self.assertEqual(plan.authentication["visitor_session"], "optional")

    def test_search_preflight_uses_salt_and_visitor_browser(self) -> None:
        with patch.dict(os.environ, {"WEIBO_PSEUDONYM_SALT": "synthetic"}, clear=True):
            item = check_platform("weibo", "search")
        self.assertTrue(item["ready"])
        self.assertEqual(item["backend"], "weibo-public")
        self.assertNotIn("implementation", item)
        with patch.dict(os.environ, {"WEIBO_PSEUDONYM_SALT": "synthetic"}), \
             patch("china_media_crawler.preflight.run_probe", return_value=(True, "installed")) as probe:
            item = check_platform("weibo", "search", visitor_session=True)
        self.assertTrue(item["ready"])
        self.assertTrue(item["visitor_session"])
        self.assertEqual(probe.call_count, 2)
        with self.assertRaises(ValueError):
            check_platform("weibo", "hot", visitor_session=True)

    def test_mobile_requests_use_mobile_user_agent(self) -> None:
        client = weibo.PublicClient(Mock(return_value={"ok": 1}), Mock())
        client.get("https://m.weibo.cn/api/example", mobile=True)
        self.assertEqual(client.fetch.call_args.kwargs["user_agent"], weibo.MOBILE_USER_AGENT)
        client.get("https://weibo.com/ajax/example")
        self.assertNotIn("user_agent", client.fetch.call_args.kwargs)

    def test_search_visitor_cookie_is_scoped_and_not_serialized(self) -> None:
        client = weibo.PublicClient(Mock(side_effect=[search(row("1"), page=None)]), Mock())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(weibo, "visitor_cookie_header", return_value="SUB=visitor"):
                code = weibo.collect_weibo(
                    output, surface="search", queries=["synthetic"], salt="test-salt",
                    client=client, visitor_session=True, session_mode="off",
                )
            manifest = json.loads((output / "weibo_manifest.json").read_text())
        self.assertEqual(code, 0)
        self.assertTrue(manifest["visitor_session"])
        self.assertEqual(manifest["session_source"], "visitor")
        self.assertNotIn("SUB=visitor", json.dumps(manifest))
        kwargs = client.fetch.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "SUB=visitor")
        self.assertEqual(kwargs["credential_host"], "m.weibo.cn")

    def test_inline_emoji_alt_is_not_lost(self) -> None:
        reply = row()
        reply["text"] = 'Example <img alt="[smile]" src="ignored">'
        code, rows, _, _ = self.collect("comments", comments(reply))
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["content"], "Example [smile]")

    def test_missing_salt_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                weibo.collect_weibo(output, surface="comments", ids=["123"])
            self.assertFalse(output.exists())

    def test_completed_posts_remain_when_next_post_fails(self) -> None:
        client = weibo.PublicClient(Mock(side_effect=[comments(row()), {"ok": -100}]), Mock())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            code = weibo.collect_weibo(output, surface="comments", ids=["123", "456"],
                                       salt="test-salt", client=client, session_mode="off")
            manifest = json.loads((output / "weibo_manifest.json").read_text())
            self.assertEqual(code, 2)
            self.assertEqual(len(manifest["objects"]), 1)
            self.assertEqual(manifest["partial_object"]["post_id"], "456")
            self.assertEqual(manifest["record_count"], 1)

    def test_visitor_cookie_header_filters_scope_expiry_and_unsafe_values(self) -> None:
        header = weibo.cookie_header_from([
            {"name": "SUB", "value": "visitor", "domain": ".weibo.cn", "expires": -1},
            {"name": "SUBP", "value": "visitor2", "domain": ".weibo.cn", "expires": -1},
            {"name": "foreign", "value": "excluded", "domain": ".weibo.com", "expires": -1},
            {"name": "expired", "value": "excluded", "domain": ".weibo.cn", "expires": 1},
        ])
        self.assertIn("SUB=visitor", header)
        self.assertIn("SUBP=visitor2", header)
        self.assertNotIn("foreign", header)
        self.assertNotIn("expired", header)
        with self.assertRaises(CollectionError) as caught:
            weibo.cookie_header_from([{"name": "OTHER", "value": "x", "domain": ".weibo.cn"}])
        self.assertEqual(caught.exception.kind, "visitor_session_unavailable")
        with self.assertRaises(CollectionError) as caught:
            weibo.cookie_header_from([
                {"name": "SUB", "value": "bad;value", "domain": ".weibo.cn", "expires": -1},
            ])
        self.assertEqual(caught.exception.kind, "visitor_session_invalid")

    def test_visitor_session_is_only_valid_for_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                weibo.collect_weibo(output, surface="hot", visitor_session=True)
            self.assertFalse(output.exists())

    def test_visitor_cookie_is_scoped_to_mobile_comments_and_not_serialized(self) -> None:
        client = weibo.PublicClient(Mock(side_effect=[comments(row())]), Mock())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(weibo, "visitor_cookie_header", return_value="SUB=visitor"):
                code = weibo.collect_weibo(
                    output, surface="comments", ids=["123"], salt="test-salt",
                    client=client, visitor_session=True, session_mode="off",
                )
            manifest = json.loads((output / "weibo_manifest.json").read_text())
        self.assertEqual(code, 0)
        self.assertTrue(manifest["visitor_session"])
        self.assertEqual(manifest["session_source"], "visitor")
        self.assertFalse(manifest["cookie_used"])
        self.assertNotIn("SUB=visitor", json.dumps(manifest))
        kwargs = client.fetch.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "SUB=visitor")
        self.assertEqual(kwargs["credential_host"], "m.weibo.cn")

    def test_visitor_session_failure_preserves_sanitized_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(weibo, "visitor_cookie_header",
                              side_effect=CollectionError("visitor_session_unavailable")):
                code = weibo.collect_weibo(
                    output, surface="comments", ids=["123"], salt="test-salt",
                    visitor_session=True, session_mode="off",
                )
            manifest = json.loads((output / "weibo_manifest.json").read_text())
            self.assertEqual(code, 2)
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["error_kind"], "visitor_session_unavailable")
            self.assertTrue(manifest["visitor_session"])
            self.assertFalse((output / "records.jsonl").exists())

    def test_visitor_session_plan_and_cli_validation(self) -> None:
        args = build_parser().parse_args([
            "plan", "--platform", "weibo", "--surface", "comments",
            "--item-id", "123", "--visitor-session", "--output", "unused",
        ])
        plan = build_plan(args)
        self.assertIn("--visitor-session", plan.steps[0].command)
        self.assertEqual(plan.authentication["visitor_session"], "optional")
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            from china_media_crawler.cli import main
            main(["plan", "--platform", "maoyan", "--item-id", "123",
                  "--visitor-session", "--output", "unused"])

    def test_preflight_visitor_session_checks_playwright(self) -> None:
        with patch.dict(os.environ, {"WEIBO_PSEUDONYM_SALT": "synthetic"}), \
             patch("china_media_crawler.preflight.run_probe", return_value=(True, "installed")) as probe:
            item = check_platform("weibo", "comments", visitor_session=True)
        self.assertTrue(item["ready"])
        self.assertTrue(item["visitor_session"])
        self.assertEqual(probe.call_count, 2)
        names = [check["name"] for check in item["checks"]]
        self.assertIn("Playwright Python package", names)
        self.assertIn("Playwright Chromium runtime", names)
        with self.assertRaises(ValueError):
            check_platform("weibo", "hot", visitor_session=True)

    def test_saved_session_uses_scoped_cookie_and_skips_visitor(self) -> None:
        provider = Mock()
        provider.collection_state.return_value = ("saved", {"cookies": [], "origins": []})
        client = weibo.PublicClient(Mock(side_effect=[comments(row())]), Mock())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(weibo, "get_session_provider", return_value=provider), \
                 patch.object(weibo.weibo_session, "cookie_header", return_value="SUB=saved"), \
                 patch.object(weibo, "visitor_cookie_header", side_effect=AssertionError("must not use visitor")):
                code = weibo.collect_weibo(
                    output, surface="comments", ids=["123"], salt="test-salt",
                    client=client, session_mode="required", visitor_session=True,
                )
            manifest = json.loads((output / "weibo_manifest.json").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(manifest["session_source"], "saved")
        self.assertTrue(manifest["cookie_used"])
        self.assertFalse(manifest["visitor_session"])
        kwargs = client.fetch.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "SUB=saved")
        self.assertEqual(kwargs["credential_host"], "m.weibo.cn")
        provider.collection_state.assert_called_once_with("required", "")

    def test_required_missing_session_stops_before_requests(self) -> None:
        provider = Mock()
        provider.collection_state.side_effect = CollectionError("login_required")
        client = weibo.PublicClient(Mock(), Mock())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(weibo, "get_session_provider", return_value=provider):
                code = weibo.collect_weibo(
                    output, surface="comments", ids=["123"], salt="test-salt",
                    client=client, session_mode="required",
                )
            manifest = json.loads((output / "weibo_manifest.json").read_text())
        self.assertEqual(code, 2)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error_kind"], "login_required")
        self.assertFalse((output / "records.jsonl").exists())
        client.fetch.assert_not_called()

    def test_session_mode_is_only_valid_for_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                weibo.collect_weibo(output, surface="hot", session_mode="required")
            self.assertFalse(output.exists())

    def test_comment_page_limit_is_fifty_but_hot_stays_five(self) -> None:
        code, _, manifest, _ = self.collect("comments", comments(row()), max_pages=50)
        self.assertEqual(code, 0)
        self.assertEqual(manifest["limits"]["pages"], 50)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                weibo.collect_weibo(output, surface="comments", ids=["123"], salt="test-salt",
                                    max_pages=51, session_mode="off")
            self.assertFalse(output.exists())
            with self.assertRaises(ValueError):
                weibo.collect_weibo(output, surface="hot", max_pages=6)
            self.assertFalse(output.exists())

    def test_comment_export_limit_is_one_thousand(self) -> None:
        code, _, manifest, _ = self.collect("comments", comments(row()), comments_per_item=1000)
        self.assertEqual(code, 0)
        self.assertEqual(manifest["limits"]["comments_per_item"], 1000)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                weibo.collect_weibo(output, surface="comments", ids=["123"], salt="test-salt",
                                    comments_per_item=1001, session_mode="off")
            self.assertFalse(output.exists())
