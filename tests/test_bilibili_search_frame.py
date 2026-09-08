from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    ROOT
    / "skills"
    / "china-social-media-research"
    / "scripts"
    / "bilibili_search_frame.py"
)
SPEC = importlib.util.spec_from_file_location("bilibili_search_frame", SCRIPT_PATH)
assert SPEC and SPEC.loader
frame = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = frame
SPEC.loader.exec_module(frame)


class BilibiliSearchFrameTests(unittest.TestCase):
    def test_normalize_deduplicates_and_caps_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = root / "candidates.txt"
            raw = root / "raw.jsonl"
            candidates.write_text("BV1\nBV1\nBV2\nBV3\n", encoding="utf-8")
            raw.write_text(
                "".join(
                    json.dumps({"bvid": bvid, "captured_at": "2026-01-01T00:00:00+00:00"})
                    + "\n"
                    for bvid in ("BV1", "BV1", "BV2", "BV3")
                ),
                encoding="utf-8",
            )
            count = frame.normalize(candidates, raw, 2)
            self.assertEqual(count, 2)
            self.assertEqual(candidates.read_text(encoding="utf-8"), "BV1\nBV2\n")
            rows = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["bvid"] for row in rows], ["BV1", "BV2"])

    def test_rejects_missing_raw_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = root / "candidates.txt"
            raw = root / "raw.jsonl"
            candidates.write_text("BV1\nBV2\n", encoding="utf-8")
            raw.write_text(json.dumps({"bvid": "BV1"}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                frame.normalize(candidates, raw, 2)


if __name__ == "__main__":
    unittest.main()
