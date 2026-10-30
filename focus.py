"""Focus Guardian: keeps you on your main task. Say "focus on the login page in jarvis" and a background
thread watches which window is in front. Drift away (YouTube, another project) and JARVIS nudges you, firmer
the longer it lasts: gentle at 5 minutes, firm at 15, strong at 30 (focus_settings.json). Leave a breadcrumb
("next step is the logout button") and it's read back when you return or ask "where was I".
Snooze, take a break, park the task for later, switch, finish or stop it by voice; "how did I do today"
reads the day's focus log.

What counts as on task: an editor or terminal window on the session's project (from the window title, e.g.
"app.py - jarvis-v0 - Visual Studio Code"), or a terminal while a Claude Code session in that project was
active in the last few minutes (~/.claude/projects). No keyboard or mouse for a while means you've stepped
away and the clock pauses, except on a video site: watching counts as drifting.

The Windows probes (window, idle time) and the clock are injectable, so it all runs under tests on any OS.
Reminders are queued for the voice loop to say between conversations (take_prompt), never over one."""
import json
import os
import queue
import random
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import config
import hud
from rules import spoken_duration, timer_seconds

DEFAULTS = {
    "check_seconds": 20,
    "gentle_minutes": 5,
    "firm_minutes": 15,
    "strong_minutes": 30,
    "repeat_minutes": 10,         # after the strong reminder, again this often while still adrift
    "idle_minutes": 5,            # no keyboard or mouse this long: you've stepped away, the clock pauses
    "claude_active_minutes": 5,   # a Claude Code session written to this recently is "working in it"
    "snooze_minutes": 10,
    "break_minutes": 15,
    "ai_reminders": True,         # reminder lines written by the local model; off = the built-in lines
    "claude_projects_dir": "~/.claude/projects",
    "editors": ["code", "cursor", "windsurf", "pycharm64", "pycharm", "idea64", "webstorm64", "sublime_text",
                "notepad++", "devenv", "zed"],
    "editor_titles": ["visual studio code", "cursor", "windsurf", "pycharm", "intellij idea", "webstorm",
                      "sublime text", "notepad++", "visual studio", "zed"],
    "terminals": ["windowsterminal", "powershell", "pwsh", "cmd", "wezterm-gui", "alacritty", "claude"],
    "video_sites": ["youtube", "netflix", "twitch", "prime video", "disney+", "hulu", "tiktok", "vimeo"],
    "distractions": ["reddit", "twitter", "x.com", "instagram", "facebook", "whatsapp", "discord", "steam",
                     "9gag", "hacker news"],
    # always fine whatever the task: reading docs or checking the app you're building
    "work_titles": ["localhost", "127.0.0.1", "stack overflow", "docs", "documentation", "github", "claude"],
    # neither on nor off task: the lock screen, the desktop, task switching, JARVIS itself
    "neutral_titles": ["jarvis", "task switching", "program manager", "start", "search", "lockapp",
                       "windows default lock screen"],
    "test_mode": {"check_seconds": 2, "gentle_minutes": 0.25, "firm_minutes": 0.5, "strong_minutes": 1,
                  "repeat_minutes": 0.5, "idle_minutes": 1, "snooze_minutes": 0.5, "break_minutes": 0.5},
}

LEVELS = {1: "gentle", 2: "firm", 3: "strong"}
# what's said when the model is off or doesn't answer; {away} is how long, {what} where you are
FALLBACK = {
    1: ["{title}, a gentle nudge: {task} is still open.", "Still with {task}, {title}?",
        "{what} can wait, {title}. We were on {task}."],
    2: ["{title}, that's {away} on {what}. Time to get back to {task}.",
        "{away} adrift, {title}. Shall we get back to {task}?"],
    3: ["{title}, it's been {away}. Back to {task}, please.",
        "{title}, {away} on {what} now. Nobody else is going to finish {task}."],
}
_AI_PROMPT = """You are JARVIS, a dry, understated British butler AI. The user asked you to keep them focused on
their main task, and they have drifted. Write ONE short spoken reminder (max 25 words) addressed to them as
"{title}". Tone: {tone}. No emojis, no exclamation marks, no lists. Mention the task.
Task: {task}
Next step they noted: {next}
Where they are instead: {what}
Time away from the task: {away}"""
_TONES = {1: "a light, friendly nudge", 2: "firmer, mildly pointed", 3: "direct and serious, still polite"}

