"""Streamlit one-pane: live data → analyse → propose → spar → you approve.

Run locally (live data + creds on your machine):

    streamlit run dashboard/app.py

Needs BREEZE_* (OHLCV + option chain), TWELVEDATA_API_KEY (macro), and
ANTHROPIC_API_KEY (Claude sparring). The chart auto-refreshes ~30s and OI ~5 min;
Claude analyses only on a Trade-1 ENTER trigger or your manual button. Nothing
trades without your tap, and an approved order is dry-run unless you flip the live
toggle AND set EXECUTION_LIVE=1.
"""

from __future__ import annotations

import base64
import time
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from loaders import get_loader
from feeds.snapshot import build_snapshot
from feeds.breeze_oi import make_chain_fetcher
from feeds.oi import chain_table
from feeds.td_macro import make_quote_fn, SCORECARD_SYMBOLS
from feeds.macro import fetch_macro
from analysis.trade1 import propose_trade1
from analysis.proposal import Recommendation
from execution import breeze_exec
from journal.log import log_decision, DEFAULT_LOG
from agent.memory import load_decisions, distill_memory
from agent.read import claude_read
from agent.chat import spar_turn

ANCHOR = "9h15min"
EXPIRY_WEEKDAY = 1     # Tuesday (Mon=0..Sun=6) — NSE Nifty weekly expiry
CHART_SECS = 30        # live chart cadence
OI_SECS = 300          # option-chain cadence (5 min)
VIZ_POINTS = 1000      # ± points shown in the chain table (time-value view)
CHART_STRIKES = 10     # ± strikes shown in the mirrored OI bar chart


# --- cached pulls (cadence via time-bucket keys) ---------------------------- #
@st.cache_data(show_spinner=False)
def _pull_daily(symbol: str):
    """Daily series — pulled once per session (long warm-up for 1d/1w/1m)."""
    loader = get_loader("breeze")
    return loader.load(symbol, "day", start=date.today() - timedelta(days=800),
                       use_cache=False)


@st.cache_data(show_spinner=False)
def _pull_intraday(symbol: str, bucket: int):
    """Bounded 1-minute base — re-pulled live each chart bucket (keeps it light)."""
    loader = get_loader("breeze")
    return loader.load(symbol, "minute", start=date.today() - timedelta(days=3),
                       use_cache=False)


@st.cache_data(show_spinner=False)
def _pull_chain(symbol: str, bucket5: int):
    """Full option chain — re-pulled every 5-min bucket."""
    return make_chain_fetcher(weekday=EXPIRY_WEEKDAY)(symbol)


@st.cache_data(show_spinner=False)
def _pull_macro(symbol: str, bucket5: int):
    """Macro scorecard — re-pulled every 5-min bucket (Twelve Data + Breeze VIX)."""
    return fetch_macro(SCORECARD_SYMBOLS, make_quote_fn(), errors=[])


# --- formatting helpers ----------------------------------------------------- #
def _n(x, d: int = 2) -> str:
    return "—" if x is None else f"{x:,.{d}f}"


def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


# --- render: market data + chain viz ---------------------------------------- #
def _render_market_data(snap, fetched_at: str) -> None:
    read = snap.chart_read
    nums, lv = read.get("numbers", {}), read.get("levels", {})
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
            st.write("Macro: " + " · ".join(
                f"{k} {_fmt_pct((v or {}).get('change_pct'))}" for k, v in macro.items()))
    if snap.notes:
        with st.expander("Feed diagnostics (why OI / macro is missing)"):
            for note in snap.notes:
                st.write(f"- {note}")


