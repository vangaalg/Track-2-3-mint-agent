"use strict";
// Training replay: pull the last-7-days 3-min triggers, rebuild each as-of moment
// (chart + OI + Claude's read, no future), let the trader take/skip + set levels, then
// reveal the real outcome and a 3-way compare. Reuses the shared chart.js engine.
const SYMBOL = "NIFTY", CHART_STRIKES = 8;
const $ = (id) => document.getElementById(id);
const n = (x, d = 2) => (x === null || x === undefined || Number.isNaN(x)) ? "—" : Number(x).toFixed(d);
const lakh = (x) => (x === null || x === undefined) ? "—" : (x / 1e5).toFixed(2);

let TRIGGERS = [], curTid = null, CUR = null;

async function loadList() {
  try {
    const d = await (await fetch(`/api/train/triggers?symbol=${SYMBOL}&days=8`)).json();
    TRIGGERS = d.triggers || [];
    refreshProgress(d.days);
    if (TRIGGERS.length) nextTrigger();
    else $("trigMeta").textContent = "No triggers found in the window.";
  } catch (e) { $("progress").textContent = "error: " + e.message; }
}

function refreshProgress(days) {
  const left = TRIGGERS.filter((t) => !t.answered).length;
  $("progress").textContent = `${left} of ${TRIGGERS.length} triggers left · last ${days || 7} days`;
}

function nextTrigger() {
  // walk the UNANSWERED triggers chronologically — never re-ask an answered one
  const pending = TRIGGERS.filter((t) => !t.answered);
  if (!pending.length) {
    $("trigMeta").innerHTML = "✅ <b>All triggers answered</b> — pull a longer window for more.";
    $("takeForm").style.display = "none"; $("revealBox").hidden = true;
    refreshProgress();
    return;
  }
  curTid = pending[0].tid;
  loadCase();
}

function markAnswered(tid) {
  const t = TRIGGERS.find((x) => x.tid === tid);
  if (t) t.answered = true;
  refreshProgress();
}

async function loadCase() {
  $("revealBox").hidden = true; $("takeForm").style.display = "";
  $("formMsg").textContent = ""; $("readBox").innerHTML = "<span class='muted'>Loading Claude's read…</span>";
  initCharts();
  try {
    CUR = await (await fetch(`/api/train/case/${curTid}?tf=${chartTF}&bars=200`)).json();
  } catch (e) { $("trigMeta").textContent = "case error: " + e.message; return; }
  const d = CUR;
  $("trigMeta").innerHTML = `${d.date} ${d.ts.slice(11, 16)} · `
    + `<b class="${d.direction === "long" ? "win-txt" : "loss-txt"}">${d.direction.toUpperCase()}</b> @ ${d.entry}`
    + (d.mtf_confidence != null ? ` · <span class="muted">MTF 45EMA ${d.mtf_confidence}/5 ${mtfTicks(d.mtf_confidence_breakdown, d.direction)}</span>` : "");
  $("entryShow").textContent = d.entry; $("dirShow").textContent = d.direction.toUpperCase();
  // suggest sensible default levels (trader edits): entry = trigger close, ±0.4% / ±0.2%
  const t = d.direction === "long" ? d.entry * 1.004 : d.entry * 0.996;
  const s = d.direction === "long" ? d.entry * 0.998 : d.entry * 1.002;
  $("inEntry").value = d.entry.toFixed(2);
  $("inTarget").value = t.toFixed(2); $("inStop").value = s.toFixed(2);
  recalcRR();
  _triggers = [{ ts: d.ts, direction: d.direction, outcome: "open" }];
  renderLW(d); renderOI(d); renderRead(d.read, d.read_err);
}

function recalcRR() {
  const e = parseFloat($("inEntry").value), t = parseFloat($("inTarget").value), s = parseFloat($("inStop").value);
  const risk = Math.abs(e - s), reward = Math.abs(t - e);
  const dir = CUR ? CUR.direction : null;
  const ok = dir === "long" ? (t > e && e > s) : dir === "short" ? (t < e && e < s) : risk > 0;
  $("rrShow").textContent = (risk > 0 && reward > 0) ? (reward / risk).toFixed(2) : "—";
  $("rrShow").className = ok ? "" : "bad";
}

async function loadCaseTF() {     // timeframe button — re-serve just the chart bars
  if (curTid === null) return;
  try { renderLW(await (await fetch(`/api/train/case/${curTid}?tf=${chartTF}&bars=200`)).json()); }
  catch (e) { /* keep */ }
}

function renderRead(rd, err) {
  if (!rd) { $("readBox").innerHTML = `<span class="muted">Claude read unavailable${err ? ": " + err : ""}.</span>`; return; }
  const v = rd.recommendation === "enter";
  $("readBox").innerHTML =
    `<div class="verdict ${v ? "enter" : "stand"}">Claude: ${v ? "ENTER" : "STAND DOWN"} · `
    + `${rd.agrees_with_engine ? "agrees with" : "DISAGREES with"} the engine · conf ${rd.confidence}/5</div>`
    + `<p><b>📈 Chart:</b> ${rd.chart_analysis}</p><p><b>🧮 OI:</b> ${rd.oi_analysis}</p>`
    + `<p><b>🧭 Where:</b> ${rd.where_moving}</p><p><b>🎯 Trade:</b> ${rd.right_trade}</p>`
    + `<p><b>⚔️ Challenge:</b> ${rd.challenge}</p><p><b>⚠️ Risk:</b> ${rd.key_risk}</p>`;
}

