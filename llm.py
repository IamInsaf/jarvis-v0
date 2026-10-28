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


def keep_alive():
    """How long Ollama keeps a model loaded after its last call (BRAIN_IDLE_MINUTES; -1 = for good).
    The next call after that just loads it again."""
    return f"{config.BRAIN_IDLE_MINUTES:g}m" if config.BRAIN_IDLE_MINUTES > 0 else -1


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


class _TagFilter:
    """Drops <think>...</think> and <tool_call>...</tool_call> from a reply arriving in pieces, where a tag
    can be split across two pieces. Text that might be the start of a tag is held back until it's clear."""
    TAGS = (("<think>", "</think>"), ("<tool_call>", "</tool_call>"))

    def __init__(self):
        self.buf, self.closing = "", None

    def feed(self, text: str) -> str:
        self.buf += text
        out = ""
        while self.buf:
            if self.closing:
                end = self.buf.find(self.closing)
                if end < 0:
                    self.buf = self.buf[-len(self.closing):]  # keep enough to spot a split closing tag
                    return out
                self.buf, self.closing = self.buf[end + len(self.closing):], None
                continue
            starts = [(self.buf.find(o), o, c) for o, c in self.TAGS if o in self.buf]
            if starts:
                at, opening, closing = min(starts)
                out, self.buf, self.closing = out + self.buf[:at], self.buf[at + len(opening):], closing
                continue
            lt = self.buf.rfind("<")
            if lt >= 0 and any(o.startswith(self.buf[lt:]) for o, _ in self.TAGS):
                out, self.buf = out + self.buf[:lt], self.buf[lt:]  # maybe the start of a tag: wait for more
                return out
            out, self.buf = out + self.buf, ""
        return out

    def flush(self) -> str:
        rest, self.buf = ("" if self.closing else self.buf), ""
        return rest


def stream_chat(messages: list, num_predict: int = REPLY_TOKENS):
    """The model's reply as it's written, a piece at a time, with any thinking or written-out tool calls removed."""
    tags = _TagFilter()
    for part in client.chat(model=config.OLLAMA_MODEL, messages=messages, think=think(), stream=True,
                            keep_alive=keep_alive(),
                            options={"temperature": config.OLLAMA_TEMPERATURE, "num_ctx": config.OLLAMA_NUM_CTX,
                                     "num_predict": num_predict}):
        if text := tags.feed(part.message.content or ""):
            yield text
    if rest := tags.flush():
        yield rest
