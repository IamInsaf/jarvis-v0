"""Self-upgrade: lets JARVIS draft a new ability for itself when you ask for something it
can't do, test it, save it as an isolated plugin file, and load it — only with your explicit OK.
Never edits jarvis's own core files; a bad plugin can only ever be deleted, not break the rest.

The loop, built so a small local model can get there:
  draft (coder model, with a worked example) -> static safety check -> test run in a separate
  process -> on any failure, the error goes back to the model to fix (up to MAX_ATTEMPTS) ->
  you approve, hearing what the test returned -> saved as plugins/<tool name>.py -> git commit."""
import ast
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import ollama

import config
import llm
import router

PROJECT_DIR = Path(__file__).parent
PLUGIN_DIR = PROJECT_DIR / "plugins"
PLUGIN_DIR.mkdir(exist_ok=True)
HARNESS = PROJECT_DIR / "plugin_harness.py"
MAX_ATTEMPTS = 3
TEST_TIMEOUT = 30  # seconds; a plugin that calls a slow website still has to answer in this

_ALLOWED_IMPORTS = {"requests", "re", "json", "math", "random", "time", "datetime", "config"}
_BANNED_SUBSTRINGS = ("os.system", "subprocess", "shutil.rmtree")
# builtins that open files, run code, or reach things the import rules keep out
_BANNED_NAMES = {"eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals",
                 "vars", "open", "input", "breakpoint", "exit", "quit", "help", "memoryview", "__builtins__"}
# attributes that lead to the OS through an allowed module (config.os, requests.utils.os, ...)
_BANNED_ATTRS = {"os", "sys", "subprocess", "system", "popen", "spawn", "unlink", "rmdir", "rmtree",
                 "write_text", "write_bytes", "modules", "builtins", "importlib", "shutil"}
# results that mean the plugin caught its own error rather than working
_FAILED_RESULT = re.compile(r"^\s*(?:error|failed|exception|traceback|an error|sorry, (?:an? )?error|"
                            r"could not|couldn't|unable to)\b", re.I)

_CONTRACT = '''You are writing exactly one plugin file for a local Python voice assistant called JARVIS.
Reply with ONLY raw Python source code. No markdown fences, no commentary before or after.

The file must define exactly these three names:
- SCHEMA: the tool definition (shape shown in the example below). The name is short snake_case.
- TEST_ARGS: a realistic example of the args dict, used to test the plugin before it is saved.
- run(args: dict, ctx) -> str: do the work and return one or two short sentences to speak aloud.
  Catch your own errors and return a message instead of raising.

`ctx` gives you:
- ctx.memory.remember(fact) / ctx.memory.recall(query) / ctx.memory.forget(text) — the user's durable facts
- ctx.speak(text) — say something out loud
- ctx.confirm(question) -> bool — ask the user yes/no before anything risky or irreversible

Hard rules:
- Only these modules may be imported: requests, re, json, math, random, time, datetime, config.
- No file access, no os/sys/subprocess, no eval/exec/getattr/open, no dunder attributes like __class__.
- Make no network calls except via `requests` (with a timeout) to a public URL the request implies,
  preferably a free API that needs no key.
- One self-contained file; do not reference any other plugin or project file.
- Arguments are filled in from speech by a small model, so keep each one a single simple value
  (e.g. separate "value": "-40", "from_unit": "C") and parse them leniently: accept numbers as strings
  or numbers, and units in any common spelling ("C", "celsius", "°C").

Example of a complete, correct plugin:

import random

SCHEMA = {
    "type": "function",
    "function": {
        "name": "roll_dice",
        "description": "Roll one or more dice with a given number of sides.",
        "parameters": {
            "type": "object",
            "properties": {
                "count": {"type": "string", "description": "how many dice, e.g. '2'"},
                "sides": {"type": "string", "description": "sides per die, e.g. '6'"}
            },
            "required": ["count", "sides"]
        }
    }
}

TEST_ARGS = {"count": "2", "sides": "6"}


def run(args: dict, ctx) -> str:
    try:
        count, sides = int(args.get("count", 1)), int(args.get("sides", 6))
    except ValueError:
        return "I need whole numbers for the dice."
    if not (1 <= count <= 20 and 2 <= sides <= 1000):
        return "That's more dice than I'm willing to roll."
    rolls = [random.randint(1, sides) for _ in range(count)]
    return f"You rolled {', '.join(map(str, rolls))}, for a total of {sum(rolls)}."
'''


