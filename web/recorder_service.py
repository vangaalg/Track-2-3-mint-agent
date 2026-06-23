"""Always-on host for the OI/macro recorder (Railway) — token endpoint + the loop.

One web process. On startup it restores ``data/`` from the private data repo and spawns
two daemon threads: ``feeds.recorder.run`` (the 15m/60m accumulation loop) and a git-sync
loop that commits + pushes ``data/`` periodically. The daily Breeze session token (which
expires and has no refresh API) is posted from the trader's phone to ``POST /token``; because
``loaders.breeze.get_breeze_client`` reads the env fresh on every fetch, the new token reaches
the recorder on its next cycle — no restart.

Token persistence: a restart wipes the in-memory token, so the POSTed token is ALSO written
under ``data/`` (the durable data repo) and auto-restored on boot — surviving redeploys
without a manual re-POST. This is a deliberate exception to "secrets never enter the repo":
the token expires daily and the data repo is private (the trader accepted the tradeoff).

Lightweight imports only (no agent/analysis) so the container is cheap to boot.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from feeds import recorder
from deploy import gitsync, control

STATUS: dict = {"started": None, "last_cycle": None, "saved": [], "macro": None,
                "errors": [], "last_push": None}

# Daily Breeze token persistence/restore is shared with the cockpit (deploy.control).
_save_token_file = control.save_token_file
_load_token_file = control.load_token_file


def _restore_token() -> bool:
    """On boot, restore the persisted token if none is in the env (sets STATUS flag)."""
    if control.restore_token():
        STATUS["token_restored"] = True
        return True
    return False


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("RECORDER_NO_BG") != "1":       # tests skip the live threads
        _start_background()
    yield


app = FastAPI(title="Recorder", lifespan=lifespan)


def _on_cycle(info: dict) -> None:
    STATUS.update(last_cycle=info["ts"], saved=info["saved"],
                  macro=info["macro"], errors=info["errors"])


def _recorder_thread() -> None:
    names = os.environ.get("RECORDER_INSTRUMENTS")
    insts = recorder.select_instruments(
        names.split(",") if names else None,
        with_stocks=os.environ.get("RECORDER_STOCKS") == "1")
    recorder.run(insts or None,
                 index_every=int(os.environ.get("INDEX_EVERY_MIN", "15")),
                 stock_every=int(os.environ.get("STOCK_EVERY_MIN", "60")),
                 on_cycle=_on_cycle)


def _sync_thread() -> None:
    every = int(os.environ.get("SYNC_EVERY_MIN", "30")) * 60
    while True:
        time.sleep(every)
        try:
            if gitsync.commit_push("data", msg=f"recorder {time.strftime('%Y-%m-%d %H:%M')}"):
                STATUS["last_push"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:                      # never kill the thread
            STATUS["errors"] = [f"sync: {exc}"]


def _start_background() -> None:
    repo = os.environ.get("DATA_REPO_URL")
    if repo:
        try:
            gitsync.clone_or_pull(repo, "data")
        except Exception as exc:
            STATUS["errors"] = [f"clone: {exc}"]
    _restore_token()                                  # survive a restart without a re-POST
    STATUS["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    threading.Thread(target=_recorder_thread, daemon=True).start()
    if repo:
        threading.Thread(target=_sync_thread, daemon=True).start()


@app.get("/healthz")
def healthz():
    return JSONResponse(STATUS)


def _check(secret: str) -> None:
    want = os.environ.get("RECORDER_TOKEN_SECRET")
    if not want or secret != want:
        raise HTTPException(status_code=403, detail="bad secret")


@app.post("/token")
def set_token(token: str = Form(...), secret: str = Form(...)):
    """Update the daily Breeze session token (guarded by RECORDER_TOKEN_SECRET).

    Sets the env (picked up on the next fetch) AND persists it under data/ so a restart
    restores it; pushes the data repo immediately so the persisted copy isn't lost if the
    container restarts before the periodic sync."""
    _check(secret)
    os.environ["BREEZE_SESSION_TOKEN"] = token.strip()
    _save_token_file(token)
    if os.environ.get("DATA_REPO_URL"):               # persist now, don't wait for the sync loop
        try:
            gitsync.commit_push("data", msg="recorder token update")
        except Exception:
            pass
    return {"ok": True, "breeze": _probe_breeze()}


@app.post("/context")
def set_context(secret: str = Form(...), gift: str = Form(""), events: str = Form("")):
    """Save the trader's daily overlay: manual GIFT Nifty + the overnight-events note (the text
    Claude produced from a screenshot). Manual GIFT is the source of truth over the auto-fetch."""
    _check(secret)
    from feeds import context_store
    ctx = context_store.save_context(gift_manual=(gift.strip() or None),
                                     events_note=(events if events else None))
    STATUS["context"] = {"gift_manual": ctx.get("gift_manual"),
                         "events_note": (ctx.get("events_note") or "")[:200],
                         "set_at": ctx.get("set_at")}
    return {"ok": True, "context": STATUS["context"]}


def _probe_breeze() -> str:
    """Best-effort: confirm the new token actually authenticates."""
    try:
        from loaders.breeze import get_breeze_client
        get_breeze_client()                           # constructs + handshakes
        return "connected"
    except Exception as exc:
        return f"token set, but probe failed: {exc}"


@app.get("/", response_class=HTMLResponse)
def home():
    s = STATUS
    err = "<br>".join(s["errors"]) if s["errors"] else "none"
    ctx = s.get("context") or {}
    inp = "width:100%;padding:8px"
    return f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Recorder</title>
<body style="font-family:system-ui;max-width:480px;margin:24px auto;padding:0 16px">
<h2>OI/macro recorder</h2>
<form method=post action=/token>
  <p><label>Breeze session token<br><input name=token style="{inp}"
     placeholder="paste today's token"></label></p>
  <p><label>Secret<br><input name=secret type=password style="{inp}"></label></p>
  <button style="padding:10px 16px">Update token</button>
</form>
<h3>Morning overlay (GIFT + events)</h3>
<form method=post action=/context>
  <p><label>GIFT Nifty (manual)<br><input name=gift style="{inp}"
     placeholder="e.g. 24050 (overrides auto)"></label></p>
  <p><label>Overnight events note (paste Claude's brief)<br>
     <textarea name=events rows=4 style="{inp}"></textarea></label></p>
  <p><label>Secret<br><input name=secret type=password style="{inp}"></label></p>
  <button style="padding:10px 16px">Save overlay</button>
</form>
<hr>
<pre>started:    {s['started']}
last cycle: {s['last_cycle']}
saved:      {s['saved']}
macro:      {s['macro']}
last push:  {s['last_push']}
gift(man):  {ctx.get('gift_manual')}
events:     {(ctx.get('events_note') or '')[:120]}
errors:     {err}</pre>
</body>"""
