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
