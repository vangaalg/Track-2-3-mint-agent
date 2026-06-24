"""Railway Postgres backend for the live time-series stores (OI chain, OI summary, macro).

When ``DATABASE_URL`` is set (Railway's Postgres plugin injects it), the OI / PCR / macro
stores read+write HERE instead of parquet: durable rows that land immediately, with no
git-sync loss window and no data lost on a redeploy. When it is UNSET (local dev, the
egress-locked sandbox, offline tests) the stores fall back to their parquet files, so this
module is never *required* to run — same graceful-degradation pattern as the loaders.

The connection factory is injectable (`set_connect`) so the routing + SQL is offline-testable
against a fake in-memory connection without a real Postgres. Schema is created lazily on the
first write, so every entry point (cockpit, recorder, CLI) just works once the env var is set.

Row shapes are kept IDENTICAL to the parquet stores (same columns, ts as an IST isoformat
string index) so the web endpoints (`/api/oi-history`, `/api/oi-download`) are unchanged.
"""

from __future__ import annotations

import json
import os

import pandas as pd

_CONNECT = None        # injectable connection factory (tests); None => real psycopg2
_conn = None           # cached live connection
_schema_ready = False

_SUMMARY_COLS = ["spot", "pcr", "max_pain", "atm", "call_wall_strike", "call_wall_oi",
                 "put_shelf_strike", "put_shelf_oi", "res_ext1", "res_ext2",
                 "sup_ext1", "sup_ext2"]
_CHAIN_COLS = ["ts", "spot", "strike", "call_oi", "put_oi", "call_ltp", "put_ltp"]


# --------------------------------------------------------------------------- #
# Connection / availability
# --------------------------------------------------------------------------- #
def set_connect(factory) -> None:
    """Inject a connection factory (tests). Pass None to reset to the real driver."""
    global _CONNECT, _conn, _schema_ready
    _CONNECT, _conn, _schema_ready = factory, None, False


def _have_driver() -> bool:
    try:
        import psycopg2  # noqa: F401
        return True
    except Exception:                                  # pragma: no cover - env dependent
        return False


def enabled() -> bool:
    """True when a Postgres backend is configured (real DATABASE_URL + driver, or injected)."""
    if _CONNECT is not None:
        return True
    return bool(os.environ.get("DATABASE_URL")) and _have_driver()


def _connect():
    if _CONNECT is not None:
        return _CONNECT()
    import psycopg2                                    # pragma: no cover - needs a real DB
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    return conn


def _get_conn():
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def _run(sql, params=None, *, fetch=False, many=False):
    """Execute SQL with one reconnect-on-failure retry (live connections go stale)."""
    global _conn
    last = None
    for attempt in (1, 2):
        try:
            cur = _get_conn().cursor()
            if many:
                cur.executemany(sql, params or [])
            else:
                cur.execute(sql, params or ())
            rows = cur.fetchall() if fetch else None
            cur.close()
            return rows
        except Exception as exc:                       # pragma: no cover - reconnect path
            last, _conn = exc, None
    raise last


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS oi_summary (
        symbol text NOT NULL, ts timestamptz NOT NULL, spot double precision,
        pcr double precision, max_pain double precision, atm double precision,
        call_wall_strike double precision, call_wall_oi double precision,
        put_shelf_strike double precision, put_shelf_oi double precision,
        res_ext1 double precision, res_ext2 double precision,
        sup_ext1 double precision, sup_ext2 double precision,
        PRIMARY KEY (symbol, ts))""",
    """CREATE TABLE IF NOT EXISTS oi_chain (
        symbol text NOT NULL, ts timestamptz NOT NULL, strike double precision NOT NULL,
        spot double precision, call_oi double precision, put_oi double precision,
        call_ltp double precision, put_ltp double precision,
        PRIMARY KEY (symbol, ts, strike))""",
    "CREATE INDEX IF NOT EXISTS oi_chain_sym_ts ON oi_chain (symbol, ts)",
    "CREATE TABLE IF NOT EXISTS macro (ts timestamptz PRIMARY KEY, data jsonb)",
]


def init_schema() -> None:
    for stmt in _SCHEMA:
        _run(stmt)


def _ensure_schema() -> None:
    global _schema_ready
    if not _schema_ready:
        init_schema()
        _schema_ready = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _f(x):
    """Coerce to float or None (NaN-safe) for numeric columns."""
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return None
        return float(x)
    except Exception:
        return None


def _ist_iso(ts) -> str:
    """A stored timestamp -> IST isoformat string, matching the parquet stores' ts index."""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tz is None else t
    return t.tz_convert("Asia/Kolkata").isoformat()


