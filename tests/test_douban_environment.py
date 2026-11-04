from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_media_crawler.cli import build_collection_parser, main
from china_media_crawler.planning import build_plan
from china_media_crawler.platforms.douban_plan import selected_python
from china_media_crawler import preflight


class DoubanEnvironmentTests(unittest.TestCase):
    def test_external_crawler_environment_does_not_affect_routes(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "DOUBAN_PYTHON": ""
        }):
            python, required = selected_python()
            self.assertEqual(python, Path(sys.executable))
            self.assertEqual(required, [])
            args = build_collection_parser().parse_args([
                "--platform", "douban", "--item-id", "123", "--output", str(Path(temporary) / "run")])
            plan = build_plan(args)
            self.assertEqual(plan.steps[0].command[0], sys.executable)
            with patch.object(preflight, "run_probe", side_effect=AssertionError("no browser probe")):
                self.assertTrue(preflight.check_platform("douban")["ready"])
            with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(main(["login", "--platform", "douban", "--status"]), 0)
            self.assertEqual(run.call_args.args[0][0], sys.executable)

    def test_explicit_override_is_respected_by_login_interpreter(self):
        with patch.dict(os.environ, {"DOUBAN_PYTHON": sys.executable}):
            python, required = selected_python()
        self.assertEqual(python, Path(sys.executable).resolve())
        self.assertEqual(required, ["DOUBAN_PYTHON"])

    def test_bad_explicit_override_does_not_block_http_preflight(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "DOUBAN_PYTHON": str(Path(temporary) / "missing-python.exe")
        }):
            with self.assertRaises(ValueError):
                selected_python()
            with patch.object(preflight, "run_probe") as probe:
                self.assertTrue(preflight.check_platform("douban")["ready"])
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
