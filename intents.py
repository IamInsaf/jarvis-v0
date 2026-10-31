"""Intent router: one small, constrained LLM call that picks a category for the request, so the
main call only sees that category's few tools and rules instead of all of them. A 7B model is
reliable at "pick one of these" and much less so at "here are 15 tools, figure it out".
(Not to be confused with router.py, which is the privacy gate for cloud calls.)"""
import json
import re
from pathlib import Path

import config
import llm
import messaging

# category -> (what it covers, for the router; tool names it may use; extra rules for the main call)
CATEGORIES = {
    "time": ("the current time or date here or in another place, or date arithmetic", ["get_time", "time_in"],
             "Always call get_time or time_in for a time or date — never state one from memory or work out "
             "a timezone yourself."),
    "timer": ("timers, alarms, countdowns and reminders: setting, checking, listing or cancelling them",
              ["set_timer", "set_alarm", "set_reminder", "list_schedule", "cancel_schedule", "briefing"],
              "set_timer is for a duration, set_alarm for a clock time, set_reminder for something to be "
              "reminded about. Pass times exactly as the user said them — never convert them yourself.\n"
              "Never say something is set or cancelled unless the tool said so."),
    "memory": ("the user shares something about themselves, says remember or forget, or asks what you "
               "know about them", ["remember", "recall", "forget"],
               "When the user shares something durable about themselves, or says \"remember\", call remember "
               "in that same turn. Never say you saved something unless the remember tool succeeded.\n"
               "For questions about the user not covered by the known facts, call recall before answering.\n"
               "Only call forget when the user clearly asks you to forget something."),
    "web": ("news, weather, prices, sports, people, or anything current or that might have changed",
            ["web_search", "get_time"],
            "Never guess about anything current: call web_search and answer from its results."),
    "files": ("creating, writing, reading or listing files and folders, or where you keep them "
              "(your working directory), including follow-ups like 'add hello world to it'. Not websites",
              ["list_workspace", "add_feature"],
              "list_workspace says where your files live and what's there. To create, write or read a file "
              "or folder with no tool for it yet, call add_feature with the user's request in their words.\n"
              "Never say you created, wrote or changed a file or folder unless a tool said so, and always "
              "give its full path."),
    "websites": ("building, changing, showing, running or stopping a website, web page or web app project",
                 ["build_website", "change_website", "show_website", "stop_website", "list_websites"],
                 "build_website starts a new site, change_website changes an existing one. Both run in the "
                 "background: tell the user it has started and that you'll say when it's ready — never that "
                 "it's finished.\nOnly use engine 'claude' when the user asked for Claude."),
    "tasks": ("the user's to-do list: adding, reading, ticking off or removing tasks or to-dos",
              ["add_task", "list_tasks", "complete_task", "remove_task"],
              "Only the to-do list tools know what's on the list: never list, count or claim tasks yourself. "
              "Adding a task is just writing it down, not doing it."),
    "focus": ("focus mode: the main task the user is working on and being kept on track with — starting or "
              "switching it, its next step, snoozing reminders, a break, parking, finishing or stopping it, "
              "'where was I', or how focused they were today. Not the to-do list",
              ["focus_start", "focus_next_step", "focus_snooze", "focus_break", "focus_park", "focus_resume",
               "focus_done", "focus_stop", "focus_status", "focus_log"],
              "focus_start starts focus mode or switches it to a new task. Never say focus mode started, stopped or "
              "paused unless a focus tool said so."),
    "messages": ("texting or messaging someone (WhatsApp), or saving, listing or deleting someone's phone number",
                 ["send_message", "save_contact", "list_contacts", "forget_contact"],
                 "send_message reads the message back and asks before sending; never say a message was sent "
                 "unless send_message said so."),
    "apps": ("opening, launching, closing or quitting an application or program on this computer",
             ["open_app", "close_app", "type_text", "press_keys", "switch_window"],
             "Call open_app to open an app and close_app to close one. If the request goes on to type or press "
             "something in it, call type_text or press_keys next. Never say an app opened or closed "
             "unless the tool said so."),
    "computer": ("controlling this computer: pressing keys or shortcuts, typing text into a window, switching "
                 "windows, volume and music playback, looking at or reading the screen, or system information "
                 "and commands (disk space, IP address, battery, processes)",
                 ["press_keys", "type_text", "switch_window", "media", "look_at_screen", "run_shell", "open_app"],
                 "Use the specific tool when one fits; run_shell (PowerShell) only for what none of them covers. "
                 "Prefer read-only Get- commands that work as written, e.g. free disk space: Get-PSDrive C; IP "
                 "address: Get-NetIPAddress -AddressFamily IPv4; battery: Get-CimInstance Win32_Battery; busiest "
                 "programs: Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 Name,CPU.\n"
                 "Never say something happened unless a tool result says so."),
    "devices": ("turning this computer's Wi-Fi or Bluetooth on or off", ["set_radio"],
                "Call set_radio to switch Wi-Fi or Bluetooth. Never say one was switched unless set_radio said so."),
    "brain": ("switching the AI model / brain you think with, or asking which one you're running on",
              ["set_brain", "which_brain", "list_brains"],
              "Call set_brain to switch models. Never say you switched unless set_brain said so."),
    "skills": ("the user asks you to learn, add or build a new ability, or to remove a learned one",
               ["add_feature", "remove_feature"],
               "To learn a new ability call add_feature; tell the user nothing is saved without their approval.\n"
               "To remove or forget a learned ability call remove_feature."),
    "chat": ("greetings, small talk, opinions, jokes, general knowledge, maths, explanations", [],
             "Answer directly. If it needs current information you don't have, say you'd need to look it up.\n"
             "You have no tools here: never say you did or will do something (created, saved, sent, set, "
             "\"I'll add it now\") — offer to learn it as a new ability instead. If an earlier reply of yours "
             "claimed something that no tool result backs up, correct it plainly."),
}
# each learned plugin becomes its own category, named after it: one "abilities" bucket made a
# small model send "flip a coin" to chat when a coin-flip ability existed
ABILITY_PREFIX = "use_"
_SKILL_ASK = re.compile(r"\b(?:learn|teach|add|build|create|make|give yourself|remove|delete|forget|uninstall|"
                        r"get rid of)\b", re.I)
