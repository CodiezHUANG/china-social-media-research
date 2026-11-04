from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "china-social-media-research" / "scripts"


def run(script: str, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


class HostEntrypointTests(unittest.TestCase):
    def test_commands_have_host_entrypoints(self) -> None:
        for script, command in (
            ("collect.py", "collect"),
            ("preflight.py", "preflight"),
            ("workflow.py", "workflow"),
            ("login.py", "login"),
            ("platforms.py", "platforms"),
            ("bili_audit.py", "bili-audit"),
            ("bili_supplement.py", "bili-supplement"),
        ):
            with self.subTest(script=script):
                with tempfile.TemporaryDirectory() as temporary:
                    completed = run(script, "--help", cwd=Path(temporary))
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertIn(command, completed.stdout.lower())

    def test_workflow_preview_from_unrelated_directory_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            output = folder / "workflow-preview"
            completed = run(
                "workflow.py",
                "--file",
                str(ROOT / "examples" / "selected-platforms.json"),
                "--output",
                str(output),
                cwd=folder,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                [task["plan"]["platform"] for task in payload["tasks"]],
                ["maoyan", "bilibili"],
            )
            self.assertEqual(payload["on_error"], "stop")
            self.assertFalse(output.exists())

    def test_platform_catalog_filters_selected_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = run(
                "platforms.py", "--platform", "douban", "--json",
                cwd=Path(temporary),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            catalog = json.loads(completed.stdout)
            self.assertEqual({entry["platform"] for entry in catalog}, {"douban"})

    def test_unsupported_login_is_rejected_without_launching_a_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = run(
                "login.py", "--platform", "maoyan", "--status",
                cwd=Path(temporary),
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("not implemented", completed.stderr)


if __name__ == "__main__":
    unittest.main()
