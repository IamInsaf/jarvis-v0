"""Camera and attention logic, no webcam or mic needed. Run: python -m unittest discover tests"""
import queue
import sys
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import ears  # noqa: E402
import vad  # noqa: E402
from eyes import Debounce, Eyes, GestureFilter, MouthMotion, Sight  # noqa: E402

LOUD = np.full(ears.FRAME, 3000, dtype=np.int16)
QUIET = np.zeros(ears.FRAME, dtype=np.int16)


class DebounceTest(unittest.TestCase):
    def test_needs_to_hold_before_switching(self):
        d = Debounce(on=0.3, off=0.3)
        self.assertFalse(d.update(0.0, True))
        self.assertFalse(d.update(0.2, True))
        self.assertTrue(d.update(0.31, True))

    def test_a_blink_does_not_flicker_it(self):
        d = Debounce(on=0.3, off=0.3)
        d.update(0.0, True)
        d.update(0.4, True)
        self.assertTrue(d.update(0.5, False))   # looked away briefly
        self.assertTrue(d.update(0.6, True))    # and back
        self.assertTrue(d.update(1.0, False))
        self.assertFalse(d.update(1.31, False))


class MouthMotionTest(unittest.TestCase):
    def test_talking_moves_still_does_not(self):
        m = MouthMotion(0.5)
        still = [m.update(t / 10, 0.05) for t in range(10)][-1]
        talk = [m.update(1 + t / 10, 0.05 if t % 2 else 0.4) for t in range(10)][-1]
        self.assertLess(still, config.MOUTH_MOVING_MIN)
        self.assertGreater(talk, config.MOUTH_MOVING_MIN)

    def test_no_face_resets(self):
        m = MouthMotion(0.5)
        for t in range(5):
            m.update(t / 10, 0.4 if t % 2 else 0.0)
        self.assertEqual(m.update(0.6, None), 0.0)


class GestureFilterTest(unittest.TestCase):
    def feed(self, f, frames, start=0.0, fps=12):
        out = None
        for i, (name, score) in enumerate(frames):
            out = f.update(start + i / fps, name, score)
        return out

    def test_held_gesture_becomes_stable(self):
        self.assertEqual(self.feed(GestureFilter(0.4, 0.6), [("Victory", 0.9)] * 8), "Victory")

    def test_too_short_or_weak_does_not(self):
        self.assertIsNone(self.feed(GestureFilter(0.4, 0.6), [("Victory", 0.9)] * 3))
        self.assertIsNone(self.feed(GestureFilter(0.4, 0.6), [("Victory", 0.4)] * 8))

    def test_hand_passing_by_does_not(self):
        frames = [("None", 0.8), ("Victory", 0.7), ("None", 0.8), ("Closed_Fist", 0.7)] * 3
        self.assertIsNone(self.feed(GestureFilter(0.4, 0.6), frames))

    def test_a_dropped_frame_is_fine(self):
        frames = [("Victory", 0.9)] * 3 + [(None, 0.0)] + [("Victory", 0.9)] * 4
        self.assertEqual(self.feed(GestureFilter(0.4, 0.6), frames), "Victory")


def fake_eyes(sight: Sight, last_mouth: float = 0.0, seen: dict | None = None) -> Eyes:
    """An Eyes with no camera thread, showing `sight`."""
    e = Eyes.__new__(Eyes)
    import threading
    e._lock = threading.Lock()
    e._sight, e._last_mouth, e._gesture_seen = sight, last_mouth, seen or {}
    return e


class EyesTest(unittest.TestCase):
    def test_engaged_needs_one_facing_face_and_a_moving_mouth(self):
        now = time.time()
        self.assertTrue(fake_eyes(Sight(now, 1, True), last_mouth=now).engaged())
        self.assertFalse(fake_eyes(Sight(now, 1, False), last_mouth=now).engaged())   # looking away
        self.assertFalse(fake_eyes(Sight(now, 2, True), last_mouth=now).engaged())    # someone else in frame
        self.assertFalse(fake_eyes(Sight(now, 1, True), last_mouth=now - 2).engaged())  # mouth still
        self.assertFalse(fake_eyes(Sight(now - 5, 1, True), last_mouth=now).engaged())  # camera stalled

    def test_recent_gesture_ignores_old_ones_and_the_palm(self):
        now = time.time()
        e = fake_eyes(Sight(now), seen={"Victory": now - 0.5, "Closed_Fist": now - 3, "Open_Palm": now})
        self.assertEqual(e.recent_gesture(1.5), "Victory")
        self.assertIsNone(fake_eyes(Sight(now), seen={"Open_Palm": now}).recent_gesture(1.5))

    def test_palm_only_counts_if_raised_after_speech_started(self):
        now = time.time()
        e = fake_eyes(Sight(now, 1, True, False, "Open_Palm", gesture_since=now - 0.1))
        self.assertTrue(e.palm_since(now - 1))
        self.assertFalse(e.palm_since(now))  # was already up before this reply


