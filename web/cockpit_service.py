"""Always-on host for the trading COCKPIT (Railway) — the live UI, authed + persisted.

A thin deployable wrapper around ``web.server.app`` (the cockpit). It adds the three
deploy concerns that the local ``uvicorn web.server:app`` doesn't need:

1. **Auth** — HTTP Basic over Railway's TLS (``COCKPIT_USER`` / ``COCKPIT_PASSWORD``),
   fail-closed so the cockpit is never exposed unauthenticated. The cockpit makes Breeze
   pulls + Claude calls, so an open URL would leak positions and spend credits.
2. **Daily Breeze token** — auto-restored from the *shared* private data repo
   (``DATA_REPO_URL``, the same one the recorder pushes its token to), so you POST the
   token once (to the recorder) and the cockpit picks it up; a ``/token`` page is the
   manual fallback. The cockpit only PULLS the data repo (read replica) → no conflict.
3. **Journal persistence** — the decision log + learning DB are redirected (via the
   ``JOURNAL_DB`` / ``DECISIONS_LOG`` env, honored by ``web.server``) into ``journal_store/``,
   a clone of a SEPARATE private repo (``JOURNAL_REPO_URL``) pushed periodically + after
   each decision, so the track-record + Claude's memory survive redeploys.

The cockpit app is **mounted** under a fresh outer app (not mutated in place) so importing
this module never alters ``web.server.app`` for the rest of the test suite. Deploy as a
SECOND Railway service with a custom start command (``uvicorn web.cockpit_service:app``) —
see DEPLOY.md. Set ``COCKPIT_NO_BG=1`` to skip the background threads (tests / local).
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Redirect the journal into the git-backed store BEFORE importing web.server (its
# JOURNAL_DB / DEFAULT_LOG read these at import time).
_JDIR = os.environ.get("JOURNAL_DIR", "journal_store")
os.environ.setdefault("JOURNAL_DB", str(Path(_JDIR) / "journal.db"))
os.environ.setdefault("DECISIONS_LOG", str(Path(_JDIR) / "decisions.jsonl"))

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import web.server as server
from web.server import app as cockpit             # the cockpit FastAPI app (routes + static)
from deploy import control, gitsync

STATUS: dict = {"started": None, "last_push": None, "last_pull": None,
                "token_restored": False, "errors": []}

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


@app.post("/token")
def set_token(token: str = Form(...), secret: str = Form(...)):
    """Manual fallback to set today's Breeze token (the cockpit normally inherits it from
    the shared data repo). Guarded by RECORDER_TOKEN_SECRET; picked up on the next fetch."""
    _check(secret)
    os.environ["BREEZE_SESSION_TOKEN"] = token.strip()
    control.save_token_file(token)
    return {"ok": True, "breeze": _probe_breeze()}


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
last pull:      {s['last_pull']}
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


def _start_background() -> None:
    STATUS["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # Shared data repo = READ replica (token + OI store); never pushed (no recorder conflict).
    control.start_repo_sync("data", os.environ.get("DATA_REPO_URL"),
                            every_min=int(os.environ.get("DATA_PULL_MIN", "10")),
                            push=False, msg_prefix="data", status=STATUS)
    STATUS["token_restored"] = control.restore_token()
    # Journal repo = READ-WRITE store for the decision log + learning DB.
    Path(_JDIR).mkdir(parents=True, exist_ok=True)
    control.start_repo_sync(_JDIR, os.environ.get("JOURNAL_REPO_URL"),
                            every_min=int(os.environ.get("SYNC_EVERY_MIN", "30")),
                            push=True, msg_prefix="journal", status=STATUS)


server.AFTER_WRITE = _push_journal               # push the journal on each decision

# Mount the cockpit LAST so the outer deploy routes (/token, /healthz, /cockpit-status)
# take precedence; everything else (/, /api/*, /static, /train) falls through to it.
app.mount("/", cockpit)
