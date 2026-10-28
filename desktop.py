"""Hands on the computer itself (Windows): key presses and shortcuts, typing, switching windows, volume and
media keys, looking at the screen, and PowerShell. Every call reports what actually happened; nothing here
decides whether it *should* run — executor.py does that before any of it is called."""
import base64
import io
import re
import subprocess
import sys
import time

import apps
import config
import fingers
import llm

_KEYUP = 0x0002
_UNICODE = 0x0004  # KEYEVENTF_UNICODE: the scan code is a character, not a key
_NAMED = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
          "start": 0x5B, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
          "space": 0x20, "spacebar": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
          "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
          "pageup": 0x21, "page up": 0x21, "pagedown": 0x22, "page down": 0x22, "printscreen": 0x2C,
          "print screen": 0x2C, "capslock": 0x14, "caps lock": 0x14,
          "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE, "dot": 0xBE}
_NAMED.update({f"f{i}": 0x6F + i for i in range(1, 13)})
_MEDIA = {"play": 0xB3, "pause": 0xB3, "play pause": 0xB3, "resume": 0xB3, "next": 0xB0, "skip": 0xB0,
          "previous": 0xB1, "back": 0xB1, "stop": 0xB2, "mute": 0xAD, "unmute": 0xAD,
          "volume up": 0xAF, "louder": 0xAF, "volume down": 0xAE, "quieter": 0xAE}
_VOL_STEP = 2  # percent per volume key press on Windows
_WORDS = {"zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
          "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "half": 50, "max": 100,
          "maximum": 100, "full": 100}


def parse_keys(spec: str) -> list[int]:
    """'ctrl+shift+t', 'control s', 'Alt F4', 'win d' -> virtual key codes, modifiers first.
    Raises ValueError for a key it doesn't know."""
    s = str(spec).lower().strip()
    for two in ("page up", "page down", "print screen", "caps lock"):
        s = s.replace(two, two.replace(" ", ""))
    parts = [p for p in re.split(r"[\s+]+|-(?=.)", s) if p and p not in ("and", "then", "key", "keys", "the")]
    if not parts:
        raise ValueError("no keys given")
    codes = []
    for p in parts:
        if p in _NAMED:
            codes.append(_NAMED[p])
        elif len(p) == 1 and (p.isalpha() or p.isdigit()):
            codes.append(ord(p.upper()))
        else:
            raise ValueError(f"I don't know the key '{p}'")
    return codes


def _send(events: list[tuple]):
    """Key events as (virtual key, flags), or (0, unicode unit, flags) for a typed character."""
    if sys.platform != "win32":
        raise OSError("this only works on Windows for now")
    import ctypes
    inputs = fingers._input_array([e if len(e) == 3 else (e[0], 0, e[1]) for e in events])
    if ctypes.windll.user32.SendInput(len(inputs), inputs, ctypes.sizeof(inputs[0])) != len(inputs):
        raise OSError("Windows blocked the key press; is that window running as administrator?")


def press_keys(spec: str) -> str:
    codes = parse_keys(spec)
    _send([(vk, 0) for vk in codes] + [(vk, _KEYUP) for vk in reversed(codes)])
    return f"Pressed {spec.strip()}."


def type_text(text: str) -> str:
    if not str(text).strip():
        return "What should I type?"
    try:
        fingers.type_text(str(text))
    except OSError as e:
        if "clipboard" not in str(e):
            raise
        type_keys(str(text))  # a clipboard manager (or Windows) is holding it: type it key by key instead
    title = foreground_title()
    return "Typed it" + (f" into {title}." if title else ".")


def type_keys(text: str):
    """Type `text` as individual Unicode key presses: slower than pasting, but needs no clipboard."""
    events = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            events += [(_NAMED["enter"], 0), (_NAMED["enter"], _KEYUP)]
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):  # characters outside the BMP are two UTF-16 units
            code = int.from_bytes(units[i:i + 2], "little")
            events += [(0, code, _UNICODE), (0, code, _UNICODE | _KEYUP)]
    _send(events)


