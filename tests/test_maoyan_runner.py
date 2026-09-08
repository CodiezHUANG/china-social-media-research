from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    ROOT
    / "skills"
    / "china-social-media-research"
    / "scripts"
    / "maoyan_runner.py"
)
SPEC = importlib.util.spec_from_file_location("maoyan_runner", SCRIPT_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


FAKE_BACKEND = '''
import argparse
import json
import os
from pathlib import Path

def utc_now():
    return "2026-01-01T00:00:00+00:00"

class TagPoolCollector:
    def __init__(self, output_dir):
        self.checkpoint_path = Path(output_dir) / "checkpoint.json"
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = {"pseudonym_salt": os.environ["MAOYAN_PSEUDONYM_SALT"], "pools": {}}

    def _save_checkpoint(self):
        self.checkpoint_path.write_text(json.dumps(self.state), encoding="utf-8")

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    collector = TagPoolCollector(args.output_dir)
    collector._save_checkpoint()
    return 0
'''


class MaoyanRunnerTests(unittest.TestCase):
    def test_wrapper_never_serializes_pseudonym_salt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tag_pool_runner.py").write_text(FAKE_BACKEND, encoding="utf-8")
            output = root / "output"
            with patch.dict(
                os.environ,
                {"MAOYAN_PSEUDONYM_SALT": "test-only-private-value"},
                clear=False,
            ):
                result = runner.run_backend(root, ["--output-dir", str(output)])
            payload = json.loads((output / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertNotIn("pseudonym_salt", payload)
            self.assertEqual(payload["pools"], {})


if __name__ == "__main__":
    unittest.main()
