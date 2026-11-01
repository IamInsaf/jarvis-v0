"""Hold to talk: the mic is open only while WAKE_HOTKEY (Ctrl+Shift) is held, and "activate voice mode" keeps
it on for the wake word. No mic, keyboard or models. Run: python -m unittest discover tests"""
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ears  # noqa: E402
import fingers  # noqa: E402
import jarvis  # noqa: E402

CTRL, SHIFT = 0x11, 0x10
FRAME = np.zeros((ears.FRAME, 1), dtype=np.int16)


def hold(script, others=(), seconds_per_poll=0.1):
    """Run watch_chord once over `script` (the set of keys down at each poll); returns the calls it made."""
    steps, now, calls, ticks = iter(script), [set()], [], [0]
    polls = iter(range(10_000))

    def advance(_):  # one poll: the next keys, and the clock moves on
        now[0] = next(steps, set())
        ticks[0] += 1

    with mock.patch.object(fingers.time, "sleep", advance), \
            mock.patch.object(fingers.time, "monotonic", lambda: ticks[0] * seconds_per_poll):
        advance(None)
        fingers.watch_chord([CTRL, SHIFT], lambda: calls.append("down"), lambda c: calls.append(("up", c)),
                            is_down=lambda vk: vk in now[0], others_down=lambda: next(polls) in others,
                            once=True)
    return calls


class ChordTest(unittest.TestCase):
    def test_hold_then_let_go(self):
        self.assertEqual(hold([{CTRL}, {CTRL, SHIFT}] + [{CTRL, SHIFT}] * 10 + [{SHIFT}, set()]),
                         ["down", ("up", False)])

    def test_letting_go_of_either_key_ends_it(self):
        self.assertEqual(hold([{CTRL, SHIFT}] * 10 + [{CTRL}] * 5 + [set()]), ["down", ("up", False)])

    def test_another_key_while_held_is_a_shortcut(self):  # Ctrl+Shift+T
        self.assertEqual(hold([{CTRL, SHIFT}] * 10 + [set()], others={3}), ["down", ("up", True)])

    def test_a_key_already_down_never_starts_it(self):
        self.assertEqual(hold([{CTRL, SHIFT}] * 3 + [set()], others={0}), [])

    def test_a_tap_is_cancelled(self):
        self.assertEqual(hold([{CTRL, SHIFT}, set()], seconds_per_poll=0.01), ["down", ("up", True)])

    def test_chord_keys_include_both_sides(self):
        keys = fingers._chord_keys(fingers.parse_hotkey("ctrl+shift"))
        self.assertTrue({0x11, 0xA2, 0xA3, 0x10, 0xA0, 0xA1} <= keys)
        self.assertNotIn(0x54, keys)  # T

    def test_typo_names_the_setting(self):
        with self.assertRaisesRegex(ValueError, "WAKE_HOTKEY"):
            fingers.parse_hotkey("ctrl+fn", "WAKE_HOTKEY")


class FakeStream:
    def __init__(self, **kw):
        self.started = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True


def hold_ears(transcript="what time is it"):
    e = ears.Ears.__new__(ears.Ears)
    e.q, e.tap, e.mic_lock = queue.Queue(), None, threading.Lock()
    e.ptt_held, e.ptt_audio = threading.Event(), queue.Queue()
    e.standby = True
    e.wake = type("Wake", (), {"predict": lambda self, f: {"hey_jarvis": 0.0}, "reset": lambda self: None})()
    e._transcribe = lambda audio: (transcript, audio)
    return e


def talk(e, frames=3, cancelled=False):
    """Hold the hotkey, say something, let go."""
    e.ptt_down()
    for _ in range(frames):
        e._on_audio(FRAME, ears.FRAME, None, None)
    e.ptt_up(cancelled)


