"""Builds small websites in the workspace and serves them on this computer only (127.0.0.1).
Two engines:
  local  — the coder model plans the site as JSON, writes it one file per call (so each call stays
           small enough for a 7B model and an 8k context), then checks links and syntax and sends
           any problem back to be fixed. Free and private.
  claude — Claude Code (`claude -p`) does the build in the project folder, allowed only its file
           tools (no shell), with a spending cap. Better for anything bigger; the brief goes to the cloud.
Builds run in the background and announce themselves when done. Every question (go ahead? send to
the cloud? replace the old one?) is asked before that, because only the main loop may use the mic."""
import atexit
import html.parser
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import config
import llm
import selfcode
from files import Workspace

FILE_TOKENS = 3500  # one site file
PLAN_TOKENS = 600
FIX_ROUNDS = 2
MAX_FILES = 6
_PATH = re.compile(r"[a-z0-9_-]+(?:/[a-z0-9_-]+)*\.(?:html|css|js)")
_SLUG = re.compile(r"[^a-z0-9.-]+")
# what makes a request too big for the local model to do well
_BIG = re.compile(r"\b(?:backend|database|login|sign ?up|accounts?|users?|payments?|checkout|react|vue|angular|"
                  r"next\.?js|api|dashboard|admin|full[- ]stack|e-?commerce|shop|store)\b", re.I)

_PLAN = """You plan small static websites for a local coding assistant. The user asked for:
{brief}

Plan the files. Rules:
- Plain HTML, CSS and JavaScript only: no frameworks, no build step, no external libraries, CDNs or fonts.
- No image files: use CSS, gradients, inline SVG or emoji instead.
- Keep it small: usually index.html and style.css, plus script.js only if something is interactive.
  At most {max_files} files. index.html is required.
- Paths are lowercase, like "index.html", "about.html", "style.css", "script.js".
- name: a short lowercase folder name for the project, like "insub.com" or "bakery-site".
- Each file's purpose says exactly what goes in it (sections, colours, behaviour) and which other
  files it links to, so each can be written on its own."""

_WRITE = """You are writing one file of a small static website.
The user asked for: {brief}

The whole plan:
{plan}

Write the complete contents of {path} ({purpose}).
Rules: plain HTML/CSS/JS, no external libraries, CDNs, fonts or image files. Link only to files in the
plan, with relative paths. Make it look polished and modern, and work on phones.
{context}
Reply with ONLY the raw contents of {path}. No markdown fences, no commentary."""


