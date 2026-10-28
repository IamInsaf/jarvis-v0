"""Streaming replies: clause chunking, thinking/tool-call tags split across pieces, and Brain.think_stream with
Ollama faked. Run: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402
import config  # noqa: E402
import intents  # noqa: E402
import llm  # noqa: E402
import speech_chunker  # noqa: E402
from speech_chunker import Chunker  # noqa: E402


def pieces(text: str, size: int = 3) -> list[str]:
    """Text cut into small pieces, the way a model streams it."""
    return [text[i:i + size] for i in range(0, len(text), size)]


class ChunkerTest(unittest.TestCase):
    def test_splits_at_sentence_ends(self):
        out = list(speech_chunker.clauses(pieces("Good evening, sir. The weather is fine! Shall I go on?")))
        self.assertEqual(out, ["Good evening, sir.", "The weather is fine!", "Shall I go on?"])

    def test_no_split_inside_numbers_or_abbreviations(self):
        out = list(speech_chunker.clauses(pieces("Version 3.5 is out. Dr. Smith agrees with Mr. Jones.")))
        self.assertEqual(out, ["Version 3.5 is out.", "Dr. Smith agrees with Mr. Jones."])

    def test_first_clause_may_end_at_a_comma(self):
        c = Chunker()
        first = c.feed("Certainly, sir, I have looked into the matter carefully, ")
        self.assertEqual(first, ["Certainly, sir, I have looked into the matter carefully,"])

    def test_short_comma_is_not_a_split(self):
        self.assertEqual(Chunker().feed("Yes, sir, "), [])

    def test_long_run_without_punctuation_is_capped(self):
        out = list(speech_chunker.clauses(pieces("word " * 60)))
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(c) <= speech_chunker.MAX_CHARS for c in out))
        self.assertEqual(" ".join(out).split(), ["word"] * 60)

    def test_flush_returns_unterminated_rest(self):
        c = Chunker()
        self.assertEqual(c.feed("No full stop here"), [])
        self.assertEqual(c.flush(), ["No full stop here"])
        self.assertEqual(c.flush(), [])


class TagFilterTest(unittest.TestCase):
    def run_filter(self, text, size=2):
        f = llm._TagFilter()
        return "".join(f.feed(p) for p in pieces(text, size)) + f.flush()

    def test_drops_think_split_across_pieces(self):
        self.assertEqual(self.run_filter("<think>hmm, the user</think>Hello sir."), "Hello sir.")

    def test_drops_written_out_tool_call(self):
        self.assertEqual(self.run_filter('Done.<tool_call>{"name": "x"}</tool_call> Anything else?'),
                         "Done. Anything else?")

    def test_unclosed_tag_is_dropped(self):
        self.assertEqual(self.run_filter("Right.<tool_call>{\"name\""), "Right.")

    def test_lone_less_than_is_kept(self):
        self.assertEqual(self.run_filter("2 < 3, sir."), "2 < 3, sir.")


class FakeTools:
    def __init__(self, memory, confirm, speak):
        self.plugins, self.schemas, self.speaker, self.user_text = {}, [], None, ""
        self.ran = []

    def run(self, name, args):
        self.ran.append((name, args))
        return f"ran {name}"


class FakeMemory:
    def __init__(self):
        self.facts, self.logged = [], []

    def relevant_facts(self, text, speaker=None):
        return []

    def remember(self, fact, speaker=None):
        self.facts.append(fact)

    def log(self, role, content):
        self.logged.append((role, content))

    def log_route(self, *a):
        pass


class FakeClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.streamed = 0

    def chat(self, model, messages, stream=False, **kw):
        if stream:
            self.streamed += 1
            return iter(SimpleNamespace(message=SimpleNamespace(content=p)) for p in pieces(self.reply, 4))
        return SimpleNamespace(message=SimpleNamespace(content=self.reply))

    def show(self, model):
        return SimpleNamespace(capabilities=["completion", "tools"])


class BrainStreamTest(unittest.TestCase):
    def make(self, reply: str, category="chat"):
        self.client = FakeClient(reply)
        for p in [mock.patch.object(brain, "Tools", FakeTools), mock.patch.object(llm, "client", self.client),
                  mock.patch.object(intents, "route", lambda *a: category),
                  mock.patch.object(intents, "tools_for", lambda *a: []),
                  mock.patch.object(config, "USER_TITLE", "sir")]:
            p.start()
            self.addCleanup(p.stop)
        self.memory = FakeMemory()
        return brain.Brain(self.memory, confirm=lambda q: False)

    def test_reply_comes_clause_by_clause(self):
        b = self.make("Good evening, sir. It is a fine night for it. Anything else?")
        out = list(b.think_stream("how are you"))
        self.assertEqual(self.client.streamed, 1)
        self.assertEqual(len(out), 3)
        self.assertEqual("".join(out), "Good evening, sir. It is a fine night for it. Anything else?")
        self.assertEqual(b.last_reply(), "Good evening, sir. It is a fine night for it. Anything else?")

    def test_think_matches_stream_text(self):
        b = self.make("Good evening, sir. All is well.")
        self.assertEqual(b.think("hello"), "Good evening, sir. All is well.")
        self.assertEqual(self.client.streamed, 0)

    def test_unbacked_claim_is_never_spoken(self):
        b = self.make("Certainly, sir. I have turned the lights up for you. Anything else?")
        out = "".join(b.think_stream("make the lights brighter"))
        self.assertNotIn("turned the lights", out)
        self.assertIn("can't actually do that", out)
        self.assertNotIn("Anything else", out)

    def test_interrupted_turn_keeps_what_was_said(self):
        b = self.make("First sentence here. Second sentence here. Third one.")
        gen = b.think_stream("tell me something")
        self.assertEqual(next(gen), "First sentence here.")
        gen.close()  # the user said "Jarvis" over it
        self.assertEqual(b.last_reply(), "First sentence here.")
        self.assertEqual(len(b.history), 1)

    def test_fast_path_needs_no_model(self):
        b = self.make("unused")
        out = list(b.think_stream("what time is it"))
        self.assertEqual(len(out), 1)
        self.assertEqual(b.tools.ran[0][0], "get_time")
        self.assertEqual(self.client.streamed, 0)

    def test_ollama_down_is_spoken(self):
        b = self.make("x")

        def boom(*a, **kw):
            raise ConnectionError("refused")
        self.client.chat = boom
        out = "".join(b.think_stream("hello"))
        self.assertIn("thinking engine isn't responding", out)


if __name__ == "__main__":
    unittest.main()