@mock.patch.object(ears.sd, "InputStream", FakeStream)
class HoldToTalkTest(unittest.TestCase):
    def test_starts_with_the_mic_off(self):
        self.assertFalse(hold_ears().mic_on)

    def test_mic_is_on_only_while_held(self):
        e = hold_ears()
        e.ptt_down()
        self.assertTrue(e.mic_on and e.stream.started)
        stream = e.stream
        e.ptt_up(False)
        self.assertFalse(e.mic_on)
        self.assertTrue(stream.closed)

    def test_what_was_said_comes_back(self):
        e = hold_ears()
        talk(e)
        heard = e.wait_for_attention(None)
        self.assertEqual((heard.how, heard.text), ("hotkey", "what time is it"))
        self.assertEqual(len(heard.audio), 3 * ears.FRAME)

    def test_held_audio_skips_the_wake_word(self):
        e = hold_ears()
        e.ptt_down()
        e._on_audio(FRAME, ears.FRAME, None, None)
        self.assertTrue(e.q.empty())  # the wake detector never hears it
        e.ptt_up(False)

    def test_shortcut_or_tap_is_dropped(self):
        e = hold_ears()
        talk(e, cancelled=True)
        self.assertTrue(e.ptt_audio.empty())
        self.assertFalse(e.mic_on)

    def test_typing_works_with_the_mic_off(self):
        self.assertEqual(hold_ears().wait_for_attention(None, typed=lambda: True).how, "typed")

    def test_holding_it_stops_a_reply(self):
        e = hold_ears()
        e.ptt_held.set()
        self.assertEqual(e.watch_for_interrupt(lambda: False), "hotkey")
        self.assertEqual(e.heard_interrupt(), "hotkey")

    def test_nothing_to_record_with_the_mic_off(self):
        self.assertIsNone(hold_ears()._record(wait=30))

    def test_yes_with_the_hotkey(self):
        e = hold_ears("yes please")
        threading.Timer(0.2, talk, (e,)).start()
        self.assertEqual(e.wait_ptt(5)[0], "yes please")

    def test_no_answer_times_out(self):
        self.assertEqual(hold_ears().wait_ptt(0.2), ("", None))

    def test_dictation_needs_voice_mode(self):
        e = hold_ears()
        e.transcribe = lambda audio: self.fail("nothing should be transcribed with the mic off")
        keys = iter([True, True, False])
        with mock.patch.object(fingers.time, "sleep", lambda s: None):
            fingers.hold_to_talk(e, [0xA3], is_down=lambda vk: next(keys, False), typer=self.fail, once=True)
        self.assertIsNone(e.tap)


@mock.patch.object(ears.sd, "InputStream", FakeStream)
class VoiceModeTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, jarvis, "standby", jarvis.standby)
        self.addCleanup(setattr, jarvis, "voice_mode", jarvis.voice_mode)
        jarvis.standby = True

    def test_mic_stays_on_after_talking(self):
        e = hold_ears()
        jarvis.set_voice_mode(e, True)
        talk(e)
        self.assertTrue(e.mic_on)
        self.assertEqual(e.wait_for_attention(None).how, "hotkey")  # the hotkey still works in voice mode

    def test_switching_back_closes_the_mic(self):
        e = hold_ears()
        self.assertIn("Voice mode on", jarvis.voice_mode_switch(e, True))
        self.assertTrue(e.mic_on)
        self.assertIn("Hold Ctrl+Shift", jarvis.voice_mode_switch(e, False))
        self.assertFalse(e.mic_on)

    def test_hints(self):
        e = hold_ears()
        jarvis.set_voice_mode(e, False)
        self.assertEqual(jarvis.wake_hint(), "hold Ctrl+Shift and talk")
        jarvis.set_voice_mode(e, True)
        self.assertEqual(jarvis.wake_hint(), 'say "hey Jarvis"')

    def test_without_the_hotkey_it_is_always_voice_mode(self):
        jarvis.standby = False
        self.assertIn("always on", jarvis.voice_mode_switch(hold_ears(), False))

    def test_phrases(self):
        for said, on in [("activate voice mode", True), ("jarvis, activate voice mode please", True),
                         ("voice mode on", True), ("switch to voice mode", True), ("voice mode", True),
                         ("deactivate voice mode", False), ("voice mode off", False), ("push to talk", False),
                         ("go back to push to talk mode", False), ("stop voice mode", False),
                         ("what is voice mode", None), ("open voice memos", None)]:
            self.assertEqual(jarvis.voice_mode_command(said), on, said)


if __name__ == "__main__":
    unittest.main()
