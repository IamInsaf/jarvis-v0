"""Privacy router: the only code path that can send text to a cloud model.
Off unless CLOUD_ENABLED=true. Redacts obvious personal data and (by default) asks you first."""
import re

import config

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d[\d\s-]{7,}\d")
_LONGNUM = re.compile(r"\b\d{6,}\b")


def redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return _LONGNUM.sub("[number]", text)


def ask_cloud(question: str, confirm, label: str | None = None) -> str:
    """label: a short, speakable description of what's being sent, shown/spoken for confirmation
    instead of the full question (which may be long or technical, e.g. a code-generation prompt)."""
    if not config.CLOUD_ENABLED:
        return "Cloud model is disabled. Answer locally as best you can."
    q = redact(question)
    shown = redact(label) if label else q
    if config.CLOUD_CONFIRM and not confirm(f"That one needs the cloud model. Shall I send: '{shown}'?"):
        return "The user declined sending this to the cloud. Answer locally as best you can."
    import anthropic  # imported lazily so the package is optional
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    msg = client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=1024,
                                 messages=[{"role": "user", "content": q}])
    return "".join(b.text for b in msg.content if b.type == "text")
