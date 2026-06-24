"""FastAPI backend for the web cockpit.

Thin JSON layer over the engine with server-side TTL caches so the frontend can
poll every ~15s cheaply (heavy intraday pull ~60s, option chain + macro ~5min).
The live dependencies (loader, chain/macro fetchers, Claude completer) are module
globals so tests can inject mocks and run fully offline.
"""

from __future__ import annotations

import json
import os
import time
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from loaders import get_loader
from indicators.directional import (
    journal_mtf_config, cpr_st_mtf_config, orb_mtf_config)
from feeds.snapshot import build_snapshot, build_snapshot_at
from feeds.breeze_oi import make_chain_fetcher
from feeds.oi import chain_table, summarise_chain
from feeds.td_macro import make_quote_fn, SCORECARD_SYMBOLS
from feeds.macro import fetch_macro
from feeds import oi_store, oi_summary_store, scanner
from feeds.instruments import (
    INSTRUMENTS, get_instrument, instrument_list, offsets_for, DEFAULT_INSTRUMENT,
    scanner_symbols)
from analysis.trade1 import (
    propose_trade1, apply_strike, apply_oi_boost, size_for_confidence, LOT_SIZE)
from analysis.cpr_st import propose_cpr_st
from analysis.orb import propose_orb
from analysis.condor import propose_condor, list_condor_triggers
from analysis.strike import select_strike
from analysis.triggers import replay_today, list_triggers, simulate_intraday
from analysis.proposal import Recommendation, TradeProposal
from agent.memory import load_decisions, distill_memory, distill_context
from agent.read import claude_read
from agent.reason import explain_outcome
from agent.chat import spar_turn
from execution import breeze_exec
from journal.log import log_decision, DEFAULT_LOG
from journal.outcomes import (
    settle_log, settle_store, matrix_summary, conviction_breakdown, grade_training,
    manual_exit_outcome, _matrix)
from journal import store

ANCHOR = "9h15min"
EXPIRY_WEEKDAY = 1
VIZ_POINTS = 1000
PULL_TTL = 60          # chart/snapshot re-pull cadence (s)
OI_TTL = 300           # option chain + macro cadence (s)
LOG_OI = True          # persist each fresh chain to feeds.oi_store (the flywheel)
DEFAULT_SIZE = 1       # flat lot count for non-conviction paths (condor); directional tabs
                       # recompute via size_for_confidence (1-2 lots)
# THE active resolver: the trader's journal 3-min strategy (trio + 2-close confirm,
# trigger-only — HTF is trend context, not a gate). Live cockpit + training both use it.
RESOLVER_CFG = journal_mtf_config()
# Multi-strategy registry — the 4 alert streams on the cockpit. The three directional
# streams (3-min, CPR-ST, ORB) are OI-automated: Claude auto-reads + the OI-confluence
# sizing boost auto-applies after a trigger. The condor is non-directional / propose-only.
# Execution stays propose-only on all but the 3-min (the trader places the legs).
STRATEGIES = [
    {"id": "trade1", "label": "3-min", "cfg": RESOLVER_CFG, "kind": "directional"},
    {"id": "cpr_st", "label": "CPR-ST", "cfg": cpr_st_mtf_config(), "kind": "directional"},
    {"id": "orb", "label": "ORB", "cfg": orb_mtf_config(), "kind": "directional"},
    {"id": "condor", "label": "Expiry", "cfg": None, "kind": "nondirectional"},
]
_STRAT = {s["id"]: s for s in STRATEGIES}
# Journal paths honor env overrides so a deploy wrapper (web.cockpit_service) can point
# them at a git-backed dir that persists across redeploys; defaults unchanged otherwise.
JOURNAL_DB = os.environ.get("JOURNAL_DB", store.DB_PATH)   # full-context SQLite store
DEFAULT_LOG = os.environ.get("DECISIONS_LOG", DEFAULT_LOG)  # append-only decision log
OI_SUMMARY_ROOT = os.environ.get("OI_SUMMARY_ROOT")        # recorder's PCR/OI time series (None=default)
OI_ROOT = os.environ.get("OI_ROOT")                        # recorder's full per-strike chain snapshots (None=default)
# NSE-50 scanner: pace between stock pulls; SCAN_SYMBOLS optionally limits/overrides the basket.
SCAN_PACE_S = float(os.environ.get("SCAN_PACE_S", "0.3"))
SCAN_SYMBOLS = [s.strip().upper() for s in os.environ.get("SCAN_SYMBOLS", "").split(",") if s.strip()]
AFTER_WRITE = None   # optional hook: deploy wrapper sets it to push the journal repo
_STATIC = Path(__file__).parent / "static"

# --- injectable seams (overridden in tests) -------------------------------- #
def _default_pull(symbol: str):
    loader = get_loader("breeze")
    ls = get_instrument(symbol)["loader_symbol"]      # NIFTY / CNXBAN (Bank Nifty) / …
    base_min = loader.load(ls, "minute", start=date.today() - timedelta(days=3),
                           use_cache=False)
    daily = loader.load(ls, "day", start=date.today() - timedelta(days=800),
                        use_cache=False)
    return base_min, daily


def _default_chain(symbol: str):
    inst = get_instrument(symbol)                     # per-instrument expiry (monthly for Bank Nifty)
    fetch = make_chain_fetcher(weekday=inst["weekday"], exchange=inst["exchange"],
                               monthly=inst["monthly"])
    return fetch(inst["loader_symbol"])


def _default_macro(symbol: str):
    return fetch_macro(SCORECARD_SYMBOLS, make_quote_fn(), errors=[])


def _default_train_pull(symbol: str, days: int):
    """~`days` of 3-min base + the long daily history, for trigger reconstruction."""
    loader = get_loader("breeze")
    base_min = loader.load(symbol, "minute", start=date.today() - timedelta(days=days + 2),
                           use_cache=False)
    daily = loader.load(symbol, "day", start=date.today() - timedelta(days=800),
                        use_cache=False)
    return base_min, daily


PULL_FN = _default_pull
CHAIN_FN = _default_chain
MACRO_FN = _default_macro
TRAIN_PULL_FN = _default_train_pull
READ_COMPLETER = None    # claude_read completer (None -> live Anthropic call)
CHAT_COMPLETER = None     # spar_turn completer (None -> live Anthropic call)
REASON_COMPLETER = None   # explain_outcome completer (None -> live Anthropic call)

# --- in-process state (single local user) ---------------------------------- #
# Gated trigger queue: each strategy pins its oldest still-open, un-actioned trigger as
# the "head" (frozen entry/stop/target) until the trader approves/rejects it; resolved
# triggers auto-expire out of the head. ``queues`` caches the per-strategy replay,
# ``heads`` the current actionable trigger, ``actioned`` the durable decided set, and
# ``reads`` Claude's read cached per (strategy, ts) so it fires once per trigger.
# Per-instrument cockpit state: each symbol keeps its own snap/chain/queues/heads/
# exits/etc. ``_active`` selects the instrument for the current request (set by each
# endpoint), so the helpers resolve the right state via ``_st()`` with no symbol arg.
_states: dict[str, dict] = {}
_active: ContextVar = ContextVar("active_symbol", default=DEFAULT_INSTRUMENT)


def _new_state() -> dict:
    return {
        "snap": None, "prop": None, "chain": None,
        "snap_at": 0.0, "oi_at": 0.0,
        "read": None, "analysed_bar": None,
        "chat": [],
        "queues": {}, "heads": {}, "actioned": {}, "reads": {},
        # manual exits the trader took off the triggers table: overlay + store-row ids
        "exits": {}, "records": {},
        # one-position-at-a-time: the open APPROVED trade per strategy (auto-flattened on a new one)
        "position": {},
    }


def _st(symbol: str | None = None) -> dict:
    """The per-instrument state dict (lazily created), for the active instrument."""
    sym = (symbol or _active.get()).upper()
    s = _states.get(sym)
    if s is None:
        s = _states[sym] = _new_state()
    return s


# back-compat: the default NIFTY state object (tests mutate it in place via .update)
_state = _st(DEFAULT_INSTRUMENT)

