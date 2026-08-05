"""Robust env flags — 1/true/yes/on all count (the RECORDER_STOCKS='true' trap)."""

from __future__ import annotations

from feeds.envutil import flag, raw


def test_truthy_spellings(monkeypatch):
    for v in ("1", "true", "True", "TRUE", "yes", "Yes", "on", "y", " 1 "):
        monkeypatch.setenv("X_FLAG", v)
        assert flag("X_FLAG") is True, v


def test_falsy_spellings(monkeypatch):
    for v in ("0", "false", "False", "no", "off", "n", ""):
        monkeypatch.setenv("X_FLAG", v)
        assert flag("X_FLAG") is False, v


def test_unset_and_unrecognised_use_default(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert flag("X_FLAG") is False
    assert flag("X_FLAG", default=True) is True
    monkeypatch.setenv("X_FLAG", "banana")
    assert flag("X_FLAG") is False and flag("X_FLAG", default=True) is True
    assert raw("X_FLAG") == "banana"
