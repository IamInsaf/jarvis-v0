"""JARVIS v0 — run `python jarvis.py` for voice, or `python jarvis.py --text` to test by typing."""
import argparse

import config
from brain import Brain
from memory import Memory

SLEEP_WORDS = ("goodbye", "shut down", "go to sleep", "power down")


def run_text(memory):
    confirm = lambda q: input(f"{q} [y/N] ").strip().lower().startswith("y")
    speak = lambda text: print(f"JARVIS: {text}")  # timers etc. fire on a background thread
    brain = Brain(memory, confirm, speak)
    print("JARVIS (text mode). Type 'exit' to quit.")
    while True:
        text = input("YOU: ").strip()
        if text.lower() in ("exit", "quit"):
            break
        if text:
            print(f"JARVIS: {brain.think(text)}")


def run_voice(memory):
    from ears import Ears
    from mouth import Mouth
    from voice_id import VoiceID

    ears = Ears()
    mouth = Mouth(ears)  # saying "jarvis" again mid-reply interrupts it and wakes it early
    voice_id = VoiceID()

    def confirm(q):
        mouth.say(q)
        reply, _ = ears.listen()
        reply = reply.lower()
        print(f"YOU: {reply}")
        return any(w in reply for w in ("yes", "yeah", "sure", "go ahead", "do it"))

    brain = Brain(memory, confirm, mouth.say)
    mouth.say(f"Online and ready, {config.USER_TITLE}.")
    woken = False
    while True:
        if not woken:
            ears.wait_for_wake_word()
        woken = False
        mouth.say(f"Yes, {config.USER_TITLE}?")
        text, audio = ears.listen()
        if not text:
            continue
        speaker, score = voice_id.identify(audio) if audio is not None else ("unknown", 0.0)
        print(f"YOU ({speaker} {score:.2f}): {text}")
        if text.lower().strip(" .!?") in SLEEP_WORDS:
            mouth.say(f"Going quiet, {config.USER_TITLE}. Say my name when you need me.")
            continue
        woken = mouth.say(brain.think(text, speaker=speaker))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--text", action="store_true", help="type instead of speaking")
    args = p.parse_args()
    mem = Memory(config.DB_PATH)
    try:
        run_text(mem) if args.text else run_voice(mem)
    except KeyboardInterrupt:
        print("\nJARVIS offline.")
