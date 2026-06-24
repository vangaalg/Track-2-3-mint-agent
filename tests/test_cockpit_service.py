"""web.cockpit_service — auth + token endpoint (background threads disabled)."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient


def _client(monkeypatch, password="pw"):
    monkeypatch.setenv("COCKPIT_NO_BG", "1")            # no live threads/clone
    monkeypatch.setenv("RECORDER_TOKEN_SECRET", "s3cret")
    if password is not None:
        monkeypatch.setenv("COCKPIT_USER", "trader")
        monkeypatch.setenv("COCKPIT_PASSWORD", password)
    else:
        monkeypatch.delenv("COCKPIT_PASSWORD", raising=False)
    from web.cockpit_service import app
    return TestClient(app)


def test_healthz_open_without_auth(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/healthz")
    assert r.status_code == 200 and "started" in r.json()


def test_protected_route_401_without_credentials(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/cockpit-status").status_code == 401


def test_protected_route_ok_with_credentials(monkeypatch):
    c = _client(monkeypatch, password="pw")
    r = c.get("/cockpit-status", auth=("trader", "pw"))
    assert r.status_code == 200 and "Cockpit service" in r.text
    # wrong password is rejected
    assert c.get("/cockpit-status", auth=("trader", "nope")).status_code == 401


def test_fail_closed_when_password_unset(monkeypatch):
    c = _client(monkeypatch, password=None)              # COCKPIT_PASSWORD missing
    assert c.get("/cockpit-status").status_code == 503    # never serves unauthed


def test_token_endpoint_sets_env_and_persists(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                          # keep the token file under tmp
    c = _client(monkeypatch)
    bad = c.post("/token", data={"token": "abc", "secret": "wrong"}, auth=("trader", "pw"))
    assert bad.status_code == 403
    ok = c.post("/token", data={"token": "tok999", "secret": "s3cret"}, auth=("trader", "pw"))
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert os.environ["BREEZE_SESSION_TOKEN"] == "tok999"
    from deploy.control import TOKEN_PATH
    assert TOKEN_PATH.read_text() == "tok999"


def test_breeze_token_endpoint_no_secret_sets_env_and_forwards(monkeypatch, tmp_path):
    """The in-cockpit 🔑 form posts just the token (login-gated, no secret): it sets the env,
    persists the file, and forwards to the recorder over HTTP."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RECORDER_URL", "https://recorder.example/")   # trailing slash trimmed
    c = _client(monkeypatch)
    import web.cockpit_service as cs

    monkeypatch.setattr(cs, "_probe_breeze", lambda: "connected")
    posted: dict = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=0):
        posted["url"] = req.full_url
        posted["body"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(cs.urllib.request, "urlopen", _fake_urlopen)

    r = c.post("/api/breeze-token", data={"token": "  tokABC  "}, auth=("trader", "pw"))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["cockpit"] == "connected" and body["recorder"] == "ok"
    assert os.environ["BREEZE_SESSION_TOKEN"] == "tokABC"             # stripped
    from deploy.control import TOKEN_PATH
    assert TOKEN_PATH.read_text() == "tokABC"
    assert posted["url"] == "https://recorder.example/token"          # no double slash
    assert "token=tokABC" in posted["body"] and "secret=s3cret" in posted["body"]


def test_breeze_token_endpoint_recorder_unconfigured(monkeypatch, tmp_path):
    """With RECORDER_URL unset the cockpit still applies the token and says so plainly."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RECORDER_URL", raising=False)
    c = _client(monkeypatch)
    import web.cockpit_service as cs
    monkeypatch.setattr(cs, "_probe_breeze", lambda: "connected")
    r = c.post("/api/breeze-token", data={"token": "tok1"}, auth=("trader", "pw"))
    assert r.status_code == 200
    assert r.json()["recorder"] == "not configured — set RECORDER_URL"
    assert os.environ["BREEZE_SESSION_TOKEN"] == "tok1"


def test_breeze_token_endpoint_requires_auth(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/breeze-token", data={"token": "x"}).status_code == 401


def test_get_breeze_token_prefills_and_reports_connection(monkeypatch, tmp_path):
    """GET returns the last token (full, to prefill the field) + connection state so the
    banner only opens when actually disconnected."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BREEZE_SESSION_TOKEN", "lasttok42")
    c = _client(monkeypatch)
    import web.cockpit_service as cs
    cs._BREEZE_CACHE.update(ts=0.0, result="")              # force a fresh probe

    monkeypatch.setattr(cs, "_probe_breeze", lambda: "connected")
    r = c.get("/api/breeze-token", auth=("trader", "pw"))
    assert r.status_code == 200
    body = r.json()
    assert body["token"] == "lasttok42" and body["connected"] is True

    # a failed probe → not connected (cockpit will re-open the banner)
    cs._BREEZE_CACHE.update(ts=0.0, result="")
    monkeypatch.setattr(cs, "_probe_breeze", lambda: "token set, but probe failed: boom")
    body2 = c.get("/api/breeze-token", auth=("trader", "pw")).json()
    assert body2["connected"] is False and "boom" in body2["status"]


