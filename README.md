# JARVIS v0 — private, local-first voice assistant

Say **"Hey Jarvis"**, ask anything, and it answers out loud. The brain, memory, voice and
speech recognition all run on your own machine. The internet is only used for private web
search (via your own SearXNG) and, if you turn it on, an anonymized cloud model for hard questions.

```
mic → wake word → Whisper (local) → Ollama LLM (local) ⇄ tools → Piper voice (local)
                                          │
                       ┌──────────────────┼──────────────────┐
                    memory            web_search         ask_cloud_expert
                (SQLite, local)    (SearXNG, private)   (off by default, redacted,
                                                          asks you before sending)
```

## Files
| File | What it does |
|---|---|
| `jarvis.py` | Main loop. `python jarvis.py` for voice, `--text` to type |
| `ears.py` | Wake word (openWakeWord) + record until silence + faster-whisper |
| `brain.py` | Ollama tool-calling agent loop + JARVIS personality |
| `tools.py` | Abilities: time, web search, remember/recall/forget, cloud expert, self-upgrade |
| `selfcode.py` | Drafts, checks and saves new abilities as plugins, with your approval |
| `plugins/` | One file per learned ability — safe to inspect, edit by hand, or delete |
| `memory.py` | SQLite facts + conversation log |
| `router.py` | The only path to the cloud: redacts, asks permission |
| `mouth.py` | Piper text-to-speech |
| `fingers.py` | Type mode: dictate into any app (hold right Ctrl, or say "type mode start") |
| `hud.py` | Live dashboard: voice orb, request pipeline, tasks, timers, stats (http://127.0.0.1:8766) |
| `config.py` / `.env` | All settings |

## Setup (Windows, macOS or Linux)

**1. Python 3.10+ and packages**
```bash
cd jarvis
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

**2. Local brain — Ollama**
Install from ollama.com, then:
```bash
ollama pull qwen3.5:4b
```
Any tool-calling model works. It fits fully in 6 GB of VRAM next to Whisper. Bigger GPU (8 GB+) → try `qwen3.5:9b` and set `OLLAMA_MODEL` in `.env`.

**3. Voice — Piper**
Download a voice (`.onnx` + `.onnx.json`) from the Piper voices collection on Hugging Face
(rhasspy/piper-voices). `en_GB-alan-medium` sounds the most JARVIS-like. Put both files in `voices/`.

**4. Private search — SearXNG** (needs Docker)
Edit `searxng/settings.yml` and change `secret_key` to a long random string, then:
```bash
docker compose up -d
```
It's bound to `127.0.0.1` only, so nothing else on your network can use it.

**5. Run**
```bash
python jarvis.py --text   # test the brain by typing first
python jarvis.py          # then voice: say "Hey Jarvis"
```

Try: "Hey Jarvis, remember that I study mechanical engineering" → later "what do I study?"
Try: "Hey Jarvis, what's the latest news on SpaceX?" (uses private search)

## HUD: watch JARVIS think
While `python jarvis.py` runs (voice or `--text`), open **http://127.0.0.1:8766** — F11 on a second
monitor looks the part.
- **Centre:** the voice orb pulses with your mic and with JARVIS's own voice; its colour is the state
  (cyan standby/listening, amber processing, white speaking, red when Ollama stops answering).
- **Pipeline:** each request travels through the real parts — input → voice ID → rules or router →
  category → memory / tools → LLM → output — with the tool and category it picked.
- **Panels:** system (model, Ollama, camera, CPU/RAM), activity (requests, facts, routes per category),
  tasks and live timer countdowns, and the transcript.

It only listens on this machine and loads nothing from the internet. Change the port with `HUD_PORT`
or turn it off with `HUD_ENABLED=false`. `python hud.py` on its own shows tasks and stats, without the live parts.

## Type mode: dictate into any app (Windows)
Like Wispr Flow, but offline and free: the same local Whisper types what you say wherever your cursor is.
- **Hold right Ctrl**, speak, let go: it's typed at the cursor. Change the key with `TYPE_HOTKEY`
  (`right alt`, `f8`, `ctrl+win`...) or turn it off with `TYPE_HOTKEY=off`.
- **"Hey Jarvis" → "type mode start"**: every phrase is typed as you say it, until you say
  **"type mode done"** (or stay quiet for `TYPE_IDLE_SECONDS`, 60 by default).
- Say "new line" or "new paragraph" for line breaks.
- The text is pasted (clipboard + Ctrl+V), which works in every app; whatever text you had copied is put back.
- Click where the text should go first. Apps running as administrator need JARVIS run as administrator too.

## Self-upgrade: learning new abilities on the fly
Ask for something JARVIS has no tool for ("Hey Jarvis, learn to flip a coin") and it calls
`add_feature`. It drafts a small plugin — locally by default, free and private but a weaker
coder — runs it through a safety check (valid syntax, only safe imports, no
`eval`/`subprocess`/`os.system`/file access), tells you what it plans to add, and only saves +
loads it if you say yes. With `CLOUD_ENABLED=true` it drafts with the cloud model instead for
noticeably better code, at a small per-request cost and the usual cloud confirmation prompt.

Learned abilities live as plain Python files in `plugins/` — read any of them, hand-edit them,
or just say "forget the coin flip ability" (`remove_feature`) to delete one. JARVIS's own core
files are never touched by this; a bad plugin can only ever be removed, not break anything else.

## Optional: cloud model for hard questions
In `.env`: `CLOUD_ENABLED=true` and add `ANTHROPIC_API_KEY`. Emails, phone numbers and long
numbers are stripped first, and with `CLOUD_CONFIRM=true` JARVIS asks you out loud before
anything leaves the machine.

## Tuning
- Wakes too easily → raise `WAKE_THRESHOLD` (0.6–0.7). Misses you → lower it.
- Cuts you off mid-sentence → raise `SILENCE_SECONDS`. Noisy room → raise `SILENCE_RMS`.
- Mishears you → `WHISPER_MODEL=medium.en` (slower, more accurate). GPU → `WHISPER_DEVICE=cuda`.
- "Hey Jarvis" is a free pre-trained openWakeWord model; you can train your own wake word later.

## Privacy checklist
- Keep `.env` and `data/` out of git (`.gitignore` included).
- Put `data/` on an encrypted disk (BitLocker / FileVault / LUKS).
- Reach it from your phone through Tailscale, never by opening ports.

## Next upgrades (v1 → v3)
1. **Vector memory** — Qdrant/Chroma over the `log` table so it recalls past conversations by meaning.
2. **Streaming** — stream LLM tokens into Piper sentence-by-sentence for sub-second replies.
3. **Hands** — Home Assistant (lights, fans), filesystem, Gmail/Calendar (read-only first;
   confirm before send/delete).
4. **Phone** — small web app with a mic button, served over Tailscale.
5. **Proactive** — scheduler for morning briefings and reminders.
