"use strict";
// Shared TradingView Lightweight-Charts module used by both the live cockpit (app.js)
// and the training replay (train.js): candles + BB/EMA/Supertrend overlays, synced
// MACD/RSI sub-panes, CPR price-lines, trigger markers, and the ⚙ indicator panel
// (per-line colour / show-hide / width, persisted to localStorage). Each page owns its
// own data fetch + timeframe wiring and calls renderLW({bars, cpr}) + wireChartUI(onTF).

let LW = null, chartTF = "3min", _triggers = [];
const _el = (id) => document.getElementById(id);

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
  const rows = _el("indRows"); if (!rows) return;
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
  return LightweightCharts.createChart(_el(elId), {
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

// Wire the timeframe buttons + ⚙ panel. `onTF` is the page's data refetch for the new TF.
function wireChartUI(onTF) {
  document.querySelectorAll("#tfbar button").forEach((btn) => btn.onclick = () => {
    document.querySelectorAll("#tfbar button").forEach((b) => b.classList.remove("on"));
    btn.classList.add("on");
    chartTF = btn.dataset.tf;
    if (LW) LW.loadedTf = null;       // force a full reload for the new TF
    onTF();
  });
  const gear = _el("indCfgBtn");
  if (gear) gear.onclick = () => { const p = _el("indCfg"); p.hidden = !p.hidden; };
  const reset = _el("indReset");
  if (reset) reset.onclick = (e) => {
    e.preventDefault();
    try { localStorage.removeItem(IND_KEY); } catch (err) { /* ignore */ }
    IND = loadIndCfg(); buildIndPanel(); applyIndicatorConfig();
  };
  buildIndPanel();
}
