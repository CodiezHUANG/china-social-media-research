from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "china-social-media-research" / "scripts"


def run(script: Path, cwd: Path, *, env: dict[str, str] | None = None,
        isolated: bool = True) -> subprocess.CompletedProcess[str]:
    command = [sys.executable]
    if isolated:
        command.append("-S")
    command += [str(script), "--help"]
    process_env = os.environ.copy()
    process_env.pop("PYTHONPATH", None)
    process_env.pop("CMC_ROOT", None)
    if env:
        process_env.update(env)
    return subprocess.run(command, cwd=cwd, env=process_env, text=True,
                          encoding="utf-8", errors="replace",
                          capture_output=True, check=False)


class SkillBootstrapTests(unittest.TestCase):
    def test_full_checkout_script_works_without_site_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run(SCRIPTS / "collect.py", Path(temp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: collect.py", result.stdout)

    def test_skill_only_copy_reports_actionable_install_error(self):
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / "scripts"
            shutil.copytree(SCRIPTS, copied,
                            ignore=shutil.ignore_patterns("__pycache__"))
            result = run(copied / "collect.py", Path(temp))
        self.assertEqual(result.returncode, 1)
        self.assertIn("not self-contained", result.stderr)
        self.assertIn("CMC_ROOT", result.stderr)
        self.assertIn("pip install", result.stderr)

    def test_cmc_root_resolves_core_package(self):
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / "scripts"
            shutil.copytree(SCRIPTS, copied,
                            ignore=shutil.ignore_patterns("__pycache__"))
            result = run(copied / "collect.py", Path(temp),
                         env={"CMC_ROOT": str(ROOT)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: collect.py", result.stdout)

    def test_invalid_cmc_root_fails_before_import(self):
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / "scripts"
            shutil.copytree(SCRIPTS, copied,
                            ignore=shutil.ignore_patterns("__pycache__"))
            result = run(copied / "collect.py", Path(temp),
                         env={"CMC_ROOT": str(Path(temp) / "missing")})
        self.assertEqual(result.returncode, 1)
        self.assertIn("CMC_ROOT must point", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