# --- voice commands (rules.py tries these before the LLM) ---
_F = r"^\W*(?:(?:hey|ok(?:ay)?|so|jarvis|please|right|alright)[\s,]+)*(?:(?:can|could|would) you |please |let'?s )?"
_E = r"[\s,]*(?:please|now|jarvis|sir|thanks?(?: you)?)?[\s.!?]*$"
_DUR = r"(?: for (?P<d>.+?))?"  # "for 20 minutes", "for a bit", "for lunch" (the default length)
_START = re.compile(_F + r"(?:(?:start|begin) (?:a )?(?:focus(?:ing)?|focus (?:mode|session))(?: on| for)? |"
                    r"(?:help me )?(?:stay )?focus(?:ing)? on |keep me (?:focused|on track) on |"
                    r"(?:i'?m|i am) (?:now |going to |gonna )?(?:be )?(?:working|focusing) on |"
                    r"(?:i'?m|i am) going to (?:work|focus) on |"
                    r"my (?:main )?(?:task|focus) (?:today |now )?is |"
                    r"(?:switch|change) (?:my )?(?:focus|task) to )"
                    r"(?P<t>.+?)" + _E, re.I)
_START_BARE = re.compile(_F + r"(?:(?:start|begin|turn on) (?:the )?focus(?: mode| session| guardian)?|"
                         r"focus (?:mode|guardian) on|focus mode)" + _E, re.I)
_NEXT = re.compile(_F + r"(?:(?:note|remember|save|log)(?: that)? )?(?:(?:the|my) )?next step(?: is| will be)?[:,]? "
                   r"(?P<s>.+?)" + _E + r"|" + _F + r"(?:leave a |drop a )?breadcrumb[:,]? (?P<s2>.+?)" + _E, re.I)
_STATUS = re.compile(_F + r"(?:where was i|what (?:am|was) i (?:working on|doing|focusing on)|"
                     r"what should i be (?:working on|doing)|what'?s my (?:focus|main task|next step)|"
                     r"focus status|how(?:'s| is) my focus(?: going)?)" + _E, re.I)
_SNOOZE = re.compile(_F + r"snooze(?: (?:focus|focus mode|the reminders?|reminders?|that|it|you))?" + _DUR + _E, re.I)
_BREAK = re.compile(_F + r"(?:(?:i'?m |i am )?(?:going to |gonna )?(?:take|taking|have|having|need) a (?:short |quick |little )?"
                    r"break|break (?:time|mode))" + _DUR + _E, re.I)
_RESUME = re.compile(_F + r"(?:back to work|(?:i'?m |i am )back(?: from (?:my |the )?break)?(?: to work)?|"
                     r"break(?:'s| is) over|end (?:the |my )?break|resume (?:the |my )?(?:focus|task)(?: mode)?|"
                     r"unpark(?: (?:it|that|the task|my task))?|(?:un)?pause (?:the |my )?focus|"
                     r"pick up (?:where i left off|my (?:task|focus)))" + _E, re.I)
_PARK = re.compile(_F + r"(?:park (?:this|that|it|the task|my task|focus|my focus)(?: for (?:now|later))?|"
                   r"put (?:this|that|it|the task|my task) on hold|pause (?:the |my )?(?:focus|task|focus mode))" + _E,
                   re.I)
_DONE = re.compile(_F + r"(?:(?:i'?m |i am |i'?ve |i have )?(?:done|finished)(?: with)? (?:this|that|the|my) "
                   r"(?:task|focus)|(?:task|focus) (?:is )?"
                   r"(?:done|complete|completed|finished)|mark (?:the |my )?(?:task|focus) (?:as )?(?:done|complete))"
                   + _E, re.I)
_STOP = re.compile(_F + r"(?:(?:stop|end|cancel|turn off|exit|quit|disable|switch off) (?:the )?"
                   r"focus(?: mode| session| guardian)?|focus (?:mode |guardian )?off|"
                   r"stop (?:nagging|reminding) me(?: to focus)?)" + _E, re.I)
_LOG = re.compile(_F + r"(?:how (?:did i do|was my focus|focused (?:was|have) i been|focused was i)(?: today)?|"
                  r"(?:my |today'?s |the )?focus (?:log|report|summary)(?: for today)?|"
                  r"what did i (?:work on|get done|focus on) today)" + _E, re.I)
