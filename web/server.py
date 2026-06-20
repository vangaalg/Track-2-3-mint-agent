"""FastAPI backend for the web cockpit.

Thin JSON layer over the engine with server-side TTL caches so the frontend can
poll every ~15s cheaply (heavy intraday pull ~60s, option chain + macro ~5min).
The live dependencies (loader, chain/macro fetchers, Claude completer) are module
globals so tests can inject mocks and run fully offline.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from loaders import get_loader
from indicators.directional import journal_mtf_config
from feeds.snapshot import build_snapshot, build_snapshot_at
from feeds.breeze_oi import make_chain_fetcher
from feeds.oi import chain_table, summarise_chain
from feeds.td_macro import make_quote_fn, SCORECARD_SYMBOLS
from feeds.macro import fetch_macro
from feeds import oi_store
from analysis.trade1 import propose_trade1, apply_strike, apply_oi_boost, LOT_SIZE
from analysis.strike import select_strike
from analysis.triggers import replay_today, list_triggers, simulate_intraday
from analysis.proposal import Recommendation
from agent.memory import load_decisions, distill_memory, distill_context
from agent.read import claude_read
from agent.chat import spar_turn
from execution import breeze_exec
from journal.log import log_decision, DEFAULT_LOG
from journal.outcomes import settle_log, settle_store, matrix_summary, grade_training
from journal import store

ANCHOR = "9h15min"
EXPIRY_WEEKDAY = 1
VIZ_POINTS = 1000
PULL_TTL = 60          # chart/snapshot re-pull cadence (s)
OI_TTL = 300           # option chain + macro cadence (s)
LOG_OI = True          # persist each fresh chain to feeds.oi_store (the flywheel)
DEFAULT_SIZE = 75
# THE active resolver: the trader's journal 3-min strategy (trio + 2-close confirm,
# trigger-only — HTF is trend context, not a gate). Live cockpit + training both use it.
RESOLVER_CFG = journal_mtf_config()
JOURNAL_DB = store.DB_PATH   # full-context SQLite store (overridden in tests)
_STATIC = Path(__file__).parent / "static"

# --- injectable seams (overridden in tests) -------------------------------- #
def _default_pull(symbol: str):
    loader = get_loader("breeze")
    base_min = loader.load(symbol, "minute", start=date.today() - timedelta(days=3),
                           use_cache=False)
    daily = loader.load(symbol, "day", start=date.today() - timedelta(days=800),
                        use_cache=False)
    return base_min, daily


def _default_chain(symbol: str):
    return make_chain_fetcher(weekday=EXPIRY_WEEKDAY)(symbol)


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

# --- in-process state (single local user) ---------------------------------- #
_state: dict = {
    "snap": None, "prop": None, "chain": None,
    "snap_at": 0.0, "oi_at": 0.0,
    "read": None, "analysed_bar": None,
    "chat": [],
}

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
    now = time.time()
    if _state["chain"] is None or now - _state["oi_at"] > OI_TTL:
        try:
            _state["chain"] = CHAIN_FN(symbol)
        except Exception as exc:
            _state["chain"] = None
            _state["chain_err"] = str(exc)
        _state["macro"] = MACRO_FN(symbol)
        _state["oi_at"] = now

    if _state["snap"] is None or now - _state["snap_at"] > PULL_TTL:
        base_min, daily = PULL_FN(symbol)
        chain = _state["chain"]
        snap = build_snapshot(
            symbol, base_min, daily, anchor=ANCHOR, mtf_cfg=RESOLVER_CFG,
            oi_fetch_fn=(lambda i: chain) if chain is not None else None,
            macro=_state.get("macro"),
        )
        if snap.oi is None and _state.get("chain_err"):
            snap.notes.append(f"oi: {_state['chain_err']}")
        _state["snap"] = snap
        prop = propose_trade1(snap, size)
        # LIVE strike agent: pick the ITM vehicle off the live chain (least theta).
        if chain is not None and not chain.empty and prop.direction in ("long", "short"):
            pick = select_strike(chain_table(chain, snap.spot, window=VIZ_POINTS),
                                 snap.spot, prop.direction)
            apply_strike(prop, pick)
        _state["prop"] = prop
        _state["snap_at"] = now
        # log the chain snapshot (the OI flywheel) once per fresh OI bucket
        if LOG_OI and chain is not None and not chain.empty \
                and _state.get("oi_logged_at") != _state["oi_at"]:
            try:
                oi_store.save_chain(symbol, snap.ts, snap.spot, chain)
                _state["oi_logged_at"] = _state["oi_at"]
            except Exception:
                pass


def _payload(symbol: str) -> dict:
    snap, prop, chain = _state["snap"], _state["prop"], _state["chain"]
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
        "chain": rows, "proposal": prop.as_dict(),
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


def _run_read() -> dict:
    snap, prop = _state["snap"], _state["prop"]
    memory = _learning_memory()
    _state["memory"] = memory
    read = claude_read(snap, prop, memory, completer=READ_COMPLETER)
    # LIVE OI confluence: +1 conviction (re-nudges size) when Claude's chain lean
    # agrees with the trade direction.
    if prop is not None:
        apply_oi_boost(prop, getattr(read, "oi_bias", None))
    _state["read"] = read
    _state["analysed_bar"] = snap.ts
    return asdict(read)


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
    payload = _payload(symbol)
    # auto-analyse once per new ENTER bar (server-side dedupe)
    payload["analysed_bar"] = _state.get("analysed_bar")
    payload["auto_trigger"] = (
        _state["prop"].recommendation is Recommendation.ENTER
        and _state.get("analysed_bar") != _state["snap"].ts
    )
    return payload


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
def chart(tf: str = "3min", bars: int = 200):
    """Candlestick + indicator overlays for the price panel (computed per TF)."""
    if _state["snap"] is None:
        raise HTTPException(status_code=409, detail="no snapshot yet")
    snap = _state["snap"]
    frame = snap.frames.get(tf)
    if frame is None or frame.empty:
        raise HTTPException(status_code=404, detail=f"no frame for tf {tf!r}")
    data = _serialize_chart(frame, bars)
    return {"tf": tf, "bars": data["bars"], "cpr": _daily_cpr(snap) or data["cpr"]}


@app.get("/api/record")
def record():
    """Settle the decision log against today's bars and return the 2x2 track record."""
    frames = _state["snap"].frames if _state["snap"] is not None else {}
    decisions = settle_log(DEFAULT_LOG, frames)
    try:
        settle_store(frames, path=JOURNAL_DB)   # grade the rich store too (same 2x2)
    except Exception:
        pass
    recent = decisions[-12:]
    return {"summary": matrix_summary(decisions),
            "recent": [{"decision": r.get("decision"), "process": r.get("process_grade"),
                        "matrix": r.get("matrix"), "ts": (r.get("proposal") or {}).get("ts"),
                        "direction": (r.get("proposal") or {}).get("direction"),
                        "outcome": r.get("outcome")} for r in recent]}


