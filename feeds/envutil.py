"""Robust boolean env flags (PURE).

Several features were gated on ``os.environ.get(X) == "1"`` — a trader setting the
Railway variable to ``true`` / ``yes`` silently counted as OFF (this actually happened
with RECORDER_STOCKS, and earlier with EXECUTION_LIVE). ``flag`` accepts the common
truthy spellings, case-insensitive; anything else (including unset) is the default.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "n", ""}


def flag(name: str, default: bool = False) -> bool:
    """The boolean value of an env var: 1/true/yes/on → True, 0/false/no/off → False,
    unset or unrecognised → ``default``."""
    v = os.environ.get(name)
    if v is None:
        return default
    s = v.strip().lower()
    if s in _TRUTHY:
        return True
    if s in _FALSY:
        return False
    return default


def raw(name: str) -> str | None:
    """The raw env value (diagnostics — e.g. showing a misspelled flag in a UI hint)."""
    return os.environ.get(name)