# NSE-50 scanner cache: the latest scan rows (shared by the bg thread + the on-demand refresh).
_SCAN: dict = {"at": 0.0, "rows": [], "scanning": False, "error": None}

# --- training-mode state (separate from the live cockpit) ------------------- #
TRAIN_TTL = 900          # re-pull the 7-day history every ~15 min
TRAIN_LOTS = 2           # training trades are always sized at 2 lots
OI_MAX_AGE_MIN = 180     # reject an as-of OI snapshot older than this (same-session only)
_train: dict = {"symbol": None, "base": None, "daily": None, "frame3m": None,
                "triggers": None, "at": 0.0, "cases": {}}

app = FastAPI(title="Nifty Agent cockpit")
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


# --- engine glue ----------------------------------------------------------- #
def _refresh(symbol: str, size: int) -> None:
    """Re-pull on TTL: chain/macro every OI_TTL, snapshot every PULL_TTL."""
    _active.set(symbol.upper())
    inst = get_instrument(symbol)
    now = time.time()
    if _st()["chain"] is None or now - _st()["oi_at"] > OI_TTL:
        try:
            _st()["chain"] = CHAIN_FN(symbol)
        except Exception as exc:
            _st()["chain"] = None
            _st()["chain_err"] = str(exc)
        _st()["macro"] = MACRO_FN(symbol)
        _st()["oi_at"] = now

    if _st()["snap"] is None or now - _st()["snap_at"] > PULL_TTL:
        base_min, daily = PULL_FN(symbol)
        chain = _st()["chain"]
        snap = build_snapshot(
            symbol, base_min, daily, anchor=ANCHOR, mtf_cfg=RESOLVER_CFG,
            oi_fetch_fn=(lambda i: chain) if chain is not None else None,
            macro=_st().get("macro"),
        )
        if snap.oi is None and _st().get("chain_err"):
            snap.notes.append(f"oi: {_st()['chain_err']}")
        _st()["snap"] = snap
        table = (chain_table(chain, snap.spot, window=VIZ_POINTS)
                 if chain is not None and not chain.empty else None)

        def _strike(p):
            """LIVE strike agent: pick the ITM vehicle off the live chain (least theta)."""
            if table is not None and p.direction in ("long", "short"):
                apply_strike(p, select_strike(table, snap.spot, p.direction))
            return p

        prop = _strike(propose_trade1(snap, size))     # strike now; OI boost in _run_head_read
        props = {
            "trade1": prop,
            # directional streams: strike now, Claude read + OI boost auto-applied on the head
            "cpr_st": _strike(propose_cpr_st(snap, size_lots=size)),
            "orb": _strike(propose_orb(snap, size_lots=size)),
            "condor": propose_condor(snap, table, expiry_weekday=inst["weekday"]),
        }
        for p in props.values():     # ₹ risk uses the instrument's lot size (NIFTY 65 / BankNifty 30)
            _scale_rupee(p, inst["lot_size"])
        _st()["prop"] = prop          # back-compat: chat/payload reference Trade-1
        _st()["props"] = props
        _st()["table"] = table
        _st()["snap_at"] = now
        # log the chain snapshot (the OI flywheel) once per fresh OI bucket
        if LOG_OI and chain is not None and not chain.empty \
                and _st().get("oi_logged_at") != _st()["oi_at"]:
            try:
                oi_store.save_chain(symbol, snap.ts, snap.spot, chain)
                _st()["oi_logged_at"] = _st()["oi_at"]
            except Exception:
                pass
        _load_persisted_exits(symbol)   # restore manual exits from the durable store (survive restart)
        for s in STRATEGIES:        # cache today's triggers per strategy (throttled)
            _st()["queues"][s["id"]] = _apply_exits(
                s["id"], _strategy_queue(s["id"], snap, size, lot_size=inst["lot_size"]))


def _scale_rupee(prop, lot_size: int) -> None:
    """Re-base a proposal's ₹ risk onto the instrument's lot size (propose_* bake in the
    module LOT_SIZE = NIFTY 65; Bank Nifty etc. override it). No-op for NIFTY."""
    if prop is not None and getattr(prop, "rupee_risk", None) and lot_size != LOT_SIZE:
        prop.rupee_risk = round(prop.rupee_risk * lot_size / LOT_SIZE, 2)


def _lot_size_for(snap) -> int:
    """The active instrument's contract lot size (from the snapshot's instrument)."""
    return get_instrument(getattr(snap, "instrument", None))["lot_size"]


def _strategy_queue(sid: str, snap, size: int, session_date=None, lot_size: int | None = None) -> dict:
    """One session's discrete triggers for a strategy (replay_today / condor proxy).
    ``session_date`` browses a previous day (default = the latest session); ``lot_size``
    scales the ₹ column to the instrument (NIFTY 65 / Bank Nifty 30)."""
    meta = _STRAT[sid]
    lot = lot_size or _lot_size_for(snap)
    if meta["kind"] == "nondirectional":
        return _condor_today(snap, size, session_date=session_date, lot_size=lot)
    # one_position: a strategy holds ONE directional position — each trigger flattens the
    # prior at the next trigger's entry (no long+short open at once), per the trader's rule.
    return replay_today(snap.feats, snap.frames, cfg=meta["cfg"], size_lots=size,
                        lot_size=lot, session_date=session_date, one_position=True)


def _session_dates(snap) -> list[str]:
    """Distinct session dates in the live frame (NEWEST first — for the date toggle)."""
    f = snap.frames.get("3min") if snap is not None else None
    if f is None or f.empty:
        return []
    return [str(d) for d in sorted({ts.date() for ts in f.index}, reverse=True)]


def _rows_summary(rows: list[dict]) -> dict:
    """Footer over an arbitrary set of trigger rows (used by the merged 'all' view)."""
    wins = sum(1 for r in rows if r.get("outcome") == "win")
    losses = sum(1 for r in rows if r.get("outcome") == "loss")
    return {"n": len(rows), "wins": wins, "losses": losses,
            "open": sum(1 for r in rows if r.get("outcome") == "open"),
            "exited": sum(1 for r in rows if r.get("outcome") == "exit"),
            "net_points": round(sum(r.get("points", 0) or 0 for r in rows), 2),
            "net_rupees": round(sum(r.get("rupees", 0) or 0 for r in rows), 0),
            "hit_rate": (round(wins / (wins + losses), 2) if (wins + losses) else None)}


def _apply_exits(sid: str, queue: dict) -> dict:
    """Overlay the trader's MANUAL exits onto the replay rows (the replay itself stays a pure
    measurement) and recompute the footer, so a closed trade shows on the table as `exit`."""
    exits = _st().get("exits") or {}
    rows = queue.get("triggers") or []
    if not exits or not rows:
        return queue
    touched = False
    for r in rows:
        ex = exits.get((sid, r.get("ts")))
        if ex:
            r.update(outcome="exit", points=ex["points"], rupees=ex["rupees"],
                     exit=ex["exit"], exit_ts=ex["exit_ts"])
            touched = True
    if touched:
        wins = sum(1 for r in rows if r.get("outcome") == "win")
        losses = sum(1 for r in rows if r.get("outcome") == "loss")
        s = dict(queue.get("summary") or {})
        s.update(wins=wins, losses=losses,
                 open=sum(1 for r in rows if r.get("outcome") == "open"),
                 exited=sum(1 for r in rows if r.get("outcome") == "exit"),
                 net_points=round(sum(r.get("points", 0) or 0 for r in rows), 2),
                 net_rupees=round(sum(r.get("rupees", 0) or 0 for r in rows), 0),
                 hit_rate=(round(wins / (wins + losses), 2) if (wins + losses) else None))
        queue["summary"] = s
    return queue


