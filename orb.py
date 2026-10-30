"""Orb: a small always-on-top JARVIS orb on the desktop, over every app. It glows in the colour of what JARVIS is
doing (listening, thinking, speaking), pulses with the voice, shows live captions, and puts YES / NO buttons on
a question JARVIS asks out loud.

It's its own process, started by jarvis.py (or `python orb.py [hud url]` next to a running JARVIS), and only
reads the HUD's live event stream: closing or crashing it never touches JARVIS. Stdlib only (tkinter).
Left-click opens the full HUD, drag moves it (the spot is remembered), right-click opens a chat bar to type a
request instead of saying it (Enter sends, Esc closes; its ⋯ button has the menu). Ask JARVIS to show what it
sees and a small live camera view opens above the orb, with boxes round the faces and hands it's tracking."""
import base64
import json
import math
import queue
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# the HUD's palette (hud.py STATES): colour, spin speed
STATES = {
    "idle": ((42, 212, 255), .45), "listening": ((110, 240, 255), 1.1), "thinking": ((255, 181, 71), 2.8),
    "speaking": ((175, 246, 255), 1.5), "dictating": ((93, 255, 176), 1.1), "error": ((255, 77, 94), .25),
    "offline": ((95, 120, 135), .12), "asking": ((255, 181, 71), .9),
}
ACTIVE = {"listening", "thinking", "speaking", "dictating", "asking"}
CAPTION_HOLD = 4.0   # seconds a caption stays after JARVIS goes quiet
POV_FPS = 12         # camera frames a second the POV view asks for
POV_WIDTH = 320      # its width in pixels, at 100% display scaling (16:9)
IDLE_AFTER = 4.0     # seconds of quiet before the orb dims (or hides, with ORB_IDLE=hide)
BG = (4, 18, 28)     # the orb's dark body; things fade towards this, never towards the see-through key
KEY = "#010203"      # this exact colour is see-through (and click-through) on Windows


def mix(a, b, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return tuple(x + (y - x) * t for x, y in zip(a, b))  # floats: rounding each step would stall short of b


def hexrgb(c) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(x))) for x in c)


class OrbModel:
    """Everything the orb shows, worked out from HUD events; no window, so it can be tested."""

    def __init__(self, idle: str = "dim"):
        self.idle = idle
        self.state = "offline"
        self.mic = self.voice = 0.0
        self.level = 0.0       # smoothed loudness that drives the pulse
        self.rgb = STATES["offline"][0]
        self.spin = 0.0
        self.caption, self.caption_kind, self.caption_at = "", "", 0.0
        self.ask: tuple[int, str] | None = None
        self.active_at = 0.0   # last time JARVIS was doing something
        self.pov = False       # the camera view is open

    def feed(self, e: dict, now: float | None = None):
        now = time.time() if now is None else now
        kind = e.get("kind")
        if kind == "state":
            self.state = e.get("state") if e.get("state") in STATES else "idle"
            if self.state in ACTIVE:
                self.active_at = now
            if self.state == "listening" and self.caption_kind not in ("ask", "gate"):
                self._say("Listening…", "status", now)
        elif kind == "level":
            self.mic, self.voice = float(e.get("mic") or 0), float(e.get("voice") or 0)
        elif kind == "_offline":  # the stream dropped: JARVIS isn't running (yet)
            self.state, self.ask, self.pov = "offline", None, False
            self._say("", "", 0.0)
        elif kind == "heard":
            self._say(f"“{e.get('text', '')}”", "heard", now)
        elif kind == "say":
            self._say(self.caption + " " + e["text"] if self.caption_kind == "say" else e["text"], "say", now)
        elif kind == "ack":
            self._say(e.get("text", ""), "say", now)
        elif kind == "gate" and e.get("tier") == "blocked":
            self._say(f"⛔ Blocked {e.get('tool', '')}: {e.get('why', '')}", "gate", now)
        elif kind == "ask":
            self.ask = (e["ask"], e.get("question", ""))
            self._say(e.get("question", ""), "ask", now)
        elif kind == "answered":
            if self.ask and self.ask[0] == e.get("ask"):
                self.ask = None
                self._say("✓ Yes" if e.get("yes") else "✗ No", "status", now)
        elif kind == "pov":
            self.pov = bool(e.get("on"))
        elif kind == "typed":
            self._say(f"⌨ {e.get('text', '')}", "heard", now)
        elif kind == "_typed_failed":
            self._say("Couldn't reach JARVIS: is it running?", "gate", now)
        elif kind == "barge":
            self._say("Stopped." if e.get("how") == "stop" else "Go on…", "status", now)

    def _say(self, text: str, kind: str, now: float):
        # a long reply keeps only its end, like subtitles
        self.caption = text if len(text) <= 220 else "…" + text[-219:].lstrip()
        self.caption_kind, self.caption_at = kind, now
        if text:
            self.active_at = now

    def shown_state(self) -> str:
        return "asking" if self.ask and self.state != "offline" else self.state

    def tick(self, dt: float, now: float | None = None) -> dict:
        """Advance the animation by dt seconds; returns what to draw."""
        now = time.time() if now is None else now
        rgb, speed = STATES[self.shown_state()]
        self.rgb = mix(self.rgb, rgb, dt * 4)
        want = 0.0 if self.state == "offline" else max(self.voice, self.mic * .85)
        self.level += (want - self.level) * min(1.0, dt * (18 if want > self.level else 6))
        self.spin = (self.spin + dt * speed * 1.6) % (2 * math.pi)
        busy = self.state in ACTIVE or bool(self.ask)
        if busy:
            self.active_at = now
        quiet = not busy and now - self.active_at > IDLE_AFTER
        # an open question stays on screen whatever else happens (it's read out, then "Listening…" would hide it)
        caption = self.ask[1] if self.ask else self.caption if (busy or now - self.caption_at < CAPTION_HOLD) else ""
        return {"rgb": self.rgb, "level": self.level, "spin": self.spin, "caption": caption,
                "caption_kind": "ask" if self.ask else self.caption_kind, "ask": self.ask,
                "dim": quiet and self.idle == "dim", "hidden": quiet and self.idle == "hide" and not caption,
                "offline": self.state == "offline", "pov": self.pov}


