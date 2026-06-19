"""Streamlit one-pane: source → analyse → propose → you approve.

Run locally (live data + Breeze creds on your machine):

    streamlit run dashboard/app.py

Nothing trades without your tap, and even an approved trade is **dry-run** unless
you flip the live toggle AND set EXECUTION_LIVE=1 in the environment.
"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from loaders import get_loader
from feeds.snapshot import build_snapshot
from analysis.trade1 import propose_trade1
from analysis.proposal import Recommendation
from execution import breeze_exec
from journal.log import log_decision

ANCHOR = "9h15min"


@st.cache_data(show_spinner=True)
def _pull(symbol: str):
    """Pull Breeze 1-minute base + daily (cached per session)."""
    loader = get_loader("breeze")
    today = date.today()
    base_min = loader.load(symbol, "minute", start=today - timedelta(days=10))
    daily = loader.load(symbol, "day", start=today - timedelta(days=800))
    return base_min, daily


def main() -> None:
    st.set_page_config(page_title="Nifty Agent — propose & approve", layout="wide")
    st.title("Nifty Agent — Trade 1 (propose-only)")

    with st.sidebar:
        symbol = st.text_input("Instrument", "NIFTY")
        size_lots = st.slider("Size (lots)", 65, 130, 75, step=5)
        live = st.toggle("Live execution (else dry-run)", value=False)
        st.caption("Live also needs EXECUTION_LIVE=1 in the environment.")
        if st.button("Refresh snapshot & propose", type="primary"):
            base_min, daily = _pull(symbol)
            snap = build_snapshot(symbol, base_min, daily, anchor=ANCHOR)
            st.session_state["proposal"] = propose_trade1(snap, size_lots)

    prop = st.session_state.get("proposal")
    if prop is None:
        st.info("Set size and press **Refresh snapshot & propose**.")
        return

    left, right = st.columns([2, 1])
    with left:
        st.subheader(f"{prop.instrument} — spot {prop.spot}  ·  read: {prop.direction}")
        st.write("**Why:**")
        for r in prop.reasons:
            st.write(f"- {r}")
        if prop.context.get("notes"):
            st.warning("  ·  ".join(prop.context["notes"]))
        st.write("**Six-line check:**")
        st.table({k: [v] for k, v in prop.checklist.items()})

    with right:
        if prop.recommendation is Recommendation.ENTER:
            st.success("RECOMMENDATION: ENTER")
        else:
            st.error("RECOMMENDATION: STAND DOWN  (no-trade is a win)")
        st.metric("Entry", prop.entry)
        st.metric("Stop", prop.stop)
        st.metric("Target", prop.target)
        st.metric("R:R", prop.rr_ratio)
        st.metric("Approx ₹ risk", prop.rupee_risk)
        st.write(f"**Vehicle:** {prop.vehicle}")

    st.divider()
    if prop.recommendation is Recommendation.ENTER:
        a, b = st.columns(2)
        if a.button("✅ Approve & place", type="primary"):
            result = breeze_exec.place(prop, live=live)
            log_decision(prop, "approved", execution=result)
            st.success(f"Logged. Execution: {result['status']}")
            st.json(result)
        if b.button("❌ Reject (stand down)"):
            log_decision(prop, "rejected")
            st.info("Logged a no-trade — a good decision.")
    else:
        if st.button("Log stand-down"):
            log_decision(prop, "rejected")
            st.info("Logged.")


if __name__ == "__main__":
    main()
