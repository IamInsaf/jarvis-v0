"""Rules fast-path: requests simple enough to handle with plain Python, before the LLM sees them.
Instant, and never wrong in the ways a small model is (made-up times, "timer set" without setting one).
Patterns are strict on purpose — anything they don't fully match goes to the LLM as before."""
import re

import config

# leading filler a speech transcript often carries: "hey jarvis, ...", "okay so ..."
_FILLER = r"^\s*(?:(?:hey|ok(?:ay)?|so|jarvis|please)[\s,]*)*(?:(?:can|could) you tell me |do you know |tell me )?"
_TAIL = r"(?: now| right now| today| please)?[\s,]*(?:jarvis|please)?[\s?.!]*$"
_TIME_ASK = re.compile(_FILLER + r"(?:what(?:'s| is) the (?:current )?time|what time is it|the time)" + _TAIL, re.I)
_DATE_ASK = re.compile(_FILLER + r"(?:what(?:'s| is) (?:the date|today's date|the day|today)|"
                                 r"what (?:day|date) is (?:it|today))" + _TAIL, re.I)

# a request to start a timer ("dimer" is a common mishearing) — not cancelling or asking about one
_TIMER_ASK = re.compile(r"\b(?:[td]imers?|remind me|count ?down)\b", re.I)
_TIMER_NOT = re.compile(r"\b(?:cancel|stop|delete|clear|how long|how many|left|remaining|don'?t|do not|never)\b", re.I)
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
              "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
_UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600}
_DURATION = re.compile(r"\b(\d+(?:\.\d+)?|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) +
                       r")\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.I)
_HALF_HOUR = re.compile(r"\bhalf an hour\b", re.I)
_REMIND_LABEL = re.compile(r"\bremind me\b.*?\bto (.+)$", re.I)
_DANGLING = re.compile(r"(?:\s+\b(?:in|for|after|and)\b)+\s*$", re.I)  # "call mom in" once the duration is cut


def timer_seconds(text: str) -> float | None:
    """Total seconds in a spoken duration ("1 minute", "an hour and 30 minutes", "half an hour")."""
    text, halves = _HALF_HOUR.subn("", text)
    total = 1800.0 * halves
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


def _timer_label(text: str) -> str:
    m = _REMIND_LABEL.search(text)
    if not m:
        return ""
    label = _HALF_HOUR.sub("", _DURATION.sub("", m.group(1)))
    return _DANGLING.sub("", label.strip(" ,.!?")).strip(" ,.!?")


def fast_path(text: str):
    """(tool name, args, say) when the request needs no LLM, else None.
    say(tool_result) -> the sentence to speak."""
    title = config.USER_TITLE
    if _TIME_ASK.match(text):  # get_time gives "Saturday 24 October 2026, 09:05 AM"
        return "get_time", {}, lambda r: f"It's {r.split(', ')[-1].lstrip('0')}, {title}."
    if _DATE_ASK.match(text):
        return "get_time", {}, lambda r: f"It's {r.split(', ')[0]}, {title}."
    if _TIMER_ASK.search(text) and not _TIMER_NOT.search(text) and (seconds := timer_seconds(text)):
        label = _timer_label(text)
        heard = spoken_duration(seconds)
        ok = f"I'll remind you to {label} in {heard}, {title}." if label else f"Timer set for {heard}, {title}."
        return "set_timer", {"seconds": seconds, "label": label}, lambda r: ok if r.startswith("Your") else r
    return None
