"""JARVIS v0 — run `python jarvis.py` for voice, or `python jarvis.py --text` to test by typing."""
import argparse
import atexit
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import brains
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
# "show me what you're seeing", "what do you see", "show your POV": the small live camera view by the orb
_YOU_SEE = r"(?:you(?:'re| are)?|youre) (?:seeing|see|looking at|watching)"
_VIEW = (r"(?:pov|p o v|point of view|perspective|view|vision|eyes|camera(?: view| feed)?|(?:live |camera )?feed|"
         r"webcam|cam)")
POV_ON = re.compile(rf"(?:show|let) me what {_YOU_SEE}|show what {_YOU_SEE}|"
                    rf"what (?:are you|you(?:'re| are)|do you|can you|you) (?:seeing|see|looking at|watching)|"
                    rf"(?:show|give)(?: me)? (?:your |the )?{_VIEW}|"
                    # not "open camera": that may be the Windows Camera app
                    r"open (?:your |the )?(?:pov|point of view|view|vision|camera (?:view|feed)|feed)|"
                    rf"let me see (?:what {_YOU_SEE}|"
                    rf"through your eyes|your {_VIEW})|(?:your |the )?(?:pov|point of view)|what you show me")
# "close the view", "hide your POV", "stop showing me": not "close the camera", which may be the Camera app
POV_OFF = re.compile(r"(?:close|hide|stop|exit|dismiss|turn off|switch off)(?: the| your| that)? "
                     r"(?:pov|point of view|view|vision|camera (?:view|feed)|(?:live )?feed|preview)|"
                     r"stop showing(?: me)?(?: (?:that|it|the camera|what you(?:'re| are)? (?:see|seeing)))?")
_POLITE_START = re.compile(r"^(?:(?:hey |ok |okay )?jarvis |(?:can|could|would|will) you |please |just )+")
_POLITE_END = re.compile(r"(?: (?:please|now|jarvis|sir|right now|for me|for a sec(?:ond)?))+$")
# "type mode start" / "start typing": dictate into the focused window until "type mode done"
TYPE_START = re.compile(r"(?:start[\s,-]+)?(?:type|typing|tight|dictation)[\s-]*mode(?:[\s,-]+(?:start|started|on|please))?|"
                        r"start[\s,-]+(?:typing|dictation|dictating)")
YES = re.compile(r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|do it|keep it|please|absolutely|of course)\b")
NO = re.compile(r"\b(?:no|nope|nah|don'?t|do not|cancel|stop|never ?mind)\b")
# ends a conversation without a reply
DONE = re.compile(r"(?:(?:ok(?:ay)?|no|great|cool|perfect|good|nice|alright)[, ]*)?(?:thanks?(?: you)?(?: jarvis)?|"
                  r"that'?s (?:all|it)|nothing(?: else)?|never ?mind|no thanks?|i'?m good|that'?s fine|bye|"
                  r"cheers|ok(?:ay)?|cool|got it|great|perfect)(?:[, ]*(?:jarvis|sir|mate))?")
# said over a reply to just stop it talking: no answer needed ("stop", "okay, quiet", "enough, thanks")
STOP = re.compile(r"(?:(?:ok(?:ay)?|jarvis|no|alright)[, ]*)*(?:stop(?: (?:talking|there|it))?|quiet|be quiet|"
                  r"shush|shh+|hush|enough|that'?s enough|shut up|hold on|wait|pause|silence)"
                  r"(?:[, ]*(?:please|jarvis|sir|thanks?(?: you)?))*")
# "activate voice mode": the mic stays on and "hey Jarvis" works; "voice mode off": back to holding Ctrl+Shift
_VOICE = r"(?:voice|voices|wake word|always on|always listening|hands free|hands-free)"
VOICE_ON = re.compile(rf"(?:activate|enable|start|turn on|switch on|switch to|go to|go into|back to|use|enter) "
                      rf"(?:the )?{_VOICE} mode|{_VOICE} mode(?: on| activate| activated| please)?")
VOICE_OFF = re.compile(rf"(?:deactivate|disable|stop|end|exit|leave|turn off|switch off|cancel|close) "
                       rf"(?:the )?{_VOICE} mode|{_VOICE} mode (?:off|stop|done|over)|"
                       r"(?:switch to |go to |(?:go )?back to |use )?(?:push to talk|push-to-talk|hold to talk|hold mode)"
                       r"(?: mode)?")