def test_get_breeze_token_requires_auth(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/breeze-token").status_code == 401


def test_breeze_token_recorder_inprocess(monkeypatch, tmp_path):
    """Combined service: no RECORDER_URL, but the in-process recorder is running, so the token
    endpoint reports it's covered locally (not a misconfiguration)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RECORDER_URL", raising=False)
    c = _client(monkeypatch)
    import web.cockpit_service as cs
    monkeypatch.setattr(cs, "_probe_breeze", lambda: "connected")
    monkeypatch.setitem(cs.STATUS, "recorder", "running")     # simulate the live loop
    r = c.post("/api/breeze-token", data={"token": "tokX"}, auth=("trader", "pw"))
    assert r.status_code == 200
    assert r.json()["recorder"] == "in-process (combined service)"
    assert os.environ["BREEZE_SESSION_TOKEN"] == "tokX"


def test_combined_starts_recorder_and_writes_data(monkeypatch):
    """_start_background runs the recorder loop in a thread and makes the data repo a WRITER."""
    monkeypatch.setenv("RECORDER_TOKEN_SECRET", "s3cret")
    monkeypatch.setenv("COCKPIT_PASSWORD", "pw")
    monkeypatch.delenv("DATA_REPO_URL", raising=False)
    monkeypatch.delenv("JOURNAL_REPO_URL", raising=False)
    import web.cockpit_service as cs

    syncs: list = []
    monkeypatch.setattr(cs.control, "start_repo_sync",
                        lambda path, url, **kw: syncs.append((path, kw.get("push"))))
    monkeypatch.setattr(cs.control, "restore_token", lambda: False)
    started: dict = {}
    monkeypatch.setattr(cs.threading, "Thread",
                        lambda target, daemon=False: type("T", (), {"start": lambda self: started.setdefault("t", target)})())

    cs._start_background()
    assert ("data", True) in syncs                            # data repo is now READ-WRITE
    assert started["t"] is cs._recorder_thread                # recorder loop launched
    assert cs.STATUS["recorder"] == "running"


def test_cockpit_routes_reachable_through_mount(monkeypatch):
    """The mounted cockpit is served under the auth-gated outer app."""
    c = _client(monkeypatch)
    # an unknown cockpit path 404s (proves the mount is wired) — but only AFTER auth
    assert c.get("/api/nope").status_code == 401
    assert c.get("/api/nope", auth=("trader", "pw")).status_code == 404


def test_journal_env_redirected_to_store(monkeypatch):
    _client(monkeypatch)
    import web.server as server
    # cockpit_service set the journal paths into the git-backed store before importing server
    assert os.environ["JOURNAL_DB"].endswith("journal.db")
    assert os.environ["DECISIONS_LOG"].endswith("decisions.jsonl")
    assert "journal_store" in os.environ["JOURNAL_DB"]
    assert server.AFTER_WRITE is not None                # decision push hook wired