def _load_persisted_exits(symbol: str) -> None:
    """Rebuild the manual-exit overlay from the DURABLE store so exits survive a restart.

    `/api/exit` writes each close to the SQLite store; the in-memory overlay is wiped on a
    redeploy. Re-seed `exits` (+ the store-row id / actioned flag) for this instrument from
    the saved manual-exit outcomes, WITHOUT clobbering an exit taken this session."""
    st = _st()
    try:
        recs = store.load_records(JOURNAL_DB, kind="live", symbol=symbol)
    except Exception:
        return
    for r in recs:
        o = r.get("outcome") or {}
        if not o.get("manual"):
            continue
        prop = r.get("proposal") or {}
        # key on the TRIGGER ts (proposal.ts) — the row id the table uses; the record's
        # ``ts`` column is the live snapshot bar, not the trigger.
        key = (prop.get("trade_type") or "trade1", prop.get("ts") or r.get("ts"))
        if key[1] is None or key in st["exits"]:
            continue
        st["exits"][key] = {k: o.get(k) for k in ("exit", "exit_ts", "points", "rupees")}
        st["records"].setdefault(key, r.get("id"))
        st["actioned"].setdefault(key, "approved")


def _head_for(sid: str, queue: dict) -> dict | None:
    """The actionable HEAD trigger: the oldest still-OPEN, un-actioned trigger for the
    strategy. Resolved (win/loss) triggers auto-expire out of the head (kept in history).
    The condor (non-directional, propose-only — its proxy rows are always win/loss) uses
    the LIVE gate-open proposal as its head instead."""
    actioned = _st()["actioned"]
    if _STRAT[sid]["kind"] == "nondirectional":
        prop = (_st().get("props") or {}).get("condor")
        if prop is None or prop.recommendation is not Recommendation.ENTER:
            return None
        if (sid, prop.ts) in actioned:
            return None
        return {"ts": prop.ts, "direction": "flat", "condor": True,
                "entry": prop.entry, "stop": prop.stop, "target": prop.target,
                "rr": prop.rr_ratio, "mtf_confidence": 0, "outcome": "open"}
    for t in queue.get("triggers", []):
        if (sid, t["ts"]) in actioned:
            continue
        if t.get("outcome") != "open":          # auto-expire resolved triggers
            continue
        return t
    return None


def _recompute_heads() -> None:
    """Re-select each strategy's HEAD from the cached queue (cheap — runs every poll) so a
    just-actioned trigger advances to the next open one immediately, and auto-run Claude
    ONCE per new head (all four tabs). Heads are deterministic from the trigger list minus
    the actioned set, so the decision card stays put until the trader approves/rejects."""
    for s in STRATEGIES:
        sid = s["id"]
        old = _st()["heads"].get(sid)
        head = _head_for(sid, _st()["queues"].get(sid, {}))
        _st()["heads"][sid] = head
        if head is not None and (old or {}).get("ts") != head["ts"]:
            key = (sid, head["ts"])
            if key not in _st()["reads"]:
                try:
                    _run_head_read(sid, head)
                except Exception:
                    pass                          # never let a Claude error break the poll


def _head_out(sid: str, h: dict | None) -> dict | None:
    """Serialise a head for the card: the frozen trigger + its cached Claude read, with
    Claude's clamped target/stop/R:R overlaid when present (else the engine levels stand).
    Copies the head — never mutates `_st()["heads"]` or the replay queue."""
    if h is None:
        return None
    cached = _st()["reads"].get((sid, h["ts"])) or {}
    out = {**h, "read": cached or None, "levels_source": "engine"}
    if cached.get("claude_target") is not None:
        out.update(target=cached["claude_target"], stop=cached["claude_stop"],
                   rr=cached["claude_rr"], levels_source="claude")
    return out


def _payload(symbol: str) -> dict:
    snap, prop, chain = _st()["snap"], _st()["prop"], _st()["chain"]
    props = _st().get("props") or {"trade1": prop}
    rows = []
    if chain is not None and not chain.empty:
        t = chain_table(chain, snap.spot, window=VIZ_POINTS)
        rows = json.loads(t.to_json(orient="records"))   # NaN -> null
    read = snap.chart_read
    return {
        "ts": snap.ts, "spot": snap.spot, "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "chart": {"mtf_call": read.get("mtf_call"), "regime": read.get("regime_45_daily"),
                  "mtf_confidence": read.get("mtf_confidence"),
                  "mtf_confidence_breakdown": read.get("mtf_confidence_breakdown", {}),
                  "numbers": read.get("numbers", {}), "levels": read.get("levels", {})},
        "oi": snap.oi, "macro": snap.macro, "notes": snap.notes,
        "chain": rows,
        "proposal": prop.as_dict(),                                  # back-compat (Trade-1)
        "proposals": {sid: p.as_dict() for sid, p in props.items()},  # all 4 strategy streams
        # the GATED decision card per tab: the frozen actionable trigger + its cached
        # Claude read (None = "watching, no active trigger"). Stable across polls. When Claude
        # set the levels, overlay them so the card shows Claude's target/stop/R:R (not engine's).
        "heads": {sid: _head_out(sid, h) for sid, h in _st().get("heads", {}).items()},
        "strategies": [{"id": s["id"], "label": s["label"], "kind": s["kind"]}
                       for s in STRATEGIES],
        # multi-instrument selector: the available instruments + the active one
        "instruments": instrument_list(), "symbol": symbol.upper(),
    }


def _learning_memory() -> str:
    """Combine the fast JSONL tally with the rich store's past-reasoning-vs-outcome
    block — the same memory the live read and the training read both learn from."""
    memory = distill_memory(load_decisions(DEFAULT_LOG))
    try:
        memory += "\n" + distill_context(store.load_records(JOURNAL_DB))
    except Exception:
        pass
    return memory


def _reason_why(ctx: dict) -> dict | None:
    """Claude's post-outcome reason-why for a resolved trigger (None if it errors)."""
    try:
        rw = explain_outcome(ctx, _learning_memory(), completer=REASON_COMPLETER)
        return asdict(rw)
    except Exception:
        return None


def _reason_text(rw: dict | None) -> str | None:
    """Compact scalar string for the store/queries from a reason-why dict."""
    if not rw:
        return None
    return f"{rw.get('trigger_quality', '?')}: {rw.get('why', '')} " \
           f"(lesson: {rw.get('lesson', '')})".strip()


def _settle_reasons(settled: list[dict]) -> None:
    """Generate Claude's reason-why ONCE for each newly-resolved live trade that
    lacks one, and patch it onto the store row (so it feeds the learning memory)."""
    for r in settled or []:
        if (r.get("outcome_status") in ("win", "loss") and not r.get("reason_why")
                and r.get("id")):
            prop = r.get("proposal") or {}
            rw = _reason_why({
                "instrument": r.get("symbol"), "ts": r.get("ts"), "direction": r.get("direction"),
                "entry": r.get("entry"), "stop": r.get("stop"), "target": r.get("target"),
                "action": r.get("decision"), "trigger_label": r.get("trigger_label"),
                "outcome": r.get("outcome"),
                "chart_read": (prop.get("context") or {}).get("chart_read"),
            })
            if rw:
                store.update_reason(r["id"], _reason_text(rw), path=JOURNAL_DB)


def _proposal_from_head(sid: str, head: dict, snap, table) -> TradeProposal:
    """Build a real ``TradeProposal`` from a FROZEN head trigger so log_decision /
    save_decision (which need the dataclass) work unchanged. Levels are frozen from the
    trigger; the option vehicle is picked off the LIVE chain (you fill it now)."""
    inst = get_instrument(getattr(snap, "instrument", None))
    if head.get("condor"):
        prop = propose_condor(snap, table, expiry_weekday=inst["weekday"])
        _scale_rupee(prop, inst["lot_size"])
        return prop
    direction = head["direction"]
    conf = int(head.get("mtf_confidence") or 0)
    lots = size_for_confidence(conf)
    entry = head.get("entry")
    cached = _st()["reads"].get((sid, head["ts"])) or {}
    # Claude OWNS the target/stop (sanity-railed in _run_head_read); fall back to the engine's
    # structural levels when Claude stood down / proposed nothing usable.
    if cached.get("claude_target") is not None:
        stop, target, rr = cached["claude_stop"], cached["claude_target"], cached["claude_rr"]
        levels_source = "claude"
    else:
        stop, target, rr = head.get("stop"), head.get("target"), head.get("rr")
        levels_source = "engine"
    rupee_risk = (round(abs(entry - stop) * inst["lot_size"] * lots, 2)
                  if entry is not None and stop is not None else None)
    prop = TradeProposal(
        instrument=snap.instrument, trade_type=sid, ts=head["ts"], direction=direction,
        spot=snap.spot, entry=entry, stop=stop, target=target,
        rr_ratio=rr, size_lots=lots, mtf_confidence=conf, rupee_risk=rupee_risk,
        recommendation=Recommendation.ENTER,
        context={"chart_read": snap.chart_read, "oi": snap.oi, "macro": snap.macro,
                 "levels_source": levels_source},
    )
    if table is not None and direction in ("long", "short"):
        apply_strike(prop, select_strike(table, snap.spot, direction))
    if direction in ("long", "short"):   # auto OI-confluence nudge on every directional tab
        apply_oi_boost(prop, cached.get("oi_bias"))
    return prop