function renderOI(d) {
  const oi = d.oi, chain = d.chain || [];
  if (oi) {
    const age = d.oi_age_min != null
      ? ` · <span class="muted">as of ${d.oi_as_of ? d.oi_as_of.slice(11, 16) : "?"} (${d.oi_age_min} min before trigger)</span>` : "";
    $("oiSummary").innerHTML = `PCR <b>${n(oi.pcr)}</b> · max-pain <b>${oi.max_pain}</b> · ATM ${oi.atm}${age}`;
  } else {
    $("oiSummary").innerHTML = "<span class='muted'>No OI recorded for this moment — "
      + "run the 7-day backfill (<code>python -m feeds.oi_backfill</code>) or keep the cockpit "
      + "logging during the session. (Chart + Claude are still valid.)</span>";
  }
  if (!chain.length) { $("walls").textContent = ""; $("chainTbl").innerHTML = ""; Plotly.purge("oichart"); return; }

  const byCall = [...chain].filter((r) => r.call_oi != null).sort((a, b) => b.call_oi - a.call_oi).slice(0, 2);
  const byPut = [...chain].filter((r) => r.put_oi != null).sort((a, b) => b.put_oi - a.put_oi).slice(0, 2);
  $("walls").innerHTML = `🔴 Call walls: ${byCall.map((r) => `${r.strike} (${lakh(r.call_oi)}L)`).join(" · ")}`
    + ` &nbsp;|&nbsp; 🟢 Put shelves: ${byPut.map((r) => `${r.strike} (${lakh(r.put_oi)}L)`).join(" · ")}`;
  const cwS = new Set(byCall.map((r) => r.strike)), psS = new Set(byPut.map((r) => r.strike));
  const atm = oi ? oi.atm : null;

  const win = chain.filter((r) => atm == null || Math.abs(r.strike - atm) <= CHART_STRIKES * 50);
  const y = win.map((r) => r.strike);
  Plotly.react("oichart", [
    { type: "bar", orientation: "h", name: "Call OI", y, x: win.map((r) => -(r.call_oi || 0) / 1e5),
      text: win.map((r) => r.call_oi ? lakh(r.call_oi) : ""), textposition: "outside",
      marker: { color: "#e45756" }, hovertemplate: "%{y} call %{text}L<extra></extra>" },
    { type: "bar", orientation: "h", name: "Put OI", y, x: win.map((r) => (r.put_oi || 0) / 1e5),
      text: win.map((r) => r.put_oi ? lakh(r.put_oi) : ""), textposition: "outside",
      marker: { color: "#54a24b" }, hovertemplate: "%{y} put %{text}L<extra></extra>" },
  ], {
    barmode: "overlay", height: 26 * win.length + 40, showlegend: false,
    margin: { l: 50, r: 20, t: 8, b: 28 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#555", size: 10 }, yaxis: { autorange: "reversed", type: "category" },
    xaxis: { title: "← Call OI (L)   |   Put OI (L) →", zeroline: true, zerolinecolor: "#3a4258" },
  }, { displayModeBar: false, responsive: true });

  let h = "<thead><tr><th>Call TV</th><th>Call LTP</th><th>Call OI(L)</th><th>Strike</th>"
    + "<th>Put OI(L)</th><th>Put LTP</th><th>Put TV</th></tr></thead><tbody>";
  for (const r of chain) {
    const cls = r.strike === atm ? "atm" : "";
    const cw = cwS.has(r.strike) ? (r.strike === byCall[0]?.strike ? "cwall" : "cwall2") : "";
    const ps = psS.has(r.strike) ? (r.strike === byPut[0]?.strike ? "pshelf" : "pshelf2") : "";
    h += `<tr class="${cls}"><td>${n(r.call_extrinsic)}</td><td>${n(r.call_ltp)}</td>`
      + `<td class="${cw}">${lakh(r.call_oi)}</td><td class="strike">${r.strike}</td>`
      + `<td class="${ps}">${lakh(r.put_oi)}</td><td>${n(r.put_ltp)}</td><td>${n(r.put_extrinsic)}</td></tr>`;
  }
  $("chainTbl").innerHTML = h + "</tbody>";
}

async function answer(action) {
  if (curTid === null) return;
  const fd = new FormData(); fd.append("tid", curTid); fd.append("action", action);
  fd.append("entry", $("inEntry").value); fd.append("reason", $("inReason").value);
  if (action === "take") { fd.append("target", $("inTarget").value); fd.append("stop", $("inStop").value); }
  const r = await fetch("/api/train/answer", { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok) { $("formMsg").textContent = d.detail || "error"; return; }
  markAnswered(curTid);
  renderReveal(d);
  if (d.record) renderScore(d.record);
}

function _oc(o) {
  const cls = o.status === "win" ? "win-txt" : (o.status === "loss" ? "loss-txt" : "muted");
  return `<b class="${cls}">${(o.status || "open").toUpperCase()}</b> `
    + `${o.points >= 0 ? "+" : ""}${o.points} pts (${o.rupees >= 0 ? "+" : ""}₹${o.rupees})`;
}

const CELL_TXT = {
  deserved: ["✅ Deserved", "took a winner — good call"],
  accept: ["😐 Accept", "took a valid signal, lost to variance"],
  missed: ["⚠️ Missed", "you skipped a winner"],
  avoided: ["🛡️ Avoided", "you skipped a loser — good discipline"],
  open: ["• Open", "never resolved intraday"],
};
const ROUND_TXT = { you: "🏆 You win the round", claude: "🤖 Claude wins the round", tie: "🤝 Tie" };
function renderReveal(d) {
  $("takeForm").style.display = "none";
  const [lbl, sub] = CELL_TXT[d.cell] || [d.cell, ""];
  const claude = d.claude ? `${(d.claude.recommendation || "?").toUpperCase()} (conf ${d.claude.confidence}/5)` : "—";
  const youLine = d.action === "take"
    ? `You <b>TOOK</b> it (entry ${d.your_levels.entry} · target ${d.your_levels.target} / stop ${d.your_levels.stop} · R:R ${d.rr ?? "—"}) → ${_oc(d.your_outcome)}`
    : `You <b>SKIPPED</b> it → would-be ${_oc(d.your_outcome)}`;
  const reasonLine = d.reason ? `<p class="muted">Your reason: ${d.reason}</p>` : "";
  const vsLine = d.claude
    ? `<p class="vs">You <b>${d.agree ? "AGREED" : "DISAGREED"}</b> with Claude · <b>${ROUND_TXT[d.round_winner] || ""}</b></p>`
    : "";
  $("revealBox").hidden = false;
  $("revealBox").innerHTML =
    `<div class="reveal-cell ${d.cell}"><span class="rc-lbl">${lbl}</span><span class="rc-sub">${sub}</span></div>`
    + `<p>${youLine}</p>` + reasonLine
    + `<p>Engine levels → ${_oc(d.engine_outcome)} (target ${d.engine_outcome.target} / stop ${d.engine_outcome.stop} · R:R ${d.engine_outcome.rr ?? "—"})</p>`
    + `<p>Claude had said: <b>${claude}</b></p>` + vsLine
    + `<p class="muted small">Saved to the learning store (kind=training, 2 lots) — the agent will see this.</p>`
    + `<button id="next2" class="btn primary">Next trigger ▶</button>`;
  $("next2").onclick = nextTrigger;
}

const _pl = (p, rs) => `${p >= 0 ? "+" : ""}${p} pts (${rs >= 0 ? "+" : ""}₹${rs})`;
const _pct = (x) => x == null ? "—" : `${Math.round(x * 100)}%`;
function renderScore(s) {
  if (!s || !s.n) { $("scoreboard").textContent = "⚔️ Claude vs You — no rounds played yet."; return; }
  const r = s.rounds || {}, you = s.you || {}, cl = s.claude || {};
  const lead = r.you > r.claude ? "win-txt" : (r.claude > r.you ? "loss-txt" : "");
  $("scoreboard").innerHTML =
    `<div class="hh"><span class="hh-h">⚔️ Head-to-head</span> `
    + `<b class="${lead}">Claude ${r.claude || 0} – ${r.you || 0} You</b> `
    + `<span class="muted">(${r.ties || 0} ties · agreed ${s.agree || 0}/${(s.agree || 0) + (s.disagree || 0)})</span></div>`
    + `<div class="hh-row">P&L (${s.lots} lots): Claude <b>${_pl(cl.net_points || 0, cl.net_rupees || 0)}</b> · `
    + `You <b>${_pl(you.net_points || 0, you.net_rupees || 0)}</b></div>`
    + `<div class="hh-row">Hit-rate: Claude <b>${_pct(cl.hit_rate)}</b> (${cl.correct || 0}/${(cl.correct || 0) + (cl.wrong || 0)}) · `
    + `You <b>${_pct(you.hit_rate)}</b> (${you.correct || 0}/${(you.correct || 0) + (you.wrong || 0)})</div>`;
}

async function fetchScore() {
  try { renderScore(await (await fetch("/api/train/record")).json()); } catch (e) { /* keep */ }
}

$("nextBtn").onclick = nextTrigger;
$("takeBtn").onclick = () => answer("take");
$("skipBtn").onclick = () => answer("skip");
["inEntry", "inTarget", "inStop"].forEach((id) => $(id).addEventListener("input", recalcRR));
wireChartUI(loadCaseTF);          // timeframe buttons + ⚙ indicator panel (chart.js)
loadList(); fetchScore();
