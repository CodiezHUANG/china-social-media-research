from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from china_media_crawler import preflight


class PreflightTests(unittest.TestCase):
    def test_missing_salt_explains_its_purpose(self) -> None:
        routes = {
            "BILI_PSEUDONYM_SALT": ("bilibili", "search"),
            "MAOYAN_PSEUDONYM_SALT": ("maoyan", "reviews"),
            "WEIBO_PSEUDONYM_SALT": ("weibo", "comments"),
            "DOUYIN_PSEUDONYM_SALT": ("douyin", "search"),
            "XHS_PSEUDONYM_SALT": ("xhs", "search"),
        }
        for name, (platform, surface) in routes.items():
            with self.subTest(name=name), patch.dict("os.environ", {}, clear=True), patch.object(
                    preflight, "playwright_checks", return_value=[]):
                checks = preflight._check_dependencies(platform, surface)["checks"]
                check = next(item for item in checks if item["name"] == name)
                self.assertFalse(check["ok"])
                self.assertIn("pseudonym", check["detail"])
                self.assertIn("never in chat", check["detail"])

    def test_present_salt_reports_only_that_it_is_set(self) -> None:
        with patch.dict("os.environ", {"MAOYAN_PSEUDONYM_SALT": "synthetic-salt"}, clear=True):
            checks = preflight._check_dependencies("maoyan", "reviews")["checks"]
        check = next(item for item in checks if item["name"] == "MAOYAN_PSEUDONYM_SALT")
        self.assertEqual(check["detail"], "set")
        self.assertNotIn("synthetic-salt", str(checks))

    def test_probe_converts_timeout_to_failed_check(self) -> None:
        with patch.object(
            preflight.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("python", 10),
        ):
            ok, detail = preflight.run_probe(["python"], "test")
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_probe_converts_launch_error_to_failed_check(self) -> None:
        with patch.object(preflight.subprocess, "run", side_effect=OSError("blocked")):
            ok, detail = preflight.run_probe(["python"], "test")
        self.assertFalse(ok)
        self.assertIn("could not start", detail)


if __name__ == "__main__":
    unittest.main()
