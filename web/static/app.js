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
let _scanRows = [];                          // last scanner rows (for the 💬 full-read lookup)

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
    fetchChart(); fetchRecord(); fetchTable(); fetchPcrHistory();
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

function renderRead(rd) {
  const v = rd.recommendation === "enter";
  $("readBox").innerHTML =
    `<div class="verdict ${v ? "enter" : "stand"}">Claude: ${v ? "ENTER" : "STAND DOWN"} · `
    + `${rd.agrees_with_engine ? "agrees with" : "DISAGREES with"} the engine · conf ${rd.confidence}/5</div>`
    + `<p><b>📈 Chart:</b> ${rd.chart_analysis}</p><p><b>🧮 OI:</b> ${rd.oi_analysis}</p>`
    + `<p><b>🧭 Where:</b> ${rd.where_moving}</p><p><b>🎯 Trade:</b> ${rd.right_trade}</p>`
    + `<p><b>⚔️ Challenge:</b> ${rd.challenge}</p><p><b>⚠️ Risk:</b> ${rd.key_risk}</p>`;
}

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
    // then it's logged by date; Exit records a real fill. Already-decided rows show the verdict.
    let act;
    if (t.outcome === "exit") act = `<td class="muted">@ ${n(t.exit)}</td>`;
    else if (!dir) act = "<td></td>";
    else if (t.actioned) act = `<td class="muted">${t.actioned === "approved" ? "✓ taken"
      : t.actioned === "rejected" ? "✗ rejected" : t.actioned}</td>`;
    else act = `<td class="trigact">`
      + `<button class="btn ok" title="Approve / take — logged" data-decide="approve" data-ts="${t.ts}" data-strat="${t.strategy || ""}">✓</button>`
      + `<button class="btn no" title="Reject / stand down — logged" data-decide="reject" data-ts="${t.ts}" data-strat="${t.strategy || ""}">✗</button>`
      + `<button class="btn" title="Discuss with Claude" data-discuss="${t.ts}" data-strat="${t.strategy || ""}">💬</button>`
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
  const r = await fetch("/api/decision", { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok) { $("decisionMsg").textContent = "⚠ " + (d.detail || "decision failed"); return; }
  const verb = action === "approve" ? "Approved" : action === "skip" ? "Skipped" : "Rejected";
  const conv = acted.mtf_confidence != null ? ` · conviction ${acted.mtf_confidence}/5` : "";
  $("decisionMsg").textContent = `${verb} ${currentStrat} ${(acted.ts || "").slice(11, 16)}` + conv
    + (action === "skip" ? " · not recorded"
       : ` · logged ${d.logged} · ${d.status || "—"}` + (lbl ? ` · trigger ${lbl.value}` : ""));
  advanceTo(d.next_head);        // instant swap to the next pending trigger (if any)
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

function appendMsg(role, text, file) {
  const div = document.createElement("div"); div.className = "msg " + role;
  div.textContent = text || "";
  if (file) { const img = document.createElement("img"); img.src = URL.createObjectURL(file); div.appendChild(img); }
  $("chatLog").appendChild(div); $("chatLog").scrollTop = $("chatLog").scrollHeight;
}

$("analyseBtn").onclick = analyse;
$("approveBtn").onclick = () => decide("approve");
$("rejectBtn").onclick = () => decide("reject");
$("skipBtn").onclick = () => decide("skip");
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

// Approve / reject ANY trigger row by ts (logged by date with Claude's read), then refresh.
async function decideTrigger(ts, strat, action) {
  const fd = new FormData();
  fd.append("action", action); fd.append("strategy", strat || "trade1");
  fd.append("ts", ts); fd.append("symbol", currentSymbol);
  const r = await fetch("/api/decision", { method: "POST", body: fd });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { alert((action === "approve" ? "Approve" : "Reject") + " failed: " + (d.detail || r.statusText)); return; }
  fetchTable(); fetchRecord();
}

// Show Claude's full saved read for a trigger and jump to the chat to discuss it.
async function discussTrigger(ts, strat) {
  try {
    const rd = await (await fetch(
      `/api/trigger-read?strategy=${strat || "trade1"}&ts=${encodeURIComponent(ts)}&symbol=${sym()}`)).json();
    if (rd && rd.recommendation) renderRead(rd);
    else $("readBox").innerHTML = "<span class='muted'>No saved read for this trigger yet.</span>";
  } catch (e) { $("readBox").innerHTML = "<span class='muted'>No saved read for this trigger.</span>"; }
  $("readBox").scrollIntoView({ behavior: "smooth", block: "center" });
}
// Switch the whole cockpit to an instrument (index dropdown OR a scanner stock).
function focusInstrument(symbolName) {
  currentSymbol = symbolName;
  _trigDate = null; _trigPage = 0; _triggers = []; currentHead = null;
  _pcrDay = "all"; _pcrDays = [];                  // PCR history follows the active instrument
  resetChart();                                    // wipe the prior instrument's candles (no overlap)
  $("chatLog").innerHTML = "";
  $("dot").className = "dot"; $("meta").textContent = `loading ${currentSymbol}…`;
  poll();
}
$("instrSel").addEventListener("change", (e) => focusInstrument(e.target.value));
$("trigDate").addEventListener("change", (e) => { _trigDate = e.target.value; _trigPage = 0; fetchTable(); });
$("trigStrat").addEventListener("change", (e) => { _trigStrat = e.target.value; _trigPage = 0; fetchTable(); });
$("pcrDay").addEventListener("change", (e) => { _pcrDay = e.target.value; fetchPcrHistory(); });
$("scanRefresh").onclick = scanRescan;
$("scanAuto").checked = localStorage.getItem("scanAuto") !== "0";   // restore the toggle
$("scanAuto").addEventListener("change", (e) => {
  localStorage.setItem("scanAuto", e.target.checked ? "1" : "0");
  if (e.target.checked) fetchScanner();              // refresh immediately when re-enabled
});
$("scanTbl").addEventListener("click", (e) => {     // scanner row actions
  const f = e.target.closest("button[data-focus]");
  if (f) { focusInstrument(f.dataset.focus); return; }    // load that stock's full cockpit
  const rd = e.target.closest("button[data-scanread]");   // read Claude's full analysis for the stock
  if (rd) {
    const row = _scanRows.find((x) => x.symbol === rd.dataset.scanread);
    if (row && row.claude_full) {
      renderRead(row.claude_full);
      $("readBox").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
});
$("dlPcr").onclick = () => downloadCsv("summary");
$("dlChain").onclick = () => downloadCsv("chain");
$("trigPrev").onclick = () => { if (_trigPage > 0) { _trigPage--; renderTriggers(); } };
$("trigNext").onclick = () => { _trigPage++; renderTriggers(); };
$("chatForm").onsubmit = sendChat;
document.querySelectorAll("#stratTabs button").forEach((b) =>
  b.addEventListener("click", () => setStrat(b.dataset.strat)));
wireChartUI(fetchChart);          // timeframe buttons + ⚙ indicator panel (chart.js)
poll(); setInterval(poll, POLL_MS);
refreshTokenStatus(); setInterval(refreshTokenStatus, 60000);   // token prefill + connection state
