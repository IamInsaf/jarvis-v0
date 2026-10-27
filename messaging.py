"""Texting people on WhatsApp by voice: "text my mom: I will be late for school".
JARVIS keeps its own small contact list (WhatsApp's own contacts can't be read), tidies what you said
into a proper message, reads it back, and only after your yes opens the chat through a whatsapp://
link with the message filled in and presses Enter — and only while WhatsApp is the window in front,
so the keypress can't land anywhere else."""
import ctypes
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from ctypes import wintypes

import config
import llm

# what people call a contact -> the name it's saved under, so "mum" and "mother" find "mom"
_ALIASES = {"mum": "mom", "mommy": "mom", "mummy": "mom", "mother": "mom", "ma": "mom", "amma": "mom",
            "ammi": "mom", "dad": "dad", "daddy": "dad", "father": "dad", "papa": "dad", "pa": "dad",
            "abba": "dad", "thaththa": "dad", "bro": "brother", "sis": "sister"}
_NUMBER_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "six": "6", "seven": "7", "eight": "8", "nine": "9", "plus": "+", "double": "double",
                 "triple": "triple"}
# "text my mom: I'll be late" / "send a WhatsApp message to John saying hi" / "message dad that I'm home"
SEND = re.compile(r"^\s*(?:(?:hey |ok |okay )?jarvis,?\s*)?(?:please\s+)?(?:can you\s+)?(?:send\s+)?"
                   r"(?:an?\s+)?(?:text|message|whats ?app|msg)(?:\s+message)?\s+(?:to\s+)?(?:my\s+)?"
                   r"(?P<who>[a-z][\w'.-]*(?:\s+(?!saying\b|that\b|on\b|and\b|to\b|tell\b)[a-z][\w'.-]*)?)"
                   r"(?:\s+on\s+whats ?app)?\s*(?:[:,.-]|\bsaying\b|\bthat\b|\band say\b|\band tell (?:her|him|them)\b)"
                   r"\s*(?P<text>.+)$", re.I)
# "mom's number is +94 77 123 4567" / "save dad's number as ..." / "John's phone number is ..."
SAVE = re.compile(r"(?:save\s+)?(?:my\s+)?(?P<who>[a-z][\w.-]*(?:\s+[a-z][\w.-]*)?)'?s?\s+(?:phone\s+|mobile\s+|"
                   r"whats ?app\s+)?number\s+(?:is|as)\s+(?P<num>.+)$", re.I)

user32 = ctypes.windll.user32 if os.name == "nt" else None
kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
VK_RETURN, VK_MENU, VK_CONTROL, KEYUP = 0x0D, 0x12, 0x11, 0x0002
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
if user32:
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalAlloc.restype = ctypes.c_void_p


class Messenger:
    def __init__(self, db_path: str, confirm):
        self.confirm = confirm
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS contacts(name TEXT PRIMARY KEY, phone TEXT, added REAL)")
        self.db.commit()
        self.lock = threading.Lock()

    # --- contacts ---

    def save_contact(self, name: str, number: str, user_text: str = "") -> str:
        if m := SAVE.search(user_text or ""):  # the user's own words beat the model's reading of them
            name, number = m.group("who"), m.group("num")
        key = _key(name)
        phone = parse_number(number)
        if not key:
            return "Whose number is that?"
        if not phone:
            return (f"I didn't catch a phone number for {key}. Say it with the country code, "
                    "like 'plus nine four, seven seven, one two three...'.")
        if not phone.startswith("+"):
            return (f"I need the country code too, {config.USER_TITLE}: say {key}'s number starting with plus, "
                    "like plus nine four.")
        old = self.phone(key)
        if old and old != phone and not self.confirm(f"I already have {_spoken(old)} for {key}. Replace it?"):
            return f"Kept the old number for {key}."
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO contacts(name, phone, added) VALUES (?, ?, ?)",
                            (key, phone, time.time()))
            self.db.commit()
        return f"Saved {key}'s number as {_spoken(phone)}."

    def phone(self, name: str) -> str | None:
        row = self.db.execute("SELECT phone FROM contacts WHERE name = ?", (_key(name),)).fetchone()
        return row[0] if row else None

    def contacts(self) -> str:
        rows = self.db.execute("SELECT name FROM contacts ORDER BY name").fetchall()
        if not rows:
            return "I don't have anyone's number yet. Tell me, for example, \"Mom's number is plus nine four...\"."
        return "I have numbers for " + ", ".join(r[0] for r in rows) + "."

    def forget_contact(self, name: str) -> str:
        key = _key(name)
        with self.lock:
            n = self.db.execute("DELETE FROM contacts WHERE name = ?", (key,)).rowcount
            self.db.commit()
        return f"Removed {key}'s number." if n else f"I don't have a number for {key}."

    # --- sending ---

    def send(self, contact: str, text: str, user_text: str = "") -> str:
        if m := SEND.match(user_text or ""):  # "text my mom: ..." parsed directly, not left to the model
            contact, text = m.group("who"), m.group("text")
        key, text = _key(contact), str(text or "").strip()
        if not key:
            return "Who should I text?"
        if not text:
            return f"What should I say to {key}?"
        phone = self.phone(key)
        if not phone:
            return (f"I don't have a number for {key} yet. Tell me \"{key.capitalize()}'s number is\" "
                    "followed by the number with its country code, then ask me again.")
        message = tidy(text, key)
        if not self.confirm(f"Text {key} on WhatsApp: \"{message}\". Send it?"):
            return "Okay, I didn't send it."
        return _send_whatsapp(phone, message, key)


