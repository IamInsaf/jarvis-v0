"""Switching the brain model by voice: "switch to gemma", "use the 9B", "which brain are you using?".
Every caller reads config.OLLAMA_MODEL when it makes a call, so a switch is just reassigning it.
The choice is saved in data/brain.json and wins over OLLAMA_MODEL from .env on the next start.
"Turn off your brain" unloads every model from RAM/VRAM and stops all LLM calls until it's turned on again."""
import json
import re
import time
from pathlib import Path

import config
import hud
import llm

SAVE_PATH = Path(config.DB_PATH).parent / "brain.json"

# what speech recognition makes of the names: "gamma", "jemma", "quen", "queen", "lama"
_SPOKEN = {r"g[ae]m+a|jemma|jenna": "gemma", r"q\s?wen|quen|kwen|queen|cuen|quinn": "qwen",
           r"l+a+ma|yama": "llama", r"mistrel": "mistral"}
_NUMBERS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
            "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fourteen": "14"}
_SMALL = re.compile(r"\b(?:small(?:er|est)?|little|fast(?:er|est)?|quick(?:er)?|light(?:er)?|tiny|mini)\b")
_BIG = re.compile(r"\b(?:big(?:ger|gest)?|large(?:r|st)?|smart(?:er|est)?|strong(?:er|est)?|heav(?:y|ier))\b")

_FILLER = r"^\s*(?:(?:hey|ok(?:ay)?|so|jarvis|please|can you|could you)[\s,]*)*"
_TAIL = r"[\s,.!?]*(?:please|now|jarvis|sir)?[\s,.!?]*$"
_BRAIN = r"(?:brain|model|llm|language model|ai)"
_WHICH = re.compile(_FILLER + rf"(?:which|what) {_BRAIN}(?: are| is)?(?: you)?(?: using| running(?: on)?| on| is this)?"
                    r"(?: (?:right )?now)?" + _TAIL + r"|" + _FILLER +
                    r"what are you running on" + _TAIL, re.I)
_LIST = re.compile(_FILLER + rf"(?:(?:what|which) {_BRAIN}s (?:do you have|are (?:there|available|installed)|"
                   rf"can you (?:use|switch to))|list (?:your |the )?{_BRAIN}s)" + _TAIL, re.I)
# "switch your brain to gemma", "change model to qwen 9b", "use gemma as your brain"
_SET_NAMED = re.compile(_FILLER + rf"(?:switch|change|swap|set)(?: over| back)? (?:your |the |my )?{_BRAIN}"
                        r"(?: over| back)?(?: to| for)? (?P<m>.+?)" + _TAIL + "|" + _FILLER +
                        rf"use (?P<m2>.+?) (?:as|for) (?:your |the )?{_BRAIN}" + _TAIL, re.I)
# "switch to gemma": only when it names a model family, so "switch to dark mode" isn't caught
_SET_BARE = re.compile(_FILLER + r"(?:(?:switch|change|swap|go)(?: over| back)? to|use) (?P<m>.+?)" + _TAIL, re.I)

# "turn off your brain", "brain off", "go brainless" / "turn on your brain", "wake up your brain"
_OFF = re.compile(_FILLER + rf"(?:(?:turn|switch|shut|power)(?: off| down) (?:your |the |my )?{_BRAIN}|"
                  rf"(?:turn|switch|shut|power) (?:your |the |my )?{_BRAIN} (?:off|down)|"
                  rf"(?:your |the )?{_BRAIN} off|go brainless|unload (?:your |the |my )?{_BRAIN})" + _TAIL, re.I)
_ON = re.compile(_FILLER + rf"(?:(?:turn|switch|power)(?: on| up) (?:your |the |my )?{_BRAIN}|"
                 rf"(?:turn|switch|power) (?:your |the |my )?{_BRAIN} (?:on|up)|"
                 rf"(?:your |the )?{_BRAIN} on|wake (?:up )?(?:your |the |my )?{_BRAIN}(?: up)?|"
                 rf"load (?:your |the |my )?{_BRAIN})" + _TAIL, re.I)

_installed_cache = (0.0, [])
awake = True  # False after "turn off your brain": nothing may call the LLM (brain.py asks first)


def installed() -> list[dict]:
    """Installed models, one per distinct model ([] if Ollama is down): gemma4:latest and gemma4:e4b
    are the same file, so only the named tag is kept."""
    global _installed_cache
    if time.time() - _installed_cache[0] < 30 and _installed_cache[1]:
        return _installed_cache[1]
    try:
        models = llm.client.list().models
    except llm.ERRORS:
        return []
    by_digest = {}
    for m in sorted(models, key=lambda m: m.model.endswith(":latest")):  # explicit tags first
        by_digest.setdefault(m.digest, {"name": m.model, "size": m.size or 0, **_parts(m.model)})
    _installed_cache = (time.time(), list(by_digest.values()))
    return _installed_cache[1]


