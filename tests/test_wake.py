"""Wake word loading: the voice verifier from enroll_wake.py is used only once it exists. No mic needed.
Run: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import ears  # noqa: E402


class WakeModelTest(unittest.TestCase):
    def load(self, exists: bool, path="data/wake_verifier.pkl"):
        with mock.patch.object(ears, "Model") as model, \
                mock.patch.object(ears.openwakeword.utils, "download_models"), \
                mock.patch.object(ears.os.path, "exists", return_value=exists), \
                mock.patch.object(config, "WAKE_VERIFIER", path), \
                mock.patch("builtins.print"):
            ears.load_wake_model()
        return model.call_args.kwargs

    def test_stock_model_until_you_enroll(self):
        self.assertNotIn("custom_verifier_models", self.load(exists=False))

    def test_verifier_once_enrolled(self):
        kw = self.load(exists=True)
        self.assertEqual(kw["custom_verifier_models"], {"hey_jarvis": "data/wake_verifier.pkl"})
        self.assertEqual(kw["custom_verifier_threshold"], config.WAKE_VERIFIER_THRESHOLD)
        self.assertEqual(kw["wakeword_models"], ["hey_jarvis"])

    def test_empty_setting_turns_it_off(self):
        self.assertNotIn("custom_verifier_models", self.load(exists=True, path=""))


if __name__ == "__main__":
    unittest.main()