class Builder:
    def __init__(self, confirm, speak):
        self.confirm = confirm
        self.speak = speak
        self.root = Path(config.WORKSPACE_DIR) / "sites"
        self.servers = {}  # project name -> (process, port)
        self.busy = None  # what's being built right now; one build at a time
        atexit.register(self.stop_all)

    # --- what the tools call ---

    def build(self, brief: str, engine: str) -> str:
        """engine: "local", "claude" or "auto" (local unless the request sounds too big for it)."""
        if self.busy:
            return f"I'm still building {self.busy}. I'll tell you when it's done."
        brief = _clean_brief(brief)
        if engine == "auto":
            engine = "claude" if _BIG.search(brief) and _claude_path() else "local"
        if engine == "claude" and not _claude_path():
            return "Claude Code isn't installed on this computer, so I can only build it myself."
        who = "Claude" if engine == "claude" else "I"
        if not self.confirm(f"{who} will build: {brief}. Shall I start?"):
            return "Alright, I haven't started anything."
        if engine == "claude" and config.CLOUD_CONFIRM and not self.confirm(
                "That sends the request to Claude in the cloud. Is that okay?"):
            return "Okay, nothing was sent. I can build it myself instead if you like."
        self.busy = brief[:60]
        threading.Thread(target=self._background, args=(brief, engine), daemon=True).start()
        return (f"{'Claude is' if engine == 'claude' else 'I am'} building it now. That takes a few minutes; "
                "I'll tell you when it's ready and open it in your browser.")

    def edit(self, project: str, change: str, engine: str) -> str:
        if self.busy:
            return f"I'm still building {self.busy}. Ask me again when that's done."
        folder = self._find(project)
        if not folder:
            return self._which_project(project)
        if engine == "auto":
            engine = "local"
        if engine == "claude" and not _claude_path():
            return "Claude Code isn't installed on this computer, so I can only change it myself."
        if engine == "claude" and config.CLOUD_CONFIRM and not self.confirm(
                f"That sends the change for {folder.name} to Claude in the cloud. Is that okay?"):
            return "Okay, nothing was sent."
        self.busy = f"the change to {folder.name}"
        threading.Thread(target=self._background_edit, args=(folder, change, engine), daemon=True).start()
        return f"Working on {folder.name} now. I'll tell you when the change is in."

    def serve(self, project: str) -> str:
        folder = self._find(project)
        if not folder:
            return self._which_project(project)
        if not (folder / "index.html").exists():
            return f"{folder.name} has no index.html yet, so there's nothing to show."
        url = self._serve(folder)
        webbrowser.open(url)
        return f"{folder.name} is running at {url}, and I've opened it in your browser."

    def stop(self, project: str) -> str:
        folder = self._find(project)
        if not folder or folder.name not in self.servers:
            return "That site isn't running."
        proc, _ = self.servers.pop(folder.name)
        proc.terminate()
        return f"Stopped {folder.name}."

    def projects(self) -> str:
        names = sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.exists() else []
        if not names:
            return f"No website projects yet. They'll go in {self.root}."
        running = [f"{n} at http://127.0.0.1:{self.servers[n][1]}" for n in names if n in self.servers]
        return (f"Projects in {self.root}: {', '.join(names)}."
                + (f" Running: {', '.join(running)}." if running else ""))

    def stop_all(self):
        for proc, _ in self.servers.values():
            proc.terminate()
        self.servers.clear()

    # --- background work ---

    def _background(self, brief: str, engine: str):
        try:
            if engine == "claude":
                folder = self._new_folder(_project_name(brief))
                summary = _run_claude(folder, _CLAUDE_BUILD.format(brief=brief))
            else:
                folder, summary = self._build_local(brief)
            if not (folder / "index.html").exists():
                self.speak(f"The build finished, {config.USER_TITLE}, but there's no index.html, so there's "
                           f"nothing to open. {summary}")
                return
            url = self._serve(folder)
            webbrowser.open(url)
            self.speak(f"Your site {folder.name} is ready, {config.USER_TITLE}, at {url}. {summary}")
        except Exception as e:  # a background thread has nobody else to report to
            print(f"  [build] failed: {e}")
            self.speak(f"Sorry {config.USER_TITLE}, the build failed: {str(e)[:200]}")
        finally:
            self.busy = None

    def _background_edit(self, folder: Path, change: str, engine: str):
        try:
            if engine == "claude":
                summary = _run_claude(folder, _CLAUDE_EDIT.format(change=change))
            else:
                summary = self._edit_local(folder, change)
            running = folder.name in self.servers
            self.speak(f"Done, {config.USER_TITLE}. {summary}"
                       + (" Refresh the page to see it." if running else ""))
        except Exception as e:
            print(f"  [build] edit failed: {e}")
            self.speak(f"Sorry {config.USER_TITLE}, that change failed: {str(e)[:200]}")
        finally:
            self.busy = None

    def _build_local(self, brief: str):
        model = selfcode._coder_model()
        plan = _plan(brief, model)
        print(f"  [build] plan: {json.dumps(plan)}")
        folder = self._new_folder(plan["name"])
        ws = Workspace(folder, confirm=lambda q: True)  # rewrites inside its own new folder are fine
        plan_text = "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan["files"])
        # index.html first, so the CSS and JS are written against the real classes and ids
        for f in sorted(plan["files"], key=lambda f: f["path"] != "index.html"):
            print(f"  [build] writing {f['path']}")
            ws.write(f["path"], _write_file(brief, plan_text, f, folder, model))
        problems = _check(folder)
        for round_ in range(FIX_ROUNDS):
            if not problems:
                break
            print(f"  [build] fix round {round_ + 1}: {problems}")
            for path, problem in problems.items():
                ws.write(path, _write_file(brief, plan_text, _plan_entry(plan, path), folder, model,
                                           fix=problem))
            problems = _check(folder)
        if problems:
            return folder, f"A few things may not work yet: {'; '.join(problems.values())[:200]}"
        return folder, f"I wrote {len(plan['files'])} files."

    def _edit_local(self, folder: Path, change: str) -> str:
        model = selfcode._coder_model()
        files = sorted(str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*")
                       if p.suffix in (".html", ".css", ".js"))
        if not files:
            raise ValueError(f"{folder.name} has no HTML, CSS or JavaScript files to change")
        pick = _json(model, f"A website has these files: {', '.join(files)}.\nThe user wants this change: {change}\n"
                            "Which ONE file should be edited to make it?",
                     {"type": "object", "properties": {"path": {"type": "string", "enum": files}},
                      "required": ["path"]})
        path = pick["path"]
        print(f"  [build] editing {path}")
        old = (folder / path).read_text(encoding="utf-8", errors="replace")
        new = _code(model, f"Here is {path} from a small website:\n\n{old[:12000]}\n\n"
                           f"Make this change: {change}\nKeep everything else the same. "
                           f"Reply with ONLY the complete new contents of {path}, no markdown fences.")
        Workspace(folder, confirm=lambda q: True).write(path, new)
        problems = _check(folder)
        if path in problems:  # one fix attempt, with the error
            new = _code(model, f"This version of {path} has a problem: {problems[path]}\n\n{new}\n\n"
                               f"Reply with ONLY the complete fixed contents of {path}.")
            Workspace(folder, confirm=lambda q: True).write(path, new)
        return f"I changed {path} in {folder.name}."

    # --- helpers ---

    def _new_folder(self, name: str) -> Path:
        """A fresh project folder; adds -2, -3... rather than overwrite an existing project."""
        name = _slug(name) or "site"
        folder, n = self.root / name, 2
        while folder.exists():
            folder, n = self.root / f"{name}-{n}", n + 1
        folder.mkdir(parents=True)
        return folder

    def _find(self, project: str) -> Path | None:
        if not self.root.exists():
            return None
        names = [p for p in self.root.iterdir() if p.is_dir()]
        key = _slug(project)
        if not key:  # "show me the site" with one project, or the newest one
            return max(names, key=lambda p: p.stat().st_mtime, default=None)
        return (next((p for p in names if p.name == key), None)
                or next((p for p in names if key in p.name or p.name in key), None))

    def _which_project(self, project: str) -> str:
        return f"I couldn't find a project called '{project}'. {self.projects()}"

    def _serve(self, folder: Path) -> str:
        if folder.name in self.servers and self.servers[folder.name][0].poll() is None:
            return f"http://127.0.0.1:{self.servers[folder.name][1]}"
        port = _free_port()
        # 127.0.0.1 only: nothing on the network, let alone the internet, can reach it
        proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                                 "--directory", str(folder)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.servers[folder.name] = (proc, port)
        return f"http://127.0.0.1:{port}"