# "the login page in jarvis-v0", "the API for project atlas": the project, said with the task
_PROJECT = re.compile(r"\s+(?:in|on|for) (?:the |my )?(?:project (?P<a>[\w.-]+)|(?P<b>[\w.-]+) project|"
                      r"(?P<c>[a-z0-9]+(?:[-_][a-z0-9]+)+))\s*$", re.I)
_SEP = re.compile(r"\s+[-–—|]\s+")


def parse(text: str):
    """(tool, args) for a spoken focus command, else None."""
    if m := _NEXT.match(text):
        return "focus_next_step", {"step": (m["s"] or m["s2"]).strip()}
    if _STATUS.match(text):
        return "focus_status", {}
    if _LOG.match(text):
        return "focus_log", {}
    if _STOP.match(text):
        return "focus_stop", {}
    if _DONE.match(text):
        return "focus_done", {}
    if _PARK.match(text):
        return "focus_park", {}
    if _RESUME.match(text):
        return "focus_resume", {}
    if m := _SNOOZE.match(text):
        return "focus_snooze", {"minutes": m["d"] or ""}
    if m := _BREAK.match(text):
        return "focus_break", {"minutes": m["d"] or ""}
    if _START_BARE.match(text):
        return "focus_start", {"task": ""}
    if (m := _START.match(text)) and m["t"].lower() not in ("it", "that", "this", "something", "stuff"):
        return "focus_start", {"task": m["t"].strip()}
    return None


