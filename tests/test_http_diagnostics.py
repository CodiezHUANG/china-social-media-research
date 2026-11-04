from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_media_crawler import http


class RateLimitDiagnosticsTests(unittest.TestCase):
    def test_http_error_keeps_safe_rate_limit_headers(self):
        headers = {
            "Retry-After": "120",
            "X-RateLimit-Limit": "20",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1700000000",
        }

        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 432, "rate limited", headers, None)

        with self.assertRaises(http.CollectionError) as caught:
            http.fetch_json(
                "https://m.weibo.cn/api/container/getIndex",
                referer="https://m.weibo.cn/",
                opener_factory=lambda *args: Opener(),
            )
        error = caught.exception
        self.assertEqual(error.kind, "http_error")
        self.assertEqual(error.http_status, 432)
        self.assertEqual(error.retry_after, "120")
        self.assertEqual(error.rate_limit_limit, "20")
        self.assertEqual(error.rate_limit_remaining, "0")
        self.assertEqual(error.rate_limit_reset, "1700000000")

    def test_unsafe_rate_limit_header_is_discarded(self):
        metadata = http.rate_limit_metadata({
            "Retry-After": "bad\nvalue",
            "X-RateLimit-Limit": "20",
        })
        self.assertNotIn("retry_after", metadata)
        self.assertEqual(metadata["rate_limit_limit"], "20")


if __name__ == "__main__":
    unittest.main()
