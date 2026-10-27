"""The one Ollama client everything uses. It has a timeout because a stuck Ollama (seen when it swaps
between the main and coder models on a small GPU) otherwise freezes JARVIS forever, mid-sentence."""
import httpx
import ollama

import config

client = ollama.Client(timeout=config.OLLAMA_TIMEOUT)

# caps on generated tokens. Ollama has none by default, and a small model under a JSON constraint can
# emit whitespace forever — the request never ends and Ollama can't even unload the model
JSON_TOKENS = 200   # a router pick or a tool's arguments
REPLY_TOKENS = 400  # a spoken reply of a few sentences
CODE_TOKENS = 2500  # a whole plugin file

# what a dead, stuck or missing Ollama looks like to callers
ERRORS = (httpx.HTTPError, ollama.ResponseError, ConnectionError)

_capabilities: dict[str, list] = {}


def capabilities(model: str) -> list:
    """What Ollama says the model can do: "tools", "thinking", "vision"... ([] if Ollama can't say)."""
    if model not in _capabilities:
        try:
            _capabilities[model] = list(client.show(model).capabilities or [])
        except ERRORS:
            return []
    return _capabilities[model]


def think() -> bool:
    """OLLAMA_THINK for the current brain, if it can think: asking a model without it fails the call,
    and the brain can be switched by voice (brains.py) to one that can't."""
    return config.OLLAMA_THINK and "thinking" in capabilities(config.OLLAMA_MODEL)