def create_plugin(description: str, confirm, speak, taken: set) -> str:
    """Drafts locally with the coder model by default (free, private). If CLOUD_ENABLED, drafts
    with the cloud model instead (better code, costs a little, asks before anything is sent).
    taken: tool names already in use, which the new one must not collide with."""
    speak(f"Give me a minute to build that, {config.USER_TITLE}.")
    model = _coder_model()
    chat = [{"role": "user", "content": f"{_CONTRACT}\n\nAbility the user asked for: {description}\n"}]
    problem = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if problem:  # show the model its own code and exactly what went wrong
            chat.append({"role": "user", "content": f"That attempt failed: {problem}\n"
                                                    "Reply with the complete corrected file only."})
        code = _draft(chat, model, confirm, description)
        if code is None:
            return "The user declined sending this to the cloud, so nothing was drafted."
        chat.append({"role": "assistant", "content": code})
        code = _strip_fences(code)
        name, problem = _check(code, taken)
        if not problem:
            result, problem = _test_run(code)
        print(f"  [selfcode] attempt {attempt}: {problem or 'passed'}")
        if not problem:
            break
    else:
        return (f"I couldn't get a working version after {MAX_ATTEMPTS} tries (last problem: {problem}), "
                "so nothing was saved.")

    if not confirm(f"I've built and tested a new ability called '{name.replace('_', ' ')}'. "
                   f"In the test it said: '{result[:200]}'. Shall I keep it?"):
        return "Declined — the new ability wasn't saved."
    path = PLUGIN_DIR / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    ok, load_problem = _try_load(path)
    if not ok:
        path.unlink(missing_ok=True)
        return f"Saved it but it failed to load ({load_problem}), so I removed it again."
    git_commit(path, f"Learn ability: {name}\n\nAsked for: {description}")
    speak(f"Learned a new ability: {name.replace('_', ' ')}.")
    return f"Learned a new ability called '{name}'. It's ready to use now."


def git_commit(path: Path, message: str):
    """Commit just this plugin file (added, changed or deleted), so any learned ability can be rolled
    back with git. Quietly does nothing if this isn't a git checkout or git isn't installed."""
    rel = str(path.relative_to(PROJECT_DIR))
    try:
        subprocess.run(["git", "add", "-A", "--", rel], cwd=PROJECT_DIR, capture_output=True, timeout=15)
        # `-- <path>` commits only this file, never anything else you happened to have staged
        r = subprocess.run(["git", "commit", "-q", "-m", message, "--", rel],
                           cwd=PROJECT_DIR, capture_output=True, text=True, timeout=30)
        if r.returncode:
            print(f"  [selfcode] git commit skipped: {(r.stderr or r.stdout).strip()[:200]}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  [selfcode] git commit skipped: {e}")


def _coder_model() -> str:
    """The coding-tuned model if it's installed, else the main one."""
    try:
        llm.client.show(config.CODER_MODEL)
        return config.CODER_MODEL
    except ollama.ResponseError:
        print(f"  [selfcode] {config.CODER_MODEL} isn't installed, drafting with {config.OLLAMA_MODEL}")
        return config.OLLAMA_MODEL


