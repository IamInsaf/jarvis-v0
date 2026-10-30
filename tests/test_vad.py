"""When someone is speaking (Silero VAD) and when they've finished. Run: python -m unittest discover tests"""
import queue
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import ears  # noqa: E402
import vad  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def spoken(text: str) -> np.ndarray:
    """`text` read out by the Piper voice, resampled to the mic's 16 kHz."""
    from piper import PiperVoice
    voice = PiperVoice.load(str(ROOT / config.PIPER_VOICE))
    pcm = np.frombuffer(b"".join(c.audio_int16_bytes for c in voice.synthesize(text)), dtype=np.int16)
    rate = voice.config.sample_rate
    idx = np.arange(0, len(pcm), rate / ears.RATE).astype(int)
    return pcm[idx[idx < len(pcm)]]


def frames(audio: np.ndarray) -> list[np.ndarray]:
    return [audio[i:i + ears.FRAME] for i in range(0, len(audio) - ears.FRAME + 1, ears.FRAME)]


@unittest.skipUnless(vad._model_path() and (ROOT / config.PIPER_VOICE).exists(), "needs Silero and the Piper voice")
class SileroTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.speech = spoken("Remind me to call my mother tomorrow morning at nine.")

    def test_speech_is_speech(self):
        d = vad.SpeechDetector()
        self.assertIsNotNone(d.session)
        hits = sum(d.is_speech(f) for f in frames(self.speech))
        self.assertGreater(hits, len(frames(self.speech)) // 2)

    def test_loud_noise_is_not_speech(self):
        d = vad.SpeechDetector()
        rng = np.random.default_rng(0)
        noise = (rng.normal(0, 3000, ears.RATE * 2)).astype(np.int16)  # far above SILENCE_RMS
        self.assertFalse(any(d.is_speech(f) for f in frames(noise)))

    def test_recording_ends_after_the_speech(self):
        e = ears.Ears.__new__(ears.Ears)
        e.q, e.vad = queue.Queue(), vad.SpeechDetector()
        silence = np.zeros(int(ears.RATE * 3), dtype=np.int16)
        for f in frames(np.concatenate([silence[:ears.RATE // 2], self.speech, silence])):
            e.q.put(f)
        audio = e._record(wait=2)
        self.assertIsNotNone(audio)
        seconds = len(audio) / ears.RATE
        speech_end = 0.5 + len(self.speech) / ears.RATE
        self.assertGreater(seconds, speech_end - 0.1)  # didn't cut the end off
        self.assertLess(seconds, speech_end + config.SILENCE_SECONDS + 0.5)  # stopped soon after


class StopEarlyTest(unittest.TestCase):
    def test_stop_ends_the_recording(self):
        e = ears.Ears.__new__(ears.Ears)
        e.q, e.vad = queue.Queue(), vad.SpeechDetector(loudness_only=True)
        for _ in range(50):
            e.q.put(np.zeros(ears.FRAME, dtype=np.int16))
        calls = []
        self.assertIsNone(e._record(wait=30, stop=lambda: calls.append(1) or len(calls) > 3))
        self.assertEqual(len(calls), 4)  # gave up at once, didn't wait out the 30 seconds


class LoudnessFallbackTest(unittest.TestCase):
    def test_loudness_only(self):
        d = vad.SpeechDetector(loudness_only=True)
        self.assertIsNone(d.session)
        self.assertTrue(d.is_speech(np.full(ears.FRAME, 4000, dtype=np.int16)))
        self.assertFalse(d.is_speech(np.zeros(ears.FRAME, dtype=np.int16)))


class UnfinishedTest(unittest.TestCase):
    def test_trailing_off(self):
        for text in ["Remind me to call mum and", "Set a timer for, um", "Open the", "Send a message to",
                     "Add milk, eggs,", "What's the weather like in"]:
            self.assertTrue(ears.sounds_unfinished(text), text)

    def test_finished(self):
        for text in ["Remind me to call mum.", "What time is it?", "Open Spotify", "Thanks", "", "And?",
                     "Turn the lights on"]:
            self.assertFalse(ears.sounds_unfinished(text), text)

    def test_listens_on_and_joins_the_rest(self):
        e = ears.Ears.__new__(ears.Ears)
        more = np.ones(ears.FRAME, dtype=np.int16)
        e._record = lambda wait: more
        heard = []
        e._transcribe = lambda audio: (heard.append(len(audio)) or "Remind me to call mum and dad.", audio)
        text, audio = e._finish_thought("Remind me to call mum and", np.zeros(ears.FRAME, dtype=np.float32))
        self.assertEqual(text, "Remind me to call mum and dad.")
        self.assertEqual(heard, [2 * ears.FRAME])

    def test_gives_up_when_nothing_follows(self):
        e = ears.Ears.__new__(ears.Ears)
        e._record = lambda wait: None
        audio = np.zeros(ears.FRAME, dtype=np.float32)
        self.assertEqual(e._finish_thought("Open the", audio), ("Open the", audio))


if __name__ == "__main__":
    unittest.main()
