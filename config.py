"""All settings in one place. Override any of them in a .env file."""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Brain (local LLM via Ollama) ---
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")   # any tool-capable model works
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "12"))    # conversation turns kept in RAM
# 0 = deterministic. Local models skip tool calls (e.g. set_timer) much more often at
# higher temperatures, so keep this low unless you don't rely on tools.
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0"))

# --- Ears ---
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small.en")   # tiny.en / base.en / small.en / medium.en
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")     # auto / cpu / cuda
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
SILENCE_RMS = float(os.getenv("SILENCE_RMS", "500"))     # raise if your room is noisy
SILENCE_SECONDS = float(os.getenv("SILENCE_SECONDS", "0.9"))
MAX_RECORD_SECONDS = float(os.getenv("MAX_RECORD_SECONDS", "15"))

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

# --- Personality ---
USER_TITLE = os.getenv("USER_TITLE", "sir")
