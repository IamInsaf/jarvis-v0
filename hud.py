"""HUD: a live heads-up display of what JARVIS is doing, served by JARVIS itself.
Open http://127.0.0.1:8766 while `python jarvis.py` runs (F11 on a second monitor looks the part).

The rest of JARVIS reports into it with one-liners — hud.state("thinking"), hud.node("tools", "add_task"),
hud.level("mic", 0.4) — which only append to an in-memory buffer, so they cost nothing with no page open.
The page gets them live over Server-Sent Events and reads tasks, timers and stats straight from SQLite.
Only listens on this machine (127.0.0.1), stdlib only, and the page loads nothing from the internet."""
import json
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import config

SESSION_GAP = 30 * 60  # same as memory_viewer.py: a silence longer than this starts a new conversation
LEVEL_HOLD = 0.25      # a level not updated for this long has gone quiet

_cond = threading.Condition()
_events: deque = deque(maxlen=200)
_next_id = 1
_state = "idle"
_levels = {"mic": (0.0, 0.0), "voice": (0.0, 0.0)}  # source -> (0..1, when)
_info = {"started": time.time(), "mode": None, "camera": None, "first_audio": None}
_asks: dict[int, dict] = {}  # yes/no questions JARVIS is waiting on: id -> {"question", "answer": None/True/False}
_next_ask = 1
_typed: deque = deque(maxlen=20)  # requests typed into the orb's chat bar, waiting for the voice loop
TYPED_MAX = 1000                  # characters in one typed request
_camera = None                    # eyes.view_png while the camera is in use: width -> PNG bytes or None
_server = None


# --- reporting: called from the rest of JARVIS, from any thread ---

def emit(kind: str, **data):
    global _next_id
    with _cond:
        _events.append({**data, "id": _next_id, "t": time.time(), "kind": kind})  # data can never overwrite these
        _next_id += 1
        _cond.notify_all()


def state(name: str):
    """idle / listening / thinking / speaking / dictating / error"""
    global _state
    if name != _state:
        _state = name
        emit("state", state=name)


def current() -> str:
    return _state


def ask(question: str) -> int:
    """JARVIS asked a yes/no question out loud: the page and the orb show it with YES / NO buttons too.
    Returns its id for pending_answer() and close_ask()."""
    global _next_ask
    with _cond:
        qid, _next_ask = _next_ask, _next_ask + 1
        _asks[qid] = {"question": question, "answer": None}
    emit("ask", ask=qid, question=question)  # not id=: that is the event's own number
    return qid


def answer(qid: int, yes: bool) -> bool:
    """A click on YES or NO. False if that question isn't open (already answered, or never asked)."""
    with _cond:
        q = _asks.get(qid)
        if q is None or q["answer"] is not None:
            return False
        q["answer"] = bool(yes)
        _cond.notify_all()
    return True


def pending_answer(qid: int) -> bool | None:
    """The clicked answer to an open question, or None if nobody has clicked."""
    with _cond:
        q = _asks.get(qid)
        return None if q is None else q["answer"]


def close_ask(qid: int, how: str, yes: bool | None):
    """The question is settled ("voice", "click" or "timeout"): the buttons go away."""
    with _cond:
        _asks.pop(qid, None)
    emit("answered", ask=qid, how=how, yes=yes)


def type_request(text: str) -> bool:
    """A request typed into the orb's chat bar: the voice loop takes it like something said after
    "hey Jarvis". False if it's empty."""
    text = " ".join(text.split())[:TYPED_MAX]
    if not text:
        return False
    with _cond:
        _typed.append(text)
        _cond.notify_all()
    emit("typed", text=text)
    return True


def has_typed() -> bool:
    return bool(_typed)


def take_typed() -> str | None:
    """The oldest typed request not handled yet, or None."""
    with _cond:
        return _typed.popleft() if _typed else None


def set_camera(view_png):
    """Where /api/camera.png gets its frames: eyes.view_png, or None for no camera."""
    global _camera
    _camera = view_png


def pov(on: bool):
    """Open (or close) the small view of what the camera sees, beside the orb."""
    emit("pov", on=on)


def node(name: str, label: str = ""):
    """A request passing through one part of JARVIS: ears, voice, rules, router, cat:<category>,
    memory, tools, llm, mouth. The page draws a pulse from the previous part to this one."""
    emit("node", node=name, label=label)


def level(source: str, value: float):
    """Loudness 0..1 of the mic ("mic") or JARVIS's own voice ("voice"); sampled by the page, not queued."""
    _levels[source] = (max(0.0, min(1.0, value)), time.time())


def info(**kw):
    """Slow-changing facts the System panel shows: mode, camera, first_audio (seconds from the end of
    the request to JARVIS's first word, last turn)."""
    _info.update(kw)
    emit("info", **kw)


def since(last_id: int, timeout: float) -> list[dict]:
    """Events after last_id, waiting up to `timeout` seconds for one if there are none yet."""
    with _cond:
        if not _events or _events[-1]["id"] <= last_id:
            _cond.wait(timeout)
        return [e for e in _events if e["id"] > last_id]


def _levels_now() -> dict:
    now = time.time()
    return {k: round(v, 3) if now - t < LEVEL_HOLD else 0.0 for k, (v, t) in _levels.items()}


# --- what the panels show ---

def _rows(db, sql: str, args=()) -> list:
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:  # a table JARVIS hasn't created yet on a fresh database
        return []


def snapshot(db_path: str | None = None) -> dict:
    db_path = db_path or config.DB_PATH
    now = time.time()
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    data = {"now": now, "facts": 0, "requests": 0, "today": 0, "sessions": 0, "routes": [],
            "todos": [], "done": 0, "done_today": 0, "schedule": [], "transcript": []}
    if os.path.exists(db_path):
        with closing(sqlite3.connect(db_path, timeout=2)) as db:
            data["facts"] = (_rows(db, "SELECT COUNT(*) FROM facts") or [(0,)])[0][0]
            user_ts = [r[0] for r in _rows(db, "SELECT ts FROM log WHERE role = 'user' ORDER BY ts")]
            data["requests"] = len(user_ts)
            data["today"] = sum(ts >= midnight for ts in user_ts)
            all_ts = [r[0] for r in _rows(db, "SELECT ts FROM log ORDER BY ts")]
            data["sessions"] = sum(1 for i, ts in enumerate(all_ts) if i == 0 or ts - all_ts[i - 1] > SESSION_GAP)
            data["routes"] = [{"category": c, "n": n} for c, n in _rows(
                db, "SELECT category, COUNT(*) AS n FROM routes GROUP BY category ORDER BY n DESC")]
            data["todos"] = [{"id": i, "text": t, "created": c} for i, t, c in _rows(
                db, "SELECT id, text, created FROM todos WHERE done = 0 ORDER BY id")]
            data["done"] = (_rows(db, "SELECT COUNT(*) FROM todos WHERE done = 1") or [(0,)])[0][0]
            data["done_today"] = (_rows(db, "SELECT COUNT(*) FROM todos WHERE done = 1 AND done_at >= ?",
                                        (midnight,)) or [(0,)])[0][0]
            data["schedule"] = [{"kind": k, "label": l, "due": d, "created": c} for k, l, d, c in _rows(
                db, "SELECT kind, label, due, created FROM schedule WHERE done = 0 AND due > ? ORDER BY due", (now,))]
            data["transcript"] = [{"role": r, "text": t, "ts": ts} for r, t, ts in _rows(
                db, "SELECT role, content, ts FROM log ORDER BY id DESC LIMIT 14")][::-1]
    import brains  # here: it imports this module
    data["system"] = {"model": config.OLLAMA_MODEL if brains.awake else "off", "think": config.OLLAMA_THINK,
                      "ollama": _ollama_up(), "cpu": _cpu(), "ram": _ram(), "state": _state, **_info}
    data["categories"] = _categories()
    with _cond:
        data["asks"] = [{"id": i, "question": q["question"]} for i, q in _asks.items() if q["answer"] is None]
    return data


