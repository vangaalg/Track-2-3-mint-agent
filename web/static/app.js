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

let _triggers = [];
let chartTF = "3min";
let LW = null;

// --- indicator customization (color / show-hide / width), persisted locally ----- //
const IND_KEY = "chartIndicators";
const LINE_KEYS = ["bbU", "bbM", "bbL", "ema5", "ema45", "ema100", "ema200", "st", "macdL", "sigL", "rsi"];
const PANEL_KEYS = ["candleUp", "candleDn", "ema5", "ema45", "ema100", "ema200",
  "bbU", "bbM", "bbL", "st", "macdL", "sigL", "rsi"];
const IND_DEFAULTS = {
  candleUp: { label: "Candle up", color: "#26a69a", width: 1 },
  candleDn: { label: "Candle down", color: "#ef5350", width: 1 },
  bbU: { label: "BB upper", color: "#b0b4c0", width: 1 },
  bbM: { label: "BB mid", color: "#b0b4c0", width: 1 },
  bbL: { label: "BB lower", color: "#b0b4c0", width: 1 },
  ema5: { label: "EMA 5", color: "#2962ff", width: 1 },
  ema45: { label: "EMA 45", color: "#f0a000", width: 2 },
  ema100: { label: "EMA 100", color: "#9c27b0", width: 1 },
  ema200: { label: "EMA 200", color: "#787b86", width: 2 },
  st: { label: "Supertrend", color: "#ff6d00", width: 2 },
  macdL: { label: "MACD", color: "#2962ff", width: 1 },
  sigL: { label: "Signal", color: "#ef5350", width: 1 },
  rsi: { label: "RSI", color: "#9c27b0", width: 2 },
};
function loadIndCfg() {
  const base = {};
  for (const k in IND_DEFAULTS) base[k] = Object.assign({ visible: true }, IND_DEFAULTS[k]);
  try {
    const saved = JSON.parse(localStorage.getItem(IND_KEY) || "{}");
    for (const k in saved) if (base[k]) Object.assign(base[k], saved[k]);
  } catch (e) { /* ignore corrupt prefs */ }
  return base;
}
let IND = loadIndCfg();
function saveIndCfg() { try { localStorage.setItem(IND_KEY, JSON.stringify(IND)); } catch (e) { /* quota */ } }

function applyIndicatorConfig() {
  if (!LW) return;
  LW.candle.applyOptions({
    upColor: IND.candleUp.color, wickUpColor: IND.candleUp.color, borderUpColor: IND.candleUp.color,
    downColor: IND.candleDn.color, wickDownColor: IND.candleDn.color, borderDownColor: IND.candleDn.color });
  for (const k of LINE_KEYS) {
    const cfg = IND[k];
    if (LW[k] && cfg) LW[k].applyOptions({ color: cfg.color, lineWidth: cfg.width, visible: cfg.visible !== false });
  }
}

function buildIndPanel() {
  const rows = $("indRows"); if (!rows) return;
  rows.innerHTML = "";
  for (const k of PANEL_KEYS) {
    const cfg = IND[k], hasVis = k !== "candleUp" && k !== "candleDn";
    const row = document.createElement("div"); row.className = "indrow";
    row.innerHTML = `<input type="color" value="${cfg.color}" data-k="${k}" class="ic-color" />`
      + `<span class="ic-label">${cfg.label}</span>`
      + (hasVis ? `<input type="checkbox" ${cfg.visible !== false ? "checked" : ""} data-k="${k}" class="ic-vis" />` : "");
    rows.appendChild(row);
  }
  rows.querySelectorAll(".ic-color").forEach((el) => el.onchange = () => {
    IND[el.dataset.k].color = el.value; saveIndCfg(); applyIndicatorConfig(); });
  rows.querySelectorAll(".ic-vis").forEach((el) => el.onchange = () => {
    IND[el.dataset.k].visible = el.checked; saveIndCfg(); applyIndicatorConfig(); });
}

// Lightweight Charts renders UTC; shift +5:30 so the axis shows IST wall-clock.
const _lwTime = (iso) => Math.floor(Date.parse(iso) / 1000) + 19800;
const _fmtT = (t) => { const d = new Date(t * 1000);
  return String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0"); };

function _mkChart(elId) {
  return LightweightCharts.createChart($(elId), {
    autoSize: true,
    layout: { background: { color: "#ffffff" }, textColor: "#2b2f3a", fontSize: 11 },
    grid: { vertLines: { color: "#eef0f4" }, horzLines: { color: "#eef0f4" } },
    rightPriceScale: { borderColor: "#d4d7de" },
    timeScale: { borderColor: "#d4d7de", timeVisible: true, secondsVisible: false, tickMarkFormatter: _fmtT },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { timeFormatter: _fmtT },
  });
}

function initCharts() {
  if (LW || !window.LightweightCharts) return;
  const main = _mkChart("priceChart");
  const candle = main.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350",
    borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350" });
  const ln = (color, w) => main.addLineSeries({ color, lineWidth: w, priceLineVisible: false,
    lastValueVisible: true, crosshairMarkerVisible: false });
  const o = { main, candle,
    bbU: ln("#b0b4c0", 1), bbM: ln("#b0b4c0", 1), bbL: ln("#b0b4c0", 1),
    ema5: ln("#2962ff", 1), ema45: ln("#f0a000", 2), ema100: ln("#9c27b0", 1), ema200: ln("#787b86", 2),
    st: ln("#ff6d00", 2) };

  const macdC = _mkChart("macdChart");
  o.macdC = macdC;
  o.hist = macdC.addHistogramSeries({ priceLineVisible: false });
  o.macdL = macdC.addLineSeries({ color: "#2962ff", lineWidth: 1, priceLineVisible: false });
  o.sigL = macdC.addLineSeries({ color: "#ef5350", lineWidth: 1, priceLineVisible: false });

  const rsiC = _mkChart("rsiChart");
  o.rsiC = rsiC;
  o.rsi = rsiC.addLineSeries({ color: "#9c27b0", lineWidth: 1.5, priceLineVisible: false });
  o.rsi.createPriceLine({ price: 70, color: "#cfcfcf", lineStyle: 2, lineWidth: 1 });
  o.rsi.createPriceLine({ price: 30, color: "#cfcfcf", lineStyle: 2, lineWidth: 1 });

  let lock = false;
  const sync = (src, dests) => src.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (lock || !r) return; lock = true;
    dests.forEach((d) => d.timeScale().setVisibleLogicalRange(r)); lock = false;
  });
  sync(main, [macdC, rsiC]); sync(macdC, [main, rsiC]); sync(rsiC, [main, macdC]);
  o.loadedTf = null; o.cprLines = [];
  LW = o;
  applyIndicatorConfig();    // honour saved colors / show-hide on first paint
}

