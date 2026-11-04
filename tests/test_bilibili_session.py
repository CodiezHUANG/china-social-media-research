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
from china_media_crawler import bilibili_session as session, http, preflight
from china_media_crawler.browser_session import BrowserSession, BILIBILI, DOUBAN
from china_media_crawler.authentication import get_session_provider
from china_media_crawler.cli import main
from china_media_crawler.platforms import bilibili_content as content, bilibili_search as search

BVID = "BV1000000001"
SECRET = "synthetic-session-secret"


def state():
    return {"cookies": [{"name": "SESSDATA", "value": SECRET, "domain": ".bilibili.com",
                         "path": "/", "expires": -1, "secure": True}], "origins": []}


def video():
    return {"code": 0, "data": {"bvid": BVID, "aid": 123, "title": "Example", "desc": "",
            "owner": {"mid": 1}, "pubdate": 1704067200, "stat": {}, "pages": [{"page": 1, "cid": 456}]}}


class BilibiliSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "bilibili/storage_state.json"
        patcher = patch.object(session, "session_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_private_stores_and_clear_are_platform_isolated(self):
        bili = session.store()
        douban = BrowserSession(DOUBAN, lambda: self.root / "douban/storage_state.json")
        bili.save(state())
        d = {"cookies": [{"name": "dbcl2", "value": "douban-private", "domain": ".douban.com"}], "origins": []}
        douban.save(d)
        self.assertEqual(bili.load(), state())
        self.assertNotIn("douban-private", json.dumps(bili.load()))
        self.assertTrue(bili.clear())
        self.assertEqual(douban.load(), d)

    def test_scope_filter_excludes_other_platforms(self):
        value = state()
        value["cookies"].append({"name": "dbcl2", "value": "douban-private", "domain": ".douban.com"})
        value["origins"] = [{"origin": "https://www.bilibili.com", "localStorage": []},
                            {"origin": "https://movie.douban.com", "localStorage": []}]
        scoped = session.store().scoped_state(value)
        self.assertEqual(len(scoped["cookies"]), 1)
        self.assertEqual(len(scoped["origins"]), 1)

    def test_saved_session_selects_only_bilibili_store(self):
        session.store().save(state())
        source, selected = get_session_provider("bilibili").collection_state("required", "")
        self.assertEqual(source, "saved")
        self.assertEqual(selected, state())

    def test_required_session_missing_and_expired_are_distinct(self):
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
            self.assertEqual(session.collection_session("off", "SESSDATA=" + SECRET), ("anonymous", None))

    def test_explicit_cookie_takes_precedence_without_loading_file(self):
        with patch.object(BrowserSession, "load", side_effect=AssertionError("must not load")):
            source, selected = session.collection_session("auto", "SESSDATA=" + SECRET + "; bili_jct=csrf")
        self.assertEqual(source, "cookie")
        self.assertIn("SESSDATA=" + SECRET, session.cookie_header(selected, "https://api.bilibili.com/x/test"))

    def test_cookie_header_rejects_foreign_hosts_schemes_ports_and_userinfo(self):
        for url in ("https://evil.invalid/x", "http://api.bilibili.com/x", "https://api.bilibili.com:444/x",
                    "https://api.bilibili.com.evil.invalid/x", "https://user@api.bilibili.com/x",
                    "https://api.bilibili.com:bad/x"):
            with self.subTest(url=url), self.assertRaises(http.CollectionError):
                session.cookie_header(state(), url)

    def test_cookie_domain_path_and_expiry_are_respected(self):
        value = state()
        value["cookies"] += [{"name": "host-only", "value": "exclude", "domain": "www.bilibili.com", "path": "/"},
                              {"name": "path-only", "value": "include", "domain": ".bilibili.com", "path": "/x"},
                              {"name": "expired", "value": "exclude", "domain": ".bilibili.com", "expires": 1}]
        header = session.cookie_header(value, "https://api.bilibili.com/x/reply")
        self.assertIn("path-only=include", header)
        self.assertNotIn("exclude", header)
        self.assertNotIn("path-only", session.cookie_header(value, "https://api.bilibili.com/xy/reply"))

    def test_unsafe_cookie_value_is_never_forwarded(self):
        for value in ("token\r\nInjected: value", "token; another=value", "非ASCII"):
            payload = state()
            payload["cookies"][0]["value"] = value
            with self.assertRaises(http.CollectionError) as caught:
                session.cookie_header(payload, "https://api.bilibili.com/x")
            self.assertEqual(str(caught.exception), "cookie_invalid")

    def test_host_only_auth_cookie_cannot_claim_api_authentication(self):
        value = state()
        value["cookies"][0]["domain"] = "www.bilibili.com"
        session.store().save(value)
        with self.assertRaises(http.CollectionError) as caught:
            session.collection_session("required", "")
        self.assertEqual(caught.exception.kind, "session_scope_invalid")

    def test_manual_login_saves_bilibili_state_and_closes_window(self):
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.goto.return_value = SimpleNamespace(status=200)
        page.is_closed.return_value = False
        page.url = BILIBILI.home
        context.cookies.return_value = state()["cookies"]
        context.storage_state.return_value = state()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            session.store().login(15, lambda: driver)
        self.assertEqual(session.store().load(), state())
        self.assertNotIn(SECRET, output.getvalue())
        browser.close.assert_called_once()

    def test_json_transport_sends_credentials_only_to_declared_host(self):
        response = io.BytesIO(b'{"code":0,"data":{}}')
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        factory = Mock(return_value=opener)
        http.fetch_json("https://api.bilibili.com/x", referer="https://www.bilibili.com/",
                        cookie_header="SESSDATA=" + SECRET, credential_host="api.bilibili.com", opener_factory=factory)
        self.assertEqual(opener.open.call_args.args[0].get_header("Cookie"), "SESSDATA=" + SECRET)
        with self.assertRaises(http.CollectionError):
            http.fetch_json("https://evil.invalid/x", referer="https://www.bilibili.com/",
                            cookie_header="SESSDATA=" + SECRET, credential_host="api.bilibili.com", opener_factory=factory)
        self.assertEqual(factory.call_count, 1)

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaises(http.CollectionError) as caught:
            http.RejectRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid/")
        self.assertEqual(caught.exception.kind, "unexpected_redirect")

    def test_authenticated_content_uses_per_video_referer_without_exporting_secret(self):
        session.store().save(state())
        payload = {"code": 0, "data": {"page": {"num": 1, "count": 0, "size": 20}, "replies": None}}
        output = self.root / "run"
        with patch.object(content, "transport_fetch_json", side_effect=[video(), payload]) as fetch:
            client = content.PublicClient(sleep=Mock())
            code = content.collect_content([BVID], output, surface="comments", salt="test",
                                           session_mode="required", client=client, precheck=False)
        self.assertEqual(code, 0)
        for call in fetch.call_args_list:
            self.assertEqual(call.kwargs["referer"], "https://www.bilibili.com/video/" + BVID)
            self.assertIn(SECRET, call.kwargs["cookie_header"])
        manifest = json.loads((output / "bilibili_comments_manifest.json").read_text())
        self.assertTrue(manifest["cookie_used"])
        self.assertEqual(manifest["session_source"], "saved")
        self.assertNotIn(SECRET, json.dumps(manifest))

    def test_authenticated_xml_uses_same_scoped_credentials(self):
        session.store().save(state())
        body = io.BytesIO(b'<i><chatid>456</chatid></i>')
        body.status = 200
        body.headers = {}
        opener = Mock()
        opener.open.return_value = body
        with patch.object(content, "transport_fetch_json", return_value=video()), \
             patch.object(content, "build_opener", return_value=opener):
            code = content.collect_content([BVID], self.root / "xml", surface="danmaku", salt="test",
                                           session_mode="required", client=content.PublicClient(sleep=Mock()),
                                           precheck=False)
        self.assertEqual(code, 0)
        self.assertIn(SECRET, opener.open.call_args.args[0].get_header("Cookie"))

    def test_search_loads_saved_state_without_exporting_secret(self):
        session.store().save(state())
        payload = {"code": 0, "data": {"page": 1, "numPages": 1, "result": [{"bvid": BVID, "title": "Example"}]}}
        output = self.root / "search"
        with patch.object(search, "fetch_json", return_value=payload) as fetch:
            code = search.collect_search("example", output, salt="test", session_mode="required")
        self.assertEqual(code, 0)
        self.assertIn(SECRET, fetch.call_args.kwargs["cookie_header"])
        manifest = json.loads((output / "bilibili_manifest.json").read_text())
        self.assertEqual(manifest["session_source"], "saved")
        self.assertNotIn(SECRET, json.dumps(manifest))

    def test_required_missing_session_stops_before_any_network(self):
        client = content.PublicClient(json_fetch=Mock(), sleep=Mock())
        output = self.root / "missing"
        self.assertEqual(content.collect_content([BVID], output, surface="comments", salt="test",
                                                session_mode="required", client=client), 2)
        client.json_fetch.assert_not_called()
        manifest = json.loads((output / "bilibili_comments_manifest.json").read_text())
        self.assertEqual(manifest["failure_stage"], "session_load")
        self.assertEqual(manifest["error_kind"], "login_required")
        self.assertEqual(manifest["next_action"], "cmc login --platform bilibili")

    def test_preflight_required_session_checks_presence_not_online_validity(self):
        with patch.dict(os.environ, {"BILI_PSEUDONYM_SALT": "synthetic", "BILIBILI_COOKIE": ""}):
            self.assertFalse(preflight.check_platform("bilibili", "comments", session="required")["ready"])
            session.store().save(state())
            item = preflight.check_platform("bilibili", "comments", session="required")
        self.assertTrue(item["ready"])
        self.assertFalse(item["online_validated"])

    def test_common_session_option_and_conflicting_cookie_choice(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["plan", "--platform", "bilibili", "--surface", "comments", "--item-id", BVID,
                                   "--session", "required", "--output", str(self.root / "plan")]), 0)
        plan = json.loads(output.getvalue())
        self.assertIn("required", plan["steps"][0]["command"])
        self.assertNotIn(SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()

