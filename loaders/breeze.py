"""Breeze loader — Indian instruments (Nifty, Bank Nifty, Fin Nifty, F&O stocks).

Uses ICICI Direct's official **breeze-connect** SDK, which performs the daily
session handshake (exchange the ``API_Session`` for a real session token via
``generate_session``) and the checksum auth for us — the part a hand-rolled HTTP
client gets wrong ("Invalid User Details"). Credentials come from the env:

    BREEZE_API_KEY       (App Key)
    BREEZE_API_SECRET    (Secret Key)
    BREEZE_SESSION_TOKEN (the daily API_Session from the login flow — expires daily)

Install the SDK:  ``pip install breeze-connect``

Breeze has **no native 3-minute interval**, so a ``3min``/``3minute`` request
pulls ``1minute`` bars and resamples to 3-min locally (midnight-anchored 3-min
bins line up with the 9:15 NSE open, since 9:15 is divisible by 3).

A legacy ``pull_fn`` / ``breeze_pull`` hook is still honoured as a fallback when
no creds are configured but a pull function is supplied.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Callable

import pandas as pd

from loaders.base import OHLCVLoader

# Our canonical interval names -> breeze-connect get_historical_data_v2 intervals.
# The v2 endpoint accepts: 1second, 1minute, 5minute, 30minute, 1day. No native
# 3min, so 3min is pulled as "1minute" and resampled (see _fetch).
_INTERVAL_MAP = {
    "minute": "1minute",
    "1minute": "1minute",
    "1min": "1minute",
    "3minute": "1minute",   # resampled to 3min after the pull
    "3min": "1minute",
    "5minute": "5minute",
    "5min": "5minute",
    "30minute": "30minute",
    "30min": "30minute",
    "day": "1day",
    "1day": "1day",
    "1d": "1day",
}
_RESAMPLE_TO_3MIN = {"3minute", "3min"}


def get_breeze_client(api_key=None, api_secret=None, session_token=None):
    """Return a session-authenticated ``BreezeConnect`` client (creds from env).

    Shared auth path for both the OHLCV loader and the option-chain feed so the
    session handshake lives in one place. Raises a clear ``RuntimeError`` when the
    SDK is missing or the daily ``API_Session`` token is rejected.
    """
    api_key = api_key or os.environ.get("BREEZE_API_KEY")
    api_secret = api_secret or os.environ.get("BREEZE_API_SECRET")
    session_token = session_token or os.environ.get("BREEZE_SESSION_TOKEN")
    if not (api_key and api_secret and session_token):
        raise RuntimeError(
            "Breeze needs creds: set BREEZE_API_KEY, BREEZE_API_SECRET and "
            "BREEZE_SESSION_TOKEN."
        )
    try:
        from breeze_connect import BreezeConnect
    except ImportError as exc:
        raise RuntimeError(
            "Breeze needs the official SDK: pip install breeze-connect"
        ) from exc

    client = BreezeConnect(api_key=api_key)
    try:
        client.generate_session(api_secret=api_secret, session_token=session_token)
    except Exception as exc:
        raise RuntimeError(
            "Breeze session failed — check BREEZE_SESSION_TOKEN is today's fresh "
            f"API_Session and BREEZE_API_KEY/SECRET are correct: {exc}"
        ) from exc
    return client


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
        self._breeze = None  # cached, session-authenticated SDK client

    # --- legacy hook ------------------------------------------------------- #
    def _resolve_pull_fn(self) -> Callable | None:
        """Return an explicit/legacy pull function if available, else None.

        Used only when API creds are absent; the primary path is the SDK.
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

    # --- SDK session ------------------------------------------------------- #
    def _session(self):
        """Return a session-authenticated BreezeConnect client (cached)."""
        if self._breeze is None:
            self._breeze = get_breeze_client(
                self.api_key, self.api_secret, self.session_token
            )
        return self._breeze

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
        sdk_interval = _INTERVAL_MAP.get(interval, interval)

        if self.api_key and self.api_secret and self.session_token:
            df = self._sdk_pull(symbol, sdk_interval, start, end)
        else:
            legacy = self._resolve_pull_fn()
            if legacy is None:
                raise RuntimeError(
                    "BreezeLoader needs creds: set BREEZE_API_KEY, "
                    "BREEZE_API_SECRET and BREEZE_SESSION_TOKEN (or pass pull_fn=...)."
                )
            df = legacy(symbol, interval, start, end)

        if interval in _RESAMPLE_TO_3MIN:
            df = self._to_3min(df)
        return df

    def _sdk_pull(self, symbol, sdk_interval, start, end) -> pd.DataFrame:
        result = self._session().get_historical_data_v2(
            interval=sdk_interval,
            from_date=self._to_iso(start, end=False),
            to_date=self._to_iso(end, end=True),
            stock_code=symbol,
            exchange_code=self.exchange_code,
            product_type=self.product_type,
        )
        if result.get("Error"):
            raise RuntimeError(f"Breeze error: {result.get('Error')}")
        rows = result.get("Success") or []
        if not rows:
            raise RuntimeError(
                f"Breeze returned no data for {symbol!r} ({sdk_interval}) — check "
                "stock_code/exchange_code/product_type and the date window."
            )

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