def _render_chain_viz(chain: pd.DataFrame, snap) -> None:
    """Per-strike OI (mirrored bars) + a table with LTP, OI (lakh) and TIME VALUE.

    Time value (extrinsic) ≈ 0 means the strike is deep-ITM (near-zero theta) — the
    high-delta vehicle. Top-2 call OI (walls) / put OI (shelves) are marked.
    """
    if chain is None or chain.empty:
        return
    spot = snap.spot
    atm = (snap.oi or {}).get("atm")
    t = chain_table(chain, spot, window=VIZ_POINTS)
    if t.empty:
        return

    st.markdown(f"**🪜 Option chain — OI · LTP · time value  (±{int(VIZ_POINTS)} pts)**")
    cw = t.dropna(subset=["call_oi"]).nlargest(2, "call_oi")
    ps = t.dropna(subset=["put_oi"]).nlargest(2, "put_oi")
    _w = lambda df, col: " · ".join(
        f"{int(r.strike)} ({r[col] / 1e5:.2f}L)" for _, r in df.iterrows())
    st.caption(
        f"🔴 **Call walls** (max CE OI): {_w(cw, 'call_oi')}   |   "
        f"🟢 **Put shelves** (max PE OI): {_w(ps, 'put_oi')}")
    st.caption("Time value = LTP − intrinsic; ≈ 0 ⇒ deep-ITM, near-zero theta "
               "(high-delta vehicle — pick the lowest non-negative value).")

    # Mirrored OI bars (lakh): calls left (negative), puts right.
    chart_win = t[(t["strike"] - spot).abs() <= CHART_STRIKES * 50]
    bars = pd.concat([
        pd.DataFrame({"strike": chart_win["strike"], "side": "Call OI",
                      "oi": -chart_win["call_oi"] / 1e5}),
        pd.DataFrame({"strike": chart_win["strike"], "side": "Put OI",
                      "oi": chart_win["put_oi"] / 1e5}),
    ])
    try:
        import altair as alt
        st.altair_chart(
            alt.Chart(bars).mark_bar().encode(
                y=alt.Y("strike:O", sort="descending", title="Strike"),
                x=alt.X("oi:Q", title="← Call OI (lakh)    |    Put OI (lakh) →"),
                color=alt.Color("side:N", scale=alt.Scale(
                    domain=["Call OI", "Put OI"], range=["#e45756", "#54a24b"])),
                tooltip=["strike", "side", "oi"],
            ).properties(height=26 * len(chart_win)),
            use_container_width=True,
        )
    except Exception:
        st.bar_chart(chart_win.set_index("strike")[["call_oi", "put_oi"]])

    # Broker-style table: Call time-val | Call LTP | Call OI(L) | Strike | Put OI(L) | Put LTP | Put time-val
    disp = pd.DataFrame({
        "Call time-val": t["call_extrinsic"].round(2),
        "Call LTP": t["call_ltp"].round(2),
        "Call OI (L)": (t["call_oi"] / 1e5).round(2),
        "Strike": t["strike"],
        "Put OI (L)": (t["put_oi"] / 1e5).round(2),
        "Put LTP": t["put_ltp"].round(2),
        "Put time-val": t["put_extrinsic"].round(2),
    })
    cw_s, ps_s = set(cw["strike"]), set(ps["strike"])
    co, po = disp.columns.get_loc("Call OI (L)"), disp.columns.get_loc("Put OI (L)")

    def _hl(row):
        styles = [""] * len(row)
        s = row["Strike"]
        if s in cw_s:
            styles[co] = "background-color:#f5c6cb"
        if s in ps_s:
            styles[po] = "background-color:#c3e6cb"
        if atm is not None and s == int(atm):
            styles = [(st_ + ";background-color:#fff3cd") for st_ in styles]
        return styles

    try:
        st.dataframe(disp.style.apply(_hl, axis=1).format(precision=2),
                     hide_index=True, use_container_width=True)
    except Exception:
        st.dataframe(disp, hide_index=True, use_container_width=True)


def _render_chat_content(content) -> None:
    if isinstance(content, str):
        st.write(content)
        return
    for block in content:
        if block.get("type") == "text":
            st.write(block["text"])
        elif block.get("type") == "image":
            st.image(base64.b64decode(block["source"]["data"]), width=320)


def _run_claude(snap, prop) -> None:
    """Run + store one Claude read for this snapshot (used by trigger + button)."""
    try:
        mem = distill_memory(load_decisions(DEFAULT_LOG))
        st.session_state["memory"] = mem
        st.session_state["claude_read"] = claude_read(snap, prop, mem)
        st.session_state["claude_error"] = None
        st.session_state["analysed_bar"] = snap.ts
    except Exception as exc:
        st.session_state["claude_error"] = str(exc)


def _render_claude(read, err) -> None:
    if err:
        st.warning(f"Claude sparring unavailable: {err}")
        return
    if read is None:
        st.caption("No Claude read yet — fires on a Trade-1 ENTER, or press Analyse.")
        return
    verdict = "ENTER" if read.enter else "STAND DOWN"
    agree = "agrees with" if read.agrees_with_engine else "DISAGREES with"
    (st.success if read.enter else st.error)(
        f"Claude: {verdict}  ·  {agree} the engine  ·  confidence {read.confidence}/5")
    st.write(f"**📈 Chart analysis:** {read.chart_analysis}")
    st.write(f"**🧮 OI analysis:** {read.oi_analysis}")
    st.write(f"**🧭 Where it's moving:** {read.where_moving}")
    st.write(f"**🎯 Right trade (chart + OI):** {read.right_trade}")
    st.write(f"**⚔️ Challenge:** {read.challenge}")
    st.write(f"**⚠️ Key risk:** {read.key_risk}")


