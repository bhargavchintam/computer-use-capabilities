"""Minimal operator console, served from inside the runner process (localhost only).

It is a deliberately thin surface over the real mechanism in control.py:
intervention requests with context, and decisions that move the control lease.
A production console (co-browsing, routing, auth, SLAs) is out of scope; see REPORT.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .control import DecisionKind, SessionController
from .evidence import RunRecorder


class _EmbeddedServer(uvicorn.Server):
    """uvicorn inside our event loop: leave signal handling to the runner so evidence gets flushed."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class DecisionBody(BaseModel):
    decision: DecisionKind
    operator: str
    note: str | None = None


class DialogBody(BaseModel):
    decision: str
    operator: str


class OperatorConsole:
    def __init__(self, controller: SessionController, recorder: RunRecorder, *, port: int = 8765) -> None:
        self.controller = controller
        self.recorder = recorder
        self.port = port
        self.app = self._build()
        self._server: _EmbeddedServer | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def _build(self) -> FastAPI:
        app = FastAPI(title="cua operator console", docs_url=None, redoc_url=None, openapi_url=None)
        c = self.controller

        @app.get("/")
        async def index() -> HTMLResponse:
            return HTMLResponse(_PAGE)

        @app.get("/api/state")
        async def state() -> JSONResponse:
            reqs = sorted(c.requests.values(), key=lambda r: r.requested_at, reverse=True)
            live = [a.model_dump(mode="json") for a in c._human_actions] if c.state.value == "human" else []
            step_done = None
            if c.state.value == "human" and c.step_done_probe is not None:
                step_done = await c.step_done_probe()  # read-only look at the live page
            return JSONResponse(
                {
                    "run_id": self.recorder.run_id,
                    "control": {"state": c.state.value, "holder": c.holder, "epoch": c.epoch},
                    "interventions": [r.model_dump(mode="json") for r in reqs],
                    "live_human_actions": live,
                    "pending_dialog": c.pending_dialog.view() if c.pending_dialog else None,
                    "step_done": step_done,
                }
            )

        @app.get("/api/interventions/{iid}/screenshot")
        async def screenshot(iid: str) -> Any:
            req = c.requests.get(iid)
            if req is None or not req.screenshot:
                raise HTTPException(404)
            return FileResponse(self.recorder.dir / req.screenshot, media_type="image/png")

        @app.post("/api/interventions/{iid}/decision")
        async def decide(iid: str, body: DecisionBody) -> JSONResponse:
            ok, msg = c.decide(iid, body.decision, body.operator.strip() or "operator", body.note)
            self.recorder.event(
                "operator_decision_received",
                intervention_id=iid,
                decision=body.decision,
                operator=body.operator,
                accepted=ok,
                detail=msg,
            )
            return JSONResponse({"ok": ok, "detail": msg}, status_code=200 if ok else 409)

        @app.post("/api/dialogs/{did}")
        async def answer_dialog(did: str, body: DialogBody) -> JSONResponse:
            ok, msg = c.answer_dialog(did, body.decision, body.operator.strip() or "operator")
            return JSONResponse({"ok": ok, "detail": msg}, status_code=200 if ok else 409)

        return app

    async def start(self) -> None:
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
        )
        self._server = _EmbeddedServer(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.05)
        if not self._server.started:  # e.g. the port is taken: an operator could never answer
            self._server.should_exit = True
            raise RuntimeError(f"the operator console could not start on {self.url} (port in use?)")
        self.recorder.event("operator_console_started", url=self.url)
        print(f"\n  Operator console: {self.url}\n", flush=True)

    async def stop(self) -> None:
        if self._server and self._task:
            self._server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Operator Console</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--muted:#5b6474;--line:#dde1e8;--accent:#2457c5;--warn:#b45309;--ok:#15803d;--bad:#b91c1c;--chip:#eef2f8}
