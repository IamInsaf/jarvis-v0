"""Timers, alarms and reminders that survive a restart: kept in SQLite, announced by a background
thread. Anything that came due while JARVIS was off is mentioned once at the next start-up."""
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

import config
from rules import find_clock, spoken_clock, spoken_duration

MISSED_WINDOW = 24 * 3600  # older missed items are dropped silently instead of announced
_KINDS = {"timer": "timer", "timers": "timer", "dimer": "timer", "dimers": "timer", "countdown": "timer",
          "alarm": "alarm", "alarms": "alarm", "reminder": "reminder", "reminders": "reminder"}
_WORD = re.compile(r"[a-z0-9']+")
# words in a cancel request that say nothing about which item is meant
_FILLER = {"cancel", "stop", "delete", "remove", "clear", "turn", "off", "switch", "get", "rid", "the", "my",
           "all", "every", "everything", "please", "jarvis", "and", "that", "this", "one", "for", "at", "to",
           "can", "you", "could", "set", "am", "pm", "of", *_KINDS}


class Scheduler:
    def __init__(self, path: str, speak):
        self.speak = speak
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()  # the background thread and the main loop share this connection
        self.db.execute("CREATE TABLE IF NOT EXISTS schedule(id INTEGER PRIMARY KEY, kind TEXT, label TEXT, "
                        "due REAL, created REAL, done INTEGER DEFAULT 0)")
        self.db.commit()
        self._wake = threading.Event()
        self._missed = self._take_due()
        threading.Thread(target=self._loop, daemon=True).start()

    def add(self, kind: str, label: str, due: datetime) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO schedule(kind, label, due, created) VALUES (?, ?, ?, ?)",
                                  (kind, label.strip(), due.timestamp(), time.time()))
            self.db.commit()
        self._wake.set()  # recompute how long to sleep
        return cur.lastrowid

    def pending(self) -> list[tuple]:
        """(id, kind, label, due datetime) for everything still to come, soonest first."""
        with self.lock:
            rows = self.db.execute("SELECT id, kind, label, due FROM schedule WHERE done = 0 ORDER BY due").fetchall()
        return [(i, k, label, datetime.fromtimestamp(d)) for i, k, label, d in rows]

    def describe(self, kind: str, label: str, due: datetime, now: datetime | None = None) -> str:
        """"a tea timer with 4 minutes left", "an alarm at 7:00 AM tomorrow", "a reminder to call mom at 5:00 PM"."""
        now = now or datetime.now()
        left = (due - now).total_seconds()
        when = f"in {spoken_duration(left)}" if left < 3600 else f"at {spoken_clock(due, now)}"
        if kind == "timer":
            return f"a {label + ' ' if label else ''}timer with {spoken_duration(max(left, 0))} left"
        if kind == "alarm":
            return f"an alarm at {spoken_clock(due, now)}" + (f" for {label}" if label else "")
        return f"a reminder {_about(label)} {when}"

    def summary(self) -> str:
        items = self.pending()
        if not items:
            return "You have no timers, alarms or reminders set."
        parts = [self.describe(k, label, due) for _, k, label, due in items]
        listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"You have {listed}." if len(parts) == 1 else f"You have {len(parts)} things set: {listed}."

    def cancel(self, which: str) -> str:
        """Cancel by what the user said: "all alarms", "the tea timer", "my 7 am alarm", "the timer"."""
        items = self.pending()
        words = set(_WORD.findall(which.lower()))
        kinds = {_KINDS[w] for w in words if w in _KINDS}
        if kinds:
            items = [i for i in items if i[1] in kinds]
        if not (words & {"all", "every", "everything"}):
            label_words = words - _FILLER
            by_label = [i for i in items if label_words & set(_WORD.findall(i[2].lower()))]
            clock = find_clock(which)
            by_time = [i for i in items if clock and abs((i[3] - clock[0]).total_seconds()) < 60]
            items = by_label or by_time or (items if not label_words else [])
        what = " or ".join(f"{k}s" for k in sorted(kinds)) if kinds else "timers, alarms or reminders"
        if not items:
            return "There's nothing like that to cancel." if words - _FILLER else f"You have no {what} set."
        if len(items) > 1 and not (words & {"all", "every", "everything"}):
            listed = " or ".join(self.describe(k, label, due) for _, k, label, due in items)
            return f"You have {len(items)} of those: {listed}. Which one?"
        with self.lock:
            self.db.executemany("UPDATE schedule SET done = 1 WHERE id = ?", [(i[0],) for i in items])
            self.db.commit()
        self._wake.set()
        if len(items) == 1:
            _, k, label, due = items[0]
            return f"Cancelled {self.describe(k, label, due)}."
        return f"Cancelled {len(items)} {what}."

    def _take_due(self) -> list[tuple]:
        """Mark everything due now as done and return it as (kind, label, due datetime)."""
        with self.lock:
            rows = self.db.execute("SELECT id, kind, label, due FROM schedule WHERE done = 0 AND due <= ?",
                                   (time.time(),)).fetchall()
            self.db.executemany("UPDATE schedule SET done = 1 WHERE id = ?", [(r[0],) for r in rows])
            self.db.commit()
        return [(k, label, datetime.fromtimestamp(d)) for _, k, label, d in rows]

    def _loop(self):
        missed = [m for m in self._missed if time.time() - m[2].timestamp() < MISSED_WINDOW]
        if missed:
            listed = "; ".join(_missed(k, label, due) for k, label, due in missed)
            self.speak(f"While I was offline, {config.USER_TITLE}, I missed {listed}.")
        while True:
            for kind, label, due in self._take_due():
                self.speak(_announce(kind, label, due))
            with self.lock:
                nxt = self.db.execute("SELECT MIN(due) FROM schedule WHERE done = 0").fetchone()[0]
            # sleep until the next item (capped, so a changed system clock is noticed), or until add/cancel
            self._wake.wait(30 if nxt is None else min(max(nxt - time.time(), 0.05), 30))
            self._wake.clear()


def _about(label: str) -> str:
    """"to call mom" / "about the meeting" stay as they are; a bare "call mom" gets "to"."""
    return label if re.match(r"(?:to|about)\b", label, re.I) else f"to {label}"


def _announce(kind: str, label: str, due: datetime) -> str:
    title = config.USER_TITLE
    if kind == "timer":
        return f"Your {label + ' ' if label else ''}timer is up, {title}."
    if kind == "alarm":
        return f"It's {spoken_clock(due)}, {title}. " + (f"Your alarm for {label}." if label else "This is your alarm.")
    return f"Reminder, {title}: {re.sub(r'^(?:to|about)\s+', '', label, flags=re.I)}."


def _missed(kind: str, label: str, due: datetime) -> str:
    if kind == "timer":
        return f"your {label + ' ' if label else ''}timer"
    if kind == "alarm":
        return "your alarm" + (f" for {label}" if label else "") + f" at {due:%I:%M %p}".replace(" 0", " ", 1)
    return f"your reminder {_about(label)} at {spoken_clock(due)}"
