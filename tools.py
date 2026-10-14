"""Hands: the tools the brain can call. Add new abilities here (Home Assistant, files, Gmail...),
or let JARVIS learn them itself via add_feature — see selfcode.py and plugins/."""
import importlib.util
import threading
from datetime import datetime
from pathlib import Path

import requests

import config
import router
import selfcode

PLUGIN_DIR = Path(__file__).parent / "plugins"


class PluginContext:
    """What a learned-ability plugin gets instead of the full Tools object."""

    def __init__(self, memory, confirm, speak):
        self.memory = memory
        self.confirm = confirm
        self.speak = speak


class Tools:
    def __init__(self, memory, confirm, speak=print):
        self.memory = memory
        self.confirm = confirm
        self.speak = speak  # how to announce things that happen later (e.g. a timer firing)
        self.ctx = PluginContext(memory, confirm, speak)
        self.plugins = {}  # tool name -> loaded module
        self._loaded_plugin_files = set()
        self.schemas = [
            _fn("get_time", "Current local date and time.", {}),
            _fn("web_search", "Private web search for anything current or that you are unsure of.",
                {"query": "search query"}),
            _fn("remember", "Store a durable fact about the user (preferences, people, projects, routines).",
                {"fact": "the fact, written as one short sentence"}),
            _fn("recall", "Search stored facts about the user.", {"query": "what to look for"}),
            _fn("forget", "Delete stored facts that match the text, when the user asks.", {"text": "text to match"}),
            _fn("set_timer",
                "Set a timer that announces itself out loud when it ends. Use for any "
                "'set a timer for X', 'remind me in X', or countdown request.",
                {"seconds": "how many seconds from now the timer should run for",
                 "label": "short name for what the timer is for, empty string if none given"},
                required=["seconds"]),
            _fn("ask_cloud_expert",
                "Ask a stronger cloud model a HARD general reasoning or coding question. "
                "Never include names, contacts, locations or other personal details.",
                {"question": "self-contained, anonymized question"}),
            _fn("add_feature",
                "When the user asks you to do something you have no tool for, draft, save and "
                "load a brand new ability for it. Nothing is saved without the user's approval "
                "first — say so.",
                {"description": "plain description of the new ability, in the user's own words"}),
            _fn("remove_feature",
                "Delete a previously learned ability, when the user asks to remove or forget it.",
                {"name": "the ability's name"}),
        ]
        self._load_plugins()

    def _load_plugins(self):
        for path in sorted(PLUGIN_DIR.glob("*.py")):
            if path.stem.startswith("_") or path in self._loaded_plugin_files:
                continue
            try:
                self._load_plugin_file(path)
                self._loaded_plugin_files.add(path)
            except Exception as e:  # a broken plugin must never take the rest of JARVIS down
                print(f"  [plugin] failed to load {path.name}: {e}")

    def _load_plugin_file(self, path: Path) -> str:
        spec = importlib.util.spec_from_file_location(f"plugins.{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tool_name = module.SCHEMA["function"]["name"]
        if tool_name in self.plugins or any(s["function"]["name"] == tool_name for s in self.schemas):
            raise ValueError(f"tool name '{tool_name}' already exists")
        self.plugins[tool_name] = module
        self.schemas.append(module.SCHEMA)
        return tool_name

    def _remove_feature(self, name: str) -> str:
        key = name.strip().lower().replace(" ", "_")
        match = next((n for n in self.plugins if key in n or n in key), None)
        if not match:
            return f"No learned ability matching '{name}'."
        (PLUGIN_DIR / f"{match}.py").unlink(missing_ok=True)
        del self.plugins[match]
        self.schemas = [s for s in self.schemas if s["function"]["name"] != match]
        self._loaded_plugin_files = {p for p in self._loaded_plugin_files if p.stem != match}
        return f"Removed the '{match}' ability."

    def run(self, name: str, args: dict) -> str:
        try:
            if name == "get_time":
                return datetime.now().strftime("%A %d %B %Y, %I:%M %p")
            if name == "web_search":
                return _search(args["query"])
            if name == "remember":
                return self.memory.remember(args["fact"])
            if name == "recall":
                return self.memory.recall(args["query"])
            if name == "forget":
                return self.memory.forget(args["text"])
            if name == "set_timer":
                return self._set_timer(args)
            if name == "ask_cloud_expert":
                return router.ask_cloud(args["question"], self.confirm)
            if name == "add_feature":
                result = selfcode.create_plugin(args["description"], self.confirm, self.speak)
                self._load_plugins()  # picks up the new one for this same session
                return result
            if name == "remove_feature":
                return self._remove_feature(args["name"])
            if name in self.plugins:
                return self.plugins[name].run(args, self.ctx)
            return f"Unknown tool: {name}"
        except Exception as e:  # tools must never crash the loop
            return f"Tool {name} failed: {e}"

    def _set_timer(self, args: dict) -> str:
        seconds = float(args["seconds"])
        if seconds <= 0:
            return "Timer needs a positive number of seconds."
        label = (args.get("label") or "").strip()
        what = f"Your {label} timer" if label else "Your timer"

        def fire():
            self.speak(f"{what} is up, {config.USER_TITLE}.")

        t = threading.Timer(seconds, fire)
        t.daemon = True  # don't block program exit if one is still pending
        t.start()
        return f"{what} set for {seconds:g} seconds."


def _fn(name, description, params, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object",
                       "properties": {k: {"type": "string", "description": v} for k, v in params.items()},
                       "required": required if required is not None else list(params)}}}


def _search(query: str) -> str:
    r = requests.get(f"{config.SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=10)
    r.raise_for_status()
    results = r.json().get("results", [])[:5]
    if not results:
        return "No results."
    return "\n\n".join(f"{x.get('title')}\n{x.get('url')}\n{(x.get('content') or '')[:300]}" for x in results)