def _categories() -> list[str]:
    try:
        import intents  # imported here: pulls in the Ollama client, which tests of the bus don't need
        return [*intents.CATEGORIES, "abilities"]  # learned plugins (use_<name>) all draw as "abilities"
    except Exception:
        return []


_ollama_cache = (0.0, None)


def _ollama_up() -> bool | None:
    global _ollama_cache
    if time.time() - _ollama_cache[0] < 10:
        return _ollama_cache[1]
    import requests
    host = os.getenv("OLLAMA_HOST", "127.0.0.1:11434").replace("0.0.0.0", "127.0.0.1")
    if "://" not in host:
        host = "http://" + host
    if host.count(":") < 2:
        host += ":11434"
    try:
        up = requests.get(host.rstrip("/") + "/api/version", timeout=1).ok
    except requests.RequestException:
        up = False
    _ollama_cache = (time.time(), up)
    return up


_cpu_prev = None


def _cpu() -> float | None:
    """Whole-machine CPU use 0..1 since the last call (Windows; None elsewhere)."""
    global _cpu_prev
    if sys.platform != "win32":
        return None
    import ctypes
    idle, kernel, user = (ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong())
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    now = (idle.value, kernel.value + user.value)  # kernel time includes idle time
    prev, _cpu_prev = _cpu_prev, now
    if prev is None or now[1] == prev[1]:
        return None
    return round(1 - (now[0] - prev[0]) / (now[1] - prev[1]), 3)


def _ram() -> float | None:
    """Memory in use 0..1 (Windows; None elsewhere)."""
    if sys.platform != "win32":
        return None
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    *((n, ctypes.c_ulonglong) for n in ("total", "avail", "pagefile", "availpage", "virtual",
                                                        "availvirtual", "availext"))]

    m = MemoryStatus(dwLength=ctypes.sizeof(MemoryStatus))
    return m.dwMemoryLoad / 100 if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) else None


# --- the server ---

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype if ctype.startswith("image/") else f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local(self) -> bool:
        # transcripts are private: a web page that points its own domain at 127.0.0.1 (DNS rebinding)
        # mustn't be able to read them, so only requests addressed to this machine by name are served
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_POST(self):
        # an answer approves real actions and a typed request can start them, so besides the Host check
        # these need a header that a form or a simple cross-site request can't send: no other web page
        # can click YES or give orders for you
        if not self._local() or self.headers.get("X-Jarvis") != "1":
            return self._send(403, '{"error": "local only"}')
        if self.path == "/api/type":
            return self._post_typed()
        if self.path != "/api/answer":
            return self._send(404, '{"error": "not found"}')
        try:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(size, 1024)) or b"{}")
            qid, yes = int(body["id"]), body["yes"]
        except (ValueError, KeyError, TypeError):
            return self._send(400, '{"error": "expected {\\"id\\": n, \\"yes\\": true|false}"}')
        if not isinstance(yes, bool):
            return self._send(400, '{"error": "yes must be true or false"}')
        ok = answer(qid, yes)
        self._send(200 if ok else 409, json.dumps({"ok": ok}))

    def _post_typed(self):
        try:
            size = int(self.headers.get("Content-Length") or 0)
            text = json.loads(self.rfile.read(min(size, 4 * TYPED_MAX)) or b"{}")["text"]
        except (ValueError, KeyError, TypeError):
            return self._send(400, '{"error": "expected {\\"text\\": \\"...\\"}"}')
        if not isinstance(text, str) or not type_request(text):
            return self._send(400, '{"error": "text must be a non-empty string"}')
        self._send(200, '{"ok": true}')

    def do_GET(self):
        if not self._local():
            return self._send(403, '{"error": "local only"}')
        url = urlsplit(self.path)
        if url.path == "/api/camera.png":
            return self._camera_frame(parse_qs(url.query))
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/api/snapshot":
            self._send(200, json.dumps(snapshot()))
        elif self.path == "/events":
            self._stream()
        else:
            self._send(404, '{"error": "not found"}')

    def _camera_frame(self, query: dict):
        # the live camera: like an answer, only for a client that can send the header (the orb), so another
        # web page can't even put it in an <img> on your screen
        if self.headers.get("X-Jarvis") != "1":
            return self._send(403, '{"error": "local only"}')
        try:
            width = int(query.get("w", ["400"])[0])
        except ValueError:
            return self._send(400, '{"error": "w must be a number"}')
        png = _camera(width) if _camera else None
        if png is None:  # no camera, or it's still starting
            return self._send(204, b"")
        self._send(200, png, "image/png")

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = _events[-1]["id"] if _events else 0
        sent_levels, quiet = None, time.time()
        try:
            self._event({"kind": "state", "state": _state})
            while True:
                for e in since(last, 0.06):
                    self._event(e)
                    last, quiet = e["id"], time.time()
                if (lv := _levels_now()) != sent_levels:
                    self._event({"kind": "level", **lv})
                    sent_levels, quiet = lv, time.time()
                if time.time() - quiet > 15:  # keeps proxies and the browser from timing out an idle stream
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    quiet = time.time()
        except OSError:  # the tab was closed
            pass

    def _event(self, e: dict):
        self.wfile.write(f"data: {json.dumps(e)}\n\n".encode())
        self.wfile.flush()

    def log_message(self, *args):
        pass  # keep the console quiet


