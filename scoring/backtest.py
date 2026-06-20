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
from datetime import date, timedelta

import pandas as pd

from analysis.triggers import list_triggers
from analysis.trade1 import LOT_SIZE
from indicators.directional import journal_mtf_config
from feeds.snapshot import build_snapshot


def _stats(rows: list[dict], lot_size: int, lots: int) -> dict:
    wins = [r for r in rows if r["outcome"] == "win"]
    losses = [r for r in rows if r["outcome"] == "loss"]
    opens = [r for r in rows if r["outcome"] == "open"]
    decided = len(wins) + len(losses)
    net_pts = round(sum(r["points"] for r in rows), 2)
    gross_win = sum(r["points"] for r in wins)
    gross_loss = sum(r["points"] for r in losses)   # negative
    return {
        "n": len(rows), "wins": len(wins), "losses": len(losses), "open": len(opens),
        "hit_rate": round(len(wins) / decided, 3) if decided else None,
        "net_points": net_pts,
        "net_rupees": round(net_pts * lot_size * lots, 0),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else None,
        "expectancy": round(net_pts / decided, 2) if decided else None,
        "profit_factor": round(gross_win / abs(gross_loss), 2) if gross_loss else None,
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


def run_backtest(snap, lots: int = 1, cfg=None) -> dict:
    """Backtest the trigger over a pre-built snapshot's feats/frames.

    Reuses the LIVE resolver (``journal_mtf_config``) so the backtest fires exactly the
    triggers the cockpit + training would. Returns ``{"triggers": [...], "report": {...}}``.
    """
    cfg = cfg or journal_mtf_config()
    triggers = list_triggers(snap.feats, snap.frames, cfg=cfg, size_lots=lots, lot_size=LOT_SIZE)
    return {"triggers": triggers, "report": aggregate(triggers, LOT_SIZE, lots)}


# --------------------------------------------------------------------------- #
# CLI — pull + report
# --------------------------------------------------------------------------- #
def _pull(symbol: str, days: int, loader_name: str):
    """Pull ~`days` of 1-minute base + a long daily history (runs on the user's box)."""
    from loaders import get_loader
    loader = get_loader(loader_name)
    base = loader.load(symbol, "minute", start=date.today() - timedelta(days=days + 4),
                       use_cache=False)
    daily = loader.load(symbol, "day", start=date.today() - timedelta(days=800),
                        use_cache=False)
    return base, daily


def _fmt(s: dict) -> str:
    hit = "—" if s["hit_rate"] is None else f"{s['hit_rate'] * 100:.0f}%"
    return (f"n={s['n']}  W/L/O={s['wins']}/{s['losses']}/{s['open']}  hit={hit}  "
            f"net={s['net_points']:+.1f} pts (₹{s['net_rupees']:+,.0f})  "
            f"exp={s['expectancy']} pf={s['profit_factor']}")


def report_text(symbol: str, report: dict) -> str:
    o = report["overall"]
    lines = [f"Backtest — {symbol}  (breakout-pullback, session-low stop, "
             f"{report['lots']} lot × {report['lot_size']})", ""]
    lines.append(f"OVERALL  {_fmt(o)}")
    lines.append(f"  long   {_fmt(report['by_direction']['long'])}")
    lines.append(f"  short  {_fmt(report['by_direction']['short'])}")
    lines.append("")
    lines.append("Per day:")
    for d in report["by_day"]:
        lines.append(f"  {d['date']}  {_fmt(d)}")
    return "\n".join(lines)


def write_outputs(symbol: str, triggers: list[dict], report: dict, outdir: str) -> tuple[str, str]:
    from pathlib import Path
    Path(outdir).mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    csv_path = f"{outdir}/backtest_{symbol}_{stamp}.csv"
    md_path = f"{outdir}/backtest_{symbol}_{stamp}.md"
    pd.DataFrame(triggers).to_csv(csv_path, index=False)
    Path(md_path).write_text("```\n" + report_text(symbol, report) + "\n```\n")
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=30, help="lookback window (~1 month default)")
    ap.add_argument("--loader", default="breeze", help="loaders.get_loader name (breeze/twelvedata)")
    ap.add_argument("--lots", type=int, default=1, help="position size for the ₹ column")
    ap.add_argument("--out", default="results", help="output dir for the CSV + markdown")
    args = ap.parse_args(argv)

    print(f"Pulling ~{args.days}d of {args.symbol} 1-min via '{args.loader}' "
          f"(needs creds + network)…", file=sys.stderr)
    base, daily = _pull(args.symbol, args.days, args.loader)
    snap = build_snapshot(args.symbol, base, daily, mtf_cfg=journal_mtf_config())
    out = run_backtest(snap, lots=args.lots)
    print(report_text(args.symbol, out["report"]))
    csv_path, md_path = write_outputs(args.symbol, out["triggers"], out["report"], args.out)
    print(f"\nWrote {csv_path}\n      {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