_ALWAYS_GHOSTS = {"you", "thanks for watching", "thank you for watching", "uh", "um", "hmm", "the", "and"}
_WHISPER_GHOSTS = _ALWAYS_GHOSTS | {"thank you", "bye", "so", "okay", "oh", "ah", "i"}


def run_text(memory):
    confirm = lambda q: input(f"{q} [y/N] ").strip().lower().startswith("y")
    speak = lambda text: print(f"JARVIS: {text}")  # timers etc. fire on a background thread
    brain = Brain(memory, confirm, speak)
    if brain.tools.focus:
        brain.tools.focus.deliver = speak  # no voice loop to wait for: focus reminders are printed at once
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
    hud.set_camera(eyes.view_png if eyes else None)  # the POV view's frames
    mouth = Mouth(ears, eyes)  # saying "jarvis" again or an open palm interrupts a reply
    voice_id = VoiceID()
    try:
        fingers.start_hotkey(ears)  # hold right Ctrl (TYPE_HOTKEY), speak, let go: typed into any app
    except ValueError as e:
        print(f"  [type] hotkey off: {e}")
    global standby
    try:
        # hold Ctrl+Shift (WAKE_HOTKEY) and talk: the mic is open only while it's held
        standby = fingers.start_wake_hotkey(ears.ptt_down, ears.ptt_up) is not None
    except ValueError as e:
        print(f"  [ears] talk hotkey off, so it's voice mode all the time: {e}")
    ears.standby = standby
    set_voice_mode(ears, not standby or config.VOICE_MODE_AT_START)

    def confirm(q):
        qid = hud.ask(q)  # also answerable with a click on the HUD or the orb
        clicked = lambda: hud.pending_answer(qid) is not None  # noqa: E731
        settled, answer = "timeout", False
        try:
            how = mouth.say(q)
            if how == "palm":  # a palm while it asks means "no, leave it"
                settled = "palm"
                return False
            for _ in range(3):  # silence or a mumble isn't a "no": ask again rather than throw work away
                if clicked():
                    break
                # "yes" said over the question counts; otherwise a yes/no after a long question takes a moment.
                # Outside voice mode the answer comes with the talk hotkey held (or a click)
                if how == "voice":
                    reply, _ = ears.after_barge()
                elif ears.mic_on and how != "hotkey":
                    reply, _ = ears.listen(wait=8, stop=clicked)
                else:
                    reply, _ = ears.wait_ptt(12, stop=clicked)
                how = None
                if clicked():
                    break
                reply = (reply or "").lower()
                print(f"YOU: {reply}")
                if NO.search(reply):  # first, so "don't keep it" isn't heard as "keep it"
                    settled = "voice"
                    return False
                if YES.search(reply):
                    settled, answer = "voice", True
                    return True
                mouth.say(f"Sorry {config.USER_TITLE}, was that a yes or a no?")
            if clicked():
                settled, answer = "click", hud.pending_answer(qid)
                print(f"YOU (clicked): {'yes' if answer else 'no'}")
                return answer
            return False
        finally:
            hud.close_ask(qid, settled, answer)

    brain = Brain(memory, confirm, mouth.say)
    guard = brain.tools.focus  # its reminders are said here, between conversations, never over one
    reminder_due = guard.has_prompt if guard else (lambda: False)
    mouth.say(f"Online and ready, {config.USER_TITLE}. {wake_hint().capitalize()} when you need me.")
    woken = False
    barged = False  # the user talked over the last reply: what they said is the next request
    follow_up = 0  # seconds to keep listening without the wake word after a reply; 0 = wait for it
    asked = False  # the last reply was a question, so a short "okay" is an answer, not "I'm done"
    while True:
        if not (barged or woken or follow_up) and guard and (reminder := guard.take_prompt()):
            how = mouth.say(reminder)
            hud.emit("reply", text=reminder)
            woken, barged = how == "wake", how == "voice"  # "hey Jarvis, snooze" said over it
            continue
        typed = None if barged else hud.take_typed()  # typed into the orb's chat bar (right-click it)
        if typed:
            text, audio = typed, None
        elif barged:
            barged = False
            text, audio = ears.after_barge()
            if not text or is_noise(text) or is_echo(text, brain.last_reply()):
                continue  # a cough, or the mic hearing JARVIS itself
            if STOP.fullmatch(text.lower().strip(" .!?,")):
                print("  [barge] stopped")
                hud.emit("barge", how="stop")
                continue
        elif follow_up:
            # still in the conversation: a short beep says "go on", and silence ends it
            mouth.beep()
            text, audio = ears.listen(wait=follow_up, stop=hud.has_typed)
            follow_up = 0
            if not text or is_noise(text, asked) or is_echo(text, brain.last_reply()):
                continue
        else:
            # "hey Jarvis" anywhere; on camera also "Jarvis, ..." or a hand sign while speaking
            heard = (ears.wait_for_attention(eyes, typed=lambda: hud.has_typed() or reminder_due())
                     if not woken else None)
            woken = False
            if heard and heard.how == "typed":
                continue  # taken at the top of the loop, like a focus reminder that came up
            text, audio = (heard.text, heard.audio) if heard else ("", None)
            if heard and heard.how not in ("wake", "hotkey"):
                print(f"  [eyes] {heard.how}" + (f" ({heard.gesture})" if heard.gesture else ""))
            if not text and heard and heard.how == "hotkey":
                continue  # nothing caught while it was held
            if not text:  # the wake word, or just "Jarvis" on its own
                how = mouth.say(f"Yes, {config.USER_TITLE}?")
                text, audio = ears.after_barge() if how == "voice" else ears.listen(stop=hud.has_typed)
                if not text:
                    continue
        if typed:
            # no voice to recognise: whoever spoke to JARVIS last is the one at the keyboard
            speaker = voice_id.last[0] if voice_id.last else "unknown"
            print(f"YOU ({speaker}, typed): {text}")
            hud.node("ears", "typed")
        else:
            speaker, score = voice_id.identify(audio) if audio is not None else ("unknown", 0.0)
            print(f"YOU ({speaker} {score:.2f}): {text}")
            hud.node("ears", "voice")
        hud.node("voice", speaker)
        said = text.lower().strip(" .!?,")
        if said in SLEEP_WORDS:
            if standby:
                set_voice_mode(ears, False)
            mouth.say(f"Going quiet, {config.USER_TITLE}. {wake_hint().capitalize()} when you need me.")
            continue
        if (on := eyes_command(said)) is not None:
            mouth.say(eyes_switch(eyes, on))
            continue
        if (on := pov_command(said)) is not None:
            mouth.say(pov_switch(eyes, on))
            continue
        if (on := voice_mode_command(said)) is not None:
            mouth.say(voice_mode_switch(ears, on))
            continue
        if TYPE_START.fullmatch(said):
            type_mode(ears, mouth)
            continue
        if not asked and DONE.fullmatch(said):  # "thanks" / "that's all" ends it, no reply needed
            asked = False
            continue
        asked_at = time.time()
        interrupted = mouth.say_stream(brain.think_stream(text, speaker=speaker))  # speaks as it's written
        if mouth.first_audio_at:
            hud.info(first_audio=round(mouth.first_audio_at - asked_at, 2))
        answer = brain.last_reply()
        woken = interrupted == "wake"
        barged = interrupted == "voice"
        if interrupted:
            hud.emit("barge", how=interrupted)
        asked = answer.rstrip().endswith("?")
        if interrupted in ("palm", "hotkey"):  # told to stop, or talking with the hotkey: no follow-up window
            asked = False
            continue
        if not (woken or barged) and config.FOLLOW_UP and ears.mic_on:
            # a question waits for its answer; anything else leaves a moment for "and also..."
            follow_up = config.FOLLOW_UP_QUESTION_SECONDS if asked else config.FOLLOW_UP_SECONDS


