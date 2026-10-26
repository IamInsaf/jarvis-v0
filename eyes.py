"""Eyes: a background thread that watches the webcam and reports what it sees — whether you're
facing the screen, whether your mouth is moving, and which hand sign you're holding. It never
touches the mic or the brain; the main loop reads `sight()` where it already makes decisions.
Frames stay in memory and are never saved. Without a camera (or MediaPipe) it reports nobody there,
and JARVIS works on the wake word alone."""
import math
import os
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import config

MODELS = {
    "face_landmarker": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task",
    "gesture_recognizer": "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task",
}
PALM = "Open_Palm"  # stop / be quiet, never a way to start talking
IDLE_AFTER = 30     # seconds with no face before dropping to IDLE_FPS
IDLE_FPS = 2
STALE = 1.0         # a sight older than this (camera stalled or paused) counts as nobody there


@dataclass(frozen=True)
class Sight:
    at: float = 0.0
    faces: int = 0
    facing: bool = False
    mouth_moving: bool = False
    gesture: str | None = None    # stable gesture right now
    gesture_since: float = 0.0    # when it became stable


class Debounce:
    """A bool that turns on after `on` seconds of True and off after `off` seconds of False,
    so a blink or a glance away doesn't flicker it."""

    def __init__(self, on: float, off: float):
        self.on, self.off = on, off
        self.state, self._since, self._want = False, 0.0, False

    def update(self, t: float, value: bool) -> bool:
        if value != self._want:
            self._want, self._since = value, t
        if value != self.state and t - self._since >= (self.on if value else self.off):
            self.state = value
        return self.state


class MouthMotion:
    """Std of the jawOpen blendshape over the last `window` seconds: high while talking, ~0 when still."""

    def __init__(self, window: float = 0.5):
        self.window = window
        self._jaw: deque = deque()

    def update(self, t: float, jaw_open: float | None) -> float:
        if jaw_open is None:
            self._jaw.clear()
            return 0.0
        self._jaw.append((t, jaw_open))
        while self._jaw and t - self._jaw[0][0] > self.window:
            self._jaw.popleft()
        return float(np.std([v for _, v in self._jaw])) if len(self._jaw) > 2 else 0.0


class GestureFilter:
    """Turns noisy per-frame guesses into a stable gesture: the one seen (with score >= min_score)
    in at least `share` of the frames of the last `hold` seconds. Hands passing by don't count."""

    def __init__(self, hold: float, min_score: float, share: float = 0.7):
        self.hold, self.min_score, self.share = hold, min_score, share
        self._seen: deque = deque()
        self._start: float | None = None

    def update(self, t: float, name: str | None, score: float) -> str | None:
        if self._start is None:
            self._start = t
        seen = name if name not in (None, "None") and score >= self.min_score else None
        self._seen.append((t, seen))
        while self._seen and t - self._seen[0][0] > self.hold:
            self._seen.popleft()
        if t - self._start < self.hold:
            return None
        names = [n for _, n in self._seen]
        best = max(set(names), key=names.count)
        return best if best and names.count(best) / len(names) >= self.share else None


def head_angles(matrix) -> tuple[float, float]:
    """(yaw, pitch) in degrees from MediaPipe's facial transformation matrix; 0, 0 = facing the camera."""
    r = np.asarray(matrix)[:3, :3]
    yaw = math.degrees(math.asin(max(-1.0, min(1.0, -r[2, 0]))))
    pitch = math.degrees(math.atan2(r[2, 1], r[2, 2]))
    return yaw, pitch


