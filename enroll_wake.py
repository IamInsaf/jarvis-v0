"""Teaches the wake word your voice: records you saying "hey Jarvis" and some normal speech, and trains a
small verifier on it (data/wake_verifier.pkl), which ears.py loads on the next start.
Usage: enroll_wake.py [--retrain] [device_index]    --retrain: reuse the clips recorded last time"""
import pickle
import sys
from pathlib import Path

import numpy as np
import openwakeword.utils
import scipy.io.wavfile
import sounddevice as sd
from openwakeword.custom_verifier_model import get_reference_clip_features, train_verifier_model
from openwakeword.model import Model

import config

RATE = 16000
FRAME = 1280
TAKES = 10          # "hey Jarvis" recordings
TAKE_SECONDS = 3
CLIPS = Path("data/wake_enroll")
# read aloud for the "not the wake word" examples: your voice saying anything else
SENTENCES = [
    "I was thinking we could order some food and watch a film tonight.",
    "Can you check whether the meeting moved to Thursday afternoon?",
    "The weather looks fine today, but it might rain later in the evening.",
    "Remind me to call my brother and pay the electricity bill tomorrow.",
    "Jasmine said the train was late again, so she's working from home.",
]

args = [a for a in sys.argv[1:] if a != "--retrain"]
retrain = "--retrain" in sys.argv
device = int(args[0]) if args else None


def record(seconds: float) -> np.ndarray:
    audio = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="int16", device=device)
    sd.wait()
    return audio[:, 0]


def save(path: Path, audio: np.ndarray):
    # a second of silence first: the detector needs some audio before the phrase to judge it
    padded = np.concatenate([np.zeros(RATE, np.int16), audio, np.zeros(RATE // 2, np.int16)])
    scipy.io.wavfile.write(path, RATE, padded)


def peak_score(model: Model, path: Path) -> float:
    model.reset()
    _, audio = scipy.io.wavfile.read(path)
    return max((model.predict(audio[i:i + FRAME])["hey_jarvis"]
                for i in range(0, len(audio) - FRAME, FRAME)), default=0.0)


openwakeword.utils.download_models(["hey_jarvis"])
model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
(CLIPS / "yes").mkdir(parents=True, exist_ok=True)
(CLIPS / "no").mkdir(parents=True, exist_ok=True)

if not retrain:
    print(f"Say \"Hey Jarvis\" the way you normally would, {TAKES} times. Mix it up a little:")
    print("closer and further from the mic, quicker and slower.\n")
    for n in range(1, TAKES + 1):
        input(f"[{n}/{TAKES}] Press Enter, then say \"Hey Jarvis\"... ")
        path = CLIPS / "yes" / f"take{n:02d}.wav"
        save(path, record(TAKE_SECONDS))
        print(f"      stock model's score: {peak_score(model, path):.2f}")
    print("\nNow some normal speech. Read each sentence aloud after pressing Enter.\n")
    for n, sentence in enumerate(SENTENCES, 1):
        input(f"[{n}/{len(SENTENCES)}] Press Enter, then read: \"{sentence}\" ")
        save(CLIPS / "no" / f"speech{n:02d}.wav", record(6))

yes = sorted((CLIPS / "yes").glob("*.wav"))
no = sorted((CLIPS / "no").glob("*.wav"))
if not yes or not no:
    sys.exit(f"No clips in {CLIPS}: run without --retrain first.")

# only the moments the stock model already half-thinks are "hey Jarvis" are what the verifier will be asked
# about at run time, so those are what it learns from
floor = config.WAKE_VERIFIER_THRESHOLD
positives = []
for path in yes:
    model.reset()
    positives.append(get_reference_clip_features(str(path), model, "hey_jarvis", threshold=floor, N=5))
positives = np.vstack(positives)
if positives.shape[0] == 0:
    sys.exit(f"None of your takes reached a score of {floor} on the stock model, so there's nothing to train on.\n"
             f"Try WAKE_VERIFIER_THRESHOLD=0.1 in .env and run: enroll_wake.py --retrain")
negatives = []
for path in no:
    model.reset()
    negatives.append(get_reference_clip_features(str(path), model, "hey_jarvis", threshold=0.0, N=1))
negatives = np.vstack(negatives)

print(f"\nTraining on {positives.shape[0]} wake-word moments and {negatives.shape[0]} of other speech...")
verifier = train_verifier_model(np.vstack([positives, negatives]),
                                np.array([1] * len(positives) + [0] * len(negatives)))
Path(config.WAKE_VERIFIER).parent.mkdir(parents=True, exist_ok=True)
with open(config.WAKE_VERIFIER, "wb") as f:
    pickle.dump(verifier, f)
print(f"Saved {config.WAKE_VERIFIER}. Restart JARVIS: it should say \"wake word: verifier on\".")