# --------------------------------------------------------------------------- #
# OI summary (PCR / max-pain / walls / bands series)
# --------------------------------------------------------------------------- #
def oi_summary_append(symbol: str, row: dict) -> pd.DataFrame | None:
    """Upsert one summary row (the dict from oi_summary_store._row), return the series."""
    _ensure_schema()
    cols = ["symbol", "ts"] + _SUMMARY_COLS
    vals = [symbol, row["ts"]] + [_f(row.get(c)) for c in _SUMMARY_COLS]
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in _SUMMARY_COLS)
    _run(f"INSERT INTO oi_summary ({', '.join(cols)}) "
         f"VALUES ({', '.join(['%s'] * len(cols))}) "
         f"ON CONFLICT (symbol, ts) DO UPDATE SET {updates}", vals)
    return oi_summary_load(symbol)


def oi_summary_load(symbol: str) -> pd.DataFrame | None:
    _ensure_schema()
    cols = ["ts"] + _SUMMARY_COLS
    rows = _run(f"SELECT {', '.join(cols)} FROM oi_summary WHERE symbol=%s ORDER BY ts",
                (symbol,), fetch=True)
    if not rows:
        return None
    df = pd.DataFrame(list(rows), columns=cols)
    df["ts"] = [_ist_iso(t) for t in df["ts"]]
    return df.set_index("ts")


# --------------------------------------------------------------------------- #
# OI chain (per-strike snapshots)
# --------------------------------------------------------------------------- #
def oi_chain_save(symbol: str, ts, spot, chain: pd.DataFrame):
    """Upsert every strike of one chain snapshot."""
    _ensure_schema()
    ts_iso = pd.Timestamp(ts).isoformat()
    recs = [(symbol, ts_iso, _f(r.get("strike")), _f(spot), _f(r.get("call_oi")),
             _f(r.get("put_oi")), _f(r.get("call_ltp")), _f(r.get("put_ltp")))
            for _, r in chain.iterrows()]
    if recs:
        _run("INSERT INTO oi_chain (symbol, ts, strike, spot, call_oi, put_oi, call_ltp, put_ltp) "
             "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
             "ON CONFLICT (symbol, ts, strike) DO UPDATE SET spot=EXCLUDED.spot, "
             "call_oi=EXCLUDED.call_oi, put_oi=EXCLUDED.put_oi, "
             "call_ltp=EXCLUDED.call_ltp, put_ltp=EXCLUDED.put_ltp", recs, many=True)
    return None


def _chain_df(rows) -> pd.DataFrame | None:
    if not rows:
        return None
    df = pd.DataFrame(list(rows), columns=_CHAIN_COLS)
    df["ts"] = [_ist_iso(t) for t in df["ts"]]
    return df


def oi_chain_history(symbol: str, day: str | None = None) -> pd.DataFrame | None:
    _ensure_schema()
    df = _chain_df(_run(
        "SELECT ts, spot, strike, call_oi, put_oi, call_ltp, put_ltp FROM oi_chain "
        "WHERE symbol=%s ORDER BY ts, strike", (symbol,), fetch=True))
    if df is None:
        return None
    if day and day != "all":
        df = df[df["ts"].str[:10] == day]
    return df if not df.empty else None


def oi_chain_nearest(symbol: str, ts, max_age_min: float | None = None) -> pd.DataFrame | None:
    _ensure_schema()
    target = pd.Timestamp(ts).isoformat()
    got = _run("SELECT max(ts) FROM oi_chain WHERE symbol=%s AND ts<=%s",
               (symbol, target), fetch=True)
    if not got or got[0][0] is None:
        return None
    snap_ts = got[0][0]
    if max_age_min is not None:
        age = (pd.Timestamp(ts) - pd.Timestamp(_ist_iso(snap_ts))).total_seconds() / 60
        if age > max_age_min:
            return None
    return _chain_df(_run(
        "SELECT ts, spot, strike, call_oi, put_oi, call_ltp, put_ltp FROM oi_chain "
        "WHERE symbol=%s AND ts=%s ORDER BY strike", (symbol, snap_ts), fetch=True))


# --------------------------------------------------------------------------- #
# Macro scorecard series
# --------------------------------------------------------------------------- #
def macro_append(row: dict) -> pd.DataFrame | None:
    """Upsert one macro snapshot (the dict from macro_store._flatten)."""
    _ensure_schema()
    data = {k: v for k, v in row.items() if k != "ts"}
    _run("INSERT INTO macro (ts, data) VALUES (%s, %s::jsonb) "
         "ON CONFLICT (ts) DO UPDATE SET data=EXCLUDED.data",
         (row["ts"], json.dumps(data)))
    return macro_load()


def macro_load() -> pd.DataFrame | None:
    _ensure_schema()
    rows = _run("SELECT ts, data FROM macro ORDER BY ts", fetch=True)
    if not rows:
        return None
    recs = []
    for ts, data in rows:
        d = data if isinstance(data, dict) else json.loads(data or "{}")
        recs.append({"ts": _ist_iso(ts), **d})
    return pd.DataFrame(recs).set_index("ts")
