"""Streamlit one-pane: source → analyse → propose → spar → you approve.

Run locally (live data + creds on your machine):

    streamlit run dashboard/app.py

Needs BREEZE_* (OHLCV + option chain), TWELVEDATA_API_KEY (macro), and
ANTHROPIC_API_KEY (Claude sparring). Nothing trades without your tap, and even an
approved trade is **dry-run** unless you flip the live toggle AND set
EXECUTION_LIVE=1 in the environment.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta

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
EXPIRY_WEEKDAY = 1  # Tuesday (Mon=0..Sun=6) — NSE Nifty weekly expiry (was Thursday)


@st.cache_data(show_spinner=True)
def _pull(symbol: str, nonce: int):
    """Pull Breeze 1-minute base + daily LIVE (force a fresh fetch each Refresh).

    ``nonce`` busts Streamlit's cache so pressing Refresh re-pulls; ``use_cache=False``
    bypasses the loader's parquet cache so the bars are live (and, after the close,
    the day's CLOSING bars).
    """
    loader = get_loader("breeze")
    today = date.today()
    base_min = loader.load(symbol, "minute", start=today - timedelta(days=10),
                           use_cache=False)
    daily = loader.load(symbol, "day", start=today - timedelta(days=800),
                        use_cache=False)
    return base_min, daily


def _n(x, d: int = 2) -> str:
    return "—" if x is None else f"{x:,.{d}f}"


def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _render_market_data(snap, fetched_at: str) -> None:
    """Always-visible live chart + OI numbers (last fetched; closing bars off-hours)."""
    read = snap.chart_read
    nums = read.get("numbers", {})
    lv = read.get("levels", {})
    st.caption(f"Data as of bar **{snap.ts}**  ·  fetched {fetched_at}")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**📈 Chart**")
        st.write(f"Spot **{_n(snap.spot)}**  ·  MTF read **{read.get('mtf_call')}**")
        st.write(
            f"EMA 5/45/100/200: {_n(nums.get('ema_5'))} / {_n(nums.get('ema_45'))} / "
            f"{_n(nums.get('ema_100'))} / {_n(nums.get('ema_200'))}"
        )
        st.write(
            f"Supertrend {_n(nums.get('supertrend'))}  ·  RSI {_n(nums.get('rsi_14'))}  "
            f"·  MACD hist {_n(nums.get('macd_hist'))}"
        )
        st.write(
            f"CPR pivot {_n(lv.get('cpr_pivot'))}  (TC {_n(lv.get('cpr_tc'))} / "
            f"BC {_n(lv.get('cpr_bc'))})"
        )
    with c2:
        st.markdown("**🧮 OI**")
        oi = snap.oi
        if oi:
            cw, ps = oi.get("call_wall") or {}, oi.get("put_shelf") or {}
            st.write(f"PCR **{_n(oi.get('pcr'))}**  ·  max-pain **{oi.get('max_pain')}**")
            st.write(f"Call wall {cw.get('strike')}  ·  put shelf {ps.get('strike')}")
        else:
            st.write("OI — **unavailable** (see diagnostics)")
        macro = snap.macro
        if macro:
            st.write(
                "Macro: "
                + " · ".join(f"{k} {_fmt_pct((v or {}).get('change_pct'))}"
                             for k, v in macro.items())
            )
    if snap.notes:
        with st.expander("Feed diagnostics (why OI / macro is missing)"):
            for note in snap.notes:
                st.write(f"- {note}")


def _render_chat_content(content) -> None:
    if isinstance(content, str):
        st.write(content)
        return
    for block in content:
        if block.get("type") == "text":
            st.write(block["text"])
        elif block.get("type") == "image":
            st.image(base64.b64decode(block["source"]["data"]), width=320)


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
            st.session_state["nonce"] = st.session_state.get("nonce", 0) + 1
            base_min, daily = _pull(symbol, st.session_state["nonce"])
            snap = build_snapshot(
                symbol, base_min, daily, anchor=ANCHOR,
                oi_fetch_fn=make_chain_fetcher(weekday=EXPIRY_WEEKDAY),
                macro_quote_fn=make_quote_fn(),
                macro_symbols=SCORECARD_SYMBOLS,
            )
            prop = propose_trade1(snap, size_lots)
            memory = distill_memory(load_decisions(DEFAULT_LOG))
            st.session_state.update(
                proposal=prop, snapshot=snap, memory=memory, chat=[],
                claude_read=None, claude_error=None,
                fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            if spar:
                try:
                    st.session_state["claude_read"] = claude_read(snap, prop, memory)
                except Exception as exc:  # missing key / network / SDK
                    st.session_state["claude_error"] = str(exc)

    snap = st.session_state.get("snapshot")
    prop = st.session_state.get("proposal")
    if snap is None:
        st.info("Set size and press **Refresh snapshot & propose**.")
        return

    # --- Always-visible live market data -------------------------------------
    _render_market_data(snap, st.session_state.get("fetched_at", "?"))
    st.divider()

    # --- The proposal --------------------------------------------------------
    left, right = st.columns([2, 1])
    with left:
        st.subheader(f"{prop.instrument} — read: {prop.direction}")
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

    # --- Claude's read: chart + OI + where + trade ---------------------------
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
        st.write(f"**📈 Chart analysis:** {read.chart_analysis}")
        st.write(f"**🧮 OI analysis:** {read.oi_analysis}")
        st.write(f"**🧭 Where it's moving:** {read.where_moving}")
        st.write(f"**🎯 Right trade (chart + OI):** {read.right_trade}")
        st.write(f"**⚔️ Challenge:** {read.challenge}")
        st.write(f"**⚠️ Key risk:** {read.key_risk}")

        # --- Spar back (paste a screenshot of the chain/chart to have Claude read it)
        st.write("**Spar with Claude** — argue your read, or paste a chart/option-chain screenshot.")
        chat = st.session_state.setdefault("chat", [])
        for m in chat:
            with st.chat_message(m["role"]):
                _render_chat_content(m["content"])
        sub = st.chat_input(
            "Defend your thesis, ask why, or attach a screenshot…",
            accept_file=True, file_type=["png", "jpg", "jpeg"],
        )
        if sub:
            text = getattr(sub, "text", "") or ""
            files = getattr(sub, "files", []) or []
            blocks = []
            if text:
                blocks.append({"type": "text", "text": text})
            for f in files:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": f.type or "image/png",
                               "data": base64.standard_b64encode(f.getvalue()).decode()},
                })
            chat.append({"role": "user", "content": blocks if files else text})
            try:
                reply = spar_turn(chat, snap, prop, st.session_state.get("memory", ""))
            except Exception as exc:
                reply = f"(chat unavailable: {exc})"
            chat.append({"role": "assistant", "content": reply})
            st.rerun()

    # --- Decision ------------------------------------------------------------
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
