"""Fingers: type mode, like Wispr Flow but local and free. What you say is typed into whatever window
has focus (your editor, browser, chat), transcribed by the same local Whisper the ears use.

Two ways in:
  - hold the hotkey (TYPE_HOTKEY, right Ctrl by default), speak, let go: it's typed at the cursor
  - "hey Jarvis" -> "type mode start": every phrase is typed as you say it, until "type mode done"
    (or TYPE_IDLE_SECONDS without speech)
Saying "new line" or "new paragraph" breaks the line. The text is pasted (clipboard + Ctrl+V, your
clipboard is put back), so no extra packages, but it can't reach apps running as administrator
unless JARVIS does too."""
import re
import sys
import threading
import time

import config

MIN_HOLD_SECONDS = 0.3  # a shorter tap of the hotkey is a normal keypress (Ctrl+C), not dictation
_POLL = 0.02

# hotkey names for TYPE_HOTKEY; combine with "+", e.g. "ctrl+win" or "right alt"
_KEYS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "left ctrl": 0xA2, "right ctrl": 0xA3,
         "left alt": 0xA4, "right alt": 0xA5, "left shift": 0xA0, "right shift": 0xA1, "right win": 0x5C,
         "scroll lock": 0x91, "pause": 0x13, "insert": 0x2D, "caps lock": 0x14,
         **{f"f{n}": 0x6F + n for n in range(1, 25)}}

# "type mode done" ends voice type mode, at the end of a phrase. Whisper also writes "tight mode",
# "type mod" and "type mode store" (stop)
_MODE = r"(?:type|typing|tight|tied?|time|dictation)[\s-]*mod(?:e|es)?"
END = re.compile(r"[\s,]*(?:(?:hey|ok(?:ay)?)\W+)?(?:jarvis\W+)?"
                 rf"(?:{_MODE}[\s,.]*(?:done|don|stop|stopped|store|off|end|ended|over|finish|finished|complete|completed)|stop typing|"
                 rf"(?:end|exit|close|stop)\s+{_MODE})\W*$", re.I)
# Whisper's stock phrases for near-silence and background sound; never something to type
GHOSTS = {"you", "thanks for watching", "thank you for watching", "uh", "um", "hmm", "bye"}


def parse_hotkey(spec: str) -> list[int]:
    """Virtual-key codes for "right ctrl" / "ctrl+win"; [] for "off" or "". Raises ValueError on a typo."""
    spec = spec.strip().lower()
    if spec in ("", "off", "none", "false"):
        return []
    try:
        return [_KEYS[k.strip()] for k in spec.split("+")]
    except KeyError as e:
        raise ValueError(f"unknown key {e} in TYPE_HOTKEY; use one of: {', '.join(_KEYS)}") from None


def clean(text: str) -> str:
    """What to type for one phrase: spoken "new line" / "new paragraph" become line breaks, and a
    trailing space so the next phrase doesn't run into this one. "" for nothing or a Whisper ghost."""
    text = text.strip()
    if text.lower().strip(" .!?,") in GHOSTS:
        return ""
    text = re.sub(r"[ ,]*\bnew paragraph\b[.,!?]?[ ]*", "\n\n", text, flags=re.I)
    text = re.sub(r"[ ,]*\bnew line\b[.,!?]?[ ]*", "\n", text, flags=re.I)
    if not text.strip():
        return text if "\n" in text else ""
    return text if text.endswith("\n") else text + " "


def split_end(text: str) -> tuple[str, bool]:
    """(the phrase without a closing "type mode done", whether it was there)."""
    m = END.search(text)
    return (text[:m.start()], True) if m else (text, False)


def dictate(listen, type_text, idle: float = None) -> str:
    """Voice type mode: type each phrase as soon as it's transcribed. listen(wait) is ears.listen
    without flushing, so words said while the last phrase was being transcribed aren't lost.
    Returns "done" (said "type mode done") or "idle" (nothing said for `idle` seconds)."""
    idle = config.TYPE_IDLE_SECONDS if idle is None else idle
    last = time.monotonic()
    while True:
        text, _ = listen(min(10.0, idle))
        if not text:
            if time.monotonic() - last >= idle:
                return "idle"
            continue
        last = time.monotonic()
        text, done = split_end(text)
        print(f"  [type] {text!r}" + (" (done)" if done else ""))
        if clean(text):
            type_text(clean(text))
        if done:
            return "done"


def start_hotkey(ears, spec: str = None) -> threading.Thread | None:
    """Hold-to-talk on a background thread: while the hotkey is held the mic goes to dictation only
    (so "hey Jarvis" can't fire mid-sentence), and on release the text is typed at the cursor."""
    keys = parse_hotkey(config.TYPE_HOTKEY if spec is None else spec)
    if not keys:
        return None
    if sys.platform != "win32":
        print("  [type] the type mode hotkey only works on Windows for now")
        return None
    t = threading.Thread(target=hold_to_talk, args=(ears, keys), daemon=True, name="type-hotkey")
    t.start()
    print(f"  [type] hold {config.TYPE_HOTKEY if spec is None else spec} to dictate into any app")
    return t