@media (prefers-color-scheme: dark){:root{--bg:#12151b;--card:#1b2029;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2c3340;--accent:#7aa2ff;--warn:#f0a44b;--ok:#4ade80;--bad:#f87171;--chip:#242b37}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0}
h1{font-size:16px;margin:0}.muted{color:var(--muted)}main{max-width:1100px;margin:0 auto;padding:16px}
.lease{display:flex;gap:8px;align-items:center}.dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
.dot.automation{background:var(--accent)}.dot.awaiting_human{background:var(--warn)}.dot.human{background:var(--ok)}.dot.closed{background:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
.row{display:flex;gap:16px;flex-wrap:wrap}.col{flex:1 1 340px;min-width:0}
.chip{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--chip);font-size:12px;margin-right:6px}
.kind-approval{color:var(--accent)}.kind-human_required,.kind-implicit_takeover{color:var(--ok)}.kind-unrecoverable,.kind-stuck{color:var(--bad)}
img{max-width:100%;border:1px solid var(--line);border-radius:6px}pre{white-space:pre-wrap;background:var(--chip);padding:10px;border-radius:6px;max-height:260px;overflow:auto;font-size:12px}
button{font:inherit;padding:7px 14px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;margin:4px 6px 0 0}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--bad)}
input,textarea{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
textarea{width:100%;min-height:54px}dl{display:grid;grid-template-columns:120px 1fr;gap:4px 10px;margin:8px 0}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.empty{text-align:center;padding:40px;color:var(--muted)}
</style></head>
<body>
<header>
  <div><h1>Operator Console</h1><div class="muted" id="run">connecting…</div></div>
  <div class="lease"><span class="dot" id="dot"></span><span id="lease">…</span></div>
  <label class="muted">Operator <input id="op" size="14" placeholder="your name"></label>
</header>
<main id="list"><div class="empty">No interventions yet. Automation is running.</div></main>
<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
try { $("#op").value = localStorage.getItem("cua-operator") || ""; } catch (e) {}
$("#op").addEventListener("change", () => { try { localStorage.setItem("cua-operator", $("#op").value); } catch (e) {} });
async function decide(id, decision) {
  const operator = $("#op").value.trim() || "operator";
  const note = (document.getElementById("note-" + id) || {}).value || null;
  const r = await fetch(`/api/interventions/${id}/decision`, {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({decision, operator, note})});
  const j = await r.json(); if (!j.ok) alert(j.detail); refresh();
}
function buttons(iv, control) {
  const b = [], can = (d) => iv.allowed_decisions.includes(d);
  if (iv.status === "pending" && control.state === "awaiting_human") {
    if (can("approve")) b.push(`<button class="primary" onclick="decide('${iv.id}','approve')">Approve step</button>`);
    if (can("reject")) b.push(`<button onclick="decide('${iv.id}','reject')">Reject</button>`);
    if (can("take_control")) b.push(`<button onclick="decide('${iv.id}','take_control')">Take control of the live session</button>`);
    if (can("abort")) b.push(`<button class="danger" onclick="decide('${iv.id}','abort')">Abort run</button>`);
  } else if (iv.status === "active") {
    if (iv.done_when) b.push(`<p><b>You hold the live session.</b> Finish the step in the bank window, and while it shows the result (${esc(iv.done_when)}) come back here and hand control back. Do not navigate away from that page first.</p>`);
    b.push(`<textarea id="note-${iv.id}" placeholder="What did you do? (recorded with the run)"></textarea>`);
    b.push(`<button class="primary" onclick="decide('${iv.id}','hand_back')">Hand control back to automation</button>`);
    b.push(`<button class="danger" onclick="decide('${iv.id}','abort')">Abort run</button>`);
  }
  return b.join("");
}
async function answerDialog(id, decision) {
  const operator = $("#op").value.trim() || "operator";
  const r = await fetch(`/api/dialogs/${id}`, {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({decision, operator})});
  const j = await r.json(); if (!j.ok) alert(j.detail); last = ""; refresh();
}
function dialogCard(d) {
  return `<div class="card"><div><span class="chip kind-human_required">page dialog</span><span class="muted">${esc(d.id)}</span></div>
    <h3 style="margin:8px 0 4px">The page opened a ${esc(d.type)} dialog while you hold the session</h3>
    <pre>${esc(d.message)}</pre>
    <button class="primary" onclick="answerDialog('${d.id}','accept')">Accept (OK)</button>
    <button onclick="answerDialog('${d.id}','dismiss')">Dismiss (Cancel)</button></div>`;
}
function card(iv, control, live) {
  const actions = (iv.status === "active" ? live : iv.human_actions) || [];
  return `<div class="card"><div class="row"><div class="col">
    <div><span class="chip kind-${esc(iv.kind)}">${esc(iv.kind.replace("_"," "))}</span><span class="chip">${esc(iv.status)}</span><span class="muted">${esc(iv.id)}</span></div>
    <h3 style="margin:8px 0 4px">${esc(iv.reason)}</h3>
    <dl><dt>Capability</dt><dd>${esc(iv.capability || iv.goal || "")}</dd><dt>Step</dt><dd>${esc(iv.step_id || "")} ${iv.step_intent ? "— " + esc(iv.step_intent) : ""}</dd>
    ${iv.proposed_action ? `<dt>Proposed</dt><dd>${esc(iv.proposed_action)}</dd>` : ""}
    ${iv.done_when ? `<dt>Done when</dt><dd>${esc(iv.done_when)}</dd>` : ""}
    <dt>Requested</dt><dd>${esc(iv.requested_at)} (deadline ${esc(iv.deadline)})</dd>
    ${iv.decision ? `<dt>Decision</dt><dd>${esc(iv.decision)} by ${esc(iv.operator)}${iv.note ? ": " + esc(iv.note) : ""}</dd>` : ""}</dl>
    ${buttons(iv, control)}
    ${actions.length ? `<p class="muted">Human actions captured (${actions.length}):</p><pre>${actions.map(a => esc(`${a.type} ${a.target}${a.value ? " = " + a.value : ""}`)).join("\\n")}</pre>` : ""}
    ${iv.snapshot_excerpt ? `<details><summary class="muted">Page excerpt (redacted)</summary><pre>${esc(iv.snapshot_excerpt)}</pre></details>` : ""}
  </div><div class="col">${iv.screenshot ? `<img src="/api/interventions/${iv.id}/screenshot?t=${Date.now()}" alt="masked screenshot at escalation">` : ""}</div></div></div>`;
}
let last = "";
async function refresh() {
  try {
    const s = await (await fetch("/api/state")).json();
    $("#run").textContent = "Run " + s.run_id;
    $("#dot").className = "dot " + s.control.state;
    $("#lease").textContent = `${s.control.state.replace("_", " ")} · holder: ${s.control.holder} · epoch ${s.control.epoch}` +
      (s.step_done ? " · STEP COMPLETE: hand control back now" : "");
    const key = JSON.stringify([s.control, s.interventions.map(i => [i.id, i.status]), s.live_human_actions.length,
      s.pending_dialog && s.pending_dialog.id]);
    if (key !== last) {
      last = key;
      const dialog = s.pending_dialog ? dialogCard(s.pending_dialog) : "";
      $("#list").innerHTML = dialog + (s.interventions.length ? s.interventions.map(i => card(i, s.control, s.live_human_actions)).join("")
        : '<div class="empty">No interventions yet. Automation is running.</div>');
    }
  } catch (e) { $("#run").textContent = "runner finished or unreachable"; }
}
refresh(); setInterval(refresh, 1200);
</script></body></html>"""