def _run_head_read(sid: str, head: dict) -> dict:
    """Run Claude once for a strategy's head trigger and cache it by (sid, ts). The OI
    confluence sizing boost auto-applies on every DIRECTIONAL tab (trade1/cpr_st/orb);
    the condor is non-directional so it gets no boost."""
    snap = _st()["snap"]
    memory = _learning_memory()
    _st()["memory"] = memory
    prop = _proposal_from_head(sid, head, snap, _st().get("table"))
    read = claude_read(snap, prop, memory, completer=READ_COMPLETER)
    if getattr(prop, "direction", None) in ("long", "short"):   # auto OI boost, all directional tabs
        apply_oi_boost(prop, getattr(read, "oi_bias", None))
    cached = asdict(read)
    # Claude DECIDES the levels on a directional trigger — sanity-rail them (correct side +
    # 2%-of-price stop cap), but NO R:R floor (min_rr=0): R:R is whatever Claude chose. Unusable
    # / stand-down levels clamp to None → _proposal_from_head falls back to the engine levels.
    if head.get("direction") in ("long", "short") and head.get("entry") is not None:
        from scoring.backtest import clamp_levels
        tgt, stp, rr = clamp_levels(head["direction"], head["entry"],
                                    read.proposed_target, read.proposed_stop, min_rr=0.0)
        cached["claude_target"], cached["claude_stop"], cached["claude_rr"] = tgt, stp, rr
    _st()["reads"][(sid, head["ts"])] = cached
    _st()["read"] = read              # back-compat for _save_context_for / chat
    return cached


def _run_read(sid: str = "trade1") -> dict:
    """Manual 're-analyse' for a tab's current head (used by /api/analyse)."""
    head = _st()["heads"].get(sid)
    if head is None:
        raise HTTPException(status_code=409, detail="no active trigger to analyse")
    return _run_head_read(sid, head)


# --- routes ---------------------------------------------------------------- #
@app.get("/")
def index():
    f = _STATIC / "index.html"
    return FileResponse(f) if f.exists() else JSONResponse({"status": "cockpit"})


@app.get("/api/snapshot")
def snapshot(symbol: str = "NIFTY", size: int = DEFAULT_SIZE):
    try:
        _refresh(symbol, size)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"data pull failed: {exc}")
    _recompute_heads()      # advance/auto-analyse the gated head every poll (not just on a new bar)
    return _payload(symbol)


def _jf(x):
    import math
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def _serialize_chart(frame, bars: int) -> dict:
    """Candlestick + indicator overlays for one TF frame (shared by the route + store)."""
    from indicators.engine import compute_indicators
    feats = compute_indicators(frame)
    f = feats.tail(bars)
    out = []
    for t, r in f.iterrows():
        out.append({
            "t": t.isoformat(), "o": _jf(r["open"]), "h": _jf(r["high"]),
            "l": _jf(r["low"]), "c": _jf(r["close"]),
            "bb_u": _jf(r.get("bb_upper")), "bb_m": _jf(r.get("bb_mid")), "bb_l": _jf(r.get("bb_lower")),
            "ema5": _jf(r.get("ema_5")), "ema45": _jf(r.get("ema_45")),
            "ema100": _jf(r.get("ema_100")), "ema200": _jf(r.get("ema_200")),
            "st": _jf(r.get("supertrend")), "st_dir": int(r.get("st_dir", 0)),
            "rsi": _jf(r.get("rsi_14")), "macd": _jf(r.get("macd")),
            "signal": _jf(r.get("macd_signal")), "hist": _jf(r.get("macd_hist")),
        })
    last = feats.iloc[-1]
    cpr = {"pivot": _jf(last.get("cpr_pivot")), "tc": _jf(last.get("cpr_tc")),
           "bc": _jf(last.get("cpr_bc"))}
    return {"bars": out, "cpr": cpr}


def _daily_cpr(snap) -> dict | None:
    """Today's CPR from the DAILY frame — the same level CPR broadcasts onto every TF.

    Sourcing it from the daily series (which always carries a prior session) avoids the
    NaN a shallow single-session intraday frame would give, so the chart's CPR lines
    always render.
    """
    from indicators.engine import compute_indicators
    daily = getattr(snap, "frames", {}).get("1day")
    if daily is None or daily.empty:
        return None
    last = compute_indicators(daily).iloc[-1]
    cpr = {"pivot": _jf(last.get("cpr_pivot")), "tc": _jf(last.get("cpr_tc")),
           "bc": _jf(last.get("cpr_bc"))}
    return cpr if cpr["pivot"] is not None else None


def _chart_bundle(snap, tfs=("3min", "15min", "60min", "1day"), bars: int = 60) -> dict:
    """Compact multi-TF chart datapoints saved with each decision (Training-Mode fuel)."""
    out, daily_cpr = {}, _daily_cpr(snap)
    for tf in tfs:
        frame = snap.frames.get(tf)
        if frame is not None and not frame.empty:
            try:
                bundle = _serialize_chart(frame, bars)
                if daily_cpr is not None:
                    bundle["cpr"] = daily_cpr
                out[tf] = bundle
            except Exception:
                pass
    return out


@app.get("/api/chart")
def chart(tf: str = "3min", bars: int = 200, symbol: str = "NIFTY"):
    """Candlestick + indicator overlays for the price panel (computed per TF)."""
    _active.set(symbol.upper())
    if _st()["snap"] is None:
        raise HTTPException(status_code=409, detail="no snapshot yet")
    snap = _st()["snap"]
    frame = snap.frames.get(tf)
    if frame is None or frame.empty:
        raise HTTPException(status_code=404, detail=f"no frame for tf {tf!r}")
    data = _serialize_chart(frame, bars)
    return {"tf": tf, "symbol": symbol.upper(), "bars": data["bars"],
            "cpr": _daily_cpr(snap) or data["cpr"]}


@app.get("/api/oi-history")
def oi_history(symbol: str = "NIFTY", day: str | None = None):
    """The recorder's PCR / max-pain / wall+band time series for one instrument — for the
    cockpit's PCR-over-time line graph + table. ``day`` (a YYYY-MM-DD) filters to one recorded
    session; omitted / "all" returns the full accumulated history. Empty when the recorder
    hasn't run yet (the time series lives on the trader's open-network machine)."""
    df = oi_summary_store.load_summary(symbol.upper(), root=OI_SUMMARY_ROOT)
    if df is None or df.empty:
        return {"symbol": symbol.upper(), "rows": [], "days": []}
    days = sorted({str(t)[:10] for t in df.index}, reverse=True)   # recorded sessions, newest-first
    if day and day != "all":
        df = df[[str(t)[:10] == day for t in df.index]]
    rows = json.loads(df.reset_index().to_json(orient="records"))  # NaN -> null
    return {"symbol": symbol.upper(), "rows": rows, "days": days}


def _chain_history_df(sym: str, day: str | None):
    """Concatenate the recorder's per-strike chain snapshots for ``sym`` (optionally one
    ``day``) into a single frame: ts·spot·strike·call/put OI+LTP, one row per strike/cycle."""
    base = OI_ROOT or oi_store.DATA_DIR
    snaps = oi_store.list_snapshots(sym, base=base)
    if day and day != "all":
        snaps = [(t, f) for t, f in snaps if str(t.date()) == day]
    frames = []
    for _, f in snaps:
        try:
            frames.append(pd.read_parquet(f))
        except Exception:
            continue
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    cols = [c for c in ("ts", "spot", "strike", "call_oi", "put_oi", "call_ltp", "put_ltp")
            if c in df.columns] + [c for c in df.columns
                                   if c not in ("ts", "spot", "strike", "call_oi", "put_oi",
                                                "call_ltp", "put_ltp")]
    sort_by = [c for c in ("ts", "strike") if c in df.columns]
    return (df[cols].sort_values(sort_by) if sort_by else df[cols])


