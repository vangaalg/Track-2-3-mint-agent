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
    $("progress").textContent = `${TRIGGERS.length} triggers · last ${d.days} days`;
    if (TRIGGERS.length) nextTrigger();
    else $("trigMeta").textContent = "No triggers found in the window.";
  } catch (e) { $("progress").textContent = "error: " + e.message; }
}

function nextTrigger() {
  if (!TRIGGERS.length) return;
  curTid = TRIGGERS[Math.floor(Math.random() * TRIGGERS.length)].tid;
  loadCase();
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
    + `<b class="${d.direction === "long" ? "win-txt" : "loss-txt"}">${d.direction.toUpperCase()}</b> @ ${d.entry}`;
  $("entryShow").textContent = d.entry; $("dirShow").textContent = d.direction;
  // suggest sensible default levels (trader edits): ±0.4% / ±0.2%
  const t = d.direction === "long" ? d.entry * 1.004 : d.entry * 0.996;
  const s = d.direction === "long" ? d.entry * 0.998 : d.entry * 1.002;
  $("inTarget").value = t.toFixed(2); $("inStop").value = s.toFixed(2);
  _triggers = [{ ts: d.ts, direction: d.direction, outcome: "open" }];
  renderLW(d); renderOI(d); renderRead(d.read, d.read_err);
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
  if (oi) $("oiSummary").innerHTML = `PCR <b>${n(oi.pcr)}</b> · max-pain <b>${oi.max_pain}</b> · ATM ${oi.atm}`;
  else { $("oiSummary").textContent = "OI — not stored for this moment."; }
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
  if (action === "take") { fd.append("target", $("inTarget").value); fd.append("stop", $("inStop").value); }
  const r = await fetch("/api/train/answer", { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok) { $("formMsg").textContent = d.detail || "error"; return; }
  renderReveal(d);
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
function renderReveal(d) {
  $("takeForm").style.display = "none";
  const [lbl, sub] = CELL_TXT[d.cell] || [d.cell, ""];
  const claude = d.claude ? `${(d.claude.recommendation || "?").toUpperCase()} (conf ${d.claude.confidence}/5)` : "—";
  const youLine = d.action === "take"
    ? `You <b>TOOK</b> it (target ${d.your_levels.target} / stop ${d.your_levels.stop}) → ${_oc(d.your_outcome)}`
    : `You <b>SKIPPED</b> it → would-be ${_oc(d.your_outcome)}`;
  $("revealBox").hidden = false;
  $("revealBox").innerHTML =
    `<div class="reveal-cell ${d.cell}"><span class="rc-lbl">${lbl}</span><span class="rc-sub">${sub}</span></div>`
    + `<p>${youLine}</p>`
    + `<p>Engine levels → ${_oc(d.engine_outcome)} (target ${d.engine_outcome.target} / stop ${d.engine_outcome.stop})</p>`
    + `<p>Claude had said: <b>${claude}</b></p>`
    + `<p class="muted small">Saved to the learning store (kind=training) — the agent will see this.</p>`
    + `<button id="next2" class="btn primary">Next trigger ▶</button>`;
  $("next2").onclick = nextTrigger;
}

$("nextBtn").onclick = nextTrigger;
$("takeBtn").onclick = () => answer("take");
$("skipBtn").onclick = () => answer("skip");
wireChartUI(loadCaseTF);          // timeframe buttons + ⚙ indicator panel (chart.js)
loadList();
