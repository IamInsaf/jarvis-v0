"""The kill switch (orb and HUD) and the one-JARVIS-at-a-time guard behind the desktop icon. Nothing really
exits. Run: python -m unittest discover tests"""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hud  # noqa: E402
import jarvis  # noqa: E402
import orb  # noqa: E402


class KillSwitchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = hud.start(0)

    def post(self, headers=None):
        req = urllib.request.Request(self.url + "/api/shutdown", data=b"{}", method="POST", headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_shuts_down_and_runs_the_exit_handlers(self):
        exited = threading.Event()
        with mock.patch.object(hud.os, "_exit", side_effect=lambda code: exited.set()) as exit_, \
                mock.patch.object(hud.atexit, "_run_exitfuncs") as cleanup:
            self.assertEqual(self.post({"X-Jarvis": "1"}), 200)
            self.assertTrue(exited.wait(3))
        exit_.assert_called_once_with(0)
        cleanup.assert_called_once()  # the orb is closed with it

    def test_needs_the_header_and_a_local_host(self):
        with mock.patch.object(hud, "shutdown") as shutdown:
            self.assertEqual(self.post(), 403)  # no other web page can switch JARVIS off
            self.assertEqual(self.post({"X-Jarvis": "1", "Host": "evil.example"}), 403)
        shutdown.assert_not_called()

    def test_the_page_has_the_button(self):
        with urllib.request.urlopen(self.url + "/") as r:
            page = r.read().decode()
        self.assertIn('id="kill"', page)
        self.assertIn("/api/shutdown", page)

    def test_orb_asks_the_hud(self):
        with mock.patch.object(orb, "_post", return_value=True) as post:
            self.assertTrue(orb.shut_down("http://127.0.0.1:8766"))
        post.assert_called_once_with("http://127.0.0.1:8766/api/shutdown", {})


@unittest.skipUnless(sys.platform == "win32", "the guard is a Windows named mutex")
class OneAtATimeTest(unittest.TestCase):
    def test_second_start_sees_the_first(self):
        name = r"Local\JARVIS-v0-test-guard"
        held = jarvis._instance
        try:
            self.assertFalse(jarvis.already_running(name))  # the first one starts
            self.assertTrue(jarvis.already_running(name))   # a second click on the icon doesn't
        finally:
            jarvis._instance = held


if __name__ == "__main__":
    unittest.main()