@app.get("/api/oi-download")
def oi_download(symbol: str = "NIFTY", day: str | None = None, kind: str = "summary"):
    """Download the recorder's saved data as CSV (opens in Excel), date-wise. ``kind=summary``
    = the PCR/max-pain/walls/bands series (one row per cycle); ``kind=chain`` = the full
    per-strike option chain at each cycle. ``day`` (YYYY-MM-DD) follows the cockpit's Day picker
    (a single session, or omitted/"all" for the whole history)."""
    sym = symbol.upper()
    if kind == "chain":
        df = _chain_history_df(sym, day)
        fname = f"{sym}_chain_{day or 'all'}.csv"
    else:
        df = oi_summary_store.load_summary(sym, root=OI_SUMMARY_ROOT)
        if df is not None and day and day != "all":
            df = df[[str(t)[:10] == day for t in df.index]]
        if df is not None:
            df = df.reset_index()
        fname = f"{sym}_oi_summary_{day or 'all'}.csv"
    csv = df.to_csv(index=False) if df is not None and not df.empty else ""
    return Response(content=csv, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# --- NSE-50 scanner: screen all option stocks for trigger + OI + Claude agreement -------- #
def _scan_read_fn(snap, prop):
    """Claude read for one scanned stock (the live memory + completer)."""
    return claude_read(snap, prop, _learning_memory(), completer=READ_COMPLETER)


def _run_scan() -> dict:
    """Run ONE scan over the stock universe into ``_SCAN``. Mechanical 3-min triggers are
    cheap; the OI chain pull + Claude read fire only on a stock that actually triggered."""
    if _SCAN.get("scanning"):
        return _SCAN
    _SCAN["scanning"] = True
    try:
        syms = SCAN_SYMBOLS or scanner_symbols()
        rows = scanner.scan_universe(syms, PULL_FN, CHAIN_FN, _scan_read_fn,
                                     cfg=RESOLVER_CFG, pace_s=SCAN_PACE_S)
        _SCAN.update(rows=rows, at=time.time(), error=None)
    except Exception as exc:
        _SCAN["error"] = str(exc)
    finally:
        _SCAN["scanning"] = False
    return _SCAN


def _scan_payload() -> dict:
    rows = _SCAN.get("rows") or []
    return {"rows": rows, "count": len(rows),
            "highlights": sum(1 for r in rows if r.get("highlight")),
            "triggers": sum(1 for r in rows if r.get("trigger")),
            "at": _SCAN.get("at"), "scanning": _SCAN.get("scanning", False),
            "error": _SCAN.get("error")}


@app.get("/api/scanner")
def scanner_get():
    """The latest NSE-50 scan — rows (highlights first) where each stock's 3-min trigger,
    OI bias and Claude verdict are surfaced; ``highlight`` = full agreement (focus on this)."""
    return _scan_payload()


@app.post("/api/scanner/refresh")
def scanner_refresh():
    """Re-scan the universe now (the manual kick; the bg thread scans every ~5 min live).
    Runs inline — for ~50 stocks this takes a few seconds of paced pulls."""
    _run_scan()
    return _scan_payload()


@app.get("/api/record")
def record(symbol: str = "NIFTY"):
    """Settle the decision log against today's bars and return the 2x2 track record FOR THE
    SELECTED INSTRUMENT.

    The track record is summarised from the durable SQLite STORE (not the JSONL log): the
    store is git-synced + survives Railway restarts, and the trader's manual /api/exit
    closes write their realized outcome there — the ephemeral JSONL would read 0. Scoped to
    ``symbol`` and settled against THAT instrument's bars (Bank Nifty trades must not settle
    against NIFTY)."""
    _active.set(symbol.upper())
    sym = symbol.upper()
    frames = _st()["snap"].frames if _st()["snap"] is not None else {}
    settle_log(DEFAULT_LOG, frames)        # keep the local JSONL settled (back-compat)
    settled = []
    try:
        settled = settle_store(frames, path=JOURNAL_DB, symbol=sym)   # grade this instrument (same 2x2)
        _settle_reasons(settled)                            # post-mortem newly-resolved trades
    except Exception:
        pass
    recent = settled[-12:]
    posts = []     # settled live trades that carry a Claude post-mortem
    try:
        for r in store.load_records(JOURNAL_DB, kind="live", limit=20, symbol=sym):
            if r.get("reason_why"):
                posts.append({"ts": r.get("ts"), "direction": r.get("direction"),
                              "label": r.get("trigger_label"), "matrix": r.get("matrix"),
                              "outcome": r.get("outcome_status"), "reason_why": r.get("reason_why")})
    except Exception:
        pass
    return {"summary": matrix_summary(settled),
            "by_conviction": conviction_breakdown(settled),   # win-rate by conviction bucket
            "posts": posts[-8:],
            "recent": [{"decision": r.get("decision"), "process": r.get("process_grade"),
                        "matrix": r.get("matrix"), "ts": r.get("ts"),
                        "direction": r.get("direction"),
                        # conviction = engine (mtf+OI, 0-5); confidence = Claude's read (1-5)
                        "conviction": r.get("final_confidence"),
                        "confidence": r.get("confidence"),
                        "outcome": r.get("outcome")} for r in recent]}


def _condor_today(snap, size: int, session_date=None, lot_size: int | None = None) -> dict:
    """One session's expiry-day condor setups in a replay_today-compatible shape."""
    inst = get_instrument(getattr(snap, "instrument", None))
    lot = lot_size or inst["lot_size"]
    trigs = list_condor_triggers(snap.feats.get("3min"), snap.frames.get("3min"),
                                 expiry_weekday=inst["weekday"], size_lots=size, lot_size=lot)
    if session_date is not None:
        today = str(pd.Timestamp(session_date).date())
    else:
        today = str(snap.frames["3min"].index[-1].date()) if "3min" in snap.frames else None
    trigs = [t for t in trigs if t["date"] == today]
    wins = sum(1 for t in trigs if t["outcome"] == "win")
    losses = sum(1 for t in trigs if t["outcome"] == "loss")
    net = round(sum(t["points"] for t in trigs), 2)
    return {"session": today, "triggers": trigs, "last": trigs[-1] if trigs else None,
            "summary": {"n": len(trigs), "wins": wins, "losses": losses, "open": 0,
                        "net_points": net, "net_rupees": round(net * lot * size, 0),
                        "hit_rate": round(wins / (wins + losses), 2) if (wins + losses) else None}}


@app.get("/api/triggers")
def triggers(size: int = DEFAULT_SIZE, strategy: str = "trade1", date: str | None = None,
             symbol: str = "NIFTY"):
    """Today's (or ``date``'s) triggers. ``strategy="all"`` merges every DIRECTIONAL
    strategy into one table (each row tagged with its strategy); the condor — a different,
    non-directional instrument — is browsed on its own tab. ``date`` browses a prior session
    from the multi-day live pull; ``dates`` lists what's available (NEWEST first) for the toggle.

    replay_today sizes each row by its own conviction (1-2 lot band), so the ₹ column matches
    the proposal; the condor (no conviction) keeps the flat size."""
    _active.set(symbol.upper())
    snap = _st()["snap"]
    if snap is None:
        raise HTTPException(status_code=409, detail="no snapshot yet")
    if strategy != "all" and strategy not in _STRAT:
        raise HTTPException(status_code=404, detail=f"unknown strategy {strategy!r}")
    dates = _session_dates(snap)
    sd = date or (dates[0] if dates else None)         # dates are newest-first → [0] = latest
    strat_list = [{"id": s["id"], "label": s["label"]} for s in STRATEGIES]
    if strategy == "all":
        merged: list[dict] = []
        for s in STRATEGIES:
            if s["kind"] == "nondirectional":
                continue                       # condor has its own shape/tab
            q = _apply_exits(s["id"], _strategy_queue(s["id"], snap, size, session_date=sd))
            for r in q.get("triggers", []):
                merged.append({**r, "strategy": s["id"], "strategy_label": s["label"]})
        merged.sort(key=lambda r: r.get("ts") or "")
        return {"session": sd, "triggers": merged, "last": merged[-1] if merged else None,
                "summary": _rows_summary(merged), "dates": dates, "strategy": "all",
                "strategies": strat_list}
    q = _apply_exits(strategy, _strategy_queue(strategy, snap, size, session_date=sd))
    meta = _STRAT[strategy]
    for r in q.get("triggers", []):
        r["strategy"], r["strategy_label"] = strategy, meta["label"]
    q.update(dates=dates, strategy=strategy, strategies=strat_list)
    return q


@app.post("/api/exit")
def exit_trade(strategy: str = Form("trade1"), ts: str = Form(...),
               exit_px: float | None = Form(None), symbol: str = Form("NIFTY")):
    """Manually CLOSE/record ANY directional trigger at ``exit_px`` (default = the live spot):
    record the realized P&L as a trade the trader actually took + closed (feeds the journal
    track-record) and overlay the triggers table — overriding that row's hypothetical replay
    outcome. Works on any row (open OR replay-resolved), any date. Propose-only — you square
    off on your own broker."""
    _active.set(symbol.upper())
    if strategy not in _STRAT:
        raise HTTPException(status_code=404, detail=f"unknown strategy {strategy!r}")
    snap = _st()["snap"]
    if snap is None:
        raise HTTPException(status_code=409, detail="no snapshot yet")
    key = (strategy, ts)
    if key in _st()["exits"]:
        raise HTTPException(status_code=409, detail="trade already exited")
    lot = get_instrument(symbol)["lot_size"]
    q = (_st()["queues"].get(strategy)
         or _strategy_queue(strategy, snap, DEFAULT_SIZE, lot_size=lot))
    # the row may be on a prior session not in the cached queue → search any session's date
    trig = next((t for t in q.get("triggers", []) if t.get("ts") == ts), None)
    if trig is None and ts[:10] != (q.get("session") or ""):
        alt = _strategy_queue(strategy, snap, DEFAULT_SIZE, session_date=ts[:10], lot_size=lot)
        trig = next((t for t in alt.get("triggers", []) if t.get("ts") == ts), None)
    if trig is None or trig.get("direction") not in ("long", "short"):
        raise HTTPException(status_code=409, detail="no directional trade at that trigger")
    px = float(exit_px) if exit_px is not None else snap.spot
    outcome = _record_exit(strategy, trig, px, symbol, lot, auto=False)
    return {"ok": True, "outcome": outcome}


def _record_exit(strategy: str, trig: dict, px: float, symbol: str, lot: int,
                 auto: bool = False) -> dict:
    """Record a close of ``trig`` at ``px`` (a manual exit OR an auto-flatten-on-reversal):
    persist to the journal store + overlay the triggers table, and clear the strategy's open
    position if this was it. Returns the outcome dict (``auto`` flags the reversal close)."""
    key = (strategy, trig["ts"])
    exit_ts = datetime.now().isoformat(timespec="seconds")
    outcome = manual_exit_outcome(trig, px, exit_ts, lot_size=lot, auto=auto)
    # persist: reuse the card-approved store row if it exists, else log the taken trade now
    prop = _proposal_from_head(strategy, trig, _st()["snap"], _st().get("table"))
    rid = _st()["records"].get(key)
    if rid is None:
        status = "auto" if auto else "manual"
        log_decision(prop, "approved", execution={"status": status})
        rid = _save_context_for(prop, "approved", symbol, {"status": status})
        _st()["records"][key] = rid
        _st()["actioned"][key] = "approved"
    if rid is not None:
        store.update_outcome(rid, outcome, "good", _matrix("good", outcome["status"]),
                             path=JOURNAL_DB)
    if AFTER_WRITE:
        try:
            AFTER_WRITE()
        except Exception:
            pass
    _st()["exits"][key] = {k: outcome[k] for k in ("exit", "exit_ts", "points", "rupees")}
    if (_st()["position"].get(strategy) or {}).get("ts") == trig["ts"]:
        _st()["position"].pop(strategy, None)        # that position is now flat
    return outcome


def _auto_flatten(strategy: str, symbol: str) -> dict | None:
    """One position at a time: close the strategy's currently-open approved trade at the live
    spot before a NEW trade in that strategy opens (opposite = reverse, same = re-enter)."""
    pos = _st()["position"].get(strategy)
    snap = _st()["snap"]
    if not pos or snap is None:
        return None
    if (strategy, pos["ts"]) in _st()["exits"]:       # already closed
        _st()["position"].pop(strategy, None)
        return None
    lot = get_instrument(symbol)["lot_size"]
    out = _record_exit(strategy, pos, snap.spot, symbol, lot, auto=True)
    return {"ts": pos["ts"], "direction": pos.get("direction"),
            "points": out["points"], "exit": out["exit"]}


@app.post("/api/analyse")
def analyse(strategy: str = "trade1", symbol: str = "NIFTY"):
    _active.set(symbol.upper())
    if _st()["snap"] is None:
        raise HTTPException(status_code=409, detail="no snapshot yet")
    if strategy not in _STRAT:
        raise HTTPException(status_code=404, detail=f"unknown strategy {strategy!r}")
    try:
        return _run_read(strategy)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"claude unavailable: {exc}")


