"""Memory's route log, on a throwaway database. Run: python -m unittest discover tests"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from memory import Memory  # noqa: E402


class RouteLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "jarvis.db")
        self.memory = Memory(self.path)

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def test_routes_come_back_in_order(self):
        self.memory.log_route("set a timer for 5 minutes", "", "timer")
        self.memory.log_route("make it ten", "set a timer for 5 minutes -> Timer set.", "timer")
        self.memory.log_route("hmm", "", None)  # router unsure
        self.assertEqual(self.memory.routes(), [
            ("set a timer for 5 minutes", "", "timer"),
            ("make it ten", "set a timer for 5 minutes -> Timer set.", "timer"),
            ("hmm", "", None),
        ])

    def test_existing_database_gets_the_table(self):
        self.memory.db.close()
        old = sqlite3.connect(self.path)  # a database from before the routes table existed
        old.execute("DROP TABLE routes")
        old.commit()
        old.close()
        self.memory = Memory(self.path)
        self.memory.log_route("what's the weather", "", "web")
        self.assertEqual(self.memory.routes(), [("what's the weather", "", "web")])


if __name__ == "__main__":
    unittest.main()
