from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    ROOT
    / "skills"
    / "china-social-media-research"
    / "scripts"
    / "preflight.py"
)
SPEC = importlib.util.spec_from_file_location("preflight", SCRIPT_PATH)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
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
