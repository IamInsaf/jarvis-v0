"""Is someone speaking? Silero VAD (the ONNX model faster-whisper already ships) run on the live mic, frame by
frame. Unlike a loudness threshold it ignores fans, typing and music, and doesn't drop quiet trailing words,
so recording stops when you've finished talking rather than when you pause for breath.
Falls back to loudness (SILENCE_RMS) if the model can't be loaded."""
import glob
import os

import numpy as np

import config

WINDOW = 512   # samples per Silero step at 16 kHz (32 ms)
CONTEXT = 64   # samples of the previous step it also looks at


def _model_path() -> str | None:
    try:
        import faster_whisper
    except ImportError:
        return None
    found = sorted(glob.glob(os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad*.onnx")))
    return found[-1] if found else None


class SpeechDetector:
    def __init__(self, path: str | None = None, loudness_only: bool = False):
        self.session = None
        path = None if loudness_only else path or _model_path()
        if path:
            try:
                import onnxruntime
                opts = onnxruntime.SessionOptions()
                opts.inter_op_num_threads = opts.intra_op_num_threads = 1
                opts.log_severity_level = 4
                self.session = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"],
                                                            sess_options=opts)
            except Exception as e:  # any failure here just means loudness is used instead
                print(f"  [vad] Silero unavailable ({e}); using loudness")
        self.reset()

    def reset(self):
        """Forget earlier audio: call at the start of each recording."""
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(CONTEXT, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.prob = 0.0

    def probability(self, frame: np.ndarray) -> float:
        """Chance (0..1) that this int16 mic frame has speech in it."""
        if self.session is None:
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            return 1.0 if rms > config.SILENCE_RMS else 0.0
        self.pending = np.concatenate([self.pending, frame.astype(np.float32) / 32768.0])
        best = None
        while len(self.pending) >= WINDOW:
            window, self.pending = self.pending[:WINDOW], self.pending[WINDOW:]
            x = np.concatenate([self.context, window])[None, :]
            out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
            self.context = window[-CONTEXT:]
            p = float(np.ravel(out)[0])
            best = p if best is None else max(best, p)
        if best is not None:
            self.prob = best
        return self.prob

    def is_speech(self, frame: np.ndarray) -> bool:
        return self.probability(frame) >= config.VAD_THRESHOLD