def type_mode(ears, mouth):
    """Type everything said into the window that has focus, until "type mode done"."""
    import fingers
    mouth.say(f"Typing, {config.USER_TITLE}. Say \"type mode done\" when you're finished.")
    opened = not ears.mic_on  # outside voice mode the mic stays on just for type mode
    if opened:
        ears.open_mic()
    ears.flush()  # not my own voice
    hud.state("dictating")
    try:
        how = fingers.dictate(lambda wait: ears.listen(wait, flush=False), fingers.type_text)
    except OSError as e:
        mouth.say(f"I couldn't type there, {config.USER_TITLE}. {e}")
        return
    finally:
        if opened and not ears.voice_mode:
            ears.close_mic()
        else:
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


def pov_command(said: str) -> bool | None:
    """True to show the camera view ("show me what you're seeing"), False to close it, None if it's neither.
    Ignores punctuation and polite padding ("jarvis, can you ... please")."""
    t = " ".join(re.sub(r"[^\w' ]", " ", said.lower()).split())
    t = _POLITE_END.sub("", _POLITE_START.sub("", t))
    if POV_OFF.fullmatch(t):
        return False
    if POV_ON.fullmatch(t):
        return True
    return None


def pov_switch(eyes, on: bool) -> str:
    """Open or close the small view of what the camera sees (with what's being tracked drawn on), by the orb."""
    if eyes is None:
        return f"My camera is switched off in settings, {config.USER_TITLE}. Set CAMERA_ENABLED to true to use it."
    if not on:
        hud.pov(False)
        return f"Closed it, {config.USER_TITLE}."
    opened = eyes.show_view()
    hud.pov(True)
    return (f"Here's what I'm seeing, {config.USER_TITLE}." +
            (" I've opened the camera just for this; it closes again with the view." if opened else ""))


