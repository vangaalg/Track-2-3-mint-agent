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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from loaders import get_loader
from feeds.snapshot import build_snapshot
from feeds.breeze_oi import make_chain_fetcher
from feeds.oi import chain_table
from feeds.td_macro import make_quote_fn, SCORECARD_SYMBOLS
from feeds.macro import fetch_macro
from feeds import oi_store
from analysis.trade1 import propose_trade1
from analysis.triggers import replay_today
from analysis.proposal import Recommendation
from agent.memory import load_decisions, distill_memory
from agent.read import claude_read
from agent.chat import spar_turn
from execution import breeze_exec
from journal.log import log_decision, DEFAULT_LOG
from journal.outcomes import settle_log, matrix_summary

ANCHOR = "9h15min"
EXPIRY_WEEKDAY = 1
VIZ_POINTS = 1000
PULL_TTL = 60          # chart/snapshot re-pull cadence (s)
OI_TTL = 300           # option chain + macro cadence (s)
LOG_OI = True          # persist each fresh chain to feeds.oi_store (the flywheel)
DEFAULT_SIZE = 75
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


PULL_FN = _default_pull
CHAIN_FN = _default_chain
MACRO_FN = _default_macro
READ_COMPLETER = None    # claude_read completer (None -> live Anthropic call)
CHAT_COMPLETER = None     # spar_turn completer (None -> live Anthropic call)

# --- in-process state (single local user) ---------------------------------- #
_state: dict = {
    "snap": None, "prop": None, "chain": None,
    "snap_at": 0.0, "oi_at": 0.0,
    "read": None, "analysed_bar": None,
    "chat": [],
}

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
            symbol, base_min, daily, anchor=ANCHOR,
            oi_fetch_fn=(lambda i: chain) if chain is not None else None,
            macro=_state.get("macro"),
        )
        if snap.oi is None and _state.get("chain_err"):
            snap.notes.append(f"oi: {_state['chain_err']}")
        _state["snap"] = snap
        _state["prop"] = propose_trade1(snap, size)
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
                  "numbers": read.get("numbers", {}), "levels": read.get("levels", {})},
        "oi": snap.oi, "macro": snap.macro, "notes": snap.notes,
        "chain": rows, "proposal": prop.as_dict(),
    }


def _run_read() -> dict:
    snap, prop = _state["snap"], _state["prop"]
    memory = distill_memory(load_decisions(DEFAULT_LOG))
    _state["memory"] = memory
    read = claude_read(snap, prop, memory, completer=READ_COMPLETER)
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


@app.get("/api/record")
def record():
    """Settle the decision log against today's bars and return the 2x2 track record."""
    frames = _state["snap"].frames if _state["snap"] is not None else {}
    decisions = settle_log(DEFAULT_LOG, frames)
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
    return replay_today(snap.feats, snap.frames, size_lots=size)


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


@app.post("/api/decision")
def decision(action: str = Form(...), live: bool = Form(False)):
    prop = _state["prop"]
    if prop is None:
        raise HTTPException(status_code=409, detail="no proposal yet")
    if action == "approve":
        result = breeze_exec.place(prop, live=live)
        rec = log_decision(prop, "approved", execution=result)
        return {"status": result["status"], "logged": rec["decision"]}
    rec = log_decision(prop, "rejected")
    return {"status": "rejected", "logged": rec["decision"]}