async function fetchChart() {
  initCharts();
  if (!LW) return;
  try { renderLW(await (await fetch(`/api/chart?tf=${chartTF}&bars=200`)).json()); }
  catch (e) { /* keep last */ }
}

function renderLW(d) {
  const b = d.bars || [];
  if (!b.length) return;
  const ser = (k) => b.filter((r) => r[k] != null).map((r) => ({ time: _lwTime(r.t), value: r[k] }));
  const fresh = LW.loadedTf !== chartTF;

  if (fresh) {
    LW.candle.setData(b.map((r) => ({ time: _lwTime(r.t), open: r.o, high: r.h, low: r.l, close: r.c })));
    LW.bbU.setData(ser("bb_u")); LW.bbM.setData(ser("bb_m")); LW.bbL.setData(ser("bb_l"));
    LW.ema5.setData(ser("ema5")); LW.ema45.setData(ser("ema45"));
    LW.ema100.setData(ser("ema100")); LW.ema200.setData(ser("ema200")); LW.st.setData(ser("st"));
    LW.hist.setData(b.filter((r) => r.hist != null).map((r) =>
      ({ time: _lwTime(r.t), value: r.hist, color: r.hist >= 0 ? "#26a69a" : "#ef5350" })));
    LW.macdL.setData(ser("macd")); LW.sigL.setData(ser("signal")); LW.rsi.setData(ser("rsi"));
    LW.cprLines.forEach((l) => LW.candle.removePriceLine(l)); LW.cprLines = [];
    const c = d.cpr || {};
    const addCpr = (p, t) => p && LW.cprLines.push(LW.candle.createPriceLine(
      { price: p, color: "#9aa0b4", lineStyle: 2, lineWidth: 1, title: t }));
    addCpr(c.pivot, "CPR"); addCpr(c.tc, "TC"); addCpr(c.bc, "BC");
    LW.main.timeScale().fitContent();
    LW.loadedTf = chartTF;
  } else {
    const last = b[b.length - 1], t = _lwTime(last.t);
    LW.candle.update({ time: t, open: last.o, high: last.h, low: last.l, close: last.c });
    const up = (s, k) => last[k] != null && s.update({ time: t, value: last[k] });
    up(LW.bbU, "bb_u"); up(LW.bbM, "bb_m"); up(LW.bbL, "bb_l");
    up(LW.ema5, "ema5"); up(LW.ema45, "ema45"); up(LW.ema100, "ema100"); up(LW.ema200, "ema200"); up(LW.st, "st");
    if (last.hist != null) LW.hist.update({ time: t, value: last.hist, color: last.hist >= 0 ? "#26a69a" : "#ef5350" });
    up(LW.macdL, "macd"); up(LW.sigL, "signal"); up(LW.rsi, "rsi");
  }
  // triggers are 3-min signals — only mark them on the 3m chart
  LW.candle.setMarkers(chartTF === "3min" ? _triggers.map((tg) => ({
    time: _lwTime(tg.ts), position: tg.direction === "long" ? "belowBar" : "aboveBar",
    color: ({ win: "#26a69a", loss: "#ef5350", open: "#2962ff" }[tg.outcome] || "#2962ff"),
    shape: tg.direction === "long" ? "arrowUp" : "arrowDown",
    text: `${tg.direction[0].toUpperCase()} ${tg.outcome}`,
  })) : []);
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
document.querySelectorAll("#tfbar button").forEach((btn) => btn.onclick = () => {
  document.querySelectorAll("#tfbar button").forEach((b) => b.classList.remove("on"));
  btn.classList.add("on");
  chartTF = btn.dataset.tf;
  if (LW) LW.loadedTf = null;       // force a full reload for the new TF
  fetchChart();
});
$("indCfgBtn").onclick = () => { const p = $("indCfg"); p.hidden = !p.hidden; };
$("indReset").onclick = (e) => {
  e.preventDefault();
  try { localStorage.removeItem(IND_KEY); } catch (err) { /* ignore */ }
  IND = loadIndCfg(); buildIndPanel(); applyIndicatorConfig();
};
buildIndPanel();
poll(); setInterval(poll, POLL_MS);
