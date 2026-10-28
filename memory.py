"""Memory: durable facts + a conversation log in local SQLite. Facts are found with SQLite's full-text search
(FTS5, stemmed: "dogs" finds "dog", "cooking" finds "cook") ranked by relevance, not plain word overlap.
Upgrade path: add a vector DB (Qdrant/Chroma) for searching past conversations by meaning."""
import os
import re
import sqlite3
import time

_WORD = re.compile(r"[a-z0-9]+")
# words that say nothing about which fact is meant
_STOP = {"the", "and", "for", "you", "your", "what", "who", "how", "does", "did", "know", "about", "tell", "with",
         "are", "was", "his", "her", "its", "they", "them", "that", "this", "have", "has", "can", "any", "all",
         "remember", "recall", "something", "anything", "jarvis", "please", "there", "where", "when", "which"}


class Memory:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, fact TEXT UNIQUE, created REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY, role TEXT, content TEXT, ts REAL)")
        # every routed request and the category it got: training data for a learned intent classifier.
        # category NULL = the router wasn't sure; previous = the exchange before it, for follow-ups
        self.db.execute("CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY, text TEXT, previous TEXT, "
                        "category TEXT, ts REAL)")
        try:
            self.db.execute("ALTER TABLE facts ADD COLUMN speaker TEXT")  # NULL = general/shared fact
        except sqlite3.OperationalError:
            pass  # already migrated
        self.fts = self._make_index()
        self.db.commit()

    def _make_index(self) -> bool:
        """A full-text index over facts, kept in step by triggers. False if this SQLite has no FTS5."""
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(fact, content='facts', "
                            "content_rowid='id', tokenize='porter unicode61')")
        except sqlite3.OperationalError:
            return False
        self.db.executescript("""
            CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
              INSERT INTO facts_fts(rowid, fact) VALUES (new.id, new.fact); END;
            CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
              INSERT INTO facts_fts(facts_fts, rowid, fact) VALUES ('delete', old.id, old.fact); END;
            CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
              INSERT INTO facts_fts(facts_fts, rowid, fact) VALUES ('delete', old.id, old.fact);
              INSERT INTO facts_fts(rowid, fact) VALUES (new.id, new.fact); END;
        """)
        # counting facts_fts itself would read the facts table; the index's own row count is in _docsize
        indexed = self.db.execute("SELECT count(*) FROM facts_fts_docsize").fetchone()[0]
        if not indexed and self.db.execute("SELECT count(*) FROM facts").fetchone()[0]:
            self.db.execute("INSERT INTO facts_fts(facts_fts) VALUES ('rebuild')")  # facts from before the index
        return True

    def search(self, query: str, speaker: str | None = None, k: int = 8) -> list[str]:
        """Facts matching any meaningful word of `query`, best first ([] if none match).
        `speaker`, when known, limits this to facts about them plus general/shared facts."""
        words = [w for w in _WORD.findall(query.lower()) if len(w) > 2 and w not in _STOP]
        if not words:
            return []
        if not self.fts:
            return self._overlap(set(words), speaker, k)
        match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
        where, args = ("AND (f.speaker = ? OR f.speaker IS NULL)", [speaker]) if speaker else ("", [])
        rows = self.db.execute(f"SELECT f.fact FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                               f"WHERE facts_fts MATCH ? {where} ORDER BY bm25(facts_fts) LIMIT ?",
                               [match, *args, k]).fetchall()
        return [r[0] for r in rows]

    def _overlap(self, q_words: set, speaker: str | None, k: int) -> list[str]:
        """The search without FTS5: rank by shared words."""
        if speaker:
            rows = self.db.execute(
                "SELECT fact FROM facts WHERE speaker = ? OR speaker IS NULL ORDER BY created DESC LIMIT 200",
                (speaker,)).fetchall()
        else:
            rows = self.db.execute("SELECT fact FROM facts ORDER BY created DESC LIMIT 200").fetchall()
        scored = [(len(q_words & set(_WORD.findall(f.lower()))), f) for (f,) in rows]
        return [f for n, f in sorted(scored, reverse=True)[:k] if n]

    def remember(self, fact: str, speaker: str | None = None) -> str:
        self.db.execute("INSERT OR IGNORE INTO facts(fact, created, speaker) VALUES (?, ?, ?)",
                         (fact.strip(), time.time(), speaker))
        self.db.commit()
        return "Saved."

    def forget(self, text: str) -> str:
        cur = self.db.execute("DELETE FROM facts WHERE fact LIKE ?", (f"%{text}%",))
        self.db.commit()
        return f"Forgot {cur.rowcount} fact(s)."

    def recall(self, query: str) -> str:
        found = self.search(query, k=20)
        if not found:  # a word that FTS splits differently ("e-mail"): fall back to a substring match
            words = [w for w in query.lower().split() if len(w) > 2] or [query.lower()]
            sql = "SELECT fact FROM facts WHERE " + " OR ".join("lower(fact) LIKE ?" for _ in words)
            found = [r[0] for r in self.db.execute(sql + " LIMIT 20", [f"%{w}%" for w in words]).fetchall()]
        return "\n".join(found) or "Nothing stored about that."

    def all_facts(self, speaker: str | None = None, limit: int = 50) -> list[str]:
        if speaker:
            rows = self.db.execute(
                "SELECT fact FROM facts WHERE speaker = ? OR speaker IS NULL ORDER BY created DESC LIMIT ?",
                (speaker, limit)).fetchall()
        else:
            rows = self.db.execute("SELECT fact FROM facts ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    def relevant_facts(self, query: str, speaker: str | None = None, k: int = 8) -> list[str]:
        """The facts most relevant to the query, instead of dumping all of them; the most recent ones if none
        match. `speaker`, when known, limits this to facts about them plus general/shared facts."""
        return self.search(query, speaker, k) or self.all_facts(speaker, k)

    def log(self, role: str, content: str):
        self.db.execute("INSERT INTO log(role, content, ts) VALUES (?, ?, ?)", (role, content, time.time()))
        self.db.commit()

    def log_route(self, text: str, previous: str, category: str | None):
        self.db.execute("INSERT INTO routes(text, previous, category, ts) VALUES (?, ?, ?, ?)",
                        (text, previous, category, time.time()))
        self.db.commit()

    def routes(self) -> list[tuple[str, str, str | None]]:
        """(text, previous, category) for every routed request, oldest first."""
        return self.db.execute("SELECT text, previous, category FROM routes ORDER BY id").fetchall()
