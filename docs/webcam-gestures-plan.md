# Webcam + gesture control: plan

Status: plan only, nothing built yet. Written 2026-10-24.

## Goal

Let JARVIS use the webcam as a second set of senses, next to the microphone:

1. **Two ways to get JARVIS's attention, and both always work:**
   - **By name.** Say "Jarvis" or "Hey Jarvis" and it responds, with or without a gesture, looking at
     the camera or not. When you're facing the camera, just "Jarvis" is enough, even as the first
     word of a sentence ("Jarvis, open Spotify").
   - **By gesture.** Face the camera, hold a hand sign and speak, and JARVIS listens. You don't
     need to say its name.
   Facing the camera and talking with **no name and no gesture does nothing.** That's what keeps
   phone calls and talking to people in the room from setting it off.
2. **Gestures as structured commands.** A hand sign held while you speak tells JARVIS what *kind* of
   request it is, so it skips the guessing in `intents.route()`. It works like a slash command.
3. **Gestures as controls.** ✋ stops JARVIS mid-sentence. 👍 / 👎 answer yes/no questions.
4. **Later: face plus voice identity.** Knowing who's at the desk makes voice ID more reliable and
   protects sensitive actions.

Non-goals: no cloud vision (everything stays local, like the rest of JARVIS), no always-recording,
no face recognition of strangers.

The wake word **stays**. The camera is an extra way in, not a replacement: across the room, in the
dark, or with the camera off, "hey Jarvis" keeps working exactly as it does today.

---

## How it fits into the current code

How it works today (`jarvis.py` → `run_voice`):

```
ears.wait_for_wake_word()  →  mouth.say("Yes, sir?")  →  ears.listen()  →  voice_id.identify()
   →  brain.think(text, speaker)  →  mouth.say(answer)   (ears.watch_for_wake_word interrupts it)
   →  follow-up window (beep + ears.listen(wait=5/10))
```

What changes:

```
               ┌──────────── eyes.py (background thread, owns the camera) ───────────┐
               │ webcam → FaceLandmarker + GestureRecognizer → Sight snapshot        │
               │ present · facing · mouth_moving · gesture (stable) · faces · who    │
               └─────────────────────────────────┬───────────────────────────────────┘
                                                 │ read-only, thread-safe
ears.wait_for_attention(eyes)
   ├─ "hey jarvis" wake word (anywhere, camera or not)              → "wake"
   ├─ facing + mouth moving + voice onset + gesture held             → "gesture"
   └─ facing + mouth moving + voice onset, no gesture → transcribe it:
         starts with "jarvis" / "hey jarvis" → "name" (strip the name)   else → drop it silently
      ▼
"gesture" / "name": no "Yes, sir?", keep the audio already said (pre-roll)  /  "wake": as today
      ▼
brain.think(text, speaker, mode=GESTURE_MODES.get(gesture))  ── mode skips intents.route()
      ▼
mouth.say(answer) ── interrupted by wake word (listen again) OR ✋ held (stop, stay quiet)
```

The rule that keeps this safe: **the main thread still owns the microphone.** `eyes.py` never touches
audio and never calls the brain. It only publishes what it sees, and the existing loop in `jarvis.py`
reads that state at the points where it already makes decisions.

---

## Components

### 1. `eyes.py` (new)

A background thread that owns the webcam.

- **Capture:** `cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)` at 640×360. Run inference at
  `CAMERA_FPS` (default 12). Drop to 2 fps after 30 s with no face (idle mode, saves CPU), and jump
  back up as soon as a face appears.
