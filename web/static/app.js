"use strict";
let currentSymbol = "NIFTY";          // active instrument (NIFTY / BANKNIFTY / …)
const sym = () => encodeURIComponent(currentSymbol);
const SIZE = 75, POLL_MS = 15000, CHART_STRIKES = 8;
const $ = (id) => document.getElementById(id);
const n = (x, d = 2) => (x === null || x === undefined || Number.isNaN(x)) ? "—" : Number(x).toFixed(d);
const lakh = (x) => (x === null || x === undefined) ? "—" : (x / 1e5).toFixed(2);

let analysing = false, lastPayload = null, currentStrat = "trade1", currentHead = null;
const STRAT_LABEL = { trade1: "3-min", cpr_st: "CPR-ST", orb: "ORB", condor: "Expiry condor" };

// Triggers table (independent of the chart/decision tabs): merged across strategies,
// browseable by day, paginated 10/page.
const TRIG_PAGE = 10;
let _trigStrat = "all", _trigDate = null, _trigPage = 0;
let _trigRows = [], _trigSummary = {}, _trigLast = null, _trigSession = null, _trigPending = 0;
let _trigDates = [], _trigStrats = [];
let _pcrDay = "all", _pcrDays = [];          // PCR-over-time: recorded session picker
let _mrDay = "all", _mrDays = [], _mrRows = [];  // saved "Market view" reads: day picker + rows
let _logDay = "all", _logStrat = "all", _logDays = [], _logRows = [];  // triggers+analysis log (all instruments)
let _scanRows = [];                          // last scanner rows (for the 💬 full-read lookup)
let _seenPending = new Set();                // pending trigger ts already alerted (one beep each)

