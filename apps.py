"""Opens programs on this Windows computer by name: a few built-ins by their everyday names, then
anything with a Start menu or desktop shortcut, matched loosely ("vs code" finds "Visual Studio Code").
It only ever launches shortcuts that are already there — never a path or command from the request."""
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


def open_app(name: str) -> str:
    want = _FILLER.sub(" ", str(name).lower()).strip()
    want = re.sub(r"\s+", " ", want)
    if not want:
        return "Which app should I open?"
    if want in _BUILTIN:
        return _launch(_BUILTIN[want], want)
    shortcuts = _shortcuts()
    names = {k.lower(): k for k in shortcuts}
    match = (names.get(want)
             or next((names[k] for k in names if k.startswith(want)), None)
             or next((names[k] for k in names if want in k), None)
             or next((names[k] for k in difflib.get_close_matches(want, list(names), n=1, cutoff=0.6)), None)
             or _initials(want, names))
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