@app.post("/api/chat")
async def chat(text: str = Form(""), files: list[UploadFile] = File(default=[]),
               symbol: str = Form("NIFTY")):
    _active.set(symbol.upper())
    if _st()["snap"] is None:
        raise HTTPException(status_code=409, detail="no snapshot yet")
    import base64
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    for f in files:
        data = await f.read()
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": f.content_type or "image/png",
            "data": base64.standard_b64encode(data).decode()}})
    _st()["chat"].append({"role": "user", "content": blocks if files else text})
    try:
        reply = spar_turn(_st()["chat"], _st()["snap"], _st()["prop"],
                          _st().get("memory", ""), completer=CHAT_COMPLETER)
    except Exception as exc:
        reply = f"(chat unavailable: {exc})"
    _st()["chat"].append({"role": "assistant", "content": reply})
    return {"reply": reply}


def _save_context_for(prop, decision: str, symbol: str, execution: dict | None,
                      label: str | None = None) -> None:
    """Archive the WHOLE decision moment to the SQLite store (chat, Claude read,
    chart datapoints, raw chain, all macro) so the agent can learn from everything.
    ``prop`` is the FROZEN proposal being decided (the queue head), not the live bar;
    the surrounding context (chain/chart/macro) is still sourced from the live snapshot."""
    snap, chain = _st()["snap"], _st()["chain"]
    chain_rows = None
    if chain is not None and not chain.empty:
        try:
            chain_rows = json.loads(chain_table(chain, snap.spot, window=VIZ_POINTS).to_json(orient="records"))
        except Exception:
            chain_rows = None
    read = _st().get("read")
    payload = {
        "ts": snap.ts, "symbol": symbol, "decision": decision, "spot": snap.spot,
        "proposal": prop.as_dict(),
        "trigger_label": (label or "").strip().lower() or None,
        "claude_read": asdict(read) if read is not None else None,
        "chat": _st().get("chat") or None,
        "chart": _chart_bundle(snap),
        "chain": chain_rows,
        "macro": snap.macro, "oi_summary": snap.oi, "notes": snap.notes,
        "execution": execution,
    }
    rid = None
    try:
        rid = store.save_decision(payload, path=JOURNAL_DB)
    except Exception:
        rid = None
    if AFTER_WRITE:                     # e.g. push the git-backed journal (deploy only)
        try:
            AFTER_WRITE()
        except Exception:
            pass
    return rid


def _advance_head(strategy: str) -> dict | None:
    """Re-select the strategy's HEAD from its cached queue (cheap, NO Claude) after the
    current one was actioned, and return the serialised next head so the client can swap
    the card instantly — without waiting on the next snapshot poll's synchronous read."""
    nh = _head_for(strategy, _st()["queues"].get(strategy, {}))
    _st()["heads"][strategy] = nh
    return _head_out(strategy, nh)


