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


if __name__ == "__main__":
    unittest.main()