def start(port: int | None = None) -> str | None:
    """Serve the HUD on a background thread; returns its address, or None if the port is taken."""
    global _server
    if _server is None:
        try:
            _server = ThreadingHTTPServer(("127.0.0.1", config.HUD_PORT if port is None else port), Handler)
        except OSError as e:
            print(f"  [hud] couldn't start: {e}")
            return None
        _server.daemon_threads = True
        threading.Thread(target=_server.serve_forever, daemon=True, name="hud").start()
    return f"http://127.0.0.1:{_server.server_address[1]}"


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS HUD</title>
<style>
:root { --bg:#01060b; --cyan:#2ad4ff; --line:rgba(42,212,255,.22); --line2:rgba(42,212,255,.45);
        --text:#cfefff; --muted:#6f93a6; --amber:#ffb547; --red:#ff4d5e; --green:#5dffb0;
        --mono:Consolas, "Cascadia Mono", ui-monospace, monospace;
        --sans:Bahnschrift, "Segoe UI", system-ui, sans-serif; color-scheme:dark; }
* { box-sizing:border-box; }
html, body { margin:0; height:100%; }
body { background:radial-gradient(ellipse at 50% 42%, #072a42 0%, #03101b 50%, var(--bg) 100%);
       color:var(--text); font:14px/1.45 var(--sans); overflow:hidden; }
#field { position:fixed; inset:0; width:100%; height:100%; z-index:0; }
.scan { position:fixed; inset:0; pointer-events:none; z-index:1;
        background:repeating-linear-gradient(0deg, rgba(160,230,255,.025) 0 1px, transparent 1px 3px); }
main { position:relative; z-index:2; height:100vh; padding:14px 16px; display:grid; gap:14px;
       grid-template-columns:minmax(250px, 330px) minmax(0, 1fr) minmax(250px, 330px);
       grid-template-rows:auto minmax(0, 1fr) minmax(0, 1fr); }
header { grid-column:1 / -1; display:flex; align-items:center; justify-content:space-between; gap:16px;
         border-bottom:1px solid var(--line); padding-bottom:10px; }
.brand { display:flex; align-items:baseline; gap:14px; }
.brand b { font:600 22px/1 var(--sans); letter-spacing:.42em; color:var(--cyan);
           text-shadow:0 0 14px rgba(42,212,255,.6); }
.brand span, .clock small { font:11px/1 var(--mono); letter-spacing:.2em; color:var(--muted); }
.clock { text-align:right; font:600 20px/1 var(--mono); letter-spacing:.12em; }
.clock small { display:block; margin-top:4px; }
.panel { position:relative; min-height:0; display:flex; flex-direction:column; padding:12px 14px;
         background:linear-gradient(180deg, rgba(10,40,60,.55), rgba(3,16,26,.4));
         border:1px solid var(--line);
         clip-path:polygon(14px 0, 100% 0, 100% calc(100% - 14px), calc(100% - 14px) 100%, 0 100%, 0 14px); }
.panel::before, .panel::after { content:""; position:absolute; width:22px; height:22px; pointer-events:none; }
.panel::before { top:0; right:0; border-top:2px solid var(--cyan); border-right:2px solid var(--cyan); }
.panel::after { bottom:0; left:0; border-bottom:2px solid var(--cyan); border-left:2px solid var(--cyan); }
.panel h2 { margin:0 0 10px; display:flex; justify-content:space-between; gap:8px;
            font:600 11px/1 var(--mono); letter-spacing:.28em; color:var(--cyan); }
.panel h2 span { color:var(--muted); letter-spacing:.12em; }
.scroll { overflow:auto; min-height:0; flex:1; scrollbar-width:thin; scrollbar-color:var(--line2) transparent; }
#sys { grid-column:1; grid-row:2; } #tasks { grid-column:1; grid-row:3; }
#stats { grid-column:3; grid-row:2; } #talk { grid-column:3; grid-row:3; }
#core { grid-column:2; grid-row:2 / 4; display:flex; flex-direction:column; min-height:0; }
#orbwrap { position:relative; flex:1 1 auto; min-height:200px; }
#orb { position:absolute; inset:0; width:100%; height:100%; }
#status { text-align:center; margin-top:-6px; }
#status b { display:block; font:600 18px/1 var(--mono); letter-spacing:.55em; padding-left:.55em; color:var(--cyan);
            text-shadow:0 0 12px currentColor; transition:color .4s; }
#status small { display:block; margin-top:6px; min-height:1.4em; font:12px/1.4 var(--mono); color:var(--muted);
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
#pipe { flex:0 0 auto; height:min(33vh, 300px); margin-top:10px; }
#pipe svg { width:100%; height:100%; display:block; }
.row { display:flex; justify-content:space-between; gap:10px; padding:5px 0; border-bottom:1px dashed rgba(42,212,255,.1);
       font:12px/1.3 var(--mono); }
.row > span:first-child { color:var(--muted); letter-spacing:.14em; }
.row > span:last-child { text-align:right; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:6px; vertical-align:1px;
       background:var(--muted); }
.dot.on { background:var(--green); box-shadow:0 0 8px var(--green); }
.dot.off { background:var(--red); box-shadow:0 0 8px var(--red); }
.meter { display:grid; grid-template-columns:52px 1fr 40px; gap:8px; align-items:center; padding:5px 0;
         font:12px/1 var(--mono); }
.meter > span:first-child { color:var(--muted); letter-spacing:.14em; }
.meter > span:last-child { text-align:right; }
.bar { height:6px; background:rgba(42,212,255,.1); position:relative; overflow:hidden; }
.bar i { position:absolute; inset:0 auto 0 0; width:0; background:linear-gradient(90deg, rgba(42,212,255,.5), var(--cyan));
         box-shadow:0 0 8px var(--cyan); transition:width .25s; }
