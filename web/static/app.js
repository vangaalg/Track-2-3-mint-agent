"use strict";
const SYMBOL = "NIFTY", SIZE = 75, POLL_MS = 15000, CHART_STRIKES = 8;
const $ = (id) => document.getElementById(id);
const n = (x, d = 2) => (x === null || x === undefined || Number.isNaN(x)) ? "—" : Number(x).toFixed(d);
const lakh = (x) => (x === null || x === undefined) ? "—" : (x / 1e5).toFixed(2);

let analysing = false, lastPayload = null, currentStrat = "trade1", currentHead = null;
const STRAT_LABEL = { trade1: "3-min", cpr_st: "CPR-ST", orb: "ORB", condor: "Expiry condor" };

async function poll() {
  try {
    const r = await fetch(`/api/snapshot?symbol=${SYMBOL}`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const d = await r.json();
    lastPayload = d;
    $("dot").className = "dot live";
    $("meta").textContent = `as of ${d.ts} · fetched ${d.fetched_at}`;
    renderChart(d); renderOI(d); renderStrategy();
    fetchChart(); fetchRecord();
    // Reveal the token form when the feed reports a Breeze/token/OI problem in its notes.
    flagTokenNeeded(/token|session|breeze|oi:/i.test((d.notes || []).join(" ")));
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

// POST today's Breeze token to the cockpit (applies it here + forwards to the recorder).
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
    $("tokenInput").value = "";
    poll();                    // pick up the freshly authenticated feed
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
  $("trigTitle").textContent = `📛 ${STRAT_LABEL[currentStrat]} — today's triggers`;

  if (!head) { renderWatching(); }
  else if (currentStrat === "condor") { renderCondor((d.proposals || {}).condor || {}); $("decision").hidden = false; }
  else { renderHead(head); }

  // Claude's read (auto-fired per trigger, server-side) on every tab.
  const rd = head && head.read;
  if (rd) renderRead(rd);
  else $("readBox").innerHTML = `<span class="muted">${head ? "Analysing this trigger… (or press Analyse)" : "No active trigger — watching."}</span>`;

  fetchTriggers(currentStrat);
}

function setStrat(strat) {
  currentStrat = strat;
  document.querySelectorAll("#stratTabs button").forEach((b) =>
    b.classList.toggle("on", b.dataset.strat === strat));
  renderStrategy();
}

function renderChart(d) {
  const c = d.chart, num = c.numbers || {}, lv = c.levels || {};
  $("spot").textContent = n(d.spot);
  $("mtf").textContent = (c.mtf_call || "—")
    + (c.mtf_confidence != null ? ` · 45EMA ${c.mtf_confidence}/5 ${mtfTicks(c.mtf_confidence_breakdown, c.mtf_call)}` : "");
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
  $("propBox").innerHTML = `🔔 TRIGGER · ${head.direction.toUpperCase()} `
    + `<span class="muted">${(head.ts || "").slice(11, 16)}</span>`
    + `<br>Entry ${n(head.entry)} · Stop ${n(head.stop)} · Target ${n(head.target)}`
    + `<br>R:R ${head.rr} · ${head.size_lots} lots <span class="muted">(conviction ${head.mtf_confidence}/5)</span>`
    + `<br><span class="small muted">pinned — won't advance until you approve/reject</span>`;
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

async function fetchTriggers(strat) {
  try {
    // no size param — the server sizes each row by its own conviction (matches the card)
    const d = await (await fetch(`/api/triggers?strategy=${strat || currentStrat}`)).json();
    renderTriggers(d, strat || currentStrat);
  } catch (e) { /* keep last */ }
}

function renderTriggers(d, strat) {
  _triggers = (strat === currentStrat || !strat) ? (d.triggers || []) : _triggers;
  const condor = strat === "condor";
  const s = d.summary || {}, last = d.last;
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
    $("trigLast").textContent = `No triggers yet today (${d.session || "—"}).`;
  }
  $("trigSummary").innerHTML = `${s.n || 0} triggers · ${s.wins || 0}W / ${s.losses || 0}L / ${s.open || 0} open`
    + ` · net <b class="${s.net_points >= 0 ? "win-txt" : "loss-txt"}">${s.net_points >= 0 ? "+" : ""}${s.net_points || 0} pts `
    + `(${s.net_rupees >= 0 ? "+" : ""}₹${s.net_rupees || 0})</b> if all taken`
    + (s.hit_rate != null ? ` · hit-rate ${(s.hit_rate * 100).toFixed(0)}%` : "");
  let h = condor
    ? "<thead><tr><th>Time</th><th>Short PE</th><th>Short CE</th><th>Credit</th><th>Out</th><th>Pts</th><th>₹</th></tr></thead><tbody>"
    : "<thead><tr><th>Time</th><th>Dir</th><th>Entry</th><th>Stop</th><th>Target</th><th>Out</th><th>Pts</th><th>₹</th></tr></thead><tbody>";
  for (const t of (d.triggers || [])) {
    const pts = `<td class="${t.points >= 0 ? "win" : "loss"}">${t.points >= 0 ? "+" : ""}${t.points}</td>`;
    const rs = `<td>${t.rupees >= 0 ? "+" : ""}${t.rupees}</td>`;
    h += condor
      ? `<tr><td>${t.ts.slice(11, 16)}</td><td>${t.short_put}</td><td>${t.short_call}</td>`
        + `<td>${t.credit}</td><td class="${t.outcome}">${t.outcome}</td>${pts}${rs}</tr>`
      : `<tr><td>${t.ts.slice(11, 16)}</td><td>${t.direction}</td><td>${t.entry}</td>`
        + `<td>${t.stop}</td><td>${t.target}</td><td class="${t.outcome}">${t.outcome}</td>${pts}${rs}</tr>`;
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
    const r = await fetch(`/api/analyse?strategy=${currentStrat}`, { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    renderRead(await r.json());
  } catch (e) { $("readBox").innerHTML = `<span class="muted">Claude unavailable: ${e.message}</span>`; }
  analysing = false; $("analyseBtn").textContent = "🤖 Analyse with Claude";
}

async function decide(action) {
  if (!currentHead) { $("decisionMsg").textContent = "No active trigger to decide."; return; }
  const fd = new FormData();
  fd.append("action", action); fd.append("strategy", currentStrat); fd.append("ts", currentHead.ts);
  const lbl = document.querySelector('input[name="liveLabel"]:checked');
  if (lbl) fd.append("label", lbl.value);
  const r = await fetch("/api/decision", { method: "POST", body: fd });
  const d = await r.json();
  if (!r.ok) { $("decisionMsg").textContent = "⚠ " + (d.detail || "decision failed"); return; }
  $("decisionMsg").textContent = `${action === "approve" ? "Approved" : "Rejected"} ${currentStrat} `
    + `${(currentHead.ts || "").slice(11, 16)} · logged ${d.logged} · ${d.status || "—"}`
    + (lbl ? ` · trigger ${lbl.value}` : "");
  document.querySelectorAll('input[name="liveLabel"]').forEach((el) => { el.checked = false; });
  poll();        // advance to the next pending trigger (if any)
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
$("tokenBtn").onclick = () => { $("tokenForm").hidden = !$("tokenForm").hidden; };
$("tokenSave").onclick = postToken;
$("tokenInput").addEventListener("keydown", (e) => { if (e.key === "Enter") postToken(); });
$("chatForm").onsubmit = sendChat;
document.querySelectorAll("#stratTabs button").forEach((b) =>
  b.addEventListener("click", () => setStrat(b.dataset.strat)));
wireChartUI(fetchChart);          // timeframe buttons + ⚙ indicator panel (chart.js)
poll(); setInterval(poll, POLL_MS);
