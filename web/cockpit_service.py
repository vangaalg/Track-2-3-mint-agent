"""Always-on host for the trading COCKPIT (Railway) — the live UI + the OI recorder.

A deployable wrapper around ``web.server.app`` (the cockpit). One Railway service runs BOTH
the dashboard AND the OI/macro recorder loop, so the trader has a single URL, a single login,
and a single place to enter the daily token. It adds the deploy concerns the local
``uvicorn web.server:app`` doesn't need:

1. **Auth** — HTTP Basic over Railway's TLS (``COCKPIT_USER`` / ``COCKPIT_PASSWORD``),
   fail-closed so the cockpit is never exposed unauthenticated. The cockpit makes Breeze
   pulls + Claude calls, so an open URL would leak positions and spend credits.
2. **Daily Breeze token** — entered once in the dashboard (the header 🔑 button →
   ``POST /api/breeze-token``); applied to ``os.environ`` (both the cockpit pulls and the
   in-process recorder read it fresh on every fetch), persisted under the data repo, and
   eagerly pushed so a restart restores it. The ``/token`` page is a secret-guarded fallback.
3. **OI/macro recorder** — ``feeds.recorder.run`` runs in a daemon thread (15-min indices /
   60-min stocks), so OI accumulation no longer needs a separate service. This service is the
   **sole writer** of the data repo (no two-writer conflict).
4. **Journal persistence** — the decision log + learning DB are redirected (via the
   ``JOURNAL_DB`` / ``DECISIONS_LOG`` env, honored by ``web.server``) into ``journal_store/``,
   a clone of a SEPARATE private repo (``JOURNAL_REPO_URL``) pushed periodically + after
   each decision, so the track-record + Claude's memory survive redeploys.

The cockpit app is **mounted** under a fresh outer app (not mutated in place) so importing
this module never alters ``web.server.app`` for the rest of the test suite. Deploy with the
start command ``uvicorn web.cockpit_service:app`` (also the Procfile default) — see DEPLOY.md.
Set ``COCKPIT_NO_BG=1`` to skip the background threads (tests / local). To split the recorder
back out into its own service instead, run ``web.recorder_service`` separately and point the
cockpit's ``RECORDER_URL`` at it (the token form then forwards over HTTP).
"""

from __future__ import annotations

import os
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

# Redirect the journal into the git-backed store BEFORE importing web.server (its
# JOURNAL_DB / DEFAULT_LOG read these at import time).
_JDIR = os.environ.get("JOURNAL_DIR", "journal_store")
os.environ.setdefault("JOURNAL_DB", str(Path(_JDIR) / "journal.db"))
os.environ.setdefault("DECISIONS_LOG", str(Path(_JDIR) / "decisions.jsonl"))

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import pandas as pd

import web.server as server
from web.server import app as cockpit             # the cockpit FastAPI app (routes + static)
from deploy import control, gitsync
from feeds import recorder

STATUS: dict = {"started": None, "last_push": None, "last_pull": None,
                "token_restored": False, "errors": [],
                "last_cycle": None, "saved": [], "macro": None, "recorder": "off",
                "scanner": "off", "last_scan": None, "highlights": None}

@asynccontextmanager
async def lifespan(app):
    if os.environ.get("COCKPIT_NO_BG") != "1":     # tests / local skip the live threads
        _start_background()
    yield


# Outer app owns auth + the deploy routes; the cockpit is mounted under it untouched.
app = FastAPI(title="Cockpit (deploy)", lifespan=lifespan)
app.add_middleware(control.BasicAuthMiddleware, user_env="COCKPIT_USER",
                   pass_env="COCKPIT_PASSWORD", open_paths=("/healthz",), realm="cockpit")


def _check(secret: str) -> None:
    want = os.environ.get("RECORDER_TOKEN_SECRET")
    if not want or secret != want:
        raise HTTPException(status_code=403, detail="bad secret")


def _probe_breeze() -> str:
    try:
        from loaders.breeze import get_breeze_client
        get_breeze_client()
        return "connected"
    except Exception as exc:
        return f"token set, but probe failed: {exc}"