.bar.fast i { transition:width .06s linear; }
.big { display:grid; grid-template-columns:repeat(4, 1fr); gap:6px; margin-bottom:12px; }
.big div { text-align:center; padding:8px 2px; border:1px solid var(--line); background:rgba(42,212,255,.04); }
.big b { display:block; font:600 22px/1.1 var(--mono); color:#e9fbff; text-shadow:0 0 10px rgba(42,212,255,.5); }
.big small { font:10px/1 var(--mono); letter-spacing:.16em; color:var(--muted); }
.cat { display:grid; grid-template-columns:78px 1fr 34px; gap:8px; align-items:center; padding:3px 0;
       font:11px/1.2 var(--mono); }
.cat > span:first-child { color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.cat > span:last-child { text-align:right; }
.sub { font:10px/1 var(--mono); letter-spacing:.24em; color:var(--muted); margin:12px 0 6px; }
.task { display:flex; gap:10px; padding:6px 0; border-bottom:1px dashed rgba(42,212,255,.1); }
.task b { font:600 11px/1.6 var(--mono); color:var(--cyan); min-width:18px; }
.task span { overflow-wrap:anywhere; }
.timer { padding:6px 0 8px; border-bottom:1px dashed rgba(42,212,255,.1); }
.timer div { display:flex; justify-content:space-between; gap:8px; font:12px/1.3 var(--mono); margin-bottom:5px; }
.timer div b { color:var(--amber); font-weight:600; }
.timer .bar i { background:linear-gradient(90deg, rgba(255,181,71,.4), var(--amber)); box-shadow:0 0 8px var(--amber); }
.empty { color:var(--muted); font:12px/1.4 var(--mono); padding:4px 0; }
.line { padding:6px 0; border-bottom:1px dashed rgba(42,212,255,.1); overflow-wrap:anywhere; }
.line small { display:block; font:10px/1.4 var(--mono); letter-spacing:.16em; color:var(--muted); }
.line.user small { color:#8fc7ff; }
.line.assistant small { color:var(--cyan); }
.line.assistant span { color:#e9fbff; }
.tag { font:10px/1.6 var(--mono); letter-spacing:.12em; color:var(--amber); padding:2px 0; }
.tag.bad { color:var(--red); } .tag.good { color:var(--green); } .tag.soft { color:var(--muted); }
.line.live span::after { content:"▍"; color:var(--cyan); animation:blink 1s steps(1) infinite; margin-left:2px; }
@keyframes blink { 50% { opacity:0; } }
#ask { margin:12px auto 0; max-width:520px; width:100%; padding:12px 14px; border:1px solid var(--amber);
       background:linear-gradient(180deg, rgba(60,40,8,.55), rgba(26,16,3,.45)); text-align:center;
       box-shadow:0 0 18px rgba(255,181,71,.25); animation:askglow 1.6s ease-in-out infinite; }
#ask[hidden] { display:none; }
#ask small { display:block; font:10px/1 var(--mono); letter-spacing:.28em; color:var(--amber); margin-bottom:8px; }
#ask p { margin:0 0 10px; font:13px/1.45 var(--mono); color:#fff3dc; overflow-wrap:anywhere; }
#ask button { font:600 12px/1 var(--mono); letter-spacing:.24em; padding:9px 22px; margin:0 6px; cursor:pointer;
              background:transparent; color:var(--text); border:1px solid var(--line2); }
#ask button.yes { border-color:var(--green); color:var(--green); }
#ask button.no { border-color:var(--red); color:var(--red); }
#ask button:hover, #ask button:focus-visible { background:rgba(255,255,255,.08); outline:none; }
#ask button:disabled { opacity:.4; cursor:default; }
@keyframes askglow { 50% { box-shadow:0 0 30px rgba(255,181,71,.45); } }
/* pipeline */
.edge { stroke:rgba(42,212,255,.16); stroke-width:1; fill:none; }
.n circle.core { fill:#03121d; stroke:var(--cyan); stroke-width:1.5; }
.n circle.halo { fill:var(--cyan); opacity:0; }
.n text { fill:var(--muted); font:600 10px var(--mono); letter-spacing:.18em; text-anchor:middle; }
.n text.lbl { fill:var(--amber); font-weight:400; letter-spacing:.04em; }
.n.cat text { text-anchor:start; font-size:9px; letter-spacing:.1em; }
.n.sel circle.core { fill:var(--amber); stroke:var(--amber); }
.n.sel text { fill:var(--amber); }
.n.hit circle.halo { animation:flash .9s ease-out; }
.n.hit circle.core { stroke:#fff; }
@keyframes flash { 0% { opacity:.75; r:6; } 100% { opacity:0; r:26; } }
.pulse { fill:#fff; filter:drop-shadow(0 0 6px var(--cyan)); }
.trail { stroke:var(--cyan); stroke-width:2; opacity:.0; }
@media (max-width: 900px) {
  body { overflow:auto; height:auto; }
  main { height:auto; grid-template-columns:minmax(0, 1fr); grid-template-rows:none; }
  header, #core, #sys, #tasks, #stats, #talk { grid-column:1; grid-row:auto; }
  #core { order:1; } #talk { order:2; } #tasks { order:3; } #stats { order:4; } #sys { order:5; }
  #orbwrap { flex:none; height:min(86vw, 420px); }
  #pipe { height:230px; }
  .panel { max-height:420px; }
  .brand span { display:none; }
}
@media (prefers-reduced-motion: reduce) { .n.hit circle.halo, #ask, .line.live span::after { animation:none; } }
</style></head>
<body>
<canvas id="field"></canvas><div class="scan"></div>
<main>
  <header>
    <div class="brand"><b>J.A.R.V.I.S</b><span>LOCAL ASSISTANT · v0</span></div>
    <div class="clock"><span id="clock">--:--:--</span><small id="date"></small></div>
  </header>

  <section class="panel" id="sys"><h2>SYSTEM <span id="uptime">--</span></h2><div class="scroll">
    <div class="row"><span>MODEL</span><span id="model">—</span></div>
    <div class="row"><span>ENGINE</span><span id="ollama"><i class="dot"></i>—</span></div>
    <div class="row"><span>THINKING</span><span id="think">—</span></div>
    <div class="row"><span>MODE</span><span id="mode">—</span></div>
    <div class="row"><span>CAMERA</span><span id="camera">—</span></div>
    <div class="row"><span>FIRST WORD</span><span id="firstaudio">—</span></div>
    <div class="meter"><span>CPU</span><div class="bar"><i id="cpu"></i></div><span id="cpuv">—</span></div>
    <div class="meter"><span>RAM</span><div class="bar"><i id="ram"></i></div><span id="ramv">—</span></div>
    <div class="meter"><span>MIC</span><div class="bar fast"><i id="mic"></i></div><span id="micv">0</span></div>
    <div class="meter"><span>VOICE</span><div class="bar fast"><i id="voice"></i></div><span id="voicev">0</span></div>
  </div></section>

  <section class="panel" id="tasks"><h2>TASKS &amp; TIMERS <span id="taskcount"></span></h2><div class="scroll">
    <div id="timers"></div>
    <div class="sub">TO-DO</div><div id="todos"></div>
  </div></section>

  <div id="core">
    <div id="orbwrap"><canvas id="orb"></canvas></div>
    <div id="status"><b id="statelbl">OFFLINE</b><small id="statesub">Connecting to JARVIS…</small></div>
    <div id="ask" hidden role="alertdialog" aria-labelledby="askq"><small>AWAITING YOUR APPROVAL</small>
      <p id="askq"></p><button class="yes" id="askyes">YES</button><button class="no" id="askno">NO</button></div>
    <div id="pipe"><svg viewBox="0 0 1000 300" preserveAspectRatio="xMidYMid meet" aria-label="Request pipeline">
      <g id="edges"></g><g id="trails"></g><g id="nodes"></g><g id="pulses"></g></svg></div>
  </div>

  <section class="panel" id="stats"><h2>ACTIVITY <span id="statsub"></span></h2><div class="scroll">
    <div class="big">
      <div><b id="requests">0</b><small>REQUESTS</small></div><div><b id="today">0</b><small>TODAY</small></div>
      <div><b id="facts">0</b><small>FACTS</small></div><div><b id="sessions">0</b><small>SESSIONS</small></div>
    </div>
    <div class="sub">ROUTED BY CATEGORY</div><div id="cats"></div>
  </div></section>

  <section class="panel" id="talk"><h2>TRANSCRIPT <span>LIVE</span></h2><div class="scroll" id="lines"></div></section>
</main>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
const STATES = {
  idle:      {label:"STANDBY",    rgb:[42,212,255],  speed:.45, fire:.03},
  listening: {label:"LISTENING",  rgb:[110,240,255], speed:1.1, fire:.12},
  thinking:  {label:"PROCESSING", rgb:[255,181,71],  speed:2.8, fire:.6},
  speaking:  {label:"SPEAKING",   rgb:[175,246,255], speed:1.5, fire:.2},
  dictating: {label:"DICTATING",  rgb:[93,255,176],  speed:1.1, fire:.1},
  error:     {label:"FAULT",      rgb:[255,77,94],   speed:.25, fire:.02},
  offline:   {label:"OFFLINE",    rgb:[95,120,135],  speed:.12, fire:0},
};
let state = "offline", rgb = [...STATES.offline.rgb], speed = .12;
let mic = 0, voice = 0, lv = 0, snap = null, clockSkew = 0;
const hist = new Float32Array(90);
const rgba = (a, c = rgb) => `rgba(${c[0]|0},${c[1]|0},${c[2]|0},${a})`;

function setState(s) {
  if (!STATES[s]) s = "idle";
  state = s;
  $("#statelbl").textContent = STATES[s].label;
  $("#statelbl").style.color = `rgb(${STATES[s].rgb})`;
  if (s === "offline") $("#statesub").textContent = "Connecting to JARVIS…";
}

function fit(c) {
  const r = c.getBoundingClientRect(), d = devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * d)), h = Math.max(1, Math.round(r.height * d));
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  const x = c.getContext("2d"); x.setTransform(d, 0, 0, d, 0, 0);
  return [x, r.width, r.height];
}

// --- background: a drifting neuron field that fires faster while JARVIS thinks ---
const field = $("#field"); let pts = [], sparks = [], fieldSize = "";
function drawField(dt) {
  const [x, w, h] = fit(field);
  if (fieldSize !== `${w}x${h}`) {
    fieldSize = `${w}x${h}`;
    const n = Math.min(170, Math.floor(w * h / 8500));
    pts = Array.from({length:n}, () => ({x:Math.random()*w, y:Math.random()*h,
                                         vx:(Math.random()-.5)*14, vy:(Math.random()-.5)*14}));
  }
  x.clearRect(0, 0, w, h);
  const k = still ? 0 : dt * (.5 + speed * .5);
  for (const p of pts) {
    p.x += p.vx * k; p.y += p.vy * k;
    if (p.x < 0 || p.x > w) p.vx *= -1;
    if (p.y < 0 || p.y > h) p.vy *= -1;
  }
  const D = 125;
  x.lineWidth = 1;
  for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
    const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y, d = dx*dx + dy*dy;
    if (d < D * D) {
      x.strokeStyle = rgba((1 - Math.sqrt(d) / D) * .14);
      x.beginPath(); x.moveTo(pts[i].x, pts[i].y); x.lineTo(pts[j].x, pts[j].y); x.stroke();
    }
  }
  x.fillStyle = rgba(.5);
  for (const p of pts) { x.beginPath(); x.arc(p.x, p.y, 1.3, 0, 7); x.fill(); }
  if (!still && pts.length && Math.random() < STATES[state].fire * dt * 30) {
    const a = pts[Math.random() * pts.length | 0];
    let best = null, bd = D * D;
    for (const b of pts) { const d = (a.x-b.x)**2 + (a.y-b.y)**2; if (b !== a && d < bd) { bd = d; best = b; } }
    if (best) sparks.push({a, b:best, t:0});
  }
  sparks = sparks.filter(s => (s.t += dt * 1.8) < 1);
  for (const s of sparks) {
    const px = s.a.x + (s.b.x - s.a.x) * s.t, py = s.a.y + (s.b.y - s.a.y) * s.t;
    x.strokeStyle = rgba(.55 * (1 - s.t)); x.lineWidth = 1.2;
    x.beginPath(); x.moveTo(s.a.x, s.a.y); x.lineTo(px, py); x.stroke();
    x.fillStyle = rgba(1); x.beginPath(); x.arc(px, py, 2, 0, 7); x.fill();
  }
}

// --- centre: the voice orb and its rings ---
const orb = $("#orb");
function ring(x, cx, cy, r, rot, dash, width, alpha, from = 0, to = Math.PI * 2) {
  x.save(); x.translate(cx, cy); x.rotate(rot);
  x.setLineDash(dash); x.lineWidth = width; x.strokeStyle = rgba(alpha);
  x.beginPath(); x.arc(0, 0, r, from, to); x.stroke(); x.restore();
}
function drawOrb(t) {
  const [x, w, h] = fit(orb);
  x.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) * .45, T = still ? 0 : t;
  let g = x.createRadialGradient(cx, cy, R * .1, cx, cy, R * 1.05);
  g.addColorStop(0, rgba(.22 + lv * .35)); g.addColorStop(.55, rgba(.06)); g.addColorStop(1, rgba(0));
  x.fillStyle = g; x.beginPath(); x.arc(cx, cy, R * 1.05, 0, 7); x.fill();

  const s = T * speed;
  ring(x, cx, cy, R * .98, s * .08, [2, 7], 1, .45);
  x.save(); x.translate(cx, cy); x.rotate(-s * .05);
  for (let i = 0; i < 120; i++) {
    const a = i / 120 * Math.PI * 2, long = i % 10 === 0;
    x.strokeStyle = rgba(long ? .7 : .25); x.lineWidth = long ? 1.6 : 1;
    x.beginPath(); x.moveTo(Math.cos(a) * R * .9, Math.sin(a) * R * .9);
    x.lineTo(Math.cos(a) * R * (long ? .84 : .87), Math.sin(a) * R * (long ? .84 : .87)); x.stroke();
  }
  x.restore();
  for (let k = 0; k < 3; k++) {
    const a0 = k * Math.PI * 2 / 3 - s * .35;
    ring(x, cx, cy, R * .79, a0, [], 3, .85, 0, Math.PI * .42);
    ring(x, cx, cy, R * .75, -a0 * .7, [], 1, .35, 0, Math.PI * .3);
  }
  ring(x, cx, cy, R * .7, 0, [], 1, .3);
  ring(x, cx, cy, R * .58, s * .5, [16, 10], 1.5, .5);

  // "audio analysis": the last few seconds of loudness, wrapped around the core
  x.save(); x.translate(cx, cy);
  for (let i = 0; i < hist.length; i++) {
    const a = i / hist.length * Math.PI * 2 - Math.PI / 2, v = hist[(i + histAt) % hist.length];
    const r0 = R * .46, r1 = r0 + R * (.015 + v * .2);
    x.strokeStyle = rgba(.35 + v * .6); x.lineWidth = 2;
    x.beginPath(); x.moveTo(Math.cos(a) * r0, Math.sin(a) * r0); x.lineTo(Math.cos(a) * r1, Math.sin(a) * r1); x.stroke();
  }
  x.restore();

  const rc = R * (.3 + lv * .07), wob = .025 + lv * .2;
  x.beginPath();
  for (let i = 0; i <= 96; i++) {
    const a = i / 96 * Math.PI * 2;
    const r = rc * (1 + wob * (Math.sin(3 * a + T * 1.3) * .5 + Math.sin(5 * a - T * 2.1) * .3 + Math.sin(9 * a + T * 3.7) * .2));
    i ? x.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r) : x.moveTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r);
  }
  g = x.createRadialGradient(cx, cy, 0, cx, cy, rc * 1.2);
  g.addColorStop(0, "rgba(255,255,255,.95)"); g.addColorStop(.35, rgba(.85)); g.addColorStop(1, rgba(.08));
  x.fillStyle = g; x.shadowColor = rgba(1); x.shadowBlur = 30 + lv * 40; x.fill(); x.shadowBlur = 0;
  x.strokeStyle = rgba(.9); x.lineWidth = 1.5; x.stroke();
  x.save(); x.translate(cx, cy); x.rotate(s * .2); x.strokeStyle = "rgba(255,255,255,.35)"; x.lineWidth = 1;
  x.beginPath();
  for (let k = 0; k <= 3; k++) { const a = k * Math.PI * 2 / 3 - Math.PI / 2; x.lineTo(Math.cos(a) * rc * .55, Math.sin(a) * rc * .55); }
  x.stroke(); x.restore();
}

