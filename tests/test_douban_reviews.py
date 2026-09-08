from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    ROOT
    / "skills"
    / "china-social-media-research"
    / "scripts"
    / "douban_reviews.py"
)
SPEC = importlib.util.spec_from_file_location("douban_reviews", SCRIPT_PATH)
assert SPEC and SPEC.loader
douban = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = douban
SPEC.loader.exec_module(douban)


class FakePage:
    def __init__(self, url: str, html: str = "<html><title>Movie</title></html>") -> None:
        self.url = url
        self._html = html

    def content(self) -> str:
        return self._html

    def title(self) -> str:
        return "Movie"


class DoubanValidationTests(unittest.TestCase):
    def test_rejects_login_redirect_even_with_http_200(self) -> None:
        page = FakePage("https://accounts.douban.com/passport/login")
        with self.assertRaisesRegex(RuntimeError, "redirect"):
            douban.validate_page(page, SimpleNamespace(status=200), "123", comments=False)

    def test_accepts_expected_comments_page(self) -> None:
        page = FakePage("https://movie.douban.com/subject/123/comments?start=0")
        douban.validate_page(page, SimpleNamespace(status=200), "123", comments=True)

    def test_rejects_unexpected_comment_schema(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "schema"):
            douban.validate_rows(None)


if __name__ == "__main__":
    unittest.main()
