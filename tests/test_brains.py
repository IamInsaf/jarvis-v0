"""Switching the brain model by voice, with Ollama faked. Run: python -m unittest discover tests"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brains  # noqa: E402
import config  # noqa: E402
import llm  # noqa: E402

GB = 1_000_000_000
# what `ollama list` showed: same digest = same file under two tags
INSTALLED = [("qwen2.5:latest", "a", 4.7), ("qwen2.5-coder:latest", "b", 4.7), ("gemma4:latest", "c", 6.6),
             ("qwen3.5-ctf:latest", "d", 6.6), ("gemma4:e4b", "c", 6.6), ("qwen3.5:9b", "e", 6.6),
             ("qwen3.5:4b", "f", 3.3), ("qwen2.5-coder:7b", "b", 4.7), ("qwen2.5:7b", "a", 4.7),
             ("llama3.2:latest", "g", 2.0)]
CAPS = {"gemma4:e4b": ["completion", "tools", "thinking"], "qwen3.5:9b": ["completion", "tools", "thinking"],
        "qwen3.5:4b": ["completion", "tools", "thinking"], "llama3.2:latest": ["completion", "tools"],
        "qwen2.5:7b": ["completion"]}


class FakeClient:
    def __init__(self):
        self.chats, self.unloaded = [], []

    def list(self):
        return SimpleNamespace(models=[SimpleNamespace(model=n, digest=d, size=int(s * GB)) for n, d, s in INSTALLED])

    def show(self, name):
        return SimpleNamespace(capabilities=CAPS.get(name, ["completion", "tools"]))

    def chat(self, model, **kw):
        self.chats.append((model, kw))

    def generate(self, model, keep_alive=None):
        self.unloaded.append(model)


class BrainsTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.tmp = tempfile.TemporaryDirectory()
        patches = [mock.patch.object(llm, "client", self.client), mock.patch.object(llm, "_capabilities", {}),
                   mock.patch.object(brains, "_installed_cache", (0.0, [])),
                   mock.patch.object(brains, "SAVE_PATH", Path(self.tmp.name) / "brain.json"),
                   mock.patch.object(config, "OLLAMA_MODEL", "qwen3.5:4b"),
                   mock.patch.object(config, "OLLAMA_THINK", True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_resolves_loose_spoken_names(self):
        cases = {"gemma": "gemma4:e4b", "gamma": "gemma4:e4b", "qwen 9b": "qwen3.5:9b",
                 "qwen nine b": "qwen3.5:9b", "the small one": "qwen3.5:4b", "the big one": "qwen3.5:9b",
                 "llama": "llama3.2:latest", "qwen 2.5": "qwen2.5:7b", "qwen coder": "qwen2.5-coder:7b",
                 "qwen three point five four b": "qwen3.5:4b", "qwen3.5-ctf": "qwen3.5-ctf:latest"}
        for said, want in cases.items():
            self.assertEqual(brains.resolve(said), (want, []), said)

    def test_ambiguous_and_unknown(self):
        self.assertEqual(brains.resolve("qwen"), (None, ["qwen3.5:4b", "qwen3.5:9b"]))
        self.assertEqual(brains.resolve("banana"), (None, []))
        self.assertTrue(brains.switch("qwen").endswith("?"))
        self.assertIn("Gemma 4 E4B", brains.switch("banana"))
        self.assertEqual(config.OLLAMA_MODEL, "qwen3.5:4b")

    def test_parse_catches_only_brain_requests(self):
        self.assertEqual(brains.parse("switch to gemma"), ("set_brain", {"model": "gemma"}))
        self.assertEqual(brains.parse("Jarvis, switch your brain to qwen 9b please"),
                         ("set_brain", {"model": "qwen 9b"}))
        self.assertEqual(brains.parse("use gemma as your brain"), ("set_brain", {"model": "gemma"}))
        self.assertEqual(brains.parse("which brain are you using?"), ("which_brain", {}))
        self.assertEqual(brains.parse("what models do you have"), ("list_brains", {}))
        for other in ("what's the weather", "switch to dark mode", "switch to wi fi",
                      "use gemma to write a long poem about cats"):
            self.assertIsNone(brains.parse(other), other)

    def test_switch_loads_saves_and_unloads_the_old_one(self):
        said = brains.switch("gemma")
        self.assertIn("Gemma 4 E4B", said)
        self.assertEqual(config.OLLAMA_MODEL, "gemma4:e4b")
        self.assertEqual(self.client.chats[0][0], "gemma4:e4b")
        self.assertEqual(self.client.unloaded, ["qwen3.5:4b"])
        config.OLLAMA_MODEL = "qwen3.5:4b"
        brains.load_saved()
        self.assertEqual(config.OLLAMA_MODEL, "gemma4:e4b")

    def test_refuses_a_model_without_tools(self):
        self.assertIn("can't use tools", brains.switch("qwen 2.5"))
        self.assertEqual(config.OLLAMA_MODEL, "qwen3.5:4b")

    def test_saved_model_gone_falls_back(self):
        brains.SAVE_PATH.write_text('{"model": "mistral:7b"}')
        brains.load_saved()
        self.assertEqual(config.OLLAMA_MODEL, "qwen3.5:4b")

    def test_think_only_for_models_that_can(self):
        self.assertTrue(llm.think())
        config.OLLAMA_MODEL = "llama3.2:latest"
        self.assertFalse(llm.think())


if __name__ == "__main__":
    unittest.main()
