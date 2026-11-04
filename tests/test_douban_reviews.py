from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from china_media_crawler.platforms import douban_reviews as douban


class DoubanValidationTests(unittest.TestCase):
    def test_accepts_normal_title(self) -> None:
        self.assertEqual(
            douban.validate_html("<html><title>Movie (豆瓣)</title></html>"),
            "Movie (豆瓣)",
        )

    def test_rejects_login_or_security_markers(self) -> None:
        for marker in ("sec.douban.com", "accounts.douban.com/passport/login",
                       "登录豆瓣", "请先登录", "异常请求", "验证码"):
            with self.subTest(marker=marker):
                with self.assertRaises(douban.CollectionError) as raised:
                    douban.validate_html(f"<html><title>{marker}</title></html>")
                self.assertEqual(raised.exception.kind, "access_or_login_challenge")

    def test_missing_title_is_unexpected_html(self) -> None:
        with self.assertRaises(douban.CollectionError) as raised:
            douban.validate_html("<html><body>no title</body></html>")
        self.assertEqual(raised.exception.kind, "unexpected_html")

    def test_rejects_unexpected_comment_schema(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "schema"):
            douban.validate_rows(None)


if __name__ == "__main__":
    unittest.main()
