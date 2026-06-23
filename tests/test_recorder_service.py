"""web.recorder_service — token endpoint + status (background threads disabled)."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.setenv("RECORDER_NO_BG", "1")          # don't spawn live threads/clone
    monkeypatch.setenv("RECORDER_TOKEN_SECRET", "s3cret")
    from web.recorder_service import app
    return TestClient(app)


def test_token_requires_secret(monkeypatch):
    with _client(monkeypatch) as c:
        r = c.post("/token", data={"token": "abc", "secret": "wrong"})
        assert r.status_code == 403


def test_token_sets_env_and_status_renders(monkeypatch):
    with _client(monkeypatch) as c:
        r = c.post("/token", data={"token": "tok123", "secret": "s3cret"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert os.environ["BREEZE_SESSION_TOKEN"] == "tok123"   # picked up next fetch
        assert "breeze" in r.json()                              # probe result present
        assert c.get("/healthz").status_code == 200
        assert "recorder" in c.get("/").text.lower()            # mobile form page


def test_token_persists_to_data_repo(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                          # data/ lands under tmp
    monkeypatch.delenv("DATA_REPO_URL", raising=False)   # no repo → skip the immediate push
    with _client(monkeypatch) as c:
        r = c.post("/token", data={"token": "tok-persist", "secret": "s3cret"})
        assert r.status_code == 200 and r.json()["ok"] is True
        # the token is written under the synced data/ tree (restored on the next boot)
        f = tmp_path / "data" / "recorder_state" / "breeze_session.txt"
        assert f.exists() and f.read_text().strip() == "tok-persist"


def test_restore_token_from_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BREEZE_SESSION_TOKEN", raising=False)
    import web.recorder_service as svc
    # no file yet → nothing to restore
    assert svc._restore_token() is False
    # write a persisted token, clear the env, and restore it (the boot path)
    svc._save_token_file("restored-tok")
    monkeypatch.delenv("BREEZE_SESSION_TOKEN", raising=False)
    assert svc._restore_token() is True
    assert os.environ["BREEZE_SESSION_TOKEN"] == "restored-tok"
    # with a token already in the env, restore is a no-op
    assert svc._restore_token() is False


def test_context_endpoint_saves_overlay(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                          # data/context.json under tmp
    with _client(monkeypatch) as c:
        assert c.post("/context", data={"secret": "wrong", "gift": "24000"}).status_code == 403
        r = c.post("/context", data={"secret": "s3cret", "gift": "24,050", "events": "Fed hiked"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["context"]["gift_manual"] == 24050.0
        assert (tmp_path / "data" / "context.json").exists()
