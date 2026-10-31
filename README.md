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
| `ears.py` | Wake word (openWakeWord) + record until you stop talking + faster-whisper; talk-over interruptions |
| `debug_wake.py` / `enroll_wake.py` | Check your wake-word score / train it on your voice |
| `install_startup.ps1` | Start JARVIS when you sign in to Windows (`-Remove` to undo) |
| `vad.py` | Silero voice detection: is that speech, or just noise? |
| `brain.py` | Ollama tool-calling agent loop + JARVIS personality |
| `tools.py` | Abilities: time, web search, remember/recall/forget, cloud expert, self-upgrade |
| `selfcode.py` | Drafts, checks and saves new abilities as plugins, with your approval |
| `plugins/` | One file per learned ability — safe to inspect, edit by hand, or delete |
| `memory.py` | SQLite facts + conversation log |
| `router.py` | The only path to the cloud: redacts, asks permission |
| `mouth.py` | Piper text-to-speech, spoken clause by clause as the reply is written |
| `speech_chunker.py` | Cuts a streaming reply into speakable clauses |
| `desktop.py` | Hands on the PC: keys, typing, windows, media and volume, reading the screen, PowerShell |
| `executor.py` | The safety gate every tool passes: run, ask first, or refuse |
| `briefing.py` | "Good morning": the day at a glance, on request or daily at `BRIEFING_TIME` |
| `focus.py` / `focus_settings.json` | Focus Guardian: keeps you on your main task, nudging you back when you drift |
| `fingers.py` | Type mode: dictate into any app (hold right Ctrl, or say "type mode start") |
| `hud.py` | Live dashboard: voice orb, request pipeline, tasks, timers, stats (http://127.0.0.1:8766) |
| `orb.py` | Always-on-top desktop orb with live captions and YES / NO buttons |
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

**"Hey Jarvis" doesn't always hear you?** The stock wake-word model was trained mostly on American and
British voices, so other accents can score under its 0.5 cut-off.
1. `python debug_wake.py`: say "Hey Jarvis" a few times, press Ctrl+C, and it suggests a `WAKE_THRESHOLD`
   for `.env`. Lowering the threshold is often enough.
2. If it's still hit and miss, run `python enroll_wake.py`. You record "Hey Jarvis" 10 times and read a few sentences
   aloud, and it trains a small verifier on your voice (`data/wake_verifier.pkl`). JARVIS then accepts weaker matches,
   but only in your voice, so the TV and other people don't set it off. Want to re-record? Just run it again.
   `--retrain` retrains on the last recordings, e.g. after changing `WAKE_VERIFIER_THRESHOLD`.

**6. Start with Windows** (optional)
```powershell
.\install_startup.ps1          # adds JARVIS to your Startup folder
.\install_startup.ps1 -Remove  # takes it out again
```
When you sign in, JARVIS starts with no console window; the orb in the corner shows it's running, and its output
goes to `data\jarvis.log`. Ollama already starts with Windows. For web search, turn on "Start Docker Desktop when you
sign in" in Docker Desktop's settings (JARVIS works without it, just without search). To stop it, end `pythonw.exe`
in Task Manager ("shut down" only makes it go quiet). Don't also run `python jarvis.py` by hand while it's running, or two of them will answer.

## HUD: watch JARVIS think
While `python jarvis.py` runs (voice or `--text`), open **http://127.0.0.1:8766** — F11 on a second
monitor looks the part.
- **Centre:** the voice orb pulses with your mic and with JARVIS's own voice; its colour is the state
  (cyan standby/listening, amber processing, white speaking, red when Ollama stops answering).
- **Pipeline:** each request travels through the real parts — input → voice ID → rules or router →
  category → memory / tools → LLM → output — with the tool and category it picked.
- **Panels:** system (model, Ollama, camera, CPU/RAM, time to first word), activity (requests, facts,
  routes per category), tasks and live timer countdowns, and the transcript, which fills in clause by
  clause as JARVIS speaks and marks interruptions and anything the safety gate blocked or asked about.
- **Approvals:** when JARVIS asks a yes/no question, a card with YES / NO appears; click or just answer out loud.

It only listens on this machine and loads nothing from the internet. Change the port with `HUD_PORT`
or turn it off with `HUD_ENABLED=false`. `python hud.py` on its own shows tasks and stats, without the live parts;
`python hud.py --demo` plays a scripted conversation through it (and the orb) so you can see it all without a mic.

## The orb
With the voice loop running, a small orb sits in the bottom-right corner over every app: the same colours as
the HUD, pulsing as you and JARVIS speak, with captions of what was heard and said beside it. When JARVIS asks
a yes/no question it shows YES / NO buttons too. Drag it anywhere (it remembers), click it to open the HUD,
right-click to type instead of talking: a chat bar opens above the orb, Enter sends (JARVIS answers just as if
you'd said it, out loud and in the captions), Esc closes it, and its ⋯ button has the menu. Say or type "show me what you're
seeing" (or "what do you see", "show your POV") and a small live camera view opens above the orb, with boxes
round the faces and hands JARVIS is tracking, whether you're facing it and talking, and the hand sign it reads;
"close the view" or its ✕ closes it. If the camera was off it opens just for the view and closes again after. It runs as its own process, so closing it never stops JARVIS. Settings: `ORB_ENABLED`,
`ORB_SIZE`, `ORB_CAPTIONS`, `ORB_IDLE` (`dim`, `hide` or `show` when nothing's happening).

## Type mode: dictate into any app (Windows)
Like Wispr Flow, but offline and free: the same local Whisper types what you say wherever your cursor is.
- **Hold right Ctrl**, speak, let go: it's typed at the cursor. Change the key with `TYPE_HOTKEY`
  (`right alt`, `f8`, `ctrl+win`...) or turn it off with `TYPE_HOTKEY=off`.
- **"Hey Jarvis" → "type mode start"**: every phrase is typed as you say it, until you say
  **"type mode done"** (or stay quiet for `TYPE_IDLE_SECONDS`, 60 by default).
- Say "new line" or "new paragraph" for line breaks.
- The text is pasted (clipboard + Ctrl+V), which works in every app; whatever text you had copied is put back.
- Click where the text should go first. Apps running as administrator need JARVIS run as administrator too.

## Focus Guardian: stay on your main task
Tell JARVIS what you're working on and it watches which window is in front. Drift off and it reminds you,
firmer the longer it lasts: **gentle at 5 minutes, firm at 15, strong at 30**, then every 10 minutes. The
lines are written by your local model to fit (stock lines when the brain is off or Ollama doesn't answer), and
they're only ever said between conversations, never over one.

| Say | What happens |
|---|---|
| "Focus on the login page in jarvis-v0" / "I'm working on the report" | Starts watching. The project is taken from what you said, or from the editor in front |
| "The next step is the logout button" / "Breadcrumb: ..." | Leaves a breadcrumb, read back when you return or ask |
| "Where was I?" / "What am I working on?" | The task, time focused so far and the next step |
| "Snooze for 20 minutes" | No reminders for a while (default 10 minutes) |
| "I'm taking a break for 15 minutes" | No reminders; calls you back to the task when it's over |
| "Park this" → "Back to work" | Puts the task on hold, keeping its next step, and picks it up again |
| "Switch my focus to the tests" | Logs the old task as switched and starts the new one |
| "I'm done with this task" / "Stop focus mode" | Ends the session (finished or not) and logs it |
| "How did I do today?" | The day's log: time on each task, time adrift, reminders |

**What counts as on task:** with a project, any window whose title names it (VS Code's
`app.py - jarvis-v0 - Visual Studio Code`, the GitHub page), or a terminal while a Claude Code session in that
project was written to in the last few minutes (read from `~/.claude/projects`). An editor on another project
counts as drifting. Without a project, any editor or terminal, a window mentioning the task, or docs and
localhost. No keyboard or mouse for 5 minutes means you've stepped away and the clock pauses, except on a video
site (YouTube, Netflix, Twitch...): watching counts as drifting. A session survives a restart.

**Settings:** thresholds, editors, terminals, video and distraction sites are in `focus_settings.json`;
`FOCUS_ENABLED=false` turns it off. **Try it in seconds:** `python jarvis.py --focus-test` (add `--text` to type)
prints what it can see (window, idle time, latest Claude Code session, the project it would pick), then uses the
file's `test_mode` block: reminders after 15, 30 and 60 seconds, every check printed. Say "focus on ..." with your
project open, switch to YouTube, and wait.

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
