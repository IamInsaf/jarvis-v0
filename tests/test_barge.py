"""Talking over JARVIS: its own echo must not stop it, the user's voice must; "stop" and echoes get no reply.
Run: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import ears  # noqa: E402
import jarvis  # noqa: E402
import vad  # noqa: E402
from test_vad import ROOT, frames, spoken  # noqa: E402


def rms(a):
    return float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))


@unittest.skipUnless(vad._model_path() and (ROOT / config.PIPER_VOICE).exists(), "needs Silero and the Piper voice")
class BargeInTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jarvis_voice = frames(spoken("Certainly, sir. The forecast for tomorrow is mild, with light rain "
                                         "in the afternoon and clearing skies by the evening."))
        cls.user_voice = frames(spoken("No, wait, I meant the weather in London please."))

    def setUp(self):
        self.clock = [1000.0]
        p = mock.patch.object(ears.time, "time", lambda: self.clock[0])
        p.start()
        self.addCleanup(p.stop)

    def feed_reply(self, mic_frames, out_frames):
        """Play `out_frames` as JARVIS while the mic hears `mic_frames`; True if it barged in."""
        level = [0.0]
        barge = ears.BargeIn(lambda: level[0])
        detector = vad.SpeechDetector()
        for mic, out in zip(mic_frames, out_frames):
            level[0] = rms(out)
            if barge.feed(mic, detector):
                return True
            self.clock[0] += ears.FRAME / ears.RATE
        return False

    def test_own_echo_does_not_interrupt(self):
        echo = [(f * 0.35).astype(np.int16) for f in self.jarvis_voice]  # speakers heard by the mic
        self.assertFalse(self.feed_reply(echo, self.jarvis_voice))

    def test_user_talking_over_it_interrupts(self):
        n = len(self.jarvis_voice)
        quiet_start = 12  # ~1 s of reply first, so the echo has been learned
        user = [np.zeros(ears.FRAME, dtype=np.int16)] * quiet_start + self.user_voice
        user += [np.zeros(ears.FRAME, dtype=np.int16)] * max(0, n - len(user))
        mic = [np.clip(j * 0.35 + u * 1.0, -32768, 32767).astype(np.int16)
               for j, u in zip(self.jarvis_voice, user[:n])]
        self.assertTrue(self.feed_reply(mic, self.jarvis_voice))

    def test_talking_while_it_thinks_interrupts(self):
        silence = [np.zeros(ears.FRAME, dtype=np.int16)] * len(self.user_voice)
        self.assertTrue(self.feed_reply(self.user_voice, silence))

    def test_keeps_the_interrupting_words(self):
        barge = ears.BargeIn(lambda: 0.0)
        detector = vad.SpeechDetector()
        for f in self.user_voice:
            if barge.feed(f, detector):
                break
            self.clock[0] += ears.FRAME / ears.RATE
        self.assertGreaterEqual(len(barge.recent), config.BARGE_IN_MS / 80)


class ReplyRulesTest(unittest.TestCase):
    def test_stop_phrases(self):
        for t in ["stop", "okay stop", "quiet please", "enough, thanks", "that's enough", "hold on", "shh",
                  "jarvis stop talking"]:
            self.assertTrue(jarvis.STOP.fullmatch(t), t)
        for t in ["stop the timer", "stop the music", "what's the weather"]:
            self.assertFalse(jarvis.STOP.fullmatch(t), t)

    def test_echo_of_own_reply(self):
        last = "The forecast for tomorrow is mild, with light rain in the afternoon."
        self.assertTrue(jarvis.is_echo("light rain in the afternoon", last))
        self.assertFalse(jarvis.is_echo("what about rain in London", last))
        self.assertFalse(jarvis.is_echo("rain", last))  # too short to tell


if __name__ == "__main__":
    unittest.main()
