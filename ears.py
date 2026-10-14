"""Ears: always-on wake word + record until silence + local Whisper transcription.
Nothing here touches the internet (models download once on first run)."""
import queue
import time

import numpy as np
import sounddevice as sd
import openwakeword.utils
from openwakeword.model import Model
from faster_whisper import WhisperModel

import config

RATE = 16000
FRAME = 1280  # 80 ms frames, what openWakeWord expects


class Ears:
    def __init__(self):
        openwakeword.utils.download_models(["hey_jarvis"])
        self.wake = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        self.stt = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE, compute_type="int8")
        self.q: queue.Queue = queue.Queue()
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                     blocksize=FRAME, callback=self._on_audio)
        self.stream.start()

    def _on_audio(self, indata, frames, t, status):
        self.q.put(indata[:, 0].copy())

    def flush(self):
        """Drop buffered audio (e.g. JARVIS's own voice) and reset the wake detector."""
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.wake.reset()

    def wait_for_wake_word(self):
        self.flush()
        while True:
            scores = self.wake.predict(self.q.get())
            if max(scores.values()) >= config.WAKE_THRESHOLD:
                return

    def _record(self):
        frames, silent, started = [], 0.0, False
        t0, frame_sec = time.time(), FRAME / RATE
        while True:
            frame = self.q.get()
            frames.append(frame)
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            if rms > config.SILENCE_RMS:
                started, silent = True, 0.0
            else:
                silent += frame_sec
            elapsed = time.time() - t0
            if started and silent >= config.SILENCE_SECONDS:
                break
            if not started and elapsed > 5:      # nobody spoke
                return None
            if elapsed > config.MAX_RECORD_SECONDS:
                break
        return np.concatenate(frames)

    def listen(self) -> str:
        """Record one utterance and return the text ('' if nothing was said)."""
        self.flush()
        audio = self._record()
        if audio is None:
            return ""
        segments, _ = self.stt.transcribe(audio.astype(np.float32) / 32768.0,
                                          language="en", vad_filter=True, beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()
