"""Self-upgrade: lets JARVIS draft a new ability for itself when you ask for something it
can't do, save it as an isolated plugin file, and load it — all only with your explicit OK.
Never edits jarvis's own core files; a bad plugin can only ever be deleted, not break the rest."""
import ast
import importlib.util
import re
from pathlib import Path

import ollama

import config
import router

PLUGIN_DIR = Path(__file__).parent / "plugins"
PLUGIN_DIR.mkdir(exist_ok=True)

_ALLOWED_IMPORTS = {"requests", "re", "json", "math", "random", "time", "datetime", "pathlib", "config"}
_BANNED_SUBSTRINGS = ("os.system", "subprocess", "eval(", "exec(", "__import__", "shutil.rmtree", "open(")

_CONTRACT = '''You are writing exactly one plugin file for a local Python voice assistant called JARVIS.
Reply with ONLY raw Python source code. No markdown fences, no commentary before or after.

The file must define exactly these two names:

SCHEMA = {
    "type": "function",
    "function": {
        "name": "short_unique_snake_case_name",
        "description": "one sentence an LLM can use to decide when to call this tool",
        "parameters": {
            "type": "object",
            "properties": {"arg_name": {"type": "string", "description": "what it is"}},
            "required": ["arg_name"]
        }
    }
}

def run(args: dict, ctx) -> str:
    """Do the work, return a short string result for the assistant to speak. Catch your own
    errors and return a message instead of raising."""
    ...

`ctx` gives you:
- ctx.memory.remember(fact) / ctx.memory.recall(query) / ctx.memory.forget(text) — the user's durable facts
- ctx.speak(text) — say something out loud later, e.g. when a background timer fires
- ctx.confirm(question) -> bool — ask the user yes/no before anything risky or irreversible

Hard rules:
- Only stdlib modules plus `requests` and this project's `config` may be imported. Nothing else.
- Never use os.system, subprocess, eval, exec, __import__, shutil.rmtree, or open() (no file access).
- Make no network calls except via `requests` to a URL the user's request implies.
- One self-contained file; do not reference any other plugin or project file.
'''


def create_plugin(description: str, confirm, speak) -> str:
    """Drafts locally by default (free, private, but a weaker coder). If CLOUD_ENABLED, drafts
    with the cloud model instead (better code, costs a little, asks before anything is sent)."""
    prompt = f"{_CONTRACT}\n\nAbility the user asked for: {description}\n"
    if config.CLOUD_ENABLED:
        code = router.ask_cloud(prompt, confirm, label=f"draft a new ability for: {description}")
        if code.startswith("The user declined") or code.startswith("Cloud model is disabled"):
            return code
    else:
        code = _draft_locally(prompt)

    code = _strip_fences(code)
    ok, problem = _safety_check(code)
    if not ok:
        hint = "" if config.CLOUD_ENABLED else " Enable CLOUD_ENABLED for a stronger drafting model if this keeps happening."
        return f"The draft didn't pass a safety check ({problem}), so I didn't save it.{hint}"

    name = _free_slug(description)
    if not confirm(f"I've drafted a new ability called '{name}' for: {description}. Save and load it?"):
        return "Declined — the new ability wasn't saved."

    path = PLUGIN_DIR / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    ok, problem = _try_load(path)
    if not ok:
        path.unlink(missing_ok=True)
        return f"Saved it but it failed to load ({problem}), so I removed it again."
    speak(f"Learned a new ability: {name}.")
    return f"Learned a new ability called '{name}'. It's ready to use now."


def _draft_locally(prompt: str) -> str:
    resp = ollama.chat(model=config.OLLAMA_MODEL, messages=[{"role": "user", "content": prompt}])
    return resp.message.content or ""


def _safety_check(code: str):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"syntax error: {e}"
    if "SCHEMA" not in code or "def run(" not in code:
        return False, "missing SCHEMA or run()"
    for bad in _BANNED_SUBSTRINGS:
        if bad in code:
            return False, f"uses disallowed '{bad}'"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (node.names[0].name if isinstance(node, ast.Import) else (node.module or "")).split(".")[0]
            if mod not in _ALLOWED_IMPORTS:
                return False, f"disallowed import '{mod}'"
    return True, ""


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
    m = re.match(r"^```(?:python)?\s*(.*?)\s*```$", text, re.S)
    return m.group(1).strip() if m else text


def _free_slug(description: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", description.lower()).strip("_")[:40] or "feature"
    candidate, i = base, 2
    while (PLUGIN_DIR / f"{candidate}.py").exists():
        candidate, i = f"{base}_{i}", i + 1
    return candidate
