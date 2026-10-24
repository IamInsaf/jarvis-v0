"""Mouth: local Piper text-to-speech."""
import re
import threading
import time

import numpy as np
import sounddevice as sd
from piper import PiperVoice

import config


class Mouth:
    def __init__(self, ears=None):
        self.voice = PiperVoice.load(config.PIPER_VOICE)
        self.rate = self.voice.config.sample_rate
        self.ears = ears  # if set, saying the wake word again interrupts playback
        # one output stream stays open for the whole session, playing silence between replies.
        # Opening a fresh stream per reply lets the audio device (Realtek jack, Bluetooth) fall
        # asleep in between, and short replies like "Yes, sir?" finish before it wakes up.
        self._buf = np.zeros(0, dtype=np.int16)
        self._pos = 0
        self._buf_lock = threading.Lock()
        self.lock = threading.Lock()  # a timer speaking from a background thread waits its turn
        self.out = None
        self._open()

    def _open(self):
        if self.out is not None:
            self.out.close()
        self.out = sd.OutputStream(samplerate=self.rate, channels=1, dtype="int16", callback=self._fill)
        self.out.start()

    def _fill(self, outdata, frames, t, status):
        with self._buf_lock:
            chunk = self._buf[self._pos:self._pos + frames]
            self._pos += len(chunk)
        outdata[:len(chunk), 0] = chunk
        outdata[len(chunk):] = 0

    def _done(self) -> bool:
        with self._buf_lock:
            return self._pos >= len(self._buf)

    def _play(self, audio: np.ndarray, watch: bool) -> bool:
        """Queue `audio` on the open stream and wait until it has played (or the wake word interrupts it)."""
        if not self.out.active:  # device unplugged or changed: reopen on the current default
            self._open()
        with self._buf_lock:
            self._buf, self._pos = audio, 0
        if watch:
            interrupted = self.ears.watch_for_wake_word(self._done)
        else:
            interrupted = False
            while not self._done():
                time.sleep(0.02)
        with self._buf_lock:
            self._buf, self._pos = np.zeros(0, dtype=np.int16), 0
        if not interrupted:
            time.sleep(self.out.latency)  # let the last buffered audio reach the speaker
        return interrupted

    def _synth(self, text: str) -> bytes:
        if hasattr(self.voice, "synthesize_stream_raw"):          # piper-tts < 1.3
            return b"".join(self.voice.synthesize_stream_raw(text))
        return b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text))  # >= 1.3

    def say(self, text: str) -> bool:
        """Speak `text`. Returns True if the user interrupted it by saying the wake word."""
        print(f"JARVIS: {text}")
        clean = re.sub(r"[*_#`>]", "", text or "").strip()
        if not clean:
            return False
        audio = np.frombuffer(self._synth(clean), dtype=np.int16)
        # only the main loop may read the mic; a background announcement just plays to the end
        watch = self.ears is not None and threading.current_thread() is threading.main_thread()
        with self.lock:
            return self._play(audio, watch)

    def beep(self):
        t = np.linspace(0, 0.12, int(0.12 * self.rate), False)
        with self.lock:
            self._play((0.2 * 32767 * np.sin(2 * np.pi * 880 * t)).astype(np.int16), watch=False)