// --- the real pipeline: one node per part of JARVIS a request passes through ---
const SVGNS = "http://www.w3.org/2000/svg";
const BASE = {
  ears:   [60, 150, "INPUT"],   voice: [185, 150, "VOICE ID"], rules: [320, 62, "RULES"],
  router: [320, 238, "ROUTER"], tools: [665, 92, "TOOLS"],     memory:[665, 228, "MEMORY"],
  llm:    [810, 150, "LLM"],    mouth: [940, 150, "OUTPUT"],
};
let nodes = {}, graphCats = "", lastNode = null, pulses = [];
function el(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  parent && parent.appendChild(e); return e;
}
function buildGraph(cats) {
  if (!cats.length) cats = ["chat"];
  if (cats.join() === graphCats) return;
  graphCats = cats.join();
  const E = $("#edges"), N = $("#nodes"); E.innerHTML = N.innerHTML = ""; nodes = {};
  const pos = {...BASE};
  const top = 18, step = Math.min(24, (282 - top) / Math.max(1, cats.length - 1));
  const y0 = 150 - step * (cats.length - 1) / 2;
  cats.forEach((c, i) => pos["cat:" + c] = [480, y0 + i * step, c.toUpperCase()]);
  const line = (a, b) => el("path", {class:"edge", d:`M${pos[a][0]},${pos[a][1]} L${pos[b][0]},${pos[b][1]}`}, E);
  line("ears", "voice"); line("voice", "rules"); line("voice", "router"); line("rules", "tools");
  for (const c of cats) { line("router", "cat:" + c); line("cat:" + c, "tools"); line("cat:" + c, "llm"); }
  line("memory", "llm"); line("tools", "llm"); line("llm", "mouth"); line("tools", "mouth");
  for (const [id, [x, y, name]] of Object.entries(pos)) {
    const cat = id.startsWith("cat:");
    const g = el("g", {class:"n" + (cat ? " cat" : ""), transform:`translate(${x},${y})`}, N);
    el("circle", {class:"halo", r:6}, g);
    el("circle", {class:"core", r:cat ? 3.5 : 7}, g);
    const t = el("text", cat ? {x:9, y:3} : {y:25}, g); t.textContent = name;
    const lbl = cat ? null : el("text", {class:"lbl", y:-15}, g);
    nodes[id] = {g, x, y, lbl};
  }
}
function hit(id, label) {
  if (id.startsWith("cat:")) {
    const c = id.slice(4);
    id = "cat:" + (c.startsWith("use_") ? "abilities" : c);
    document.querySelectorAll(".n.sel").forEach(n => n.classList.remove("sel"));
  }
  if (id === "ears") {
    lastNode = null;
    document.querySelectorAll(".n.sel").forEach(n => n.classList.remove("sel"));
    Object.values(nodes).forEach(n => n.lbl && (n.lbl.textContent = ""));
  }
  const n = nodes[id];
  if (!n) return;
  if (id.startsWith("cat:")) n.g.classList.add("sel");
  if (n.lbl && label) n.lbl.textContent = String(label).slice(0, 22);
  n.g.classList.remove("hit"); void n.g.getBBox(); n.g.classList.add("hit");
  if (lastNode && nodes[lastNode] && lastNode !== id && !still) {
    const a = nodes[lastNode];
    pulses.push({a, b:n, t0:performance.now(), dot:el("circle", {class:"pulse", r:4}, $("#pulses")),
                 trail:el("line", {class:"trail", x1:a.x, y1:a.y, x2:a.x, y2:a.y}, $("#trails"))});
  }
  lastNode = id;
}
function drawPulses(now) {
  pulses = pulses.filter(p => {
    const t = Math.min(1, (now - p.t0) / 450), e = 1 - (1 - t) ** 3;
    const x = p.a.x + (p.b.x - p.a.x) * e, y = p.a.y + (p.b.y - p.a.y) * e;
    p.dot.setAttribute("cx", x); p.dot.setAttribute("cy", y);
    p.trail.setAttribute("x2", x); p.trail.setAttribute("y2", y);
    p.trail.style.opacity = t < 1 ? .7 : Math.max(0, .7 - (now - p.t0 - 450) / 600);
    if (now - p.t0 > 1050) { p.dot.remove(); p.trail.remove(); return false; }
    return true;
  });
}