def media(action: str) -> str:
    """Play/pause, next, previous, stop, mute, volume up/down, or 'volume 40' to set a level."""
    a = re.sub(r"\s+", " ", str(action).lower().replace("/", " ")).strip()
    a = re.sub(r"^(?:the |turn (?:it |the volume )?)", "", a)
    level = re.search(r"(\d{1,3})\s*(?:%|percent)?", a)
    word = next((v for k, v in _WORDS.items() if re.search(rf"\b{k}\b", a)), None)
    if ("volume" in a or "%" in a or "percent" in a) and (level or word is not None):
        return set_volume(int(level.group(1)) if level else word)
    key = next((k for k in sorted(_MEDIA, key=len, reverse=True) if k in a), None)
    if key in ("volume up", "louder", "volume down", "quieter"):
        presses = 5  # about 10%
        _send([e for _ in range(presses) for e in ((_MEDIA[key], 0), (_MEDIA[key], _KEYUP))])
        return f"Volume {'up' if key in ('volume up', 'louder') else 'down'}."
    if key is None:
        raise ValueError(f"I don't know the media action '{action}'")
    _send([(_MEDIA[key], 0), (_MEDIA[key], _KEYUP)])
    return {"play": "Playing.", "resume": "Resuming.", "pause": "Paused.", "play pause": "Done.",
            "next": "Next track.", "skip": "Skipped.", "previous": "Previous track.", "back": "Going back.",
            "stop": "Stopped.", "mute": "Sound muted.", "unmute": "Sound back on."}[key]


def set_volume(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    down, up = _MEDIA["volume down"], _MEDIA["volume up"]
    events = [e for _ in range(100 // _VOL_STEP) for e in ((down, 0), (down, _KEYUP))]  # to zero first
    events += [e for _ in range(round(percent / _VOL_STEP)) for e in ((up, 0), (up, _KEYUP))]
    _send(events)
    return f"Volume set to {percent} percent."


def foreground_title() -> str:
    if apps._user32 is None:
        return ""
    import ctypes
    buf = ctypes.create_unicode_buffer(512)
    apps._user32.GetWindowTextW(apps._user32.GetForegroundWindow(), buf, 512)
    return buf.value.strip()


def foreground() -> int:
    return apps._user32.GetForegroundWindow() if apps._user32 else 0


def wait_for_new_window(before: int, timeout: float = 6.0) -> bool:
    """After opening an app: wait until a different window has focus (the app's), so typing lands in it."""
    end = time.time() + timeout
    while time.time() < end:
        if foreground() not in (0, before):
            time.sleep(0.4)  # the window is up; give it a moment to be ready for input
            return True
        time.sleep(0.1)
    return False


def switch_window(name: str) -> str:
    want = re.sub(r"^(?:to |the )+", "", str(name).lower()).strip()
    if not want:
        return "Which window should I switch to?"
    windows = apps._windows()
    hits = apps._pick(want, windows)
    if not hits:
        open_now = ", ".join(sorted({apps._label(w) for w in windows})) or "nothing"
        return f"I couldn't find a window called '{name}'. Open now: {open_now}."
    hwnd = hits[0]["hwnd"]
    u = apps._user32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows only lets the app the user last touched take focus; a tap of Alt counts as touching
    _send([(_NAMED["alt"], 0), (_NAMED["alt"], _KEYUP)])
    u.SetForegroundWindow(hwnd)
    return f"Switched to {apps._label(hits[0])}."


def look_at_screen(question: str) -> str:
    """Screenshot the screen and ask a vision model about it. Nothing leaves this computer."""
    model = vision_model()
    if not model:
        return ("None of my installed models can see images. Install one with vision, like gemma3 or "
                "qwen2.5vl, and I can look at your screen.")
    from PIL import ImageGrab
    shot = ImageGrab.grab()
    shot.thumbnail((1280, 1280))
    buf = io.BytesIO()
    shot.save(buf, format="PNG")
    q = str(question).strip() or "What's on the screen?"
    msg = llm.client.chat(model=model, think=False, messages=[{
        "role": "user", "images": [base64.b64encode(buf.getvalue()).decode()],
        "content": f"This is a screenshot of the user's screen. {q}\nAnswer in one to three short spoken "
                   "sentences, no markdown. Only describe what you can actually see."}],
        options={"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX, "num_predict": llm.REPLY_TOKENS}).message
    return (msg.content or "").strip() or "I couldn't make anything out on the screen."


def vision_model() -> str | None:
    """The current brain if it can see, else any installed model that can."""
    if "vision" in llm.capabilities(config.OLLAMA_MODEL):
        return config.OLLAMA_MODEL
    import brains
    return next((m["name"] for m in brains.installed() if "vision" in llm.capabilities(m["name"])), None)


def run_shell(command: str, timeout: float = 30) -> str:
    """Run one PowerShell command and return what it printed (cut short if long)."""
    if not str(command).strip():
        return "What command should I run?"
    if sys.platform != "win32":
        raise OSError("run_shell is Windows-only for now")
    encoded = base64.b64encode(str(command).encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return f"The command was still running after {timeout:.0f} seconds, so I stopped waiting."
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    text = out + (("\nErrors: " + err) if err else "")
    if len(text) > 1500:
        text = text[:1500] + " ... (cut short)"
    if r.returncode != 0:
        return f"The command failed (exit code {r.returncode}). {text or 'It printed nothing.'}"
    return text or "Done; the command printed nothing."
