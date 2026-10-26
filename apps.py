"""Opens and closes programs on this Windows computer by name: a few built-ins by their everyday names,
then anything with a Start menu or desktop shortcut, matched loosely ("vs code" finds "Visual Studio Code").
It only ever launches shortcuts that are already there — never a path or command from the request — and
only closes apps with a window open, asking them to close the way the X button does (never killing them)."""
import ctypes
import difflib
import os
import re
import subprocess
from pathlib import Path

# everyday name -> what to launch
_BUILTIN = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
    "file explorer": "explorer.exe", "explorer": "explorer.exe", "files": "explorer.exe",
    "task manager": "taskmgr.exe", "settings": "ms-settings:", "control panel": "control.exe",
    "command prompt": "cmd.exe", "cmd": "cmd.exe", "terminal": "wt.exe", "powershell": "powershell.exe",
    "snipping tool": "ms-screenclip:", "camera": "microsoft.windows.camera:", "store": "ms-windows-store:",
    "clock": "ms-clock:", "mail": "outlookmail:", "calendar": "outlookcal:",
}
_SKIP = re.compile(r"uninstall|readme|help|documentation|release notes|website|license", re.I)
_FILLER = re.compile(r"\b(?:the|app|application|program|please|open|launch|start|up|my)\b", re.I)
_CLOSE_FILLER = re.compile(r"\b(?:the|app|application|program|window|please|close|quit|exit|shut|down|kill|my)\b",
                           re.I)
# everyday name -> the process its window belongs to, where that isn't obvious from the name
_PROCESS = {
    "calculator": "CalculatorApp", "calc": "CalculatorApp", "paint": "mspaint", "file explorer": "explorer",
    "files": "explorer", "task manager": "Taskmgr", "settings": "SystemSettings", "terminal": "WindowsTerminal",
    "snipping tool": "SnippingTool", "camera": "WindowsCamera", "store": "WinStore.App", "clock": "Time",
    "edge": "msedge", "word": "WINWORD", "excel": "EXCEL", "powerpoint": "POWERPNT", "vs code": "Code",
}


def open_app(name: str) -> str:
    want = _FILLER.sub(" ", str(name).lower()).strip()
    want = re.sub(r"\s+", " ", want)
    if not want:
        return "Which app should I open?"
    if want in _BUILTIN:
        return _launch(_BUILTIN[want], want)
    shortcuts = _shortcuts()
    names = {k.lower(): k for k in shortcuts}
    match = _match(want, names)
    if not match:
        close = difflib.get_close_matches(want, list(names), n=3, cutoff=0.4)
        return (f"I couldn't find an app called '{name}'."
                + (f" Did you mean {', '.join(names[c] for c in close)}?" if close else ""))
    return _launch(str(shortcuts[match]), match)


def _launch(target: str, label: str) -> str:
    try:
        os.startfile(target)
    except OSError as e:
        return f"I couldn't open {label}: {e.strerror or e}"
    return f"Opening {label}."


def close_app(name: str) -> str:
    want = re.sub(r"\s+", " ", _CLOSE_FILLER.sub(" ", str(name).lower())).strip()
    if not want:
        return "Which app should I close?"
    windows = _windows()
    if not windows:
        return "I couldn't see any open apps."
    hits = _pick(want, windows)
    if not hits:
        open_now = ", ".join(sorted({_label(w) for w in windows}))
        return f"I couldn't find an open app called '{name}'. Open now: {open_now}."
    for w in hits:
        _user32.PostMessageW(w["hwnd"], _WM_CLOSE, 0, 0)
    return f"Closing {_label(hits[0])}."


def _pick(want: str, windows: list) -> list:
    """The open windows meant by what was said. A window is known by its process ("chrome") and its
    title ("notes.txt - Notepad"); the process comes first so a document called "chrome tips" open in
    Word doesn't close Word. Every window of the matched process closes, or the one matched title."""
    by_process = {}
    for w in windows:
        by_process.setdefault(w["process"].lower(), []).append(w)
    process = _PROCESS.get(want, "").lower() or _match(want, {k: k for k in by_process if k != _FRAME_HOST})
    if process in by_process and process != _FRAME_HOST:
        return by_process[process]
    titles = {w["title"].lower(): w for w in windows}
    title = (next((k for k in titles if k.endswith(" - " + want) or k == want), None)
             or next((k for k in titles if want in k), None) or _match(want, {k: k for k in titles}))
    return [titles[title]] if title else []


