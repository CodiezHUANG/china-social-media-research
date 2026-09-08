from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
COLLECT_PATH = ROOT / "skills" / "china-social-media-research" / "scripts" / "collect.py"
SPEC = importlib.util.spec_from_file_location("collect", COLLECT_PATH)
assert SPEC and SPEC.loader
collect = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collect
SPEC.loader.exec_module(collect)


class CollectPlanTests(unittest.TestCase):
    @staticmethod
    def make_python(root: Path) -> Path:
        python = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
            "python.exe" if os.name == "nt" else "python"
        )
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        return python

    def test_douban_plan_uses_bundled_adapter_and_caps(self) -> None:
        parser = collect.build_parser()
        args = parser.parse_args(
            [
                "--platform",
                "douban",
                "--surface",
                "reviews",
                "--item-id",
                "123456",
                "--max-pages",
                "5",
                "--output",
                "run-douban",
            ]
        )
        plan = collect.build_plan(args)
        self.assertEqual(plan.platform, "douban")
        self.assertIn("douban_reviews.py", plan.steps[0].command[1])
        self.assertEqual(plan.limits["pages"], 5)

    def test_mediacrawler_plan_never_contains_cookie_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("", encoding="utf-8")
            self.make_python(root)
            parser = collect.build_parser()
            args = parser.parse_args(
                [
                    "--platform",
                    "xhs",
                    "--query",
                    "example film",
                    "--login",
                    "cookie",
                    "--output",
                    str(root / "output"),
                ]
            )
            with patch.dict(
                os.environ,
                {"MEDIACRAWLER_ROOT": str(root), "MEDIACRAWLER_COOKIES": "secret-cookie-value"},
                clear=False,
            ):
                plan = collect.build_plan(args)
            serialized = str(collect.plan_dict(plan))
            self.assertNotIn("secret-cookie-value", serialized)
            self.assertIn("MEDIACRAWLER_COOKIES", plan.required_environment)

    def test_xhs_detail_requires_full_note_url_with_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.py").write_text("", encoding="utf-8")
            self.make_python(root)
            parser = collect.build_parser()
            bare_id = parser.parse_args(
                [
                    "--platform",
                    "xhs",
                    "--surface",
                    "detail",
                    "--item-id",
                    "66fad51c000000001b0224b8",
                    "--output",
                    str(root / "bad"),
                ]
            )
            with patch.dict(os.environ, {"MEDIACRAWLER_ROOT": str(root)}, clear=False):
                with self.assertRaisesRegex(ValueError, "item-url"):
                    collect.build_plan(bare_id)

                full_url = parser.parse_args(
                    [
                        "--platform",
                        "xhs",
                        "--surface",
                        "detail",
                        "--item-url",
                        (
                            "https://www.xiaohongshu.com/explore/66fad51c000000001b0224b8"
                            "?xsec_token=token&xsec_source=pc_search"
                        ),
                        "--headless",
                        "--output",
                        str(root / "good"),
                    ]
                )
                plan = collect.build_plan(full_url)
            self.assertIn("--specified_id", plan.steps[0].command)
            self.assertEqual(
                plan.steps[0].command[plan.steps[0].command.index("--headless") + 1],
                "yes",
            )

    def test_maoyan_max_pages_maps_to_complete_page_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tag_pool_runner.py").write_text("", encoding="utf-8")
            self.make_python(root)
            parser = collect.build_parser()
            args = parser.parse_args(
                [
                    "--platform",
                    "maoyan",
                    "--item-id",
                    "123456",
                    "--max-pages",
                    "1",
                    "--output",
                    str(root / "output"),
                ]
            )
            with patch.dict(os.environ, {"MAOYAN_CRAWLER_ROOT": str(root)}, clear=False):
                plan = collect.build_plan(args)
            command = plan.steps[0].command
            self.assertEqual(command[command.index("--ceiling") + 1], "15")

    def test_bilibili_search_builds_enough_pages_and_a_cap_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crawl = root / "crawl"
            crawl.mkdir()
            (crawl / "search.py").write_text("", encoding="utf-8")
            (crawl / "main.py").write_text("", encoding="utf-8")
            self.make_python(root)
            parser = collect.build_parser()
            args = parser.parse_args(
                [
                    "--platform",
                    "bilibili",
                    "--surface",
                    "search",
                    "--query",
                    "example film",
                    "--max-items",
                    "21",
                    "--output",
                    str(root / "output"),
                ]
            )
            with patch.dict(os.environ, {"BILIBILI_CRAWLER_ROOT": str(root)}, clear=False):
                plan = collect.build_plan(args)
            self.assertEqual(plan.steps[0].env["BILI_SEARCH_PAGES"], "2")
            self.assertTrue(plan.steps[0].env["BILI_CAPTURE_TIME"])
            self.assertEqual(len(plan.steps), 2)
            self.assertIn("bilibili_search_frame.py", plan.steps[1].command[1])

    def test_rejects_surface_from_another_platform(self) -> None:
        parser = collect.build_parser()
        args = parser.parse_args(
            [
                "--platform",
                "douban",
                "--surface",
                "hot",
                "--item-id",
                "123456",
                "--output",
                "run",
            ]
        )
        with self.assertRaisesRegex(ValueError, "invalid"):
            collect.build_plan(args)


