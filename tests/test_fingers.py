"""Type mode logic, no mic, Whisper or real typing. Run: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ears  # noqa: E402
import fingers  # noqa: E402
from jarvis import TYPE_START  # noqa: E402


class CleanTest(unittest.TestCase):
    def test_phrase_gets_a_trailing_space(self):
        self.assertEqual(fingers.clean(" Hello there. "), "Hello there. ")

    def test_new_line_and_paragraph(self):
        self.assertEqual(fingers.clean("Hi Sam. New line. See you soon."), "Hi Sam.\nSee you soon. ")
        self.assertEqual(fingers.clean("Thanks, new paragraph, Bye now"), "Thanks\n\nBye now ")
        self.assertEqual(fingers.clean("New line."), "\n")

    def test_whisper_ghosts_are_dropped(self):
        for ghost in ["you", "Thanks for watching!", " ", "Um."]:
            self.assertEqual(fingers.clean(ghost), "", ghost)


class EndPhraseTest(unittest.TestCase):
    def test_ends_and_keeps_what_came_before(self):
        for text, rest in [("Type mode done.", ""), ("See you at six. Type mode done.", "See you at six."),
                           ("tight mode, stop", ""), ("and that's it, Jarvis, type mode done", "and that's it"),
                           ("Stop typing.", ""), ("end type-mode", ""),
                           # what Whisper actually wrote in a real session
                           ("Type mode finish", ""), ("type mod stop", ""), ("Type mode store", "")]:
            self.assertEqual(fingers.split_end(text), (rest, True), text)

    def test_only_at_the_end(self):
        for text in ["I'll stop typing tomorrow and write by hand", "type mode is great"]:
            self.assertEqual(fingers.split_end(text), (text, False), text)


class StartPhraseTest(unittest.TestCase):
    def test_starts(self):
        for said in ["type mode start", "type mode", "start type mode", "type mode, start", "typing mode on",
                     "start typing", "dictation mode", "type-mode start"]:
            self.assertTrue(TYPE_START.fullmatch(said), said)

    def test_not_other_requests(self):
        for said in ["type a message to sam", "what is type mode", "type mode done"]:
            self.assertFalse(TYPE_START.fullmatch(said), said)


class DictateTest(unittest.TestCase):
    def test_types_each_phrase_until_done(self):
        said = iter(["Dear Sam,", "", "thanks for lunch. New line.", "Type mode done."])
        typed = []
        how = fingers.dictate(lambda wait: (next(said), None), typed.append, idle=60)
        self.assertEqual((how, typed), ("done", ["Dear Sam, ", "thanks for lunch.\n"]))

    def test_silence_ends_it(self):
        typed = []
        self.assertEqual(fingers.dictate(lambda wait: ("", None), typed.append, idle=0), "idle")
        self.assertEqual(typed, [])


class HotkeyTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(fingers.parse_hotkey("Right Ctrl"), [0xA3])
        self.assertEqual(fingers.parse_hotkey("ctrl+win"), [0x11, 0x5B])
        self.assertEqual(fingers.parse_hotkey("f8"), [0x77])
        self.assertEqual(fingers.parse_hotkey("off"), [])
        with self.assertRaises(ValueError):
            fingers.parse_hotkey("right control")

    def hold(self, seconds, transcript="Hello world."):
        """Press the hotkey, speak for `seconds`, let go; returns what got typed."""
        e = ears.Ears.__new__(ears.Ears)
        e.tap = None
        e.transcribe = lambda audio: (transcript, audio)
        clock = iter([False, True] + [True] * int(seconds / fingers._POLL) + [False] * 10)

        def is_down(vk):
            down = next(clock)
            if down and e.tap is not None:
                e._on_audio(np.zeros((ears.FRAME, 1), dtype=np.int16), ears.FRAME, None, None)
            return down

        typed = []
        real_sleep, fingers.time.sleep = fingers.time.sleep, lambda s: None
        real_clock, t = fingers.time.monotonic, iter(range(0, 10_000))
        fingers.time.monotonic = lambda: next(t) * (seconds or 0.01)  # press -> release = `seconds`
        try:
            fingers.hold_to_talk(e, [0xA3], is_down=is_down, typer=typed.append, once=True)
        finally:
            fingers.time.sleep, fingers.time.monotonic = real_sleep, real_clock
        return typed, e

    def test_hold_types_what_was_said(self):
        typed, e = self.hold(1.0)
        self.assertEqual(typed, ["Hello world. "])
        self.assertIsNone(e.tap)  # the mic goes back to the wake word

    def test_quick_tap_is_just_a_keypress(self):
        typed, _ = self.hold(0.1)
        self.assertEqual(typed, [])


class TapTest(unittest.TestCase):
    def test_tap_takes_the_mic_from_the_wake_word(self):
        import queue
        e = ears.Ears.__new__(ears.Ears)
        e.q, e.tap = queue.Queue(), None
        frame = np.ones((ears.FRAME, 1), dtype=np.int16)
        e.start_tap()
        e._on_audio(frame, ears.FRAME, None, None)
        e._on_audio(frame, ears.FRAME, None, None)
        self.assertEqual(len(e.stop_tap()), 2 * ears.FRAME)
        self.assertTrue(e.q.empty())
        e._on_audio(frame, ears.FRAME, None, None)
        self.assertEqual(e.q.qsize(), 1)


class PasteTest(unittest.TestCase):
    def test_ctrl_v(self):
        self.assertEqual(fingers.paste_keys(), [(0x11, 0, 0), (0x56, 0, 0), (0x56, 0, 2), (0x11, 0, 2)])

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_input_struct_is_the_size_windows_expects(self):
        import ctypes
        arr = fingers._input_array(fingers.paste_keys())
        self.assertEqual(ctypes.sizeof(arr[0]), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_clipboard_round_trip(self):
        """Uses the real clipboard, and puts back whatever text was on it."""
        before = fingers.get_clipboard()
        try:
            fingers.set_clipboard("Hi Sam.\r\nSee you 😀")
            self.assertEqual(fingers.get_clipboard(), "Hi Sam.\r\nSee you 😀")
        finally:
            if before is not None:
                fingers.set_clipboard(before)


if __name__ == "__main__":
    unittest.main()