def _parts(name: str) -> dict:
    """"qwen3.5:9b" -> family qwen, version 3.5, tag 9b; "qwen2.5-coder:7b" also has variant coder."""
    base, _, tag = name.partition(":")
    m = re.match(r"([a-z]+)([\d.]*)(?:-(\w+))?", base.lower())
    family, version, variant = (m[1], m[2], m[3] or "") if m else (base.lower(), "", "")
    return {"family": family, "version": version, "variant": variant, "tag": tag.lower()}


def spoken_name(name: str) -> str:
    """"qwen3.5:9b" -> "Qwen 3.5 9B", "gemma4:latest" -> "Gemma 4"."""
    p = _parts(name)
    words = [p["family"].capitalize(), p["version"], p["variant"].capitalize(),
             "" if p["tag"] in ("latest", "") else p["tag"].upper()]
    return " ".join(w for w in words if w)


def _normalize(spoken: str) -> str:
    t = " " + spoken.lower().replace("-", " ") + " "
    for pattern, family in _SPOKEN.items():
        t = re.sub(rf"\b(?:{pattern})\b", family, t)
    t = re.sub(r"\b(" + "|".join(_NUMBERS) + r")\b", lambda m: _NUMBERS[m[1]], t)
    t = re.sub(r"(\d)\s*(?:point|\.)\s*(\d)", r"\1.\2", t)        # "three point five" -> 3.5
    t = re.sub(r"(\d+)\s*(?:b|bee|billion)\b", r" \1b", t)           # "9 b" -> 9b
    t = re.sub(r"\b([a-z]{3,})\s+(\d+(?:\.\d+)?)\b", r"\1\2", t)  # "qwen 3.5" -> qwen3.5, not "qwen 9b"
    return " ".join(t.split())


def resolve(spoken: str) -> tuple[str | None, list[str]]:
    """(model name, []) for the installed model the words mean, or (None, candidates) when it could
    be several of them — (None, []) when it's none."""
    models = installed()
    words = _normalize(spoken)
    for m in models:  # said exactly: "qwen3.5-ctf", "gemma4:e4b"
        if words.replace(" ", "") in (m["name"], m["name"].removesuffix(":latest")):
            return m["name"], []
    families = {m["family"] for m in models}
    said = [f for f in families if re.search(rf"\b{f}", words)]
    pool = [m for m in models if m["family"] in said] if said else []
    if not said:  # "the small one" / "the big one": within the current family if it has a choice
        if not (_SMALL.search(words) or _BIG.search(words)):
            return None, []
        family = _parts(config.OLLAMA_MODEL)["family"]
        pool = [m for m in models if m["family"] == family and not m["variant"]]
        if len({m["size"] for m in pool}) < 2:
            pool = [m for m in models if not m["variant"]]
    variants = [m for m in pool if m["variant"] and re.search(rf"\b{m['variant']}\b", words)]
    pool = variants or [m for m in pool if not m["variant"]]  # "qwen coder" means the coder
    if version := re.search(r"[a-z](\d+(?:\.\d+)?)\b", words):  # qwen3.5, gemma4
        if matched := [m for m in pool if m["version"] == version[1]]:
            pool = matched
        else:  # "qwen 4" meant the 4B
            words += f" {version[1]}b"
            version = None
    if size := re.search(r"\b(e?\d+(?:\.\d+)?b)\b", words):  # 9b, e4b
        pool = [m for m in pool if m["tag"] == size[1] or m["tag"].lstrip("e") == size[1].lstrip("e")]
    if not pool:
        return None, []
    if _SMALL.search(words):
        pool = [min(pool, key=lambda m: m["size"])]
    elif _BIG.search(words):
        pool = [max(pool, key=lambda m: m["size"])]
    if not version:  # just "qwen": the newest version of it
        newest = max(_version_key(m["version"]) for m in pool)
        pool = [m for m in pool if _version_key(m["version"]) == newest]
    if len(pool) == 1:
        return pool[0]["name"], []
    return None, [m["name"] for m in sorted(pool, key=lambda m: m["size"])]


def _version_key(version: str) -> tuple:
    return tuple(int(n) for n in version.split(".") if n.isdigit())


def _available() -> str:
    names = [spoken_name(m["name"]) for m in installed() if not m["variant"]]
    return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1] if names else "none"


def current() -> str:
    return f"I'm running on {spoken_name(config.OLLAMA_MODEL)}, {config.USER_TITLE}."


def describe() -> str:
    if not installed():
        return f"I can't reach Ollama to see which brains are installed, {config.USER_TITLE}."
    return (f"I have {_available()}, {config.USER_TITLE}. "
            f"{spoken_name(config.OLLAMA_MODEL)} is in charge at the moment.")


