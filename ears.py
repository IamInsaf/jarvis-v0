"""Ears: always-on wake word + record until silence + local Whisper transcription.
Nothing here touches the internet (models download once on first run)."""
import queue
import re
import threading
import time
from collections import deque
from typing import NamedTuple

import numpy as np
import sounddevice as sd
import openwakeword.utils
from openwakeword.model import Model
from faster_whisper import WhisperModel

import config
import hud
from vad import SpeechDetector

RATE = 16000
FRAME = 1280  # 80 ms frames, what openWakeWord expects
PREROLL_FRAMES = 13  # ~1 s kept before speech starts: on camera you start talking straight away
ONSET_FRAMES = 2     # this many loud frames in a row (160 ms) = someone started speaking
LATE_FRAMES = 6      # up to ~0.5 s after that for the camera to catch up on the mouth moving
# "Jarvis, open Spotify" / "hey Jarvis." / "OK Jarvis". Whisper writes the name a few ways.
NAME = re.compile(r"^\W*(?:(?:hey|hi|ok(?:ay)?|yo)\W+)?(?:jarvis|jarvas|javis|jervis)\b\W*", re.I)


class Heard(NamedTuple):
    """Why JARVIS is listening: "wake" (hey Jarvis: say "Yes, sir?" then listen), "name" (facing the
    camera, said "Jarvis ..."), or "gesture" (facing the camera, holding a hand sign). For name and
    gesture, text is what was already said (without the name); "" means nothing came after the name."""
    how: str
    gesture: str | None = None
    text: str = ""
    audio: np.ndarray | None = None


def without_name(text: str) -> str | None:
    """The request after a leading "Jarvis" ("" for just the name), or None if it doesn't start with it."""
    m = NAME.match(text)
    return text[m.end():].strip() if m else None


# a request that trails off like this isn't finished: "set a timer for, um", "remind me to call mum and"
_UNFINISHED = re.compile(r"(?:,|\b(?:and|or|but|so|because|then|um+|uh+|er+|like|the|a|an|to|for|with|of|"
                         r"my|your|about|that|if|when|which|is|are|also|plus|in|at|from|into|by))\W*$", re.I)


def sounds_unfinished(text: str) -> bool:
    """True if the transcript stops mid-thought, so it's worth listening a moment longer."""
    t = text.strip()
    return bool(t) and not t.endswith(("?", "!")) and bool(_UNFINISHED.search(t.rstrip(". ")))


def _rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


class BargeIn:
    """Hears the user start talking while JARVIS speaks, without a wake word. With no echo cancelling,
    the mic also picks up JARVIS's own voice from the speakers, so for the first moments of each reply it
    learns how loud that echo is relative to what's playing (`echo` gives the current output loudness),
    and after that only speech clearly louder than the expected echo counts."""

    def __init__(self, echo):
        self.echo = echo
        self.playing_since = None  # when JARVIS's voice first came out of the speakers
        self.ratio = 0.0   # mic loudness per unit of output loudness, i.e. how much of JARVIS the mic hears
        self.run = 0       # frames in a row that sounded like the user talking
        self.recent: deque = deque(maxlen=PREROLL_FRAMES)  # kept so their first words aren't lost

    def feed(self, frame: np.ndarray, vad) -> bool:
        """True once the user has been talking over JARVIS for BARGE_IN_MS."""
        if not self.recent:  # a new reply: forget what the detector heard before it
            vad.reset()
        self.recent.append(frame)
        rms, out = _rms(frame), self.echo()
        if out > 1 and self.playing_since is None:
            self.playing_since = time.time()
        if self.playing_since is not None and time.time() - self.playing_since < config.BARGE_IN_GRACE:
            self.ratio = max(self.ratio, rms / max(out, 1.0))
            vad.probability(frame)  # keep the detector's memory of the audio current
            self.run = 0
            return False
        # before JARVIS makes a sound (still thinking) there's no echo, just the room
        floor = config.BARGE_IN_MARGIN * self.ratio * out + config.SILENCE_RMS
        speaking = vad.is_speech(frame) and rms > floor
        self.run = self.run + 1 if speaking else 0
        return self.run * FRAME / RATE * 1000 >= config.BARGE_IN_MS


