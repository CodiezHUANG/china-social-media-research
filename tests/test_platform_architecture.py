from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_media_crawler import authentication, preflight
from china_media_crawler.cli import main
from china_media_crawler.platforms import capabilities


class PlatformArchitectureTests(unittest.TestCase):
    def test_multi_platform_preflight_checks_only_selected_platforms(self):
        output = io.StringIO()
        with patch.object(preflight, "check_platform", side_effect=lambda platform, *a, **k:
                          {"platform": platform, "ready": True, "checks": []}) as check, \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(["preflight", "--platform", "bilibili",
                                   "--platform", "maoyan", "--platform", "bilibili",
                                   "--json"]), 0)
        self.assertEqual([call.args[0] for call in check.call_args_list], ["bilibili", "maoyan"])
        self.assertEqual(len(json.loads(output.getvalue())), 2)

    def test_ambiguous_multi_platform_surface_and_mixed_all_are_rejected(self):
        for args in (["--platform", "all", "--platform", "douban"],
                     ["--platform", "douban", "--platform", "bilibili", "--surface", "comments"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                preflight.main(args)

    def test_missing_selected_dependency_is_reported_without_hiding_other_result(self):
        output = io.StringIO()

        def check(platform, *a, **k):
            return {"platform": platform, "ready": platform == "maoyan", "checks": []}

        with patch.object(preflight, "check_platform", side_effect=check), \
             contextlib.redirect_stdout(output):
            self.assertEqual(preflight.main(["--platform", "maoyan",
                                             "--platform", "weibo", "--json"]), 1)
        self.assertEqual([r["ready"] for r in json.loads(output.getvalue())], [True, False])

    def test_catalog_is_only(self):
        catalog = capabilities()
        self.assertTrue(catalog)
        self.assertTrue(all("implementation" not in row and row["backend"] for row in catalog))
        self.assertNotIn("available_backends", catalog[0])

    def test_catalog_exposes_session_providers(self):
        index = {(row["platform"], row["surface"]): row for row in capabilities()}
        for platform in ("douban", "bilibili", "weibo", "douyin", "xhs"):
            with self.subTest(platform=platform):
                row = next(value for (name, _), value in index.items() if name == platform)
                self.assertIsNotNone(row["authentication"]["session_provider"])
        self.assertEqual(index["maoyan", "reviews"]["authentication"]["requirement"], "anonymous")

    def test_filtered_catalog_returns_only_requested_platforms(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["platforms", "--platform", "douban",
                                   "--platform", "maoyan", "--json"]), 0)
        self.assertEqual({r["platform"] for r in json.loads(output.getvalue())},
                         {"douban", "maoyan"})

    def test_session_provider_is_and_lazy(self):
        for platform in ("douban", "bilibili", "weibo", "douyin", "xhs"):
            with self.subTest(platform=platform):
                provider = authentication.get_session_provider(platform)
                self.assertEqual(provider.platform, platform)
        with self.assertRaises(ValueError):
            authentication.get_session_provider("maoyan")


if __name__ == "__main__":
    unittest.main()