def hold_to_talk(ears, keys, is_down=None, typer=None, once=False):
    is_down, typer = is_down or _is_down, typer or type_text
    while True:
        while not all(is_down(k) for k in keys):
            time.sleep(_POLL)
        ears.start_tap()
        t0 = time.monotonic()
        print("  [type] listening... let go to type it")
        while all(is_down(k) for k in keys):
            time.sleep(_POLL)
        audio = ears.stop_tap()
        held = time.monotonic() - t0
        while any(is_down(k) for k in keys):  # fully let go first, or the paste becomes Ctrl+Ctrl+V
            time.sleep(_POLL)
        try:
            if held < MIN_HOLD_SECONDS or audio is None:
                print("  [type] too short, ignored")
            else:
                text, _ = ears.transcribe(audio)
                print(f"  [type] {text!r}")
                if clean(text):
                    typer(clean(text))
        except Exception as e:  # one bad recording mustn't kill the hotkey for the rest of the session
            print(f"  [type] couldn't type it: {e!r}")
        if once:
            return


# --- Windows clipboard + keyboard (ctypes, no extra packages) ---
# Text goes in by pasting, like Wispr Flow: typing it key by key with SendInput drops and repeats
# letters in some apps (Windows 11 Notepad among them). Your clipboard's text is put back afterwards.

_KEYEVENTF_KEYUP = 0x2
_VK_CONTROL, _VK_V = 0x11, 0x56
_CF_UNICODETEXT, _GMEM_MOVEABLE = 13, 0x2
PASTE_SETTLE_SECONDS = 0.25  # how long the app gets to read the clipboard before it's put back


def paste_keys() -> list[tuple[int, int, int]]:
    """(virtual key, scan code, flags) for Ctrl+V."""
    return [(_VK_CONTROL, 0, 0), (_VK_V, 0, 0), (_VK_V, 0, _KEYEVENTF_KEYUP), (_VK_CONTROL, 0, _KEYEVENTF_KEYUP)]


def type_text(text: str):
    """Put `text` into the focused window at the cursor, by pasting it."""
    if sys.platform != "win32":
        raise OSError("typing into other apps only works on Windows for now")
    import ctypes
    if not text:
        return
    before = get_clipboard()
    set_clipboard(text.replace("\r\n", "\n").replace("\n", "\r\n"))  # Windows line breaks
    inputs = _input_array(paste_keys())
    sent = ctypes.windll.user32.SendInput(len(inputs), inputs, ctypes.sizeof(inputs[0]))
    time.sleep(PASTE_SETTLE_SECONDS)
    if before is not None:
        set_clipboard(before)  # already in your clipboard history once; don't add it again
    if sent != len(inputs):
        raise OSError("Windows blocked the paste; is that window running as administrator?")


def _win():
    """user32 and kernel32 with the pointer-sized types the clipboard calls need on 64-bit."""
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetClipboardData.restype = k.GlobalAlloc.restype = k.GlobalLock.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    return ctypes, u, k


def _open_clipboard(u):
    for _ in range(20):  # another app (a clipboard manager) may have it open for a moment
        if u.OpenClipboard(None):
            return
        time.sleep(0.01)
    raise OSError("the clipboard is busy")


def get_clipboard() -> str | None:
    """The clipboard's text, or None if it holds no text (an image, files, nothing)."""
    ctypes, u, k = _win()
    _open_clipboard(u)
    try:
        h = u.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return None
        p = k.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def set_clipboard(text: str):
    """Put `text` on the clipboard, kept out of clipboard history (Win+V) and cloud clipboard."""
    ctypes, u, k = _win()
    data = (text + "\0").encode("utf-16-le")
    _open_clipboard(u)
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        if not u.SetClipboardData(_CF_UNICODETEXT, h):
            raise OSError("couldn't set the clipboard")
        for name in ("ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory"):
            fmt = u.RegisterClipboardFormatW(name)
            flag = k.GlobalAlloc(_GMEM_MOVEABLE, 4)  # a DWORD 0 for CanIncludeInClipboardHistory
            ctypes.memset(k.GlobalLock(flag), 0, 4)
            k.GlobalUnlock(flag)
            u.SetClipboardData(fmt, flag)
    finally:
        u.CloseClipboard()


def _is_down(vk: int) -> bool:
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _input_array(evs):
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):  # the union's biggest member: SendInput checks the struct size
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    arr = (INPUT * len(evs))()
    for i, (vk, scan, flags) in enumerate(evs):
        arr[i].type = 1  # INPUT_KEYBOARD
        arr[i].u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return arr