def switch(spoken: str) -> str:
    """Make the model the words mean the brain from now on; says what happened."""
    title = config.USER_TITLE
    if not installed():
        return f"I can't reach Ollama to switch brains, {title}."
    name, options = resolve(spoken)
    if not name:
        if options:
            return f"{' or '.join(spoken_name(o) for o in options)}, {title}?"
        return f"I don't have a model called {spoken}, {title}. I have {_available()}."
    if name == config.OLLAMA_MODEL:
        return f"I'm already running on {spoken_name(name)}, {title}."
    if not awake:
        return f"My brain is switched off, {title}. Turn it on first, then I'll switch to {spoken_name(name)}."
    caps = llm.capabilities(name)
    if "tools" not in caps:
        return (f"{spoken_name(name)} can't use tools, {title}, so I couldn't set a timer or open an app "
                "with it. I'll stay as I am.")
    old = config.OLLAMA_MODEL
    try:  # load it now, so the next reply isn't a long silence while Ollama swaps models
        _warm(name, caps)
    except llm.ERRORS as e:
        print(f"  [brain] couldn't load {name}: {type(e).__name__}: {e}")
        return f"{spoken_name(name)} wouldn't load, {title}, so I've stayed on {spoken_name(old)}."
    config.OLLAMA_MODEL = name
    _save(name)
    try:  # free the old one's memory: two models at once push a small GPU onto the CPU
        llm.client.generate(model=old, keep_alive=0)
    except llm.ERRORS:
        pass
    print(f"  [brain] {old} -> {name}")
    hud.emit("brain", model=name)
    return f"{spoken_name(name)} is at the helm, {title}. Same me, different engine."


def _warm(name: str, caps: list):
    """Load the model into memory with a one-token reply."""
    llm.client.chat(model=name, messages=[{"role": "user", "content": "hi"}], keep_alive=llm.keep_alive(),
                    **({"think": False} if "thinking" in caps else {}),
                    options={"num_ctx": config.OLLAMA_NUM_CTX, "num_predict": 1})


def sleep() -> str:
    """"Turn off your brain": unload every model Ollama has in memory and stop using the LLM."""
    global awake
    title = config.USER_TITLE
    if not awake:
        return f"My brain is already off, {title}."
    awake = False
    try:  # whatever is loaded: the brain, the coder model, a vision model
        loaded = [m.model for m in llm.client.ps().models]
    except (*llm.ERRORS, AttributeError):
        loaded = []
    names = list(dict.fromkeys([config.OLLAMA_MODEL, *loaded]))
    for name in names:
        try:
            llm.client.generate(model=name, keep_alive=0)
        except llm.ERRORS:
            pass
    print(f"  [brain] off (unloaded {', '.join(names)})")
    hud.emit("brain", model="off")
    return (f"Brain off, {title}. The memory is yours. I can still do the time, timers, alarms and the radios; "
            "say \"turn on your brain\" for anything else.")


def wake() -> str:
    """"Turn on your brain": load the model again and allow LLM calls."""
    global awake
    title = config.USER_TITLE
    if awake:
        return f"My brain is already on, {title}."
    try:
        _warm(config.OLLAMA_MODEL, llm.capabilities(config.OLLAMA_MODEL))
    except llm.ERRORS as e:
        print(f"  [brain] couldn't load {config.OLLAMA_MODEL}: {type(e).__name__}: {e}")
        return f"My brain wouldn't load, {title}. Ollama may need restarting."
    awake = True
    print(f"  [brain] on ({config.OLLAMA_MODEL})")
    hud.emit("brain", model=config.OLLAMA_MODEL)
    return f"Brain on, {title}. {spoken_name(config.OLLAMA_MODEL)} at your service."


def _save(name: str):
    try:
        SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SAVE_PATH.write_text(json.dumps({"model": name}))
    except OSError as e:
        print(f"  [brain] couldn't save the choice: {e}")


def load_saved():
    """At startup: the last model switched to by voice, if it's still installed."""
    try:
        name = json.loads(SAVE_PATH.read_text())["model"]
    except (OSError, ValueError, KeyError):
        return
    models = installed()
    if models and name not in {m["name"] for m in models}:
        print(f"  [brain] saved model {name} isn't installed any more, using {config.OLLAMA_MODEL}")
        return
    config.OLLAMA_MODEL = name
    print(f"  [brain] {name} (switched to by voice; say \"switch to ...\" to change)")


def parse(text: str):
    """(tool, args) for a spoken brain switch or question, else None."""
    if _OFF.match(text):  # before the switch patterns: "switch off your brain" names no model
        return "brain_off", {}
    if _ON.match(text):
        return "brain_on", {}
    if _WHICH.match(text):
        return "which_brain", {}
    if _LIST.match(text):
        return "list_brains", {}
    if m := _SET_NAMED.match(text):
        return "set_brain", {"model": (m["m"] or m["m2"]).strip()}
    if (m := _SET_BARE.match(text)) and len(m["m"].split()) <= 4 and _names_a_family(m["m"]):
        return "set_brain", {"model": m["m"].strip()}
    return None


def _names_a_family(words: str) -> bool:
    words = _normalize(words)
    families = {m["family"] for m in installed()} | {"qwen", "gemma", "llama", "mistral", "deepseek"}
    return any(re.search(rf"\b{f}", words) for f in families)
