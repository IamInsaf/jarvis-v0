"""The day at a glance. Run: python -m unittest discover tests"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import briefing  # noqa: E402
import rules  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from todos import Todos  # noqa: E402

NOW = datetime(2026, 10, 28, 7, 30)


class FakeScheduler(Scheduler):
    def __init__(self, items):
        self.items = items

    def pending(self):
        return [(i, *item) for i, item in enumerate(self.items)]


class BriefingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.todos = Todos(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.todos.db.close()
        self.tmp.cleanup()

    def test_empty_day(self):
        out = briefing.compose(FakeScheduler([]), self.todos, NOW)
        self.assertIn("Good morning", out)
        self.assertIn("Wednesday the 28th", out)
        self.assertIn("Nothing is scheduled for today", out)
        self.assertIn("to-do list is clear", out)

    def test_today_only_and_tasks(self):
        items = [("reminder", "call mum", NOW + timedelta(hours=2)), ("alarm", "", NOW + timedelta(days=2))]
        self.todos.add("Finish the website; buy milk")
        out = briefing.compose(FakeScheduler(items), self.todos, NOW)
        self.assertIn("a reminder to call mum", out)
        self.assertNotIn("alarm", out)
        self.assertIn("2 items, starting with finish the website and buy milk", out)

    def test_asks(self):
        for t in ["good morning", "Good morning, Jarvis.", "brief me", "what's my day look like?",
                  "what do I have on today", "give me my briefing"]:
            self.assertEqual((rules.fast_path(t) or [None])[0], "briefing", t)
        for t in ["good morning, what's the weather", "what's the time"]:
            self.assertNotEqual((rules.fast_path(t) or [None])[0], "briefing", t)

    def test_next_time(self):
        self.assertEqual(briefing.next_time((8, 0), NOW), NOW.replace(hour=8, minute=0))
        self.assertEqual(briefing.next_time((7, 0), NOW), NOW.replace(hour=7, minute=0) + timedelta(days=1))
        self.assertIsNone(briefing._parse_time(""))
        self.assertIsNone(briefing._parse_time("25:00"))
        self.assertEqual(briefing._parse_time("7:05"), (7, 5))


if __name__ == "__main__":
    unittest.main()