def eyes_switch(eyes, on: bool) -> str:
    """Turn the camera on or off by voice; off frees it for video calls and turns the LED off."""
    if eyes is None:
        return f"My camera is switched off in settings, {config.USER_TITLE}. Set CAMERA_ENABLED to true to use it."
    if on == eyes.watching:
        return f"{'Already watching' if on else 'My eyes are already closed'}, {config.USER_TITLE}."
    eyes.resume() if on else eyes.pause()
    hud.info(camera=on)
    if not on:
        hud.pov(False)  # nothing left to show
    return (f"Watching again, {config.USER_TITLE}." if on else
            f"Eyes closed, {config.USER_TITLE}. The camera is free; {wake_hint()} when you need me.")


standby = False     # the talk hotkey is running: outside voice mode, the mic is on only while it's held
voice_mode = False  # the mic stays on and "hey Jarvis" works (always, without the hotkey)


def hotkey_name() -> str:
    return "+".join(k.strip().title() for k in config.WAKE_HOTKEY.split("+"))


def wake_hint() -> str:
    """How to get JARVIS's attention: "hold Ctrl+Shift and talk", or "say "hey Jarvis"" in voice mode."""
    if standby and not voice_mode:
        return f"hold {hotkey_name()} and talk"
    return 'say "hey Jarvis"'


def set_voice_mode(ears, on: bool):
    """Voice mode on: the mic stays open for the wake word. Off: closed until the talk hotkey is held."""
    global voice_mode
    voice_mode = ears.voice_mode = on
    ears.open_mic() if on else ears.close_mic()
    hud.info(mode="voice" if on else "hold to talk")


def voice_mode_command(said: str) -> bool | None:
    """True for "activate voice mode", False for "voice mode off" / "push to talk", None for anything else."""
    t = " ".join(re.sub(r"[^\w' -]", " ", said.lower()).split())
    t = _POLITE_END.sub("", _POLITE_START.sub("", t))
    if VOICE_OFF.fullmatch(t):
        return False
    if VOICE_ON.fullmatch(t):
        return True
    return None


def voice_mode_switch(ears, on: bool) -> str:
    if not standby:
        return (f"Voice mode is always on, {config.USER_TITLE}: the {hotkey_name()} hotkey isn't available, "
                "so there's no hold to talk.")
    if on == voice_mode:
        return f"{'Voice mode is already on' if on else 'Already on hold to talk'}, {config.USER_TITLE}."
    set_voice_mode(ears, on)
    if on:
        return f"Voice mode on, {config.USER_TITLE}. Say \"hey Jarvis\" whenever you need me."
    return f"Voice mode off, {config.USER_TITLE}. Hold {hotkey_name()} and talk when you need me."


