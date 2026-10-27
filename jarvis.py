"""JARVIS v0 — run `python jarvis.py` for voice, or `python jarvis.py --text` to test by typing."""
import argparse
import re

import config
import hud
from brain import Brain
from memory import Memory

SLEEP_WORDS = ("goodbye", "shut down", "go to sleep", "power down")
# "off the camera", "eye off", "turn the camera off"... Whisper often hears "eyes" as "i", "eye" or "ice"
_EYE = r"(?:the |your |my )?(?:camera|webcam|cam|eyes?|ice|i)"
# "open/close camera" is left alone: that may mean the Windows Camera app
EYES_OFF = re.compile(rf"(?:(?:turn|switch|shut) ?(?:off|of)|off|of|disable|stop) {_EYE}|"
                      rf"(?:turn |switch |shut )?{_EYE} (?:off|of)|stop watching|close (?:your |my )?(?:eyes|ice)")
EYES_ON = re.compile(rf"(?:(?:turn|switch) ?on|on|enable|start) {_EYE}|"
                     rf"(?:turn |switch )?{_EYE} on|start watching|open (?:your |my )?(?:eyes|ice)")
# "type mode start" / "start typing": dictate into the focused window until "type mode done"
TYPE_START = re.compile(r"(?:start[\s,-]+)?(?:type|typing|tight|dictation)[\s-]*mode(?:[\s,-]+(?:start|started|on|please))?|"
                        r"start[\s,-]+(?:typing|dictation|dictating)")
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
    hud.info(mode="text")
    print("JARVIS (text mode). Type 'exit' to quit.")
    while True:
        text = input("YOU: ").strip()
        if text.lower() in ("exit", "quit"):
            break
        if text:
            hud.node("ears", "typed")
            print(f"JARVIS: {brain.think(text)}")


def run_voice(memory, camera: bool = True):
    import fingers
    from ears import Ears
    from mouth import Mouth
    from voice_id import VoiceID

    ears = Ears()
    eyes = None
    if camera and config.CAMERA_ENABLED:
        from eyes import Eyes
        eyes = Eyes(watching=config.CAMERA_START_ON)  # off by default: say "camera on" to use it
        if not eyes.watching:
            print("  [eyes] camera off; say \"camera on\" to use it")
    hud.info(mode="voice", camera=eyes.watching if eyes else None)
    mouth = Mouth(ears, eyes)  # saying "jarvis" again or an open palm interrupts a reply
    voice_id = VoiceID()
    try:
        fingers.start_hotkey(ears)  # hold right Ctrl (TYPE_HOTKEY), speak, let go: typed into any app
    except ValueError as e:
        print(f"  [type] hotkey off: {e}")

    def confirm(q):
        if mouth.say(q) == "palm":  # a palm while it asks means "no, leave it"
            return False
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
            # "hey Jarvis" anywhere; on camera also "Jarvis, ..." or a hand sign while speaking
            heard = ears.wait_for_attention(eyes) if not woken else None
            woken = False
            text, audio = (heard.text, heard.audio) if heard else ("", None)
            if heard and heard.how != "wake":
                print(f"  [eyes] {heard.how}" + (f" ({heard.gesture})" if heard.gesture else ""))
            if not text:  # the wake word, or just "Jarvis" on its own
                mouth.say(f"Yes, {config.USER_TITLE}?")
                text, audio = ears.listen()
                if not text:
                    continue
        speaker, score = voice_id.identify(audio) if audio is not None else ("unknown", 0.0)
        print(f"YOU ({speaker} {score:.2f}): {text}")
        hud.node("ears", "voice")
        hud.node("voice", speaker)
        said = text.lower().strip(" .!?,")
        if said in SLEEP_WORDS:
            mouth.say(f"Going quiet, {config.USER_TITLE}. Say my name when you need me.")
            continue
        if (on := eyes_command(said)) is not None:
            mouth.say(eyes_switch(eyes, on))
            continue
        if TYPE_START.fullmatch(said):
            type_mode(ears, mouth)
            continue
        if not asked and DONE.fullmatch(said):  # "thanks" / "that's all" ends it, no reply needed
            asked = False
            continue
        answer = brain.think(text, speaker=speaker)
        interrupted = mouth.say(answer)
        woken = interrupted == "wake"
        asked = answer.rstrip().endswith("?")
        if interrupted == "palm":  # told to stop: no follow-up window either
            asked = False
            continue
        if not woken and config.FOLLOW_UP:
            # a question waits for its answer; anything else leaves a moment for "and also..."
            follow_up = config.FOLLOW_UP_QUESTION_SECONDS if asked else config.FOLLOW_UP_SECONDS


def type_mode(ears, mouth):
    """Type everything said into the window that has focus, until "type mode done"."""
    import fingers
    mouth.say(f"Typing, {config.USER_TITLE}. Say \"type mode done\" when you're finished.")
    ears.flush()  # not my own voice
    hud.state("dictating")
    try:
        how = fingers.dictate(lambda wait: ears.listen(wait, flush=False), fingers.type_text)
    except OSError as e:
        mouth.say(f"I couldn't type there, {config.USER_TITLE}. {e}")
        return
    finally:
        hud.state("idle")
    mouth.say(f"Type mode off, {config.USER_TITLE}." if how == "done" else
              f"It went quiet, so I've stopped typing, {config.USER_TITLE}.")


def eyes_command(said: str) -> bool | None:
    """True for "camera on", False for "camera off", None if it's neither. Ignores punctuation,
    a leading "jarvis" and a trailing "please"."""
    t = " ".join(re.sub(r"[^\w' ]", " ", said.lower()).split())
    t = re.sub(r"^(?:hey |ok |okay )?jarvis ", "", t)
    t = re.sub(r" (?:please|now|jarvis|sir)$", "", t)
    if EYES_OFF.fullmatch(t):
        return False
    if EYES_ON.fullmatch(t):
        return True
    return None


def eyes_switch(eyes, on: bool) -> str:
    """Turn the camera on or off by voice; off frees it for video calls and turns the LED off."""
    if eyes is None:
        return f"My camera is switched off in settings, {config.USER_TITLE}. Set CAMERA_ENABLED to true to use it."
    if on == eyes.watching:
        return f"{'Already watching' if on else 'My eyes are already closed'}, {config.USER_TITLE}."
    eyes.resume() if on else eyes.pause()
    hud.info(camera=on)
    return (f"Watching again, {config.USER_TITLE}." if on else
            f"Eyes closed, {config.USER_TITLE}. The camera is free; say \"hey Jarvis\" when you need me.")


def is_noise(text: str, asked: bool = False) -> bool:
    """Whisper turns near-silence and background sound into stock phrases ("Thank you.", "you");
    in a follow-up window, where nobody has to speak, those mean nobody did. After a question,
    short words like "okay" are real answers, so only the pure ghosts are dropped."""
    t = text.lower().strip(" .!?,")
    return len(t) < 2 or t in (_ALWAYS_GHOSTS if asked else _WHISPER_GHOSTS)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--text", action="store_true", help="type instead of speaking")
    p.add_argument("--no-camera", action="store_true", help="don't use the webcam even if CAMERA_ENABLED=true")
    args = p.parse_args()
    mem = Memory(config.DB_PATH)
    if config.HUD_ENABLED and (url := hud.start()):
        print(f"  [hud] {url}")
    try:
        run_text(mem) if args.text else run_voice(mem, camera=not args.no_camera)
    except KeyboardInterrupt:
        print("\nJARVIS offline.")