def main() -> None:
    st.set_page_config(page_title="Nifty Agent — live", layout="wide")
    st.title("Nifty Agent — Trade 1 (propose-only)")

    with st.sidebar:
        symbol = st.text_input("Instrument", "NIFTY")
        size_lots = st.slider("Size (lots)", 65, 130, 75, step=5)
        live = st.toggle("Live execution (else dry-run)", value=False)
        st.caption("Live also needs EXECUTION_LIVE=1 in the environment.")
        spar = st.toggle("Auto-analyse on ENTER trigger", value=True)
        st.caption(f"Chart auto-refreshes ~{CHART_SECS}s · OI ~{OI_SECS // 60} min.")

    # --- LIVE fragment: re-runs on its own timer, isolated from the chat ----- #
    @st.fragment(run_every=f"{CHART_SECS}s")
    def live_region():
        chart_bucket = int(time.time() // CHART_SECS)
        bucket5 = int(time.time() // OI_SECS)
        try:
            base_min = _pull_intraday(symbol, chart_bucket)
            daily = _pull_daily(symbol)
        except Exception as exc:
            st.error(f"Data pull failed: {exc}")
            return

        try:
            chain = _pull_chain(symbol, bucket5)
        except Exception as exc:
            chain = None
            st.session_state["oi_error"] = str(exc)
        macro = _pull_macro(symbol, bucket5)

        snap = build_snapshot(
            symbol, base_min, daily, anchor=ANCHOR,
            oi_fetch_fn=(lambda i: chain) if chain is not None else None,
            macro=macro,
        )
        if st.session_state.get("oi_error") and snap.oi is None:
            snap.notes.append(f"oi: {st.session_state['oi_error']}")
        prop = propose_trade1(snap, size_lots)
        st.session_state.update(
            snapshot=snap, proposal=prop, chain=chain,
            fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        _render_market_data(snap, st.session_state["fetched_at"])
        _render_chain_viz(chain, snap)
        st.divider()

        left, right = st.columns([2, 1])
        with left:
            st.subheader(f"{snap.instrument} — read: {prop.direction}")
            for r in prop.reasons:
                st.write(f"- {r}")
        with right:
            if prop.recommendation is Recommendation.ENTER:
                st.success("ENTER setup")
                st.metric("Entry", prop.entry)
                st.metric("Stop", prop.stop)
                st.metric("Target", prop.target)
                st.write(f"R:R {prop.rr_ratio} · {prop.vehicle}")
            else:
                st.error("STAND DOWN (no-trade is a win)")

        # Auto-analyse once per new ENTER bar; manual button anytime.
        if spar and prop.recommendation is Recommendation.ENTER \
                and st.session_state.get("analysed_bar") != snap.ts:
            with st.spinner("Trade-1 ENTER — analysing with Claude…"):
                _run_claude(snap, prop)
        st.divider()
        st.subheader("🤖 Claude's read & challenge")
        if st.button("Analyse with Claude now"):
            with st.spinner("Analysing…"):
                _run_claude(snap, prop)
        _render_claude(st.session_state.get("claude_read"),
                       st.session_state.get("claude_error"))

    live_region()

    # --- OUTSIDE the fragment: chat + decision (stable; not on the 30s timer) -
    snap = st.session_state.get("snapshot")
    prop = st.session_state.get("proposal")
    if snap is None:
        return

    st.divider()
    st.subheader("💬 Spar with Claude")
    st.caption("Argue your read, or paste a chart / option-chain screenshot.")
    chat = st.session_state.setdefault("chat", [])
    for m in chat:
        with st.chat_message(m["role"]):
            _render_chat_content(m["content"])
    sub = st.chat_input("Defend your thesis, ask why, or attach a screenshot…",
                        accept_file=True, file_type=["png", "jpg", "jpeg"])
    if sub:
        text = getattr(sub, "text", "") or ""
        files = getattr(sub, "files", []) or []
        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        for f in files:
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": f.type or "image/png",
                "data": base64.standard_b64encode(f.getvalue()).decode()}})
        chat.append({"role": "user", "content": blocks if files else text})
        try:
            reply = spar_turn(chat, snap, prop, st.session_state.get("memory", ""))
        except Exception as exc:
            reply = f"(chat unavailable: {exc})"
        chat.append({"role": "assistant", "content": reply})
        st.rerun()

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
