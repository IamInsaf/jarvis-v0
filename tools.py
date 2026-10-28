"""Hands: the tools the brain can call. Add new abilities here (Home Assistant, files, Gmail...),
or let JARVIS learn them itself via add_feature — see selfcode.py and plugins/."""
import importlib.util
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

import apps
import brains
import briefing
import config
import desktop
import executor
import radios
import router
import rules
import selfcode
import timezones
from files import Workspace
from messaging import Messenger
from scheduler import Scheduler
from sitebuilder import Builder
from todos import Todos

PLUGIN_DIR = Path(__file__).parent / "plugins"
# "use Claude" / "use your own ability" in the request decides who builds a website
_USE_CLAUDE = re.compile(r"\b(?:use|using|with|via|ask|let|get) (?:claude|the cloud)\b|\bclaude (?:code|build|do)\b",
                         re.I)
_USE_OWN = re.compile(r"\b(?:your own|yourself|locally|local model|on your own|without claude)\b", re.I)


class PluginContext:
    """What a learned-ability plugin gets instead of the full Tools object."""

    def __init__(self, memory, confirm, speak):
        self.memory = memory
        self.confirm = confirm
        self.speak = speak
        self.files = Workspace(config.WORKSPACE_DIR, confirm)


class Tools:
    def __init__(self, memory, confirm, speak=print):
        self.memory = memory
        self.confirm = confirm
        self.speak = speak  # how to announce things that happen later (e.g. a timer firing)
        self.speaker = None  # set by Brain.think() before each call; None = unrecognized/general
        self.user_text = ""  # the request being handled, also set by Brain.think()
        self.builder = Builder(confirm, speak)
        self.messenger = Messenger(config.DB_PATH, confirm)
        self.todos = Todos(config.DB_PATH)
        self.ctx = PluginContext(memory, confirm, speak)
        self.scheduler = Scheduler(config.DB_PATH, speak)
        briefing.start_daily(lambda: speak(briefing.compose(self.scheduler, self.todos)))  # if BRIEFING_TIME is set
        self._opened = (0, None)  # (window in front, when) at the last open_app, see _settle
        self.plugins = {}  # tool name -> loaded module
        self.plugin_paths = {}  # tool name -> its file (named after the description, not the tool)
        self._loaded_plugin_files = set()
        self.schemas = [
            _fn("get_time", "Current local date and time.", {}),
            _fn("web_search", "Private web search for anything current or that you are unsure of.",
                {"query": "search query"}),
            _fn("remember", "Store a durable fact about the user (preferences, people, projects, routines).",
                {"fact": "the fact, written as one short sentence"}),
            _fn("recall", "Search stored facts about the user.", {"query": "what to look for"}),
            _fn("forget", "Delete stored facts that match the text, when the user asks.", {"text": "text to match"}),
            _fn("time_in", "Current time in another city or country.", {"place": "the city or country"}),
            _fn("set_timer",
                "Start a countdown timer for a duration ('set a timer for 10 minutes'). "
                "It announces itself out loud when it ends, even after a restart.",
                {"seconds": "how many seconds the timer should run for",
                 "label": "short name for what the timer is for, empty string if none given"},
                required=["seconds"]),
            _fn("set_alarm", "Set an alarm for a clock time ('wake me at 7', 'alarm for 6:30 tomorrow').",
                {"time": "the time exactly as the user said it, e.g. '7 am' or 'tomorrow at 6:30'",
                 "label": "what the alarm is for, empty string if none given"},
                required=["time"]),
            _fn("set_reminder", "Remind the user about something at a time or after a duration.",
                {"text": "what to remind them about, e.g. 'call mom'",
                 "when": "exactly as the user said it, e.g. 'in 20 minutes', 'at 5 pm', 'tomorrow at 9'"}),
            _fn("list_schedule", "List the pending timers, alarms and reminders, with time left.", {}),
            _fn("briefing", "The user's day at a glance: date, what's scheduled today, top of the to-do list "
                "('good morning', 'brief me', 'what's my day look like').", {}),
            _fn("cancel_schedule", "Cancel timers, alarms or reminders.",
                {"which": "which ones, as the user said it, e.g. 'the tea timer', 'all alarms', 'my 7 am alarm'"}),
            _fn("list_workspace",
                "Where JARVIS keeps the files and folders it creates (its working directory), "
                "and what is in there.", {}),
            _fn("build_website",
                "Build a new website or web page and run it on this computer. engine: 'claude' when the "
                "user says to use Claude, 'local' when they say to do it yourself or use your own ability, "
                "otherwise 'auto'.",
                {"description": "what the site should be and have, in the user's own words",
                 "engine": "auto, local or claude"}),
            _fn("change_website", "Change an existing website project ('make the header blue').",
                {"project": "the project's name, empty string for the most recent one",
                 "change": "the change, in the user's own words",
                 "engine": "auto, local or claude"}),
            _fn("show_website", "Run a website project on this computer and open it in the browser.",
                {"project": "the project's name, empty string for the most recent one"}),
            _fn("stop_website", "Stop a running website project.",
                {"project": "the project's name, empty string for the most recent one"}),
            _fn("list_websites", "List the website projects and which are running.", {}),
            _fn("open_app", "Open an application on this computer ('open notepad', 'launch chrome').",
                {"name": "the app's name as the user said it"}),
            _fn("close_app", "Close an application that's open on this computer ('close chrome', 'quit notepad').",
                {"name": "the app's name as the user said it"}),
            _fn("press_keys", "Press a key or keyboard shortcut in the window that has focus ('press ctrl s', "
                "'hit enter', 'alt tab').",
                {"keys": "the keys, like 'ctrl+s', 'enter', 'alt+tab' or 'win+d'"}),
            _fn("type_text", "Type text into the window that has focus, at the cursor ('type hello world').",
                {"text": "exactly the text to type, as the user said it"}),
            _fn("switch_window", "Bring an open window or app to the front ('switch to chrome', 'go to spotify').",
                {"name": "the app or window as the user said it"}),
            _fn("media", "Media and volume: play, pause, next track, previous, mute, volume up or down, or set "
                "the volume ('volume 30').",
                {"action": "what to do, e.g. 'pause', 'next', 'volume up', 'volume 30'"}),
            _fn("look_at_screen", "Look at what's on the user's screen and answer a question about it "
                "('what's on my screen', 'read this error').",
                {"question": "what the user wants to know about the screen"}),
            _fn("run_shell", "Run one PowerShell command on this computer, for system information or a task no other "
                "tool covers ('how much disk space is free', 'what's my IP address'). Prefer read-only Get- "
                "commands; anything that changes the system is read back to the user first.",
                {"command": "one PowerShell command"}),
            _fn("set_radio", "Turn Wi-Fi and/or Bluetooth on or off on this computer.",
                {"radio": "wifi, bluetooth or both", "state": "on or off"}),
            _fn("set_brain", "Switch the AI model you think with ('switch to gemma', 'use the 9B').",
                {"model": "the model as the user said it, e.g. 'gemma' or 'qwen 9b'"}),
            _fn("which_brain", "Say which AI model you're running on right now.", {}),
            _fn("list_brains", "Say which AI models are installed that you can switch to.", {}),
            _fn("send_message", "Text someone on WhatsApp ('text my mom: I'll be late'). Reads it back and asks "
                "before sending.",
                {"contact": "who to text, e.g. 'mom' or 'John'", "message": "what to say, as the user said it"}),
            _fn("save_contact", "Save someone's phone number for texting ('mom's number is plus 94...').",
                {"name": "whose number, e.g. 'mom'", "number": "the phone number as the user said it"}),
            _fn("list_contacts", "Say whose phone numbers you have saved.", {}),
            _fn("add_task", "Add one or more tasks to the user's to-do list.",
                {"tasks": "the task, or several separated by semicolons, in the user's words without "
                          "'add to my list'"}),
            _fn("list_tasks", "Read out the user's to-do list (what's still to do).", {}),
            _fn("complete_task", "Tick a task off the to-do list as done.",
                {"which": "its number ('2', 'the second one') or some of its words"}),
            _fn("remove_task", "Delete a task from the to-do list without doing it, or 'all' to clear it.",
                {"which": "its number, some of its words, or 'all'"}),
            _fn("forget_contact", "Delete a saved phone number, when the user asks.", {"name": "whose number"}),
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
        self._refresh_removable()

    def _refresh_removable(self):
        """remove_feature's name can only be one of the learned abilities: left free, a small model
        filled it with things like 'remove_feature'."""
        param = next(s for s in self.schemas if s["function"]["name"] == "remove_feature")
        name = param["function"]["parameters"]["properties"]["name"]
        if self.plugins:
            name["enum"] = sorted(self.plugins)
        else:
            name.pop("enum", None)

    def _load_plugin_file(self, path: Path) -> str:
        spec = importlib.util.spec_from_file_location(f"plugins.{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tool_name = module.SCHEMA["function"]["name"]
        if tool_name in self.plugins or any(s["function"]["name"] == tool_name for s in self.schemas):
            raise ValueError(f"tool name '{tool_name}' already exists")
        self.plugins[tool_name] = module
        self.plugin_paths[tool_name] = path
        self.schemas.append(module.SCHEMA)
        return tool_name

    def _remove_feature(self, name: str) -> str:
        key = name.strip().lower().replace(" ", "_")
        match = next((n for n in self.plugins if key in n or n in key), None)
        if not match:
            learned = ", ".join(n.replace("_", " ") for n in self.plugins) or "none yet"
            return f"No learned ability matching '{name}'. Learned abilities: {learned}."
        path = self.plugin_paths[match]
        path.unlink(missing_ok=True)  # raises (and keeps the ability loaded) if Windows can't delete it
        del self.plugin_paths[match]
        del self.plugins[match]
        self.schemas = [s for s in self.schemas if s["function"]["name"] != match]
        self._loaded_plugin_files.discard(path)
        self._refresh_removable()
        selfcode.git_commit(path, f"Forget ability: {match}")  # git history can still bring it back
        return f"Removed the '{match}' ability."

    def run(self, name: str, args: dict) -> str:
        tier, why = executor.check(name, args)
        if tier == "block":
            print(f"  [executor] blocked {name}: {why}")
            return f"I won't do that: it would mean {why}. Nothing was run."
        if tier == "confirm" and not self.confirm(why):
            return "Cancelled; nothing was run."
        try:
            if name == "get_time":
                return datetime.now().strftime("%A %d %B %Y, %I:%M %p")
            if name == "web_search":
                return _search(args["query"])
            if name == "remember":
                return self.memory.remember(args["fact"], speaker=self.speaker)
            if name == "recall":
                return self.memory.recall(args["query"])
            if name == "forget":
                return self.memory.forget(args["text"])
            if name == "time_in":
                return timezones.time_in(args["place"])
            if name == "set_timer":
                return self._set_timer(args)
            if name == "set_alarm":
                return self._set_at("alarm", args.get("label") or "", args["time"])
            if name == "set_reminder":
                return self._set_at("reminder", args["text"], args["when"])
            if name == "list_schedule":
                return self.scheduler.summary()
            if name == "briefing":
                return briefing.compose(self.scheduler, self.todos)
            if name == "cancel_schedule":
                return self.scheduler.cancel(args["which"])
            if name == "list_workspace":
                names = self.ctx.files.list()
                return f"My workspace is {self.ctx.files.path()}. " + (
                    f"It contains: {', '.join(names)}." if names else "It's empty.")
            if name == "build_website":
                return self.builder.build(args["description"], self._engine(args.get("engine")))
            if name == "change_website":
                return self.builder.edit(args.get("project", ""), args["change"], self._engine(args.get("engine")))
            if name == "show_website":
                return self.builder.serve(args.get("project", ""))
            if name == "stop_website":
                return self.builder.stop(args.get("project", ""))
            if name == "list_websites":
                return self.builder.projects()
            if name == "open_app":
                self._opened = (desktop.foreground(), datetime.now())
                return apps.open_app(args["name"])
            if name == "press_keys":
                self._settle()
                return desktop.press_keys(args.get("keys", ""))
            if name == "type_text":
                self._settle()
                return desktop.type_text(args.get("text", ""))
            if name == "switch_window":
                return desktop.switch_window(args.get("name", ""))
            if name == "media":
                return desktop.media(args.get("action", "") or self.user_text)
            if name == "look_at_screen":
                return desktop.look_at_screen(args.get("question", "") or self.user_text)
            if name == "run_shell":
                return desktop.run_shell(args.get("command", ""))
            if name == "close_app":
                return apps.close_app(args["name"])
            if name == "set_radio":
                return radios.set_radios(args.get("radio", ""), args.get("state", ""))
            if name == "set_brain":
                return brains.switch(args.get("model") or self.user_text)
            if name == "which_brain":
                return brains.current()
            if name == "list_brains":
                return brains.describe()
            if name == "send_message":
                return self.messenger.send(args.get("contact", ""), args.get("message", ""), self.user_text)
            if name == "save_contact":
                return self.messenger.save_contact(args.get("name", ""), args.get("number", ""), self.user_text)
            if name == "list_contacts":
                return self.messenger.contacts()
            if name == "add_task":
                return self.todos.add(args.get("tasks") or self.user_text)
            if name == "list_tasks":
                return self.todos.list()
            if name == "complete_task":
                return self.todos.complete(args.get("which", ""))
            if name == "remove_task":
                return self.todos.remove(args.get("which", ""))
            if name == "forget_contact":
                return self.messenger.forget_contact(args["name"])
            if name == "ask_cloud_expert":
                return router.ask_cloud(args["question"], self.confirm)
            if name == "add_feature":
                taken = {s["function"]["name"] for s in self.schemas}
                result = selfcode.create_plugin(args["description"], self.confirm, self.speak, taken)
                self._load_plugins()  # picks up the new one for this same session
                return result
            if name == "remove_feature":
                return self._remove_feature(args["name"])
            if name in self.plugins:
                return self.plugins[name].run(args, self.ctx)
            return f"Unknown tool: {name}"
        except Exception as e:  # tools must never crash the loop
            return f"Tool {name} failed: {e}"

    def _settle(self):
        """Keys or typing right after open_app in the same request ("open notepad and type hello") wait for
        the new window to take focus, or they'd land in whatever was in front."""
        before, when = self._opened
        if when and (datetime.now() - when).total_seconds() < 15:
            desktop.wait_for_new_window(before)
            self._opened = (0, None)

    def _engine(self, said) -> str:
        """Who builds: what the user said wins over the model's pick, which mixed the two up."""
        if _USE_CLAUDE.search(self.user_text):
            return "claude"
        if _USE_OWN.search(self.user_text):
            return "local"
        said = str(said or "").lower().strip()
        return said if said in ("local", "claude") else "auto"

    def _set_timer(self, args: dict) -> str:
        try:
            seconds = float(args["seconds"])
        except ValueError:  # the model sometimes passes "10 minutes" instead of 600
            seconds = rules.timer_seconds(str(args["seconds"])) or 0
        if seconds <= 0:
            return "Timer needs a positive number of seconds."
        label = (args.get("label") or "").strip()
        self.scheduler.add("timer", label, datetime.now() + timedelta(seconds=seconds))
        return f"{label.capitalize() + ' timer' if label else 'Timer'} set for {rules.spoken_duration(seconds)}."

    def _set_at(self, kind: str, label: str, when: str) -> str:
        """An alarm or reminder at a spoken time; the parsing is done here, never by the model."""
        found = rules.find_when(str(when))
        if not found:
            return f"I couldn't work out the time '{when}'. Ask the user for a clock time like '7 am'."
        due = found[0]
        self.scheduler.add(kind, label, due)
        if kind == "alarm":
            return f"Alarm set for {rules.spoken_clock(due)}" + (f" for {label}." if label else ".")
        return f"I'll remind you {self.scheduler.describe(kind, label, due).removeprefix('a reminder ')}."


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
