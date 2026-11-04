from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_media_crawler.cli import build_collection_parser
from china_media_crawler.http import CollectionError
from china_media_crawler.planning import build_plan
from china_media_crawler.platforms import douban_reviews as douban


def item(content="synthetic comment", timestamp="2026-10-29 12:00:00",
         votes="3", rating="allstar40", location="上海", author="synthetic-user"):
    return (
        '<div class="comment-item"><div class="comment">'
        '<h3><span class="comment-vote"><span class="votes vote-count">'
        f'{votes}</span></span><span class="comment-info">'
        f'<a href="https://www.douban.com/people/123/"> {author} </a>'
        f'<span class="{rating} rating"></span>'
        f'<a class="comment-time" title="{timestamp}">time</a>'
        f'<span class="comment-location">{location}</span></span></h3>'
        f'<p class="comment-content"><span class="short">{content}</span></p>'
        '</div></div>'
    )


def subject_html(title="Synthetic Movie (豆瓣)"):
    return f"<html><head><title>{title}</title></head><body>subject</body></html>"


def comments_html(*items, container=True):
    body = "".join(items)
    inner = f'<div id="comments">{body}</div>' if container else body
    return f"<html><head><title>Synthetic Movie 短评</title></head><body>{inner}</body></html>"


class DoubanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "run"

    def run_collection(self, *extra, responses=None):
        responses = responses or [(200, subject_html()), (200, comments_html(item()))]
        with patch.object(douban, "fetch_page", side_effect=responses) as fetch, \
             patch.object(douban.time, "sleep"), \
             patch.dict(os.environ, {"DOUBAN_COOKIE": "", "DOUBAN_PSEUDONYM_SALT": "synthetic-salt"}):
            code = douban.main([
                "--subject-id", "123", "--session", "off", "--max-pages", "2",
                "--output", str(self.output), *extra,
            ])
        return code, fetch

    def manifest(self):
        return json.loads((self.output / "douban_manifest.json").read_text(encoding="utf-8"))

    def records(self):
        return [json.loads(line) for line in
                (self.output / "douban_reviews.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_parser_extracts_fields_and_normalizes(self):
        rows, saw = douban.parse_comments(comments_html(item(), item("second")))
        self.assertTrue(saw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["content"], "synthetic comment")
        self.assertEqual(rows[0]["author"].strip(), "synthetic-user")
        self.assertEqual(rows[0]["rating"], 4)
        self.assertEqual(rows[0]["votes"], "3")
        self.assertEqual(rows[0]["ip_location"], "上海")
        self.assertEqual(douban.normalize_timestamp(rows[0]["timestamp"]),
                         "2026-10-29T04:00:00+00:00")
        self.assertEqual(douban.normalize_votes("1,234"), 1234)

    def test_global_cap_and_privacy(self):
        code, fetch = self.run_collection(
            "--max-items", "1",
            responses=[(200, subject_html()), (200, comments_html(item("first"), item("second")))],
        )
        self.assertEqual(code, 0)
        manifest = self.manifest()
        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(manifest["termination_reason"], "max_items")
        self.assertEqual(manifest["transport"], "http")
        self.assertEqual(fetch.call_count, 2)
        record = self.records()[0]
        self.assertEqual(record["published_at"], "2026-10-29T04:00:00+00:00")
        self.assertEqual(record["engagement"], {"votes": 3})
        saved = json.dumps([manifest, record])
        self.assertNotIn("synthetic-user", saved)
        self.assertNotIn("synthetic-salt", saved)

    def test_two_pages_deduplicate_and_keep_page_counts(self):
        code, fetch = self.run_collection(
            responses=[
                (200, subject_html()),
                (200, comments_html(item("first"))),
                (200, comments_html(item("first"), item("second"))),
            ],
        )
        self.assertEqual(code, 0)
        manifest = self.manifest()
        self.assertEqual(manifest["record_count"], 2)
        self.assertEqual(manifest["duplicate_count"], 1)
        self.assertEqual([page["accepted"] for page in manifest["pages"]], [1, 1])
        self.assertEqual(manifest["termination_reason"], "page_budget")
        self.assertIn("start=20", fetch.call_args_list[-1].args[0])

    def test_genuine_empty_page_completes(self):
        code, _ = self.run_collection(
            responses=[(200, subject_html()), (200, comments_html())],
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.manifest()["termination_reason"], "empty_page")
        self.assertEqual(self.records(), [])

    def test_missing_comments_container_is_not_empty_success(self):
        code, _ = self.run_collection(
            responses=[(200, subject_html()), (200, comments_html(container=False))],
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.manifest()["error_kind"], "missing_comments_container")

    def test_security_page_stops_with_login_action(self):
        code, _ = self.run_collection(
            responses=[(200, "<html><title>sec.douban.com</title></html>")])
        self.assertEqual(code, 2)
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error_kind"], "access_or_login_challenge")
        self.assertTrue(manifest["login_action_required"])
        self.assertEqual(manifest["next_action"], "cmc login --platform douban")
        self.assertFalse((self.output / "douban_reviews.jsonl").exists())

    def test_http_error_is_reported_and_stops(self):
        code, _ = self.run_collection(
            responses=[CollectionError("http_error", http_status=403)])
        self.assertEqual(code, 2)
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["http_status"], 403)

    def test_redirect_is_classified_as_access_challenge(self):
        with patch.object(douban, "fetch_html",
                          side_effect=CollectionError("unexpected_redirect", http_status=302)):
            with self.assertRaises(CollectionError) as caught:
                douban.fetch_page("https://movie.douban.com/subject/1/comments", "dbcl2=x")
        self.assertEqual(caught.exception.kind, "access_or_login_challenge")
        self.assertEqual(caught.exception.http_status, 302)

    def test_second_page_failure_retains_first_page(self):
        responses = [
            (200, subject_html()),
            (200, comments_html(item("first"))),
            CollectionError("http_error", http_status=403),
        ]
        code, _ = self.run_collection(responses=responses)
        self.assertEqual(code, 2)
        manifest = self.manifest()
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure_stage"], "comments_page")
        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(len(manifest["pages"]), 1)
        self.assertEqual(len(self.records()), 1)
        self.assertNotIn("termination_reason", manifest)

    def test_plan_is_pure_http(self):
        args = build_collection_parser().parse_args([
            "--platform", "douban", "--surface", "wantsee", "--item-id", "123",
            "--max-items", "3", "--max-pages", "2",
            "--output", str(self.output)])
        with patch.dict(os.environ, {"DOUBAN_COOKIE": "hidden-cookie"}):
            plan = build_plan(args)
        self.assertEqual(plan.backend, "douban-public")
        self.assertFalse(hasattr(plan, "implementation"))
        self.assertNotIn("--headless", plan.steps[0].command)
        self.assertNotIn("hidden-cookie", str(plan))
        self.assertIn("status=F", douban.build_url("123", "wantsee", 2))

    def test_invalid_cap_does_not_create_run_directory(self):
        with self.assertRaises(SystemExit):
            self.run_collection("--max-items", "0")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()