_TASK_TALK = re.compile(r"\b(?:to-?dos?|to do list|task ?list|tasks?)\b", re.I)
# "I'm done with this task", "switch my task to the API": the focus task, not the to-do list
_FOCUS_TALK = re.compile(r"\b(?:focus(?:ed|ing)?|next step|breadcrumb|snooze|on a break|take a break|park|"
                         r"working on|where was i|back to work|distract\w*)\b", re.I)
_TIMER_TALK = re.compile(r"\b(?:timers?|alarms?|remind(?:er|ers)?)\b", re.I)
_SITE_JOB = re.compile(r"\b(?:build|make|create|design|code|change|edit|update|fix|show|run|serve|stop|start)\b.*"
                       r"\b(?:web ?sites?|web ?pages?|web apps?|landing pages?|portfolio(?: site)?)\b", re.I)
_NOT_A_SKILL = re.compile(r"\b(?:web ?sites?|web ?pages?|web apps?|landing pages?|portfolio)\b"
                          r"|^\s*(?:open|launch|close|quit)\b", re.I)
_ABILITY_QUESTION = re.compile(r"^\s*(?:do|does|did|are|is|have|has)\s+you\b|\bare you able\b", re.I)

_ROUTER = """Classify the user's latest message into exactly one category.
Categories:
{categories}

If a use_ category fits the request, pick it rather than chat. Requests to learn, add or remove an
ability are always skills, even when they name an ability that already exists. Building or changing
a website, web page or web app is always websites, and opening an app is always apps.

Previous exchange, for context only (e.g. "make it ten minutes" continues a timer):
{previous}

Reply with JSON only, like {{"category": "chat"}}."""


