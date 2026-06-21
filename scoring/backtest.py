"""Backtest the journal 3-min breakout-pullback trigger over a history window.

The trigger engine already exists: ``analysis.triggers.list_triggers`` enumerates
EVERY Trade-1 trigger across a multi-session frame and resolves each to win/loss/open
within its own session (session-low stop + structural/R-multiple target). This module
wraps it into a one-call backtest:

    pull ~N days of 1-minute NIFTY (+ long daily) -> build the MTF ladder
    -> list_triggers (the locked breakout-pullback resolver) -> aggregate.

Aggregation is pure + testable (``aggregate``); the CLI does the (networked) pull via
``loaders.get_loader`` and writes a ranked CSV + a markdown summary to results/.

Live data pulls run on the user's machine (creds in env); this stays import-clean and
the engine is exercised offline with synthetic/already-built frames.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import pandas as pd

from analysis.triggers import list_triggers, trigger_excursion
from analysis.trade1 import LOT_SIZE
from indicators.directional import journal_mtf_config
from feeds.snapshot import build_snapshot


def _stats(rows: list[dict], lot_size: int, lots: int) -> dict:
    wins = [r for r in rows if r["outcome"] == "win"]
    losses = [r for r in rows if r["outcome"] == "loss"]
    eods = [r for r in rows if r["outcome"] not in ("win", "loss")]   # exited at the close
    decided = len(wins) + len(losses)
    net_pts = round(sum(r["points"] for r in rows), 2)                # all exits realised
    gross_win = sum(r["points"] for r in wins)
    gross_loss = sum(r["points"] for r in losses)                     # negative
    gains = sum(r["points"] for r in rows if r["points"] > 0)         # incl. eod, by sign
    pains = sum(r["points"] for r in rows if r["points"] < 0)
    n = len(rows)
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "eod": len(eods),
        "hit_rate": round(len(wins) / decided, 3) if decided else None,   # target vs stop
        "net_points": net_pts,
        "net_rupees": round(net_pts * lot_size * lots, 0),
        "eod_points": round(sum(r["points"] for r in eods), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else None,
        "expectancy": round(net_pts / n, 2) if n else None,               # per trade, all exits
        "profit_factor": round(gains / abs(pains), 2) if pains else None,
    }


def aggregate(triggers: list[dict], lot_size: int = LOT_SIZE, lots: int = 1) -> dict:
    """Roll a list of trigger dicts (from ``list_triggers``) into a backtest report.

    Returns overall stats + per-direction + per-day breakdowns. Pure — pass any list
    of trigger dicts carrying ``outcome``/``points``/``direction``/``date``.
    """
    overall = _stats(triggers, lot_size, lots)
    by_dir = {d: _stats([r for r in triggers if r["direction"] == d], lot_size, lots)
              for d in ("long", "short")}
    days = defaultdict(list)
    for r in triggers:
        days[r["date"]].append(r)
    by_day = [{"date": d, **_stats(rows, lot_size, lots)} for d, rows in sorted(days.items())]
    return {"overall": overall, "by_direction": by_dir, "by_day": by_day,
            "lot_size": lot_size, "lots": lots}


def make_claude_filter(symbol: str, base_1m, daily, memory: str = "",
                       completer=None, cfg=None, verbose: bool = False):
    """Build a per-trigger Claude take/skip filter for the backtest.

    Returns ``fn(trigger) -> {"verdict","target","stop"}``: rebuilds the AS-OF world at
    the trigger's timestamp (``build_snapshot_at`` — no future leakage), runs the engine
    proposal + ``claude_read`` against it, and returns Claude's verdict + its own levels.
    ``completer`` is the injectable Anthropic seam (stub it in tests); live needs
    ANTHROPIC_API_KEY.

    Diagnostics: an errored read is tagged ``"error": True`` (still returns stand_down,
    so one bad bar can't kill the run) and counted on ``fn.state``; the FIRST error's
    traceback is captured there so the caller can tell genuine stand-downs from a
    systematic failure. ``verbose`` prints each verdict to stderr as it goes.
    """
    from feeds.snapshot import build_snapshot_at
    from analysis.trade1 import propose_trade1
    from agent.read import claude_read
    cfg = cfg or journal_mtf_config()
    state = {"n": 0, "enter": 0, "stand_down": 0, "errors": 0, "first_error": None}

    def fn(trigger: dict) -> dict:
        state["n"] += 1
        try:
            snap = build_snapshot_at(symbol, base_1m, daily, trigger["ts"], mtf_cfg=cfg)
            prop = propose_trade1(snap)
            read = claude_read(snap, prop, memory, completer=completer)
            v = "enter" if read.recommendation == "enter" else "stand_down"
            state[v] += 1
            if verbose:
                lv = f" tgt {read.proposed_target} stp {read.proposed_stop}" if v == "enter" else ""
                print(f"  [{state['n']}] {trigger['ts']} {trigger['direction']} -> {v}{lv}",
                      file=sys.stderr)
            return {"verdict": v, "target": read.proposed_target, "stop": read.proposed_stop}
        except Exception as exc:
            state["errors"] += 1
            if state["first_error"] is None:
                import traceback
                state["first_error"] = traceback.format_exc()
                print(f"  [{state['n']}] ERROR (first): {exc}", file=sys.stderr)
            return {"verdict": "stand_down", "error": True}

    fn.state = state
    return fn


def clamp_levels(direction: str, entry: float, target, stop,
                 min_rr: float = 1.5, max_risk_frac: float = 0.02):
    """Guardrail Claude's proposed target/stop. Returns ``(target, stop, rr)`` or
    ``(None, None, None)`` if unusable (wrong side / zero risk).

    Both must sit on the correct side of entry; an absurdly wide stop is capped to
    ``max_risk_frac`` of price; and if Claude's reward:risk is below ``min_rr`` the
    target is pushed out to meet it (we never tighten Claude's stop past its idea).
    """
    if target is None or stop is None:
        return None, None, None
    long = direction == "long"
    if long and not (target > entry and stop < entry):
        return None, None, None
    if (not long) and not (target < entry and stop > entry):
        return None, None, None
    risk = abs(entry - stop)
    if risk <= 0:
        return None, None, None
    if risk > max_risk_frac * entry:                 # sanity-cap an insane stop
        risk = max_risk_frac * entry
        stop = entry - risk if long else entry + risk
    reward = abs(target - entry)
    if reward / risk < min_rr:                        # honour the R:R floor via the target
        reward = min_rr * risk
        target = entry + reward if long else entry - reward
    return round(target, 2), round(stop, 2), round(reward / risk, 2)


def run_backtest(snap, lots: int = 1, cfg=None, target_driven: bool = True,
                 claude_filter=None, min_stop: float = 0.0,
                 atr_mult: float = 0.0, atr_period: int = 14,
                 min_confidence: int = 0, skip_open_min: int = 0) -> dict:
    """Backtest the trigger over a pre-built snapshot's feats/frames.

    Reuses the LIVE resolver (``journal_mtf_config``) so the backtest fires exactly the
    triggers the cockpit + training would. ``target_driven`` uses the target-first
    level model (SL derived from R:R); ``min_stop`` floors the stop distance (points).
    If ``claude_filter`` is given, each trigger is tagged with Claude's take/skip verdict
    and a CLAUDE-FILTERED (ENTER-only) report is added alongside the unfiltered one.
    ``min_confidence`` (1..5) keeps only HTF-aligned triggers — those whose 45-EMA MTF
    confidence is ``>= min_confidence`` — and adds a CONFIDENCE-FILTERED report (engine
    levels, no Claude). This MEASURES the "trade with the higher-timeframe trend" idea;
    it does NOT change the live engine (which still uses confidence only to size).

    Returns ``{"triggers": [...], "report": {...}, "filtered": ..., "conf_filtered": ...}``.
    """
    from analysis.triggers import _resolve_intraday
    cfg = cfg or journal_mtf_config()
    triggers = list_triggers(snap.feats, snap.frames, cfg=cfg, size_lots=lots,
                             lot_size=LOT_SIZE, realistic=True, target_driven=target_driven,
                             min_stop=min_stop, atr_mult=atr_mult, atr_period=atr_period,
                             skip_open_min=skip_open_min)
    filtered = None
    if claude_filter is not None:
        frame3m = snap.frames["3min"]
        taken = []
        for t in triggers:
            cf = claude_filter(t)
            if isinstance(cf, str):                  # back-compat: verdict-only filters
                cf = {"verdict": cf}
            t["claude"] = cf["verdict"]
            tgt, stp, rr = clamp_levels(t["direction"], t["entry"], cf.get("target"), cf.get("stop"))
            if tgt is not None:
                t["claude_target"], t["claude_stop"], t["claude_rr"] = tgt, stp, rr
            if cf["verdict"] != "enter":
                continue
            if tgt is not None:                      # trade CLAUDE's own levels
                outcome, exit_px, points, _ = _resolve_intraday(
                    frame3m, t["ts"], t["direction"], t["entry"], stp, tgt)
                taken.append({**t, "eng_stop": stp, "eng_target": tgt, "eng_rr": rr,
                              "outcome": outcome, "points": points,
                              "rupees": round(points * LOT_SIZE * lots, 0)})
            else:                                    # no usable levels -> engine's
                taken.append(t)
        filtered = aggregate(taken, LOT_SIZE, lots)
    conf_filtered = None
    if min_confidence > 0:
        aligned = [t for t in triggers if (t.get("mtf_confidence") or 0) >= min_confidence]
        conf_filtered = aggregate(aligned, LOT_SIZE, lots)
    return {"triggers": triggers, "report": aggregate(triggers, LOT_SIZE, lots),
            "filtered": filtered, "conf_filtered": conf_filtered}


# --------------------------------------------------------------------------- #
# CLI — pull + report
# --------------------------------------------------------------------------- #
def _pull(symbol: str, days: int, loader_name: str, chunk_days: int = 3,
          offline: bool = False, refresh: bool = False):
    """Serve ~`days` of 1-minute base + a long daily history from the local store,
    pulling from Breeze only what's missing (runs on the user's box).

    Breeze's get_historical_data_v2 caps rows per call (~1000 ≈ ~3 trading days of
    1-minute bars), so we PAGINATE in `chunk_days`-wide windows. Every pull is MERGED
    into ``feeds.ohlcv_store`` (parquet under data/ohlcv/), so each run only fetches
    the GAP since the last stored bar and history accumulates for years. ``offline``
    serves entirely from the store (no network); ``refresh`` re-pulls the full window.
    """
    import time
    from feeds import ohlcv_store as store
    today = date.today()

    # OFFLINE: serve the whole window from the local store, no network at all.
    if offline:
        base = store.load_ohlcv(symbol, "minute")
        daily = store.load_ohlcv(symbol, "day")
        if base is None or base.empty:
            raise SystemExit(f"--offline but no stored 1-min data for {symbol!r} in "
                             f"{store.STORE_DIR}/. Run once online first to seed it.")
        cov = store.coverage(symbol, "minute")
        print(f"  OFFLINE: {cov[2]} stored 1-min bars {cov[0].date()}…{cov[1].date()}",
              file=sys.stderr)
        if days:                                        # honour the requested window
            cutoff = pd.Timestamp(today - timedelta(days=days), tz=base.index.tz)
            base = base[base.index >= cutoff]
        return base, daily

    from loaders import get_loader
    loader = get_loader(loader_name)
    # Pull only the GAP: from the last stored bar forward (full window on first run /
    # when --refresh). Each run MERGES into the store, so history accumulates for years.
    cov = store.coverage(symbol, "minute")
    start = today - timedelta(days=days + 4)
    if cov is not None and not refresh:
        start = max(start, cov[1].date() + timedelta(days=1))
        print(f"  store has {cov[2]} bars to {cov[1].date()}; pulling {start}…{today}",
              file=sys.stderr)
    cur = start
    while cur <= today:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), today)
        try:
            part = loader.load(symbol, "minute", start=cur, end=chunk_end, use_cache=False)
            if part is not None and not part.empty:
                store.merge_save(symbol, "minute", part)    # persist as we go
                print(f"  {cur} … {chunk_end}: {len(part)} bars", file=sys.stderr)
        except Exception as exc:                       # empty window or transient error
            print(f"  {cur} … {chunk_end}: skipped ({exc})", file=sys.stderr)
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.3)                                # gentle on Breeze rate limits
    base = store.load_ohlcv(symbol, "minute")
    if base is None or base.empty:
        raise RuntimeError(f"no 1-minute data for {symbol!r} (store empty + pull failed) — "
                           "check creds/session token and the symbol.")
    daily = loader.load(symbol, "day", start=today - timedelta(days=800), use_cache=False)
    store.merge_save(symbol, "day", daily)
    daily = store.load_ohlcv(symbol, "day")
    if days:                                            # return just the requested window
        cutoff = pd.Timestamp(today - timedelta(days=days), tz=base.index.tz)
        base = base[base.index >= cutoff]
    return base, daily


def _preflight(loader_name: str, timeout: float = 5.0) -> None:
    """Fail fast (seconds, not hours) if the data host is unreachable.

    A dead network / DNS makes each Breeze call hang on getaddrinfo retries, so a
    30-day paginated pull can grind silently for ages. Resolve + TCP-connect once
    up front and raise a clear, actionable error instead.
    """
    import socket
    host = {"breeze": "breezeapi.icicidirect.com",
            "twelvedata": "api.twelvedata.com"}.get(loader_name)
    if not host:                                          # unknown loader → skip the check
        return
    try:
        # NB: use create_connection's own timeout — do NOT call socket.setdefaulttimeout,
        # which sets a process-wide default that would then kill every later data read.
        with socket.create_connection((host, 443), timeout=timeout):
            pass
    except OSError as exc:
        raise SystemExit(
            f"\nCannot reach {host!r} ({exc}).\n"
            f"This is a NETWORK/DNS problem on this machine, not the backtest.\n"
            f"  • disconnect any VPN/proxy that may block icicidirect.com\n"
            f"  • check the internet is up (try opening a website)\n"
            f"  • try switching DNS to 8.8.8.8 / 8.8.4.4, then: ipconfig /flushdns\n"
            f"  • allow python.exe through the firewall/antivirus\n"
        )


def _pctile(vals, qs=(0.25, 0.5, 0.75, 0.9)) -> dict:
    if not vals:
        return {q: None for q in qs}
    s = pd.Series(vals)
    return {q: round(float(s.quantile(q)), 1) for q in qs}


def excursion_stats(snap, triggers: list[dict]) -> dict:
    """Attach mfe/mae/eod_pts (points AFTER the trigger) to each trigger and summarise
    the favourable vs adverse reach, overall + per direction. Target-agnostic — it
    measures the raw move the trigger predicts so targets can be set off the real
    distribution. ``edge_ratio`` = median MFE / median MAE (>1 ⇒ runs farther our way
    than against ⇒ genuine directional pull; ≈1 ⇒ coin-flip)."""
    frame3m = snap.frames["3min"]
    for t in triggers:
        mfe, mae, eod = trigger_excursion(frame3m, t["ts"], t["direction"], t["entry"])
        t["mfe"], t["mae"], t["eod_pts"] = mfe, mae, eod

    def _block(rows):
        mfe = [r["mfe"] for r in rows]
        mae = [r["mae"] for r in rows]
        eod = [r["eod_pts"] for r in rows]
        med_mfe = float(pd.Series(mfe).median()) if mfe else None
        med_mae = float(pd.Series(mae).median()) if mae else None
        return {
            "n": len(rows), "mfe": _pctile(mfe), "mae": _pctile(mae),
            "med_mfe": None if med_mfe is None else round(med_mfe, 1),
            "med_mae": None if med_mae is None else round(med_mae, 1),
            "med_eod": None if not eod else round(float(pd.Series(eod).median()), 1),
            "edge_ratio": None if not med_mae else round(med_mfe / med_mae, 2),
        }
    return {"overall": _block(triggers),
            "long": _block([t for t in triggers if t["direction"] == "long"]),
            "short": _block([t for t in triggers if t["direction"] == "short"])}


def excursion_text(stats: dict) -> str:
    def _row(name, b):
        if not b["n"]:
            return f"  {name:7s} n=0"
        p = lambda d: f"p25/50/75/90 = {d[0.25]}/{d[0.5]}/{d[0.75]}/{d[0.9]}"
        return (f"  {name:7s} n={b['n']}  MFE {p(b['mfe'])}  |  MAE {p(b['mae'])}  |  "
                f"median MFE/MAE = {b['med_mfe']}/{b['med_mae']} (edge {b['edge_ratio']})  "
                f"hold-to-close median {b['med_eod']}")
    return ("POST-TRIGGER EXCURSION (points after entry, target-agnostic; "
            "MFE=favourable reach, MAE=adverse heat)\n"
            + "\n".join(_row(n, stats[n]) for n in ("overall", "long", "short")))


def level_sweep(snap, triggers: list[dict], targets, stops, lot_size: int, lots: int,
                cost_pts: float = 0.0) -> dict:
    """Hold the trigger ENTRIES fixed and re-simulate each (target_pts, stop_pts) pair
    against the real bars — a controlled test of which fixed levels extract the most.
    Every trade is intraday (``_resolve_intraday`` exits at the bell — flat by EOD).

    ``cost_pts`` is the per-round-trip cost in index points (brokerage + spread +
    slippage), subtracted from every trade so the table reads NET of costs.

    For each cell reports expectancy (net points / trade) over the whole window plus
    the first and second HALVES of the date range, so a level only counts if it holds
    OUT-OF-SAMPLE (the lesson from the confidence-filter blow-up). Entries are held
    fixed (no re-dedup), so cells differ only by their levels — the cleanest comparison.
    """
    from analysis.triggers import _resolve_intraday
    frame3m = snap.frames["3min"]
    dates = sorted({t["date"] for t in triggers})
    mid = dates[len(dates) // 2] if dates else None

    def _sim(tg, sl):
        rows = []
        for t in triggers:
            e, d = t["entry"], t["direction"]
            stop = e - sl if d == "long" else e + sl
            target = e + tg if d == "long" else e - tg
            _, _, pts, _ = _resolve_intraday(frame3m, t["ts"], d, e, stop, target)
            rows.append((t["date"], pts - cost_pts))            # net of round-trip cost
        exp = lambda rs: round(sum(p for _, p in rs) / len(rs), 2) if rs else None
        h1 = [r for r in rows if r[0] < mid]
        h2 = [r for r in rows if r[0] >= mid]
        return {"target": tg, "stop": sl, "rr": round(tg / sl, 2), "n": len(rows),
                "net": round(sum(p for _, p in rows), 0), "exp": exp(rows),
                "exp_h1": exp(h1), "exp_h2": exp(h2)}

    cells = [_sim(tg, sl) for tg in targets for sl in stops]
    return {"cells": cells, "split_date": mid, "lot_size": lot_size, "lots": lots,
            "cost_pts": cost_pts}


def level_sweep_text(sweep: dict) -> str:
    cells = sorted(sweep["cells"], key=lambda c: (c["exp"] is None, -(c["exp"] or 0)))
    cost = sweep.get("cost_pts", 0.0)
    cost_note = (f"NET of {cost:.2f} pt/trade cost (≈₹{cost * sweep['lot_size']:.0f}/lot)"
                 if cost else "GROSS (no costs — pass --cost)")
    lines = [f"LEVEL SWEEP (fixed target × stop on the real bars, intraday/flat-by-EOD; "
             f"{cost_note}; OOS split at {sweep['split_date']}; * = profitable in BOTH halves)",
             "  target  stop   R:R    n    net      exp   exp_H1   exp_H2"]
    for c in cells:
        both = (c["exp_h1"] or 0) > 0 and (c["exp_h2"] or 0) > 0
        lines.append(f"  {c['target']:>5}  {c['stop']:>4}  {c['rr']:>4}  {c['n']:>4}  "
                     f"{c['net']:>+7.0f}  {c['exp']:>+6.2f}  {c['exp_h1']:>+6.2f}  "
                     f"{c['exp_h2']:>+6.2f}  {'*' if both else ''}")
    return "\n".join(lines)


def _fmt(s: dict) -> str:
    hit = "—" if s["hit_rate"] is None else f"{s['hit_rate'] * 100:.0f}%"
    return (f"n={s['n']}  W/L/EOD={s['wins']}/{s['losses']}/{s['eod']}  hit={hit}  "
            f"net={s['net_points']:+.1f} pts (₹{s['net_rupees']:+,.0f})  "
            f"exp={s['expectancy']} pf={s['profit_factor']}")


def report_text(symbol: str, report: dict, levels: str = "target",
                filtered: dict | None = None, conf_filtered: dict | None = None,
                min_confidence: int = 0) -> str:
    o = report["overall"]
    stop_desc = "target-first SL from R:R" if levels == "target" else "session-low stop"
    lines = [f"Backtest — {symbol}  (breakout-pullback, {stop_desc}, R:R≥1.5, "
             f"one position at a time, flat by close; {report['lots']} lot × {report['lot_size']})",
             "hit = target-vs-stop only · EOD = exited at the bell (P&L still counted) · "
             "exp = net/trade · pf incl. EOD by sign", ""]
    lines.append(f"OVERALL  {_fmt(o)}")
    lines.append(f"  long   {_fmt(report['by_direction']['long'])}")
    lines.append(f"  short  {_fmt(report['by_direction']['short'])}")
    if filtered is not None:
        lines.append("")
        lines.append(f"CLAUDE-FILTERED (took {filtered['overall']['n']} of {o['n']}):"
                     f"  {_fmt(filtered['overall'])}")
        lines.append(f"  long   {_fmt(filtered['by_direction']['long'])}")
        lines.append(f"  short  {_fmt(filtered['by_direction']['short'])}")
    if conf_filtered is not None:
        lines.append("")
        lines.append(f"CONFIDENCE-FILTERED (HTF 45-EMA align ≥{min_confidence}/5; "
                     f"took {conf_filtered['overall']['n']} of {o['n']}):  "
                     f"{_fmt(conf_filtered['overall'])}")
        lines.append(f"  long   {_fmt(conf_filtered['by_direction']['long'])}")
        lines.append(f"  short  {_fmt(conf_filtered['by_direction']['short'])}")
    lines.append("")
    lines.append("Per day:")
    for d in report["by_day"]:
        lines.append(f"  {d['date']}  {_fmt(d)}")
    return "\n".join(lines)


def write_outputs(symbol: str, triggers: list[dict], report: dict, outdir: str,
                  levels: str = "target", filtered: dict | None = None,
                  conf_filtered: dict | None = None, min_confidence: int = 0,
                  extra: str = "") -> tuple[str, str]:
    from pathlib import Path
    Path(outdir).mkdir(parents=True, exist_ok=True)
    # IST date+time so successive runs don't overwrite each other (was date-only)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S_IST")
    csv_path = f"{outdir}/backtest_{symbol}_{stamp}.csv"
    md_path = f"{outdir}/backtest_{symbol}_{stamp}.md"
    pd.DataFrame(triggers).to_csv(csv_path, index=False)
    gen = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    body = report_text(symbol, report, levels=levels, filtered=filtered,
                       conf_filtered=conf_filtered, min_confidence=min_confidence)
    if extra:
        body += "\n\n" + extra
    Path(md_path).write_text(f"_generated {gen}_\n\n```\n" + body + "\n```\n", encoding="utf-8")
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=30, help="lookback window (~1 month default)")
    ap.add_argument("--loader", default="breeze", help="loaders.get_loader name (breeze/twelvedata)")
    ap.add_argument("--lots", type=int, default=1, help="position size for the ₹ column")
    ap.add_argument("--chunk-days", type=int, default=3,
                    help="pagination window for the 1-min pull (Breeze caps rows/call)")
    ap.add_argument("--levels", choices=["target", "stop"], default="target",
                    help="target = SL derived from R:R off the objective; stop = session-low stop")
    ap.add_argument("--min-stop", type=float, default=0.0,
                    help="fixed floor on the stop distance in points (0=off)")
    ap.add_argument("--atr-mult", type=float, default=1.0,
                    help="ATR-based stop floor: stop >= atr_mult × ATR (0=off; default 1.0)")
    ap.add_argument("--atr-period", type=int, default=14, help="ATR period (3-min bars)")
    ap.add_argument("--min-confidence", type=int, default=0, metavar="N",
                    help="keep only HTF-aligned triggers (45-EMA MTF confidence >= N, 1..5); "
                         "adds a CONFIDENCE-FILTERED report. Measurement only — live unchanged.")
    ap.add_argument("--skip-open-min", type=int, default=0, metavar="N",
                    help="skip triggers in the first N minutes after the 09:15 open "
                         "(opening-whipsaw filter; 0=off). Measurement only — live unchanged.")
    ap.add_argument("--excursion", action="store_true",
                    help="report post-trigger MFE/MAE (how far price runs the trigger's way "
                         "vs against) + add mfe/mae/eod_pts columns to the CSV")
    ap.add_argument("--level-sweep", action="store_true",
                    help="sweep a grid of fixed target × stop (points) on the real bars, with "
                         "an out-of-sample first/second-half split; finds the best levels")
    ap.add_argument("--sweep-targets", default="20,30,40,50,70",
                    help="comma-separated target distances in points for --level-sweep")
    ap.add_argument("--sweep-stops", default="15,20,30,40,50",
                    help="comma-separated stop distances in points for --level-sweep")
    ap.add_argument("--cost", type=float, default=0.0, metavar="RUPEES",
                    help="per round-trip cost in ₹/lot (brokerage+spread+slippage); the "
                         "level sweep reports NET of it (e.g. 150 ≈ 2 NIFTY points)")
    ap.add_argument("--offline", action="store_true",
                    help="backtest off the saved local store only (no network; instant). "
                         "Seed it once with a normal online run first.")
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull the full window from Breeze instead of just the new gap")
    ap.add_argument("--claude", action="store_true",
                    help="run Claude take/skip on each trigger (needs ANTHROPIC_API_KEY; slow)")
    ap.add_argument("--out", default="results", help="output dir for the CSV + markdown")
    args = ap.parse_args(argv)

    if args.offline:
        print(f"OFFLINE: backtesting {args.symbol} off the local store (no network)…",
              file=sys.stderr)
    else:
        _preflight(args.loader)
        print(f"Updating {args.symbol} store via '{args.loader}' (pulling only the new gap; "
              f"--refresh to re-pull, --offline to skip network)…", file=sys.stderr)
    base, daily = _pull(args.symbol, args.days, args.loader, args.chunk_days,
                        offline=args.offline, refresh=args.refresh)
    snap = build_snapshot(args.symbol, base, daily, mtf_cfg=journal_mtf_config())
    cfilter = None
    if args.claude:
        print("Running Claude take/skip per trigger (this is slow; verdicts below)…",
              file=sys.stderr)
        cfilter = make_claude_filter(args.symbol, base, daily, verbose=True)
    out = run_backtest(snap, lots=args.lots, target_driven=(args.levels == "target"),
                       claude_filter=cfilter, min_stop=args.min_stop,
                       atr_mult=args.atr_mult, atr_period=args.atr_period,
                       min_confidence=args.min_confidence, skip_open_min=args.skip_open_min)
    if cfilter is not None:                              # diagnostics: errors vs genuine
        st = cfilter.state
        print(f"\nClaude verdicts: {st['enter']} enter / {st['stand_down']} stand_down / "
              f"{st['errors']} ERRORED (of {st['n']}).", file=sys.stderr)
        if st["errors"]:
            print("First error was:\n" + (st["first_error"] or ""), file=sys.stderr)
            print("→ the stand_downs may be masked failures, not real Claude calls.",
                  file=sys.stderr)
    print(report_text(args.symbol, out["report"], levels=args.levels, filtered=out["filtered"],
                      conf_filtered=out["conf_filtered"], min_confidence=args.min_confidence))
    extra = []
    if args.excursion:
        txt = excursion_text(excursion_stats(snap, out["triggers"]))
        print("\n" + txt); extra.append(txt)
    if args.level_sweep:
        tgs = [float(x) for x in args.sweep_targets.split(",") if x.strip()]
        sls = [float(x) for x in args.sweep_stops.split(",") if x.strip()]
        cost_pts = args.cost / LOT_SIZE                          # ₹/lot → index points
        txt = level_sweep_text(level_sweep(snap, out["triggers"], tgs, sls, LOT_SIZE,
                                           args.lots, cost_pts=cost_pts))
        print("\n" + txt); extra.append(txt)
    csv_path, md_path = write_outputs(args.symbol, out["triggers"], out["report"], args.out,
                                      levels=args.levels, filtered=out["filtered"],
                                      conf_filtered=out["conf_filtered"],
                                      min_confidence=args.min_confidence, extra="\n\n".join(extra))
    print(f"\nWrote {csv_path}\n      {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