_BREEZE_CACHE: dict = {"ts": 0.0, "result": ""}


def _breeze_status(ttl: int = 60, force: bool = False) -> str:
    """Cached `_probe_breeze` — the Breeze handshake is real network, so don't repeat it
    on every poll. `force=True` (after a token update) refreshes immediately."""
    now = time.time()
    if force or not _BREEZE_CACHE["result"] or now - _BREEZE_CACHE["ts"] > ttl:
        _BREEZE_CACHE.update(ts=now, result=_probe_breeze())
    return _BREEZE_CACHE["result"]


def _forward_token(token: str) -> str:
    """Best-effort: hand today's token to a SEPARATE recorder service (the optional
    two-service layout) so its OI accumulation keeps running. Forward over HTTP to its
    `POST /token` (guarded by the shared RECORDER_TOKEN_SECRET). Never raises."""
    base = (os.environ.get("RECORDER_URL") or "").strip().rstrip("/")
    if not base:
        return "not configured — set RECORDER_URL"
    secret = os.environ.get("RECORDER_TOKEN_SECRET", "")
    try:
        body = urllib.parse.urlencode({"token": token, "secret": secret}).encode()
        req = urllib.request.Request(f"{base}/token", data=body, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return "ok" if resp.status == 200 else f"recorder HTTP {resp.status}"
    except Exception as exc:
        return f"forward failed: {exc}"


def _recorder_target(token: str) -> str:
    """Where today's token reaches the OI recorder. Combined deploy: the recorder runs in
    THIS process (same env), so it just picks the token up — report that. Two-service deploy:
    forward over HTTP to the external recorder (``RECORDER_URL``)."""
    if os.environ.get("RECORDER_URL"):
        return _forward_token(token)
    if STATUS.get("recorder") == "running":
        return "in-process (combined service)"
    return "not configured — set RECORDER_URL"


def _persist_token_now() -> None:
    """Eagerly commit+push the token file to the data repo so a restart shortly after the
    POST still restores it (don't wait for the periodic sync). No-op without a data repo."""
    if not os.environ.get("DATA_REPO_URL"):
        return
    try:
        gitsync.commit_push("data", msg="cockpit token update")
    except Exception:
        pass


@app.post("/token")
def set_token(token: str = Form(...), secret: str = Form(...)):
    """Manual fallback to set today's Breeze token (the cockpit normally inherits it from
    the shared data repo). Guarded by RECORDER_TOKEN_SECRET; picked up on the next fetch."""
    _check(secret)
    os.environ["BREEZE_SESSION_TOKEN"] = token.strip()
    control.save_token_file(token)
    return {"ok": True, "breeze": _breeze_status(force=True)}


@app.post("/api/breeze-token")
def set_breeze_token(token: str = Form(...)):
    """In-cockpit token entry (the header 🔑 button) — the one place to refresh the daily
    Breeze token. No secret param: the cockpit is already behind HTTP Basic, so a logged-in
    user is trusted. Applies the token here AND forwards it to the recorder so a single
    entry point feeds both services."""
    token = token.strip()
    os.environ["BREEZE_SESSION_TOKEN"] = token
    control.save_token_file(token)
    _persist_token_now()
    return {"ok": True, "cockpit": _breeze_status(force=True),
            "recorder": _recorder_target(token)}


@app.get("/healthz")
def healthz():
    return JSONResponse(STATUS)


@app.get("/cockpit-status", response_class=HTMLResponse)
def status_page():
    s = STATUS
    err = "<br>".join(s["errors"]) if s["errors"] else "none"
    inp = "width:100%;padding:8px"
    return f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Cockpit status</title>
<body style="font-family:system-ui;max-width:480px;margin:24px auto;padding:0 16px">
<h2>Cockpit service</h2>
<p><a href="/">→ open the cockpit</a></p>
<h3>Set today's Breeze token (fallback)</h3>
<form method=post action=/token>
  <p><label>Breeze session token<br><input name=token style="{inp}"
     placeholder="paste today's token"></label></p>
  <p><label>Secret<br><input name=secret type=password style="{inp}"></label></p>
  <button style="padding:10px 16px">Update token</button>
</form>
<hr><pre>started:        {s['started']}
token restored: {s['token_restored']}
recorder:       {s['recorder']}
last cycle:     {s['last_cycle']}
saved:          {s['saved']}
macro:          {s['macro']}
last push:      {s['last_push']}
errors:         {err}</pre></body>"""


def _push_journal() -> None:
    """Best-effort immediate push after a decision so the track record isn't lost if the
    container restarts before the periodic sync (no-op when JOURNAL_REPO_URL is unset)."""
    if not os.environ.get("JOURNAL_REPO_URL"):
        return
    try:
        if gitsync.commit_push(_JDIR, msg=f"journal {time.strftime('%Y-%m-%d %H:%M')}"):
            STATUS["last_push"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass


def _on_cycle(info: dict) -> None:
    """Surface the recorder loop's live status on /healthz + /cockpit-status."""
    STATUS.update(last_cycle=info["ts"], saved=info["saved"], macro=info["macro"])
    if info.get("errors"):
        STATUS["errors"] = info["errors"]


def _recorder_thread() -> None:
    """Run the OI/macro accumulation loop in-process (same env → reads today's token)."""
    names = os.environ.get("RECORDER_INSTRUMENTS")
    insts = recorder.select_instruments(
        names.split(",") if names else None,
        with_stocks=os.environ.get("RECORDER_STOCKS") == "1")
    recorder.run(insts or None,
                 index_every=int(os.environ.get("INDEX_EVERY_MIN", "15")),
                 stock_every=int(os.environ.get("STOCK_EVERY_MIN", "60")),
                 on_cycle=_on_cycle)


def _scanner_thread() -> None:
    """Screen the NSE-50 option stocks every SCAN_EVERY_MIN during market hours, in-process —
    writes web.server._SCAN, which /api/scanner serves to the cockpit's Scanner panel."""
    every = int(os.environ.get("SCAN_EVERY_MIN", "5"))
    while True:
        try:
            if not recorder.in_session(pd.Timestamp.now(tz=recorder.IST)):
                time.sleep(60)
                continue
            server._run_scan()
            rows = server._SCAN.get("rows") or []
            STATUS.update(scanner="running",
                          last_scan=pd.Timestamp.now(tz=recorder.IST).isoformat(timespec="seconds"),
                          highlights=sum(1 for r in rows if r.get("highlight")))
        except Exception as exc:
            STATUS["scanner"] = f"error: {exc}"
        time.sleep(every * 60)


def _start_background() -> None:
    STATUS["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # Shared data repo = READ-WRITE: this combined service is the SOLE writer (cockpit reads
    # the token + OI store, and the in-process recorder writes fresh OI/macro). One writer →
    # no two-service conflict.
    control.start_repo_sync("data", os.environ.get("DATA_REPO_URL"),
                            every_min=int(os.environ.get("SYNC_EVERY_MIN", "30")),
                            push=True, msg_prefix="data", status=STATUS)
    STATUS["token_restored"] = control.restore_token()
    # Journal repo = READ-WRITE store for the decision log + learning DB.
    Path(_JDIR).mkdir(parents=True, exist_ok=True)
    control.start_repo_sync(_JDIR, os.environ.get("JOURNAL_REPO_URL"),
                            every_min=int(os.environ.get("SYNC_EVERY_MIN", "30")),
                            push=True, msg_prefix="journal", status=STATUS)
    # OI/macro recorder loop — accumulate the live flywheel from the same service.
    threading.Thread(target=_recorder_thread, daemon=True).start()
    STATUS["recorder"] = "running"
    # NSE-50 scanner loop — screen the option stocks for trigger + OI + Claude agreement.
    if os.environ.get("SCAN_STOCKS", "1") != "0":
        threading.Thread(target=_scanner_thread, daemon=True).start()
        STATUS["scanner"] = "running"


server.AFTER_WRITE = _push_journal               # push the journal on each decision

# Mount the cockpit LAST so the outer deploy routes (/token, /healthz, /cockpit-status)
# take precedence; everything else (/, /api/*, /static, /train) falls through to it.
app.mount("/", cockpit)
