"""Rules fast-path: requests simple enough to handle with plain Python, before the LLM sees them.
Instant, and never wrong in the ways a small model is (made-up times, "timer set" without setting one).
Patterns are strict on purpose — anything they don't fully match goes to the LLM as before.
Also home to the spoken time/duration parsing the scheduling tools use on the LLM path."""
import re
from datetime import datetime, timedelta

import config
import timezones

# leading filler a speech transcript often carries: "hey jarvis, ...", "okay so ..."
_FILLER = r"^\s*(?:(?:hey|ok(?:ay)?|so|jarvis|please)[\s,]*)*(?:(?:can|could) you tell me |do you know |tell me )?"
_TAIL = r"(?: now| right now| today| please)?[\s,]*(?:jarvis|please)?[\s?.!]*$"
_TIME_Q = r"(?:what(?:'s| is) the (?:current )?time|what time is it)"
_TIME_ASK = re.compile(_FILLER + r"(?:" + _TIME_Q + r"|the time)" + _TAIL, re.I)
_TIME_IN = re.compile(_FILLER + _TIME_Q + r" (?:right now |now )?in (?P<place>[a-z .'-]+?)" + _TAIL, re.I)
_DATE_ASK = re.compile(_FILLER + r"(?:what(?:'s| is) (?:the date|today's date|the day|today)|"
                                 r"what (?:day|date) is (?:it|today))" + _TAIL, re.I)

# a request to start a timer ("dimer" is a common mishearing) — not cancelling or asking about one
_TIMER_ASK = re.compile(r"\b(?:[td]imers?|remind me|count ?down)\b", re.I)
_TIMER_NOT = re.compile(r"\b(?:cancel|stop|delete|clear|how long|how many|left|remaining|don'?t|do not|never)\b", re.I)
_ALARM_ASK = re.compile(r"\b(?:alarms?|wake me(?: up)?)\b", re.I)
_SCHED = re.compile(r"\b(?:[td]imers?|alarms?|reminders?|count ?downs?)\b", re.I)
_CANCEL = re.compile(r"\b(?:cancel|stop|delete|remove|clear|turn off|switch off|get rid of)\b", re.I)
_LIST = re.compile(r"\b(?:what|which|any|list|show|how long|how much time|how many|left|remaining|"
                   r"do i have|are there|is there)\b", re.I)
_SET = re.compile(r"\b(?:set|start|create|add|make|put)\b", re.I)

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
              "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
_UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600}
_DURATION = re.compile(r"\b(\d+(?:\.\d+)?|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) +
                       r")\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)
_HALF_HOUR = re.compile(r"\bhalf an hour\b", re.I)
_AND_HALF = re.compile(r"\b(hours?|minutes?)\s+and a half\b", re.I)
_CLOCK_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}

# a clock time: "7 am", "6.30pm", "at 19:00", "for 7 o'clock", "noon". Never "for 5 minutes".
_NOT_UNIT = r"(?!\s*(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b)"
_AMPM = r"(?:\s*(?P<{}>[ap])\.?\s?m\b\.?)"
_CLOCK = re.compile(
    r"\b(?P<word>noon|midday|midnight)\b"
    r"|\b(?P<kw>at|by|for)\s+(?P<h1>\d{1,2})(?!\d)(?:[:.](?P<m1>\d{2})(?!\d))?(?:" + _AMPM.format("ap1") +
    r"|\s*o'?clock\b)?" + _NOT_UNIT +
    r"|\b(?P<h2>\d{1,2})(?!\d)(?:[:.](?P<m2>\d{2})(?!\d))?" + _AMPM.format("ap2") +
    r"|\b(?P<h3>\d{1,2})[:.](?P<m3>\d{2})\b" + _NOT_UNIT, re.I)
_TOMORROW = re.compile(r"\btomorrow\b", re.I)
_REMIND_LABEL = re.compile(r"\bremind me\b.*?\b((?:to|about) .+)$", re.I)  # keeps "to"/"about" for phrasing
_ALARM_LABEL = re.compile(r"\b(?:to|for|called|labell?ed) (.+)$", re.I)
_DANGLING = re.compile(r"(?:\s+\b(?:in|for|after|and|at|by|on|today|tomorrow)\b)+\s*$", re.I)