_CLAUDE_BUILD = """Build this website in the current folder: {brief}

Make it a static site that opens straight from index.html with a simple static file server: plain
HTML, CSS and JavaScript, no build step and no npm packages. Keep every file inside this folder.
Make it polished, modern and responsive. When done, reply with one or two short sentences, suitable
to be read aloud, saying what you built."""

_CLAUDE_EDIT = """Change the website in the current folder: {change}

Keep it a static site (no build step, no npm packages) and keep every file inside this folder.
When done, reply with one or two short sentences, suitable to be read aloud, saying what you changed."""


def _run_claude(folder: Path, prompt: str) -> str:
    """Runs Claude Code headless in the project folder. It may only read and write files there:
    no shell, no web, and anything else it tries is refused because nobody answers its prompts."""
    print(f"  [build] running Claude Code in {folder}")
    r = subprocess.run([_claude_path(), "-p", prompt, "--output-format", "json",
                        "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                        "--allowedTools", "Read Write Edit Glob Grep",
                        "--max-budget-usd", str(config.CLAUDE_BUILD_BUDGET_USD)],
                       cwd=folder, capture_output=True, text=True, encoding="utf-8",
                       timeout=config.CLAUDE_BUILD_TIMEOUT, stdin=subprocess.DEVNULL)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or f"Claude Code exited with {r.returncode}")
    cost = out.get("total_cost_usd")
    print(f"  [build] Claude Code done, cost ${cost}")
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result") or out.get("subtype"))[:300])
    return str(out.get("result", "")).strip()[:400] + (f" It cost about {cost:.2f} dollars." if cost else "")


