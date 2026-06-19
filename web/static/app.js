"use strict";
const SYMBOL = "NIFTY", SIZE = 75, POLL_MS = 15000, CHART_STRIKES = 8;
const $ = (id) => document.getElementById(id);
const n = (x, d = 2) => (x === null || x === undefined || Number.isNaN(x)) ? "—" : Number(x).toFixed(d);
const lakh = (x) => (x === null || x === undefined) ? "—" : (x / 1e5).toFixed(2);

let lastBar = null, analysing = false;

async function poll() {
  try {
    const r = await fetch(`/api/snapshot?symbol=${SYMBOL}&size=${SIZE}`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const d = await r.json();
    $("dot").className = "dot live";
    $("meta").textContent = `as of ${d.ts} · fetched ${d.fetched_at}`;
    renderChart(d); renderOI(d); renderProposal(d.proposal);
    fetchChart(); fetchTriggers(); fetchRecord();
    // auto-analyse once per new ENTER bar
    if (d.auto_trigger && d.ts !== lastBar && !analysing) { lastBar = d.ts; analyse(); }
  } catch (e) {
    $("dot").className = "dot err"; $("meta").textContent = "error: " + e.message;
  }
}

function renderChart(d) {
  const c = d.chart, num = c.numbers || {}, lv = c.levels || {};
  $("spot").textContent = n(d.spot); $("mtf").textContent = c.mtf_call || "—";
  $("rsi").textContent = n(num.rsi_14); $("macd").textContent = n(num.macd_hist);
  $("st").textContent = n(num.supertrend); $("cpr").textContent = n(lv.cpr_pivot);
  $("emas").textContent = `EMA 5/45/100/200: ${n(num.ema_5)} / ${n(num.ema_45)} / ${n(num.ema_100)} / ${n(num.ema_200)}`;
  $("cprband").textContent = `CPR TC/BC: ${n(lv.cpr_tc)} / ${n(lv.cpr_bc)}`;
  if (d.macro) $("macro").textContent = "Macro: " + Object.entries(d.macro)
    .map(([k, v]) => `${k} ${v && v.change_pct != null ? v.change_pct.toFixed(2) + "%" : "—"}`).join(" · ");
  $("notes").innerHTML = (d.notes || []).map((x) => `<div class="small">• ${x}</div>`).join("");
  $("diag").style.display = (d.notes && d.notes.length) ? "block" : "none";
}

function renderOI(d) {
  const oi = d.oi, chain = d.chain || [];
  if (oi) {
    $("oiSummary").innerHTML = `PCR <b>${n(oi.pcr)}</b> · max-pain <b>${oi.max_pain}</b> · ATM ${oi.atm}`;
  } else { $("oiSummary").textContent = "OI — unavailable (see diagnostics)"; }
  if (!chain.length) { $("walls").textContent = ""; $("chainTbl").innerHTML = ""; return; }

  const byCall = [...chain].filter(r => r.call_oi != null).sort((a, b) => b.call_oi - a.call_oi).slice(0, 2);
  const byPut = [...chain].filter(r => r.put_oi != null).sort((a, b) => b.put_oi - a.put_oi).slice(0, 2);
  $("walls").innerHTML = `🔴 Call walls: ${byCall.map(r => `${r.strike} (${lakh(r.call_oi)}L)`).join(" · ")}`
    + ` &nbsp;|&nbsp; 🟢 Put shelves: ${byPut.map(r => `${r.strike} (${lakh(r.put_oi)}L)`).join(" · ")}`;
  const cwS = new Set(byCall.map(r => r.strike)), psS = new Set(byPut.map(r => r.strike));
  const atm = oi ? oi.atm : null;

  // mirrored bar chart (ATM window) with data labels
  const win = chain.filter(r => atm == null || Math.abs(r.strike - atm) <= CHART_STRIKES * 50);
  const y = win.map(r => r.strike);
  Plotly.react("oichart", [
    { type: "bar", orientation: "h", name: "Call OI", y, x: win.map(r => -(r.call_oi || 0) / 1e5),
      text: win.map(r => r.call_oi ? lakh(r.call_oi) : ""), textposition: "outside",
      marker: { color: "#e45756" }, hovertemplate: "%{y} call %{text}L<extra></extra>" },
    { type: "bar", orientation: "h", name: "Put OI", y, x: win.map(r => (r.put_oi || 0) / 1e5),
      text: win.map(r => r.put_oi ? lakh(r.put_oi) : ""), textposition: "outside",
      marker: { color: "#54a24b" }, hovertemplate: "%{y} put %{text}L<extra></extra>" },
  ], {
    barmode: "overlay", height: 26 * win.length + 40, showlegend: false,
    margin: { l: 50, r: 20, t: 8, b: 28 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#555", size: 10 }, yaxis: { autorange: "reversed", type: "category" },
    xaxis: { title: "← Call OI (L)   |   Put OI (L) →", zeroline: true, zerolinecolor: "#3a4258" },
  }, { displayModeBar: false, responsive: true });

  // time-value table
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

function renderProposal(p) {
  const box = $("propBox"), dec = $("decision");
  if (p.recommendation === "enter") {
    box.className = "propbox enter";
    box.innerHTML = `ENTER · ${p.direction}<br>Entry ${n(p.entry)} · Stop ${n(p.stop)} · Target ${n(p.target)}`
      + `<br>R:R ${p.rr_ratio} · ${p.size_lots} lots<br><span class="muted">${p.vehicle || ""}</span>`;
    dec.hidden = false;
  } else {
    box.className = "propbox stand";
    box.innerHTML = "STAND DOWN — flat/conflicted read, no trade to size. <span class='muted'>(no-trade is a win)</span>";
    dec.hidden = false;
  }
}

function renderRead(rd) {
  const v = rd.recommendation === "enter";
  $("readBox").innerHTML =
    `<div class="verdict ${v ? "enter" : "stand"}">Claude: ${v ? "ENTER" : "STAND DOWN"} · `
    + `${rd.agrees_with_engine ? "agrees with" : "DISAGREES with"} the engine · conf ${rd.confidence}/5</div>`
    + `<p><b>📈 Chart:</b> ${rd.chart_analysis}</p><p><b>🧮 OI:</b> ${rd.oi_analysis}</p>`
    + `<p><b>🧭 Where:</b> ${rd.where_moving}</p><p><b>🎯 Trade:</b> ${rd.right_trade}</p>`
    + `<p><b>⚔️ Challenge:</b> ${rd.challenge}</p><p><b>⚠️ Risk:</b> ${rd.key_risk}</p>`;
}

async function fetchTriggers() {
  try {
    const d = await (await fetch(`/api/triggers?size=${SIZE}`)).json();
    renderTriggers(d);
  } catch (e) { /* keep last */ }
}

function renderTriggers(d) {
  _triggers = d.triggers || [];
  const s = d.summary || {}, last = d.last;
  if (last) {
    const dir = last.direction.toUpperCase(), oc = last.outcome;
    const t = last.ts.slice(11, 16);
    $("trigLast").className = "trig-last " + oc;
    $("trigLast").innerHTML = `Last trigger ${t} · <b>${dir}</b> @ ${last.entry} `
      + `→ stop ${last.stop} / target ${last.target} · <b>${oc.toUpperCase()}</b> `
      + `${last.points >= 0 ? "+" : ""}${last.points} pts (${last.rupees >= 0 ? "+" : ""}₹${last.rupees})`;
  } else {
    $("trigLast").className = "trig-last";
    $("trigLast").textContent = `No triggers yet today (${d.session || "—"}).`;
  }
  $("trigSummary").innerHTML = `${s.n || 0} triggers · ${s.wins || 0}W / ${s.losses || 0}L / ${s.open || 0} open`
    + ` · net <b class="${s.net_points >= 0 ? "win-txt" : "loss-txt"}">${s.net_points >= 0 ? "+" : ""}${s.net_points || 0} pts `
    + `(${s.net_rupees >= 0 ? "+" : ""}₹${s.net_rupees || 0})</b> if all taken`
    + (s.hit_rate != null ? ` · hit-rate ${(s.hit_rate * 100).toFixed(0)}%` : "");
  let h = "<thead><tr><th>Time</th><th>Dir</th><th>Entry</th><th>Stop</th><th>Target</th>"
    + "<th>Out</th><th>Pts</th><th>₹</th></tr></thead><tbody>";
  for (const t of (d.triggers || [])) {
    h += `<tr><td>${t.ts.slice(11, 16)}</td><td>${t.direction}</td><td>${t.entry}</td>`
      + `<td>${t.stop}</td><td>${t.target}</td><td class="${t.outcome}">${t.outcome}</td>`
      + `<td class="${t.points >= 0 ? "win" : "loss"}">${t.points >= 0 ? "+" : ""}${t.points}</td>`
      + `<td>${t.rupees >= 0 ? "+" : ""}${t.rupees}</td></tr>`;
  }
  $("trigTbl").innerHTML = h + "</tbody>";
}

// Chart engine (Lightweight Charts module + ⚙ indicator panel) lives in chart.js,
// shared with the training page. `_triggers`, `chartTF`, `LW`, `initCharts`,
// `renderLW`, `wireChartUI` are provided there.

async function fetchChart() {
  initCharts();
  if (!LW) return;
  try { renderLW(await (await fetch(`/api/chart?tf=${chartTF}&bars=200`)).json()); }
  catch (e) { /* keep last */ }
}

async function fetchRecord() {
  try { renderRecord(await (await fetch("/api/record")).json()); } catch (e) { /* keep */ }
}

const CELL = {
  deserved: ["✅ Deserved", "good process · won"], accept: ["😐 Accept", "good process · lost (variance)"],
  dangerous: ["⚠️ Dangerous", "BAD process · won (luck — don't repeat)"], correct: ["🔴 Correct", "bad process · lost"],
};
function renderRecord(d) {
  const c = (d.summary && d.summary.cells) || {};
  $("recMatrix").innerHTML = Object.entries(CELL).map(([k, [lbl, sub]]) =>
    `<div class="cell ${k}"><span class="cn">${c[k] || 0}</span><span class="cl">${lbl}</span>`
    + `<span class="cs">${sub}</span></div>`).join("");
  const s = d.summary || {};
  $("recSummary").innerHTML = `${s.n_settled || 0} settled · net `
    + `<b class="${s.net_points >= 0 ? "win-txt" : "loss-txt"}">${s.net_points >= 0 ? "+" : ""}${s.net_points || 0} pts `
    + `(${s.net_rupees >= 0 ? "+" : ""}₹${s.net_rupees || 0})</b> · graded by process, not P&L`;
  let h = "<thead><tr><th>Time</th><th>Decision</th><th>Process</th><th>Outcome</th><th>Pts</th><th>Cell</th></tr></thead><tbody>";
  for (const r of (d.recent || []).slice().reverse()) {
    const o = r.outcome || {}, t = r.ts ? r.ts.slice(11, 16) : "—";
    h += `<tr><td>${t}</td><td>${r.decision || "—"} ${r.direction || ""}</td><td>${r.process || "—"}</td>`
      + `<td class="${o.status || ""}">${o.status || "—"}</td>`
      + `<td class="${(o.points || 0) >= 0 ? "win" : "loss"}">${o.points != null ? (o.points >= 0 ? "+" : "") + o.points : "—"}</td>`
      + `<td>${r.matrix || "—"}</td></tr>`;
  }
  $("recTbl").innerHTML = h + "</tbody>";
}

async function analyse() {
  analysing = true; $("analyseBtn").textContent = "Analysing…";
  try {
    const r = await fetch("/api/analyse", { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    renderRead(await r.json());
  } catch (e) { $("readBox").innerHTML = `<span class="muted">Claude unavailable: ${e.message}</span>`; }
  analysing = false; $("analyseBtn").textContent = "🤖 Analyse with Claude";
}

async function decide(action) {
  const fd = new FormData(); fd.append("action", action);
  const r = await fetch("/api/decision", { method: "POST", body: fd });
  const d = await r.json();
  $("decisionMsg").textContent = `Logged ${d.logged} · execution ${d.status || "—"}`;
}

async function sendChat(ev) {
  ev.preventDefault();
  const text = $("chatText").value.trim(), file = $("chatFile").files[0];
  if (!text && !file) return;
  const fd = new FormData(); fd.append("text", text); if (file) fd.append("files", file);
  appendMsg("user", text, file);
  $("chatText").value = ""; $("chatFile").value = "";
  const r = await fetch("/api/chat", { method: "POST", body: fd });
  const d = await r.json(); appendMsg("assistant", d.reply);
}

function appendMsg(role, text, file) {
  const div = document.createElement("div"); div.className = "msg " + role;
  div.textContent = text || "";
  if (file) { const img = document.createElement("img"); img.src = URL.createObjectURL(file); div.appendChild(img); }
  $("chatLog").appendChild(div); $("chatLog").scrollTop = $("chatLog").scrollHeight;
}

$("analyseBtn").onclick = analyse;
$("approveBtn").onclick = () => decide("approve");
$("rejectBtn").onclick = () => decide("reject");
$("chatForm").onsubmit = sendChat;
wireChartUI(fetchChart);          // timeframe buttons + ⚙ indicator panel (chart.js)
poll(); setInterval(poll, POLL_MS);
