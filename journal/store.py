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
    "logged_at", "ts", "symbol", "kind", "decision", "recommendation", "direction",
    "entry", "stop", "target", "size_lots", "rr_ratio", "vehicle", "spot",
    "confidence", "final_confidence", "agrees_with_engine",
    "process_grade", "matrix", "outcome_status", "outcome_points", "outcome_rupees",
    # Phase 2 — the trader's genuine/false trigger label + Claude's post-outcome reason.
    "trigger_label", "reason_why",
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
        "logged_at TEXT", "ts TEXT", "symbol TEXT", "kind TEXT", "decision TEXT",
        "recommendation TEXT", "direction TEXT",
        "entry REAL", "stop REAL", "target REAL", "size_lots INTEGER",
        "rr_ratio REAL", "vehicle TEXT", "spot REAL",
        "confidence INTEGER", "final_confidence INTEGER", "agrees_with_engine INTEGER",
        "process_grade TEXT", "matrix TEXT",
        "outcome_status TEXT", "outcome_points REAL", "outcome_rupees REAL",
        "trigger_label TEXT", "reason_why TEXT",
    ] + [f"{c}_json TEXT" for c in _BLOB_COLS]
    with _connect(path) as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS decisions ({', '.join(cols)})")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts)")
        # Migration: add columns to a pre-existing DB that lacks them.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
        for c in ("kind", "trigger_label", "reason_why"):
            if c not in have:
                conn.execute(f"ALTER TABLE decisions ADD COLUMN {c} TEXT")
        if "final_confidence" not in have:   # engine conviction (0-5) — for analysis
            conn.execute("ALTER TABLE decisions ADD COLUMN final_confidence INTEGER")


def save_decision(payload: dict, path: str | Path = DB_PATH) -> int:
    """Persist one full-context decision record; return its row id.

    ``payload`` carries the structures (proposal, claude_read, chat, chart, chain,
    macro, oi_summary, notes, execution); the scalar columns are derived from it.
    """
    init_db(path)
    prop = payload.get("proposal") or {}
    read = payload.get("claude_read") or {}
    outc = payload.get("outcome") or {}
    row = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "ts": payload.get("ts") or prop.get("ts"),
        "symbol": payload.get("symbol") or prop.get("instrument"),
        "kind": payload.get("kind") or "live",
        "decision": payload.get("decision"),
        "recommendation": prop.get("recommendation"),
        "direction": prop.get("direction"),
        "entry": prop.get("entry"), "stop": prop.get("stop"),
        "target": prop.get("target"), "size_lots": prop.get("size_lots"),
        "rr_ratio": prop.get("rr_ratio"), "vehicle": prop.get("vehicle"),
        "spot": payload.get("spot") if payload.get("spot") is not None else prop.get("spot"),
        "confidence": read.get("confidence"),
        # Engine conviction (mtf 45-EMA alignment + OI boost, 0-5) — a queryable column
        # so future analysis can correlate the agent's conviction with the outcome.
        "final_confidence": (prop.get("final_confidence")
                             if prop.get("final_confidence") is not None
                             else prop.get("mtf_confidence")),
        "agrees_with_engine": (None if read.get("agrees_with_engine") is None
                               else int(bool(read.get("agrees_with_engine")))),
        # Live decisions settle later (None); training records carry their grade now.
        "process_grade": payload.get("process_grade"), "matrix": payload.get("matrix"),
        "outcome_status": outc.get("status"), "outcome_points": outc.get("points"),
        "outcome_rupees": outc.get("rupees"),
        # Phase 2 — trader's genuine/false trigger label + Claude's reason-why text.
        "trigger_label": payload.get("trigger_label"),
        "reason_why": payload.get("reason_why"),
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


def update_reason(row_id: int, reason_why: str | None,
                  trigger_label: str | None = None, path: str | Path = DB_PATH) -> None:
    """Fill in Claude's post-outcome reason-why (and optional trader label) on a row.

    Live decisions are saved before the trade resolves, so the reason-why is generated
    later (at settle) and patched in here; ``trigger_label`` is only overwritten when
    a non-None value is passed (so a trader label set earlier survives)."""
    with _connect(path) as conn:
        if trigger_label is None:
            conn.execute("UPDATE decisions SET reason_why=? WHERE id=?", (reason_why, row_id))
        else:
            conn.execute("UPDATE decisions SET reason_why=?, trigger_label=? WHERE id=?",
                         (reason_why, trigger_label, row_id))


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for c in _BLOB_COLS:
        raw = d.pop(f"{c}_json", None)
        d[c] = json.loads(raw) if raw else None
    if d.get("agrees_with_engine") is not None:
        d["agrees_with_engine"] = bool(d["agrees_with_engine"])
    return d


def load_records(path: str | Path = DB_PATH, limit: int | None = None,
                 kind: str | None = None, symbol: str | None = None) -> list[dict]:
    """Read decision rows (newest last), JSON columns parsed back to objects.

    ``kind`` optionally filters to "live" or "training" (legacy rows have NULL kind,
    treated as "live"). ``symbol`` optionally scopes to one instrument (the cockpit's
    per-instrument track record).
    """
    p = Path(path)
    if not p.exists():
        return []
    init_db(path)            # the file may exist with only the trigger_reads table — ensure decisions
    clauses, params = [], []
    if kind == "live":
        clauses.append("(kind='live' OR kind IS NULL)")
    elif kind:
        clauses.append("kind=?")
        params.append(kind)
    if symbol:
        clauses.append("symbol=?")
        params.append(symbol)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    q = f"SELECT * FROM decisions {where} ORDER BY id"
    if limit:
        q = (f"SELECT * FROM (SELECT * FROM decisions {where} ORDER BY id DESC "
             f"LIMIT {int(limit)}) ORDER BY id")
    with _connect(path) as conn:
        return [_row_to_dict(r) for r in conn.execute(q, params).fetchall()]


