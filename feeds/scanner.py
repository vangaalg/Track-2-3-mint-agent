"""NSE-50 multi-stock scanner — find the stock to focus on right now.

Runs the same per-instrument pipeline the cockpit uses, across a basket of stocks, and
flags the ones where everything agrees: a fresh 3-min trigger fires AND the OI bias agrees
with its direction AND Claude says ENTER. The expensive parts (the option-chain pull + the
Claude call) are GATED — they only run for a stock that already has a mechanical trigger, so
a 50-stock scan is mostly cheap local computation.

Pure given injected fns (pull / chain / Claude-read), so it's fully offline-testable.
Reuses: feeds.snapshot.build_snapshot, analysis.triggers.list_triggers, feeds.oi.fetch_oi,
agent.read.claude_read, analysis.trade1._oi_agrees / size_for_confidence.
"""

from __future__ import annotations

import time as _time

from feeds.snapshot import build_snapshot
from feeds.oi import fetch_oi
from analysis.triggers import list_triggers
from analysis.trade1 import _oi_agrees, size_for_confidence
from analysis.proposal import Recommendation, TradeProposal
from indicators.directional import journal_mtf_config

ANCHOR = "9h15min"


def _no_trigger(symbol: str, snap) -> dict:
    return {"symbol": symbol, "spot": round(float(snap.spot), 2) if snap else None,
            "trigger": None, "oi_bias": None, "claude": None,
            "pcr": (getattr(snap, "oi", None) or {}).get("pcr") if snap else None,
            "agree": False, "highlight": False}


def _read_dict(read) -> dict | None:
    """The full Claude read as a plain dict (so the scanner can SHOW the analysis, like an index)."""
    if read is None:
        return None
    from dataclasses import asdict, is_dataclass
    if is_dataclass(read):
        return asdict(read)
    if isinstance(read, dict):
        return read
    return dict(vars(read)) if hasattr(read, "__dict__") else None


def scan_symbol(symbol: str, base, daily, chain_fn, read_fn, cfg=None,
                anchor: str = ANCHOR, lot_size: int = 1, cache=None) -> dict:
    """Scan ONE stock. Cheap path: build the snapshot (no OI) + detect the 3-min trigger.
    Only if a fresh actionable trigger exists do we spend the OI chain pull (``chain_fn``)
    and the Claude read (``read_fn``). Returns a row with ``highlight=True`` on full
    agreement (trigger ∧ OI-bias-agrees ∧ Claude ENTER).

    ``cache`` (a dict keyed by ``(symbol, trigger_ts)``) MEMOISES the expensive OI pull +
    Claude read per distinct trigger, so a still-open trigger scanned every cycle is read
    ONCE — not re-sent to the Claude API every 5 minutes (the token-drain fix)."""
    cfg = cfg or journal_mtf_config()
    snap = build_snapshot(symbol, base, daily, anchor=anchor, mtf_cfg=cfg)   # no OI yet (cheap)
    trigs = list_triggers(snap.feats, snap.frames, cfg=cfg)
    head = trigs[-1] if trigs else None                      # the latest enumerated trigger
    if (head is None or head.get("outcome") != "open"        # only a STILL-OPEN trigger is actionable now
            or head.get("direction") not in ("long", "short")):
        return _no_trigger(symbol, snap)

    direction = head["direction"]
    conf = int(head.get("mtf_confidence") or 0)
    key = (symbol, head["ts"])
    if cache is not None and key in cache:                   # already read THIS trigger — reuse (no API)
        c = cache[key]
        oi_bias, rec, conf_c, full, pcr = (c["oi_bias"], c["recommendation"],
                                           c["confidence"], c["read"], c["pcr"])
    else:
        # first time we see this (symbol, trigger): spend the OI chain pull + ONE Claude read.
        oi = None
        if chain_fn is not None:
            try:
                oi = fetch_oi(symbol, snap.spot, fetch_fn=chain_fn)
            except Exception:
                oi = None
        snap.oi = oi
        prop = TradeProposal(
            instrument=symbol, trade_type="trade1", ts=head["ts"], direction=direction,
            spot=snap.spot, entry=head["entry"], stop=head["eng_stop"], target=head["eng_target"],
            rr_ratio=head.get("eng_rr"), size_lots=size_for_confidence(conf), mtf_confidence=conf,
            rupee_risk=(round(abs(head["entry"] - head["eng_stop"]) * lot_size
                              * size_for_confidence(conf), 2) if head.get("eng_stop") is not None else None),
            recommendation=Recommendation.ENTER,
            context={"chart_read": snap.chart_read, "oi": snap.oi, "levels_source": "engine"})
        read = read_fn(snap, prop) if read_fn is not None else None
        oi_bias = getattr(read, "oi_bias", None) if read is not None else None
        rec = getattr(read, "recommendation", None) if read is not None else None
        conf_c = getattr(read, "confidence", None) if read is not None else None
        full, pcr = _read_dict(read), (oi or {}).get("pcr")
        if cache is not None:
            cache[key] = {"oi_bias": oi_bias, "recommendation": rec, "confidence": conf_c,
                          "read": full, "pcr": pcr}

    agree = bool(rec == "enter" and _oi_agrees(oi_bias, direction))
    return {
        "symbol": symbol, "spot": round(float(snap.spot), 2),
        "trigger": {"direction": direction, "entry": round(head["entry"], 2),
                    "stop": head.get("eng_stop"), "target": head.get("eng_target"),
                    "rr": head.get("eng_rr"), "mtf_confidence": conf, "ts": head["ts"]},
        "oi_bias": oi_bias, "pcr": pcr,
        "claude": ({"recommendation": rec, "confidence": conf_c} if rec is not None else None),
        "claude_full": full,                                 # the 4-part read, so you can READ it
        "agree": agree, "highlight": agree,
    }


def scan_universe(symbols, pull_fn, chain_fn, read_fn, cfg=None,
                  pace_s: float = 0.0, sleep=_time.sleep, cache=None) -> list[dict]:
    """Scan a basket of stocks. Per-symbol try/except (one failure never blocks the rest);
    ``pace_s`` paces the OHLCV pulls (Breeze-friendly). ``cache`` is threaded to ``scan_symbol``
    so each distinct (stock, trigger) is Claude-read once across scans. Highlights first."""
    cfg = cfg or journal_mtf_config()
    rows: list[dict] = []
    for i, sym in enumerate(symbols):
        try:
            base, daily = pull_fn(sym)
            rows.append(scan_symbol(sym, base, daily, chain_fn, read_fn, cfg=cfg, cache=cache))
        except Exception as exc:                              # isolate a bad symbol
            rows.append({"symbol": sym, "error": str(exc), "trigger": None,
                         "oi_bias": None, "claude": None, "agree": False, "highlight": False})
        if pace_s and i < len(symbols) - 1:
            sleep(pace_s)
    rows.sort(key=lambda r: (not r.get("highlight"), r.get("trigger") is None, r.get("symbol") or ""))
    return rows
