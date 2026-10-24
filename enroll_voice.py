"""One-time setup: enroll a person's voice so JARVIS can recognize who's speaking.
Run: python enroll_voice.py <name>"""
import sys
import time

import numpy as np
import sounddevice as sd

from voice_id import VoiceID

RATE = 16000
SAMPLES = 3
SECONDS = 4


def record(seconds: float) -> np.ndarray:
    audio = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="float32")
    sd.wait()
    return audio[:, 0]


def main():
    if len(sys.argv) < 2:
        print("Usage: python enroll_voice.py <name>")
        return
    name = sys.argv[1].strip().lower()
    voice_id = VoiceID()
    samples = []
    for i in range(SAMPLES):
        input(f"Press Enter, then talk naturally for {SECONDS}s (sample {i + 1}/{SAMPLES})...")
        print("Recording...")
        samples.append(record(SECONDS))
        print("Got it.")
        time.sleep(0.3)
    voice_id.enroll(name, samples)
    print(f"Enrolled '{name}'. JARVIS will recognize this voice from now on.")


if __name__ == "__main__":
    main()