# --------------------------------------------------------------------------- #
# Trigger reads — Claude's frozen per-trigger read (separate table so it never
# pollutes the decisions/track-record queries). Survives a restart.
# --------------------------------------------------------------------------- #
def init_trigger_reads(path: str | Path = DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS trigger_reads ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, logged_at TEXT, symbol TEXT, "
            "strategy TEXT, ts TEXT, read_json TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_treads_sym ON trigger_reads(symbol)")


def save_trigger_read(symbol: str, strategy: str, ts: str, read: dict | None,
                      path: str | Path = DB_PATH) -> None:
    """Append one frozen trigger read. Re-asks append a fresh row (load returns latest-wins)."""
    init_trigger_reads(path)
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO trigger_reads (logged_at, symbol, strategy, ts, read_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), symbol, strategy, ts,
             json.dumps(read) if read is not None else None))


def load_trigger_reads(symbol: str, path: str | Path = DB_PATH) -> list[dict]:
    """All persisted reads for ``symbol`` (newest last), each ``{strategy, ts, read}``."""
    if not Path(path).exists():
        return []
    init_trigger_reads(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT strategy, ts, read_json FROM trigger_reads WHERE symbol=? ORDER BY id",
            (symbol,)).fetchall()
    return [{"strategy": r["strategy"], "ts": r["ts"],
             "read": json.loads(r["read_json"]) if r["read_json"] else None} for r in rows]


# --------------------------------------------------------------------------- #
# Market reads — Claude's on-demand "Market view" reads (no trigger). Own table
# so the day's reads can be browsed/re-opened. ``ts`` is the IST moment the read
# was generated (used for the day picker + display).
# --------------------------------------------------------------------------- #
def init_market_reads(path: str | Path = DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS market_reads ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, logged_at TEXT, symbol TEXT, "
            "ts TEXT, read_json TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_mreads_sym ON market_reads(symbol)")


def save_market_read(symbol: str, ts: str, read: dict | None,
                     path: str | Path = DB_PATH) -> None:
    """Append one on-demand market read (``ts`` = IST moment it was generated)."""
    init_market_reads(path)
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO market_reads (logged_at, symbol, ts, read_json) "
            "VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), symbol, ts,
             json.dumps(read) if read is not None else None))


def load_market_reads(symbol: str, path: str | Path = DB_PATH) -> list[dict]:
    """All persisted market reads for ``symbol`` (oldest first), each ``{ts, read}``."""
    if not Path(path).exists():
        return []
    init_market_reads(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, read_json FROM market_reads WHERE symbol=? ORDER BY id",
            (symbol,)).fetchall()
    return [{"ts": r["ts"],
             "read": json.loads(r["read_json"]) if r["read_json"] else None} for r in rows]


# --------------------------------------------------------------------------- #
# Live broker positions — the open, broker-backed trades the exit-monitor manages.
# Persisted so a Railway restart can reconcile against the broker (own table so it
# never touches the decision/track-record queries). One open row per (symbol,
# strategy); ``close_live_position`` flips status when the exit fills.
# --------------------------------------------------------------------------- #
_LIVE_COLS = ("symbol", "strategy", "ts", "segment", "direction", "entry", "stop",
              "target", "tsl_points", "qty", "broker_order_id", "status")


def init_live_positions(path: str | Path = DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS live_positions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, logged_at TEXT, symbol TEXT, "
            "strategy TEXT, ts TEXT, segment TEXT, direction TEXT, entry REAL, stop REAL, "
            "target REAL, tsl_points REAL, qty INTEGER, broker_order_id TEXT, "
            "status TEXT, raw_json TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_livepos_open "
                     "ON live_positions(status)")


def save_live_position(pos: dict, path: str | Path = DB_PATH) -> int:
    """Persist (append) one OPEN live position; returns its row id."""
    init_live_positions(path)
    row = {c: pos.get(c) for c in _LIVE_COLS}
    row["status"] = row.get("status") or "open"
    row["logged_at"] = datetime.now(timezone.utc).isoformat()
    row["raw_json"] = json.dumps(pos)
    cols = list(row.keys())
    with _connect(path) as conn:
        cur = conn.execute(
            f"INSERT INTO live_positions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols])
        return int(cur.lastrowid)


def load_open_live_positions(path: str | Path = DB_PATH) -> list[dict]:
    """Every still-open live position (the boot reconciliation source)."""
    if not Path(path).exists():
        return []
    init_live_positions(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM live_positions WHERE status='open' ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        raw = d.pop("raw_json", None)
        d["raw"] = json.loads(raw) if raw else None
        out.append(d)
    return out


def close_live_position(symbol: str, strategy: str, ts: str,
                        path: str | Path = DB_PATH) -> None:
    """Mark the open (symbol, strategy, ts) position closed (exit filled / reconciled out)."""
    if not Path(path).exists():
        return
    init_live_positions(path)
    with _connect(path) as conn:
        conn.execute(
            "UPDATE live_positions SET status='closed' "
            "WHERE symbol=? AND strategy=? AND ts=? AND status='open'",
            (symbol, strategy, ts))