def tidy(text: str, contact: str) -> str:
    """'i will be late for school' -> 'I'm going to be late for school.' — same meaning, nothing added.
    Falls back to the words as heard if the model wanders off."""
    original = text.strip().strip("\"'")
    try:
        resp = llm.client.chat(model=config.OLLAMA_MODEL, think=config.OLLAMA_THINK, messages=[{"role": "user", "content": (
            f"The user dictated a text message for their {contact}. Rewrite it as the message they would type: "
            "first person, natural, correct grammar, punctuation and capitals. Keep the meaning exactly. Add "
            "nothing new: no greeting, sign-off, emoji or extra details unless they said them. If it's already "
            "fine, return it unchanged. Reply with the message only, no quotes.\n\n"
            f"Dictated: {original}")}],
            options={"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX, "num_predict": 120})
        out = (resp.message.content or "").strip().strip("\"'“”")
    except llm.ERRORS:
        return original
    if not out or len(out) > 2 * len(original) + 30 or "\n" in out:
        return original
    return out


def parse_number(said: str) -> str:
    """'plus nine four seven seven double one...' or '+94 77 112 3456' -> '+94771123456' ('' if no number)."""
    words = re.findall(r"\+|\d+|[a-z]+", str(said).lower())
    out, repeat = "", 1
    for w in words:
        w = _NUMBER_WORDS.get(w, w)
        if w in ("double", "triple"):
            repeat = 2 if w == "double" else 3
        elif w == "+":
            out += "+" if not out else ""
        elif w.isdigit():
            out += w[0] * (repeat - 1) + w
            repeat = 1
    digits = out.lstrip("+")
    return out if 7 <= len(digits) <= 15 else ""


def _send_whatsapp(phone: str, message: str, who: str) -> str:
    """Opens the chat with the message filled in, and presses Enter only once the message box is seen
    to hold exactly that message; then checks the box emptied. WhatsApp's message box isn't visible to
    Windows accessibility, so it's read by select-all + copy (the clipboard is put back afterwards)."""
    if not user32:
        return "Sending WhatsApp messages only works on Windows."
    link = f"whatsapp://send?phone={phone.lstrip('+')}&text={urllib.parse.quote(message)}"
    saved = _get_clipboard()
    before = user32.GetForegroundWindow()  # handed back focus once the message is out
    try:
        if not _find_window("WhatsApp"):
            # started cold, WhatsApp opened on its home screen and dropped the chat link: start it first
            os.startfile("whatsapp:")
            if not _wait_for_whatsapp(30):
                return "WhatsApp didn't start, so nothing was sent."
            time.sleep(6)  # loading the chats
        os.startfile(link)
        typed = False
        for attempt in range(2):
            deadline = time.time() + 10
            while time.time() < deadline and not typed:
                time.sleep(1)
                hwnd = _wait_for_whatsapp(5)
                typed = bool(hwnd) and _box_text(hwnd) == message.strip()
            if typed:
                break
            if attempt == 0:  # the link can get lost while WhatsApp is busy: open it once more
                os.startfile(link)
        if not typed:
            return ("I opened WhatsApp but couldn't see the message typed into the chat, so I didn't press "
                    "send. Nothing was sent.")
        if not _is_front(hwnd):  # something else took focus: Enter would go there instead
            return "Another window jumped in front, so I didn't press send. The message is waiting in WhatsApp."
        _press(VK_RETURN)
        time.sleep(1.5)
        if _box_text(hwnd) == message.strip():
            return "I pressed send but the message is still sitting in the box, so it may not have gone. Check WhatsApp."
        # minimized, not closed: closing could cut off a message still uploading, and stops notifications.
        # On any failure above it stays open, so the waiting message is there to see.
        time.sleep(1)
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        if before and before != hwnd and user32.IsWindow(before):
            user32.SetForegroundWindow(before)
        return f"Sent to {who} on WhatsApp: \"{message}\""
    finally:
        _set_clipboard(saved)


def _box_text(hwnd) -> str:
    """What the focused box in WhatsApp holds: select all, copy, read the clipboard."""
    if not _is_front(hwnd):
        return ""
    _set_clipboard("")
    _combo(ord("A"))
    _combo(ord("C"))
    time.sleep(0.3)
    return (_get_clipboard() or "").strip()


def _combo(vk: int):
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)


def _get_clipboard() -> str | None:
    for _ in range(10):  # another program may be holding the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr) if ptr else None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard(text: str | None):
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return
    try:
        user32.EmptyClipboard()
        if text:
            data = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(data)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, size)
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def _wait_for_whatsapp(seconds: float):
    """The WhatsApp window once it's in front (bringing it there if it opened behind), else None."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        front = user32.GetForegroundWindow()
        if _title(front).startswith("WhatsApp"):
            return front
        hwnd = _find_window("WhatsApp")
        if hwnd:
            _press(VK_MENU)  # Windows only lets a program take focus right after a keypress
            user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)
    return None


def _is_front(hwnd) -> bool:
    return user32.GetForegroundWindow() == hwnd


def _find_window(prefix: str):
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _title(hwnd).startswith(prefix):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else None


def _title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _press(vk: int):
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYUP, 0)


def _key(name: str) -> str:
    name = re.sub(r"^(?:my|the)\s+", "", str(name or "").strip().lower()).strip(" .,:'\"")
    name = re.sub(r"'s$", "", name)
    return _ALIASES.get(name, name)


def _spoken(phone: str) -> str:
    """'+94771234567' -> 'plus 9 4 7 7 1 2 3 4 5 6 7', so the voice reads it digit by digit."""
    return ("plus " if phone.startswith("+") else "") + " ".join(phone.lstrip("+"))
