"""Instrument registry — per-instrument config + band resolution (offline)."""

from __future__ import annotations

from feeds.instruments import (
    INSTRUMENTS, get_instrument, instrument_list, offsets_for, DEFAULT_INSTRUMENT)


def test_registry_has_nifty_and_banknifty():
    assert get_instrument("NIFTY")["lot_size"] == 65
    assert get_instrument("NIFTY")["band"] == [37.0, 72.0]
    bn = get_instrument("BANKNIFTY")
    assert bn["lot_size"] == 30 and bn["loader_symbol"] == "CNXBAN"
    assert bn["monthly"] is True and bn["band"] == "scale"


def test_get_instrument_is_case_insensitive_and_defaults():
    assert get_instrument("banknifty")["loader_symbol"] == "CNXBAN"
    assert get_instrument(None) is INSTRUMENTS[DEFAULT_INSTRUMENT]
    assert get_instrument("UNKNOWN") is INSTRUMENTS[DEFAULT_INSTRUMENT]   # fallback NIFTY


def test_offsets_fixed_for_nifty_scaled_for_banknifty():
    assert offsets_for(get_instrument("NIFTY"), 24000) == [37.0, 72.0]
    bn = offsets_for(get_instrument("BANKNIFTY"), 52000)      # price-scaled from NIFTY 24000
    assert bn[0] > 72 and bn[1] > bn[0]                       # ~80/156, scales up with price


def test_instrument_list_is_primary_only():
    lst = instrument_list()
    assert {"id": "NIFTY", "label": "NIFTY"} in lst
    assert any(i["id"] == "BANKNIFTY" and i["label"] == "Bank Nifty" for i in lst)
    assert all(i["id"] in ("NIFTY", "BANKNIFTY") for i in lst)   # stocks NOT in the dropdown


def test_nse50_stocks_registered_for_scanner():
    from feeds.instruments import scanner_symbols
    syms = scanner_symbols()
    assert len(syms) == 50 and "RELIANCE" in syms and "M&M" in syms
    r = get_instrument("RELIANCE")                  # resolvable (points-based: lot 1, monthly, scaled)
    assert r["lot_size"] == 1 and r["monthly"] is True and r["band"] == "scale"
    assert r["primary"] is False
