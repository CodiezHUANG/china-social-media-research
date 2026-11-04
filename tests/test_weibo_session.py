from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_media_crawler import http, preflight, weibo_session as session
from china_media_crawler.browser_session import BILIBILI, DOUBAN, BrowserSession
from china_media_crawler.cli import main
from china_media_crawler.platforms import weibo

SECRET = "synthetic-weibo-secret"


def state():
    return {"cookies": [
        {"name": "SUB", "value": SECRET, "domain": ".weibo.cn", "path": "/",
         "expires": -1, "secure": True},
        {"name": "SUBP", "value": "second", "domain": ".weibo.cn", "path": "/",
         "expires": -1, "secure": True},
        {"name": "MLOGIN", "value": "1", "domain": ".weibo.cn", "path": "/",
         "expires": -1, "secure": True},
    ], "origins": []}


class WeiboSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "weibo/storage_state.json"
        patcher = patch.object(session, "session_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_private_store_is_isolated_from_other_platforms(self):
        weibo_store = session.store()
        weibo_store.save(state())
        douban = BrowserSession(DOUBAN, lambda: self.root / "douban/storage_state.json")
        bili = BrowserSession(BILIBILI, lambda: self.root / "bilibili/storage_state.json")
        douban.save({"cookies": [{"name": "dbcl2", "value": "douban-private", "domain": ".douban.com"}], "origins": []})
        bili.save({"cookies": [{"name": "SESSDATA", "value": "bili-private", "domain": ".bilibili.com"}], "origins": []})
        self.assertEqual(weibo_store.load(), state())
        self.assertNotIn("douban-private", json.dumps(weibo_store.load()))
        self.assertNotIn("bili-private", json.dumps(weibo_store.load()))

    def test_scope_filter_excludes_other_domains(self):
        value = state()
        value["cookies"].append({"name": "foreign", "value": "x", "domain": ".weibo.com"})
        value["origins"] = [
            {"origin": "https://m.weibo.cn", "localStorage": []},
            {"origin": "https://weibo.com", "localStorage": []},
        ]
        scoped = session.store().scoped_state(value)
        self.assertEqual(len(scoped["cookies"]), 3)
        self.assertEqual(len(scoped["origins"]), 1)

    def test_saved_session_selects_only_weibo_store(self):
        session.store().save(state())
        source, selected = session.collection_session("required", "")
        self.assertEqual(source, "saved")
        self.assertEqual(selected, state())

    def test_required_missing_and_expired_are_distinct(self):
        with self.assertRaises(http.CollectionError) as caught:
            session.collection_session("required", "")
        self.assertEqual(caught.exception.kind, "login_required")
        value = state()
        value["cookies"][0]["expires"] = 1
        session.store().save(value)
        with self.assertRaises(http.CollectionError) as caught:
            session.collection_session("auto", "")
        self.assertEqual(caught.exception.kind, "session_expired")

    def test_off_ignores_saved_state_and_explicit_cookie(self):
        with patch.object(session, "store", side_effect=AssertionError("must not read")):
            self.assertEqual(session.collection_session("off", "SUB=" + SECRET), ("anonymous", None))

    def test_explicit_cookie_takes_precedence_without_loading_file(self):
        with patch.object(BrowserSession, "load", side_effect=AssertionError("must not load")):
            source, selected = session.collection_session("auto", "SUB=" + SECRET + "; SUBP=second; MLOGIN=1")
        self.assertEqual(source, "cookie")
        self.assertIn("SUB=" + SECRET, session.cookie_header(selected, "https://m.weibo.cn/x"))

    def test_cookie_header_rejects_foreign_hosts_schemes_ports_and_userinfo(self):
        for url in ("https://evil.invalid/x", "http://m.weibo.cn/x",
                    "https://m.weibo.cn:444/x", "https://m.weibo.cn.evil.invalid/x",
                    "https://user@m.weibo.cn/x", "https://m.weibo.cn:bad/x"):
            with self.subTest(url=url), self.assertRaises(http.CollectionError):
                session.cookie_header(state(), url)

    def test_cookie_domain_path_and_expiry_are_respected(self):
        value = state()
        value["cookies"] += [
            {"name": "host-only", "value": "include", "domain": "m.weibo.cn", "path": "/"},
            {"name": "path-only", "value": "include", "domain": ".weibo.cn", "path": "/x"},
            {"name": "expired", "value": "exclude", "domain": ".weibo.cn", "expires": 1},
        ]
        header = session.cookie_header(value, "https://m.weibo.cn/x/comments")
        self.assertIn("host-only=include", header)
        self.assertIn("path-only=include", header)
        self.assertNotIn("expired", header)
        self.assertNotIn("path-only", session.cookie_header(value, "https://m.weibo.cn/y/comments"))

    def test_unsafe_cookie_value_is_never_forwarded(self):
        for value in ("token\r\nInjected: value", "token; another=value", "非ASCII"):
            payload = state()
            payload["cookies"][0]["value"] = value
            with self.assertRaises(http.CollectionError) as caught:
                session.cookie_header(payload, "https://m.weibo.cn/x")
            self.assertEqual(str(caught.exception), "cookie_invalid")

    def test_manual_login_saves_state_and_closes_window(self):
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.goto.return_value = SimpleNamespace(status=200)
        page.is_closed.return_value = False
        page.url = "https://m.weibo.cn/"
        context.cookies.return_value = state()["cookies"]
        context.storage_state.return_value = state()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            session.store().login(15, lambda: driver)
        self.assertEqual(session.store().load(), state())
        self.assertNotIn(SECRET, output.getvalue())
        browser.close.assert_called_once()

    def test_cli_login_status_uses_existing_interpreter_and_scrubs_cookie(self):
        with patch.dict(os.environ, {"WEIBO_COOKIE": "synthetic-cookie"}), \
             patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(main(["login", "--platform", "weibo", "--status"]), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], str(Path(sys.executable).resolve()))
        self.assertIn("status", args[0])
        self.assertNotIn("WEIBO_COOKIE", kwargs["env"])
        self.assertNotIn("synthetic-cookie", str(args))

    def test_preflight_required_session_checks_presence_not_online_validity(self):
        with patch.dict(os.environ, {"WEIBO_PSEUDONYM_SALT": "synthetic", "WEIBO_COOKIE": ""}):
            self.assertFalse(preflight.check_platform("weibo", "comments", session="required")["ready"])
            session.store().save(state())
            item = preflight.check_platform("weibo", "comments", session="required")
        self.assertTrue(item["ready"])
        self.assertFalse(item["online_validated"])

    def test_common_session_option_plan_includes_session(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["plan", "--platform", "weibo", "--surface", "comments",
                                   "--item-id", "123",
                                   "--session", "required", "--output", "unused"]), 0)
        plan = json.loads(output.getvalue())
        self.assertIn("required", plan["steps"][0]["command"])
        self.assertNotIn(SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()




