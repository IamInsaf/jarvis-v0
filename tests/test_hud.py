"""The HUD's event bus, snapshot and server, on a throwaway database. Run: python -m unittest discover tests"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hud  # noqa: E402


class BusTest(unittest.TestCase):
    def test_events_come_back_in_order_after_the_last_seen(self):
        start = hud._events[-1]["id"] if hud._events else 0
        hud.node("router", "tasks")
        hud.emit("reply", text="Added milk.")
        got = hud.since(start, 0)
        self.assertEqual([(e["kind"], e.get("node"), e.get("text")) for e in got],
                         [("node", "router", None), ("reply", None, "Added milk.")])
        self.assertEqual(hud.since(got[-1]["id"], 0), [])

    def test_state_only_emits_on_change(self):
        hud.state("idle")
        start = hud._events[-1]["id"]
        hud.state("thinking")
        hud.state("thinking")
        self.assertEqual([e["state"] for e in hud.since(start, 0)], ["thinking"])
        self.assertEqual(hud.current(), "thinking")
        hud.state("idle")

    def test_levels_are_clamped_and_go_quiet(self):
        hud.level("mic", 3.0)
        self.assertEqual(hud._levels_now()["mic"], 1.0)
        hud._levels["mic"] = (0.5, time.time() - 1)
        self.assertEqual(hud._levels_now()["mic"], 0.0)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "jarvis.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_database_is_empty_not_an_error(self):
        snap = hud.snapshot(self.path)
        self.assertEqual((snap["requests"], snap["todos"], snap["schedule"]), (0, [], []))

    def test_counts_tasks_timers_and_routes(self):
        now = time.time()
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE facts(id INTEGER PRIMARY KEY, fact TEXT, created REAL, speaker TEXT);
            CREATE TABLE log(id INTEGER PRIMARY KEY, role TEXT, content TEXT, ts REAL);
            CREATE TABLE routes(id INTEGER PRIMARY KEY, text TEXT, previous TEXT, category TEXT, ts REAL);
            CREATE TABLE todos(id INTEGER PRIMARY KEY, text TEXT, done INTEGER DEFAULT 0, created REAL, done_at REAL);
            CREATE TABLE schedule(id INTEGER PRIMARY KEY, kind TEXT, label TEXT, due REAL, created REAL,
                                  done INTEGER DEFAULT 0);""")
        db.execute("INSERT INTO facts(fact, created) VALUES ('likes tea', ?)", (now,))
        db.executemany("INSERT INTO log(role, content, ts) VALUES (?, ?, ?)", [
            ("user", "old one", now - 7200), ("assistant", "ok", now - 7199),
            ("user", "add milk", now - 5), ("assistant", "Added milk.", now - 4)])
        db.executemany("INSERT INTO routes(text, category, ts) VALUES (?, ?, ?)",
                       [("a", "tasks", now), ("b", "tasks", now), ("c", None, now)])
        db.executemany("INSERT INTO todos(text, done, created, done_at) VALUES (?, ?, ?, ?)",
                       [("milk", 0, now, None), ("bread", 1, now, now)])
        db.executemany("INSERT INTO schedule(kind, label, due, created, done) VALUES (?, ?, ?, ?, ?)",
                       [("timer", "tea", now + 120, now, 0), ("alarm", "", now - 10, now - 100, 0),
                        ("timer", "gone", now + 50, now, 1)])
        db.commit()
        db.close()
        snap = hud.snapshot(self.path)
        self.assertEqual((snap["facts"], snap["requests"], snap["sessions"]), (1, 2, 2))
        self.assertEqual(snap["routes"], [{"category": "tasks", "n": 2}, {"category": None, "n": 1}])
        self.assertEqual([t["text"] for t in snap["todos"]], ["milk"])
        self.assertEqual((snap["done"], snap["done_today"]), (1, 1))
        self.assertEqual([s["label"] for s in snap["schedule"]], ["tea"])  # past and finished ones left out
        self.assertEqual(snap["transcript"][-1], {"role": "assistant", "text": "Added milk.", "ts": now - 4})


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = hud.start(0)

    def test_page_and_snapshot(self):
        with urllib.request.urlopen(self.url + "/") as r:
            self.assertIn(b"J.A.R.V.I.S", r.read())
        with urllib.request.urlopen(self.url + "/api/snapshot") as r:
            self.assertIn("system", json.loads(r.read()))

    def test_other_hosts_are_refused(self):
        req = urllib.request.Request(self.url + "/api/snapshot", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)

    def post(self, body, headers=None):
        req = urllib.request.Request(self.url + "/api/answer", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_clicked_answer_reaches_the_question(self):
        qid = hud.ask("Shall I run this command: Start-Process calc?")
        self.assertIn({"id": qid, "question": "Shall I run this command: Start-Process calc?"},
                      hud.snapshot("missing.db")["asks"])
        self.assertEqual(self.post({"id": qid, "yes": True}, {"X-Jarvis": "1"}), 200)
        self.assertTrue(hud.pending_answer(qid))
        self.assertEqual(self.post({"id": qid, "yes": False}, {"X-Jarvis": "1"}), 409)  # already answered
        hud.close_ask(qid, "click", True)
        self.assertIsNone(hud.pending_answer(qid))
        self.assertEqual(self.post({"id": qid, "yes": True}, {"X-Jarvis": "1"}), 409)  # closed

    def test_answers_need_the_header_and_a_local_host(self):
        qid = hud.ask("Delete it?")
        self.addCleanup(hud.close_ask, qid, "timeout", None)
        self.assertEqual(self.post({"id": qid, "yes": True}), 403)  # a cross-site form can't add the header
        self.assertEqual(self.post({"id": qid, "yes": True}, {"X-Jarvis": "1", "Host": "evil.example"}), 403)
        self.assertEqual(self.post({"id": qid, "yes": "yes"}, {"X-Jarvis": "1"}), 400)
        self.assertEqual(self.post({"nope": 1}, {"X-Jarvis": "1"}), 400)
        self.assertIsNone(hud.pending_answer(qid))

    def post_typed(self, body, headers=None):
        req = urllib.request.Request(self.url + "/api/type", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_typed_request_reaches_the_voice_loop(self):
        while hud.take_typed():  # nothing left over from another test
            pass
        self.assertEqual(self.post_typed({"text": "  what's   the time  "}, {"X-Jarvis": "1"}), 200)
        self.assertTrue(hud.has_typed())
        self.assertEqual(hud.take_typed(), "what's the time")
        self.assertIsNone(hud.take_typed())
        self.assertFalse(hud.has_typed())

    def test_typed_requests_need_the_header_and_text(self):
        self.assertEqual(self.post_typed({"text": "delete everything"}), 403)  # no other page can give orders
        self.assertEqual(self.post_typed({"text": "hi"}, {"X-Jarvis": "1", "Host": "evil.example"}), 403)
        self.assertEqual(self.post_typed({"text": "   "}, {"X-Jarvis": "1"}), 400)
        self.assertEqual(self.post_typed({"text": 5}, {"X-Jarvis": "1"}), 400)
        self.assertEqual(self.post_typed({}, {"X-Jarvis": "1"}), 400)
        self.assertFalse(hud.has_typed())

    def test_long_typed_request_is_cut(self):
        hud.type_request("a" * (hud.TYPED_MAX + 50))
        self.assertEqual(len(hud.take_typed()), hud.TYPED_MAX)


    def get_camera(self, headers=None, query="?w=200"):
        req = urllib.request.Request(self.url + "/api/camera.png" + query, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_camera_frames(self):
        self.addCleanup(hud.set_camera, None)
        hud.set_camera(None)
        self.assertEqual(self.get_camera({"X-Jarvis": "1"})[0], 204)  # no camera
        widths = []
        hud.set_camera(lambda w: widths.append(w) or b"\x89PNG fake")
        self.assertEqual(self.get_camera({"X-Jarvis": "1"}), (200, b"\x89PNG fake"))
        self.assertEqual(widths, [200])
        self.assertEqual(self.get_camera(), (403, b""))  # another page can't show your camera
        self.assertEqual(self.get_camera({"X-Jarvis": "1", "Host": "evil.example"})[0], 403)
        self.assertEqual(self.get_camera({"X-Jarvis": "1"}, "?w=wide")[0], 400)

    def test_pov_event(self):
        start = hud._events[-1]["id"] if hud._events else 0
        hud.pov(True)
        self.assertEqual([(e["kind"], e["on"]) for e in hud.since(start, 0)], [("pov", True)])


class AskTest(unittest.TestCase):
    def test_ask_answer_close_events(self):
        start = hud._events[-1]["id"] if hud._events else 0
        qid = hud.ask("Send it?")
        self.assertIsNone(hud.pending_answer(qid))
        self.assertTrue(hud.answer(qid, False))
        self.assertFalse(hud.answer(qid, True))  # first answer wins
        self.assertIs(hud.pending_answer(qid), False)
        hud.close_ask(qid, "click", False)
        kinds = [(e["kind"], e.get("ask")) for e in hud.since(start, 0)]
        self.assertEqual(kinds, [("ask", qid), ("answered", qid)])
        self.assertFalse(hud.answer(999999, True))


if __name__ == "__main__":
    unittest.main()