// --- panels ---
const pct = v => v == null ? "—" : Math.round(v * 100) + "%";
const bar = (id, v) => { $(id).style.width = Math.round(Math.max(0, Math.min(1, v || 0)) * 100) + "%"; };
const hms = s => { s = Math.max(0, Math.floor(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s % 60).padStart(2, "0"); };
const nowS = () => Date.now() / 1000 + clockSkew;

function renderSnap(d) {
  snap = d; clockSkew = d.now - Date.now() / 1000;
  const s = d.system;
  $("#model").textContent = s.model;
  $("#ollama").innerHTML = `<i class="dot ${s.ollama ? "on" : s.ollama === false ? "off" : ""}"></i>${s.ollama ? "ONLINE" : s.ollama === false ? "DOWN" : "—"}`;
  $("#think").textContent = s.think ? "ON" : "OFF";
  $("#mode").textContent = (s.mode || "—").toUpperCase();
  $("#camera").textContent = s.camera == null ? "DISABLED" : s.camera ? "WATCHING" : "OFF";
  $("#firstaudio").textContent = s.first_audio == null ? "—" : s.first_audio.toFixed(1) + " S";
  bar("#cpu", s.cpu); $("#cpuv").textContent = pct(s.cpu);
  bar("#ram", s.ram); $("#ramv").textContent = pct(s.ram);
  if (state === "offline") setState(s.state);

  $("#requests").textContent = d.requests; $("#today").textContent = d.today;
  $("#facts").textContent = d.facts; $("#sessions").textContent = d.sessions;
  const total = d.routes.reduce((n, r) => n + r.n, 0);
  $("#statsub").textContent = total ? total + " ROUTED" : "";
  const max = Math.max(1, ...d.routes.map(r => r.n));
  $("#cats").innerHTML = d.routes.length ? d.routes.slice(0, 9).map(r => `<div class="cat">
    <span>${esc(r.category ? r.category.replace(/^use_/, "+") : "unsure")}</span>
    <div class="bar"><i style="width:${Math.round(r.n / max * 100)}%"></i></div><span>${r.n}</span></div>`).join("")
    : `<div class="empty">Nothing routed yet.</div>`;

  $("#taskcount").textContent = `${d.todos.length} OPEN · ${d.done_today} DONE TODAY`;
  $("#todos").innerHTML = d.todos.length ? d.todos.map((t, i) => `<div class="task"><b>${String(i + 1).padStart(2, "0")}</b>
    <span>${esc(t.text)}</span></div>`).join("") : `<div class="empty">The list is clear.</div>`;
  renderTimers();
  buildGraph(d.categories);
  if (!$("#lines").children.length) d.transcript.forEach(m => addLine(m.role, m.text, m.ts, false));
  if (d.asks && d.asks.length) { if (askId == null) showAsk(d.asks[0].id, d.asks[0].question); }
  else if (askId != null) hideAsk();
}

function renderTimers() {
  const items = (snap?.schedule || []).filter(t => t.due > nowS());
  $("#timers").innerHTML = items.length ? `<div class="sub" style="margin-top:0">COUNTDOWNS</div>` + items.map(t => {
    const left = t.due - nowS(), span = Math.max(1, t.due - (t.created || t.due - 60));
    return `<div class="timer"><div><span>${esc((t.kind || "").toUpperCase())}${t.label ? " · " + esc(t.label) : ""}</span>
      <b>${left > 86400 ? new Date(t.due * 1000).toLocaleString([], {weekday:"short", hour:"2-digit", minute:"2-digit"}) : hms(left)}</b></div>
      <div class="bar"><i style="width:${Math.round(Math.max(0, Math.min(1, 1 - left / span)) * 100)}%"></i></div></div>`;
  }).join("") : "";
}

function addLine(role, text, ts, type = true) {  // returns the line's text span
  const box = $("#lines"), div = document.createElement("div");
  div.className = "line " + role;
  const who = role === "user" ? "YOU" : role === "assistant" ? "JARVIS" : "";
  div.innerHTML = `<small>${who} · ${new Date((ts || nowS()) * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})}</small><span></span>`;
  const span = div.lastChild;
  box.appendChild(div);
  while (box.children.length > 60) box.firstChild.remove();
  if (type && !still) {
    let i = 0; const step = Math.max(1, Math.ceil(text.length / 70));
    const tick = () => { i += step; span.textContent = text.slice(0, i); box.scrollTop = box.scrollHeight;
                         if (i < text.length) setTimeout(tick, 16); };
    tick();
  } else span.textContent = text;
  box.scrollTop = box.scrollHeight;
  return span;
}
function addTag(text, kind = "") {
  const box = $("#lines"), div = document.createElement("div");
  div.className = "tag " + kind; div.textContent = text; box.appendChild(div); box.scrollTop = box.scrollHeight;
}

// a reply spoken clause by clause: one transcript line that grows as JARVIS talks
let live = null, pendingAck = null, askId = null;
function liveSay(text) {
  if (pendingAck && text === pendingAck) { pendingAck = null; return; }  // already shown as a tag
  if (!live) { live = addLine("assistant", "", null, false); live.parentNode.classList.add("live"); }
  live.textContent += (live.textContent ? " " : "") + text;
  $("#statesub").textContent = text;
  $("#lines").scrollTop = $("#lines").scrollHeight;
}
function endLive(text) {
  if (!live) return false;
  if (text) live.textContent = text;
  live.parentNode.classList.remove("live"); live = null;
  return true;
}
function showAsk(id, question) {
  askId = id; $("#askq").textContent = question; $("#ask").hidden = false;
  $("#askyes").disabled = $("#askno").disabled = false;
}
function hideAsk(id) { if (id == null || id === askId) { askId = null; $("#ask").hidden = true; } }
async function answerAsk(yes) {
  if (askId == null) return;
  $("#askyes").disabled = $("#askno").disabled = true;
  try {
    await fetch("/api/answer", {method:"POST", headers:{"Content-Type":"application/json", "X-Jarvis":"1"},
                                body:JSON.stringify({id:askId, yes})});
  } catch { $("#askyes").disabled = $("#askno").disabled = false; }
}
$("#askyes").onclick = () => answerAsk(true);
$("#askno").onclick = () => answerAsk(false);
const GATE = {blocked:["⛔ BLOCKED ", "bad"], confirm:["? CONFIRM ", ""], approved:["✓ APPROVED ", "good"],
              declined:["✗ DECLINED ", "bad"]};

// --- live data ---
let refreshTimer = null;
async function load() {
  try { renderSnap(await (await fetch("/api/snapshot")).json()); }
  catch { setState("offline"); }
}
const refreshSoon = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(load, 300); };

