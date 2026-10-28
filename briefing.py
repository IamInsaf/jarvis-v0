"""The day at a glance: date and time, what's scheduled today, and the top of the to-do list. Spoken when asked
("good morning", "brief me", "what's my day look like") and, if BRIEFING_TIME is set, once a day unprompted."""
import re
import threading
import time
from datetime import datetime, timedelta

import config
from rules import spoken_clock

ASK = re.compile(r"^\W*(?:(?:hey|ok(?:ay)?|so|jarvis)[,\s]+)*(?:good morning(?:\W+jarvis)?|morning\W+jarvis|"
                 r"(?:give me |can i (?:get|have) )?(?:my |the |a )?(?:daily |morning )?(?:briefing|brief|rundown)|"
                 r"brief me|what'?s (?:my day|on (?:for )?today|the plan (?:for )?today)(?: look(?:ing)? like)?|"
                 r"what (?:do i have|have i got) (?:on )?today|how does my day look)\W*$", re.I)


def compose(scheduler, todos, now: datetime | None = None) -> str:
    now = now or datetime.now()
    hour = now.hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    parts = [f"{greeting}, {config.USER_TITLE}. It's {now:%A} the {_ordinal(now.day)}, {spoken_clock(now, now)}."]

    end_of_day = now.replace(hour=23, minute=59, second=59)
    today = [(k, label, due) for _, k, label, due in scheduler.pending() if due <= end_of_day]
    if today:
        listed = [scheduler.describe(k, label, due, now) for k, label, due in today[:4]]
        more = f", and {len(today) - 4} more" if len(today) > 4 else ""
        parts.append(f"Today you have {_join(listed)}{more}.")
    else:
        parts.append("Nothing is scheduled for today.")

    tasks = [text[:1].lower() + text[1:] for _, text in todos._open()]  # mid-sentence: "buy milk", not "Buy milk"
    if tasks:
        top = _join(tasks[:3])
        parts.append(f"Your to-do list has {len(tasks)} item{'s' if len(tasks) != 1 else ''}"
                     + (f", starting with {top}." if len(tasks) > 1 else f": {top}."))
    else:
        parts.append("Your to-do list is clear.")
    return " ".join(parts)


def start_daily(say_briefing) -> threading.Thread | None:
    """Call say_briefing() every day at BRIEFING_TIME ("07:30"); does nothing if it isn't set."""
    at = _parse_time(config.BRIEFING_TIME)
    if at is None:
        return None

    def loop():
        while True:
            time.sleep(max(1.0, (next_time(at) - datetime.now()).total_seconds()))
            try:
                say_briefing()
            except Exception as e:  # a failed briefing must not kill the thread for tomorrow's
                print(f"  [briefing] failed: {e}")
            time.sleep(61)  # past the minute, so it isn't said twice

    t = threading.Thread(target=loop, daemon=True, name="briefing")
    t.start()
    print(f"  [briefing] daily at {at[0]:02d}:{at[1]:02d}")
    return t


def next_time(at: tuple[int, int], now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    due = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    return due if due > now else due + timedelta(days=1)


def _parse_time(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*", text or "")
    if not m or int(m[1]) > 23 or int(m[2] or 0) > 59:
        return None
    return int(m[1]), int(m[2] or 0)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