def load_settings(path: str | os.PathLike | None = None, test_mode: bool = False) -> dict:
    """DEFAULTS with focus_settings.json over them, and its test_mode block over that in test mode."""
    s = {k: v for k, v in DEFAULTS.items()}
    try:
        s.update(json.loads(Path(path or config.FOCUS_SETTINGS).read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"  [focus] couldn't read {path or config.FOCUS_SETTINGS}, using defaults: {e}")
    if test_mode:
        s.update(s.get("test_mode") or {})
    return s


# --- what's on screen (Windows) ---

def foreground_window() -> tuple[str, str]:
    """(title, process name) of the window in front; ("", "") off Windows or if it can't be read."""
    if sys.platform != "win32":
        return "", ""
    import ctypes
    from ctypes import wintypes

    import apps
    u = apps._user32
    hwnd = u.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(hwnd, buf, 512)
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return buf.value.strip(), apps._process_name(ctypes.WinDLL("kernel32"), pid.value)


def idle_seconds() -> float:
    """Seconds since the last key press or mouse move anywhere (0 off Windows)."""
    if sys.platform != "win32":
        return 0.0
    import ctypes

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInput(ctypes.sizeof(LastInput), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def _squash(text: str) -> str:
    """"Jarvis V0" / "jarvis-v0" / "C--Users-me-jarvis-v0" -> letters and digits only, to compare names."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _model_line(prompt: str) -> str:
    """One reminder from the local model; raises llm.ERRORS if Ollama doesn't answer."""
    import llm
    msg = llm.client.chat(model=config.OLLAMA_MODEL, messages=[{"role": "user", "content": prompt}],
                          think=llm.think(), keep_alive=llm.keep_alive(),
                          options={"temperature": 0.8, "num_ctx": config.OLLAMA_NUM_CTX, "num_predict": 80}).message
    return msg.content or ""


class FocusGuardian:
    def __init__(self, db_path: str, settings: dict | None = None, *, clock=time.time, window=foreground_window,
                 idle=idle_seconds, ai=_model_line, deliver=None, verbose: bool = False):
        self.settings = settings if settings is not None else load_settings(test_mode=config.FOCUS_TEST_MODE)
        self.clock, self.window, self.idle, self.ai = clock, window, idle, ai
        self.deliver = deliver  # set (text mode): reminders are said at once; None: queued for the voice loop
        self.verbose = verbose or config.FOCUS_TEST_MODE
        self.prompts: queue.Queue = queue.Queue()
        self.lock = threading.RLock()
        self.state_path = Path(db_path).parent / "focus_state.json"
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS focus_log(id INTEGER PRIMARY KEY, day TEXT, task TEXT, "
                        "project TEXT, started REAL, ended REAL, focused REAL, drifted REAL, reminders INTEGER, "
                        "outcome TEXT)")
        self.db.commit()
        self.s: dict = {}
        self._thread = None
        self._load()
        if self.s.get("task"):
            self._ensure_thread()

    # --- voice commands: each returns the sentence to say ---

    def start(self, task: str) -> str:
        title = config.USER_TITLE
        task, project = self._split_project(task.strip(" .,"))
        with self.lock:
            if not task:
                if self.s.get("task"):
                    if self.s["status"] != "active":
                        return self.resume()
                    return f"Already keeping you on {self.s['task']}, {title}."
                return f"What are we focusing on, {title}?"
            previous = self.s.get("task")
            if previous:
                self._close("switched")
            project = project or self.detect_project()
            now = self.clock()
            self.s = {"task": task, "project": project, "next_step": "", "status": "active", "until": 0,
                      "started": now, "last_tick": now, "drift_start": None, "level": 0, "last_nag": 0,
                      "focused": 0.0, "drifted": 0.0, "reminders": 0, "last_what": ""}
            self._save()
        self._ensure_thread()
        hud.emit("focus", event="start", task=task, project=project)
        watching = f" I'll keep an eye on {project}." if project else ""
        if previous:
            return f"Switched from {previous} to {task}, {title}.{watching}"
        return f"Focusing on {task}, {title}.{watching} Tell me the next step whenever you like."

    def next_step(self, step: str) -> str:
        title = config.USER_TITLE
        step = step.strip(" .,")
        with self.lock:
            if not self.s.get("task"):
                return f"There's no focus session running, {title}. Tell me what you're working on first."
            if not step:
                return f"What's the next step, {title}?"
            self.s["next_step"] = step
            self._save()
        return f"Noted, {title}. Next step: {step}."

    def snooze(self, spoken: str = "") -> str:
        return self._pause("snoozed", spoken, self.settings["snooze_minutes"])

    def take_break(self, spoken: str = "") -> str:
        return self._pause("break", spoken, self.settings["break_minutes"])

    def _pause(self, status: str, spoken: str, default_minutes: float) -> str:
        title = config.USER_TITLE
        seconds = timer_seconds(spoken or "") or (600 if re.search(r"\b(?:bit|while|few)\b", spoken or "") else 0)
        seconds = seconds or default_minutes * 60
        with self.lock:
            if not self.s.get("task"):
                return (f"Enjoy it, {title}. There's no focus session to pause." if status == "break" else
                        f"There's nothing to snooze, {title}. No focus session is running.")
            if self.s["status"] == "parked" and status == "snoozed":
                return f"You've parked {self.s['task']}, {title}, so there's nothing to snooze."
            self.s.update(status=status, until=self.clock() + seconds, drift_start=None, level=0)
            self._save()
        how_long = _spoken(seconds)
        if status == "break":
            return f"Enjoy your break, {title}. I'll call you back to {self.s['task']} in {how_long}."
        return f"Reminders snoozed for {how_long}, {title}."

    def park(self) -> str:
        title = config.USER_TITLE
        with self.lock:
            if not self.s.get("task"):
                return f"There's nothing to park, {title}."
            self._account()
            self.s.update(status="parked", until=0, drift_start=None, level=0)
            self._save()
        crumb = f" Next step when you return: {self.s['next_step']}." if self.s.get("next_step") else ""
        return f"Parked {self.s['task']}, {title}. Say \"back to work\" to pick it up.{crumb}"

    def resume(self) -> str:
        title = config.USER_TITLE
        with self.lock:
            if not self.s.get("task"):
                return f"There's nothing to resume, {title}. Tell me what you're working on."
            was = self.s["status"]
            self.s.update(status="active", until=0, drift_start=None, level=0, last_tick=self.clock())
            self._save()
        self._ensure_thread()
        if was == "active":
            return f"We're still on {self.s['task']}, {title}." + self._crumb()
        return f"Back to {self.s['task']}, {title}." + self._crumb()

    def done(self) -> str:
        title = config.USER_TITLE
        with self.lock:
            if not self.s.get("task"):
                return f"There's no focus session running, {title}."
            task, focused = self.s["task"], self._account()["focused"]
            self._close("done")
        return f"Well done, {title}. That's {task} finished, after {_worked(focused)} of focus."

    def stop(self) -> str:
        title = config.USER_TITLE
        with self.lock:
            if not self.s.get("task"):
                return f"Focus mode is already off, {title}."
            task = self.s["task"]
            self._close("stopped")
        return f"Focus mode off, {title}. I've logged your time on {task}."

    def status(self) -> str:
        title = config.USER_TITLE
        with self.lock:
            if not self.s.get("task"):
                return f"No focus session is running, {title}. Say \"focus on\" and the task to start one."
            s = self._account()
            state = {"active": "", "parked": " It's parked at the moment.",
                     "break": f" You're on a break for another {_spoken(max(s['until'] - self.clock(), 0))}.",
                     "snoozed": f" Reminders are snoozed for another {_spoken(max(s['until'] - self.clock(), 0))}."}
            project = f" in {s['project']}" if s.get("project") else ""
            return (f"You're working on {s['task']}{project}, {title}: {_worked(s['focused'])} focused so far."
                    + state.get(s["status"], "") + self._crumb())

    def log(self, day: str | None = None) -> str:
        """The day's focus: total time on task, each task and how it ended, time adrift and reminders."""
        title = config.USER_TITLE
        day = day or datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
        with self.lock:
            rows = self.db.execute("SELECT task, focused, drifted, reminders, outcome FROM focus_log WHERE day = ? "
                                   "ORDER BY started", (day,)).fetchall()
            if self.s.get("task") and _day(self.s["started"]) == day:
                s = self._account()
                rows.append((s["task"], s["focused"], s["drifted"], s["reminders"], "in progress"))
        if not rows:
            return f"No focus sessions today yet, {title}."
        tasks: dict[str, list] = {}  # one task parked and resumed is one entry
        for task, focused, drifted, reminders, outcome in rows:
            t = tasks.setdefault(task, [0.0, 0.0, 0, outcome])
            t[0] += focused
            t[1] += drifted
            t[2] += reminders
            t[3] = outcome
        focused = sum(t[0] for t in tasks.values())
        drifted = sum(t[1] for t in tasks.values())
        reminders = sum(t[2] for t in tasks.values())
        listed = [f"{task}, {_worked(t[0])}" + (f", {t[3]}" if t[3] in ("done", "in progress") else "")
                  for task, t in tasks.items()]
        listed = listed[0] if len(listed) == 1 else "; ".join(listed[:-1]) + "; and " + listed[-1]
        return (f"Today you've focused for {_worked(focused)}, {title}: {listed}. "
                + (f"You drifted for {_spoken(drifted)}, with {reminders} reminder{'s' if reminders != 1 else ''}."
                   if drifted >= 60 or reminders else "Not a moment adrift."))

    # --- the watch ---

    def has_prompt(self) -> bool:
        return not self.prompts.empty()

    def take_prompt(self) -> str | None:
        """The newest reminder waiting to be said (older ones it replaces are dropped), or None."""
        latest = None
        while True:
            try:
                latest = self.prompts.get_nowait()
            except queue.Empty:
                return latest

    def tick(self):
        """One look at the screen: count the time, and queue a reminder if one is due."""
        nag = None
        with self.lock:
            s = self.s
            if not s.get("task"):
                return
            now = self.clock()
            if s["status"] in ("snoozed", "break") and now >= s["until"]:
                was = s["status"]
                s.update(status="active", until=0, drift_start=None, level=0, last_tick=now)
                self._save()
                if was == "break":
                    self._say(f"Break's over, {config.USER_TITLE}. Back to {s['task']}." + self._crumb())
                return
            if s["status"] != "active":
                return
            # capped, so a sleeping PC or a stalled thread doesn't count hours in one go
            dt = min(max(now - s["last_tick"], 0), self.settings["check_seconds"] * 3)
            s["last_tick"] = now
            verdict, what = self.classify()
            if self.verbose:
                print(f"  [focus] {verdict}: {what or '-'}")
            if verdict == "on":
                s["focused"] += dt
                if s["level"] >= 2 and s.get("next_step"):  # back after a real drift: where you'd got to
                    self._say(f"Welcome back, {config.USER_TITLE}. Next step: {s['next_step']}.")
                s.update(drift_start=None, level=0)
            elif verdict == "off":
                s["drifted"] += dt
                s["last_what"] = what
                if s["drift_start"] is None:
                    s["drift_start"] = now - dt
                away = now - s["drift_start"]
                level = sum(away >= self.settings[f"{name}_minutes"] * 60 for name in LEVELS.values())
                repeat = level == 3 and s["level"] == 3 and now - s["last_nag"] >= self.settings["repeat_minutes"] * 60
                if level > s["level"] or repeat:
                    s.update(level=level, last_nag=now, reminders=s["reminders"] + 1)
                    nag = (level, s["task"], s.get("next_step", ""), what, away)
            elif s["drift_start"] is not None:  # idle or neutral: the drift clock waits with you
                s["drift_start"] += dt
            self._save()
        if nag:
            line = self._line(*nag)
            hud.emit("focus", event="reminder", level=LEVELS[nag[0]], text=line)
            self._say(line)

    def classify(self) -> tuple[str, str]:
        """("on" | "off" | "idle" | "neutral", where you are)."""
        st = self.settings
        title, proc = self.window()
        low, p = title.lower(), proc.lower()
        video = next((v for v in st["video_sites"] if v in low), None)
        if not video and self.idle() >= st["idle_minutes"] * 60:
            return "idle", ""
        if not title and not proc:
            return "neutral", ""
        if not video and (any(n == low or low.startswith(n + " ") for n in st["neutral_titles"])
                          or p in ("lockapp", "explorer") and not title):
            return "neutral", ""
        what = self._what(title, proc, video)
        if video:
            return "off", what
        if self._on_task(title, proc):
            return "on", what
        return "off", what

    def _on_task(self, title: str, proc: str) -> bool:
        st, low, p = self.settings, title.lower(), proc.lower()
        editor = p in st["editors"] or any(low.endswith(e) for e in st["editor_titles"])
        terminal = p in st["terminals"]
        project = self.s.get("project") or ""
        if project:
            if _squash(project) in _squash(title):
                return True
            if terminal:  # Claude Code's terminal title doesn't name the project; its session files do
                latest = self.claude_latest()
                return bool(latest and _same_project(latest, project) and
                            self.clock() - latest[2] <= st["claude_active_minutes"] * 60)
            if editor:
                return False  # an editor on another project
        elif editor or terminal:
            return True
        if any(any(w in d for d in _SEP.split(low)) for w in st["work_titles"]) and not \
                any(d in low for d in st["distractions"]):
            return True
        words = [w for w in re.findall(r"[a-z0-9]{4,}", self.s.get("task", "").lower()) if w not in _STOPWORDS]
        return any(re.search(rf"\b{re.escape(w)}", low) for w in words)

    def _what(self, title: str, proc: str, video: str | None) -> str:
        """A short, speakable name for where you are: "YouTube", "Reddit", "Code on another project"."""
        low = title.lower()
        site = video or next((d for d in self.settings["distractions"] if d in low), None)
        if site:
            return {"youtube": "YouTube", "x.com": "X"}.get(site, site.title())
        parts = [x for x in _SEP.split(title) if x]
        return (parts[-1] if parts else proc)[:60]

    def claude_latest(self) -> tuple[str, str, float] | None:
        """(folder name or "", session directory name, last write) of the most recently active Claude Code
        session, or None if there's none."""
        root = Path(os.path.expanduser(self.settings["claude_projects_dir"]))
        try:
            sessions = [f for d in root.iterdir() if d.is_dir() for f in d.glob("*.jsonl")]
        except OSError:
            return None
        if not sessions:
            return None
        newest = max(sessions, key=lambda f: f.stat().st_mtime)
        return _session_cwd(newest), newest.parent.name, newest.stat().st_mtime

    def detect_project(self) -> str:
        """The project in front right now: the editor's folder from its title, or Claude Code's session."""
        title, proc = self.window()
        st, p = self.settings, proc.lower()
        clean = re.sub(r"^[●*\s]+|\s*\[(?:administrator|admin)\]|\s*\((?:workspace|wsl[^)]*)\)", "", title,
                       flags=re.I)
        parts = [x.strip() for x in _SEP.split(clean) if x.strip()]
        names = st["editor_titles"]
        if parts and any(parts[-1].lower().endswith(e) for e in names):
            body = parts[:-1]  # "app.py - jarvis-v0 - Visual Studio Code": the folder is the last one left
            name = body[-1] if body else ""
        elif p in st["editors"] and parts:
            name = parts[0]  # PyCharm: "jarvis-v0 – app.py"
        elif p in st["terminals"] and (latest := self.claude_latest()) and \
                self.clock() - latest[2] <= st["claude_active_minutes"] * 60:
            name = latest[0]
        else:
            name = ""
        return "" if name.lower() in ("", "welcome", "untitled", "get started", "settings") else name

    def _split_project(self, task: str) -> tuple[str, str]:
        if m := _PROJECT.search(task):
            return task[:m.start()].strip(" ,"), (m["a"] or m["b"] or m["c"]).strip(".")
        return task, ""

    def _line(self, level: int, task: str, next_step: str, what: str, away: float) -> str:
        title = config.USER_TITLE
        fallback = random.choice(FALLBACK[level]).format(title=title, task=task, what=what or "something else",
                                                         away=_spoken(away))
        fallback = fallback[0].upper() + fallback[1:]
        if level == 3 and next_step:
            fallback += f" The next step was {next_step}."
        import brains
        if not self.settings["ai_reminders"] or not brains.awake:
            return fallback
        try:
            text = self.ai(_AI_PROMPT.format(title=title, tone=_TONES[level], task=task, next=next_step or "(none)",
                                             what=what or "unknown", away=_spoken(away)))
        except Exception as e:  # Ollama down or slow: never lose the reminder over it
            print(f"  [focus] model didn't answer, using a stock line: {type(e).__name__}")
            return fallback
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip().strip("\"'").replace("!", ".")
        text = " ".join(re.split(r"(?<=[.?])\s+", text)[:2])
        return text if 8 <= len(text) <= 240 else fallback

    def _say(self, text: str):
        if self.deliver:
            self.deliver(text)
        else:
            print(f"  [focus] queued: {text}")
            self.prompts.put(text)

    def _crumb(self) -> str:
        return f" Next step: {self.s['next_step']}." if self.s.get("next_step") else ""

    def _account(self) -> dict:
        """Count the time since the last look as focused if the session is running (for status and logs)."""
        s = self.s
        if s.get("status") == "active" and s.get("drift_start") is None:
            now = self.clock()
            s["focused"] += min(max(now - s["last_tick"], 0), self.settings["check_seconds"] * 3)
            s["last_tick"] = now
        return s

    def _close(self, outcome: str):
        """End the session: one row in the day's log."""
        s = self._account()
        self.db.execute("INSERT INTO focus_log(day, task, project, started, ended, focused, drifted, reminders, "
                        "outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (_day(s["started"]), s["task"], s.get("project", ""), s["started"], self.clock(),
                         s["focused"], s["drifted"], s["reminders"], outcome))
        self.db.commit()
        hud.emit("focus", event=outcome, task=s["task"])
        self.s = {}
        self._save()

    def _save(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.s), encoding="utf-8")
        except OSError as e:
            print(f"  [focus] couldn't save state: {e}")

    def _load(self):
        """A session survives a restart; the time JARVIS was off isn't counted either way."""
        try:
            self.s = json.loads(self.state_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            self.s = {}
        if self.s.get("task"):
            self.s.update(last_tick=self.clock(), drift_start=None, level=0)
            print(f"  [focus] still on: {self.s['task']} ({self.s['status']})")

    def _ensure_thread(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="focus")
            self._thread.start()

    def _loop(self):
        while True:
            time.sleep(self.settings["check_seconds"])
            try:
                self.tick()
            except Exception as e:  # a bad probe must never end the watch
                print(f"  [focus] check failed: {type(e).__name__}: {e}")


_STOPWORDS = {"this", "that", "with", "from", "into", "page", "work", "working", "task", "stuff", "thing", "things",
              "some", "more", "about", "their", "your", "mine"}


def _same_project(latest: tuple, project: str) -> bool:
    """A Claude Code session is in this project: its folder is named so, or its directory (the folder's path
    with every separator made a dash: "C--Users-me-code-jarvis-v0") ends with it."""
    cwd, directory, _ = latest
    return _squash(cwd) == _squash(project) if cwd else _squash(directory).endswith(_squash(project))


def _session_cwd(path: Path) -> str:
    """The name of the folder a Claude Code session ran in, from the "cwd" near the end of its file ("" if none)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(f.tell() - 65536, 0))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            if '"cwd"' in line:
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    continue
                if cwd:
                    return re.split(r"[\\/]", cwd.rstrip("\\/"))[-1]
    except OSError:
        pass
    return ""


def _worked(seconds: float) -> str:
    """Time on a task: "under a minute" rather than "12 seconds"."""
    return "under a minute" if seconds < 60 else _spoken(seconds)


def _spoken(seconds: float) -> str:
    """Minutes once it's a minute or more ("12 minutes", not "12 minutes and 7 seconds")."""
    return spoken_duration(round(seconds / 60) * 60 if seconds >= 60 else seconds)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