def _label(w: dict) -> str:
    """What to call a window's app: the end of its title ("notes.txt - Notepad" -> "Notepad")."""
    return w["title"].rsplit(" - ", 1)[-1].strip() or w["process"]


_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
_WM_CLOSE = 0x0010
_FRAME_HOST = "applicationframehost"  # hosts every Store app's window (Calculator, Settings...): never match by it
# the desktop and taskbar are windows too, and closing the desktop asks to shut down
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _windows() -> list:
    """Every app window on screen: its handle, title and process name. Leaves out Jarvis itself and the
    console it runs in, so 'close python' can't end the conversation."""
    if _user32 is None:
        return []
    from ctypes import wintypes
    kernel32, dwmapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("dwmapi")
    mine, console = {os.getpid(), os.getppid()}, kernel32.GetConsoleWindow()
    found = []

    def visit(hwnd, _):
        title, cls = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, title, 512)
        _user32.GetClassNameW(hwnd, cls, 256)
        cloaked = wintypes.DWORD()
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))  # DWMWA_CLOAKED
        if (not title.value.strip() or not _user32.IsWindowVisible(hwnd) or _user32.GetWindow(hwnd, 4)  # GW_OWNER
                or cloaked.value or cls.value in _SHELL_CLASSES or hwnd == console):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in mine:
            return True
        found.append({"hwnd": hwnd, "title": title.value.strip(), "process": _process_name(kernel32, pid.value)})
        return True

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    _user32.EnumWindows(proc, 0)
    return found


def _process_name(kernel32, pid: int) -> str:
    """'chrome' for chrome.exe; empty if Windows won't say."""
    from ctypes import wintypes
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return Path(buf.value).stem if ok else ""
    finally:
        kernel32.CloseHandle(handle)


def _match(want: str, names: dict):
    """The best of these names for what was said: exact, then starts with, contains, sounds close, initials."""
    return (names.get(want)
            or next((names[k] for k in names if k.startswith(want)), None)
            or next((names[k] for k in names if want in k), None)
            or next((names[k] for k in difflib.get_close_matches(want, list(names), n=1, cutoff=0.6)), None)
            or _initials(want, names))


_store_apps = {}


def _start_apps() -> dict:
    """App name -> shell:AppsFolder target for everything in the Start menu's app list, including
    Microsoft Store apps like Spotify, which have no shortcut file to find. Cached: it takes a second."""
    if not _store_apps:
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"],
                               capture_output=True, text=True, timeout=20)
            for line in r.stdout.splitlines():
                name, _, app_id = line.partition("\t")
                if name.strip() and app_id.strip() and not _SKIP.search(name):
                    _store_apps[name.strip()] = "shell:AppsFolder\\" + app_id.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return _store_apps


def _shortcuts() -> dict:
    """Shortcut name -> .lnk/.url path, from the Start menus (yours and everyone's) and desktops,
    plus the Start menu's own app list (see _start_apps)."""
    roots = [Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
             Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
             Path.home() / "Desktop", Path(os.environ.get("PUBLIC", "")) / "Desktop"]
    found = {}
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() in (".lnk", ".url") and not _SKIP.search(p.stem):
                found.setdefault(p.stem, p)
    for name, target in _start_apps().items():
        found.setdefault(name, target)
    return found


def _initials(want: str, names: dict):
    """'vs code' -> 'Visual Studio Code': each spoken word starts a word of the name, in order."""
    words = want.split()
    for k in names:
        parts = k.split()
        i = 0
        for w in words:
            while i < len(parts) and not (parts[i].startswith(w) or (len(w) <= 3 and _abbrev(w, parts[i:]))):
                i += 1
            if i == len(parts):
                break
            i += len(w) if len(w) <= 3 and _abbrev(w, parts[i:]) and not parts[i].startswith(w) else 1
        else:
            return names[k]
    return None


def _abbrev(w: str, parts: list) -> bool:
    return len(parts) >= len(w) and all(parts[j].startswith(c) for j, c in enumerate(w))