def timer_seconds(text: str) -> float | None:
    """Total seconds in a spoken duration ("1 minute", "an hour and 30 minutes", "half an hour")."""
    text, halves = _HALF_HOUR.subn("", text)
    total = 1800.0 * halves
    for unit in _AND_HALF.findall(text):  # "an hour and a half", "two minutes and a half"
        total += 1800 if unit.lower().startswith("h") else 30
    for num, unit in _DURATION.findall(text):
        n = float(num) if num[0].isdigit() else _NUM_WORDS[num.lower()]
        unit = unit.lower()
        total += n * _UNITS[unit[:-1] if unit.endswith("s") and len(unit) > 1 else unit]
    return total or None


def spoken_duration(seconds: float) -> str:
    """300 -> "5 minutes", 5400 -> "1 hour and 30 minutes" — nicer to hear than "5400 seconds"."""
    h, rem = divmod(round(seconds), 3600)
    m, s = divmod(rem, 60)
    parts = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    return " and ".join([", ".join(parts[:-1]), parts[-1]] if len(parts) > 1 else parts) or "0 seconds"


def spoken_clock(dt: datetime, now: datetime | None = None) -> str:
    """"7:00 AM", "7:00 AM tomorrow", "7:00 AM on Monday 26 October"."""
    now = now or datetime.now()
    day = ("" if dt.date() == now.date() else " tomorrow" if dt.date() == now.date() + timedelta(days=1)
           else f" on {dt:%A} {dt.day} {dt:%B}")  # dt.day: no "01 January"
    return f"{dt:%I:%M %p}".lstrip("0") + day


def _normalize_clock(text: str) -> str:
    """Spoken clock phrases into digits: "half past seven" -> "7:30", "at seven" -> "at 7",
    "quarter to 8" -> "7:45". Durations are left alone ("for one hour" stays unparseable as a time)."""
    word = r"(" + "|".join(_CLOCK_WORDS) + r")"
    text = re.sub(r"\b" + word + r"\b", lambda m: str(_CLOCK_WORDS[m[1].lower()]) if re.match(
        r"\s*(?:o'?clock|[ap]\.?\s?m\b)", text[m.end():], re.I) or re.search(
        r"\b(?:at|by|for|past|to|till|until)\s+$", text[:m.start()], re.I) else m[0], text, flags=re.I)
    text = re.sub(r"\bhalf past (\d{1,2})\b", r"\1:30", text, flags=re.I)
    text = re.sub(r"\bquarter past (\d{1,2})\b", r"\1:15", text, flags=re.I)
    return re.sub(r"\bquarter to (\d{1,2})\b", lambda m: f"{(int(m[1]) - 2) % 12 + 1}:45", text, flags=re.I)


def _clock_matches(text: str):
    for m in _CLOCK.finditer(_normalize_clock(text)):
        if m["word"]:
            h, mins, ap = (0 if m["word"].lower() == "midnight" else 12), 0, None
        else:
            n = next(i for i in "123" if m[f"h{i}"])
            h, mins, ap = int(m[f"h{n}"]), int(m[f"m{n}"] or 0), (m[f"ap{n}"] or "").lower() if n != "3" else ""
        if h > 23 or mins > 59 or (ap and not 1 <= h <= 12):
            continue
        # how sure we are this is a clock time: "7 pm" > "7:30" > "at 7" > "for 7" ("food for 2 people")
        score = 3 if (m["word"] or ap) else 2 if m[f"m{n}"] else 1 if (m["kw"] or "").lower() != "for" else 0
        yield score, h, mins, ap, m.group(0)


def _best_clock(text: str):
    return max(_clock_matches(text), key=lambda c: c[0], default=None)


