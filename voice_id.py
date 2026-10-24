"""Voice ID: enroll and recognize speakers by voiceprint (local, offline).
Personalization only for now — nothing here restricts what a tool call can do;
that's a separate, deliberate policy decision for later."""
import time
from pathlib import Path

import numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav

import config


class VoiceID:
    def __init__(self):
        self.encoder = VoiceEncoder()
        self.dir = Path(config.VOICEPRINT_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.voiceprints = {p.stem: np.load(p) for p in self.dir.glob("*.npy")}
        self.last: tuple[str, float] | None = None  # (speaker, when) of the last match

    def enroll(self, name: str, audio_samples: list[np.ndarray]):
        """audio_samples: a few float32 16kHz waveforms of that person talking."""
        embeds = [self.encoder.embed_utterance(preprocess_wav(a)) for a in audio_samples]
        voiceprint = np.mean(embeds, axis=0)
        np.save(self.dir / f"{name}.npy", voiceprint)
        self.voiceprints[name] = voiceprint

    def identify(self, audio: np.ndarray) -> tuple[str, float]:
        """Returns (speaker_name, confidence). speaker is 'unknown' if nobody is
        enrolled yet, or if the best match is below config.VOICE_ID_THRESHOLD — unless
        that same speaker was matched recently and this still scores above
        config.VOICE_ID_SOFT_THRESHOLD (short phrases give noisy voiceprints)."""
        if not self.voiceprints:
            return "unknown", 0.0
        embed = self.encoder.embed_utterance(preprocess_wav(audio))
        scores = {name: float(np.dot(embed, vp) / (np.linalg.norm(embed) * np.linalg.norm(vp)))
                  for name, vp in self.voiceprints.items()}
        name, score = max(scores.items(), key=lambda kv: kv[1])
        recent = (self.last is not None and self.last[0] == name
                  and time.time() - self.last[1] < config.VOICE_ID_STICKY_SECONDS)
        if score >= config.VOICE_ID_THRESHOLD or (recent and score >= config.VOICE_ID_SOFT_THRESHOLD):
            self.last = (name, time.time())
            return name, score
        return "unknown", score