async function poll() {
  try {
    const r = await fetch(`/api/snapshot?symbol=${sym()}`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const d = await r.json();
    // Drop a stale snapshot for an instrument we've already switched away from (a slow
    // in-flight response must not flash the wrong instrument's OI/proposal/notes/title).
    if (d.symbol && d.symbol !== currentSymbol) return;
    lastPayload = d;
    $("dot").className = "dot live";
    $("meta").textContent = `as of ${d.ts} · fetched ${d.fetched_at}`;
    renderInstruments(d);
    renderChart(d); renderOI(d); renderStrategy();
    fetchChart(); fetchRecord(); fetchTable(); fetchPcrHistory(); fetchPending(); fetchBreadth();
    fetchMarketReads();                               // saved Market-view reads (browse all day)
    fetchTriggersLog();                               // triggers + analysis log (all instruments)
    fetchLivePos();                                   // open LIVE broker position (fill + trailing stop)
    if ($("scanAuto").checked) fetchScanner();        // auto-refresh the scanner (toggle)
    // The token banner is driven by the real Breeze connection state (refreshTokenStatus),
    // NOT by benign OI notes — so a valid token no longer re-prompts on every refresh.
    // No client-side auto-analyse: Claude auto-fires server-side once per new trigger
    // (all four tabs); the frozen head carries its cached read.
  } catch (e) {
    $("dot").className = "dot err"; $("meta").textContent = "error: " + e.message;
    flagTokenNeeded(true);     // a failing poll is often an expired token — offer entry
  }
}

// Surface the token entry when the feed looks unauthenticated (amber button + open form).
function flagTokenNeeded(needed) {
  $("tokenBtn").classList.toggle("warn", !!needed);
  if (needed) $("tokenForm").hidden = false;
}

// Pull the last-known Breeze token + connection state: PREFILL the field so the active
// token is visible/replaceable, and open the banner only when actually disconnected.
async function refreshTokenStatus() {
  try {
    const r = await fetch("/api/breeze-token");
    if (!r.ok) return;                 // GET only exists on the deployed cockpit
    const d = await r.json();
    const f = $("tokenInput");
    if (d.token && document.activeElement !== f) f.value = d.token;   // don't clobber typing
    flagTokenNeeded(!d.connected);     // re-prompt ONLY when Breeze is truly disconnected
  } catch (e) { /* offline / non-cockpit host — leave the banner as-is */ }
}

// POST today's Breeze token to the cockpit (applies it here; the in-process recorder picks
// it up, or it's forwarded to a separate recorder when RECORDER_URL is set).
async function postToken() {
  const token = $("tokenInput").value.trim();
  if (!token) return;
  $("tokenMsg").textContent = "saving…";
  try {
    const fd = new FormData(); fd.append("token", token);
    const r = await fetch("/api/breeze-token", { method: "POST", body: fd });
    if (r.status === 404) { $("tokenMsg").textContent = "token endpoint only on the deployed cockpit"; return; }
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const d = await r.json();
    $("tokenMsg").textContent = `cockpit: ${d.cockpit}  ·  recorder: ${d.recorder}`;
    // keep the saved token in the field (visibly active + replaceable) — don't blank it
    poll();                    // pick up the freshly authenticated feed
    refreshTokenStatus();      // refresh connection state + re-prefill
  } catch (e) {
    $("tokenMsg").textContent = "failed: " + e.message;
  }
}

// Render the active tab's GATED decision card off the frozen head trigger.
function renderStrategy() {
  const d = lastPayload;
  if (!d) return;
  const head = (d.heads || {})[currentStrat] || null;
  currentHead = head;
  $("stratNote").hidden = (currentStrat === "trade1");
  if (currentStrat !== "trade1") $("stratNote").textContent =
    "Mechanical chart trigger — Claude + OI auto-read and sized after the trigger. Propose-only: place it yourself (not auto-executed).";

  if (!head) { renderWatching(); }
  else if (currentStrat === "condor") { renderCondor((d.proposals || {}).condor || {}); $("decision").hidden = false; }
  else { renderHead(head); }

  // Claude's read (auto-fired per trigger, server-side) on every tab.
  const rd = head && head.read;
  if (rd) renderRead(rd);
  else $("readBox").innerHTML = `<span class="muted">${head ? "Analysing this trigger… (or press Analyse)" : "No active trigger — watching."}</span>`;

  fetchMarkers(currentStrat);     // chart ▲/▼ overlay follows the active tab (table is independent)
}

function setStrat(strat) {
  currentStrat = strat;
  document.querySelectorAll("#stratTabs button").forEach((b) =>
    b.classList.toggle("on", b.dataset.strat === strat));
  renderStrategy();
}

// Green when spot is above the level, red when below, neutral on missing data.
function colourTile(id, spot, level) {
  const el = $(id);
  if (!el) return;
  el.classList.remove("up", "down");
  if (spot == null || level == null || Number.isNaN(spot) || Number.isNaN(level)) return;
  el.classList.add(spot >= level ? "up" : "down");
}

function renderChart(d) {
  const c = d.chart, num = c.numbers || {}, lv = c.levels || {};
  $("chartTitle").textContent = `🕯️ ${d.symbol || currentSymbol} — candles · BB · `
    + "EMA 5/45/100/200 · Supertrend · CPR · MACD · RSI";
  $("spot").textContent = n(d.spot);
  $("mtf").textContent = (c.mtf_call || "—")
    + (c.mtf_confidence != null ? ` · 45EMA ${c.mtf_confidence}/5 ${mtfTicks(c.mtf_confidence_breakdown, c.mtf_call)}` : "");
  $("rsi").textContent = n(num.rsi_14); $("macd").textContent = n(num.macd_hist);
  $("st").textContent = n(num.supertrend); $("cpr").textContent = n(lv.cpr_pivot);
  $("ema5").textContent = n(num.ema_5); $("ema45").textContent = n(num.ema_45);
  $("ema100").textContent = n(num.ema_100); $("ema200").textContent = n(num.ema_200);
  // Colour each level tile by spot vs that level (green above, red below). Spot tile
  // follows the master 45-EMA regime (the at-a-glance long/short bias).
  const sp = d.spot;
  colourTile("tileEma5", sp, num.ema_5); colourTile("tileEma45", sp, num.ema_45);
  colourTile("tileEma100", sp, num.ema_100); colourTile("tileEma200", sp, num.ema_200);
  colourTile("tileSt", sp, num.supertrend); colourTile("tileCpr", sp, lv.cpr_pivot);
  colourTile("tileSpot", sp, num.ema_45);
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

// NSE-50 scanner: poll the cached scan; highlight stocks where trigger + OI + Claude agree.
async function fetchScanner() {
  try { renderScanner(await (await fetch("/api/scanner")).json()); } catch (e) { /* keep last */ }
}

async function scanRescan() {
  $("scanRefresh").disabled = true; $("scanStatus").textContent = "scanning…";
  try { renderScanner(await (await fetch("/api/scanner/refresh", { method: "POST" })).json()); }
  catch (e) { $("scanStatus").textContent = "rescan failed"; }
  $("scanRefresh").disabled = false;
}

function renderScanner(d) {
  const rows = (d.rows || []).filter((r) => r.trigger);     // only stocks with a live trigger
  _scanRows = rows;                                         // stash for the 💬 full-read lookup
  const t = d.at ? new Date(d.at * 1000).toLocaleTimeString() : "—";
  $("scanStatus").innerHTML = d.scanning ? "scanning…"
    : `${d.highlights || 0} ✅ agree · ${d.triggers || 0} triggers · last ${t}`
      + (d.error ? ` · <span class="loss-txt">error</span>` : "");
  let h = "<thead><tr><th>Stock</th><th>Spot</th><th>Trigger</th><th>Conf</th>"
    + "<th>OI</th><th>Claude</th><th>Action</th></tr></thead><tbody>";
  if (!rows.length) {
    h += `<tr><td colspan="7" class="muted">No stock triggers right now`
      + (d.at ? "" : " — the scanner runs 09:15–15:30 IST on the live service") + ".</td></tr>";
  }
  for (const r of rows) {
    const tg = r.trigger, cl = r.claude || {};
    // two tiers: green (scanhit) = full agreement (focus); yellow (scanwatch) = trigger only
    h += `<tr class="${r.highlight ? "scanhit" : "scanwatch"}"><td><b>${r.symbol}</b></td><td>${n(r.spot)}</td>`
      + `<td>${tg.direction} @ ${n(tg.entry)} <span class="muted">SL ${n(tg.stop)} / TP ${n(tg.target)}</span></td>`
      + `<td class="conf">${tg.mtf_confidence != null ? tg.mtf_confidence + "/5" : "—"}</td>`
      + `<td>${r.oi_bias || "—"}</td>`
      + `<td>${cl.recommendation || "—"}${cl.confidence != null ? " · C" + cl.confidence : ""}</td>`
      + `<td>`
      + (r.highlight && tg ? `<button class="btn ok" data-senter="${r.symbol}|${tg.ts}" title="Take this trade (record + track + focus); ₹ is points-based until stock lot sizes are set">Enter</button> ` : "")
      + (r.claude_full ? `<button class="btn" data-scanread="${r.symbol}" title="Read Claude's full analysis">💬</button> ` : "")
      + `<button class="btn csv" data-focus="${r.symbol}">Focus</button></td></tr>`;
  }
  $("scanTbl").innerHTML = h + "</tbody>";
}

// PCR / max-pain / walls+bands over time (the recorder's oi_summary series), per instrument.
async function fetchPcrHistory() {
  try {
    const d = await (await fetch(`/api/oi-history?symbol=${sym()}&day=${_pcrDay}`)).json();
    if (d.days) { _pcrDays = d.days; populatePcrDays(); }
    renderPcr(d.rows || []);
  } catch (e) { /* keep last */ }
}

// Diagnose an empty PCR panel: surface the recorder's live status from /healthz (the
// combined Railway service) so the trader can tell "after-hours / no data" from "token failing".
async function pcrRecorderStatus() {
  try {
    const s = await (await fetch("/healthz")).json();
    if (!s || s.recorder === undefined) return;
    const last = s.last_cycle ? s.last_cycle.slice(11, 16) : "—";
    const err = (s.errors || []).slice(-1)[0];
    $("pcrEmpty").textContent += `  ·  recorder: ${s.recorder} · last cycle ${last}`
      + ` · saved ${(s.saved || []).length}` + (err ? ` · last error: ${err}` : "");
  } catch (e) { /* standalone web.server has no /healthz — keep the base note */ }
}

// Download the recorder's saved data as CSV (Excel) for the active instrument + selected day.
function downloadCsv(kind) {
  const url = `/api/oi-download?symbol=${sym()}&day=${encodeURIComponent(_pcrDay)}&kind=${kind}`;
  const a = document.createElement("a");
  a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove();
}

function populatePcrDays() {
  const sel = $("pcrDay");
  if (sel.options.length !== _pcrDays.length + 1) {
    sel.innerHTML = `<option value="all">All days</option>`
      + _pcrDays.map((x) => `<option value="${x}">${x}</option>`).join("");
  }
  sel.value = _pcrDay;
}

function renderPcr(rows) {
  if (!rows.length) {                          // nothing recorded yet (recorder runs live)
    $("pcrChart").style.display = "none";
    $("pcrEmpty").hidden = false;
    $("pcrEmpty").textContent = "No OI history recorded yet for " + currentSymbol
      + " — the recorder accumulates it live (records 09:15–15:30 IST).";
    pcrRecorderStatus();                        // best-effort: append WHY (last cycle / errors)
    $("pcrTbl").innerHTML = "";
    return;
  }
  $("pcrChart").style.display = ""; $("pcrEmpty").hidden = true;
  const allDays = _pcrDay === "all";
  const x = rows.map((r) => allDays ? (r.ts || "").slice(5, 16).replace("T", " ")
                                    : (r.ts || "").slice(11, 16));
  const lvl = (key, name, color, dash) => ({
    type: "scatter", mode: "lines", name, x, y: rows.map((r) => r[key]), yaxis: "y2",
    line: { color, width: 1, dash: dash || "solid" }, connectgaps: true,
  });
  Plotly.react("pcrChart", [
    { type: "scatter", mode: "lines", name: "PCR", x, y: rows.map((r) => r.pcr),
      line: { color: "#e45756", width: 2 }, hovertemplate: "PCR %{y:.2f}<extra></extra>" },
    lvl("max_pain", "Max-pain", "#b07d2b"),
    lvl("spot", "Spot", "#9aa0b4"),
    lvl("call_wall_strike", "Call wall", "#e45756", "dot"),
    lvl("put_shelf_strike", "Put shelf", "#54a24b", "dot"),
    lvl("res_ext1", "Res band", "#e45756", "dash"),
    lvl("res_ext2", "Res band", "#e45756", "dash"),
    lvl("sup_ext1", "Sup band", "#54a24b", "dash"),
    lvl("sup_ext2", "Sup band", "#54a24b", "dash"),
  ], {
    height: 230, showlegend: true, legend: { orientation: "h", font: { size: 9 }, y: -0.25 },
    margin: { l: 40, r: 44, t: 8, b: 36 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#555", size: 10 },
    xaxis: { type: "category", tickangle: -45, nticks: 10, automargin: true },
    yaxis: { title: "PCR", zeroline: false },
    yaxis2: { title: "Price / strikes", overlaying: "y", side: "right", zeroline: false },
  }, { displayModeBar: false, responsive: true });

  // table beneath the graph
  let h = "<thead><tr><th>Time</th><th>PCR</th><th>Max-pain</th><th>Call wall</th>"
    + "<th>Put shelf</th><th>Res bands</th><th>Sup bands</th></tr></thead><tbody>";
  for (const r of rows.slice().reverse()) {              // newest first
    h += `<tr><td>${(r.ts || "").slice(5, 16).replace("T", " ")}</td><td class="conf">${n(r.pcr)}</td>`
      + `<td>${n(r.max_pain, 0)}</td><td>${n(r.call_wall_strike, 0)}</td><td>${n(r.put_shelf_strike, 0)}</td>`
      + `<td>${n(r.res_ext1, 0)} / ${n(r.res_ext2, 0)}</td><td>${n(r.sup_ext1, 0)} / ${n(r.sup_ext2, 0)}</td></tr>`;
  }
  $("pcrTbl").innerHTML = h + "</tbody>";
}

// Saved "Market view" reads — the day's on-demand Claude analyses (own table), so the
// trader can re-open any one in the poll-immune popup all session long. Per instrument.
async function fetchMarketReads() {
  try {
    const d = await (await fetch(`/api/market-reads?symbol=${sym()}&day=${_mrDay}`)).json();
    if (d.days) { _mrDays = d.days; populateMrDays(); }
    _mrRows = d.rows || [];
    renderMarketReads();
  } catch (e) { /* keep last */ }
}
function populateMrDays() {
  const sel = $("mrDay");
  if (!sel) return;
  if (sel.options.length !== _mrDays.length + 1) {
    sel.innerHTML = `<option value="all">All days</option>`
      + _mrDays.map((x) => `<option value="${x}">${x}</option>`).join("");
  }
  sel.value = _mrDay;
}
function renderMarketReads() {
  const el = $("mrList");
  if (!el) return;
  if (!_mrRows.length) {
    el.innerHTML = `<tr><td colspan="4" class="muted">No market reads yet — press `
      + `🔍 Market view to ask Claude; saved reads appear here to re-open all day.</td></tr>`;
    return;
  }
  let h = "<thead><tr><th>Time</th><th>Verdict</th><th>Conf</th><th></th></tr></thead><tbody>";
  for (const r of _mrRows) {                              // already newest-first from the server
    const rd = r.read || {}, enter = (rd.recommendation || "").toLowerCase() === "enter";
    const day = (r.ts || "").slice(5, 10), t = (r.ts || "").slice(11, 16);
    h += `<tr class="mrrow" data-mrts="${r.ts}"><td>${t}<span class="muted"> ${day}</span></td>`
      + `<td class="${enter ? "win-txt" : "muted"}">${enter ? "ENTER" : "stand down"}</td>`
      + `<td class="conf">${rd.confidence != null ? rd.confidence + "/5" : "—"}</td>`
      + `<td><button class="btn" data-mropen="${r.ts}">Open</button></td></tr>`;
  }
  el.innerHTML = h + "</tbody>";
}

// Triggers & analysis log — every trigger + Claude's rationale, date-wise, ALL instruments.
// Cross-instrument review/export (not tied to the active symbol), built from the journal.
async function fetchTriggersLog() {
  try {
    const d = await (await fetch(`/api/triggers-log?symbol=all&date=${_logDay}&strategy=${_logStrat}`)).json();
    if (d.days) { _logDays = d.days; populateLogDays(); }
    _logRows = d.rows || [];
    $("logCount").textContent = `${d.count || 0} trigger${(d.count || 0) === 1 ? "" : "s"}`;
    $("logCsv").href = `/api/triggers-export?symbol=all&date=${_logDay}&strategy=${_logStrat}`;
    renderTriggersLog();
  } catch (e) { /* keep last */ }
}
function populateLogDays() {
  const sel = $("logDay");
  if (!sel) return;
  if (sel.options.length !== _logDays.length + 1) {
    sel.innerHTML = `<option value="all">All days</option>`
      + _logDays.map((x) => `<option value="${x}">${x}</option>`).join("");
  }
  sel.value = _logDay;
}
function renderTriggersLog() {
  const el = $("logTbl");
  if (!el) return;
  if (!_logRows.length) {
    el.innerHTML = `<tr><td colspan="9" class="muted">No triggers logged yet — once a trigger `
      + `fires and Claude reads it, it's saved here (all instruments) to review or export.</td></tr>`;
    return;
  }
  let h = "<thead><tr><th>Date</th><th>Time</th><th>Instr</th><th>Strat</th><th>Dir</th>"
    + "<th>Entry/SL/TP</th><th>Claude</th><th>Decision · P&L</th><th></th></tr></thead><tbody>";
  for (const r of _logRows) {                              // already newest-first from the server
    const enter = (r.claude_reco || "").toLowerCase() === "enter";
    const lv = r.entry != null ? `${n(r.entry)} / ${n(r.stop)} / ${n(r.target)}` : "—";
    const cl = `<span class="${enter ? "win-txt" : "muted"}">${enter ? "ENTER" : "stand"}</span>`
      + (r.claude_conf != null ? ` C${r.claude_conf}` : "")
      + (r.oi_bias ? ` · ${r.oi_bias}` : "");
    const dec = r.decision ? r.decision : "<span class='muted'>—</span>";
    const pl = r.points != null ? ` · ${n(r.points, 1)}pt` : "";
    const out = r.outcome ? ` <span class="${r.outcome === "win" ? "win-txt" : (r.outcome === "loss" ? "loss-txt" : "muted")}">${r.outcome}</span>` : "";
    h += `<tr><td>${r.date}</td><td>${r.time}</td><td><b>${r.symbol}</b></td>`
      + `<td>${r.strategy}</td><td>${r.direction || "—"}</td><td>${lv}</td>`
      + `<td>${r.read ? cl : "<span class='muted'>—</span>"}</td>`
      + `<td>${dec}${out}${pl}</td>`
      + `<td>${r.read ? `<button class="btn" data-logread="${r.symbol}|${r.ts}">💬</button>` : ""}</td></tr>`;
  }
  el.innerHTML = h + "</tbody>";
}

// The calm idle state — no flickering ENTER/STAND-DOWN between triggers.
function renderWatching() {
  $("propBox").className = "propbox watching";
  $("propBox").innerHTML = "👁 Watching — no active trigger. "
    + "<span class='muted'>(approve/reject appears the moment one fires)</span>";
  $("decision").hidden = true;
}

// A PINNED, frozen trigger awaiting the trader's approve/reject (stable across polls).
function renderHead(head) {
  $("propBox").className = "propbox enter";
  const bySrc = head.levels_source === "claude"
    ? `🎯 levels by Claude` : `levels by engine`;
  $("propBox").innerHTML = `🔔 TRIGGER · ${head.direction.toUpperCase()} `
    + `<span class="muted">${(head.ts || "").slice(11, 16)}</span>`
    + `<br>Entry ${n(head.entry)} · Stop ${n(head.stop)} · Target ${n(head.target)}`
    + `<br>R:R ${head.rr} · ${head.size_lots} lots <span class="muted">(conviction ${head.mtf_confidence}/5)</span>`
    + `<br><span class="small muted">${bySrc} · pinned — won't advance until you approve/reject</span>`;
  $("decision").hidden = false;
  fillTicket(head);
}

// Prefill the order ticket from the frozen trigger (entry/stop/target/lots). Stock instruments
// trade equity (cash) sized by a Max-₹ cap; indices trade the option vehicle sized in lots.
function fillTicket(head) {
  const stock = !!(lastPayload && lastPayload.is_stock);
  $("otSl").value = head.stop != null ? head.stop : "";
  $("otTarget").value = head.target != null ? head.target : "";
  if (!$("otTicketTouched")) {                 // don't clobber a price the trader is editing
    $("otType").value = "market"; $("otPriceWrap").hidden = true;
  }
  $("otQtyWrap").hidden = stock;
  $("otMaxWrap").hidden = !stock;
  if (stock) { recomputeStockQty(); }
  else { $("otQty").value = head.size_lots != null ? head.size_lots : 1; $("otQtyCalc").textContent = "lots"; }
}

function recomputeStockQty() {
  const max = parseFloat($("otMax").value) || 0;
  const px = (lastPayload && lastPayload.spot) || 0;
  const q = px > 0 ? Math.floor(max / px) : 0;
  $("otQtyCalc").textContent = px > 0 ? `→ ${q} shares @ ₹${n(px)}` : "no price";
  return q;
}

// Non-directional defined-risk iron condor (propose-only — manual multi-leg entry).
function renderCondor(p) {
  const box = $("propBox");
  if (p.recommendation === "enter") {
    const L = (p.context && p.context.legs) || {};
    box.className = "propbox enter";
    box.innerHTML = `IRON ${L.mode === "fly" ? "FLY" : "CONDOR"} · <b>net credit ${n(p.entry)}</b>`
      + `<br>Sell ${L.short_put}PE / ${L.short_call}CE · wings ${L.long_put}PE / ${L.long_call}CE`
      + `<br>Breakevens ${n(L.be_low, 0)} / ${n(L.be_high, 0)} · max-loss ${n(L.max_loss)} pts/lot`
      + `<br>Exit ≥ ${n(p.stop)} premium · bank ≤ ${n(p.target)} <span class="muted">(${p.size_lots} lots, defined risk)</span>`
      + `<br><span class="small muted">Propose-only — place the 4 legs manually, confirm against OI walls.</span>`;
  } else {
    box.className = "propbox stand";
    box.innerHTML = "STAND DOWN — no expiry-day range setup "
      + "<span class='muted'>(needs squeeze + inside CPR, after 11:00 IST on expiry)</span>";
  }
}

function readHtml(rd) {
  const v = rd.recommendation === "enter";
  return `<div class="verdict ${v ? "enter" : "stand"}">Claude: ${v ? "ENTER" : "STAND DOWN"} · `
    + `${rd.agrees_with_engine ? "agrees with" : "DISAGREES with"} the engine · conf ${rd.confidence}/5</div>`
    + `<p><b>📈 Chart:</b> ${rd.chart_analysis}</p><p><b>🧮 OI:</b> ${rd.oi_analysis}</p>`
    + `<p><b>🧭 Where:</b> ${rd.where_moving}</p><p><b>🎯 Trade:</b> ${rd.right_trade}</p>`
    + `<p><b>⚔️ Challenge:</b> ${rd.challenge}</p><p><b>⚠️ Risk:</b> ${rd.key_risk}</p>`;
}
function renderReadInto(el, rd) { el.innerHTML = readHtml(rd); }
function renderRead(rd) { renderReadInto($("readBox"), rd); }   // live head → decision card (poll-managed)

// Chart ▲/▼ overlay follows the active CHART tab (independent of the table filter).
async function fetchMarkers(strat) {
  try {
    const d = await (await fetch(`/api/triggers?strategy=${strat}&symbol=${sym()}`)).json();
    _triggers = (d.triggers || []).filter((t) => t.direction === "long" || t.direction === "short");
  } catch (e) { /* keep last */ }
}

// The triggers TABLE: merged across strategies (filter), browseable by day, paginated.
async function fetchTable() {
  try {
    let url = `/api/triggers?strategy=${_trigStrat}&symbol=${sym()}`;
    if (_trigDate) url += `&date=${_trigDate}`;
    const d = await (await fetch(url)).json();
    _trigRows = d.triggers || [];
    _trigSummary = d.summary || {};
    _trigPending = d.pending || 0;
    _trigLast = d.last; _trigSession = d.session;
    if (d.dates) _trigDates = d.dates;
    if (d.strategies) _trigStrats = d.strategies;
    if (_trigDate === null && _trigDates.length) _trigDate = _trigDates[0];   // newest first
    populateTrigSelectors();
    renderTriggers();
  } catch (e) { /* keep last */ }
}

// Populate the instrument selector from the snapshot's instrument list.
function renderInstruments(d) {
  const list = (d.instruments || []).slice();
  const sel = $("instrSel");
  if (!sel || !list.length) return;
  // Include the ACTIVE symbol even when it's a scanner stock (not a primary index) so a
  // Focus switch is visible + selectable in the dropdown (else sel.value silently fails).
  if (!list.some((i) => i.id === currentSymbol))
    list.push({ id: currentSymbol, label: (d.symbol || currentSymbol) + " (stock)" });
  const want = list.map((i) => `<option value="${i.id}">${i.label}</option>`).join("");
  if (sel.dataset.opts !== want) { sel.innerHTML = want; sel.dataset.opts = want; }
  sel.value = currentSymbol;
}

function populateTrigSelectors() {
  const ds = $("trigDate");
  if (ds.options.length !== _trigDates.length) {
    ds.innerHTML = _trigDates.map((x) => `<option value="${x}">${x}</option>`).join("");
  }
  ds.value = _trigDate || "";
  const ss = $("trigStrat");
  if (ss.options.length !== _trigStrats.length + 1) {
    ss.innerHTML = `<option value="all">All</option>`
      + _trigStrats.map((s) => `<option value="${s.id}">${s.label}</option>`).join("");
  }
  ss.value = _trigStrat;
}

function renderTriggers() {
  const condor = _trigStrat === "condor";
  const s = _trigSummary, last = _trigLast;
  if (last) {
    const oc = last.outcome, t = last.ts.slice(11, 16);
    $("trigLast").className = "trig-last " + oc;
    $("trigLast").innerHTML = condor
      ? `Last setup ${t} · <b>CONDOR</b> shorts ${last.short_put}/${last.short_call} `
        + `· <b>${oc.toUpperCase()}</b> ${last.points >= 0 ? "+" : ""}${last.points} pts`
      : `Last trigger ${t} · <b>${last.direction.toUpperCase()}</b> @ ${last.entry} `
        + `→ stop ${last.stop} / target ${last.target} · <b>${oc.toUpperCase()}</b> `
        + `${last.points >= 0 ? "+" : ""}${last.points} pts (${last.rupees >= 0 ? "+" : ""}₹${last.rupees})`;
  } else {
    $("trigLast").className = "trig-last";
    $("trigLast").textContent = `No triggers on ${_trigSession || "—"} yet — market opens 09:15 IST.`;
  }
  const badge = (!condor && _trigPending > 0)
    ? `<span class="pending-badge" title="triggers you haven't decided on">${_trigPending} to review</span> ` : "";
  $("trigSummary").innerHTML = badge + `${s.n || 0} triggers · ${s.wins || 0}W / ${s.losses || 0}L / ${s.open || 0} open`
    + (s.exited ? ` / ${s.exited} exited` : "")
    + ` · net <b class="${s.net_points >= 0 ? "win-txt" : "loss-txt"}">${s.net_points >= 0 ? "+" : ""}${s.net_points || 0} pts `
    + `(${s.net_rupees >= 0 ? "+" : ""}₹${s.net_rupees || 0})</b> if all taken`
    + (s.hit_rate != null ? ` · hit-rate ${(s.hit_rate * 100).toFixed(0)}%` : "");

  const total = _trigRows.length;
  const pages = Math.max(1, Math.ceil(total / TRIG_PAGE));
  if (_trigPage >= pages) _trigPage = pages - 1;
  if (_trigPage < 0) _trigPage = 0;
  const pageRows = _trigRows.slice(_trigPage * TRIG_PAGE, _trigPage * TRIG_PAGE + TRIG_PAGE);

  let h = condor
    ? "<thead><tr><th>Time</th><th>Short PE</th><th>Short CE</th><th>Credit</th><th>Out</th><th>Pts</th><th>₹</th></tr></thead><tbody>"
    : "<thead><tr><th>Time</th><th>Strategy</th><th>Dir</th><th>Conf</th><th>Entry</th><th>Stop</th><th>Target</th><th>Out</th><th>Pts</th><th>₹</th><th>Claude</th><th>Action</th></tr></thead><tbody>";
  for (const t of pageRows) {
    const pts = `<td class="${t.points >= 0 ? "win" : "loss"}">${t.points >= 0 ? "+" : ""}${t.points}</td>`;
    const rs = `<td>${t.rupees >= 0 ? "+" : ""}${t.rupees}</td>`;
    const dir = t.direction === "long" || t.direction === "short";
    // Claude's auto-read for this trigger (cached per ts as it fired) — ENTER / stand-down · C0-5.
    const rd = t.read;
    const cl = rd && rd.recommendation
      ? `<td class="${rd.recommendation === "enter" ? "win" : "loss"}">${rd.recommendation === "enter" ? "ENTER" : "stand"}`
        + `${rd.confidence != null ? " C" + rd.confidence : ""}</td>`
      : `<td class="muted">…</td>`;
    // Action cell: decide ANY trigger (✓ take / ✗ reject / 💬 discuss) even after its live window,
    // then it's logged by date; Exit records a real fill. Already-decided / exited rows still get a
    // 💬 so Claude's read (or an on-demand re-ask) is viewable for EVERY directional trigger.
    const discuss = dir
      ? `<button class="btn" title="Discuss with Claude" data-discuss="${t.ts}" data-strat="${t.strategy || ""}">💬</button>`
      : "";
    let act;
    if (!dir) act = "<td></td>";
    else if (t.outcome === "exit") act = `<td class="trigact"><span class="muted">@ ${n(t.exit)}</span> ${discuss}</td>`;
    else if (t.actioned) act = `<td class="trigact"><span class="muted">${t.actioned === "approved" ? "✓ taken"
      : t.actioned === "rejected" ? "✗ rejected" : t.actioned}</span> ${discuss}</td>`;
    else act = `<td class="trigact">`
      + `<button class="btn ok" title="Approve / take — logged" data-decide="approve" data-ts="${t.ts}" data-strat="${t.strategy || ""}">✓</button>`
      + `<button class="btn no" title="Reject / stand down — logged" data-decide="reject" data-ts="${t.ts}" data-strat="${t.strategy || ""}">✗</button>`
      + discuss
      + `<button class="btn exit" title="Record a real fill" data-exit-ts="${t.ts}" data-strat="${t.strategy || ""}">Exit</button></td>`;
    h += condor
      ? `<tr><td>${t.ts.slice(11, 16)}</td><td>${t.short_put}</td><td>${t.short_call}</td>`
        + `<td>${t.credit}</td><td class="${t.outcome}">${t.outcome}</td>${pts}${rs}</tr>`
      : `<tr><td>${t.ts.slice(11, 16)}</td><td class="muted">${t.strategy_label || ""}</td><td>${t.direction}</td>`
        + `<td class="conf">${t.mtf_confidence != null ? t.mtf_confidence + "/5" : "—"}</td><td>${t.entry}</td>`
        + `<td>${t.stop}</td><td>${t.target}</td><td class="${t.outcome}">${t.outcome}</td>${pts}${rs}${cl}${act}</tr>`;
  }
  $("trigTbl").innerHTML = h + "</tbody>";
  $("trigPage").textContent = total ? `page ${_trigPage + 1}/${pages} · ${total} triggers` : "no triggers";
  $("trigPrev").disabled = _trigPage <= 0;
  $("trigNext").disabled = _trigPage >= pages - 1;
}

// Chart engine (Lightweight Charts module + ⚙ indicator panel) lives in chart.js,
// shared with the training page. `_triggers`, `chartTF`, `LW`, `initCharts`,
// `renderLW`, `wireChartUI` are provided there.

// Manually close an OPEN trigger at a price (defaults to the live spot) → realized P&L into
// the track record; the row flips to "exit". You square off on your own broker (propose-only).
async function exitTrigger(ts, strat) {
  const spot = lastPayload ? lastPayload.spot : null;
  const ans = prompt("Exit at price?", spot != null ? n(spot) : "");
  if (ans === null) return;                        // cancelled
  const px = parseFloat(ans);
  const fd = new FormData();
  fd.append("strategy", strat || currentStrat); fd.append("ts", ts);
  fd.append("symbol", currentSymbol);
  if (!Number.isNaN(px)) fd.append("exit_px", px);
  const r = await fetch("/api/exit", { method: "POST", body: fd });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { alert("Exit failed: " + (d.detail || r.statusText)); return; }
  fetchTable(); fetchRecord();                      // refresh the table + track record
}

async function fetchChart() {
  initCharts();
  if (!LW) return;
  try { renderLW(await (await fetch(`/api/chart?tf=${chartTF}&bars=200&symbol=${sym()}`)).json()); }
  catch (e) { /* keep last */ }
}

async function fetchRecord() {
  try { renderRecord(await (await fetch(`/api/record?symbol=${sym()}`)).json()); } catch (e) { /* keep */ }
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
  // Win-rate by conviction bucket — the "does higher conviction win more?" table.
  const conv = d.by_conviction || [];
  $("recConvWrap").hidden = conv.length === 0;
  if (conv.length) {
    let ch = "<thead><tr><th>Conviction</th><th>n</th><th>W/L</th><th>Hit-rate</th><th>Net pts</th><th>Exp/trade</th></tr></thead><tbody>";
    for (const b of conv) {
      const exp = b.expectancy;
      ch += `<tr><td class="conf">${b.conviction === "—" ? "—" : b.conviction + "/5"}</td><td>${b.n}</td>`
        + `<td>${b.wins}/${b.losses}</td><td>${b.hit_rate != null ? (b.hit_rate * 100).toFixed(0) + "%" : "—"}</td>`
        + `<td class="${b.net_points >= 0 ? "win" : "loss"}">${b.net_points >= 0 ? "+" : ""}${b.net_points}</td>`
        + `<td class="${(exp || 0) >= 0 ? "win" : "loss"}">${exp != null ? (exp >= 0 ? "+" : "") + exp : "—"}</td></tr>`;
    }
    $("recConv").innerHTML = ch + "</tbody>";
  }
  let h = "<thead><tr><th>Time</th><th>Decision</th><th>Conf</th><th>Process</th><th>Outcome</th><th>Pts</th><th>Cell</th></tr></thead><tbody>";
  for (const r of (d.recent || []).slice().reverse()) {
    const o = r.outcome || {}, t = r.ts ? r.ts.slice(11, 16) : "—";
    // engine conviction (0-5) + Claude's confidence (C1-5) at decision time
    const conf = (r.conviction != null ? r.conviction + "/5" : "—")
      + (r.confidence != null ? ` · C${r.confidence}` : "");
    h += `<tr><td>${t}</td><td>${r.decision || "—"} ${r.direction || ""}</td><td class="conf">${conf}</td><td>${r.process || "—"}</td>`
      + `<td class="${o.status || ""}">${o.status || "—"}</td>`
      + `<td class="${(o.points || 0) >= 0 ? "win" : "loss"}">${o.points != null ? (o.points >= 0 ? "+" : "") + o.points : "—"}</td>`
      + `<td>${r.matrix || "—"}</td></tr>`;
  }
  $("recTbl").innerHTML = h + "</tbody>";
  const posts = d.posts || [];
  $("recPosts").innerHTML = posts.length
    ? "<div class='pm-head'>🤖 Claude post-mortems</div>" + posts.slice().reverse().map((p) => {
        const t = p.ts ? p.ts.slice(11, 16) : "—";
        const lab = p.label ? ` · you: ${p.label}` : "";
        return `<div class="pm"><span class="muted">${t} ${p.direction || ""} ${p.outcome || ""}${lab}</span>`
          + `<div>${p.reason_why || ""}</div></div>`;
      }).join("")
    : "";
}

async function analyse() {
  if (!currentHead) { $("readBox").innerHTML = "<span class='muted'>No active trigger to analyse.</span>"; return; }
  analysing = true; $("analyseBtn").textContent = "Analysing…";
  try {
    const r = await fetch(`/api/analyse?strategy=${currentStrat}&symbol=${sym()}`, { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    renderRead(await r.json());
  } catch (e) { $("readBox").innerHTML = `<span class="muted">Claude unavailable: ${e.message}</span>`; }
  analysing = false; $("analyseBtn").textContent = "🤖 Analyse with Claude";
}

// On-demand MARKET view for the selected index — Claude reads the current chart + OI + macro,
// no trigger needed. Manual (one Claude call per click).
async function marketRead() {
  const btn = $("mktBtn");
  if (btn.disabled) return;
  btn.disabled = true; const old = btn.textContent; btn.textContent = "Analysing…";
  $("readBox").innerHTML = `<span class="muted">Asking Claude for the current ${currentSymbol} market view…</span>`;
  $("readBox").scrollIntoView({ behavior: "smooth", block: "center" });
  try {
    const r = await fetch(`/api/market-read?symbol=${sym()}`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
    $("readBox").innerHTML = "";                          // hand off to the poll-immune popup
    const t = d.ts ? (("" + d.ts).slice(11, 16)) : "";
    openAnalysisModal({ symbol: sym(), kind: "market", read: d,
                        title: `${currentSymbol} · market view${t ? " · " + t : ""}` });
    fetchMarketReads();                                   // saved-reads card picks it up
  } catch (e) { $("readBox").innerHTML = `<span class="muted">Market read failed: ${e.message}</span>`; }
  btn.disabled = false; btn.textContent = old;
}

// Swap the card to the next trigger INSTANTLY off the decision response's `next_head`
// (no snapshot round-trip), then kick Claude's read for it asynchronously.
function advanceTo(nextHead) {
  currentHead = nextHead || null;
  document.querySelectorAll('input[name="liveLabel"]').forEach((el) => { el.checked = false; });
  if (currentHead) { renderHead(currentHead); $("decision").hidden = false; }
  else renderWatching();
  $("readBox").innerHTML = currentHead
    ? "<span class='muted'>Analysing this trigger…</span>" : "";
  fetchMarkers(currentStrat); fetchTable(); fetchRecord();   // chart, table + track record reflect the action
  if (currentHead) analyse();                        // load Claude's read without blocking the card
}

async function decide(action) {
  if (!currentHead) { $("decisionMsg").textContent = "No active trigger to decide."; return; }
  const acted = currentHead;
  const fd = new FormData();
  fd.append("action", action); fd.append("strategy", currentStrat); fd.append("ts", acted.ts);
  fd.append("symbol", currentSymbol);
  const lbl = document.querySelector('input[name="liveLabel"]:checked');
  if (lbl) fd.append("label", lbl.value);
  if (action === "approve") appendTicket(fd);            // order ticket → broker params
  const r = await fetch("/api/decision", { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok) { $("decisionMsg").textContent = "⚠ " + (d.detail || "decision failed"); return; }
  const verb = action === "approve" ? "Approved" : action === "skip" ? "Skipped" : "Rejected";
  const conv = acted.mtf_confidence != null ? ` · conviction ${acted.mtf_confidence}/5` : "";
  $("decisionMsg").textContent = `${verb} ${currentStrat} ${(acted.ts || "").slice(11, 16)}` + conv
    + (action === "skip" ? " · not recorded"
       : ` · logged ${d.logged} · ${d.status || "—"}` + (lbl ? ` · trigger ${lbl.value}` : ""));
  advanceTo(d.next_head);        // instant swap to the next pending trigger (if any)
  if (action === "approve") { fetchLivePos(); }
}

// Pack the order-ticket fields into the decision/stock-enter form.
function appendTicket(fd) {
  const t = $("otType").value;
  fd.append("order_type", t);
  if (t === "limit" && $("otPrice").value) fd.append("limit_price", $("otPrice").value);
  if (lastPayload && lastPayload.is_stock) {
    if ($("otMax").value) fd.append("max_amount", $("otMax").value);
  } else if ($("otQty").value) {
    fd.append("qty", $("otQty").value);          // LOTS for an index option
  }
  if ($("otSl").value) fd.append("sl", $("otSl").value);
  if ($("otTarget").value) fd.append("target_px", $("otTarget").value);
  if ($("otTsl").value) fd.append("tsl", $("otTsl").value);
  if ($("otLive").checked) fd.append("live", "true");
}

// Live broker position panel: fill state, live (trailing) stop, manual square-off.
async function fetchLivePos() {
  try {
    const d = await (await fetch(`/api/order-status?symbol=${sym()}&strategy=${currentStrat}`)).json();
    renderLivePos(d);
  } catch (e) { /* keep */ }
}
function renderLivePos(d) {
  const el = $("livePos");
  if (!el) return;
  if (!d || !d.open) { el.hidden = true; el.innerHTML = ""; return; }
  const p = d.position || {}, b = d.broker || {};
  el.hidden = false;
  el.innerHTML = `🔴 LIVE ${(p.direction || "").toUpperCase()} ${p.symbol} `
    + `<span class="muted">${(p.ts || "").slice(11, 16)}</span> · ${p.qty} ${p.segment === "equity" ? "sh" : "qty"}`
    + `<br>Entry ${n(p.entry)} · Stop ${n(p.stop)}${p.tsl_points ? ` <span class="muted">(TSL ${p.tsl_points})</span>` : ""} · Target ${n(p.target)}`
    + `<br><span class="small muted">${p.entry_filled ? "filled" : "pending fill"}`
    + `${b && b.status ? " · broker " + b.status : ""}${p.broker_order_id ? " · #" + p.broker_order_id : ""}</span>`
    + ` <button id="sqOff" class="btn no" title="Square off this position at market now">⛔ Square off</button>`;
  $("sqOff").onclick = squareOff;
}
async function squareOff() {
  if (!confirm("Square off the live position at market now?")) return;
  const fd = new FormData(); fd.append("symbol", currentSymbol); fd.append("strategy", currentStrat);
  const r = await fetch("/api/square-off", { method: "POST", body: fd });
  const d = await r.json();
  $("decisionMsg").textContent = r.ok ? "⛔ squared off" : "⚠ " + (d.detail || "square-off failed");
  fetchLivePos(); fetchTable(); fetchRecord();
}

async function sendChat(ev) {
  ev.preventDefault();
  const text = $("chatText").value.trim(), file = $("chatFile").files[0];
  if (!text && !file) return;
  const fd = new FormData(); fd.append("text", text); fd.append("symbol", currentSymbol);
  if (file) fd.append("files", file);
  appendMsg("user", text, file);
  $("chatText").value = ""; $("chatFile").value = "";
  const r = await fetch("/api/chat", { method: "POST", body: fd });
  const d = await r.json(); appendMsg("assistant", d.reply);
}

function appendMsgTo(logId, role, text, file) {
  const div = document.createElement("div"); div.className = "msg " + role;
  div.textContent = text || "";
  if (file) { const img = document.createElement("img"); img.src = URL.createObjectURL(file); div.appendChild(img); }
  $(logId).appendChild(div); $(logId).scrollTop = $(logId).scrollHeight;
}
function appendMsg(role, text, file) { appendMsgTo("chatLog", role, text, file); }

// ---- Persistent analysis + chat popup (any 💬; survives polling until ✕) ---- //
let _modal = { symbol: null, ts: null, strat: "trade1", kind: "trigger" };

function openAnalysisModal({ symbol, ts, strat, read, title, kind }) {
  _modal = { symbol: symbol || currentSymbol, ts: ts || null, strat: strat || "trade1", kind: kind || "trigger" };
  $("modalTitle").textContent = title
    || `${_modal.symbol}${ts ? " · " + (("" + ts).slice(11, 16) || ts) : ""}`;
  const body = $("modalBody");
  if (read && read.recommendation) renderReadInto(body, read);
  else body.innerHTML = "<span class='muted'>No saved read yet — re-ask Claude for a view on this level.</span>";
  _modalReaskButton();
  $("modalChatLog").innerHTML = "";
  $("analysisModal").hidden = false;
  // Give the discussed instrument a server-side snapshot so its chat has context (no-op if active).
  if (_modal.symbol && _modal.symbol !== currentSymbol)
    fetch(`/api/snapshot?symbol=${encodeURIComponent(_modal.symbol)}`).catch(() => { });
}
function closeAnalysisModal() { $("analysisModal").hidden = true; }

function _modalReaskButton() {
  if (!_modal.ts && _modal.kind !== "market") return;   // market reads re-ask without a ts
  const b = document.createElement("button");
  b.className = "btn"; b.style.marginTop = "6px"; b.textContent = "🔄 re-ask Claude";
  b.onclick = _modalReask;
  $("modalBody").appendChild(b);
}
async function _modalReask() {
  $("modalBody").insertAdjacentHTML("beforeend", "<div class='muted'>re-asking Claude…</div>");
  try {
    if (_modal.kind === "market") {                      // fresh market view (no trigger ts)
      const r = await fetch(`/api/market-read?symbol=${encodeURIComponent(_modal.symbol)}`, { method: "POST" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      renderReadInto($("modalBody"), d); _modalReaskButton(); fetchMarketReads();
      return;
    }
    const fd = new FormData();
    fd.append("strategy", _modal.strat); fd.append("ts", _modal.ts); fd.append("symbol", _modal.symbol);
    const r = await fetch("/api/reask", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
    renderReadInto($("modalBody"), d); _modalReaskButton();
    fetchTable(); fetchPending();
  } catch (e) { $("modalBody").insertAdjacentHTML("beforeend", `<div class='muted'>re-ask failed: ${e.message}</div>`); }
}
async function modalSendChat(ev) {
  ev.preventDefault();
  const text = $("modalChatText").value.trim(), file = $("modalChatFile").files[0];
  if (!text && !file) return;
  const fd = new FormData();
  fd.append("text", text); fd.append("symbol", _modal.symbol || currentSymbol);
  if (file) fd.append("files", file);
  appendMsgTo("modalChatLog", "user", text, file);
  $("modalChatText").value = ""; $("modalChatFile").value = "";
  try {
    const r = await fetch("/api/chat", { method: "POST", body: fd });
    const d = await r.json();
    appendMsgTo("modalChatLog", "assistant", r.ok ? d.reply : (d.detail || "chat unavailable"));
  } catch (e) { appendMsgTo("modalChatLog", "assistant", "chat unavailable"); }
}

$("analyseBtn").onclick = analyse;
$("mktBtn").onclick = marketRead;
$("approveBtn").onclick = () => decide("approve");
$("rejectBtn").onclick = () => decide("reject");
$("skipBtn").onclick = () => decide("skip");
$("otType").addEventListener("change", (e) => {
  $("otPriceWrap").hidden = e.target.value !== "limit";
  if (e.target.value === "limit" && !$("otPrice").value && currentHead)
    $("otPrice").value = currentHead.entry != null ? currentHead.entry : "";
});
$("otMax").addEventListener("input", recomputeStockQty);
$("tokenBtn").onclick = () => { $("tokenForm").hidden = !$("tokenForm").hidden; };
$("tokenSave").onclick = postToken;
$("tokenInput").addEventListener("keydown", (e) => { if (e.key === "Enter") postToken(); });
$("trigTbl").addEventListener("click", (e) => {     // per-row actions on the triggers table
  const ex = e.target.closest("button[data-exit-ts]");
  if (ex) { exitTrigger(ex.dataset.exitTs, ex.dataset.strat); return; }
  const dc = e.target.closest("button[data-decide]");
  if (dc) { decideTrigger(dc.dataset.ts, dc.dataset.strat, dc.dataset.decide); return; }
  const ds = e.target.closest("button[data-discuss]");
  if (ds) discussTrigger(ds.dataset.discuss, ds.dataset.strat);
});

// Approve / reject / skip ANY trigger by ts on a given instrument (defaults to the active one).
async function decideTrigger(ts, strat, action, symbol) {
  const fd = new FormData();
  fd.append("action", action); fd.append("strategy", strat || "trade1");
  fd.append("ts", ts); fd.append("symbol", symbol || currentSymbol);
  const r = await fetch("/api/decision", { method: "POST", body: fd });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { alert(action[0].toUpperCase() + action.slice(1) + " failed: " + (d.detail || r.statusText)); return; }
  fetchTable(); fetchRecord(); fetchPending();        // the inbox drops the decided row
}

// Take a SCANNER stock trade in one click: record it server-side, then focus to watch it live.
async function enterStock(key) {
  const [symbol, ts] = key.split("|");
  const fd = new FormData();
  fd.append("symbol", symbol); fd.append("ts", ts); fd.append("strategy", "trade1");
  const r = await fetch("/api/stock-enter", { method: "POST", body: fd });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { alert("Enter failed: " + (d.detail || r.statusText)); return; }
  focusInstrument(symbol);                            // record + focus (watch what happens)
}

// Open the popup with Claude's saved read for a trigger (cross-instrument via symbol).
// Re-ask is handled inside the modal (_modalReask) so it never gets clobbered by polling.
async function discussTrigger(ts, strat, symbol) {
  let rd = null;
  try {
    rd = await (await fetch(
      `/api/trigger-read?strategy=${strat || "trade1"}&ts=${encodeURIComponent(ts)}&symbol=${symbol || currentSymbol}`)).json();
  } catch (e) { /* no saved read — the modal offers re-ask */ }
  openAnalysisModal({ symbol: symbol || currentSymbol, ts, strat: strat || "trade1",
    read: (rd && rd.recommendation) ? rd : null });
}

// One-shot alert tone for a fresh trigger (best-effort; browsers may gate audio on a gesture).
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.type = "sine"; o.frequency.value = 880;
    g.gain.setValueAtTime(0.12, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
    o.start(); o.stop(ctx.currentTime + 0.25);
  } catch (e) { /* audio blocked until a user gesture — silent is fine */ }
}

// Live pending-trigger inbox: poll today's undecided triggers, alert on a new one.
async function fetchPending() {
  try {
    const d = await (await fetch(`/api/pending?symbol=${sym()}`)).json();
    renderPending(d.rows || [], d.count || 0);
  } catch (e) { /* keep last */ }
}

let _pendingRows = [];                               // last inbox rows (for the stock 💬 lookup)

function renderPending(rows, count) {
  const card = $("pendingCard");
  _pendingRows = rows || [];
  if (!count) { card.hidden = true; _seenPending.clear(); return; }
  card.hidden = false;
  $("pendingHdr").textContent = `🔔 Pending triggers — decide each (${count})`;
  let fresh = false;                                  // alert once per genuinely-new item (per instrument+ts)
  for (const r of rows) { const k = (r.symbol || "") + "|" + (r.ts || ""); if (!_seenPending.has(k)) { _seenPending.add(k); fresh = true; } }
  if (fresh) { card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash"); beep(); }
  let h = "<thead><tr><th>Time</th><th>Symbol</th><th>Strategy</th><th>Trade</th><th>Claude</th><th>Action</th></tr></thead><tbody>";
  for (const r of rows) {
    const rd = r.read;
    const cl = rd && rd.recommendation
      ? `<span class="${rd.recommendation === "enter" ? "win-txt" : "loss-txt"}">${rd.recommendation === "enter" ? "ENTER" : "stand"}${rd.confidence != null ? " C" + rd.confidence : ""}</span>`
      : "<span class='muted'>…</span>";
    const sy = r.symbol || "", st = r.strategy || "";
    const sym = `${r.highlight ? "🟢 " : ""}<b>${r.symbol_label || sy}</b>`;
    let act;
    if (r.kind === "stock") {                          // screener candidate — focus the stock to act
      act = (r.highlight ? `<button class="btn ok" title="Take this trade (record + track + focus)" data-senter="${sy}|${r.ts}">Enter</button> ` : "")
        + `<button class="btn csv" title="Open this stock's cockpit" data-focus="${sy}">Focus</button>`
        + `<button class="btn" title="Claude's read" data-sdiscuss="${sy}|${r.ts}">💬</button>`;
    } else {                                           // index trigger — decide inline on its instrument
      const a = (lbl, t, act2) => `<button class="btn ${act2 === "approve" ? "ok" : act2 === "reject" ? "no" : ""}" title="${t}" data-pdecide="${act2}" data-ts="${r.ts}" data-strat="${st}" data-sym="${sy}">${lbl}</button>`;
      act = a("✓", "Approve / take", "approve") + a("✗", "Reject / stand down", "reject")
        + a("⤼", "Skip (not recorded)", "skip")
        + `<button class="btn" title="Discuss with Claude" data-pdiscuss="${r.ts}" data-strat="${st}" data-sym="${sy}">💬</button>`;
    }
    h += `<tr><td>${(r.ts || "").slice(11, 16)}</td><td>${sym}</td><td class="muted">${r.strategy_label || ""}</td>`
      + `<td>${r.direction} @ ${n(r.entry)} <span class="muted">SL ${n(r.stop)} / TP ${n(r.target)}</span></td>`
      + `<td>${cl}</td><td class="trigact">${act}</td></tr>`;
  }
  $("pendingTbl").innerHTML = h + "</tbody>";
}

// Show a scanner stock's full read (already in the row — no server round-trip).
function discussStock(key) {
  const r = _pendingRows.find(x => ((x.symbol || "") + "|" + (x.ts || "")) === key);
  openAnalysisModal({ symbol: (r && r.symbol) || key.split("|")[0], ts: r && r.ts,
    strat: (r && r.strategy) || "trade1", read: (r && r.claude_full) || null });
}
// NIFTY-50 breadth: advance/decline tally + top-20 heavyweights' contribution to the index.
async function fetchBreadth() {
  try { renderBreadth(await (await fetch("/api/breadth")).json()); } catch (e) { /* keep last */ }
}

function renderBreadth(d) {
  const adv = d.advance || 0, dec = d.decline || 0, unch = d.unchanged || 0;
  const np = d.net_points;
  const tally = (adv || dec || unch)
    ? `<b class="win-txt">${adv}</b> : <b class="loss-txt">${dec}</b> adv:dec`
      + (unch ? ` · ${unch} unch` : "")
      + (np != null ? ` · net <b class="${np >= 0 ? "win-txt" : "loss-txt"}">${np >= 0 ? "+" : ""}${np}</b> pts` : "")
    : "no breadth yet — scanner runs 09:15–15:30 IST (needs the stock scanner on)";
  $("breadthTally").innerHTML = tally;
  const rows = d.rows || [];
  let h = "<thead><tr><th>Stock</th><th>Wt%</th><th>O</th><th>H</th><th>L</th><th>LTP</th>"
    + "<th>Vol</th><th>%Chg</th><th>Contrib</th></tr></thead><tbody>";
  if (!rows.length) h += `<tr><td colspan="9" class="muted">—</td></tr>`;
  for (const r of rows) {
    const pc = r.pct_change, ct = r.contribution;
    const pcl = pc == null ? "" : (pc >= 0 ? "win-txt" : "loss-txt");
    const ccl = ct == null ? "" : (ct >= 0 ? "win-txt" : "loss-txt");
    h += `<tr><td><b>${r.symbol}</b></td><td class="muted">${n(r.weight)}</td>`
      + `<td>${n(r.open)}</td><td>${n(r.high)}</td><td>${n(r.low)}</td><td>${n(r.close)}</td>`
      + `<td class="muted">${r.volume != null ? Math.round(r.volume).toLocaleString() : "—"}</td>`
      + `<td class="${pcl}">${pc == null ? "—" : (pc >= 0 ? "+" : "") + pc + "%"}</td>`
      + `<td class="${ccl}">${ct == null ? "—" : (ct >= 0 ? "+" : "") + ct}</td></tr>`;
  }
  $("breadthTbl").innerHTML = h + "</tbody>";
}

// Switch the whole cockpit to an instrument (index dropdown OR a scanner stock).
function focusInstrument(symbolName) {
  currentSymbol = symbolName;
  _trigDate = null; _trigPage = 0; _triggers = []; currentHead = null;
  _pcrDay = "all"; _pcrDays = [];                  // PCR history follows the active instrument
  _mrDay = "all"; _mrDays = []; _mrRows = [];      // market reads follow the active instrument
  resetChart();                                    // wipe the prior instrument's candles (no overlap)
  $("chatLog").innerHTML = "";
  $("dot").className = "dot"; $("meta").textContent = `loading ${currentSymbol}…`;
  poll();
}
$("instrSel").addEventListener("change", (e) => focusInstrument(e.target.value));
$("trigDate").addEventListener("change", (e) => { _trigDate = e.target.value; _trigPage = 0; fetchTable(); });
$("trigStrat").addEventListener("change", (e) => { _trigStrat = e.target.value; _trigPage = 0; fetchTable(); });
$("pcrDay").addEventListener("change", (e) => { _pcrDay = e.target.value; fetchPcrHistory(); });
$("mrDay").addEventListener("change", (e) => { _mrDay = e.target.value; fetchMarketReads(); });
$("mrList").addEventListener("click", (e) => {     // re-open a saved market read in the popup
  const b = e.target.closest("button[data-mropen]");
  if (!b) return;
  const row = _mrRows.find((r) => r.ts === b.dataset.mropen);
  if (!row) return;
  const t = (row.ts || "").slice(11, 16), day = (row.ts || "").slice(0, 10);
  openAnalysisModal({ symbol: currentSymbol, kind: "market", read: row.read,
                      title: `${currentSymbol} · market view · ${day} ${t}` });
});
$("logDay").addEventListener("change", (e) => { _logDay = e.target.value; fetchTriggersLog(); });
$("logStrat").addEventListener("change", (e) => { _logStrat = e.target.value; fetchTriggersLog(); });
$("logTbl").addEventListener("click", (e) => {     // open a trigger's full Claude rationale in the popup
  const b = e.target.closest("button[data-logread]");
  if (!b) return;
  const [sy, ts] = b.dataset.logread.split("|");
  const row = _logRows.find((r) => r.symbol === sy && r.ts === ts);
  if (!row || !row.read) return;
  openAnalysisModal({ symbol: sy, ts, strat: row.strategy, read: row.read,
                      title: `${sy} · ${row.strategy} · ${row.date} ${row.time}` });
});
$("scanRefresh").onclick = scanRescan;
$("scanAuto").checked = localStorage.getItem("scanAuto") !== "0";   // restore the toggle
$("scanAuto").addEventListener("change", (e) => {
  localStorage.setItem("scanAuto", e.target.checked ? "1" : "0");
  if (e.target.checked) fetchScanner();              // refresh immediately when re-enabled
});
$("pendingTbl").addEventListener("click", (e) => {  // inbox row actions (cross-instrument)
  const d = e.target.closest("button[data-pdecide]");
  if (d) { decideTrigger(d.dataset.ts, d.dataset.strat, d.dataset.pdecide, d.dataset.sym); return; }
  const en = e.target.closest("button[data-senter]");
  if (en) { enterStock(en.dataset.senter); return; }            // stock → record + focus
  const f = e.target.closest("button[data-focus]");
  if (f) { focusInstrument(f.dataset.focus); return; }          // stock → load its cockpit
  const s = e.target.closest("button[data-sdiscuss]");
  if (s) { discussStock(s.dataset.sdiscuss); return; }          // stock → show its scanner read
  const c = e.target.closest("button[data-pdiscuss]");
  if (c) discussTrigger(c.dataset.pdiscuss, c.dataset.strat, c.dataset.sym);
});
$("scanTbl").addEventListener("click", (e) => {     // scanner row actions
  const en = e.target.closest("button[data-senter]");
  if (en) { enterStock(en.dataset.senter); return; }      // record + track + focus
  const f = e.target.closest("button[data-focus]");
  if (f) { focusInstrument(f.dataset.focus); return; }    // load that stock's full cockpit
  const rd = e.target.closest("button[data-scanread]");   // read Claude's full analysis for the stock
  if (rd) {
    const row = _scanRows.find((x) => x.symbol === rd.dataset.scanread);
    if (row) openAnalysisModal({ symbol: row.symbol, ts: (row.trigger || {}).ts,
      strat: "trade1", read: row.claude_full || null });
  }
});
$("dlPcr").onclick = () => downloadCsv("summary");
$("dlChain").onclick = () => downloadCsv("chain");
$("trigPrev").onclick = () => { if (_trigPage > 0) { _trigPage--; renderTriggers(); } };
$("trigNext").onclick = () => { _trigPage++; renderTriggers(); };
$("chatForm").onsubmit = sendChat;
$("modalChatForm").onsubmit = modalSendChat;
$("modalClose").onclick = closeAnalysisModal;
$("analysisModal").addEventListener("click", (e) => { if (e.target.dataset.close) closeAnalysisModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("analysisModal").hidden) closeAnalysisModal(); });
document.querySelectorAll("#stratTabs button").forEach((b) =>
  b.addEventListener("click", () => setStrat(b.dataset.strat)));
wireChartUI(fetchChart);          // timeframe buttons + ⚙ indicator panel (chart.js)
poll(); setInterval(poll, POLL_MS);
refreshTokenStatus(); setInterval(refreshTokenStatus, 60000);   // token prefill + connection state