def _claude_path():
    return shutil.which("claude")


def _plan(brief: str, model: str) -> dict:
    schema = {"type": "object", "properties": {
        "name": {"type": "string"}, "title": {"type": "string"},
        "files": {"type": "array", "maxItems": MAX_FILES, "items": {
            "type": "object", "properties": {"path": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["path", "purpose"]}}},
        "required": ["name", "title", "files"]}
    plan = _json(model, _PLAN.format(brief=brief, max_files=MAX_FILES), schema, PLAN_TOKENS)
    files, seen = [], set()
    for f in plan.get("files", [])[:MAX_FILES]:
        path = str(f.get("path", "")).strip().lower().lstrip("./")
        if _PATH.fullmatch(path) and path not in seen:
            seen.add(path)
            files.append({"path": path, "purpose": str(f.get("purpose", ""))})
    if "index.html" not in seen:
        files.insert(0, {"path": "index.html", "purpose": f"the main page: {brief}"})
    return {"name": _slug(plan.get("name", "")) or _slug(brief), "title": plan.get("title", ""), "files": files}


def _plan_entry(plan: dict, path: str) -> dict:
    return next((f for f in plan["files"] if f["path"] == path), {"path": path, "purpose": "part of the site"})


def _write_file(brief, plan_text, f, folder: Path, model, fix: str = "") -> str:
    context = ""
    index = folder / "index.html"
    if f["path"] != "index.html" and index.exists():  # CSS/JS must match the page's real classes and ids
        context = f"\nThis is the site's index.html, already written:\n{index.read_text(encoding='utf-8')[:6000]}\n"
    if fix:
        current = folder / f["path"]
        if current.exists():
            context += f"\nThe current {f['path']}:\n{current.read_text(encoding='utf-8')[:6000]}\n"
        context += f"\nIt has this problem, which you must fix: {fix}\n"
    return _code(model, _WRITE.format(brief=brief, plan=plan_text, path=f["path"], purpose=f["purpose"],
                                      context=context))


def _check(folder: Path) -> dict:
    """{file: problem} for broken links between files, JavaScript syntax errors and unbalanced CSS."""
    problems = {}
    existing = {str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*") if p.is_file()}
    for p in folder.rglob("*"):
        rel = str(p.relative_to(folder)).replace("\\", "/")
        text = p.read_text(encoding="utf-8", errors="replace") if p.suffix in (".html", ".css", ".js") else ""
        if p.suffix == ".html":
            if "<body" not in text.lower():
                problems[rel] = "it isn't a complete HTML page (no <body>)"
                continue
            links = _Links()
            links.feed(text)
            base = p.parent.relative_to(folder)
            missing = sorted({link for link in links.found
                              if str((base / link).as_posix()).lstrip("./") not in existing})
            if missing:
                problems[rel] = (f"it links to {', '.join(missing)}, which don't exist; the only files are "
                                 f"{', '.join(sorted(existing))}")
        elif p.suffix == ".js" and shutil.which("node"):
            r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True, timeout=15)
            if r.returncode:
                problems[rel] = "JavaScript syntax error: " + " ".join(r.stderr.strip().splitlines()[-3:])[:300]
        elif p.suffix == ".css" and text.count("{") != text.count("}"):
            problems[rel] = "its { and } braces don't match"
    return problems


class _Links(html.parser.HTMLParser):
    """Relative href/src links in a page: the files it expects to exist next to it."""

    def __init__(self):
        super().__init__()
        self.found = set()

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in ("href", "src") and v and not re.match(r"(?:[a-z]+:|//|#|\{)", v, re.I):
                link = v.split("#")[0].split("?")[0]
                if link and not link.endswith("/"):
                    self.found.add(link)


def _json(model: str, prompt: str, schema: dict, tokens: int = llm.JSON_TOKENS) -> dict:
    for _ in range(2):
        resp = llm.client.chat(model=model, format=schema, messages=[{"role": "user", "content": prompt}],
                               options={"temperature": 0.2, "num_ctx": config.OLLAMA_NUM_CTX,
                                        "num_predict": tokens})
        try:
            return json.loads(resp.message.content)
        except (ValueError, TypeError):
            continue
    raise RuntimeError("the coder model didn't give a usable plan")


def _code(model: str, prompt: str) -> str:
    resp = llm.client.chat(model=model, messages=[{"role": "user", "content": prompt}],
                           options={"temperature": 0.2, "num_ctx": config.OLLAMA_NUM_CTX,
                                    "num_predict": FILE_TOKENS})
    text = selfcode._strip_fences(resp.message.content or "")
    if not text.strip():
        raise RuntimeError("the coder model returned an empty file")
    return text + "\n"


def _clean_brief(text: str) -> str:
    """'Use your own ability to make a landing page for a coffee shop' -> 'a landing page for a coffee shop'."""
    text = re.sub(r"^\s*(?:(?:hey |ok |okay )?jarvis,?\s*)?(?:please\s+)?(?:(?:use|using|with|ask|let|get) "
                  r"(?:claude|the cloud|your own ability|your own skills?|yourself)(?: and| to)?\s*)?(?:can you\s+)?"
                  r"(?:please\s+)?(?:build|make|create|design|code)(?: me| us)?\s+", "", text, flags=re.I)
    text = re.sub(r"\s*,?\s*(?:using|with) (?:claude|the cloud|your own ability|your own skills?)\s*\.?$", "", text,
                  flags=re.I)
    return text.strip(" .") or text


def _project_name(brief: str) -> str:
    """A folder name from the request: 'insub.com' from "...a site for insub.com...", else its first words."""
    m = (re.search(r"\b([a-z0-9-]+\.(?:com|net|org|io|dev|app|co|uk|in|lk))\b", brief, re.I)
         or re.search(r"\b(?:called|named)\s+[\"']?([\w.-]+(?:\s+(?!(?:for|with|that|and|which|in|on)\b)[\w.-]+){0,2})",
                      brief, re.I))
    if m:
        return _slug(m.group(1))
    words = [w for w in re.findall(r"[a-z0-9]+", brief.lower())
             if w not in {"a", "an", "the", "build", "make", "create", "me", "website", "site", "web", "page",
                          "for", "with", "and", "use", "claude", "please", "new"}]
    return "-".join(words[:3]) or "site"


def _slug(text: str) -> str:
    return _SLUG.sub("-", str(text).lower()).strip("-.")[:40]


def _free_port(start: int = 8000) -> int:
    for port in range(start, start + 100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)):
                return port
    raise RuntimeError("no free port between 8000 and 8100")
