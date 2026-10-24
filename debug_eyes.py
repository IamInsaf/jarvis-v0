"""Live webcam view of what JARVIS's eyes would see: head yaw/pitch, facing, mouth movement,
face count and hand gesture, with per-model timings. q or Esc (or Ctrl+C) to stop.
Use it to tune the thresholds in docs/webcam-gestures-plan.md before eyes.py is built.
Usage: debug_eyes.py [camera_index] [--no-window] [--seconds N]
  --no-window  print the state to the console instead of opening a window
  --seconds N  stop after N seconds and print a timing summary"""
import argparse
import math
import time
import urllib.request
from collections import deque
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

MODELS = {
    "face_landmarker": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task",
    "gesture_recognizer": "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task",
}
# starting values for the plan's settings; tune them here, then carry them into config.py
FACING_MAX_DEG = 25
MOUTH_WINDOW_S = 0.5
MOUTH_MOVING_MIN = 0.04   # std of jawOpen over MOUTH_WINDOW_S
GESTURE_MIN_SCORE = 0.6
GESTURE_HOLD_S = 0.4
GESTURE_SHARE = 0.7       # of the frames in GESTURE_HOLD_S that must show the gesture

p = argparse.ArgumentParser()
p.add_argument("camera", nargs="?", type=int, default=0)
p.add_argument("--no-window", action="store_true")
p.add_argument("--seconds", type=float, default=0)
args = p.parse_args()

Path("models").mkdir(exist_ok=True)
for name, url in MODELS.items():
    path = Path("models") / f"{name}.task"
    if not path.exists():
        print(f"Downloading {path} ...")
        urllib.request.urlretrieve(url, path)

face = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
    base_options=BaseOptions(model_asset_path="models/face_landmarker.task"),
    running_mode=vision.RunningMode.VIDEO, num_faces=3,
    output_face_blendshapes=True, output_facial_transformation_matrixes=True))
hands = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
    base_options=BaseOptions(model_asset_path="models/gesture_recognizer.task"),
    running_mode=vision.RunningMode.VIDEO))

cam = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)  # DSHOW opens much faster than MSMF on Windows
cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
if not cam.isOpened() or not cam.read()[0]:
    raise SystemExit(f"Camera {args.camera} didn't open. Is another app (Zoom, Teams, Camera) using it?")


def head_angles(matrix) -> tuple[float, float]:
    """(yaw, pitch) in degrees from MediaPipe's facial transformation matrix; 0, 0 = facing the camera."""
    r = np.asarray(matrix)[:3, :3]
    yaw = math.degrees(math.asin(max(-1.0, min(1.0, -r[2, 0]))))
    pitch = math.degrees(math.atan2(r[2, 1], r[2, 2]))
    return yaw, pitch


def stable_gesture(history: deque, now: float) -> str | None:
    """The gesture seen in most frames of the last GESTURE_HOLD_S, once that much history exists."""
    recent = [g for t, g in history if now - t <= GESTURE_HOLD_S]
    if not recent or now - history[0][0] < GESTURE_HOLD_S:
        return None
    best = max(set(recent), key=recent.count)
    return best if best and recent.count(best) / len(recent) >= GESTURE_SHARE else None


jaw = deque()       # (time, jawOpen)
gestures = deque()  # (time, gesture name or None)
timings = {"face": [], "hands": [], "frame": []}
t0 = last_print = time.time()
try:
    while True:
        ok, frame = cam.read()
        if not ok:
            print("Camera stopped giving frames.")
            break
        start = time.time()
        now, ms = start, int((start - t0) * 1000)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

        f = face.detect_for_video(image, ms)
        t_face = time.time()
        h = hands.recognize_for_video(image, ms)
        t_hands = time.time()

        faces = len(f.face_landmarks)
        yaw = pitch = jaw_open = None
        if faces:
            yaw, pitch = head_angles(f.facial_transformation_matrixes[0])
            jaw_open = next(c.score for c in f.face_blendshapes[0] if c.category_name == "jawOpen")
            jaw.append((now, jaw_open))
        while jaw and now - jaw[0][0] > MOUTH_WINDOW_S:
            jaw.popleft()
        mouth_std = float(np.std([v for _, v in jaw])) if len(jaw) > 2 else 0.0
        facing = faces > 0 and abs(yaw) < FACING_MAX_DEG and abs(pitch) < FACING_MAX_DEG
        mouth_moving = faces > 0 and mouth_std >= MOUTH_MOVING_MIN

        raw, score = None, 0.0
        if h.gestures:
            top = h.gestures[0][0]
            raw, score = top.category_name, top.score
        seen = raw if raw not in (None, "None") and score >= GESTURE_MIN_SCORE else None
        gestures.append((now, seen))
        while gestures and now - gestures[0][0] > 2:
            gestures.popleft()
        gesture = stable_gesture(gestures, now)

        timings["face"].append((t_face - start) * 1000)
        timings["hands"].append((t_hands - t_face) * 1000)
        timings["frame"].append((time.time() - start) * 1000)

        angles = f"yaw {yaw:+5.0f} pitch {pitch:+5.0f}" if faces else "no face"
        lines = [
            f"faces {faces}   {angles}   facing {'YES' if facing else 'no'}",
            f"jawOpen {jaw_open or 0:.2f}  std {mouth_std:.3f}  mouth {'MOVING' if mouth_moving else 'still'}",
            f"gesture raw {raw or '-'} {score:.2f}   stable {gesture or '-'}",
            f"face {timings['face'][-1]:.0f} ms  hands {timings['hands'][-1]:.0f} ms",
        ]
        if args.no_window:
            if now - last_print >= 0.3:
                print(" | ".join(lines))
                last_print = now
        else:
            for i, line in enumerate(lines):
                cv2.putText(frame, line, (10, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
                cv2.putText(frame, line, (10, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                            (0, 255, 136) if facing else (200, 200, 200), 1)
            cv2.imshow("JARVIS eyes (q to quit)", frame)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
        if args.seconds and now - t0 >= args.seconds:
            break
except KeyboardInterrupt:
    pass
finally:
    cam.release()
    cv2.destroyAllWindows()

if timings["frame"]:
    secs = time.time() - t0
    print(f"\n{len(timings['frame'])} frames in {secs:.1f} s ({len(timings['frame']) / secs:.1f} fps, camera-limited)")
    for k, v in timings.items():
        print(f"  {k:6s} median {np.median(v):5.1f} ms   p95 {np.percentile(v, 95):5.1f} ms")
