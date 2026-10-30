"""Eyes: a background thread that watches the webcam and reports what it sees — whether you're
facing the screen, whether your mouth is moving, and which hand sign you're holding. It never
touches the mic or the brain; the main loop reads `sight()` where it already makes decisions.
Frames stay in memory and are never saved; while someone is looking through JARVIS's eyes (the POV view, see
show_view) the latest one is also drawn on, with boxes round the faces and hands it's tracking, and handed to
the HUD to show. Without a camera (or MediaPipe) it reports nobody there,
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
VIEWER_GONE = 2.0   # seconds without a POV frame being asked for before drawing them stops
VIEW_ONLY_GRACE = 8.0  # a camera opened just for the view closes this long after the view stops asking
# the 21 hand landmarks' bones (MediaPipe's HAND_CONNECTIONS): thumb, four fingers, palm
HAND_BONES = ((0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12),
              (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17))
GREEN, AMBER, GREY, CYAN = (136, 255, 0), (71, 181, 255), (170, 170, 170), (255, 212, 42)  # BGR, the HUD's colours


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


def _box(points, w: int, h: int, pad: float = 0.04) -> tuple[int, int, int, int]:
    """Pixel box round normalized landmarks, a little padded and kept inside the frame."""
    xs, ys = [p.x for p in points], [p.y for p in points]
    px, py = (max(xs) - min(xs)) * pad + 0.005, (max(ys) - min(ys)) * pad + 0.005
    return (max(0, int((min(xs) - px) * w)), max(0, int((min(ys) - py) * h)),
            min(w - 1, int((max(xs) + px) * w)), min(h - 1, int((max(ys) + py) * h)))


def annotate(frame, faces: list, hands: list, sight: "Sight", fps: float = 0.0):
    """Draw what JARVIS is tracking on a BGR frame (in place): a box and a sparse landmark mesh per face (the
    one it's watching labelled facing / talking), each hand's skeleton and box with the gesture it reads,
    and a status line. `faces` and `hands` are MediaPipe landmark lists (points with normalized .x .y);
    `hands` items are (landmarks, gesture name or None, score)."""
    import cv2
    h, w = frame.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX

    def label(text, x, y, colour):
        y = max(14, y)
        (tw, th), _ = cv2.getTextSize(text, font, .45, 1)
        cv2.rectangle(frame, (x, y - th - 6), (x + tw + 6, y), colour, -1)
        cv2.putText(frame, text, (x + 3, y - 4), font, .45, (0, 0, 0), 1, cv2.LINE_AA)

    for i, face in enumerate(faces):
        main = i == 0
        colour = GREEN if main and sight.facing else AMBER if main else GREY
        x0, y0, x1, y1 = _box(face, w, h)
        cv2.rectangle(frame, (x0, y0), (x1, y1), colour, 2)
        for p in face[::6]:  # every 6th of the 478 points: enough to see the mesh follow the face
            cv2.circle(frame, (int(p.x * w), int(p.y * h)), 1, colour, -1)
        tags = [f"face {i + 1}"]
        if main:
            tags += ["facing" if sight.facing else "looking away"] + (["talking"] if sight.mouth_moving else [])
        label(" - ".join(tags), x0, y0 - 2, colour)  # OpenCV's fonts are ASCII only
    for marks, gesture, score in hands:
        pts = [(int(p.x * w), int(p.y * h)) for p in marks]
        for a, b in HAND_BONES:
            if a < len(pts) and b < len(pts):
                cv2.line(frame, pts[a], pts[b], CYAN, 2, cv2.LINE_AA)
        for pt in pts:
            cv2.circle(frame, pt, 3, (255, 255, 255), -1)
        x0, y0, x1, y1 = _box(marks, w, h, pad=.15)
        cv2.rectangle(frame, (x0, y0), (x1, y1), CYAN, 1)
        if gesture and gesture != "None":
            label(f"{gesture.replace('_', ' ').lower()} {score:.0%}", x0, y0 - 2, CYAN)
    status = f"faces {sight.faces}   gesture {(sight.gesture or '-').replace('_', ' ').lower()}"
    if fps:
        status += f"   {fps:.0f} fps"
    cv2.putText(frame, status, (8, h - 10), font, .5, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(frame, status, (8, h - 10), font, .5, (233, 251, 255), 1, cv2.LINE_AA)
    return frame


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
        self._view = None          # the last frame drawn for the POV view (BGR), while someone's watching it
        self._view_at = 0.0        # when the view last asked for a frame
        self._view_only = False    # the camera was opened just for the view: close it when the view goes
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
        self._view_only = False
        self._watching.clear()

    def resume(self):
        self._view_only = False
        self._watching.set()

    # --- the POV view: what the camera sees, with what's being tracked drawn on ---

    def show_view(self) -> bool:
        """Start drawing frames for the view. Opens the camera if it's closed, just for as long as the view
        keeps asking for frames; returns True if it had to."""
        self._view_at = time.time()
        if self._watching.is_set():
            return False
        self._view_only = True
        self._watching.set()
        return True

    def view_png(self, width: int = 400) -> bytes | None:
        """The latest drawn frame as a PNG `width` pixels wide, or None while there's none yet (camera
        starting) or it's gone stale. Asking keeps the view, and a camera opened for it, alive."""
        import cv2
        self._view_at = time.time()
        if self._view_only and not self._watching.is_set():
            self._watching.set()  # asked again after the grace period ran out
        with self._lock:
            frame, at = self._view, self._sight.at
        if frame is None or time.time() - at > STALE * 2:
            return None
        width = max(80, min(int(width), frame.shape[1] * 2))
        small = cv2.resize(frame, (width, round(frame.shape[0] * width / frame.shape[1])),
                           interpolation=cv2.INTER_AREA)
        ok, png = cv2.imencode(".png", small)
        return png.tobytes() if ok else None

    def _viewed(self, now: float) -> bool:
        return now - self._view_at < VIEWER_GONE

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
        t0 = last_frame = time.time()
        while True:
            if not self._watching.is_set():  # paused: let go of the camera before waiting
                if cam is not None:
                    cam.release()
                    cam = None
                with self._lock:
                    self._sight, self._gesture_seen, self._view = Sight(), {}, None
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

            if viewed := self._viewed(now):
                gestures_seen = list(h.gestures) + [[]] * len(h.hand_landmarks)  # a hand may have no guess
                hands_seen = [(marks, g[0].category_name if g else None, g[0].score if g else 0.0)
                              for marks, g in zip(h.hand_landmarks, gestures_seen)]
                try:
                    drawn = annotate(frame, f.face_landmarks, hands_seen, self.sight(),
                                     1 / max(1e-3, now - last_frame))
                except Exception as e:  # a bad drawing mustn't stop the eyes the wake gestures rely on
                    print(f"  [eyes] couldn't draw the view: {type(e).__name__}: {e}")
                    drawn = frame
                with self._lock:
                    self._view = drawn
            elif self._view is not None:
                with self._lock:
                    self._view = None
            last_frame = now
            if self._view_only and now - self._view_at > VIEW_ONLY_GRACE:
                print("  [eyes] view closed: camera off again")
                self._watching.clear()  # opened just for the view; the top of the loop lets go of it
                continue
            fps = IDLE_FPS if now - last_face > IDLE_AFTER and not viewed else config.CAMERA_FPS
            time.sleep(max(0.0, 1 / fps - (time.time() - now)))