let es = null;
function connect() {
  es = new EventSource("/events");
  es.onopen = () => load();
  es.onerror = () => { setState("offline"); mic = voice = 0; };
  es.onmessage = m => {
    const e = JSON.parse(m.data);
    if (e.kind === "level") { mic = e.mic || 0; voice = e.voice || 0; return; }
    if (e.kind === "state") setState(e.state);
    else if (e.kind === "node") {
      hit(e.node, e.label);
      if (e.node.startsWith("cat:")) { $("#statesub").textContent = "routed → " + e.node.slice(4); addTag("↳ ROUTE  " + e.node.slice(4)); }
      if (e.node === "tools" && e.label) { $("#statesub").textContent = "running " + e.label; addTag("⚙ TOOL   " + e.label); refreshSoon(); }
      if (e.node === "rules" && e.label) addTag("⚡ RULE   " + e.label);
    }
    else if (e.kind === "heard") { endLive(); addLine("user", e.text, e.t); $("#statesub").textContent = "“" + e.text + "”"; }
    else if (e.kind === "say") liveSay(e.text);
    else if (e.kind === "ack") { pendingAck = e.text; addTag("… " + e.text, "soft"); }
    else if (e.kind === "reply") { if (!endLive(e.text)) addLine("assistant", e.text, e.t); $("#statesub").textContent = ""; refreshSoon(); }
    else if (e.kind === "barge") { endLive(); addTag(e.how === "stop" ? "✋ TOLD TO STOP" : "✋ INTERRUPTED (" + e.how + ")", "soft"); }
    else if (e.kind === "gate") { const [label, kind] = GATE[e.tier] || ["", ""];
                                   addTag(label + e.tool + (e.tier === "blocked" && e.why ? " · " + e.why : ""), kind); }
    else if (e.kind === "ask") showAsk(e.ask, e.question);
    else if (e.kind === "answered") { hideAsk(e.ask); endLive();
      if (e.how === "click") addTag((e.yes ? "✓ YES" : "✗ NO") + " (clicked)", e.yes ? "good" : "bad"); }
    else if (e.kind === "error") { addTag("✖ " + e.text); }
    else if (e.kind === "brain") { addTag("⇄ " + e.model); refreshSoon(); }
    else if (e.kind === "info") refreshSoon();
  };
}