class FakeEyes:
    def __init__(self, engaged=True, gesture=None):
        self._engaged, self._gesture = engaged, gesture

    def engaged(self, within=0.6):
        return self._engaged

    def recent_gesture(self, within):
        return self._gesture


def fake_ears(frames, transcripts):
    """Ears with a scripted mic and Whisper: no audio device or models."""
    e = ears.Ears.__new__(ears.Ears)
    e.q = queue.Queue()
    for f in frames:
        e.q.put(f)
    e.wake = type("Wake", (), {"predict": lambda self, f: {"hey_jarvis": 0.9 if f is WAKE else 0.0},
                               "reset": lambda self: None})()
    said = iter(transcripts)
    e.vad = vad.SpeechDetector(loudness_only=True)  # the scripted frames are loud or quiet, not real speech
    e.flush = lambda: None  # keep the scripted frames
    e._transcribe = lambda audio: (next(said), audio)
    return e


WAKE = np.zeros(ears.FRAME, dtype=np.int16)
SPEECH = [LOUD] * 4 + [QUIET] * 13  # a phrase, then enough silence to end the recording


class AttentionTest(unittest.TestCase):
    def test_wake_word_works_without_a_camera(self):
        self.assertEqual(fake_ears([QUIET, WAKE], []).wait_for_attention(None).how, "wake")

    def test_wake_word_works_on_camera_too(self):
        self.assertEqual(fake_ears([QUIET, WAKE], []).wait_for_attention(FakeEyes(engaged=False)).how, "wake")

    def test_name_on_camera_keeps_the_request(self):
        heard = fake_ears(SPEECH, ["Jarvis, open Spotify."]).wait_for_attention(FakeEyes())
        self.assertEqual((heard.how, heard.text), ("name", "open Spotify."))

    def test_just_the_name_asks_what(self):
        heard = fake_ears(SPEECH, ["Hey Jarvis."]).wait_for_attention(FakeEyes())
        self.assertEqual((heard.how, heard.text), ("name", ""))

    def test_gesture_needs_no_name(self):
        heard = fake_ears(SPEECH, ["open Spotify"]).wait_for_attention(FakeEyes(gesture="Victory"))
        self.assertEqual((heard.how, heard.gesture, heard.text), ("gesture", "Victory", "open Spotify"))

    def test_talking_on_camera_without_name_or_gesture_is_ignored(self):
        # a phone call, then "hey Jarvis": only the wake word gets through
        e = fake_ears(SPEECH + [WAKE], ["yeah I'll be there at six"])
        self.assertEqual(e.wait_for_attention(FakeEyes()).how, "wake")

    def test_talking_while_not_facing_is_never_transcribed(self):
        e = fake_ears(SPEECH + [WAKE], [])  # no transcripts: transcribing would raise StopIteration
        self.assertEqual(e.wait_for_attention(FakeEyes(engaged=False)).how, "wake")

    def test_preroll_keeps_the_first_words(self):
        e = fake_ears(SPEECH, ["Jarvis open Spotify"])
        got = []
        e._transcribe = lambda audio: (got.append(audio), ("Jarvis open Spotify", audio))[1]
        e.wait_for_attention(FakeEyes())
        self.assertGreaterEqual(int(np.sum(np.abs(got[0]) > 0)), ears.ONSET_FRAMES * ears.FRAME)


class NameTest(unittest.TestCase):
    def test_variants(self):
        for text, rest in [("Jarvis, open Spotify.", "open Spotify."), ("Hey Jarvis.", ""), ("OK, Jarvis what time is it",
                           "what time is it"), ("Javis set a timer", "set a timer")]:
            self.assertEqual(ears.without_name(text), rest, text)

    def test_not_at_the_start_or_part_of_a_word(self):
        for text in ["I told Jarvis about it", "Jarvisson is here", "open Spotify"]:
            self.assertIsNone(ears.without_name(text), text)


if __name__ == "__main__":
    unittest.main()


class EyesCommandTest(unittest.TestCase):
    def test_off(self):
        from jarvis import eyes_command
        for said in ["off the camera", "Off eye.", "of eye", "eyes off", "camera off please", "turn off the camera",
                     "Jarvis, turn the camera off.", "switch off your eyes", "close your eyes", "stop watching",
                     "off the ice", "turn of camera", "disable the webcam"]:
            self.assertIs(eyes_command(said), False, said)

    def test_on(self):
        from jarvis import eyes_command
        for said in ["on the camera", "eye on", "camera on", "turn on the camera", "Open your eyes.",
                     "start watching", "switch the camera on, please", "enable camera"]:
            self.assertIs(eyes_command(said), True, said)

    def test_other_things_are_left_alone(self):
        from jarvis import eyes_command
        for said in ["open camera", "close the camera", "what is the camera", "turn off the lights",
                     "i think so", "off we go", "take a photo with the camera"]:
            self.assertIsNone(eyes_command(said), said)

    def test_can_start_with_the_camera_closed(self):
        from unittest import mock
        with mock.patch("eyes.threading.Thread"):  # no real camera thread
            self.assertFalse(Eyes(watching=False).watching)
            self.assertTrue(Eyes().watching)