class Ears:
    def __init__(self):
        openwakeword.utils.download_models(["hey_jarvis"])
        self.wake = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        self.stt = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE, compute_type="int8")
        self.stt_lock = threading.Lock()  # the type mode hotkey transcribes on its own thread
        self.vad = SpeechDetector()  # when someone is talking, as opposed to just noise
        self.q: queue.Queue = queue.Queue()
        self.tap: list | None = None  # while set, mic audio goes here instead of to self.q (fingers.py)
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                     blocksize=FRAME, callback=self._on_audio)
        self.stream.start()

    def _on_audio(self, indata, frames, t, status):
        tap = self.tap
        frame = indata[:, 0].copy()
        (self.q.put if tap is None else tap.append)(frame)
        hud.level("mic", _rms(frame) / (config.SILENCE_RMS * 4))  # speech threshold ≈ a quarter of the bar

    def start_tap(self):
        """Send the mic to a private recording until stop_tap(): hold-to-talk dictation."""
        self.tap = []

    def stop_tap(self) -> np.ndarray | None:
        frames, self.tap = self.tap, None
        return np.concatenate(frames) if frames else None

    def transcribe(self, audio: np.ndarray | None) -> tuple[str, np.ndarray | None]:
        """Whisper on int16 audio from stop_tap(); see _transcribe."""
        return self._transcribe(audio)

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

    def wait_for_attention(self, eyes=None) -> Heard:
        """Wait until someone wants JARVIS: "hey Jarvis" anywhere, or, facing the camera with the mouth
        moving, speech that starts with "Jarvis" or comes with a hand sign. Facing the camera and
        talking with neither (a phone call, someone in the room) is ignored without a sound."""
        on_camera = eyes is not None and (config.GESTURE_TO_TALK or config.NAME_ON_CAMERA)
        hud.state("idle")
        while True:
            self.flush()
            preroll: deque = deque(maxlen=PREROLL_FRAMES)
            loud, how, gesture = 0, None, None
            while how is None:
                frame = self.q.get()
                preroll.append(frame)
                scores = self.wake.predict(frame)
                if max(scores.values()) >= config.WAKE_THRESHOLD:
                    return Heard("wake", eyes.recent_gesture(config.GESTURE_LEAD_SECONDS) if eyes else None)
                if not on_camera:
                    continue
                loud = loud + 1 if _rms(frame) > config.SILENCE_RMS else 0
                if not ONSET_FRAMES <= loud <= ONSET_FRAMES + LATE_FRAMES or not eyes.engaged():
                    continue
                gesture = eyes.recent_gesture(config.GESTURE_LEAD_SECONDS)
                if gesture and config.GESTURE_TO_TALK:
                    how = "gesture"
                elif config.NAME_ON_CAMERA:
                    how = "name"
            text, audio = self._transcribe(self._record(0, frames=list(preroll)))
            rest = without_name(text)
            if how == "gesture" and text:
                return Heard("gesture", gesture, text if rest is None else rest, audio)
            if how == "name" and rest is not None:
                return Heard("name", None, rest, audio)
            if text:
                print("  [eyes] speech without my name or a gesture: ignored")

    def watch_for_wake_word(self, done) -> bool:
        """Poll the mic for the wake word until `done()` is true (e.g. JARVIS has
        finished speaking). Lets the user interrupt JARVIS mid-sentence by saying
        its name again. Returns True if the wake word was heard."""
        return self.watch_for_interrupt(done) == "wake"

    def watch_for_interrupt(self, done, barge: "BargeIn | None" = None) -> str | None:
        """Poll the mic until `done()` while JARVIS speaks. Returns "wake" for the wake word, "voice" if
        `barge` heard the user start talking over it (see after_barge), None if nobody interrupted."""
        self.flush()
        while not done():
            try:
                frame = self.q.get(timeout=0.08)
            except queue.Empty:
                continue
            if heard := self._interrupt(frame, barge):
                return heard
        return None

    def heard_interrupt(self, barge: "BargeIn | None" = None) -> str | None:
        """Like watch_for_interrupt, but only looks at the mic audio queued since the last look, without
        waiting: for while JARVIS is busy writing the next clause and can't sit and watch."""
        while True:
            try:
                frame = self.q.get_nowait()
            except queue.Empty:
                return None
            if heard := self._interrupt(frame, barge):
                return heard

    def heard_wake_word(self) -> bool:
        return self.heard_interrupt() == "wake"

    def _interrupt(self, frame: np.ndarray, barge: "BargeIn | None") -> str | None:
        if max(self.wake.predict(frame).values()) >= config.WAKE_THRESHOLD:
            return "wake"
        if barge is not None and barge.feed(frame, self.vad):
            self.barge_preroll = list(barge.recent)
            return "voice"
        return None

    def after_barge(self) -> tuple[str, np.ndarray | None]:
        """What the user said when they talked over JARVIS: the words that interrupted it plus the rest."""
        frames, self.barge_preroll = getattr(self, "barge_preroll", []), []
        before = hud.current()
        hud.state("listening")
        try:
            return self._finish_thought(*self._transcribe(self._record(0, frames=frames)))
        finally:
            hud.state("idle" if before == "listening" else before)

    def _record(self, wait: float, frames: list | None = None):
        """Record until silence. `frames` is audio already heard (pre-roll), which counts as started."""
        frames = frames or []
        silent, started = 0.0, bool(frames)
        t0, frame_sec = time.time(), FRAME / RATE
        self.vad.reset()
        for frame in frames:  # the pre-roll primes the detector
            self.vad.probability(frame)
        while True:
            frame = self.q.get()
            frames.append(frame)
            if self.vad.is_speech(frame):
                started, silent = True, 0.0
            else:
                silent += frame_sec
            elapsed = time.time() - t0
            if started and silent >= config.SILENCE_SECONDS:
                break
            if not started and elapsed > wait:   # nobody spoke
                return None
            if elapsed > config.MAX_RECORD_SECONDS:
                break
        return np.concatenate(frames)

    def listen(self, wait: float = 5, flush: bool = True) -> tuple[str, np.ndarray | None]:
        """Record one utterance and return (text, audio). text is '' and audio is
        None if nothing was said within `wait` seconds. audio is float32 at 16kHz, suitable for voice ID.
        flush=False keeps audio heard since the last call (dictation: you keep talking while it transcribes)."""
        if flush:
            self.flush()
        before = hud.current()  # a yes/no asked mid-request goes back to "thinking" afterwards
        hud.state("listening")
        try:
            return self._finish_thought(*self._transcribe(self._record(wait)))
        finally:
            hud.state("idle" if before == "listening" else before)

    def _finish_thought(self, text: str, audio: np.ndarray | None) -> tuple[str, np.ndarray | None]:
        """If the request trails off ("remind me to call mum and"), keep listening a moment for the rest
        and transcribe it all together, instead of answering half a sentence."""
        for _ in range(2):
            if not sounds_unfinished(text):
                break
            print(f"  [ears] sounds unfinished: {text!r}, listening on")
            more = self._record(config.CONTINUE_SECONDS)
            if more is None:
                break
            text, audio = self._transcribe(np.concatenate([(audio * 32768).astype(np.int16), more]))
        return text, audio

    def _transcribe(self, audio: np.ndarray | None) -> tuple[str, np.ndarray | None]:
        if audio is None:
            return "", None
        audio = audio.astype(np.float32) / 32768.0
        with self.stt_lock:
            segments, _ = self.stt.transcribe(audio, language="en", vad_filter=True, beam_size=1)
            text = " ".join(s.text.strip() for s in segments).strip()  # segments is lazy: decode under the lock
        return text, audio
