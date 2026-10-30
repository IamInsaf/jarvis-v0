"""Mouth: local Piper text-to-speech."""
import re
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
from piper import PiperVoice

import config
import hud


class Mouth:
    def __init__(self, ears=None, eyes=None):
        self.voice = PiperVoice.load(config.PIPER_VOICE)
        self.rate = self.voice.config.sample_rate
        self.ears = ears  # if set, saying the wake word again interrupts playback
        self.eyes = eyes  # if set, holding up an open palm stops playback
        # one output stream stays open for the whole session, playing silence between replies.
        # Opening a fresh stream per reply lets the audio device (Realtek jack, Bluetooth) fall
        # asleep in between, and short replies like "Yes, sir?" finish before it wakes up.
        self._buf = np.zeros(0, dtype=np.int16)
        self._pos = 0
        self._buf_lock = threading.Lock()
        # a timer speaking from a background thread waits its turn; re-entrant because a tool run
        # mid-reply (say_stream) may ask a yes/no question with say()
        self.lock = threading.RLock()
        self.first_audio_at = None  # when the last say_stream() started making sound
        self._levels: deque = deque(maxlen=64)  # (time, loudness) of recent output: the echo to expect
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
        if len(chunk):  # the HUD's orb moves with what's actually playing
            loud = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            self._levels.append((time.time(), loud))
            hud.level("voice", loud / 6000)
        outdata[:len(chunk), 0] = chunk
        outdata[len(chunk):] = 0

    def echo_level(self) -> float:
        """How loud JARVIS has been over the last moment (the speaker-to-mic echo arrives a little late)."""
        now = time.time()
        return max((loud for t, loud in list(self._levels) if now - t < 0.4), default=0.0)

    def _barge(self):
        from ears import BargeIn
        return BargeIn(self.echo_level) if config.BARGE_IN else None

    def _done(self) -> bool:
        with self._buf_lock:
            return self._pos >= len(self._buf)

    def _play(self, audio: np.ndarray, watch: bool) -> str | None:
        """Queue `audio` on the open stream and wait until it has played. Returns "wake" if the wake
        word interrupted it, "voice" if the user talked over it (ears.after_barge() has what they said),
        "palm" if an open palm did, None if it played to the end."""
        if not self.out.active:  # device unplugged or changed: reopen on the current default
            self._open()
        while not self._done():  # "One moment, sir." from say_stream finishes before a question starts
            time.sleep(0.01)
        with self._buf_lock:
            self._buf, self._pos = audio, 0
        return self._wait(watch, time.time())

    def _append(self, audio: np.ndarray):
        """Queue `audio` after whatever is still playing, without a gap."""
        if not self.out.active:
            self._open()
        with self._buf_lock:
            self._buf, self._pos = np.concatenate([self._buf[self._pos:], audio]), 0

    def _wait(self, watch: bool, start: float, barge=None) -> str | None:
        """Wait until the queued audio has played; see _play."""
        if watch and barge is None:
            barge = self._barge()
        palm = False

        def stop() -> bool:
            nonlocal palm
            palm = palm or (self.eyes is not None and self.eyes.palm_since(start))
            return palm or self._done()

        if watch:
            interrupted = self.ears.watch_for_interrupt(stop, barge)
        else:
            # the camera isn't tied to the main thread like the mic, so a palm also silences a ringing alarm
            interrupted = None
            while not stop():
                time.sleep(0.02)
        if palm and not interrupted:
            interrupted = "palm"
            print("  [eyes] open palm: stopped")
        with self._buf_lock:
            self._buf, self._pos = np.zeros(0, dtype=np.int16), 0
        if not interrupted:
            time.sleep(self.out.latency)  # let the last buffered audio reach the speaker
        return interrupted

    def _synth(self, text: str) -> bytes:
        if hasattr(self.voice, "synthesize_stream_raw"):          # piper-tts < 1.3
            return b"".join(self.voice.synthesize_stream_raw(text))
        return b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text))  # >= 1.3

    def say(self, text: str) -> str | None:
        """Speak `text`. Returns "wake" if the user interrupted it by saying the wake word, "voice" if they
        talked over it, "palm" if they held up an open palm, None if it played to the end."""
        print(f"JARVIS: {text}")
        clean = re.sub(r"[*_#`>]", "", text or "").strip()
        if not clean:
            return None
        hud.emit("say", text=clean)  # live captions on the HUD and the orb
        audio = np.frombuffer(self._synth(clean), dtype=np.int16)
        # only the main loop may read the mic; a background announcement just plays to the end
        watch = self.ears is not None and threading.current_thread() is threading.main_thread()
        with self.lock:
            before = hud.current()
            hud.state("speaking")
            try:
                return self._play(audio, watch)
            finally:
                hud.state("idle" if before == "speaking" else before)

    def say_stream(self, clauses) -> str | None:
        """Speak clauses as they arrive (Brain.think_stream): each is synthesized and queued while the
        one before it plays, so the first words come out while the model is still writing the rest.
        Returns "wake", "voice" or "palm" like say(); any of them stops the reply and closes `clauses`."""
        watch = self.ears is not None and threading.current_thread() is threading.main_thread()
        self.first_audio_at = None
        interrupted = None
        with self.lock:
            before = hud.current()
            start = time.time()
            barge = self._barge() if watch else None
            if watch:
                self.ears.flush()
            try:
                for text in clauses:  # pulling the next clause runs the model (and any tool) on this thread
                    clean = re.sub(r"[*_#`>]", "", text or "").strip()
                    if clean:
                        print(f"JARVIS: {clean}")
                        hud.emit("say", text=clean)
                        self._append(np.frombuffer(self._synth(clean), dtype=np.int16))
                        if self.first_audio_at is None:
                            self.first_audio_at = time.time()
                        hud.state("speaking")
                    if watch and (heard := self.ears.heard_interrupt(barge)):
                        interrupted = heard
                    elif self.eyes is not None and self.eyes.palm_since(start):
                        interrupted = "palm"
                        print("  [eyes] open palm: stopped")
                    if interrupted:
                        break
                if not interrupted:
                    interrupted = self._wait(watch, start, barge)
            finally:
                close = getattr(clauses, "close", None)
                if close:  # the brain records the turn with what was actually said
                    close()
                if interrupted:
                    with self._buf_lock:
                        self._buf, self._pos = np.zeros(0, dtype=np.int16), 0
                hud.state("idle" if before in ("speaking", "thinking") else before)
        return interrupted

    def beep(self):
        t = np.linspace(0, 0.12, int(0.12 * self.rate), False)
        with self.lock:
            self._play((0.2 * 32767 * np.sin(2 * np.pi * 880 * t)).astype(np.int16), watch=False)
