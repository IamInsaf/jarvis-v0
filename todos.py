"""The user's to-do list, kept in JARVIS's own database: add, list, tick off and remove tasks by voice.
Tasks are matched by number ("the second one") or by words ("the website one"), never guessed."""
import os
import re
import sqlite3
import threading
import time

_ORDINALS = {"first": 1, "one": 1, "second": 2, "two": 2, "third": 3, "three": 3, "fourth": 4, "four": 4,
             "fifth": 5, "five": 5, "sixth": 6, "six": 6, "seventh": 7, "seven": 7, "eighth": 8, "eight": 8,
             "ninth": 9, "nine": 9, "tenth": 10, "ten": 10, "last": -1}
# the words around a task that aren't the task: "add to my to-do list: finish the website"
_ADD_FILLER = re.compile(r"^\s*(?:(?:ok(?:ay)?|so|please|jarvis)[,\s]+)*(?:(?:can you |could you |please )?"
                         r"(?:add|put|write|note|create|make)\s+(?:(?:a|an|the|this|these|new|one|two|three|\w+ more)\s+)*"
                         r"(?:tasks?|to-?dos?|items?)?\s*(?:(?:to|on|in|into)\s+(?:my|the|your)\s+(?:to-?do|task)s?"
                         r"(?:\s+list)?)?\s*(?:called|named|saying|that says)?[:,\-]?\s*)", re.I)
_SPLIT = re.compile(r"\s*(?:;|\n|,?\s*\b(?:and also|also|then|second(?: one)?(?: is)?|third(?: one)?(?: is)?|"
                    r"another(?: one)?(?: is)?|next(?: one)?(?: is)?)\b[:,]?)\s*", re.I)
_LEAD = re.compile(r"^(?:(?:the\s+)?first(?: one)?(?: is)?|one|number \w+|\d+[.)]?|to)[:,]?\s+", re.I)


class Todos:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS todos(id INTEGER PRIMARY KEY, text TEXT, done INTEGER DEFAULT 0, "
                        "created REAL, done_at REAL)")
        self.db.commit()
        self.lock = threading.Lock()

    def add(self, tasks) -> str:
        items = split_tasks(tasks)
        if not items:
            return "What's the task?"
        with self.lock:
            for t in items:
                self.db.execute("INSERT INTO todos(text, created) VALUES (?, ?)", (t, time.time()))
            self.db.commit()
        n = len(self._open())
        added = items[0] if len(items) == 1 else f"{len(items)} tasks: " + "; ".join(items)
        return f"Added {added}. You have {n} task{'s' if n != 1 else ''} on your list."

    def list(self) -> str:
        rows = self._open()
        if not rows:
            return "Your to-do list is empty."
        items = "; ".join(f"{i}, {text}" for i, (_, text) in enumerate(rows, 1))
        return f"You have {len(rows)} task{'s' if len(rows) != 1 else ''}: {items}."

    def complete(self, which: str) -> str:
        row = self._find(which)
        if isinstance(row, str):
            return row
        with self.lock:
            self.db.execute("UPDATE todos SET done = 1, done_at = ? WHERE id = ?", (time.time(), row[0]))
            self.db.commit()
        left = len(self._open())
        return f"Ticked off: {row[1]}. {left or 'No'} task{'s' if left != 1 else ''} left."

    def remove(self, which: str) -> str:
        if re.search(r"\b(?:all|everything|whole list)\b", str(which), re.I):
            with self.lock:
                n = self.db.execute("DELETE FROM todos WHERE done = 0").rowcount
                self.db.commit()
            return f"Cleared your to-do list ({n} task{'s' if n != 1 else ''})."
        row = self._find(which)
        if isinstance(row, str):
            return row
        with self.lock:
            self.db.execute("DELETE FROM todos WHERE id = ?", (row[0],))
            self.db.commit()
        return f"Removed: {row[1]}."

    def _open(self) -> list:
        return self.db.execute("SELECT id, text FROM todos WHERE done = 0 ORDER BY id").fetchall()

    def _find(self, which: str):
        """(id, text) of the open task meant by a number or some of its words, else a question to ask."""
        rows = self._open()
        if not rows:
            return "Your to-do list is empty."
        said = str(which or "").lower()
        num = re.search(r"\b(\d+)\b", said)
        # ordinals before plain numbers: "the second one" is 2, not 1 because it says "one"
        words_said = sorted(_ORDINALS, key=lambda w: w in ("one", "two", "three", "four", "five", "six", "seven",
                                                            "eight", "nine", "ten"))
        pos = int(num.group(1)) if num else next((_ORDINALS[w] for w in words_said if re.search(rf"\b{w}\b", said)),
                                                  None)
        if pos is not None:
            if pos == -1:
                return rows[-1]
            if 1 <= pos <= len(rows):
                return rows[pos - 1]
            return f"There's no task number {pos}; you have {len(rows)}."
        words = {w for w in re.findall(r"[a-z0-9]+", said) if len(w) > 2} - {"the", "task", "one", "that", "done",
                                                                               "finished", "complete", "with", "about"}
        scored = sorted(((len(words & set(re.findall(r"[a-z0-9]+", t.lower()))), (i, t)) for i, t in rows),
                        reverse=True)
        if not words or not scored or scored[0][0] == 0:
            return f"Which task? {self.list()}"
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            return f"That matches more than one task. Which number? {self.list()}"
        return scored[0][1]


def split_tasks(tasks) -> list[str]:
    """'add these two: first one finish the website, second one complete my code' -> two tasks."""
    if isinstance(tasks, list):
        parts = [str(t) for t in tasks]
    else:
        text = _ADD_FILLER.sub("", str(tasks or ""), count=1)
        parts = _SPLIT.split(text)
    out = []
    for p in parts:
        p = _LEAD.sub("", p.strip(" .,:;-")).strip(" .,:;-")
        if len(p) > 1:
            out.append(p[0].upper() + p[1:])
    return out