@app.post("/api/decision")
def decision(action: str = Form(...), live: bool = Form(False), symbol: str = Form("NIFTY"),
             label: str = Form(""), strategy: str = Form("trade1"), ts: str = Form("")):
    """Approve/reject/skip the FROZEN queue HEAD for ``strategy`` (identified by ``ts``).
    Acts on the trigger the trader actually saw — not a moved-on live bar — and marks it
    actioned so the next open trigger surfaces (returned as ``next_head`` so the card
    advances instantly). ``skip`` records NOTHING (the track record stays clean — only a
    deliberate ``reject`` is a logged stand-down). Execution stays Trade-1 only."""
    _active.set(symbol.upper())
    if strategy not in _STRAT:
        raise HTTPException(status_code=404, detail=f"unknown strategy {strategy!r}")
    if action not in ("approve", "reject", "skip"):
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    head = _st()["heads"].get(strategy)
    if head is None:
        raise HTTPException(status_code=409, detail="no active trigger to decide")
    if ts and ts != head["ts"]:                       # stale click — the trigger moved on
        raise HTTPException(status_code=409, detail="trigger moved on — refresh and re-read")
    key = (strategy, head["ts"])
    if key in _st()["actioned"]:
        raise HTTPException(status_code=409, detail="trigger already actioned")
    if action == "skip":                              # silent — no journal, no execution
        _st()["actioned"][key] = "skipped"
        return {"status": "skipped", "next_head": _advance_head(strategy)}
    prop = _proposal_from_head(strategy, head, _st()["snap"], _st().get("table"))
    if action == "approve":
        # one position at a time: auto-exit the strategy's prior open trade before this opens
        flat = _auto_flatten(strategy, symbol)
        result = breeze_exec.place(prop, live=live) if strategy == "trade1" else \
            {"status": "logged (propose-only)"}
        rec = log_decision(prop, "approved", execution=result)
        _st()["records"][key] = _save_context_for(prop, "approved", symbol, result, label=label)
        _st()["actioned"][key] = "approved"
        _st()["position"][strategy] = dict(head)       # the new open position for this strategy
        return {"status": result.get("status"), "logged": rec["decision"],
                "auto_exit": flat, "next_head": _advance_head(strategy)}
    rec = log_decision(prop, "rejected")
    _save_context_for(prop, "rejected", symbol, None, label=label)
    _st()["actioned"][key] = "rejected"
    return {"status": "rejected", "logged": rec["decision"],
            "next_head": _advance_head(strategy)}


# --- training mode (replay past 3-min triggers, label them, back-train) ----- #
@app.get("/train")
def train_index():
    f = _STATIC / "train.html"
    return FileResponse(f) if f.exists() else JSONResponse({"status": "training cockpit"})


def _train_refresh(symbol: str, days: int) -> None:
    """Pull ~`days` of history once and enumerate every Trade-1 trigger across it."""
    now = time.time()
    if (_train["triggers"] is None or _train["symbol"] != symbol
            or now - _train["at"] > TRAIN_TTL):
        base, daily = TRAIN_PULL_FN(symbol, days)
        snap = build_snapshot(symbol, base, daily, anchor=ANCHOR, mtf_cfg=RESOLVER_CFG, macro={})
        _train.update(symbol=symbol, base=base, daily=daily,
                      frame3m=snap.frames["3min"],
                      triggers=list_triggers(snap.feats, snap.frames, cfg=RESOLVER_CFG),
                      at=now, cases={})


def _answered_ts() -> set:
    """Timestamps already answered in training (so the game stops re-asking them)."""
    return {r.get("ts") for r in store.load_records(JOURNAL_DB, kind="training")}


@app.get("/api/train/triggers")
def train_triggers(symbol: str = "NIFTY", days: int = 8):
    """List every past 3-min trigger (NO levels/outcome — that's the game)."""
    try:
        _train_refresh(symbol, days)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"training pull failed: {exc}")
    trigs = _train["triggers"] or []
    answered = _answered_ts()
    return {"symbol": symbol, "days": days, "n": len(trigs),
            "triggers": [{"tid": t["tid"], "ts": t["ts"], "date": t["date"],
                          "direction": t["direction"], "answered": t["ts"] in answered}
                         for t in trigs]}


def _train_case(tid: int) -> dict:
    """Build (and cache) the as-of world for one trigger: snapshot + OI + Claude read."""
    case = _train["cases"].get(tid)
    if case is not None:
        return case
    trig = _train["triggers"][tid]
    snap = build_snapshot_at(_train["symbol"], _train["base"], _train["daily"],
                             trig["ts"], anchor=ANCHOR, mtf_cfg=RESOLVER_CFG, macro={})
    # only a same-session snapshot counts as "as-of" (no stale/cross-day OI)
    chain = oi_store.load_nearest(_train["symbol"], trig["ts"], max_age_min=OI_MAX_AGE_MIN)
    oi_summary, oi_as_of, oi_age_min = None, None, None
    if chain is not None and not chain.empty:
        oi_summary = summarise_chain(chain, snap.spot)
        try:
            snap_ts = pd.Timestamp(chain["ts"].iloc[0])
            oi_as_of = snap_ts.isoformat()
            oi_age_min = round((pd.Timestamp(trig["ts"]) - snap_ts).total_seconds() / 60)
        except Exception:
            pass
    snap.oi = oi_summary
    prop = propose_trade1(snap, DEFAULT_SIZE)
    read, read_err = None, None
    try:
        read = claude_read(snap, prop, _learning_memory(), completer=READ_COMPLETER)
    except Exception as exc:
        read_err = str(exc)
    case = {"snap": snap, "prop": prop, "chain": chain, "oi": oi_summary,
            "oi_as_of": oi_as_of, "oi_age_min": oi_age_min,
            "read": read, "read_err": read_err}
    _train["cases"][tid] = case
    return case


def _chain_rows(chain, spot):
    if chain is None or chain.empty:
        return None
    try:
        return json.loads(chain_table(chain, spot, window=VIZ_POINTS).to_json(orient="records"))
    except Exception:
        return None


@app.get("/api/train/case/{tid}")
def train_case(tid: int, tf: str = "3min", bars: int = 200):
    """The as-of moment for trigger `tid`: chart + OI + Claude's read. NO outcome."""
    if _train["triggers"] is None:
        raise HTTPException(status_code=409, detail="no trigger list yet")
    if tid < 0 or tid >= len(_train["triggers"]):
        raise HTTPException(status_code=404, detail="unknown trigger id")
    try:
        case = _train_case(tid)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"case build failed: {exc}")
    trig, snap = _train["triggers"][tid], _train["cases"][tid]["snap"]
    frame = snap.frames.get(tf)
    chart = (_serialize_chart(frame, bars) if frame is not None and not frame.empty
             else {"bars": [], "cpr": {}})
    read = snap.chart_read
    return {
        "tid": tid, "ts": trig["ts"], "date": trig["date"],
        "direction": trig["direction"], "entry": trig["entry"], "spot": snap.spot,
        "tf": tf, "bars": chart["bars"], "cpr": chart["cpr"],
        "mtf_confidence": read.get("mtf_confidence"),
        "mtf_confidence_breakdown": read.get("mtf_confidence_breakdown", {}),
        "oi": case["oi"], "chain": _chain_rows(case["chain"], snap.spot),
        "oi_as_of": case.get("oi_as_of"), "oi_age_min": case.get("oi_age_min"),
        "macro_available": False,
        "read": asdict(case["read"]) if case["read"] else None,
        "read_err": case["read_err"],
    }


def _rupees(points: float) -> float:
    """Training P&L is always sized at 2 lots."""
    return round(points * LOT_SIZE * TRAIN_LOTS, 0)


def _rr(direction: str, entry: float, stop: float, target: float) -> float | None:
    risk, reward = abs(entry - stop), abs(target - entry)
    return round(reward / risk, 2) if risk else None


