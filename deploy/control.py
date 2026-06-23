"""Shared control-plane primitives for the always-on Railway services.

Both the OI/macro recorder (``web.recorder_service``) and the trading cockpit
(``web.cockpit_service``) need the same three things on Railway: persist/restore the
daily Breeze session token across restarts, gate the public URL behind a password, and
keep a git-backed directory in sync (Railway disks are ephemeral). Those primitives live
here so neither service reimplements them.

Lightweight: stdlib + Starlette only, so the recorder container stays cheap to boot.
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
import time
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from deploy import gitsync

# Daily Breeze token persisted under the synced data/ tree (survives restarts). The token
# expires daily and the data repo is private — a deliberate exception to "no secrets in
# the repo" (the trader accepted the tradeoff; see DEPLOY.md).
TOKEN_PATH = Path("data") / "recorder_state" / "breeze_session.txt"


def save_token_file(token: str) -> None:
    """Persist the token under data/ so a restart can restore it (best-effort)."""
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(token.strip())
    except Exception:
        pass


def load_token_file() -> str | None:
    try:
        tok = TOKEN_PATH.read_text().strip()
        return tok or None
    except Exception:
        return None


def restore_token() -> bool:
    """On boot, if no token is in the env, restore the last persisted one. Returns True
    when a token was restored (``loaders.breeze.get_breeze_client`` reads env fresh, so it
    lands on the next fetch)."""
    if os.environ.get("BREEZE_SESSION_TOKEN"):
        return False
    tok = load_token_file()
    if tok:
        os.environ["BREEZE_SESSION_TOKEN"] = tok
        return True
    return False


# --- HTTP Basic auth (over Railway's TLS) ---------------------------------- #
class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind HTTP Basic, with credentials from the env.

    Fail-CLOSED: if ``pass_env`` is unset, every request 503s — so a deploy can never
    accidentally expose the cockpit unauthenticated. ``open_paths`` (e.g. ``/healthz``)
    bypass auth for uptime probes.
    """

    def __init__(self, app, user_env="COCKPIT_USER", pass_env="COCKPIT_PASSWORD",
                 open_paths=("/healthz",), realm="cockpit"):
        super().__init__(app)
        self.user_env, self.pass_env = user_env, pass_env
        self.open_paths, self.realm = tuple(open_paths), realm

    async def dispatch(self, request, call_next):
        if request.url.path in self.open_paths:
            return await call_next(request)
        want_pw = os.environ.get(self.pass_env)
        if not want_pw:
            return Response("auth not configured (set %s)" % self.pass_env, status_code=503)
        want_user = os.environ.get(self.user_env) or "admin"
        if not _auth_ok(request.headers.get("authorization"), want_user, want_pw):
            return Response("authentication required", status_code=401,
                            headers={"WWW-Authenticate": f'Basic realm="{self.realm}"'})
        return await call_next(request)


def _auth_ok(header: str | None, user: str, pw: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode()
        got_user, _, got_pw = raw.partition(":")
    except Exception:
        return False
    return (secrets.compare_digest(got_user, user)
            and secrets.compare_digest(got_pw, pw))


# --- git-backed directory sync ---------------------------------------------- #
def start_repo_sync(path: str, repo_url: str | None, every_min: int = 30,
                    push: bool = True, msg_prefix: str = "sync",
                    status: dict | None = None) -> None:
    """Clone/pull ``repo_url`` into ``path`` on boot, then keep it in sync in a daemon
    thread: ``commit_push`` every ``every_min`` when ``push`` (read-write store), else a
    periodic ``git pull`` (read replica — e.g. the cockpit reading the recorder's data).

    No-op when ``repo_url`` is unset (the service runs without persistence). ``status`` is
    an optional dict updated with the last sync time for the health page.
    """
    if not repo_url:
        return
    try:
        gitsync.clone_or_pull(repo_url, path)
    except Exception as exc:
        if status is not None:
            status.setdefault("errors", []).append(f"clone {path}: {exc}")

    def _loop():
        every = max(1, every_min) * 60
        while True:
            time.sleep(every)
            try:
                if push:
                    if gitsync.commit_push(path, msg=f"{msg_prefix} {time.strftime('%Y-%m-%d %H:%M')}"):
                        if status is not None:
                            status["last_push"] = time.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    gitsync.clone_or_pull(repo_url, path)   # pull-only refresh
                    if status is not None:
                        status["last_pull"] = time.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as exc:
                if status is not None:
                    status["errors"] = [f"{msg_prefix} sync: {exc}"]

    threading.Thread(target=_loop, daemon=True).start()
