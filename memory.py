"""Memory v0: durable facts + a conversation log in local SQLite.
Upgrade path: add a vector DB (Qdrant/Chroma) for searching past conversations by meaning."""
import os
import sqlite3
import time


class Memory:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, fact TEXT UNIQUE, created REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY, role TEXT, content TEXT, ts REAL)")
        self.db.commit()

    def remember(self, fact: str) -> str:
        self.db.execute("INSERT OR IGNORE INTO facts(fact, created) VALUES (?, ?)", (fact.strip(), time.time()))
        self.db.commit()
        return "Saved."

    def forget(self, text: str) -> str:
        cur = self.db.execute("DELETE FROM facts WHERE fact LIKE ?", (f"%{text}%",))
        self.db.commit()
        return f"Forgot {cur.rowcount} fact(s)."

    def recall(self, query: str) -> str:
        words = [w for w in query.lower().split() if len(w) > 2] or [query.lower()]
        sql = "SELECT fact FROM facts WHERE " + " OR ".join("lower(fact) LIKE ?" for _ in words)
        rows = self.db.execute(sql + " LIMIT 20", [f"%{w}%" for w in words]).fetchall()
        return "\n".join(r[0] for r in rows) or "Nothing stored about that."

    def all_facts(self, limit: int = 50) -> list[str]:
        rows = self.db.execute("SELECT fact FROM facts ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    def log(self, role: str, content: str):
        self.db.execute("INSERT INTO log(role, content, ts) VALUES (?, ?, ?)", (role, content, time.time()))
        self.db.commit()