class ExecutionSafetyTests(unittest.TestCase):
    def test_child_environment_does_not_forward_unrelated_secrets(self) -> None:
        plan = collect.Plan(
            created_at=collect.utc_now(),
            platform="douban",
            surface="reviews",
            output="unused",
            limits={},
            required_environment=[],
            steps=[],
        )
        step = collect.Step(
            "test",
            ["test"],
            ".",
            {"EXPECTED": "value"},
            pass_environment=["DOUBAN_COOKIE"],
        )
        with patch.dict(
            os.environ,
            {"DOUBAN_COOKIE": "allowed", "UNRELATED_SECRET": "blocked"},
            clear=False,
        ):
            environment = collect.child_environment(plan, step)
        self.assertEqual(environment["DOUBAN_COOKIE"], "allowed")
        self.assertEqual(environment["EXPECTED"], "value")
        self.assertNotIn("UNRELATED_SECRET", environment)

    def test_visible_browser_requires_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            plan = collect.Plan(
                created_at=collect.utc_now(),
                platform="douban",
                surface="reviews",
                output=str(output),
                limits={},
                required_environment=[],
                steps=[],
                visible_browser=True,
                visible_browser_approved=False,
            )
            with self.assertRaisesRegex(ValueError, "visible browser"):
                collect.execute(plan)
            self.assertFalse(output.exists())

    def test_timeout_is_recorded_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            plan = collect.Plan(
                created_at=collect.utc_now(),
                platform="bilibili",
                surface="search",
                output=str(output),
                limits={},
                required_environment=[],
                steps=[collect.Step("slow", ["fake-command"], str(Path(temporary)))],
                timeout_seconds=30,
            )
            with patch.object(
                collect.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("fake-command", 30),
            ):
                returncode = collect.execute(plan)
            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(returncode, 124)
            self.assertEqual(manifest["status"], "timed_out")
            self.assertEqual(manifest["results"][0]["status"], "timed_out")

    def test_minimal_environment_can_run_python_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            plan = collect.Plan(
                created_at=collect.utc_now(),
                platform="douban",
                surface="reviews",
                output=str(output),
                limits={},
                required_environment=[],
                steps=[
                    collect.Step(
                        "local smoke test",
                        [sys.executable, "-c", "import ssl, tempfile"],
                        str(Path(temporary)),
                    )
                ],
                timeout_seconds=30,
            )
            self.assertEqual(collect.execute(plan), 0)
            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")

    def test_maoyan_checkpoint_salt_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            checkpoint = output / "checkpoint.json"
            checkpoint.write_text(
                json.dumps({"pseudonym_salt": "private", "pools": {}}),
                encoding="utf-8",
            )
            collect.scrub_maoyan_checkpoint(output)
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertNotIn("pseudonym_salt", payload)
            self.assertEqual(payload["pools"], {})


if __name__ == "__main__":
    unittest.main()
