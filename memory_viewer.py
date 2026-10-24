"""Memory viewer: a local web page showing what JARVIS remembers, in plain language.
Run: python memory_viewer.py  →  open http://127.0.0.1:8765
Reads the same SQLite file JARVIS uses, live, so it's safe to keep open while JARVIS runs.
Only listens on this machine (127.0.0.1)."""
import json
from contextlib import closing
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
SESSION_GAP = 30 * 60  # a silence longer than this starts a new conversation


def _db():
    db = sqlite3.connect(config.DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def snapshot() -> dict:
    with closing(_db()) as db:
        facts = [dict(r) for r in db.execute(
            "SELECT id, fact, created, speaker FROM facts ORDER BY created DESC")]
        log = [dict(r) for r in db.execute("SELECT id, role, content, ts FROM log ORDER BY ts")]
    sessions, current, last_ts = [], [], None
    for row in log:
        if last_ts is not None and row["ts"] - last_ts > SESSION_GAP:
            sessions.append(current)
            current = []
        current.append(row)
        last_ts = row["ts"]
    if current:
        sessions.append(current)
    return {"facts": facts, "sessions": sessions[::-1]}  # newest conversation first


def delete_fact(fact_id: int) -> bool:
    with closing(_db()) as db, db:  # outer closes the connection, inner commits
        return db.execute("DELETE FROM facts WHERE id = ?", (fact_id,)).rowcount > 0


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/api/memory":
            self._send(200, json.dumps(snapshot()))
        else:
            self._send(404, '{"error": "not found"}')

    def do_POST(self):
        # /api/facts/<id>/delete — same-origin only, so other sites can't wipe memories
        parts = self.path.strip("/").split("/")
        origin = self.headers.get("Origin", "")
        if origin and origin.split("//")[-1] != self.headers.get("Host"):
            return self._send(403, '{"error": "cross-origin"}')
        if len(parts) == 4 and parts[:2] == ["api", "facts"] and parts[3] == "delete" and parts[2].isdigit():
            return self._send(200, json.dumps({"deleted": delete_fact(int(parts[2]))}))
        self._send(404, '{"error": "not found"}')

    def log_message(self, *args):
        pass  # keep the console quiet


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS Memory</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1b1f24; --muted:#667080; --line:#e3e6ea;
        --accent:#0b6bcb; --you:#e8f1fb; --jarvis:#f1f2f4; --danger:#c62828; }
@media (prefers-color-scheme: dark) { :root { --bg:#111418; --card:#1a1f25; --text:#e6e9ed;
        --muted:#8b95a3; --line:#2a313a; --accent:#5aa9f0; --you:#1c2b3c; --jarvis:#22282f; --danger:#ef6b6b; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:860px; margin:0 auto; padding:24px 16px 64px; }
header { display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; }
h1 { font-size:22px; margin:0; }
.sub { color:var(--muted); font-size:13px; }
input[type=search] { flex:1 1 240px; max-width:320px; padding:8px 12px; border:1px solid var(--line);
       border-radius:8px; background:var(--card); color:var(--text); font:inherit; }
nav { display:flex; gap:4px; margin:20px 0 12px; border-bottom:1px solid var(--line); }
nav button { background:none; border:0; padding:8px 14px; font:inherit; color:var(--muted);
       cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px; }
nav button.on { color:var(--text); border-color:var(--accent); font-weight:600; }
h2 { font-size:15px; margin:22px 0 8px; display:flex; gap:8px; align-items:baseline; }
h2 .sub { font-weight:400; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; }
.fact { display:flex; gap:12px; align-items:flex-start; padding:12px 14px; border-top:1px solid var(--line); }
.fact:first-child { border-top:0; }
.fact .body { flex:1; min-width:0; overflow-wrap:anywhere; }
.fact .when { color:var(--muted); font-size:12px; }
.del { background:none; border:1px solid var(--line); color:var(--muted); border-radius:6px;
       padding:2px 10px; font:inherit; font-size:12px; cursor:pointer; }
.del:hover { color:var(--danger); border-color:var(--danger); }
details { margin-bottom:10px; }
summary { cursor:pointer; padding:12px 14px; list-style:none; display:flex; justify-content:space-between; gap:12px; }
summary::-webkit-details-marker { display:none; }
summary .preview { color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.msgs { padding:4px 14px 14px; display:flex; flex-direction:column; gap:6px; }
.msg { max-width:85%; padding:8px 12px; border-radius:12px; overflow-wrap:anywhere; }
.msg.user { align-self:flex-end; background:var(--you); }
.msg.assistant { align-self:flex-start; background:var(--jarvis); }
.msg .t { display:block; color:var(--muted); font-size:11px; }
.empty { color:var(--muted); padding:16px 14px; }
.explain { color:var(--muted); font-size:13px; margin:4px 0 0; }
mark { background:#ffe58a; color:inherit; border-radius:2px; }
</style></head>
<body><main>
<header>
  <div><h1>JARVIS Memory</h1><div class="sub" id="stats">Loading…</div></div>
  <input type="search" id="q" placeholder="Search memories and conversations">
</header>
<nav><button data-tab="facts" class="on">Facts</button><button data-tab="convos">Conversations</button></nav>
<section id="facts"></section>
<section id="convos" hidden></section>
</main>
<script>
let data = {facts: [], sessions: []};
const $ = s => document.querySelector(s);
const esc = s => s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const hl = (s, q) => { s = esc(s); if (!q) return s;
  const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"); return s.replace(re, m => `<mark>${m}</mark>`); };
const when = ts => new Date(ts * 1000).toLocaleString(undefined, {dateStyle:"medium", timeStyle:"short"});
const ago = ts => { const d = Date.now()/1000 - ts;
  if (d < 60) return "just now"; if (d < 3600) return Math.floor(d/60) + " min ago";
  if (d < 86400) return Math.floor(d/3600) + " h ago"; return Math.floor(d/86400) + " days ago"; };
const who = s => s ? s[0].toUpperCase() + s.slice(1) : null;

function renderFacts(q) {
  const groups = {};
  for (const f of data.facts) {
    if (q && !f.fact.toLowerCase().includes(q)) continue;
    (groups[f.speaker || ""] ||= []).push(f);
  }
  const keys = Object.keys(groups).sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
  if (!keys.length) { $("#facts").innerHTML = `<div class="card empty">${q ? "No facts match." : "JARVIS hasn't saved any facts yet."}</div>`; return; }
  $("#facts").innerHTML = keys.map(k => `
    <h2>${k ? "About " + esc(who(k)) : "Shared"} <span class="sub">${groups[k].length}</span></h2>
    <p class="explain">${k ? `Only used when JARVIS recognizes ${esc(who(k))}'s voice.` : "Saved while the voice wasn't recognized — used for everyone."}</p>
    <div class="card">${groups[k].map(f => `
      <div class="fact"><div class="body">${hl(f.fact, q)}
        <div class="when" title="${when(f.created)}">Saved ${ago(f.created)} · ${when(f.created)}</div></div>
        <button class="del" data-id="${f.id}" data-fact="${esc(f.fact)}">Forget</button></div>`).join("")}</div>`).join("");
}

function renderConvos(q) {
  const out = [];
  data.sessions.forEach((s, i) => {
    if (q && !s.some(m => m.content.toLowerCase().includes(q))) return;
    const first = s.find(m => m.role === "user") || s[0];
    out.push(`<details class="card" ${q || i === 0 ? "open" : ""}>
      <summary><span class="preview">${esc(first.content)}</span>
        <span class="sub">${when(s[0].ts)} · ${s.filter(m => m.role === "user").length} messages</span></summary>
      <div class="msgs">${s.map(m => `<div class="msg ${m.role}">${hl(m.content, q)}
        <span class="t">${m.role === "user" ? "You" : "JARVIS"} · ${new Date(m.ts*1000).toLocaleTimeString(undefined, {timeStyle:"short"})}</span></div>`).join("")}</div>
    </details>`);
  });
  $("#convos").innerHTML = out.join("") || `<div class="card empty">${q ? "No conversations match." : "No conversations yet."}</div>`;
}

function render() {
  const q = $("#q").value.trim().toLowerCase();
  renderFacts(q); renderConvos(q);
  const msgs = data.sessions.reduce((n, s) => n + s.length, 0);
  $("#stats").textContent = `${data.facts.length} saved facts · ${data.sessions.length} conversations · ${msgs} messages`;
}

async function load() {
  try { data = await (await fetch("/api/memory")).json(); render(); }
  catch { $("#stats").textContent = "Can't reach the memory viewer — is memory_viewer.py still running?"; }
}

document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.toggle("on", x === b));
  $("#facts").hidden = b.dataset.tab !== "facts"; $("#convos").hidden = b.dataset.tab !== "convos";
});
$("#q").oninput = render;
$("#facts").onclick = async e => {
  const b = e.target.closest(".del"); if (!b) return;
  if (!confirm(`Make JARVIS forget this?\n\n"${b.dataset.fact}"`)) return;
  await fetch(`/api/facts/${b.dataset.id}/delete`, {method: "POST"}); load();
};
load(); setInterval(() => { if (document.visibilityState === "visible" && !$("#q").value) load(); }, 5000);
</script></body></html>"""


if __name__ == "__main__":
    print(f"JARVIS memory viewer: http://127.0.0.1:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
