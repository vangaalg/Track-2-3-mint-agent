"""Breeze loader — Indian instruments (Nifty, Bank Nifty, Fin Nifty, F&O stocks).

Real ICICI Direct **Breeze** historical-data pull, ported into the repo so the
Indian instruments run without a separate ``breeze_pull.py`` script. Credentials
come from the environment:

    BREEZE_API_KEY       (a.k.a. App Key)
    BREEZE_API_SECRET    (a.k.a. Secret Key)
    BREEZE_SESSION_TOKEN (the daily api-session token from the login flow)

Missing creds raise a clear ``RuntimeError`` so the Stage-1 sweep skips the
instrument (global instruments still run). Live pulls happen on the user's local
machine — this web env is egress-locked, so the HTTP call is exercised via a
mocked ``requests`` in tests.

Breeze has **no native 3-minute interval**, so a ``3min``/``3minute`` request
pulls ``1minute`` bars and resamples to 3-min locally (midnight-anchored 3-min
bins line up with the 9:15 NSE open, since 9:15 is divisible by 3).

A legacy ``pull_fn`` / ``breeze_pull`` hook is still honoured as a fallback when
no creds are configured but a pull function is supplied.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import date, datetime
from typing import Callable

import pandas as pd

from loaders.base import OHLCVLoader

_BASE_URL = "https://api.icicidirect.com/breezeapi/api/v1"
_HIST_ENDPOINT = f"{_BASE_URL}/historicalcharts"

# Our canonical interval names -> Breeze interval strings. Breeze has no 3min,
# so 3min is pulled as 1minute and resampled (see _fetch).
_INTERVAL_MAP = {
    "1minute": "1minute",
    "1min": "1minute",
    "3minute": "1minute",   # resampled to 3min after the pull
    "3min": "1minute",
    "5minute": "5minute",
    "5min": "5minute",
    "30minute": "30minute",
    "30min": "30minute",
    "1day": "1day",
    "1d": "1day",
    "day": "1day",
}
_RESAMPLE_TO_3MIN = {"3minute", "3min"}


class BreezeLoader(OHLCVLoader):
    source = "breeze"

    def __init__(
        self,
        pull_fn: Callable | None = None,
        cache_dir="data",
        api_key: str | None = None,
        api_secret: str | None = None,
        session_token: str | None = None,
        exchange_code: str = "NSE",
        product_type: str = "cash",
        tz: str = "Asia/Kolkata",
    ):
        super().__init__(cache_dir=cache_dir)
        self._pull_fn = pull_fn
        self.api_key = api_key or os.environ.get("BREEZE_API_KEY")
        self.api_secret = api_secret or os.environ.get("BREEZE_API_SECRET")
        self.session_token = session_token or os.environ.get("BREEZE_SESSION_TOKEN")
        self.exchange_code = exchange_code
        self.product_type = product_type
        self.tz = tz

    # --- legacy hook ------------------------------------------------------- #
    def _resolve_pull_fn(self) -> Callable | None:
        """Return an explicit/legacy pull function if one is available, else None.

        Used only as a fallback when API creds are absent; the primary path is
        the native HTTP pull in ``_fetch``.
        """
        if self._pull_fn is not None:
            return self._pull_fn
        try:
            import breeze_pull  # type: ignore
        except ImportError:
            return None
        for name in ("pull", "fetch", "get_history", "pull_ohlcv"):
            fn = getattr(breeze_pull, name, None)
            if callable(fn):
                return fn
        return None

    # --- HTTP auth --------------------------------------------------------- #
    def _headers(self, body: str) -> dict:
        """Breeze checksum auth headers for a given JSON body string."""
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        raw = timestamp + body + self.api_secret
        checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        token = base64.b64encode(
            f"{self.api_key}:{self.session_token}".encode("utf-8")
        ).decode("utf-8")
        return {
            "Content-Type": "application/json",
            "X-Checksum": "token " + checksum,
            "X-Timestamp": timestamp,
            "X-AppKey": self.api_key,
            "X-SessionToken": token,
        }

    @staticmethod
    def _to_iso(value, end: bool) -> str:
        """Coerce a date/datetime/str into Breeze's ISO ``...T..:..:..000Z``."""
        if value is None:
            value = date.today()
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, date):
            dt = datetime(value.year, value.month, value.day,
                          23 if end else 0, 59 if end else 0, 59 if end else 0)
        else:  # already a string — trust the caller
            return str(value)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # --- fetch ------------------------------------------------------------- #
    def _fetch(self, symbol: str, interval: str, start, end) -> pd.DataFrame:
        breeze_interval = _INTERVAL_MAP.get(interval, interval)

        if not (self.api_key and self.api_secret and self.session_token):
            # Fall back to a legacy pull function if one is wired up.
            legacy = self._resolve_pull_fn()
            if legacy is not None:
                df = legacy(symbol, interval, start, end)
            else:
                raise RuntimeError(
                    "BreezeLoader needs creds: set BREEZE_API_KEY, "
                    "BREEZE_API_SECRET and BREEZE_SESSION_TOKEN (or pass them to "
                    "BreezeLoader / provide pull_fn=...)."
                )
        else:
            df = self._http_pull(symbol, breeze_interval, start, end)

        if interval in _RESAMPLE_TO_3MIN:
            df = self._to_3min(df)
        return df

    def _http_pull(self, symbol, breeze_interval, start, end) -> pd.DataFrame:
        # Imported lazily so the package imports without `requests` installed.
        import requests

        body_dict = {
            "interval": breeze_interval,
            "from_date": self._to_iso(start, end=False),
            "to_date": self._to_iso(end, end=True),
            "stock_code": symbol,
            "exchange_code": self.exchange_code,
            "product_type": self.product_type,
        }
        body = json.dumps(body_dict, separators=(",", ":"))
        resp = requests.get(  # Breeze reads the auth body from the request body
            _HIST_ENDPOINT, data=body, headers=self._headers(body), timeout=30
        )
        resp.raise_for_status()
        payload = resp.json()
        if str(payload.get("Status", 200)) not in ("200", "None"):
            raise RuntimeError(f"Breeze error: {payload.get('Error')}")

        rows = payload.get("Success") or []
        if not rows:
            raise RuntimeError(f"Breeze returned no data for {symbol!r}")

        df = pd.DataFrame(rows)
        # Breeze rows: datetime, open, high, low, close, volume (+ extras).
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df = df[["datetime", "open", "high", "low", "close", "volume"]].copy()
        # Localise naive Breeze timestamps to the exchange tz before _normalise.
        idx = pd.to_datetime(df["datetime"])
        if idx.dt.tz is None:
            idx = idx.dt.tz_localize(self.tz)
        df["datetime"] = idx
        return df

    def _to_3min(self, df: pd.DataFrame) -> pd.DataFrame:
        """Resample a 1-minute frame to 3-minute OHLCV (midnight-anchored bins)."""
        from indicators.timeframes import resample_ohlcv

        work = df.copy()
        if not isinstance(work.index, pd.DatetimeIndex):
            work = work.set_index("datetime")
        work.index = pd.to_datetime(work.index)
        work.columns = [str(c).lower() for c in work.columns]
        return resample_ohlcv(work, "3min")