def is_echo(text: str, last_reply: str) -> bool:
    """True if what was "heard" is just JARVIS's own last reply picked up by the mic (no echo cancelling):
    nearly every word of it is in that reply."""
    heard = re.findall(r"[a-z']+", text.lower())
    said = set(re.findall(r"[a-z']+", last_reply.lower()))
    return len(heard) >= 3 and sum(w in said for w in heard) / len(heard) >= 0.8


def is_noise(text: str, asked: bool = False) -> bool:
    """Whisper turns near-silence and background sound into stock phrases ("Thank you.", "you");
    in a follow-up window, where nobody has to speak, those mean nobody did. After a question,
    short words like "okay" are real answers, so only the pure ghosts are dropped."""
    t = text.lower().strip(" .!?,")
    return len(t) < 2 or t in (_ALWAYS_GHOSTS if asked else _WHISPER_GHOSTS)


def focus_probe():
    """--focus-test: what the Focus Guardian can see right now, before it starts watching."""
    import focus
    s = focus.load_settings(test_mode=True)
    print("  [focus] test mode: reminders after "
          + ", ".join(f"{s[f'{n}_minutes'] * 60:g}s ({n})" for n in focus.LEVELS.values())
          + f", checked every {s['check_seconds']}s")
    title, proc = focus.foreground_window()
    print(f"  [focus] window in front: {title or '(none)'} [{proc or '?'}]; idle {focus.idle_seconds():.0f}s")
    probe = focus.FocusGuardian(":memory:", s)  # nothing saved, and no watch until a session starts
    latest = probe.claude_latest()
    print("  [focus] latest Claude Code session: " + (f"{latest[0] or latest[1]}, {time.time() - latest[2]:.0f}s ago"
                                                     if latest else "none found"))
    print(f"  [focus] project it would pick: {probe.detect_project() or '(none)'}")


def start_orb(url: str):
    """The desktop orb, in its own process so it can never stall or crash the voice loop."""
    try:
        orb = subprocess.Popen([sys.executable, str(Path(__file__).with_name("orb.py")), url])
    except OSError as e:
        print(f"  [orb] couldn't start: {e}")
        return
    atexit.register(orb.terminate)


_instance = None  # the named mutex, held for as long as JARVIS runs


def already_running(name: str = r"Local\JARVIS-v0-voice") -> bool:
    """True if another JARVIS (voice) is already running, so a second click on the desktop icon doesn't start
    a second one that answers too. Windows only (a named mutex, freed by Windows when the process ends)."""
    global _instance
    if sys.platform != "win32":
        return False
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(ctypes.c_void_p(handle))
        return True
    _instance = handle
    return False


def log_to_file_if_windowless():
    """Started by pythonw (the login shortcut from install_startup.ps1) there's no console: print() would
    go nowhere and a crash would leave no trace, so both go to data/jarvis.log instead."""
    if sys.stdout is not None:
        return
    Path("data").mkdir(exist_ok=True)
    log = open("data/jarvis.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log
    print(f"\n--- JARVIS starting {time.strftime('%Y-%m-%d %H:%M:%S')} ---")


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)  # data/, voices/ and .env are relative to here
    log_to_file_if_windowless()
    p = argparse.ArgumentParser()
    p.add_argument("--text", action="store_true", help="type instead of speaking")
    p.add_argument("--no-camera", action="store_true", help="don't use the webcam even if CAMERA_ENABLED=true")
    p.add_argument("--focus-test", action="store_true",
                   help="Focus Guardian in seconds instead of minutes, printing every check (focus_settings.json)")
    args = p.parse_args()
    if args.focus_test:
        config.FOCUS_TEST_MODE = True
        focus_probe()
    if not args.text and already_running():
        print("JARVIS is already running (the orb shows it). Not starting a second one.")
        sys.exit(0)
    mem = Memory(config.DB_PATH)
    brains.load_saved()  # the model last switched to by voice, over OLLAMA_MODEL in .env
    if config.HUD_ENABLED and (url := hud.start()):
        print(f"  [hud] {url}")
        if config.ORB_ENABLED and not args.text:
            start_orb(url)
    try:
        run_text(mem) if args.text else run_voice(mem, camera=not args.no_camera)
    except KeyboardInterrupt:
        print("\nJARVIS offline.")
