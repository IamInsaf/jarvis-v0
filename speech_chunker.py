"""Turns a reply streaming in token by token into speakable clauses, so the voice starts on the first
clause while the model is still writing the rest. Plain string handling: nothing here may be slow."""
import re

# a sentence end: . ! ? followed by space (so "3.5" and "e.g" mid-word don't split), or ; : always
_SENTENCE = re.compile(r"[.!?]+[\"')\]]*\s|[;:]\s")
_COMMA = re.compile(r",\s")
# abbreviations whose full stop isn't the end of a sentence
_ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|vs|etc|e\.g|i\.e|approx|No)\.\s$", re.I)
FIRST_COMMA_AT = 25   # the first clause may end at a comma this early: gets the voice going sooner
COMMA_AT = 70         # later clauses only split at a comma once they're this long
MAX_CHARS = 180       # never hold more than this: split at the last space


class Chunker:
    def __init__(self):
        self.buf = ""
        self.emitted = 0

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return the clauses now complete (maybe none)."""
        self.buf += text
        out = []
        while (clause := self._take()) is not None:
            if clause:
                out.append(clause)
                self.emitted += 1
        return out

    def flush(self) -> list[str]:
        """The rest, once the stream has ended."""
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []

    def _take(self) -> str | None:
        for m in _SENTENCE.finditer(self.buf):
            if not _ABBREV.search(self.buf[:m.end()]):
                return self._cut(m.end())
        limit = FIRST_COMMA_AT if self.emitted == 0 else COMMA_AT
        for m in _COMMA.finditer(self.buf):
            if m.start() >= limit:
                return self._cut(m.end())
        if len(self.buf) > MAX_CHARS:
            space = self.buf.rfind(" ", 0, MAX_CHARS)
            return self._cut(space + 1 if space > 0 else MAX_CHARS)
        return None

    def _cut(self, at: int) -> str:
        clause, self.buf = self.buf[:at].strip(), self.buf[at:]
        return clause


def clauses(tokens) -> "iter[str]":
    """Every clause of a token stream, in order."""
    chunker = Chunker()
    for token in tokens:
        yield from chunker.feed(token)
    yield from chunker.flush()
