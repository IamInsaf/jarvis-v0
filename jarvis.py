"""JARVIS v0 — run `python jarvis.py` for voice, or `python jarvis.py --text` to test by typing."""
import argparse
import re

import config
from brain import Brain
from memory import Memory

SLEEP_WORDS = ("goodbye", "shut down", "go to sleep", "power down")
YES = re.compile(r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|do it|keep it|please|absolutely|of course)\b")
NO = re.compile(r"\b(?:no|nope|nah|don'?t|do not|cancel|stop|never ?mind)\b")
# ends a conversation without a reply
DONE = re.compile(r"(?:(?:ok(?:ay)?|no|great|cool|perfect|good|nice|alright)[, ]*)?(?:thanks?(?: you)?(?: jarvis)?|"
                  r"that'?s (?:all|it)|nothing(?: else)?|never ?mind|no thanks?|i'?m good|that'?s fine|bye|"
                  r"cheers|ok(?:ay)?|cool|got it|great|perfect)(?:[, ]*(?:jarvis|sir|mate))?")
_ALWAYS_GHOSTS = {"you", "thanks for watching", "thank you for watching", "uh", "um", "hmm", "the", "and"}
_WHISPER_GHOSTS = _ALWAYS_GHOSTS | {"thank you", "bye", "so", "okay", "oh", "ah", "i"}


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
        for _ in range(3):  # silence or a mumble isn't a "no": ask again rather than throw work away
            reply, _ = ears.listen(wait=8)  # a yes/no after a long question often takes a moment
            reply = (reply or "").lower()
            print(f"YOU: {reply}")
            if NO.search(reply):  # first, so "don't keep it" isn't heard as "keep it"
                return False
            if YES.search(reply):
                return True
            mouth.say(f"Sorry {config.USER_TITLE}, was that a yes or a no?")
        return False

    brain = Brain(memory, confirm, mouth.say)
    mouth.say(f"Online and ready, {config.USER_TITLE}.")
    woken = False
    follow_up = 0  # seconds to keep listening without the wake word after a reply; 0 = wait for it
    asked = False  # the last reply was a question, so a short "okay" is an answer, not "I'm done"
    while True:
        if follow_up:
            # still in the conversation: a short beep says "go on", and silence ends it
            mouth.beep()
            text, audio = ears.listen(wait=follow_up)
            follow_up = 0
            if not text or is_noise(text, asked):
                continue
        else:
            if not woken:
                ears.wait_for_wake_word()
            woken = False
            mouth.say(f"Yes, {config.USER_TITLE}?")
            text, audio = ears.listen()
            if not text:
                continue
        speaker, score = voice_id.identify(audio) if audio is not None else ("unknown", 0.0)
        print(f"YOU ({speaker} {score:.2f}): {text}")
        said = text.lower().strip(" .!?,")
        if said in SLEEP_WORDS:
            mouth.say(f"Going quiet, {config.USER_TITLE}. Say my name when you need me.")
            continue
        if not asked and DONE.fullmatch(said):  # "thanks" / "that's all" ends it, no reply needed
            asked = False
            continue
        answer = brain.think(text, speaker=speaker)
        woken = mouth.say(answer)
        asked = answer.rstrip().endswith("?")
        if not woken and config.FOLLOW_UP:
            # a question waits for its answer; anything else leaves a moment for "and also..."
            follow_up = config.FOLLOW_UP_QUESTION_SECONDS if asked else config.FOLLOW_UP_SECONDS


def is_noise(text: str, asked: bool = False) -> bool:
    """Whisper turns near-silence and background sound into stock phrases ("Thank you.", "you");
    in a follow-up window, where nobody has to speak, those mean nobody did. After a question,
    short words like "okay" are real answers, so only the pure ghosts are dropped."""
    t = text.lower().strip(" .!?,")
    return len(t) < 2 or t in (_ALWAYS_GHOSTS if asked else _WHISPER_GHOSTS)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--text", action="store_true", help="type instead of speaking")
    args = p.parse_args()
    mem = Memory(config.DB_PATH)
    try:
        run_text(mem) if args.text else run_voice(mem)
    except KeyboardInterrupt:
        print("\nJARVIS offline.")
