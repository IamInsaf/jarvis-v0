"""All settings in one place. Override any of them in a .env file."""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Brain (local LLM via Ollama) ---
# any tool-capable model works. This is the default: "switch to gemma" changes it while running,
# and that choice (data/brain.json) wins on the next start too (brains.py)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "12"))    # conversation turns kept in RAM
# 0 = deterministic. Local models skip tool calls (e.g. set_timer) much more often at
# higher temperatures, so keep this low unless you don't rely on tools.
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0"))
# context window in tokens. Ollama's default is far below what the model supports (qwen3.5: 256k) and it
# silently drops the oldest text when full — including the system prompt and its rules.
# Bigger costs more RAM/VRAM; lower it if the model gets slow or falls back to CPU.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
# let thinking models (qwen3.5, gemma4) reason before answering. Off: it adds seconds to every
# spoken reply, and the router and tool arguments are simple enough not to need it
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "0") == "1"
# seconds to wait for one Ollama reply (including loading a model) before giving up
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "180"))
# writes new abilities (selfcode.py); falls back to OLLAMA_MODEL if it isn't pulled
CODER_MODEL = os.getenv("CODER_MODEL", "qwen2.5-coder:7b")
# minutes of no LLM calls after which Ollama unloads the model to free RAM/VRAM (the next request
# reloads it, a few seconds slower). 0 = keep it loaded. "Turn off your brain" unloads it at once.
BRAIN_IDLE_MINUTES = float(os.getenv("BRAIN_IDLE_MINUTES", "10"))

# --- Ears ---
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small.en")   # tiny.en / base.en / small.en / medium.en
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")     # auto / cpu / cuda
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
VOICEPRINT_DIR = os.getenv("VOICEPRINT_DIR", "data/voiceprints")  # enrolled speaker voiceprints
VOICE_ID_THRESHOLD = float(os.getenv("VOICE_ID_THRESHOLD", "0.75"))  # cosine similarity to match a speaker
# short phrases score lower: keep a speaker recognized in the last few minutes down to this score
VOICE_ID_SOFT_THRESHOLD = float(os.getenv("VOICE_ID_SOFT_THRESHOLD", "0.6"))
VOICE_ID_STICKY_SECONDS = float(os.getenv("VOICE_ID_STICKY_SECONDS", "300"))
SILENCE_RMS = float(os.getenv("SILENCE_RMS", "500"))     # raise if your room is noisy
SILENCE_SECONDS = float(os.getenv("SILENCE_SECONDS", "0.9"))
# chance (0..1) from the Silero voice detector that a moment of audio is speech; raise if noise counts
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))
# a request that trails off ("set a timer for, um") waits this long for the rest of it
CONTINUE_SECONDS = float(os.getenv("CONTINUE_SECONDS", "1.5"))
# talk over JARVIS to interrupt it, no wake word needed. Turn off if it cuts itself off on loud speakers
BARGE_IN = os.getenv("BARGE_IN", "true").lower() == "true"
BARGE_IN_MS = float(os.getenv("BARGE_IN_MS", "320"))         # this much talking over it stops the reply
BARGE_IN_GRACE = float(os.getenv("BARGE_IN_GRACE", "0.6"))   # first seconds of a reply: learning its echo
BARGE_IN_MARGIN = float(os.getenv("BARGE_IN_MARGIN", "2.5"))  # how much louder than its echo you must be
MAX_RECORD_SECONDS = float(os.getenv("MAX_RECORD_SECONDS", "15"))
# after a reply, keep listening this long for a follow-up without the wake word (false = always need it)
FOLLOW_UP = os.getenv("FOLLOW_UP", "true").lower() == "true"
FOLLOW_UP_SECONDS = float(os.getenv("FOLLOW_UP_SECONDS", "5"))
FOLLOW_UP_QUESTION_SECONDS = float(os.getenv("FOLLOW_UP_QUESTION_SECONDS", "10"))  # when the reply asked something

# --- Type mode (fingers.py): dictate into any app ---
# hold to dictate, let go to type it at the cursor: "right ctrl", "right alt", "f8", "ctrl+win"... or "off"
TYPE_HOTKEY = os.getenv("TYPE_HOTKEY", "right ctrl")
TYPE_IDLE_SECONDS = float(os.getenv("TYPE_IDLE_SECONDS", "60"))  # voice type mode ends after this long silent

# --- Eyes (webcam, see docs/webcam-gestures-plan.md; tune the numbers with debug_eyes.py) ---
CAMERA_ENABLED = os.getenv("CAMERA_ENABLED", "false").lower() == "true"
# open the camera as soon as JARVIS starts; false = keep it closed until you say "camera on"
CAMERA_START_ON = os.getenv("CAMERA_START_ON", "false").lower() == "true"
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
CAMERA_FPS = float(os.getenv("CAMERA_FPS", "12"))
# facing the camera, hold a hand sign and speak: no name needed
GESTURE_TO_TALK = os.getenv("GESTURE_TO_TALK", "true").lower() == "true"
# facing the camera, plain "Jarvis" works (as the first word), not just "hey Jarvis"
NAME_ON_CAMERA = os.getenv("NAME_ON_CAMERA", "true").lower() == "true"
FACING_MAX_DEG = float(os.getenv("FACING_MAX_DEG", "25"))       # head yaw/pitch that still counts as facing
FACING_HOLD_MS = float(os.getenv("FACING_HOLD_MS", "300"))
MOUTH_MOVING_MIN = float(os.getenv("MOUTH_MOVING_MIN", "0.04"))  # std of jawOpen over half a second
GESTURE_MIN_SCORE = float(os.getenv("GESTURE_MIN_SCORE", "0.6"))
GESTURE_HOLD_MS = float(os.getenv("GESTURE_HOLD_MS", "400"))
GESTURE_LEAD_SECONDS = float(os.getenv("GESTURE_LEAD_SECONDS", "1.5"))  # a gesture this long before speaking counts

# --- Mouth ---
PIPER_VOICE = os.getenv("PIPER_VOICE", "voices/en_GB-alan-medium.onnx")

# --- Internet (private search) ---
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")

# --- Cloud router (off by default) ---
CLOUD_ENABLED = os.getenv("CLOUD_ENABLED", "false").lower() == "true"
CLOUD_CONFIRM = os.getenv("CLOUD_CONFIRM", "true").lower() == "true"  # ask before every cloud call
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# --- Memory ---
DB_PATH = os.getenv("JARVIS_DB", "data/jarvis.db")

# --- Files ---
# the one folder learned abilities may create, write and read files in (see files.py)
WORKSPACE_DIR = os.path.expanduser(os.getenv("WORKSPACE_DIR", r"~\Documents\Jarvis"))

# --- Website builder (sitebuilder.py) ---
# "use Claude" builds run Claude Code; this caps what one build or change may spend
CLAUDE_BUILD_BUDGET_USD = float(os.getenv("CLAUDE_BUILD_BUDGET_USD", "2"))
CLAUDE_BUILD_TIMEOUT = float(os.getenv("CLAUDE_BUILD_TIMEOUT", "1800"))  # seconds

# --- HUD (hud.py): live dashboard at http://127.0.0.1:HUD_PORT while JARVIS runs ---
HUD_ENABLED = os.getenv("HUD_ENABLED", "true").lower() == "true"
HUD_PORT = int(os.getenv("HUD_PORT", "8766"))

# --- Personality ---
USER_TITLE = os.getenv("USER_TITLE", "sir")
# say the day's briefing (schedule, to-dos) unprompted every day at this time, e.g. "07:30"; empty = never
BRIEFING_TIME = os.getenv("BRIEFING_TIME", "")
