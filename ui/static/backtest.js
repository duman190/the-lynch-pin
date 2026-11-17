/* The Lynch Pin · Quant Portal — 5Y backtest of a portfolio X-ray (ui/backtest.py): $10,000 in the portfolio's weights
   five years ago, held, against VOO, QQQ, SCHD, SMH and IGV with dividends reinvested. One value a week as lines on a
   canvas (a second canvas carries the crosshair, so hovering never redraws the lines); the legend is a key rather than
   a filter, so every series is always drawn,
   then the risk / return table, one column per series. Opened by "5Y backtest" under View chart on the portfolio
   page. No libraries. Depends on app.js helpers ($, el, getJSON). */
"use strict";

(() => {
  const COLORS = { Portfolio: "#5D9CEC", VOO: "#F5B041", QQQ: "#A569BD", SCHD: "#48C9B0", SMH: "#EC407A", IGV: "#B0B0B0" };
  const NAMES = { VOO: "S&P 500", QQQ: "Nasdaq-100", SCHD: "Dividend", SMH: "Semis", IGV: "Software" };
  const C = { grid: "#212121", text: "#8E8E8E", cross: "rgba(255,255,255,.35)", bg: "#121212" };
  const ROWS = [  // [metric, label, format, title]
    ["value", "Value now", "usd", "What the $10,000 grew to"],
    ["total", "Total return", "pct", "Dividends reinvested"],
    ["cagr", "CAGR", "pct", "Compound annual growth rate"],
    ["vol", "Volatility", "pct", "Annualised SD of weekly returns"],
    ["sharpe", "Sharpe", "ratio", "Annual excess return over the T-bill rate / volatility"],
    ["sortino", "Sortino", "ratio", "Annual excess return / downside deviation"],
    ["max_dd", "Max drawdown", "pct", "Largest fall from a peak"],
    ["best_12m", "Best 12 months", "pct", "Best rolling 52-week return"],
    ["worst_12m", "Worst 12 months", "pct", "Worst rolling 52-week return"],
    ["corr", "Correlation", "ratio", "Of weekly returns, with the portfolio"],
    ["beta", "Beta", "ratio", "The portfolio's weekly moves against the index's"],
  ];
  const B = { token: 0, timer: null, data: null, geo: null, ro: null };
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const usd0 = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const usdK = (v) => (Math.abs(v) >= 1000 ? `$${+(v / 1000).toFixed(v >= 100000 ? 0 : 1)}k` : `$${Math.round(v)}`);
  const pct = (v, d = 1) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(d)}%` : "–");
  const day = (iso) => {
    const t = Date.parse(`${iso}T12:00:00`);
    return Number.isFinite(t) ? new Date(t).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : iso;
  };

  function fmt(row, v) {
    if (!isNum(v)) return "–";
    if (row[2] === "usd") return usd0.format(v);
    if (row[2] === "pct") return row[0] === "vol" ? `${(v * 100).toFixed(1)}%` : pct(v);
    return v.toFixed(2);
  }

  /* ── the table: metrics down, Portfolio then each index across ───────────── */
  function table(d) {
    const cols = d.series;
    return el("div", { class: "bt-table-wrap", tabindex: "0", "aria-label": "Risk and return, scroll sideways on a phone" },
      el("table", { class: "track-table bt-table" },
        el("caption", { class: "sr-only" }, "Risk and return over the backtest, portfolio and each index"),
        el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Metric"), cols.map((s) => el("th", { scope: "col", title: NAMES[s.sym] || null },
          el("span", { class: "bt-swatch", "data-c": s.sym }), s.sym)))),
        el("tbody", {}, ROWS.map((row) => el("tr", {},
          el("th", { scope: "row", title: row[3] }, row[1]),
          cols.map((s) => {
            const v = row[0] === "value" ? s.values[s.values.length - 1] : s.metrics[row[0]];
            const neg = isNum(v) && v < 0 && row[2] !== "ratio";
            return el("td", { class: `${s.sym === "Portfolio" ? "bt-pf" : ""}${neg ? " red" : ""}`.trim() || null }, fmt(row, v));
          }))))));
  }

  /* ── the chart ───────────────────────────────────────────────────────────── */
  function niceStep(span, n) {
    const raw = span / Math.max(1, n);
    const p = 10 ** Math.floor(Math.log10(raw));
    return [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw) || 10 * p;
  }

  function sizeCanvas(cv, w, h) {
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    cv.style.width = `${w}px`;
    cv.style.height = `${h}px`;
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  function draw() {
    const d = B.data;
    const box = $("#bt-chart");
    if (!d || !box || !box.isConnected) return;
    const w = Math.max(260, box.clientWidth);
    const h = Math.round(Math.max(220, Math.min(400, w * 0.5)));
    const ctx = sizeCanvas($("#bt-lines"), w, h);
    sizeCanvas($("#bt-cross"), w, h).clearRect(0, 0, w, h);
    const narrow = w < 480;
    const pad = { l: narrow ? 42 : 54, r: 10, t: 10, b: 24 };
    const all = d.series.flatMap((s) => s.values);
    let lo = Math.min(...all, d.start_value), hi = Math.max(...all, d.start_value);
    const step = niceStep(hi - lo, narrow ? 4 : 6);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    const n = d.weeks.length;
    const x = (i) => pad.l + (i / (n - 1)) * (w - pad.l - pad.r);
    const y = (v) => pad.t + (1 - (v - lo) / (hi - lo || 1)) * (h - pad.t - pad.b);
    ctx.clearRect(0, 0, w, h);
    ctx.font = `${narrow ? 10 : 11}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    ctx.fillStyle = C.text;
    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let v = lo; v <= hi + step / 2; v += step) {
      const yy = Math.round(y(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
      ctx.fillText(usdK(v), pad.l - 6, yy);
    }
    // the start line: $10,000
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "#444";
    const y0 = Math.round(y(d.start_value)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, y0); ctx.lineTo(w - pad.r, y0); ctx.stroke();
    ctx.restore();
    // year ticks at each year's first week
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    let year = d.weeks[0].slice(0, 4);
    d.weeks.forEach((wk, i) => {
      const yr = wk.slice(0, 4);
      if (yr === year) return;
      year = yr;
      const xx = Math.round(x(i)) + 0.5;
      ctx.strokeStyle = C.grid;
      ctx.beginPath(); ctx.moveTo(xx, pad.t); ctx.lineTo(xx, h - pad.b); ctx.stroke();
      ctx.fillText(narrow ? `’${yr.slice(2)}` : yr, xx, h - pad.b + 6);
    });
    // the lines, the portfolio on top
    const order = d.series.filter((s) => s.sym !== "Portfolio").concat(d.series.filter((s) => s.sym === "Portfolio"));
    ctx.lineJoin = "round";
    for (const s of order) {
      ctx.strokeStyle = COLORS[s.sym] || "#888";
      ctx.lineWidth = s.sym === "Portfolio" ? 2.6 : 1.4;
      ctx.globalAlpha = s.sym === "Portfolio" ? 1 : 0.85;
      ctx.beginPath();
      s.values.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    B.geo = { w, h, pad, x, y, n, order };
  }

  function hover(clientX) {
    const g = B.geo, d = B.data;
    if (!g || !d) return;
    const r = $("#bt-cross").getBoundingClientRect();
    const px = clientX - r.left;
    const i = Math.max(0, Math.min(g.n - 1, Math.round(((px - g.pad.l) / (g.w - g.pad.l - g.pad.r)) * (g.n - 1))));
    const ctx = $("#bt-cross").getContext("2d");
    ctx.clearRect(0, 0, g.w, g.h);
    const xx = Math.round(g.x(i)) + 0.5;
    ctx.strokeStyle = C.cross;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(xx, g.pad.t); ctx.lineTo(xx, g.h - g.pad.b); ctx.stroke();
    for (const s of g.order) {
      ctx.fillStyle = COLORS[s.sym] || "#888";
      ctx.beginPath(); ctx.arc(g.x(i), g.y(s.values[i]), s.sym === "Portfolio" ? 4 : 3, 0, Math.PI * 2); ctx.fill();
    }
    const tip = $("#bt-tip");
    const rows = g.order.slice().sort((a, b) => b.values[i] - a.values[i]);
    tip.replaceChildren(el("div", { class: "bt-tip-date" }, `Week of ${day(d.weeks[i])}`),
      ...rows.map((s) => el("div", { class: `bt-tip-row${s.sym === "Portfolio" ? " bt-pf" : ""}` },
        el("span", { class: "bt-swatch", "data-c": s.sym }), el("span", { class: "bt-tip-sym" }, s.sym),
        el("span", { class: "bt-tip-val" }, usd0.format(s.values[i])),
        el("span", { class: `bt-tip-pct ${s.values[i] >= d.start_value ? "green" : "red"}` }, pct(s.values[i] / d.start_value - 1, 0)))));
    tip.hidden = false;
    const tw = tip.offsetWidth;
    const left = xx + 14 + tw > g.w ? xx - 14 - tw : xx + 14;
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = `${g.pad.t}px`;
  }

  function unhover() {
    const g = B.geo;
    if (g) $("#bt-cross").getContext("2d").clearRect(0, 0, g.w, g.h);
    $("#bt-tip").hidden = true;
  }

  function legend(d) {
    const items = d.series.map((s) => el("span", { class: "bt-leg", role: "listitem", title: NAMES[s.sym] || "Your portfolio" },
      el("span", { class: "bt-swatch", "data-c": s.sym }), s.sym,
      el("span", { class: `bt-leg-pct ${s.metrics.total >= 0 ? "green" : "red"}` }, pct(s.metrics.total, 0))));
    return el("div", { class: "bt-legend", role: "list", "aria-label": "Series on the chart" }, items);
  }

  function render(d) {
    B.data = d;
    const pf = d.series[0];
    $("#bt-sub").textContent = `$10,000 on ${day(d.start)} → ${usd0.format(pf.values[pf.values.length - 1])} by the week of ${day(d.end)} · ${d.weeks.length} weeks · next point ${day(d.next_update)}`;
    const cv = el("canvas", { id: "bt-lines", "aria-hidden": "true" });
    const cross = el("canvas", { id: "bt-cross", class: "bt-cross", role: "img",
      "aria-label": `Line chart, one value a week from ${day(d.start)}: ${d.series.map((s) => `${s.sym} ${usd0.format(s.values[s.values.length - 1])}`).join(", ")}` });
    const chart = el("div", { class: "bt-chart", id: "bt-chart" }, cv, cross, el("div", { class: "bt-tip", id: "bt-tip", hidden: true }));
    cross.addEventListener("pointermove", (e) => hover(e.clientX));
    cross.addEventListener("pointerdown", (e) => hover(e.clientX));
    cross.addEventListener("pointerleave", unhover);
    $("#bt-body").replaceChildren(legend(d), chart, table(d),
      el("p", { class: "track-note bt-notes" }, `${d.notes.join(" · ")}. Past returns don't predict future ones. Not financial advice.`));
    if (B.ro) B.ro.disconnect();
    if ("ResizeObserver" in window) { B.ro = new ResizeObserver(() => { unhover(); draw(); }); B.ro.observe(chart); }
    draw();
  }

  async function open(key, label) {
    const dlg = $("#backtest");
    const token = ++B.token;
    clearTimeout(B.timer);
    B.data = null;
    $("#bt-title").textContent = label ? `5Y backtest · ${label}` : "5Y backtest";
    $("#bt-sub").textContent = "Fetching five years of weekly prices…";
    $("#bt-body").replaceChildren(el("p", { class: "muted track-empty" }, "Building the backtest against VOO, QQQ, SCHD, SMH and IGV…"));
    if (!dlg.open && typeof dlg.showModal === "function") dlg.showModal();
    const tick = async () => {
      if (token !== B.token || !dlg.open) return;
      let d;
      try { d = await getJSON(`/api/portfolio/backtest?k=${encodeURIComponent(key)}`); } catch (e) {
        if (token === B.token) $("#bt-sub").textContent = `Backtest unavailable: ${e.message}`;
        return;
      }
      if (token !== B.token) return;
      if (d.status === "running") { B.timer = setTimeout(tick, 800); return; }
      if (d.status !== "done") {
        $("#bt-sub").textContent = `Backtest unavailable: ${d.error || "no data"}`;
        $("#bt-body").replaceChildren(el("p", { class: "muted track-empty" }, "Try again in a minute."));
        return;
      }
      render(d);
    };
    tick();
  }

  function init() {
    const dlg = $("#backtest");
    if (!dlg) return;
    $("#bt-close").addEventListener("click", () => dlg.close());
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener("close", () => { B.token++; clearTimeout(B.timer); if (B.ro) B.ro.disconnect(); });
  }

  window.LynchBacktest = { open };
  document.addEventListener("DOMContentLoaded", init);
})();
