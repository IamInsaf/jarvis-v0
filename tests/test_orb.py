"""What the desktop orb shows for each HUD event, without opening a window. Run: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orb  # noqa: E402
from orb import OrbModel  # noqa: E402


def settle(m, now, seconds=3.0):
    """Run the animation long enough for colours and levels to reach their targets."""
    view = None
    for i in range(int(seconds / .05)):
        view = m.tick(.05, now + i * .05)
    return view


class OrbModelTest(unittest.TestCase):
    def test_colour_follows_state(self):
        m = OrbModel()
        self.assertEqual(orb.hexrgb(settle(m, 0)["rgb"]), orb.hexrgb(orb.STATES["offline"][0]))
        m.feed({"kind": "state", "state": "thinking"}, 10)
        self.assertEqual(orb.hexrgb(settle(m, 10)["rgb"]), orb.hexrgb(orb.STATES["thinking"][0]))
        m.feed({"kind": "state", "state": "nonsense"}, 20)
        self.assertEqual(m.state, "idle")

    def test_clauses_build_one_caption(self):
        m = OrbModel()
        m.feed({"kind": "state", "state": "speaking"}, 0)
        m.feed({"kind": "say", "text": "Good evening, sir."}, 0)
        m.feed({"kind": "say", "text": "All is well."}, 1)
        self.assertEqual(m.tick(.03, 1)["caption"], "Good evening, sir. All is well.")
        m.feed({"kind": "heard", "text": "thanks"}, 2)
        self.assertEqual(m.tick(.03, 2)["caption"], "“thanks”")

    def test_long_caption_keeps_its_end(self):
        m = OrbModel()
        for i in range(40):
            m.feed({"kind": "say", "text": f"clause {i}."}, 0)
        cap = m.tick(.03, 0)["caption"]
        self.assertLessEqual(len(cap), 220)
        self.assertTrue(cap.startswith("…") and cap.endswith("clause 39."))

    def test_caption_fades_after_going_quiet(self):
        m = OrbModel()
        m.feed({"kind": "state", "state": "speaking"}, 0)
        m.feed({"kind": "say", "text": "Done."}, 0)
        m.feed({"kind": "state", "state": "idle"}, 1)
        self.assertEqual(m.tick(.03, 2)["caption"], "Done.")
        self.assertEqual(m.tick(.03, 1 + orb.CAPTION_HOLD + 1)["caption"], "")

    def test_idle_dims_or_hides(self):
        for mode, key in (("dim", "dim"), ("hide", "hidden")):
            m = OrbModel(idle=mode)
            m.feed({"kind": "state", "state": "idle"}, 0)
            self.assertFalse(m.tick(.03, 1)[key])
            self.assertTrue(m.tick(.03, orb.IDLE_AFTER + orb.CAPTION_HOLD + 1)[key])
        m = OrbModel(idle="show")
        view = m.tick(.03, 100)
        self.assertFalse(view["dim"] or view["hidden"])

    def test_blocked_action_is_shown(self):
        m = OrbModel()
        m.feed({"kind": "gate", "tool": "run_shell", "tier": "blocked", "why": "deleting folders recursively"}, 0)
        view = m.tick(.03, 0)
        self.assertIn("Blocked run_shell", view["caption"])
        self.assertEqual(view["caption_kind"], "gate")

    def test_question_stays_until_answered(self):
        m = OrbModel()
        m.feed({"kind": "state", "state": "idle"}, 0)
        m.feed({"kind": "ask", "ask": 7, "question": "Shall I run this?"}, 0)
        view = settle(m, 0, 1)
        self.assertEqual(view["ask"], (7, "Shall I run this?"))
        self.assertEqual(view["caption_kind"], "ask")
        self.assertEqual(m.shown_state(), "asking")
        m.feed({"kind": "say", "text": "Shall I run this?"}, 1)  # read out...
        m.feed({"kind": "state", "state": "listening"}, 2)    # ...then waiting for the answer
        self.assertEqual(m.tick(.03, 500)["caption"], "Shall I run this?")  # long after: still there
        m.feed({"kind": "answered", "ask": 6, "how": "click", "yes": True}, 501)  # someone else's question
        self.assertIsNotNone(m.ask)
        m.feed({"kind": "answered", "ask": 7, "how": "click", "yes": True}, 502)
        self.assertIsNone(m.ask)
        self.assertEqual(m.tick(.03, 502)["caption"], "✓ Yes")

    def test_dropped_stream_goes_offline(self):
        m = OrbModel()
        m.feed({"kind": "state", "state": "speaking"}, 0)
        m.feed({"kind": "ask", "ask": 1, "question": "?"}, 0)
        m.feed({"kind": "_offline"}, 1)
        view = m.tick(.03, 1)
        self.assertTrue(view["offline"])
        self.assertIsNone(view["ask"])
        self.assertEqual(view["caption"], "")

    def test_typed_request_is_shown(self):
        m = OrbModel()
        m.feed({"kind": "typed", "text": "what time is it"}, 0)
        view = m.tick(.03, 0)
        self.assertEqual((view["caption"], view["caption_kind"]), ("⌨ what time is it", "heard"))
        m.feed({"kind": "_typed_failed"}, 1)
        self.assertIn("Couldn't reach JARVIS", m.tick(.03, 1)["caption"])

    def test_pov_opens_and_closes(self):
        m = OrbModel()
        self.assertFalse(m.tick(.03, 0)["pov"])
        m.feed({"kind": "pov", "on": True}, 0)
        self.assertTrue(m.tick(.03, 0)["pov"])
        m.feed({"kind": "pov", "on": False}, 1)
        self.assertFalse(m.tick(.03, 1)["pov"])
        m.feed({"kind": "pov", "on": True}, 2)
        m.feed({"kind": "_offline"}, 3)  # JARVIS went away: nothing to show
        self.assertFalse(m.tick(.03, 3)["pov"])

    def test_pulse_follows_voice(self):
        m = OrbModel()
        m.feed({"kind": "state", "state": "speaking"}, 0)
        m.feed({"kind": "level", "mic": 0.0, "voice": 0.8}, 0)
        self.assertGreater(settle(m, 0, 1)["level"], .7)
        m.feed({"kind": "level", "mic": 0.0, "voice": 0.0}, 1)
        self.assertLess(settle(m, 1, 2)["level"], .05)


class ColourTest(unittest.TestCase):
    def test_mix_and_hex(self):
        self.assertEqual(orb.mix((0, 0, 0), (100, 200, 50), .5), (50, 100, 25))
        self.assertEqual(orb.mix((0, 0, 0), (100, 200, 50), 2), (100, 200, 50))
        self.assertEqual(orb.hexrgb((1, 2, 255)), "#0102ff")


if __name__ == "__main__":
    unittest.main()