def read_events(url: str, out: queue.Queue, stop: threading.Event):
    """The HUD's Server-Sent Events, forever, as dicts on `out`; {"kind": "_offline"} whenever it's unreachable."""
    while not stop.is_set():
        try:
            with urllib.request.urlopen(url + "/events", timeout=30) as r:
                for raw in r:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data: "):
                        out.put(json.loads(line[6:]))
        except (OSError, ValueError):
            pass
        out.put({"kind": "_offline"})
        stop.wait(2)


def get_camera_frame(url: str, width: int) -> bytes | None:
    """The latest camera frame JARVIS drew its tracking on, as PNG; None while there isn't one."""
    req = urllib.request.Request(f"{url}/api/camera.png?w={width}", headers={"X-Jarvis": "1"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.read() if r.status == 200 else None
    except OSError:
        return None


def post_answer(url: str, qid: int, yes: bool) -> bool:
    return _post(url + "/api/answer", {"id": qid, "yes": yes})


def post_typed(url: str, text: str) -> bool:
    """A request typed into the chat bar; JARVIS takes it as if it had been said."""
    return _post(url + "/api/type", {"text": text})


def _post(url: str, body: dict) -> bool:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Jarvis": "1"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


class OrbWindow:
    def __init__(self, url: str, size: int, captions: bool, idle: str, place_file: Path):
        import tkinter as tk
        if sys.platform == "win32":
            try:  # sharp on high-DPI screens instead of a blurry upscale
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        self.tk, self.url, self.captions, self.place_file = tk, url, captions, place_file
        self.model = OrbModel(idle)
        self.events: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.root = tk.Tk()
        self.root.title("JARVIS orb")
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.size = round(size * self.scale)
        self.cap_w = round(300 * self.scale) if captions else 0
        # taller than the orb when captions are on, so a few lines of caption fit beside it
        self.w, self.h = self.cap_w + self.size, max(self.size, round(190 * self.scale) if captions else 0)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY)
        if sys.platform == "win32":
            self.root.attributes("-transparentcolor", KEY)
        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h, bg=KEY, highlightthickness=0)
        self.canvas.pack()
        x, y = self._load_place()
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Open HUD", command=lambda: webbrowser.open(self.url))
        self.menu.add_command(label="Hide captions" if captions else "Show captions", command=self._toggle_captions)
        self.menu.add_separator()
        self.menu.add_command(label="Close chat bar", command=self._close_chat)
        self.menu.add_command(label="Close orb", command=self.close)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", lambda e: self._open_chat())
        self.buttons: dict[str, tuple] = {}  # "yes"/"no" -> clickable box (x0, y0, x1, y1)
        self._drag_from = None
        self._last = time.time()
        self.hidden = False
        self.chat = None  # the chat bar's window while it's open
        self.pov = None   # the camera view's window while it's open
        self.pov_frame: bytes | None = None  # newest frame fetched for it, waiting to be shown
        self.pov_frame_at = 0.0
        threading.Thread(target=read_events, args=(url, self.events, self.stop), daemon=True).start()

    # --- placement ---
    def _load_place(self) -> tuple[int, int]:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            p = json.loads(self.place_file.read_text())
            x, y = int(p["x"]), int(p["y"])
            if 0 <= x <= sw - 40 and 0 <= y <= sh - 40:  # still on screen (monitors change)
                return x, y
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return sw - self.w - round(24 * self.scale), sh - self.h - round(72 * self.scale)  # above the taskbar

    def _save_place(self):
        try:
            self.place_file.parent.mkdir(parents=True, exist_ok=True)
            self.place_file.write_text(json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}))
        except OSError:
            pass

    # --- mouse ---
    def _press(self, e):
        self._drag_from = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), False)

    def _drag(self, e):
        if not self._drag_from:
            return
        x0, y0, wx, wy, moved = self._drag_from
        if moved or abs(e.x_root - x0) + abs(e.y_root - y0) > 4:
            self._drag_from = (x0, y0, wx, wy, True)
            self.root.geometry(f"+{wx + e.x_root - x0}+{wy + e.y_root - y0}")
            self._close_chat()  # it was placed against the old spot
            self._place_pov()

    def _release(self, e):
        moved = self._drag_from and self._drag_from[4]
        self._drag_from = None
        if moved:
            self._save_place()
            return
        for which, (x0, y0, x1, y1) in self.buttons.items():
            if x0 <= e.x <= x1 and y0 <= e.y <= y1 and self.model.ask:
                post_answer(self.url, self.model.ask[0], which == "yes")
                return
        if e.x >= self.cap_w:  # on the orb itself
            webbrowser.open(self.url)

    # --- chat bar ---
    def _open_chat(self):
        if self.chat is not None:
            self.entry.focus_force()
            return
        tk, s = self.tk, self.scale
        bar_h, gap = round(34 * s), round(6 * s)
        self.chat = tk.Toplevel(self.root)
        self.chat.overrideredirect(True)
        self.chat.attributes("-topmost", True)
        self.chat.configure(bg=hexrgb(BG), highlightthickness=1, highlightbackground=hexrgb(STATES["idle"][0]))
        font = ("Segoe UI", max(8, round(10 * s)))
        self.entry = tk.Entry(self.chat, bg=hexrgb(BG), fg="#e9fbff", insertbackground="#e9fbff", relief="flat",
                              font=font, highlightthickness=0)
        self.entry.pack(side="left", fill="both", expand=True, padx=(round(10 * s), 0), pady=round(4 * s))
        more = tk.Label(self.chat, text="⋯", bg=hexrgb(BG), fg=hexrgb(STATES["idle"][0]), font=font,
                        cursor="hand2", padx=round(8 * s))
        more.pack(side="right", fill="y")
        more.bind("<Button-1>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.entry.bind("<Return>", self._send_chat)
        self.entry.bind("<Escape>", lambda e: self._close_chat())
        # right above the orb's window, or below it when that's off the top of the screen
        x, y = self.root.winfo_x(), self.root.winfo_y()
        y = y - bar_h - gap if y - bar_h - gap >= 0 else y + self.h + gap
        self.chat.geometry(f"{self.w}x{bar_h}+{x}+{y}")
        self.chat.after(10, self.entry.focus_force)  # an overrideredirect window only takes keys when forced

    def _close_chat(self):
        if self.chat is not None:
            self.chat.destroy()
            self.chat = None

    def _send_chat(self, _e=None):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")  # stays open: the reply shows beside the orb, and you can type the next one

        def send():
            if not post_typed(self.url, text):
                self.events.put({"kind": "_typed_failed"})
        threading.Thread(target=send, daemon=True).start()

    # --- the camera view ---
    def _open_pov(self):
        tk, s = self.tk, self.scale
        self.pov_w = round(POV_WIDTH * s)
        self.pov_h = round(self.pov_w * 9 / 16)
        head = round(22 * s)
        cyan = hexrgb(STATES["idle"][0])
        self.pov = tk.Toplevel(self.root)
        self.pov.overrideredirect(True)
        self.pov.attributes("-topmost", True)
        self.pov.configure(bg=hexrgb(BG), highlightthickness=1, highlightbackground=cyan)
        bar = tk.Frame(self.pov, bg=hexrgb(BG), height=head)
        bar.pack(fill="x")
        font = ("Consolas", max(7, round(8 * s)), "bold")
        title = tk.Label(bar, text="◉ WHAT I SEE", bg=hexrgb(BG), fg=cyan, font=font, padx=round(6 * s))
        title.pack(side="left")
        close = tk.Label(bar, text="✕", bg=hexrgb(BG), fg=cyan, font=font, cursor="hand2", padx=round(6 * s))
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self._close_pov(by_user=True))
        for w in (bar, title):  # drag it by its title bar
            w.bind("<ButtonPress-1>", self._pov_press)
            w.bind("<B1-Motion>", self._pov_drag)
        screen = tk.Frame(self.pov, bg="#000000", width=self.pov_w, height=self.pov_h)
        screen.pack_propagate(False)  # pixels, whatever the label holds (a text label counts in characters)
        screen.pack()
        self.pov_label = tk.Label(screen, bg="#000000", fg="#e9fbff", text="Camera starting…",
                                  font=("Segoe UI", max(7, round(9 * s))))
        self.pov_label.pack(fill="both", expand=True)
        self.pov_size = (self.pov_w + 2, self.pov_h + head + 2)
        self.pov_moved = False
        self._place_pov()
        self.pov_image = None
        self.pov_frame, self.pov_frame_at = None, time.time()
        stop = self.pov_stop = threading.Event()
        threading.Thread(target=self._fetch_pov, args=(stop,), daemon=True).start()

    def _place_pov(self):
        """Just above the orb (and the chat bar, if it's open), right-aligned with it; below if there's no room."""
        if self.pov is None or self.pov_moved:
            return
        w, h = self.pov_size
        gap = round(6 * self.scale)
        x, y = self.root.winfo_x() + self.w - w, self.root.winfo_y()
        above = y - gap - h - (self.chat.winfo_height() + gap if self.chat is not None else 0)
        y = above if above >= 0 else y + self.h + gap
        self.pov.geometry(f"{w}x{h}+{max(0, x)}+{y}")

    def _pov_press(self, e):
        self._pov_from = (e.x_root, e.y_root, self.pov.winfo_x(), self.pov.winfo_y())

    def _pov_drag(self, e):
        x0, y0, wx, wy = self._pov_from
        self.pov_moved = True  # stays where you put it
        self.pov.geometry(f"+{wx + e.x_root - x0}+{wy + e.y_root - y0}")

    def _close_pov(self, by_user: bool = False):
        if by_user:
            self.model.pov = False  # JARVIS notices nobody's asking for frames and stops drawing them
        if self.pov is not None:
            self.pov_stop.set()
            self.pov.destroy()
            self.pov = self.pov_image = None

    def _fetch_pov(self, stop: threading.Event):
        while not stop.is_set():
            t = time.time()
            frame = get_camera_frame(self.url, self.pov_w)
            if frame:
                self.pov_frame, self.pov_frame_at = frame, time.time()
            stop.wait(max(0.0, 1 / POV_FPS - (time.time() - t)))

    def _show_pov_frame(self, now: float):
        frame, self.pov_frame = self.pov_frame, None
        if frame:
            try:
                self.pov_image = self.tk.PhotoImage(data=base64.b64encode(frame))
                self.pov_label.configure(image=self.pov_image, text="")
            except self.tk.TclError:
                pass
        elif self.pov_image is not None and now - self.pov_frame_at > 3:  # the camera went away
            self.pov_image = None
            self.pov_label.configure(image="", text="No picture: is the camera in use?")

    def _toggle_captions(self):
        self.captions = not self.captions
        self.menu.entryconfigure(1, label="Hide captions" if self.captions else "Show captions")

    # --- loop ---
    def run(self):
        self.root.after(0, self._frame)
        try:
            self.root.mainloop()
        finally:
            self.stop.set()

    def close(self):
        self.stop.set()
        self.root.destroy()

    def _frame(self):
        now = time.time()
        while True:
            try:
                self.model.feed(self.events.get_nowait(), now)
            except queue.Empty:
                break
        view = self.model.tick(min(.1, now - self._last), now)
        self._last = now
        if self.chat is not None or self.pov is not None:  # never hide the orb from under its windows
            view["hidden"] = False
        if view["pov"] and self.pov is None:
            self._open_pov()
        elif not view["pov"] and self.pov is not None:
            self._close_pov()
        if self.pov is not None:
            self._show_pov_frame(now)
        if view["hidden"] != self.hidden:
            self.hidden = view["hidden"]
            self.root.withdraw() if self.hidden else self.root.deiconify()
        if not self.hidden:
            self._draw(view, now)
        self.root.after(33, self._frame)

    def _draw(self, v: dict, now: float):
        c, s = self.canvas, self.scale
        c.delete("all")
        self.buttons = {}
        rgb = mix(BG, v["rgb"], .45) if v["dim"] else v["rgb"]
        cx, cy = self.cap_w + self.size / 2, self.h - self.size / 2
        R = self.size * .40 * (1 + v["level"] * .12)
        glow = 1 + v["level"] * .8
        # soft halo: rings fading into the dark body
        for i, t in enumerate((.10, .18, .28)):
            r = R * (1.18 - i * .07)
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=hexrgb(mix(BG, rgb, t * glow)), width=4 * s)
        c.create_oval(cx - R, cy - R, cx + R, cy + R, fill=hexrgb(BG), outline=hexrgb(mix(BG, rgb, .55)), width=2 * s)
        # spinning arcs
        deg = math.degrees(v["spin"])
        for k, (frac, ext, width) in enumerate(((.86, 70, 3), (.86, 40, 3), (.72, 110, 2), (.60, 50, 2))):
            r = R * frac
            start = (deg * (1 if k % 2 == 0 else -1.4) + k * 97) % 360
            c.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=ext, style="arc",
                         outline=hexrgb(mix(BG, rgb, .9)), width=width * s)
        # the core: brightens and grows with the voice
        core = R * (.30 + v["level"] * .22)
        c.create_oval(cx - core * 1.6, cy - core * 1.6, cx + core * 1.6, cy + core * 1.6,
                      fill=hexrgb(mix(BG, rgb, .22 + v["level"] * .3)), outline="")
        c.create_oval(cx - core, cy - core, cx + core, cy + core, fill=hexrgb(mix(BG, rgb, .7 + v["level"] * .3)),
                      outline="")
        if v["offline"]:
            c.create_text(cx, cy + R * .62, text="OFFLINE", fill=hexrgb(mix(BG, rgb, .8)),
                          font=("Consolas", max(6, round(7 * s)), "bold"))
        if self.captions and v["caption"]:
            self._bubble(v, rgb)

    def _bubble(self, v: dict, rgb):
        c, s = self.canvas, self.scale
        pad, x1 = 12 * s, self.cap_w - 6 * s
        width = self.cap_w - 30 * s
        font = ("Segoe UI", max(7, round(10 * s)))
        text_id = c.create_text(0, 0, text=v["caption"], width=width, font=font, anchor="nw")
        x0b, y0b, x1b, y1b = c.bbox(text_id)
        tw, th = x1b - x0b, y1b - y0b
        asking = v["caption_kind"] == "ask" and v["ask"]
        bh = th + 2 * pad + (30 * s if asking else 0)
        bw = max(tw + 2 * pad, 150 * s if asking else 0)
        bx0 = x1 - bw
        by1 = min(self.h - 2 * s, self.h - self.size / 2 + bh / 2)  # centred on the orb, kept inside the window
        by0 = max(2 * s, by1 - bh)
        edge = {"ask": (255, 181, 71), "gate": (255, 77, 94), "heard": (143, 199, 255)}.get(v["caption_kind"], rgb)
        r = 10 * s
        pts = [bx0 + r, by0, x1 - r, by0, x1, by0, x1, by0 + r, x1, by1 - r, x1, by1, x1 - r, by1,
               bx0 + r, by1, bx0, by1, bx0, by1 - r, bx0, by0 + r, bx0, by0]
        bubble = c.create_polygon(pts, smooth=True, fill=hexrgb(BG), outline=hexrgb(mix(BG, edge, .8)), width=1.5 * s)
        c.tag_lower(bubble, text_id)
        c.coords(text_id, bx0 + pad, by0 + pad)
        c.itemconfigure(text_id, fill="#fff3dc" if asking else "#e9fbff")
        if asking:
            bw2, by = 58 * s, by1 - pad - 20 * s
            for which, label, col, x in (("yes", "✓ YES", (93, 255, 176), bx0 + pad),
                                         ("no", "✗ NO", (255, 77, 94), bx0 + pad + bw2 + 10 * s)):
                box = (x, by, x + bw2, by + 22 * s)
                c.create_rectangle(*box, outline=hexrgb(col), fill=hexrgb(mix(BG, col, .12)), width=1.5 * s)
                c.create_text((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, text=label, fill=hexrgb(col),
                              font=("Consolas", max(7, round(9 * s)), "bold"))
                self.buttons[which] = box


def main():
    import config
    url = sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{config.HUD_PORT}"
    OrbWindow(url.rstrip("/"), config.ORB_SIZE, config.ORB_CAPTIONS, config.ORB_IDLE,
              Path(__file__).parent / "data" / "orb.json").run()


if __name__ == "__main__":
    main()