def find_clock(text: str, now: datetime | None = None):
    """(datetime, phrase) for the clock time in text, or None. An hour with no am/pm means the next
    time that hour comes round ("at 7" at 10 am -> 7 pm today), or the morning when it says tomorrow."""
    now = now or datetime.now()
    if not (best := _best_clock(text)):
        return None
    _, h, mins, ap, phrase = best
    tomorrow = bool(_TOMORROW.search(text))
    if ap:
        h = h % 12 + (12 if ap == "p" else 0)
    base = now.replace(second=0, microsecond=0) + timedelta(days=tomorrow)
    hours = [h] if (ap or h > 12 or h == 0 or tomorrow) else [h, h + 12] if h < 12 else [12]
    candidates = [base.replace(hour=x, minute=mins) for x in hours]
    future = [c for c in candidates if c > now] or [c + timedelta(days=1) for c in candidates]
    return min(future), phrase + (" tomorrow" if tomorrow else "")


def find_when(text: str, now: datetime | None = None):
    """(datetime, phrase) for "at 5 pm" / "tomorrow at 9" / "in 20 minutes" / "7:30", or None.
    Used for the tools' `when`/`time` strings, so the LLM never converts times itself."""
    now = now or datetime.now()
    clock = find_clock(text, now)
    seconds = timer_seconds(text)
    if clock and (not seconds or _best_clock(text)[0] > 0):
        return clock
    if seconds:
        return now + timedelta(seconds=seconds), f"in {spoken_duration(seconds)}"
    if bare := re.fullmatch(r"\s*(\d{1,2})\s*", text):  # the LLM passing just "7"
        return find_clock(f"at {bare[1]}", now)
    return None


def _clean_label(label: str, phrase: str = "") -> str:
    label = _HALF_HOUR.sub("", _DURATION.sub("", label.replace(phrase.replace(" tomorrow", ""), "")))
    label = _TOMORROW.sub("", label)
    return _DANGLING.sub("", label.strip(" ,.!?")).strip(" ,.!?")


def fast_path(text: str):
    """(tool name, args, say) when the request needs no LLM, else None.
    say(tool_result) -> the sentence to speak."""
    title = config.USER_TITLE
    # tool results are already speakable: "Timer set for 5 minutes." -> "Timer set for 5 minutes, sir."
    titled = lambda r: f"{r[:-1]}, {title}{r[-1]}" if r[-1:] in (".", "?", "!") else f"{r}, {title}."  # noqa: E731
    if _TIME_ASK.match(text):  # get_time gives "Saturday 24 October 2026, 09:05 AM"
        return "get_time", {}, lambda r: f"It's {r.split(', ')[-1].lstrip('0')}, {title}."
    if _DATE_ASK.match(text):
        return "get_time", {}, lambda r: f"It's {r.split(', ')[0]}, {title}."
    if (m := _TIME_IN.match(text)) and timezones.resolve(m["place"]):
        return "time_in", {"place": m["place"].strip()}, titled
    if _SCHED.search(text) and _CANCEL.search(text):
        return "cancel_schedule", {"which": text.strip(" ?.!")}, titled
    if _SCHED.search(text) and _LIST.search(text) and not _SET.search(text):
        return "list_schedule", {}, titled
    if _TIMER_NOT.search(text):
        return None
    if (m := _REMIND_LABEL.search(text)) and (when := find_when(text)):
        if label := _clean_label(m.group(1), when[1]):
            return "set_reminder", {"text": label, "when": when[1]}, titled
    if _ALARM_ASK.search(text) and (clock := find_clock(text)):
        text = _normalize_clock(text)  # so "half past seven" is cut out of the label like "7:30" is
        rest = text.replace(clock[1].replace(" tomorrow", ""), "")
        label = _clean_label(m.group(1)) if (m := _ALARM_LABEL.search(rest)) else ""
        label = "" if re.match(r"wake|go off|ring", label, re.I) else label
        return "set_alarm", {"time": clock[1], "label": label}, titled
    if _TIMER_ASK.search(text) and (seconds := timer_seconds(text)):
        return "set_timer", {"seconds": seconds, "label": ""}, titled
    return None
