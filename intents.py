"""Intent router: one small, constrained LLM call that picks a category for the request, so the
main call only sees that category's few tools and rules instead of all of them. A 7B model is
reliable at "pick one of these" and much less so at "here are 15 tools, figure it out".
(Not to be confused with router.py, which is the privacy gate for cloud calls.)"""
import json

import ollama

import config

# category -> (what it covers, for the router; tool names it may use; extra rules for the main call)
CATEGORIES = {
    "time": ("the current time or date here or in another place, or date arithmetic", ["get_time", "time_in"],
             "Always call get_time or time_in for a time or date — never state one from memory or work out "
             "a timezone yourself."),
    "timer": ("timers, alarms, countdowns and reminders: setting, checking, listing or cancelling them",
              ["set_timer", "set_alarm", "set_reminder", "list_schedule", "cancel_schedule"],
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
    "skills": ("the user asks you to learn, add or build a new ability, or to remove a learned one",
               ["add_feature", "remove_feature"],
               "To learn a new ability call add_feature; tell the user nothing is saved without their approval.\n"
               "To remove or forget a learned ability call remove_feature."),
    "chat": ("greetings, small talk, opinions, jokes, general knowledge, maths, explanations", [],
             "Answer directly. If it needs current information you don't have, say you'd need to look it up."),
}
# filled in per request with the learned plugins, if there are any
ABILITIES = "using one of these learned abilities: {}"

_ROUTER = """Classify the user's latest message into exactly one category.
Categories:
{categories}

Previous exchange, for context only (e.g. "make it ten minutes" continues a timer):
{previous}

Reply with JSON only, like {{"category": "chat"}}."""


def route(user_text: str, previous: str, plugins: dict) -> str | None:
    """The category name, or None if the model didn't give a valid one (caller falls back to all tools).
    plugins: tool name -> loaded module, offered as an extra "abilities" category."""
    cats = {name: desc for name, (desc, _, _) in CATEGORIES.items()}
    if plugins:
        cats["abilities"] = ABILITIES.format("; ".join(
            f"{name} ({m.SCHEMA['function']['description']})" for name, m in plugins.items()))
    system = _ROUTER.format(categories="\n".join(f"- {n}: {d}" for n, d in cats.items()),
                            previous=previous or "(none)")
    schema = {"type": "object", "properties": {"category": {"type": "string", "enum": list(cats)}},
              "required": ["category"]}
    for _ in range(2):  # constrained output almost never fails; one retry covers the rest
        try:
            # same num_ctx as the main call: a different one makes Ollama reload the whole model
            resp = ollama.chat(model=config.OLLAMA_MODEL, format=schema,
                               messages=[{"role": "system", "content": system},
                                         {"role": "user", "content": user_text}],
                               options={"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX})
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
    if category == "abilities":
        names = set(plugins)
    else:
        names = set(CATEGORIES[category][1])
        if category == "chat" and config.CLOUD_ENABLED:
            names.add("ask_cloud_expert")
    return [s for s in schemas if s["function"]["name"] in names]


def rules_for(category: str | None) -> str:
    """Extra rules for the main call (all of them when routing failed)."""
    if category == "abilities":
        return "Use the learned ability that fits the request."
    rules = "\n".join(r for _, _, r in CATEGORIES.values()) if category is None else CATEGORIES[category][2]
    if category in ("chat", None) and config.CLOUD_ENABLED:
        rules += ("\nUse ask_cloud_expert only for hard general reasoning or coding, and never put "
                  "personal details in it.")
    return rules
