"""Mouth: local Piper text-to-speech."""
import re

import numpy as np
import sounddevice as sd
from piper import PiperVoice

import config


class Mouth:
    def __init__(self):
        self.voice = PiperVoice.load(config.PIPER_VOICE)
        self.rate = self.voice.config.sample_rate

    def _synth(self, text: str) -> bytes:
        if hasattr(self.voice, "synthesize_stream_raw"):          # piper-tts < 1.3
            return b"".join(self.voice.synthesize_stream_raw(text))
        return b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text))  # >= 1.3

    def say(self, text: str):
        print(f"JARVIS: {text}")
        clean = re.sub(r"[*_#`>]", "", text or "").strip()
        if not clean:
            return
        audio = np.frombuffer(self._synth(clean), dtype=np.int16)
        sd.play(audio, self.rate)
        sd.wait()

    def beep(self):
        rate = 22050
        t = np.linspace(0, 0.12, int(0.12 * rate), False)
        sd.play((0.2 * np.sin(2 * np.pi * 880 * t)).astype(np.float32), rate)
        sd.wait()
