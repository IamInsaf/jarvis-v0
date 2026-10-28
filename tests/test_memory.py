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


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(str(Path(self.tmp.name) / "jarvis.db"))
        for fact, who in [("The user's dog is called Rex", "insaf"), ("Insaf loves cooking Italian food", "insaf"),
                          ("Sara's favourite colour is green", "sara"), ("The wifi password is on the fridge", None)]:
            self.memory.remember(fact, speaker=who)

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def test_stemmed_words_match(self):
        self.assertEqual(self.memory.search("what are my dogs called"), ["The user's dog is called Rex"])
        self.assertEqual(self.memory.search("do I like to cook"), ["Insaf loves cooking Italian food"])

    def test_speaker_sees_own_and_shared_facts_only(self):
        self.assertEqual(self.memory.search("favourite colour", speaker="insaf"), [])
        self.assertEqual(self.memory.search("favourite colour", speaker="sara"), ["Sara's favourite colour is green"])
        self.assertEqual(self.memory.search("wifi password", speaker="sara"), ["The wifi password is on the fridge"])

    def test_no_match_falls_back_to_recent(self):
        self.assertEqual(self.memory.search("hello there"), [])
        self.assertEqual(len(self.memory.relevant_facts("hello there", k=2)), 2)

    def test_forget_leaves_the_index(self):
        self.memory.forget("Rex")
        self.assertEqual(self.memory.search("dog"), [])
        self.assertIn("Nothing stored", self.memory.recall("dog"))

    def test_old_database_gets_indexed(self):
        path = str(Path(self.tmp.name) / "old.db")
        old = sqlite3.connect(path)  # facts saved before the full-text index existed
        old.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, fact TEXT UNIQUE, created REAL, speaker TEXT)")
        old.execute("INSERT INTO facts(fact, created) VALUES ('The user plays the guitar', 0)")
        old.commit()
        old.close()
        m = Memory(path)
        try:
            self.assertEqual(m.search("guitars"), ["The user plays the guitar"])
        finally:
            m.db.close()