@app.get("/api/triggers")
def triggers(size: int = DEFAULT_SIZE):
    if _state["snap"] is None:
        raise HTTPException(status_code=409, detail="no snapshot yet")
    snap = _state["snap"]
    return replay_today(snap.feats, snap.frames, cfg=RESOLVER_CFG, size_lots=size)


@app.post("/api/analyse")
def analyse():
    if _state["snap"] is None:
        raise HTTPException(status_code=409, detail="no snapshot yet")
    try:
        return _run_read()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"claude unavailable: {exc}")


@app.post("/api/chat")
async def chat(text: str = Form(""), files: list[UploadFile] = File(default=[])):
    if _state["snap"] is None:
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
    _state["chat"].append({"role": "user", "content": blocks if files else text})
    try:
        reply = spar_turn(_state["chat"], _state["snap"], _state["prop"],
                          _state.get("memory", ""), completer=CHAT_COMPLETER)
    except Exception as exc:
        reply = f"(chat unavailable: {exc})"
    _state["chat"].append({"role": "assistant", "content": reply})
    return {"reply": reply}


def _save_context(decision: str, symbol: str, execution: dict | None) -> None:
    """Archive the WHOLE decision moment to the SQLite store (chat, Claude read,
    chart datapoints, raw chain, all macro) so the agent can learn from everything."""
    snap, prop, chain = _state["snap"], _state["prop"], _state["chain"]
    chain_rows = None
    if chain is not None and not chain.empty:
        try:
            chain_rows = json.loads(chain_table(chain, snap.spot, window=VIZ_POINTS).to_json(orient="records"))
        except Exception:
            chain_rows = None
    read = _state.get("read")
    payload = {
        "ts": snap.ts, "symbol": symbol, "decision": decision, "spot": snap.spot,
        "proposal": prop.as_dict(),
        "claude_read": asdict(read) if read is not None else None,
        "chat": _state.get("chat") or None,
        "chart": _chart_bundle(snap),
        "chain": chain_rows,
        "macro": snap.macro, "oi_summary": snap.oi, "notes": snap.notes,
        "execution": execution,
    }
    try:
        store.save_decision(payload, path=JOURNAL_DB)
    except Exception:
        pass


@app.post("/api/decision")
def decision(action: str = Form(...), live: bool = Form(False), symbol: str = Form("NIFTY")):
    prop = _state["prop"]
    if prop is None:
        raise HTTPException(status_code=409, detail="no proposal yet")
    if action == "approve":
        result = breeze_exec.place(prop, live=live)
        rec = log_decision(prop, "approved", execution=result)
        _save_context("approved", symbol, result)
        return {"status": result["status"], "logged": rec["decision"]}
    rec = log_decision(prop, "rejected")
    _save_context("rejected", symbol, None)
    return {"status": "rejected", "logged": rec["decision"]}


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
                 stop: float = Form(0.0), reason: str = Form("")):
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

    proposal = case["prop"].as_dict()
    proposal = {**proposal, "direction": direction, "size_lots": TRAIN_LOTS,
                "reason": reason.strip() or None,
                "claude_eval": claude_eval, "agree": agree}
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
    }
    try:
        store.save_decision(payload, path=JOURNAL_DB)
    except Exception:
        pass
    return {"action": action, "direction": direction, "entry": entry, "rr": rr,
            "reason": reason.strip() or None, "your_levels": your_levels,
            "your_outcome": your, "engine_outcome": engine, "cell": cell,
            "agree": agree, "round_winner": round_winner,
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