@app.post("/api/train/answer")
def train_answer(tid: int = Form(...), action: str = Form(...),
                 entry: float = Form(0.0), target: float = Form(0.0),
                 stop: float = Form(0.0), reason: str = Form(""),
                 label: str = Form("")):
    """Grade the trader's take/skip vs the known outcome, save it, feed learning."""
    if _train["triggers"] is None or tid < 0 or tid >= len(_train["triggers"]):
        raise HTTPException(status_code=404, detail="unknown trigger id")
    case = _train["cases"].get(tid)
    if case is None:
        raise HTTPException(status_code=409, detail="open the case first")
    trig = _train["triggers"][tid]
    direction, frame3m = trig["direction"], _train["frame3m"]
    entry = entry or trig["entry"]          # trader may edit the fill; default the trigger
    action = action.lower()
    rr = None

    if action == "take":
        ok = (target > entry > stop) if direction == "long" else (target < entry < stop)
        if not ok:
            raise HTTPException(status_code=400,
                                detail="target/stop must straddle entry on the trade's side")
        outcome, exit_px, points = simulate_intraday(frame3m, trig["ts"], direction,
                                                      entry, stop, target)
        your_levels = {"entry": entry, "stop": stop, "target": target}
        rr = _rr(direction, entry, stop, target)
    else:  # skip — would-be result with the engine's own levels
        outcome, exit_px, points = simulate_intraday(frame3m, trig["ts"], direction,
                                                      trig["entry"], trig["eng_stop"], trig["eng_target"])
        your_levels = None

    your = {"status": outcome, "exit": exit_px, "points": points, "rupees": _rupees(points)}
    engine = {"status": trig["outcome"], "points": trig["points"],
              "rupees": _rupees(trig["points"]),     # re-size the engine result to 2 lots
              "stop": trig["eng_stop"], "target": trig["eng_target"],
              "rr": _rr(direction, trig["entry"], trig["eng_stop"], trig["eng_target"])}
    cell = grade_training(action, outcome)

    snap, read = case["snap"], case["read"]
    # Claude's call graded on the same 2x2 (ENTER=take / STAND_DOWN=skip, engine levels)
    claude_eval, agree, round_winner = None, None, None
    if read is not None:
        c_action = "take" if read.recommendation == "enter" else "skip"
        c_cell = grade_training(c_action, engine["status"])
        claude_eval = {"action": c_action, "status": engine["status"],
                       "points": engine["points"], "rupees": engine["rupees"], "cell": c_cell}
        agree = (c_action == action)
        round_winner = _round_winner(cell, c_cell)

    label = (label or "").strip().lower() or None     # trader's genuine/false trigger label

    # Claude's post-outcome reason-why (process not P&L) on the trader's executed levels.
    lv = your_levels or {"entry": trig["entry"], "stop": trig["eng_stop"], "target": trig["eng_target"]}
    reason_why = _reason_why({
        "instrument": _train["symbol"], "ts": trig["ts"], "direction": direction,
        "entry": lv["entry"], "stop": lv["stop"], "target": lv["target"],
        "action": action, "trigger_label": label, "outcome": your,
        "chart_read": getattr(snap, "chart_read", None),
    })

    proposal = case["prop"].as_dict()
    proposal = {**proposal, "direction": direction, "size_lots": TRAIN_LOTS,
                "reason": reason.strip() or None,
                "claude_eval": claude_eval, "agree": agree,
                "claude_reason": reason_why}
    if action == "take":
        proposal.update(entry=entry, stop=stop, target=target, rr_ratio=rr)
    payload = {
        "kind": "training", "ts": trig["ts"], "symbol": _train["symbol"],
        "decision": f"training_{action}", "spot": snap.spot,
        "proposal": proposal,
        "claude_read": asdict(read) if read is not None else None,
        "chart": _chart_bundle(snap), "chain": _chain_rows(case["chain"], snap.spot),
        "macro": None, "oi_summary": case["oi"], "notes": snap.notes,
        "outcome": your, "matrix": cell, "process_grade": f"training_{action}",
        "trigger_label": label, "reason_why": _reason_text(reason_why),
    }
    try:
        store.save_decision(payload, path=JOURNAL_DB)
    except Exception:
        pass
    return {"action": action, "direction": direction, "entry": entry, "rr": rr,
            "reason": reason.strip() or None, "label": label, "your_levels": your_levels,
            "your_outcome": your, "engine_outcome": engine, "cell": cell,
            "agree": agree, "round_winner": round_winner, "reason_why": reason_why,
            "claude": asdict(read) if read is not None else None,
            "score": _train_score(), "record": _train_record()}


_CORRECT = {"deserved", "avoided"}     # took a winner / skipped a loser
_WRONG = {"accept", "missed"}          # took a loser / skipped a winner


def _round_winner(you_cell: str, claude_cell: str) -> str:
    yc, cc = you_cell in _CORRECT, claude_cell in _CORRECT
    if yc and not cc:
        return "you"
    if cc and not yc:
        return "claude"
    return "tie"


def _tally(items: list[dict]) -> dict:
    """Per-side training tally; realized P&L counts taken trades only."""
    from collections import Counter
    takes = [i for i in items if i["action"] == "take"]
    correct = sum(1 for i in items if i["cell"] in _CORRECT)
    wrong = sum(1 for i in items if i["cell"] in _WRONG)
    return {"answered": len(items), "takes": len(takes),
            "wins": sum(1 for i in takes if i["status"] == "win"),
            "losses": sum(1 for i in takes if i["status"] == "loss"),
            "net_points": round(sum(i["rp"] for i in items), 2),
            "net_rupees": round(sum(i["rr"] for i in items), 0),
            "correct": correct, "wrong": wrong,
            "hit_rate": round(correct / (correct + wrong), 2) if (correct + wrong) else None,
            "cells": dict(Counter(i["cell"] for i in items if i["cell"]))}


def _train_record() -> dict:
    """Claude-vs-you head-to-head from the store: rounds won, net P&L, hit-rate."""
    recs = store.load_records(JOURNAL_DB, kind="training")
    you_items, cl_items = [], []
    rounds = {"you": 0, "claude": 0, "ties": 0}
    agree = disagree = 0
    for r in recs:
        ya = "take" if r.get("decision") == "training_take" else "skip"
        yo = r.get("outcome") or {}
        yi = {"action": ya, "cell": r.get("matrix"), "status": yo.get("status"),
              "rp": (yo.get("points", 0) or 0) if ya == "take" else 0,
              "rr": (yo.get("rupees", 0) or 0) if ya == "take" else 0}
        you_items.append(yi)
        ce = (r.get("proposal") or {}).get("claude_eval")
        if not ce:
            continue
        ca = ce.get("action")
        ci = {"action": ca, "cell": ce.get("cell"), "status": ce.get("status"),
              "rp": (ce.get("points", 0) or 0) if ca == "take" else 0,
              "rr": (ce.get("rupees", 0) or 0) if ca == "take" else 0}
        cl_items.append(ci)
        agree, disagree = (agree + (ca == ya), disagree + (ca != ya))
        rounds[{"you": "you", "claude": "claude", "tie": "ties"}[
            _round_winner(yi["cell"], ci["cell"])]] += 1
    return {"n": len(recs), "rounds": rounds, "agree": agree, "disagree": disagree,
            "agree_rate": round(agree / (agree + disagree), 2) if (agree + disagree) else None,
            "you": _tally(you_items), "claude": _tally(cl_items), "lots": TRAIN_LOTS}


def _train_score() -> dict:
    """Cumulative training scoreboard from the store (realized P&L = taken trades)."""
    from collections import Counter
    recs = store.load_records(JOURNAL_DB, kind="training")
    takes = [r for r in recs if r.get("decision") == "training_take"]
    net_pts = round(sum((r.get("outcome") or {}).get("points", 0) or 0 for r in takes), 2)
    net_rs = round(sum((r.get("outcome") or {}).get("rupees", 0) or 0 for r in takes), 0)
    wins = sum(1 for r in takes if (r.get("outcome") or {}).get("status") == "win")
    losses = sum(1 for r in takes if (r.get("outcome") or {}).get("status") == "loss")
    cells = Counter(r.get("matrix") for r in recs if r.get("matrix"))
    return {"n": len(recs), "takes": len(takes), "wins": wins, "losses": losses,
            "net_points": net_pts, "net_rupees": net_rs, "lots": TRAIN_LOTS,
            "cells": dict(cells)}


@app.get("/api/train/score")
def train_score():
    return _train_score()


@app.get("/api/train/record")
def train_record():
    return _train_record()
