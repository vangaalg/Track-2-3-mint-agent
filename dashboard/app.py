"""Streamlit one-pane: source → analyse → propose → spar → you approve.

Run locally (live data + creds on your machine):

    streamlit run dashboard/app.py

Needs BREEZE_* (OHLCV + option chain), TWELVEDATA_API_KEY (macro), and
ANTHROPIC_API_KEY (Claude sparring). Nothing trades without your tap, and even an
approved trade is **dry-run** unless you flip the live toggle AND set
EXECUTION_LIVE=1 in the environment.
"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from loaders import get_loader
from feeds.snapshot import build_snapshot
from feeds.breeze_oi import make_chain_fetcher
from feeds.td_macro import make_quote_fn, SCORECARD_SYMBOLS
from analysis.trade1 import propose_trade1
from analysis.proposal import Recommendation
from execution import breeze_exec
from journal.log import log_decision, DEFAULT_LOG
from agent.memory import load_decisions, distill_memory
from agent.read import claude_read
from agent.chat import spar_turn

ANCHOR = "9h15min"
EXPIRY_WEEKDAY = 3  # Thursday (Mon=0..Sun=6) — verify your Nifty weekly weekday


@st.cache_data(show_spinner=True)
def _pull(symbol: str):
    """Pull Breeze 1-minute base + daily (cached per session)."""
    loader = get_loader("breeze")
    today = date.today()
    base_min = loader.load(symbol, "minute", start=today - timedelta(days=10))
    daily = loader.load(symbol, "day", start=today - timedelta(days=800))
    return base_min, daily


def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _render_oi_macro(ctx: dict) -> None:
    oi = ctx.get("oi")
    if oi:
        cw = oi.get("call_wall") or {}
        ps = oi.get("put_shelf") or {}
        st.write(
            f"**OI** — PCR {oi.get('pcr'):.2f} · call wall {cw.get('strike')} · "
            f"put shelf {ps.get('strike')} · max-pain {oi.get('max_pain')}"
        )
    macro = ctx.get("macro")
    if macro:
        st.write(
            "**Macro** — "
            + " · ".join(
                f"{k} {_fmt_pct((v or {}).get('change_pct'))}" for k, v in macro.items()
            )
        )
    if not oi and not macro and ctx.get("notes"):
        st.warning("  ·  ".join(ctx["notes"]))


def main() -> None:
    st.set_page_config(page_title="Nifty Agent — propose & approve", layout="wide")
    st.title("Nifty Agent — Trade 1 (propose-only)")

    with st.sidebar:
        symbol = st.text_input("Instrument", "NIFTY")
        size_lots = st.slider("Size (lots)", 65, 130, 75, step=5)
        live = st.toggle("Live execution (else dry-run)", value=False)
        st.caption("Live also needs EXECUTION_LIVE=1 in the environment.")
        spar = st.toggle("Claude sparring (ANTHROPIC_API_KEY)", value=True)
        if st.button("Refresh snapshot & propose", type="primary"):
            base_min, daily = _pull(symbol)
            snap = build_snapshot(
                symbol, base_min, daily, anchor=ANCHOR,
                oi_fetch_fn=make_chain_fetcher(weekday=EXPIRY_WEEKDAY),
                macro_quote_fn=make_quote_fn(),
                macro_symbols=SCORECARD_SYMBOLS,
            )
            prop = propose_trade1(snap, size_lots)
            memory = distill_memory(load_decisions(DEFAULT_LOG))
            st.session_state.update(
                proposal=prop, snapshot=snap, memory=memory,
                chat=[], claude_read=None, claude_error=None,
            )
            if spar:
                try:
                    st.session_state["claude_read"] = claude_read(snap, prop, memory)
                except Exception as exc:  # missing key / network / SDK
                    st.session_state["claude_error"] = str(exc)

    prop = st.session_state.get("proposal")
    if prop is None:
        st.info("Set size and press **Refresh snapshot & propose**.")
        return

    left, right = st.columns([2, 1])
    with left:
        st.subheader(f"{prop.instrument} — spot {prop.spot}  ·  read: {prop.direction}")
        _render_oi_macro(prop.context)
        st.write("**Why:**")
        for r in prop.reasons:
            st.write(f"- {r}")
        st.write("**Six-line check:**")
        st.table({k: [v] for k, v in prop.checklist.items()})

    with right:
        if prop.recommendation is Recommendation.ENTER:
            st.success("RECOMMENDATION: ENTER")
            st.metric("Entry", prop.entry)
            st.metric("Stop", prop.stop)
            st.metric("Target", prop.target)
            st.metric("R:R", prop.rr_ratio)
            st.metric("Approx ₹ risk", prop.rupee_risk)
            st.write(f"**Vehicle:** {prop.vehicle}")
        else:
            st.error("RECOMMENDATION: STAND DOWN  (no-trade is a win)")
            st.info("No levels — flat/conflicted read, so there is no trade to size.")

    # --- Claude's read --------------------------------------------------------
    st.divider()
    st.subheader("🤖 Claude's read & challenge")
    read = st.session_state.get("claude_read")
    err = st.session_state.get("claude_error")
    if err:
        st.warning(f"Claude sparring unavailable: {err}")
    elif read is None:
        st.caption("Claude sparring is off, or no read yet.")
    else:
        verdict = "ENTER" if read.enter else "STAND DOWN"
        agree = "agrees with" if read.agrees_with_engine else "DISAGREES with"
        (st.success if read.enter else st.error)(
            f"Claude: {verdict}  ·  {agree} the engine  ·  confidence {read.confidence}/5"
        )
        st.write(f"**Thesis:** {read.thesis}")
        st.write(f"**Challenge:** {read.challenge}")
        st.write(f"**Key risk:** {read.key_risk}")

        # --- Spar back ---------------------------------------------------------
        st.write("**Spar with Claude** — argue your read; it holds you to your invalidation.")
        chat = st.session_state.setdefault("chat", [])
        for m in chat:
            st.chat_message(m["role"]).write(m["content"])
        if msg := st.chat_input("Defend your thesis, or ask why…"):
            chat.append({"role": "user", "content": msg})
            try:
                reply = spar_turn(
                    chat, st.session_state["snapshot"], prop,
                    st.session_state.get("memory", ""),
                )
            except Exception as exc:
                reply = f"(chat unavailable: {exc})"
            chat.append({"role": "assistant", "content": reply})
            st.rerun()

    # --- Decision -------------------------------------------------------------
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
