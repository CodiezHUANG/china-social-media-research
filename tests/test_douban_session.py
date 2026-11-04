from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_media_crawler import douban_session as session
from china_media_crawler import browser_session
from china_media_crawler.cli import main


def state():
    return {"cookies": [{"name": "dbcl2", "value": "synthetic-login-secret", "domain": ".douban.com",
                         "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
            "origins": []}


class DoubanSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "auth" / "douban" / "storage_state.json"
        self.patcher = patch.object(session, "session_path", return_value=self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_roundtrip_scopes_credentials_to_douban(self):
        payload = state()
        payload["cookies"].append({"name": "third-party", "value": "other-secret", "domain": "example.com"})
        payload["origins"] = [{"origin": "https://movie.douban.com", "localStorage": []},
                              {"origin": "https://example.com", "localStorage": []}]
        session.save_session(payload)
        loaded = session.load_session()
        self.assertEqual(len(loaded["cookies"]), 1)
        self.assertEqual(len(loaded["origins"]), 1)
        self.assertNotIn("other-secret", self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_missing_auto_session_does_not_create_files(self):
        self.assertEqual(session.collection_session("auto", ""), ("anonymous", None))
        self.assertFalse(self.path.parent.exists())

    def test_off_never_reads_state_or_uses_cookie(self):
        with patch.object(session, "load_session", side_effect=AssertionError("must not read")):
            self.assertEqual(session.collection_session("off", "synthetic-cookie"), ("anonymous", None))

    def test_explicit_cookie_takes_precedence_without_reading_saved_state(self):
        with patch.object(session, "load_session", side_effect=AssertionError("must not read")):
            source, state = session.collection_session(
                "auto", "dbcl2=synthetic-login-secret; bid=synthetic-bid")
        self.assertEqual(source, "cookie")
        self.assertEqual([cookie["name"] for cookie in state["cookies"]], ["dbcl2", "bid"])
        self.assertIn("dbcl2=synthetic-login-secret", session.cookie_header(
            state, "https://movie.douban.com/subject/1/comments"))

    def test_cookie_header_is_scoped_and_rejects_foreign_hosts(self):
        state = {"cookies": [
            {"name": "dbcl2", "value": "secret", "domain": ".douban.com", "path": "/"},
            {"name": "foreign", "value": "other", "domain": ".example.com", "path": "/"},
            {"name": "expired", "value": "old", "domain": ".douban.com", "path": "/", "expires": 1},
        ], "origins": []}
        header = session.cookie_header(state, "https://movie.douban.com/subject/1/comments")
        self.assertIn("dbcl2=secret", header)
        self.assertNotIn("foreign", header)
        self.assertNotIn("expired", header)
        with self.assertRaises(session.CollectionError) as caught:
            session.cookie_header(state, "https://example.com/")
        self.assertEqual(caught.exception.kind, "credential_destination_rejected")

    def test_missing_required_state_is_actionable(self):
        with self.assertRaises(session.CollectionError) as caught:
            session.collection_session("required", "")
        self.assertEqual(caught.exception.kind, "login_required")

    def test_corrupt_state_does_not_fall_back_to_anonymous_or_expose_contents(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('synthetic-secret invalid json', encoding="utf-8")
        with self.assertRaises(session.CollectionError) as caught:
            session.collection_session("auto", "")
        self.assertEqual(str(caught.exception), "session_invalid")

    def test_failed_atomic_save_preserves_previous_session(self):
        session.save_session(state())
        original = self.path.read_bytes()
        with patch.object(browser_session.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                session.save_session(state())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_status_and_clear_have_no_network_or_secrets(self):
        session.save_session(state())
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(session, "login_session", side_effect=AssertionError()):
            self.assertEqual(session.main(["--action", "status"]), 0)
            self.assertEqual(session.main(["--action", "clear"]), 0)
        self.assertIn('"saved": true', output.getvalue())
        self.assertNotIn("synthetic-login-secret", output.getvalue())
        self.assertFalse(self.path.exists())
        self.assertFalse(session.clear_session())

    def test_rejects_state_inside_checkout(self):
        checkout = Path(session.__file__).absolute().parents[2]
        with patch.object(session, "session_path", return_value=checkout / "storage_state.json"):
            with self.assertRaises(session.CollectionError):
                session.load_session()

    def test_auth_cookie_detection_rejects_foreign_empty_and_expired_values(self):
        self.assertTrue(session.auth_cookie_present(state()["cookies"]))
        for override in ({"domain": "notdouban.com"}, {"expires": 1}, {"value": ""}):
            cookie = dict(state()["cookies"][0], **override)
            self.assertFalse(session.auth_cookie_present([cookie]))

    def driver(self):
        driver = MagicMock()
        browser = driver.__enter__.return_value.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.goto.return_value = SimpleNamespace(status=200)
        page.url = "https://movie.douban.com/"
        page.is_closed.return_value = False
        context.cookies.return_value = state()["cookies"]
        context.storage_state.return_value = state()
        return driver, browser, context, page

    def test_manual_login_saves_state_and_closes_browser_without_printing_secret(self):
        driver, browser, context, page = self.driver()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            session.login_session(15, lambda: driver)
        self.assertEqual(session.load_session(), state())
        driver.__enter__.return_value.chromium.launch.assert_called_once_with(headless=False)
        self.assertNotIn("synthetic-login-secret", output.getvalue())
        browser.close.assert_called_once()
        context.close.assert_called_once()

    def test_closed_login_window_preserves_old_state(self):
        session.save_session(state())
        before = self.path.read_bytes()
        driver, browser, context, page = self.driver()
        page.is_closed.return_value = True
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(session.CollectionError) as caught:
            session.login_session(15, lambda: driver)
        self.assertEqual(caught.exception.kind, "login_cancelled")
        self.assertEqual(self.path.read_bytes(), before)
        browser.close.assert_called_once()

    def test_timeout_does_not_save_unverified_state(self):
        driver, browser, context, page = self.driver()
        context.cookies.return_value = []
        with patch.object(session.time, "monotonic", side_effect=[0, 0, 20]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(session.CollectionError) as caught:
                session.login_session(15, lambda: driver)
        self.assertEqual(caught.exception.kind, "login_timeout")
        self.assertFalse(self.path.exists())
        browser.close.assert_called_once()

    def test_security_page_does_not_trigger_automatic_save(self):
        driver, browser, context, page = self.driver()
        page.url = "https://sec.douban.com/c?ticket=synthetic-token"
        with patch.object(session.time, "monotonic", side_effect=[0, 0, 20]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(session.CollectionError):
                session.login_session(15, lambda: driver)
        self.assertFalse(self.path.exists())
        context.storage_state.assert_not_called()

    def test_cli_login_uses_existing_interpreter_and_scrubs_cookie_environment(self):
        with patch.dict(os.environ, {"DOUBAN_PYTHON": sys.executable, "DOUBAN_COOKIE": "synthetic-cookie"}), \
             patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(main(["login", "--platform", "douban", "--status"]), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], str(Path(sys.executable).resolve()))
        self.assertIn("status", args[0])
        self.assertNotIn("DOUBAN_COOKIE", kwargs["env"])
        self.assertNotIn("synthetic-cookie", str(args))

    def test_standalone_entry_does_not_shadow_standard_library_http(self):
        completed = subprocess.run([sys.executable, "-B", session.__file__, "--help"],
                                   capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertIn(b"--action", completed.stdout)


if __name__ == "__main__":
    unittest.main()