// --- one loop draws everything ---
let lastT = performance.now(), histAt = 0, histClock = 0;
function frame(now) {
  const dt = Math.min(.1, (now - lastT) / 1000); lastT = now;
  const target = STATES[state];
  for (let i = 0; i < 3; i++) rgb[i] += (target.rgb[i] - rgb[i]) * Math.min(1, dt * 4);
  speed += (target.speed - speed) * Math.min(1, dt * 2);
  const want = state === "offline" ? 0 : Math.max(voice, mic * .85);
  lv += (want - lv) * Math.min(1, dt * (want > lv ? 18 : 6));
  if ((histClock += dt) > .05) { histClock = 0; hist[histAt] = lv; histAt = (histAt + 1) % hist.length; }
  bar("#mic", mic); $("#micv").textContent = Math.round(mic * 100);
  bar("#voice", voice); $("#voicev").textContent = Math.round(voice * 100);
  drawField(dt); drawOrb(now / 1000); drawPulses(now);
  requestAnimationFrame(frame);
}

setInterval(() => {
  const d = new Date(nowS() * 1000);
  $("#clock").textContent = d.toLocaleTimeString([], {hour12:false});
  $("#date").textContent = d.toLocaleDateString([], {weekday:"long", day:"numeric", month:"short"}).toUpperCase();
  if (snap) { $("#uptime").textContent = "UP " + hms(nowS() - snap.system.started); renderTimers(); }
}, 1000);
setInterval(() => { if (document.visibilityState === "visible") load(); }, 10000);
buildGraph(["time","timer","memory","web","files","websites","tasks","messages","apps","computer","devices","brain",
            "skills","chat","abilities"]);
connect(); load(); requestAnimationFrame(frame);
</script></body></html>"""


def demo():
    """A scripted conversation through the real reporting calls, over and over: for working on the HUD and the
    orb without a microphone, or for showing them off. Nothing is actually run."""
    import math

    def talk(seconds, source="voice"):
        end = time.time() + seconds
        while time.time() < end:
            level(source, .35 + .3 * abs(math.sin(time.time() * 9)) + .15 * math.sin(time.time() * 23))
            time.sleep(.05)

    def say(*clauses):
        state("speaking")
        for c in clauses:
            emit("say", text=c)
            talk(.25 + len(c) / 28)
        emit("reply", text=" ".join(clauses))
        state("idle")

    while True:
        state("idle")
        time.sleep(2.5)
        state("listening")
        talk(1.6, "mic")
        emit("heard", text="Clear out my documents folder with PowerShell", speaker="insaf")
        state("thinking")
        for n, lbl in (("ears", "voice"), ("voice", "insaf"), ("router", "computer"), ("cat:computer", ""),
                       ("memory", "2 facts"), ("tools", "run_shell")):
            node(n, lbl)
            time.sleep(.35)
        emit("gate", tool="run_shell", tier="blocked", why="deleting folders recursively")
        say("I won't do that, sir.", "Deleting your documents folder isn't something I'll run, even when asked.")
        time.sleep(2)
        emit("heard", text="Then make a backup folder on the desktop", speaker="insaf")
        state("thinking")
        node("router", "computer")
        node("tools", "run_shell")
        why = "Shall I run this command: New-Item -ItemType Directory ~\\Desktop\\Backup?"
        emit("gate", tool="run_shell", tier="confirm", why=why)
        qid = ask(why)
        state("speaking")
        emit("say", text=why)
        talk(2.4)
        state("listening")
        end = time.time() + 12  # time to click YES or NO on the page or the orb
        while time.time() < end and pending_answer(qid) is None:
            time.sleep(.1)
        yes = pending_answer(qid)
        close_ask(qid, "click" if yes is not None else "timeout", yes)
        emit("gate", tool="run_shell", tier="approved" if yes else "declined", why="")
        if yes:
            say("Done, sir.", "There's a Backup folder on your desktop now.", "Anything else?")
        else:
            say("Very well, sir. I've left it alone.")
        time.sleep(1.5)
        emit("heard", text="What's the weather going to be like this weekend?", speaker="insaf")
        state("thinking")
        node("router", "web")
        node("tools", "web_search")
        emit("ack", text="One moment, sir.")
        time.sleep(1.2)
        state("speaking")
        for c in ("Saturday looks dry and mild, around nineteen degrees,", "with rain arriving on Sunday"):
            emit("say", text=c)
            talk(1.5)
        emit("barge", how="voice")
        emit("reply", text="Saturday looks dry and mild, around nineteen degrees, with rain arriving on Sunday")
        state("listening")
        talk(1.0, "mic")
        emit("heard", text="Stop", speaker="insaf")
        emit("barge", how="stop")
        state("idle")
        time.sleep(4)


if __name__ == "__main__":
    # standalone: shows tasks, timers and stats from the database, but nothing live (JARVIS isn't in this process)
    url = start()
    if "--demo" in sys.argv:
        print(f"JARVIS HUD demo: {url}  (a scripted conversation on a loop; Ctrl-C to stop)")
        threading.Thread(target=demo, daemon=True, name="demo").start()
    else:
        print(f"JARVIS HUD: {url}  (Ctrl-C to stop; run jarvis.py instead for the live view)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