- **Models (MediaPipe Tasks, CPU):**
  - `FaceLandmarker` with blendshapes and the facial transformation matrix turned on:
    - **facing:** head yaw/pitch from the transformation matrix, `|yaw| < FACING_MAX_DEG` and
      `|pitch| < FACING_MAX_DEG` (default 25°). The webcam sits on the monitor, so "facing the
      camera" ≈ "looking at the screen", and that's the signal we want. Iris-based gaze can come
      later if head pose turns out too loose.
    - **mouth_moving:** standard deviation of the `jawOpen` blendshape over the last ~0.5 s, above
      `MOUTH_MOVING_MIN`. This is what separates you talking to JARVIS from someone else talking in
      the room, and from JARVIS hearing its own voice.
    - **faces:** how many faces are in frame.
  - `GestureRecognizer` (canned model, 7 gestures: `Closed_Fist`, `Open_Palm`, `Pointing_Up`,
    `Thumb_Down`, `Thumb_Up`, `Victory`, `ILoveYou`).
  - Model files (`face_landmarker.task`, `gesture_recognizer.task`) download once into `models/` on
    first run, the same way `ears.py` fetches `hey_jarvis`.
- **Stabilizing (in pure functions so they can be tested):**
  - A gesture only counts once it has been seen with score ≥ `GESTURE_MIN_SCORE` in most frames for
    `GESTURE_HOLD_MS` (default 400 ms). This filters out hands passing by.
  - `facing` needs `FACING_HOLD_MS` (300 ms) to switch on and a short grace period to switch off,
    so a blink or a quick glance away doesn't flicker it.
- **Public API (snapshot, lock-protected):**
  ```python
  @dataclass(frozen=True)
  class Sight:
      present: bool; facing: bool; mouth_moving: bool; faces: int
      gesture: str | None          # stable gesture right now
      gesture_at: float            # when it became stable
      who: str | None = None       # phase 3
  eyes.sight() -> Sight
  eyes.recent_gesture(within: float) -> str | None   # held at any point in the last N s
  eyes.pause() / eyes.resume()                        # release / re-take the camera
  ```
- **Failure is never fatal.** No camera, camera busy, or MediaPipe missing → log once and report
  `Sight(present=False, ...)` forever. JARVIS then behaves exactly as it does today.

### 2. `ears.py` changes

- **Pre-roll ring buffer:** keep the last ~1.0 s of mic frames. With look-to-talk you start the
  sentence straight away ("open Spotify"), so the start of it is already in the past when JARVIS
  decides to listen. Today `listen()` calls `flush()` first and would throw those words away.
- **`wait_for_attention(eyes) -> tuple[str, str | None, str]`:** replaces `wait_for_wake_word()` when
  the camera is on, and returns (how, gesture, text already heard). For each 80 ms frame:
  - "hey jarvis" wake word score ≥ threshold → `("wake", gesture_if_any, "")`. This works the same
    whether or not you're on camera.
  - **or** voice onset (RMS > `SILENCE_RMS` for ~150 ms) **and** `sight.facing` **and**
    `sight.mouth_moving` **and** `sight.faces == 1` (or the one face is you, phase 3):
    - a gesture was held (`eyes.recent_gesture(GESTURE_LEAD_SECONDS)`) → `("gesture", g, "")`
    - no gesture → record the utterance (with pre-roll) and transcribe it. If it starts with
      "jarvis" / "hey jarvis" (`NAME = re.compile(r"^\W*(?:hey|hi|ok(?:ay)?)?\W*jarvis\b\W*", re.I)`)
      → `("name", None, rest_of_sentence)`. A bare "Jarvis" with nothing after it gets a normal
      "Yes, sir?" and listen. Anything else is **discarded without a sound**: it was you talking to
      someone else.
  - Why transcribe instead of using the wake model for a bare "Jarvis"? openWakeWord only ships
    "hey_jarvis". Whisper already runs locally, it's reliable on a name at the start of a sentence,
    and it only runs when you're facing the camera and talking, so the cost is small.
    `add_wake_word` (learned ability) could add a custom "jarvis" model later if Whisper is too slow.
- **`listen(wait, preroll=False, stop=None)`:** `preroll=True` starts the recording with the ring
  buffer instead of flushing it. `stop` is a callable checked each frame so a 👍/👎 can end the
  wait early (used by `confirm`).
- **`watch_for_wake_word(done, eyes=None)`** → returns `"wake" | "palm" | None` instead of a bool, so
  `Mouth` can tell "listen to me" apart from "be quiet".

### 3. `mouth.py` changes