def route(user_text: str, previous: str, plugins: dict) -> str | None:
    """The category name, or None if the model didn't give a valid one (caller falls back to all tools).
    plugins: tool name -> loaded module, each offered as its own use_<name> category."""
    # a site request or one naming an existing site project always goes to the website builder:
    # "make a landing page" went to create_file, and "make the header blue on sweet crumbs" to web search
    # before the site check: "add a task to complete the website" is a to-do, and it went to the builder
    if _TASK_TALK.search(user_text) and not _TIMER_TALK.search(user_text):
        return "focus" if _FOCUS_TALK.search(user_text) and config.FOCUS_ENABLED else "tasks"
    projects = _site_projects()
    if _SITE_JOB.search(user_text) or any(_project_named(p, user_text) for p in projects):
        return "websites"
    # "text my mom: ..." and "mom's number is ..." are unmistakable; "mom's number" went to memory
    if messaging.SEND.match(user_text) or messaging.SAVE.search(user_text):
        return "messages"
    cats = {name: desc for name, (desc, _, _) in CATEGORIES.items() if name != "focus" or config.FOCUS_ENABLED}
    if projects:
        cats["websites"] += f" (projects so far: {', '.join(projects)})"
    for name, m in plugins.items():
        cats[ABILITY_PREFIX + name] = "learned ability: " + m.SCHEMA["function"]["description"]
    category = _pick(user_text, previous, cats)
    # right after learning "flip a coin", the model sent the next "flip a coin" to skills again;
    # skills only makes sense when the user actually asks to learn or remove something
    # "do you have the ability to create files?" is a question, not a request to build one
    # "build a website" says build, but it's a job for the website builder, not a new ability
    if category == "skills" and (not _SKILL_ASK.search(user_text) or _ABILITY_QUESTION.search(user_text)
                                 or _NOT_A_SKILL.search(user_text)):
        del cats["skills"]
        category = _pick(user_text, previous, cats)
    return category


def _site_projects() -> list[str]:
    sites = Path(config.WORKSPACE_DIR) / "sites"
    return sorted(p.name for p in sites.iterdir() if p.is_dir()) if sites.is_dir() else []


def _project_named(project: str, text: str) -> bool:
    """'sweet-crumbs' is named in "make the header blue on sweet crumbs"."""
    words = re.sub(r"[^a-z0-9]+", " ", project.lower()).split()
    spoken = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
    return bool(words) and f" {' '.join(words)} " in spoken


def _pick(user_text: str, previous: str, cats: dict) -> str | None:
    system = _ROUTER.format(categories="\n".join(f"- {n}: {d}" for n, d in cats.items()),
                            previous=previous or "(none)")
    schema = {"type": "object", "properties": {"category": {"type": "string", "enum": list(cats)}},
              "required": ["category"]}
    for _ in range(2):  # constrained output almost never fails; one retry covers the rest
        try:
            # same num_ctx as the main call: a different one makes Ollama reload the whole model
            resp = llm.client.chat(model=config.OLLAMA_MODEL, format=schema, think=llm.think(),
                                   keep_alive=llm.keep_alive(),
                                   messages=[{"role": "system", "content": system},
                                             {"role": "user", "content": user_text}],
                                   options={"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX,
                                            "num_predict": llm.JSON_TOKENS})
            category = json.loads(resp.message.content).get("category")
        except (json.JSONDecodeError, AttributeError):
            continue
        if category in cats:
            return category
    return None


def tools_for(category: str | None, schemas: list, plugins: dict) -> list:
    """The tool schemas the main call gets for this category (all of them when routing failed)."""
    if category is None:
        return schemas
    if category.startswith(ABILITY_PREFIX):
        # the chosen ability runs first; the others stay on hand for the rest of a request
        # like "make a folder and put a file in it", which needs two abilities in one go
        names = {*plugins, "list_workspace"}
    else:
        names = set(CATEGORIES[category][1])
        if category == "files":
            names |= set(plugins)
        if category == "chat" and config.CLOUD_ENABLED:
            names.add("ask_cloud_expert")
    return [s for s in schemas if s["function"]["name"] in names]


def rules_for(category: str | None) -> str:
    """Extra rules for the main call (all of them when routing failed)."""
    if category and category.startswith(ABILITY_PREFIX):
        return (f"Call {category.removeprefix(ABILITY_PREFIX)} for this request; if the request has more parts, "
                "call the right tool for each. Answer only from the tool results, and never say you did a part "
                "that no tool did.")
    rules = "\n".join(r for _, _, r in CATEGORIES.values()) if category is None else CATEGORIES[category][2]
    if category in ("chat", None) and config.CLOUD_ENABLED:
        rules += ("\nUse ask_cloud_expert only for hard general reasoning or coding, and never put "
                  "personal details in it.")
    return rules