def _draft(chat: list, model: str, confirm, description: str) -> str | None:
    if config.CLOUD_ENABLED:
        code = router.ask_cloud("\n\n".join(m["content"] for m in chat), confirm,
                                label=f"draft a new ability for: {description}")
        return None if code.startswith(("The user declined", "Cloud model is disabled")) else code
    resp = llm.client.chat(model=model, messages=chat,
                           options={"temperature": 0.2, "num_ctx": config.OLLAMA_NUM_CTX,
                                    "num_predict": llm.CODE_TOKENS})
    return resp.message.content or ""


def _check(code: str, taken: set):
    """(tool name, "") if the draft is safe and well-formed, else (None, what's wrong)."""
    ok, problem = _safety_check(code)
    if not ok:
        return None, problem
    tree = ast.parse(code)
    assigned = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    missing = [n for n in ("SCHEMA", "TEST_ARGS") if n not in assigned] + (["run"] if "run" not in functions else [])
    if missing:
        return None, f"missing {', '.join(missing)} at the top level"
    m = re.search(r"""["']name["']\s*:\s*["']([^"']+)["']""", code)
    name = m.group(1) if m else ""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        return None, f"the SCHEMA name '{name}' must be short lowercase snake_case"
    if name in taken:
        return None, f"the name '{name}' is already used by another tool; choose a different name"
    return name, ""


def _safety_check(code: str):
    """A speed bump against the model writing something harmful by mistake. Not a sandbox."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"syntax error: {e}"
    for bad in _BANNED_SUBSTRINGS:
        if bad in code:
            return False, f"uses disallowed '{bad}'"
    for node in ast.walk(tree):
        # check every module on the line: `import json, os` must not slip os through
        if isinstance(node, ast.Import):
            mods = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # `from . import x` could reach other project files
                return False, "relative imports are not allowed"
            mods = [node.module or ""]
        elif isinstance(node, ast.Name) and (node.id in _BANNED_NAMES or
                                             (node.id.startswith("__") and node.id != "__name__")):
            return False, f"uses disallowed name '{node.id}'"
        elif isinstance(node, ast.Attribute) and (node.attr in _BANNED_ATTRS or node.attr.startswith("__")):
            return False, f"uses disallowed attribute '.{node.attr}'"
        else:
            continue
        for mod in mods:
            if mod.split(".")[0] not in _ALLOWED_IMPORTS:
                return False, f"disallowed import '{mod}'"
    return True, ""


def _test_run(code: str):
    """(what run(TEST_ARGS) returned, "") or ("", what went wrong). Runs in its own Python process
    with a timeout, from a throwaway folder, so a crash or hang can't touch JARVIS itself."""
    with tempfile.TemporaryDirectory() as tmp:
        draft = Path(tmp) / "draft_plugin.py"
        draft.write_text(code, encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, "-I", str(HARNESS), str(draft), str(PROJECT_DIR)],
                               cwd=tmp, capture_output=True, text=True, timeout=TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "", f"the test run took longer than {TEST_TIMEOUT} seconds"
    if r.returncode:
        err = (r.stderr or r.stdout).strip().splitlines()
        return "", "test run crashed:\n" + "\n".join(err[-8:])  # the end of a traceback says the most
    try:
        result = json.loads(r.stdout.strip().splitlines()[-1])["result"]
    except (IndexError, ValueError, KeyError):
        return "", f"the test run printed something unexpected: {r.stdout.strip()[-300:]}"
    if _FAILED_RESULT.match(result):
        return "", f"run(TEST_ARGS) returned an error message instead of working: {result[:300]}"
    return result, ""


def _try_load(path: Path):
    try:
        spec = importlib.util.spec_from_file_location(f"plugins.{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not isinstance(module.SCHEMA, dict) or not callable(module.run):
            return False, "SCHEMA/run have the wrong shape"
        module.SCHEMA["function"]["name"]
    except Exception as e:
        return False, str(e)
    return True, ""


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)  # also when the model adds chatter around it
    return m.group(1).strip() if m else text
