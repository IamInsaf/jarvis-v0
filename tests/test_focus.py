"""Focus Guardian, with the clock, the window in front, idle time and Claude Code's session files faked.
Run: python -m unittest discover tests"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brains  # noqa: E402
import config  # noqa: E402
import focus  # noqa: E402
import intents  # noqa: E402
import rules  # noqa: E402

START = 1_793_000_000.0  # a Thursday morning in 2026
VSCODE = ("guardian.py - jarvis-v0 - Visual Studio Code", "Code")
OTHER_PROJECT = ("index.html - sweet-crumbs - Visual Studio Code", "Code")
YOUTUBE = ("Cats being cats - YouTube - Google Chrome", "chrome")
REDDIT = ("r/programming - Reddit - Google Chrome", "chrome")
TERMINAL = ("✳ Fix the watcher", "WindowsTerminal")


class Clock:
    def __init__(self):
        self.t = START

    def __call__(self):
        return self.t


class FocusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.claude = self.root / "claude-projects"
        self.claude.mkdir()
        self.settings = focus.load_settings(self.root / "missing.json")
        self.settings["claude_projects_dir"] = str(self.claude)
        self.clock = Clock()
        self.front = VSCODE
        self.idle = 0.0
        self.ai_lines = []  # what the fake model says; empty = it's down
        for p in (mock.patch.object(brains, "awake", True), mock.patch.object(config, "USER_TITLE", "sir"),
                  mock.patch.object(focus.random, "choice", lambda lines: lines[0])):  # the same stock line every time
            p.start()
            self.addCleanup(p.stop)
        self.g = self.guardian()
        # a real watch thread would tick on its own: the tests tick by hand
        self.g._ensure_thread = lambda: None

    def tearDown(self):
        self.g.db.close()
        self.tmp.cleanup()

    def guardian(self):
        g = focus.FocusGuardian(str(self.root / "data" / "jarvis.db"), self.settings, clock=self.clock,
                                window=lambda: self.front, idle=lambda: self.idle, ai=self.ai)
        g._ensure_thread = lambda: None
        return g

    def ai(self, prompt):
        self.prompt = prompt
        if not self.ai_lines:
            raise ConnectionError("ollama is down")
        return self.ai_lines.pop(0)

    def run_for(self, minutes, g=None):
        """Tick every check for this long; the reminders said along the way."""
        g = g or self.g
        said = []
        for _ in range(int(minutes * 60 / self.settings["check_seconds"])):
            self.clock.t += self.settings["check_seconds"]
            g.tick()
            if p := g.take_prompt():
                said.append(p)
        return said

    def session(self, project, cwd=None, age=0.0, name="s1.jsonl"):
        """A Claude Code session file last written `age` seconds ago."""
        d = self.claude / project
        d.mkdir(exist_ok=True)
        f = d / name
        lines = [{"type": "user", "cwd": cwd}] if cwd else [{"type": "summary"}]
        f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        os.utime(f, (self.clock.t - age, self.clock.t - age))
        return f

    # --- escalation ---

    def test_reminders_escalate_gentle_firm_strong_then_repeat(self):
        self.assertIn("I'll keep an eye on jarvis-v0", self.g.start("the watcher"))
        self.assertEqual(self.run_for(10), [])
        self.front = YOUTUBE
        said = self.run_for(4.5)
        self.assertEqual(said, [])
        said = self.run_for(1)                         # 5 minutes adrift: gentle
        self.assertEqual(len(said), 1)
        self.assertIn("the watcher", said[0])
        self.assertEqual(len(self.run_for(10)), 1)     # 15: firm
        self.assertEqual(len(self.run_for(15)), 1)     # 30: strong
        self.assertEqual(len(self.run_for(10)), 1)     # and again every 10 minutes after
        self.assertEqual(self.g.s["reminders"], 4)
        self.assertAlmostEqual(self.g.s["focused"], 600, delta=1)

    def test_strong_fallback_line_carries_the_next_step(self):
        self.g.start("the watcher")
        self.g.next_step("write the idle check")
        self.front = YOUTUBE
        said = self.run_for(31)
        self.assertEqual(len(said), 3)
        self.assertIn("YouTube", said[1])  # firm: where you are
        self.assertIn("The next step was write the idle check.", said[2])

    def test_model_writes_the_line_when_it_answers(self):
        self.ai_lines = ["<think>hmm</think>\"Sir, the watcher misses you!\""]
        self.g.start("the watcher")
        self.front = REDDIT
        said = self.run_for(5)
        self.assertEqual(said, ["Sir, the watcher misses you."])
        self.assertIn("Where they are instead: Reddit", self.prompt)
        self.assertIn("light, friendly", self.prompt)

    def test_brain_off_uses_stock_lines_without_asking_the_model(self):
        self.ai_lines = ["should not be used"]
        self.g.start("the watcher")
        self.front = REDDIT
        with mock.patch.object(brains, "awake", False):
            said = self.run_for(5)
        self.assertEqual(len(said), 1)
        self.assertNotEqual(said[0], "should not be used")

    def test_coming_back_resets_and_reads_the_breadcrumb_after_a_firm_reminder(self):
        self.g.start("the watcher")
        self.g.next_step("the idle check")
        self.front = YOUTUBE
        self.run_for(16)
        self.front = VSCODE
        said = self.run_for(1)
        self.assertEqual(said, ["Welcome back, sir. Next step: the idle check."])
        self.front = YOUTUBE
        self.assertEqual(self.run_for(4.5), [])  # the drift clock starts again from zero

    def test_take_prompt_gives_only_the_newest(self):
        self.g.start("the watcher")
        self.front = YOUTUBE
        for _ in range(int(16 * 60 / self.settings["check_seconds"])):
            self.clock.t += self.settings["check_seconds"]
            self.g.tick()
        self.assertTrue(self.g.has_prompt())
        self.assertIn("15 minutes on YouTube", self.g.take_prompt())  # the firm one; the gentle one is stale
        self.assertIsNone(self.g.take_prompt())

    # --- what counts as on task ---

    def test_idle_pauses_the_drift_clock_but_video_keeps_counting(self):
        self.g.start("the watcher")
        self.front, self.idle = REDDIT, 600
        self.assertEqual(self.run_for(20), [])
        self.assertEqual(self.g.classify()[0], "idle")
        self.front = YOUTUBE  # nobody touching the keyboard while a video plays is still watching it
        self.assertEqual(len(self.run_for(5)), 1)

    def test_another_project_in_the_editor_is_drifting(self):
        self.g.start("the watcher")
        self.front = OTHER_PROJECT
        self.assertEqual(self.g.classify()[0], "off")
        self.front = ("jarvis-v0 at main · IamInsaf/jarvis-v0 - GitHub - Google Chrome", "chrome")
        self.assertEqual(self.g.classify()[0], "on")

    def test_claude_code_terminal_counts_while_its_session_is_on_the_project(self):
        self.g.start("the watcher")
        self.front = TERMINAL
        self.assertEqual(self.g.classify()[0], "off")  # no sessions at all
        self.session("C--Users-me-code-jarvis-v0", cwd=r"C:\Users\me\code\jarvis-v0", age=60)
        self.assertEqual(self.g.classify()[0], "on")
        self.session("C--Users-me-code-sweet-crumbs", cwd=r"C:\Users\me\code\sweet-crumbs", age=10)
        self.assertEqual(self.g.classify()[0], "off")  # the newest session is another project

    def test_stale_claude_session_and_encoded_directory_names(self):
        self.g.start("the watcher")
        self.front = TERMINAL
        self.session("C--Users-me-code-jarvis-v0", age=3600)  # no cwd in it, and an hour old
        self.assertEqual(self.g.classify()[0], "off")
        self.session("C--Users-me-code-jarvis-v0", age=30, name="s2.jsonl")
        self.assertEqual(self.g.classify()[0], "on")  # the directory name still says which project

    def test_without_a_project_any_editor_or_the_task_words_count(self):
        self.front = ("Untitled - Notepad", "notepad")
        self.g.start("the quarterly report")
        self.assertEqual(self.g.s["project"], "")
        self.assertEqual(self.g.classify()[0], "off")
        self.front = ("Quarterly report draft - Word", "WINWORD")
        self.assertEqual(self.g.classify()[0], "on")
        self.front = OTHER_PROJECT
        self.assertEqual(self.g.classify()[0], "on")

    def test_neutral_windows_dont_count_either_way(self):
        self.g.start("the watcher")
        self.front = ("", "")
        self.assertEqual(self.g.classify()[0], "neutral")
        self.front = ("Task Switching", "explorer")
        self.assertEqual(self.g.classify()[0], "neutral")

    # --- the project ---

    def test_project_from_the_request_or_the_window(self):
        self.front = REDDIT
        self.assertIn("I'll keep an eye on sweet-crumbs", self.g.start("the menu page in sweet-crumbs"))
        self.assertEqual(self.g.s["task"], "the menu page")
        self.g.start("the API for project atlas")
        self.assertEqual((self.g.s["task"], self.g.s["project"]), ("the API", "atlas"))
        for title in ("● guardian.py - jarvis-v0 - Visual Studio Code [Administrator]",
                      "jarvis-v0 - Cursor", "app.py - jarvis-v0 (Workspace) - Visual Studio Code"):
            self.front = (title, "Code")
            self.assertEqual(self.g.detect_project(), "jarvis-v0", title)
        self.front = ("Welcome - Visual Studio Code", "Code")
        self.assertEqual(self.g.detect_project(), "")
        self.front = TERMINAL
        self.session("C--Users-me-code-jarvis-v0", cwd="/home/me/jarvis-v0", age=5)
        self.assertEqual(self.g.detect_project(), "jarvis-v0")

    # --- snooze, break, park, done ---

    def test_snooze_holds_reminders_then_watching_resumes(self):
        self.g.start("the watcher")
        self.front = YOUTUBE
        self.assertIn("20 minutes", self.g.snooze("20 minutes"))
        self.assertEqual(self.run_for(19), [])
        self.assertEqual(self.run_for(5.5), [])  # snooze over at 20; the drift clock starts from then
        self.assertEqual(len(self.run_for(1)), 1)

    def test_break_calls_you_back_with_the_breadcrumb(self):
        self.g.start("the watcher")
        self.g.next_step("the idle check")
        self.assertIn("in 15 minutes", self.g.take_break(""))  # the default length
        self.front = YOUTUBE
        said = self.run_for(15.5)
        self.assertEqual(said, ["Break's over, sir. Back to the watcher. Next step: the idle check."])

    def test_park_and_resume(self):
        self.g.start("the watcher")
        self.g.next_step("the idle check")
        self.assertIn("Next step when you return: the idle check", self.g.park())
        self.front = YOUTUBE
        self.assertEqual(self.run_for(40), [])
        self.assertIn("parked", self.g.status())
        self.assertIn("nothing to snooze", self.g.snooze())
        self.assertEqual(self.g.resume(), "Back to the watcher, sir. Next step: the idle check.")
        self.assertEqual(len(self.run_for(5)), 1)

    def test_switch_done_and_the_days_log(self):
        self.g.start("the watcher")
        self.run_for(30)
        self.front = YOUTUBE
        self.run_for(16)
        self.front = VSCODE
        self.assertIn("Switched from the watcher to the tests", self.g.start("the tests"))
        self.run_for(20)
        self.assertIn("That's the tests finished, after 20 minutes of focus", self.g.done())
        self.g.start("the README")
        self.run_for(10)
        log = self.g.log()
        self.assertIn("Today you've focused for 1 hour", log)
        self.assertIn("the watcher, 30 minutes;", log)
        self.assertIn("the tests, 20 minutes, done;", log)
        self.assertIn("the README, 10 minutes, in progress", log)
        self.assertIn("You drifted for 16 minutes, with 2 reminders.", log)
        self.assertIn("Focus mode off", self.g.stop())
        self.assertEqual(self.g.status()[:31], "No focus session is running, si")

    def test_nothing_running(self):
        self.assertEqual(self.g.log(), "No focus sessions today yet, sir.")
        self.assertIn("Tell me what you're working on first", self.g.next_step("x"))
        self.assertEqual(self.g.start(""), "What are we focusing on, sir?")
        self.assertIn("already off", self.g.stop())
        self.g.tick()  # no session: nothing to do

    def test_a_session_survives_a_restart_without_counting_the_downtime(self):
        self.g.start("the watcher")
        self.g.next_step("the idle check")
        self.run_for(10)
        self.g.db.close()
        self.clock.t += 3 * 3600  # JARVIS was off
        self.g = self.guardian()
        self.assertIn("You're working on the watcher in jarvis-v0, sir: 10 minutes focused so far. "
                      "Next step: the idle check.", self.g.status())
        self.front = YOUTUBE
        self.assertEqual(len(self.run_for(5)), 1)


class ParseTest(unittest.TestCase):
    def test_commands(self):
        cases = {
            "Hey Jarvis, focus on the login page": ("focus_start", {"task": "the login page"}),
            "I'm working on the quarterly report now": ("focus_start", {"task": "the quarterly report"}),
            "my main task today is the watcher in jarvis-v0": ("focus_start", {"task": "the watcher in jarvis-v0"}),
            "switch my focus to the tests": ("focus_start", {"task": "the tests"}),
            "start focus mode": ("focus_start", {"task": ""}),
            "the next step is wiring the idle check": ("focus_next_step", {"step": "wiring the idle check"}),
            "breadcrumb: write the README": ("focus_next_step", {"step": "write the README"}),
            "where was I?": ("focus_status", {}),
            "what am I working on": ("focus_status", {}),
            "snooze for 20 minutes": ("focus_snooze", {"minutes": "20 minutes"}),
            "snooze": ("focus_snooze", {"minutes": ""}),
            "I'm going to take a break for half an hour": ("focus_break", {"minutes": "half an hour"}),
            "let's take a break": ("focus_break", {"minutes": ""}),
            "park this for now": ("focus_park", {}),
            "back to work": ("focus_resume", {}),
            "I'm back from my break": ("focus_resume", {}),
            "I'm done with this task": ("focus_done", {}),
            "task complete": ("focus_done", {}),
            "stop focus mode please": ("focus_stop", {}),
            "stop nagging me": ("focus_stop", {}),
            "how did I do today?": ("focus_log", {}),
            "focus report": ("focus_log", {}),
        }
        for said, want in cases.items():
            self.assertEqual(focus.parse(said), want, said)

    def test_not_focus(self):
        for said in ("switch to gemma", "add a task to buy milk", "I'm working on it", "set a timer for 5 minutes",
                     "what's the time", "switch to chrome", "I'm done", "new task: buy milk", "pause the music"):
            self.assertIsNone(focus.parse(said), said)

    def test_rules_take_it_before_the_brain_switch(self):
        self.assertEqual(rules.fast_path("switch my focus to the tests")[:2], ("focus_start", {"task": "the tests"}))
        with mock.patch.object(config, "FOCUS_ENABLED", False):
            self.assertIsNone(rules.fast_path("focus on the tests"))

    def test_router_sends_focus_task_talk_to_focus_not_the_todo_list(self):
        with mock.patch.object(intents, "_pick", side_effect=AssertionError("no LLM needed")):
            self.assertEqual(intents.route("I've finished the task I was focusing on", "", {}), "focus")
            self.assertEqual(intents.route("add a task to buy milk", "", {}), "tasks")


if __name__ == "__main__":
    unittest.main()