- `_play` passes `eyes` through. ✋ held → stop playback, and `say()` returns `"palm"`.
- Background announcements (timers, website-ready) currently play to the end because only the main
  thread may read the mic. The camera has no such limit, so **✋ can silence a ringing alarm too.**
  That's a small but real improvement.

### 4. `jarvis.py` changes (main loop)

- Create `Eyes()` only when `CAMERA_ENABLED`, and add a `--no-camera` flag.
- `"gesture"` attention: no "Yes, sir?" (you've already started talking), `listen(preroll=True)`.
- `"name"` attention: the sentence is already transcribed; go straight to the brain with the name
  stripped. Bare "Jarvis" → "Yes, sir?" + listen, like the wake word.
- `"wake"` attention: unchanged. If a gesture is held while saying "hey Jarvis", it still sets the mode.
- `say()` returning `"palm"` → `follow_up = 0`, `woken = False`: JARVIS stops and goes quiet.
- **`confirm()`:** during each `listen(wait=8, stop=...)`, a held 👍 returns True and a held 👎 returns
  False, before or instead of a spoken answer. 👎 wins if both are seen, matching how `NO` is
  checked before `YES` today.
- **Follow-up window:** while you're facing the screen, keep it open a little longer
  (`FOLLOW_UP_FACING_SECONDS`). Looking away closes it right away.
- Voice commands for privacy: "stop watching" / "eyes off" → `eyes.pause()` (camera LED off, camera
  free for Zoom/Meet). "Start watching" / "eyes on" → `eyes.resume()`. Handle these in `jarvis.py`
  next to `SLEEP_WORDS`, not in the brain.

### 5. `brain.py` / `intents.py` changes: gesture modes

- `Brain.think(user_text, speaker=None, mode=None)`.
- A mode is either a **category** (skip `intents.route()`, use that category's tools and rules
  directly) or a **direct action** (no LLM at all).
- `rules.fast_path` still runs first for category modes ("set a timer for 5 minutes" is still a
  timer). It does **not** run for direct actions: ☝️ means "write this down", whatever you say.
- Default mapping in `config.py` (overridable with a `GESTURE_MODES` JSON string in `.env`):

| Gesture | Mode | What happens |
|---|---|---|
| ✋ `Open_Palm` | control | Stop speaking / cancel. Never a mode. |
| 👍 `Thumb_Up` / 👎 `Thumb_Down` | control | Yes / no in `confirm()` and after a question. |
| ☝️ `Pointing_Up` | direct: `add_task` | Your words go straight onto the to-do list (`todos.add(text)`). |
| ✌️ `Victory` | category: `websites` | Build/change website, with no misrouting to files or skills. |
| ✊ `Closed_Fist` | category: `apps` | Open an app. |
| 🤟 `ILoveYou` | category: `messages` | WhatsApp a contact (still read back and confirmed). |
| none | — | Normal routing, exactly as today. |

- Print `[gesture] Victory -> websites` next to the existing `[route]` log so misfires are easy to see.
- Gesture timing rule: **the gesture counts if it was held at any point from `GESTURE_LEAD_SECONDS`
  (1.5 s) before you start speaking until you start speaking.** "Hold it up and talk" and "flash it,
  then talk" both work, and a hand that goes up halfway through a sentence is ignored. This rule is
  simple to explain and simple to test.

### 6. Phase 3: face identity (`enroll_face.py`, `faceid.py`)

- OpenCV `FaceDetectorYN` (YuNet) + `FaceRecognizerSF` (SFace), both small ONNX models that ship with
  plain `opencv-python`. Enrollment works like `enroll_voice.py`: about 20 frames from slightly
  different angles go into `data/faceprints/<name>.npy`.
- Identify about once per second (not every frame) while a face is present → `Sight.who`.
- **Fusing with voice ID** in `run_voice`:
  - Voice confident (≥ `VOICE_ID_THRESHOLD`) → use voice, as today.
  - Voice weak or unknown, and exactly one face, recognized and facing → use the face's name. This
    fixes short phrases, which are where voice ID is weakest today.
  - Voice and face disagree → `unknown` (safe default: unscoped memory).
- **Sensitive tools** (`send_message`, Claude website builds, `add_feature`): in phase 3, require the
  person to be recognized by face *or* voice, plus the existing confirmation. Face alone is never
  enough to skip a confirmation, because a printed photo can fool a basic face check.

---

## Config (`config.py` additions)

```python
# --- Eyes (webcam) ---
CAMERA_ENABLED = os.getenv("CAMERA_ENABLED", "false").lower() == "true"   # opt-in
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
CAMERA_FPS = float(os.getenv("CAMERA_FPS", "12"))
GESTURE_TO_TALK = os.getenv("GESTURE_TO_TALK", "true").lower() == "true"  # face camera + gesture + speak, no name needed
NAME_ON_CAMERA = os.getenv("NAME_ON_CAMERA", "true").lower() == "true"    # facing: plain "Jarvis" works, not just "hey Jarvis"
FACING_MAX_DEG = float(os.getenv("FACING_MAX_DEG", "25"))
FACING_HOLD_MS = float(os.getenv("FACING_HOLD_MS", "300"))
MOUTH_MOVING_MIN = float(os.getenv("MOUTH_MOVING_MIN", "0.04"))      # jawOpen std; tune with debug_eyes.py
GESTURE_MIN_SCORE = float(os.getenv("GESTURE_MIN_SCORE", "0.6"))
GESTURE_HOLD_MS = float(os.getenv("GESTURE_HOLD_MS", "400"))
GESTURE_LEAD_SECONDS = float(os.getenv("GESTURE_LEAD_SECONDS", "1.5"))
FOLLOW_UP_FACING_SECONDS = float(os.getenv("FOLLOW_UP_FACING_SECONDS", "8"))
GESTURE_MODES = json.loads(os.getenv("GESTURE_MODES", "{}")) or DEFAULT_GESTURE_MODES
FACEPRINT_DIR = os.getenv("FACEPRINT_DIR", "data/faceprints")            # phase 3
FACE_ID_THRESHOLD = float(os.getenv("FACE_ID_THRESHOLD", "0.36"))         # SFace cosine; phase 3
```

New requirements: `mediapipe`, `opencv-python`.

---

## Phases

### Phase 0: spike (half a day). Prove the stack works on this machine.

- [ ] `pip install mediapipe opencv-python` in the venv. **Risk: the venv is Python 3.13.14.** Check
      that a MediaPipe wheel exists for 3.13 on Windows. If not, either make a 3.12 venv or fall
      back to YuNet (face + 5 landmarks for head pose) plus an ONNX hand-landmark model with a
      small rule-based gesture classifier.
- [ ] `debug_eyes.py` (like `debug_wake.py`): an OpenCV window with live yaw/pitch, facing,
      jawOpen std, mouth_moving, faces, and the stable gesture. Ctrl+C / q to quit.
- [ ] Measure: inference ms per frame, CPU % **while Whisper is transcribing**, and memory.
- **Done when:** all 7 gestures show reliably at desk distance in normal lighting, mouth_moving is
  clearly higher when talking than when silent, and CPU use leaves Whisper/Ollama latency
  unchanged (±10%).

### Phase 1: name or gesture to talk + ✋ stop.

- [ ] `eyes.py` with the `Sight` API, idle mode, pause/resume, and the never-fatal fallback.
- [ ] Ears: pre-roll ring buffer, `wait_for_attention`, `listen(preroll=...)`, and the new
      `watch_for_wake_word` return values.
- [ ] Mouth: ✋ stops main-thread replies and background announcements.
- [ ] `jarvis.py`: wiring, `--no-camera`, "eyes off/on", the three attention kinds.
- [ ] Tests for the pure logic: gesture stabilizer, facing hysteresis, attention rule, the `NAME`
      regex, and the gesture lead window, all driven by fake timestamped `Sight` streams (no camera
      needed).
- **Done when (manual checklist):**
  - "Hey Jarvis" works facing the camera, facing away, across the room, and with the camera off.
  - Facing the screen, "Jarvis, open Spotify" → opens, in one go, with no "Yes, sir?" in between.
  - Facing the screen, bare "Jarvis" → "Yes, sir?" and it listens.
  - Facing the screen, holding a gesture, "open Spotify" (no name) → opens, first word not lost.
  - Facing the screen and talking with no name and no gesture (e.g. on a phone call) → nothing
    happens, no sound.
  - Someone else talking off camera, or facing away → no trigger.
  - JARVIS's own voice never triggers it.
  - ✋ mid-reply → stops within ~0.5 s and stays quiet. ✋ during a ringing alarm → stops it.
  - Unplugging the camera mid-session → JARVIS keeps going on the wake word alone.

### Phase 2: gesture modes + 👍/👎.

- [ ] `mode` parameter through `Brain.think`, category and direct modes, `[gesture]` log.
- [ ] `GESTURE_MODES` config + defaults.
- [ ] 👍/👎 in `confirm()` and after questions.
- [ ] Tests: mode → category skips `intents.route` (mock `llm`), ☝️ → `todos.add` with the raw text.
- **Done when:** ✌️ + "make the header blue on sweet crumbs" → `change_website` every time;
  ☝️ + "buy milk" → on the to-do list with no LLM call; 🤟 + "tell mom I'm late" → read back
  and confirmed; 👍 answers "send it?"; no gesture → behaves exactly like today.

### Phase 3: face identity.

- [ ] `enroll_face.py`, `faceid.py`, `Sight.who`, fusion in `run_voice`, sensitive-tool gate.
- **Done when:** short phrases ("yes", "open Spotify") get the right speaker noticeably more often
  than with voice alone (log both for a week and compare), and a photo of you on a phone doesn't
  get a confirmation skipped.

### Phase 4 (optional extras)

- "Welcome back, sir" when you sit down after more than 2 hours away (once, and off by default).
- Pause a long reply when you walk away, and offer to continue when you're back.
- Custom gestures trained with MediaPipe Model Maker, if the 7 canned ones aren't enough.

---

## Risks and open questions

1. **Phone/video calls.** Mostly solved by needing a name or a gesture: ordinary talking on a call
   is dropped. What's left: saying "Jarvis" in a sentence to someone on a call, or a gesture that
   happens to match one of the 7 while you're talking. Mitigations: "eyes off" by voice, and the
   name only counts as the *first* word. Optionally, pause the camera triggers when another app is
   using the mic (Windows records this under `CapabilityAccessManager\ConsentStore` in the registry).
2. **Windows gives the camera to one app at a time.** While JARVIS holds the webcam, Zoom/Meet/Teams
   can't open it. That's why `pause()`/`resume()` and the "eyes off" voice command are in phase 1,
   not later. (b) above could also release the camera automatically.
3. **Python 3.13 support for MediaPipe:** confirm in phase 0 before anything else.
4. **CPU contention with Whisper and Ollama:** idle mode and 12 fps should be enough. If not, move
   `eyes` into its own process (`multiprocessing`) that sends the `Sight` snapshot over a pipe.
5. **Lighting, glasses, and backlight** affect face mesh and gestures. `debug_eyes.py` is how we tune
   the thresholds.
6. **Webcam LED is always on** while watching. That's expected, and "eyes off" turns it off.
7. **Privacy:** frames are processed in memory and never saved. Only faceprints (embeddings, not
   images) are stored, and only for people who enroll.

## Files touched (summary)

| File | Change |
|---|---|
| `eyes.py` | new: camera thread, MediaPipe, stabilizers, `Sight` |
| `debug_eyes.py` | new: live tuning window |
| `ears.py` | pre-roll, `wait_for_attention`, `listen(preroll, stop)`, richer interrupt result |
| `mouth.py` | ✋ interrupt, including background announcements |
| `jarvis.py` | wiring, `--no-camera`, eyes on/off, look-attention flow, 👍/👎 in `confirm` |
| `brain.py` | `think(..., mode=)`, direct actions |
| `intents.py` | accept a forced category |
| `config.py` | Eyes settings, `GESTURE_MODES` |
| `requirements.txt` | `mediapipe`, `opencv-python` |
| `enroll_face.py`, `faceid.py` | new, phase 3 |
| `tests/` | new: pure-logic tests for stabilizers, the attention rule, and modes |
