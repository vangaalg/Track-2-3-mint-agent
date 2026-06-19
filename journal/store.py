"""The full-context decision store — a SQLite "save everything" archive.

The JSONL log (``journal.log``) keeps a thin proposal+decision record for the fast
learning loop. This store keeps the *whole picture* at the moment a trade decision is
made, so the agent can learn from — and a future Training-Mode replay can reconstruct
— exactly what was on screen:

  * the proposal (entry/stop/target/size/vehicle) and the trader's approve/reject,
  * Claude's full structured read (chart/OI/where/trade/challenge/risk + verdict),
  * the entire user<->Claude chat transcript leading to the call,
  * the chart datapoints (multi-TF OHLCV + every indicator),
  * the raw per-strike option chain (call/put OI + LTP),
  * every macro value (India VIX, USD/INR, US30/Dow, Nasdaq, crude…).

One row per decision. Queryable scalar columns mirror the most-filtered fields; the
bulky structures live in JSON blob columns. Outcomes are filled in later by
``journal.outcomes.settle_store`` (the same 2x2 grading the JSONL loop uses), so the
rich store carries the win/loss + process×outcome cell too. Stdlib ``sqlite3`` only —
no new dependencies, and the file opens in any SQLite viewer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = "results/journal.db"

# Scalar columns mirrored out of the payload for easy querying/filtering.
_SCALAR_COLS = (
    "logged_at", "ts", "symbol", "decision", "recommendation", "direction",
    "entry", "stop", "target", "size_lots", "rr_ratio", "vehicle", "spot",
    "confidence", "agrees_with_engine",
    "process_grade", "matrix", "outcome_status", "outcome_points", "outcome_rupees",
)
# JSON blob columns holding the full structures (stored as ``<name>_json`` TEXT).
_BLOB_COLS = (
    "proposal", "claude_read", "chat", "chart", "chain",
    "macro", "oi_summary", "notes", "execution", "outcome",
)


def _connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | Path = DB_PATH) -> None:
    """Create the ``decisions`` table if it does not exist."""
    cols = [
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "logged_at TEXT", "ts TEXT", "symbol TEXT", "decision TEXT",
        "recommendation TEXT", "direction TEXT",
        "entry REAL", "stop REAL", "target REAL", "size_lots INTEGER",
        "rr_ratio REAL", "vehicle TEXT", "spot REAL",
        "confidence INTEGER", "agrees_with_engine INTEGER",
        "process_grade TEXT", "matrix TEXT",
        "outcome_status TEXT", "outcome_points REAL", "outcome_rupees REAL",
    ] + [f"{c}_json TEXT" for c in _BLOB_COLS]
    with _connect(path) as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS decisions ({', '.join(cols)})")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts)")


def save_decision(payload: dict, path: str | Path = DB_PATH) -> int:
    """Persist one full-context decision record; return its row id.

    ``payload`` carries the structures (proposal, claude_read, chat, chart, chain,
    macro, oi_summary, notes, execution); the scalar columns are derived from it.
    """
    init_db(path)
    prop = payload.get("proposal") or {}
    read = payload.get("claude_read") or {}
    row = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "ts": payload.get("ts") or prop.get("ts"),
        "symbol": payload.get("symbol") or prop.get("instrument"),
        "decision": payload.get("decision"),
        "recommendation": prop.get("recommendation"),
        "direction": prop.get("direction"),
        "entry": prop.get("entry"), "stop": prop.get("stop"),
        "target": prop.get("target"), "size_lots": prop.get("size_lots"),
        "rr_ratio": prop.get("rr_ratio"), "vehicle": prop.get("vehicle"),
        "spot": payload.get("spot") if payload.get("spot") is not None else prop.get("spot"),
        "confidence": read.get("confidence"),
        "agrees_with_engine": (None if read.get("agrees_with_engine") is None
                               else int(bool(read.get("agrees_with_engine")))),
        "process_grade": None, "matrix": None,
        "outcome_status": None, "outcome_points": None, "outcome_rupees": None,
    }
    for c in _BLOB_COLS:
        row[f"{c}_json"] = json.dumps(payload.get(c)) if payload.get(c) is not None else None

    cols = list(row.keys())
    with _connect(path) as conn:
        cur = conn.execute(
            f"INSERT INTO decisions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols])
        return int(cur.lastrowid)


def update_outcome(row_id: int, outcome: dict, process_grade: str | None,
                   matrix: str | None, path: str | Path = DB_PATH) -> None:
    """Fill in the settled outcome + process/2x2 grade on an existing row."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE decisions SET outcome_json=?, outcome_status=?, outcome_points=?, "
            "outcome_rupees=?, process_grade=?, matrix=? WHERE id=?",
            (json.dumps(outcome), outcome.get("status"), outcome.get("points"),
             outcome.get("rupees"), process_grade, matrix, row_id))


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for c in _BLOB_COLS:
        raw = d.pop(f"{c}_json", None)
        d[c] = json.loads(raw) if raw else None
    if d.get("agrees_with_engine") is not None:
        d["agrees_with_engine"] = bool(d["agrees_with_engine"])
    return d


def load_records(path: str | Path = DB_PATH, limit: int | None = None) -> list[dict]:
    """Read decision rows (newest last), JSON columns parsed back to objects."""
    p = Path(path)
    if not p.exists():
        return []
    q = "SELECT * FROM decisions ORDER BY id"
    if limit:
        q = f"SELECT * FROM (SELECT * FROM decisions ORDER BY id DESC LIMIT {int(limit)}) ORDER BY id"
    with _connect(path) as conn:
        return [_row_to_dict(r) for r in conn.execute(q).fetchall()]
