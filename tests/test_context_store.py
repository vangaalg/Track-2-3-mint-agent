"""feeds.context_store — the daily GIFT + events overlay (offline)."""

from __future__ import annotations

from feeds import context_store


def test_default_when_absent(tmp_path):
    c = context_store.load_context(root=tmp_path)
    assert c["gift_manual"] is None and c["events_note"] == ""


def test_save_load_and_partial_patch(tmp_path):
    context_store.save_context(gift_manual="24,050", events_note="Fed hiked 25bps", root=tmp_path)
    c = context_store.load_context(root=tmp_path)
    assert c["gift_manual"] == 24050.0 and c["events_note"] == "Fed hiked 25bps" and c["set_at"]
    # patch only the events note → gift persists
    context_store.save_context(events_note="new note", root=tmp_path)
    c = context_store.load_context(root=tmp_path)
    assert c["gift_manual"] == 24050.0 and c["events_note"] == "new note"
