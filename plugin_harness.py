"""Test-runs a drafted plugin in its own process (see selfcode._test_run):
    python -I plugin_harness.py <draft_plugin.py> <project dir>
Prints {"result": ..., "created": [...]} as the last line on success; exits non-zero with a
traceback otherwise. ctx is a stand-in: memory writes go nowhere, speak prints, confirm always
answers no, and files work for real but in a throwaway workspace inside the test's own folder."""
import importlib.util
import json
import os
import sys


class _Memory:
    def remember(self, fact, **_):
        return "Saved."

    def recall(self, query):
        return "Nothing stored about that."

    def forget(self, text):
        return "Forgot 0 fact(s)."


class _Ctx:
    memory = _Memory()

    @staticmethod
    def speak(text):
        print(f"(would say: {text})")

    @staticmethod
    def confirm(question):
        print(f"(would ask: {question} -> no)")
        return False


def main(path: str, project_dir: str):
    sys.path.append(project_dir)  # so a plugin's `import config` works
    from files import Workspace
    workspace = Workspace(os.path.join(os.getcwd(), "Jarvis"), _Ctx.confirm)
    _Ctx.files = workspace
    spec = importlib.util.spec_from_file_location("draft_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fn = module.SCHEMA["function"]
    props = fn["parameters"].get("properties", {})
    args = module.TEST_ARGS
    if not isinstance(args, dict):
        raise TypeError("TEST_ARGS must be a dict")
    missing = [k for k in fn["parameters"].get("required", []) if k not in args]
    if missing:
        raise ValueError(f"TEST_ARGS is missing required args: {missing}")
    unknown = [k for k in args if k not in props]
    if unknown:
        raise ValueError(f"TEST_ARGS has args that SCHEMA doesn't define: {unknown}")

    result = module.run(args, _Ctx())
    if not isinstance(result, str) or not result.strip():
        raise TypeError(f"run() must return a non-empty string, got {result!r}")
    created = sorted({os.path.relpath(p, workspace.root) for p in workspace.created})
    print(json.dumps({"result": result.strip(), "created": created}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
