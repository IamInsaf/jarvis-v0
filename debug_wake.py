"""Prints the live mic RMS level and 'hey jarvis' wake score every ~0.3s. Ctrl+C to stop.
Usage: debug_wake.py [device_index]"""
import queue
import sys
import time

import numpy as np
import openwakeword.utils
from openwakeword.model import Model
import sounddevice as sd

RATE = 16000
FRAME = 1280

device = int(sys.argv[1]) if len(sys.argv) > 1 else None

openwakeword.utils.download_models(["hey_jarvis"])
wake = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
q: queue.Queue = queue.Queue()


def on_audio(indata, frames, t, status):
    if status:
        print("STREAM STATUS:", status)
    q.put(indata[:, 0].copy())


info = sd.query_devices(device, "input") if device is not None else sd.query_devices(sd.default.device[0], "input")
print(f"Using device: {info['name']} (index={device if device is not None else sd.default.device[0]})")

stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                         device=device, callback=on_audio)
stream.start()
print("Speak normally, then say 'hey jarvis'. Watching RMS (loudness) and wake score.")

t0 = time.time()
best = 0.0
peak_rms = 0.0
try:
    while True:
        frame = q.get()
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        peak_rms = max(peak_rms, rms)
        scores = wake.predict(frame)
        m = max(scores.values())
        best = max(best, m)
        if time.time() - t0 > 0.3:
            print(f"rms={rms:6.1f} (peak={peak_rms:6.1f})   score={m:.3f}  (best so far={best:.3f})")
            t0 = time.time()
except KeyboardInterrupt:
    # a bit under your best "hey jarvis", so most of your attempts get through, but not so low noise does
    suggest = max(round(best * 0.7, 2), 0.2)
    print(f"\nBest wake score: {best:.3f}.")
    if best < 0.25:
        print("That's low: the stock model barely knows how you say it. Run enroll_wake.py to train it on your voice.")
    else:
        print(f"Try WAKE_THRESHOLD={suggest} in .env (now 0.5 by default). If it still misses you, run enroll_wake.py.")