class Eyes:
    def __init__(self, watching: bool = True):
        self._lock = threading.Lock()
        self._sight = Sight()
        self._last_mouth = 0.0           # when the mouth was last seen moving
        self._gesture_seen: dict = {}    # stable gesture -> last time it was held
        self._watching = threading.Event()
        if watching:  # otherwise the camera stays closed until resume()
            self._watching.set()
        threading.Thread(target=self._run, daemon=True, name="eyes").start()

    # --- what the main loop asks ---

    def sight(self) -> Sight:
        with self._lock:
            return self._sight

    def engaged(self, within: float = 0.6) -> bool:
        """One face, facing the screen, mouth moving in the last `within` seconds: someone talking *to* JARVIS
        (as long as they also say its name or hold a gesture)."""
        now = time.time()
        with self._lock:
            s, mouth = self._sight, self._last_mouth
        return now - s.at < STALE and s.faces == 1 and s.facing and now - mouth <= within

    def recent_gesture(self, within: float) -> str | None:
        """The gesture held most recently in the last `within` seconds, ignoring the stop palm."""
        now = time.time()
        with self._lock:
            recent = [(t, g) for g, t in self._gesture_seen.items() if g != PALM and now - t <= within]
        return max(recent)[1] if recent else None

    def palm_since(self, t: float) -> bool:
        """An open palm raised after `t` and still held: stop talking. One raised before `t` doesn't
        count, so a palm that just stopped one reply doesn't also cut off the next."""
        s = self.sight()
        return time.time() - s.at < STALE and s.gesture == PALM and s.gesture_since >= t

    @property
    def watching(self) -> bool:
        return self._watching.is_set()

    def pause(self):
        """Let go of the camera (LED off, free for Zoom/Teams) until resume()."""
        self._watching.clear()

    def resume(self):
        self._watching.set()

    # --- the camera thread ---

    def _run(self):
        os.environ.setdefault("GLOG_minloglevel", "2")  # MediaPipe's C++ logging is very chatty
        try:
            import cv2
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision
            Path("models").mkdir(exist_ok=True)
            for name, url in MODELS.items():
                if not (path := Path("models") / f"{name}.task").exists():
                    print(f"  [eyes] downloading {path} ...")
                    urllib.request.urlretrieve(url, path)
            face = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path="models/face_landmarker.task"),
                running_mode=vision.RunningMode.VIDEO, num_faces=3,
                output_face_blendshapes=True, output_facial_transformation_matrixes=True))
            hands = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
                base_options=BaseOptions(model_asset_path="models/gesture_recognizer.task"),
                running_mode=vision.RunningMode.VIDEO))
        except Exception as e:  # no MediaPipe, no models, no internet for the first download
            print(f"  [eyes] off: {type(e).__name__}: {e}")
            return

        facing = Debounce(config.FACING_HOLD_MS / 1000, 0.3)
        mouth = MouthMotion()
        gestures = GestureFilter(config.GESTURE_HOLD_MS / 1000, config.GESTURE_MIN_SCORE)
        cam, warned, last_face, last_ms = None, False, time.time(), -1
        t0 = time.time()
        while True:
            if not self._watching.is_set():  # paused: let go of the camera before waiting
                if cam is not None:
                    cam.release()
                    cam = None
                with self._lock:
                    self._sight, self._gesture_seen = Sight(), {}
                self._watching.wait()
            if cam is None:
                cam = cv2.VideoCapture(config.CAMERA_INDEX, cv2.CAP_DSHOW)  # DSHOW opens much faster than MSMF
                cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
                cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # the newest frame, not one from before an idle sleep
                if not cam.isOpened():
                    cam.release()
                    cam = None
                    if not warned:
                        print(f"  [eyes] camera {config.CAMERA_INDEX} didn't open (in use by another app?); "
                              "retrying every 5 s")
                        warned = True
                    time.sleep(5)
                    continue
                if warned:
                    print("  [eyes] camera is back")
                warned = False
            ok, frame = cam.read()
            if not ok:
                print("  [eyes] camera stopped giving frames")
                cam.release()
                cam = None
                with self._lock:
                    self._sight, self._gesture_seen = Sight(), {}
                continue

            now = time.time()
            ms = max(int((now - t0) * 1000), last_ms + 1)  # MediaPipe needs strictly increasing timestamps
            last_ms = ms
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            f = face.detect_for_video(image, ms)
            h = hands.recognize_for_video(image, ms)

            faces = len(f.face_landmarks)
            looking, jaw = False, None
            if faces:
                last_face = now
                yaw, pitch = head_angles(f.facial_transformation_matrixes[0])
                looking = abs(yaw) < config.FACING_MAX_DEG and abs(pitch) < config.FACING_MAX_DEG
                jaw = next((c.score for c in f.face_blendshapes[0] if c.category_name == "jawOpen"), None)
            moving = mouth.update(now, jaw) >= config.MOUTH_MOVING_MIN
            top = h.gestures[0][0] if h.gestures else None
            gesture = gestures.update(now, top.category_name if top else None, top.score if top else 0.0)

            with self._lock:
                old = self._sight
                since = old.gesture_since if gesture == old.gesture else now
                self._sight = Sight(now, faces, facing.update(now, looking), moving, gesture, since)
                if moving:
                    self._last_mouth = now
                if gesture:
                    self._gesture_seen[gesture] = now

            fps = IDLE_FPS if now - last_face > IDLE_AFTER else config.CAMERA_FPS
            time.sleep(max(0.0, 1 / fps - (time.time() - now)))
