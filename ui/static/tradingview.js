/* The Lynch Pin · Quant Portal — trading view: one ticker's candles at 18 intervals with SMA 50/100/200, EMA 8/21,
   RSI 14 and MACD 12/26/9 (built by the server: ui/tradingview.py, ui/native/src/bars.rs), and the support /
   resistance levels of the ticker's analysis on top. Drawn on two canvases, the chart and the crosshair over it, so
   moving the crosshair never redraws the candles. No libraries. Depends on app.js ($, el). */
"use strict";

(() => {
  // [code (the server's), label, group]: TradingView's choices that Yahoo's bars can build (no seconds, no ranges)
  const INTERVALS = [
    ["1", "1m", "Minutes"], ["2", "2m", "Minutes"], ["3", "3m", "Minutes"], ["5", "5m", "Minutes"],
    ["10", "10m", "Minutes"], ["15", "15m", "Minutes"], ["30", "30m", "Minutes"], ["45", "45m", "Minutes"],
    ["60", "1H", "Hours"], ["120", "2H", "Hours"], ["180", "3H", "Hours"], ["240", "4H", "Hours"],
    ["1D", "1D", "Days"], ["1W", "1W", "Days"], ["1M", "1M", "Days"], ["3M", "3M", "Days"], ["6M", "6M", "Days"], ["12M", "12M", "Days"],
  ];
  const LABEL = Object.fromEntries(INTERVALS.map(([c, l]) => [c, l]));
  // Date ranges pick an interval and the window, as TradingView's do
  const RANGES = [
    ["1D", "1", { days: 1 }], ["5D", "5", { days: 5 }], ["1M", "30", { months: 1 }], ["3M", "60", { months: 3 }],
    ["6M", "120", { months: 6 }], ["YTD", "1D", "ytd"], ["1Y", "1D", { months: 12 }], ["5Y", "1W", { months: 60 }], ["All", "1M", "all"],
  ];
  const SERIES = [  // [key, label, colour] (the legend's swatches: .tv-c-KEY in tradingview.css)
    ["sma50", "SMA 50", "#F5B041"], ["sma100", "SMA 100", "#A569BD"], ["sma200", "SMA 200", "#5D9CEC"],
    ["ema8", "EMA 8", "#48C9B0"], ["ema21", "EMA 21", "#EC407A"],
  ];
  const C = {
    bg: "#121212", grid: "#212121", sep: "#333333", text: "#B0B0B0", dim: "#8E8E8E", white: "#FFFFFF",
    up: "#2ECC71", down: "#FF4B2B", upVol: "rgba(46,204,113,.28)", downVol: "rgba(255,75,43,.28)",
    rsi: "#B388FF", rsiBand: "rgba(179,136,255,.07)", macd: "#5D9CEC", signal: "#F39C12",
    histUp: "#26A69A", histUpWeak: "rgba(38,166,154,.45)", histDown: "#FF5252", histDownWeak: "rgba(255,82,82,.45)",
    res: "#FF4B2B", sup: "#2ECC71", poc: "#F5B041", range: "rgba(93,156,236,.08)", rangeEdge: "rgba(93,156,236,.55)", year: "#8E8E8E",
    bbU: "rgba(224,64,90,.9)", bbM: "rgba(220,220,220,.55)", bbL: "rgba(38,166,154,.95)", bbFill: "rgba(93,156,236,.07)",
    zone: "rgba(46,204,113,.10)", zoneEdge: "rgba(46,204,113,.55)", ext: "rgba(255,255,255,.035)",
    shelf: "rgba(160,170,190,.22)", shelfVA: "rgba(160,170,190,.36)", vpoc: "#FF9800",
  };
  // The BUY / SELL paint (bars.rs ``paint``: blue BUY, pink SELL bars, as on TradexWhisperer's charts): code → candle colour
  const PAINT = { g: "#2AC83E", d: "#1A6420", L: "#C0F000", r: "#FC2323", x: "#A80000", B: "#0151FC", P: "#F000D8" };
  const CALLOUT = { B: ["BUY", "#0E8EA6", "#FFFFFF", 1], P: ["SELL", "#C0708A", "#FFFFFF", -1], L: ["TRIM", "#9DBF00", "#10140A", -1] };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const PREFS = "lynchpin.tv";
  const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
  const MIN_POLL = 15, MAX_POLL = 45;  // s: polls also keep this viewer's place (the server frees it after 75 s idle)

  const S = {
    open: false, sym: null, code: "1D", info: {}, levels: null, data: null, etag: null, token: 0, timer: null,
    cache: new Map(),  // "SYM|code" → {data, etag}
    prevClose: new Map(),  // SYM → yesterday's close, for the header's change
    view: { right: 0, barW: 7 }, hover: null, pinned: false, range: null, pushed: false,
    show: { sma50: true, sma100: true, sma200: true, ema8: true, ema21: true, levels: true, bb: true, shelf: true, zone: true, paint: true },
    ext: true,  // intraday bars with pre- and post-market (the BUY / SELL paint was fitted on such charts)
    draw: { tool: null, items: [], sel: null, pending: null },  // drawings of the open ticker (time / price anchors)
    dpr: 1, L: null, anim: 0, dirty: false, overlayDirty: false,
    sid: (() => {
      const a = new Uint8Array(12);
      (window.crypto || {}).getRandomValues ? crypto.getRandomValues(a) : a.forEach((_, i) => { a[i] = Math.random() * 256; });
      return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
    })(),
  };

  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(PREFS) || "{}");
      if (p && LABEL[p.code]) S.code = p.code;
      if (p && p.show && typeof p.show === "object") for (const k of Object.keys(S.show)) if (typeof p.show[k] === "boolean") S.show[k] = p.show[k];
      if (p && typeof p.ext === "boolean") S.ext = p.ext;
    } catch (_) { /* private mode */ }
  }
  function savePrefs() {
    try { localStorage.setItem(PREFS, JSON.stringify({ code: S.code, show: S.show, ext: S.ext })); } catch (_) { /* private mode */ }
  }
  const isIntraday = (code) => /^\d+$/.test(code);  // minute codes: "1" … "240"
  const extOn = (code = S.code) => S.ext && isIntraday(code);

  /* ── formatting ─────────────────────────────────────────────────────────── */
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  function fmtPrice(v, dec) {
    if (!isNum(v)) return "—";
    return v.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }
  function fmtVol(v) {
    if (!isNum(v)) return "—";
    for (const [n, s] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) if (Math.abs(v) >= n) return (v / n).toFixed(Math.abs(v) >= 100 * n ? 0 : Math.abs(v) >= 10 * n ? 1 : 2) + s;
    return String(Math.round(v));
  }
  const pad2 = (n) => String(n).padStart(2, "0");

  /* ── data: the server's columns → typed arrays + each bar's local calendar (for the time axis) ──────────── */
  function tzOffsetFn(tz, fallback) {
    let fmt = null;
    try {
      fmt = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric",
        day: "numeric", hour: "numeric", minute: "numeric", second: "numeric" });
    } catch (_) { /* unknown zone: the exchange's current offset */ }
    const memo = new Map();
    return (t) => {
      if (!fmt) return fallback;
      const key = Math.floor(t / 86400);  // a UTC day; DST moves at night, when the exchange is shut
      let off = memo.get(key);
      if (off === undefined) {
        const p = {};
        for (const x of fmt.formatToParts(new Date(t * 1000))) p[x.type] = x.value;
        const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) / 1000;
        off = Math.round((asUTC - t) / 60) * 60;
        memo.set(key, off);
      }
      return off;
    };
  }

  function prepare(j) {
    const n = j.n | 0;
    const F = (a) => { const out = new Float64Array(n); for (let i = 0; i < n; i++) { const v = a[i]; out[i] = v === null || v === undefined ? NaN : v; } return out; };
    const t = new Float64Array(n);
    let acc = 0;
    for (let i = 0; i < n; i++) { acc += j.dt[i]; t[i] = acc; }
    const d = {
      sym: j.sym, code: j.interval, name: j.name, currency: j.currency, exchange: j.exchange, tz: j.tz, dec: j.decimals | 0,
      intraday: !!j.intraday, price: j.price, prevClose: j.prev_close, n, t,
      o: F(j.o), h: F(j.h), l: F(j.l), c: F(j.c), vol: F(j.vol),
      sma50: F(j.sma50), sma100: F(j.sma100), sma200: F(j.sma200), ema8: F(j.ema8), ema21: F(j.ema21),
      rsi: F(j.rsi), macd: F(j.macd), signal: F(j.signal), hist: new Float64Array(n),
      bbu: F(j.bbu || []), bbm: F(j.bbm || []), bbl: F(j.bbl || []),
      paint: typeof j.paint === "string" && j.paint.length === n ? j.paint : null,
      sess: typeof j.sess === "string" && j.sess.length === n ? j.sess : null, ext: !!j.sess,
      regular: j.regular || null, step: /^\d+$/.test(j.interval) ? +j.interval * 60 : null,  // s per bar (intraday)
      Y: new Int16Array(n), M: new Int8Array(n), D: new Int8Array(n), W: new Int8Array(n), mins: new Int16Array(n),
    };
    for (let i = 0; i < n; i++) d.hist[i] = d.macd[i] - d.signal[i];
    // local calendar of each bar: intraday in the exchange's zone (with its DST), longer bars by their date
    const off = d.intraday ? tzOffsetFn(j.tz, j.gmtoffset | 0) : () => (j.gmtoffset | 0) + 6 * 3600;
    for (let i = 0; i < n; i++) {
      const x = new Date((t[i] + off(t[i])) * 1000);
      d.Y[i] = x.getUTCFullYear(); d.M[i] = x.getUTCMonth(); d.D[i] = x.getUTCDate(); d.W[i] = x.getUTCDay();
      d.mins[i] = x.getUTCHours() * 60 + x.getUTCMinutes();
    }
    return d;
  }

  /* ── DOM ────────────────────────────────────────────────────────────────── */
  let dlg, canvas, overlay, ctx, octx, wrap, msgBox, legendBar, legendSeries, sheet, intBtn, drawBtn, extBtn, paintBtn, liveBtn, drawSheet;

  function svgIcon(path, size = 18) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", size); svg.setAttribute("height", size); svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", path);
    svg.append(p);
    return svg;
  }
  const CANDLE_ICON = "M6 3v3M6 15v6M4 6h4v9H4zM12 2v4M12 13v4M10 6h4v7h-4zM18 5v5M18 17v4M16 10h4v7h-4z";

  function build() {
    if (dlg) return;
    intBtn = el("button", { type: "button", class: "tv-int", "aria-haspopup": "dialog", title: "Interval and date range", onclick: () => toggleSheet() });
    legendBar = el("div", { class: "tv-ohlc", "aria-live": "off" });
    legendSeries = el("div", { class: "tv-series", role: "group", "aria-label": "Indicators shown" });
    canvas = el("canvas", { class: "tv-canvas", role: "img", "aria-label": "Candlestick chart" });
    overlay = el("canvas", { class: "tv-overlay", "aria-hidden": "true" });
    msgBox = el("div", { class: "tv-msg", role: "status", "aria-live": "polite", hidden: true });
    wrap = el("div", { class: "tv-chart", tabindex: "0", "aria-label": "Chart: drag to pan, wheel or pinch to zoom, arrows to move, double-click to reset" }, canvas, overlay, msgBox);
    sheet = el("div", { class: "tv-sheet", id: "tv-sheet", hidden: true, role: "dialog", "aria-label": "Interval and date range" });
    drawSheet = el("div", { class: "tv-sheet", hidden: true, role: "dialog", "aria-label": "Drawings" });
    aiPanel = el("div", { class: "tv-sheet tv-ai", hidden: true, role: "dialog", "aria-label": "Assess this setup with AI" });
    drawBtn = el("button", { type: "button", class: "tv-tool", "aria-haspopup": "dialog", title: "Drawings: trend line, Fibonacci, rectangle…",
      onclick: () => { if (S.draw.tool) setTool(null); else { buildDrawSheet(); drawSheet.hidden = !drawSheet.hidden; sheet.hidden = aiPanel.hidden = true; } } });
    paintBtn = el("button", { type: "button", class: "tv-tool tv-ext tv-paint", title: "BUY / SELL candles (blue / pink signal bars); off: plain green / red candles",
      onclick: () => { S.show.paint = !S.show.paint; savePrefs(); markActive(); requestDraw(); } }, "BUY/SELL");
    liveBtn = el("button", { type: "button", class: "tv-tool tv-live", title: "Live: the last candle updates every 2 s (a few people at a time: it spends Yahoo's quota)",
      onclick: () => toggleLive() }, el("i", { class: "tv-live-dot", "aria-hidden": "true" }), "LIVE");
    extBtn = el("button", { type: "button", class: "tv-tool tv-ext", title: "Extended hours: pre- and post-market bars (the BUY / SELL paint was fitted on them)",
      onclick: () => { S.ext = !S.ext; savePrefs(); markActive(); setInterval_(S.code, { force: true }); } }, "ETH");
    const aiBtn = el("button", { type: "button", class: "tv-tool tv-ai-open", title: "Assess this setup with Claude or ChatGPT: a snapshot of the chart and a prompt for this timeframe",
      onclick: () => { if (aiPanel.hidden) openAssess(); else aiPanel.hidden = true; } }, "🔬 Assess");
    dlg = el("dialog", { class: "tv", id: "tv", "aria-labelledby": "tv-title" },
      el("header", { class: "tv-head" },
        el("div", { class: "tv-id" },
          el("h2", { class: "tv-title", id: "tv-title" }, el("span", { class: "sky" }, "$"), el("span", { id: "tv-sym" })),
          el("span", { class: "tv-name muted", id: "tv-name" })),
        el("div", { class: "tv-quote", id: "tv-quote" }),
        el("button", { type: "button", class: "tv-close", "aria-label": "Close the trading view", onclick: () => close(true) }, "×")),
      el("div", { class: "tv-toolbar" }, intBtn, drawBtn, aiBtn, extBtn, paintBtn, liveBtn, el("span", { class: "tv-status muted", id: "tv-status" })),
      el("div", { class: "tv-legend" }, legendBar, legendSeries),
      wrap, sheet, drawSheet, aiPanel);
    document.body.append(dlg);
    ctx = canvas.getContext("2d");
    octx = overlay.getContext("2d");
    dlg.addEventListener("cancel", (e) => {  // Esc: the innermost thing first
      e.preventDefault();
      if (!sheet.hidden) toggleSheet(false);
      else if (!aiPanel.hidden) aiPanel.hidden = true;
      else if (!drawSheet.hidden) drawSheet.hidden = true;
      else if (S.draw.tool || S.draw.pending) setTool(null);
      else if (S.draw.sel) { S.draw.sel = null; requestDraw(); }
      else close(true);
    });
    buildSheet();
    renderSeriesToggles();
    bindPointer();
    if ("ResizeObserver" in window) new ResizeObserver(() => { resize(); }).observe(wrap);
    else window.addEventListener("resize", resize);
    wrap.addEventListener("keydown", onKey);
  }

  function buildSheet() {
    const groups = {};
    for (const [code, label, group] of INTERVALS) (groups[group] = groups[group] || []).push(el("button", { type: "button", "data-code": code, onclick: () => { toggleSheet(false); setInterval_(code); } }, label));
    put(sheet,
      el("div", { class: "tv-sheet-head" }, el("h3", { text: "Date range" }),
        el("button", { type: "button", class: "tv-sheet-x", "aria-label": "Close", onclick: () => toggleSheet(false) }, "×")),
      el("div", { class: "tv-grid" }, RANGES.map(([label, code, span]) => el("button", { type: "button", "data-range": label, onclick: () => { toggleSheet(false); applyRange(label, code, span); } }, label))),
      el("h3", { class: "tv-sheet-h", text: "Interval" }),
      Object.entries(groups).map(([g, btns]) => [el("p", { class: "tv-group", text: g }), el("div", { class: "tv-grid" }, btns)]),
      el("p", { class: "muted small tv-note", text: "Yahoo Finance bars, regular session. 3m, 10m, 30m, 45m, 2H-4H and 3M-12M are built from 1m, 5m, 15m, 1H and 1M bars." }));
  }
  function put(node, ...kids) { node.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false)); }

  function toggleSheet(show = sheet.hidden) {
    sheet.hidden = !show;
    intBtn.setAttribute("aria-expanded", String(show));
    if (show) { markActive(); const b = sheet.querySelector(`[data-code="${S.code}"]`); if (b) b.focus(); }
    else intBtn.focus({ preventScroll: true });
  }

  function markActive() {
    intBtn.replaceChildren(svgIcon(CANDLE_ICON, 16), el("span", { text: LABEL[S.code] }), el("span", { class: "tv-caret", "aria-hidden": "true" }, "▾"));
    extBtn.hidden = !isIntraday(S.code);
    extBtn.setAttribute("aria-pressed", String(S.ext));
    paintBtn.setAttribute("aria-pressed", String(S.show.paint));
    liveBtn.setAttribute("aria-pressed", String(!!S.live));
    renderDrawButton();
    for (const b of dlg.querySelectorAll("[data-code]")) b.setAttribute("aria-pressed", String(b.dataset.code === S.code));
    for (const b of dlg.querySelectorAll("[data-range]")) b.setAttribute("aria-pressed", String(b.dataset.range === S.range));
  }

  function renderSeriesToggles() {
    put(legendSeries,
      SERIES.map(([k, label, col]) => el("button", { type: "button", class: "tv-chip", "data-series": k, "aria-pressed": String(S.show[k]),
        title: `Show / hide ${label}`, onclick: () => { S.show[k] = !S.show[k]; savePrefs(); renderSeriesToggles(); requestDraw(); } },
        el("i", { class: `tv-dot tv-c-${k}` }), el("span", { class: "tv-chip-l", text: label }),
        el("span", { class: "tv-chip-s", text: `${label[0]}${label.split(" ")[1]}` }), el("b", { "data-val": k }))),
      el("button", { type: "button", class: "tv-chip tv-chip-lv", "aria-pressed": String(S.show.levels), title: "Show / hide the support / resistance levels from the analysis",
        onclick: () => { S.show.levels = !S.show.levels; savePrefs(); renderSeriesToggles(); requestDraw(); } },
        el("i", { class: "tv-lv-i" }), el("span", { class: "tv-chip-l", text: "S/R levels" }), el("span", { class: "tv-chip-s", text: "S/R" })),
      [["bb", "Bollinger 20,2", "BB", "tv-bb-i", "Bollinger Bands (20, 2)"], ["shelf", "Volume shelf", "VP", "tv-vp-i", "Volume by price over the bars on screen, with its point of control"],
        ["zone", "Accumulation zone", "Acc", "tv-zone-i", "The analysis' accumulation zone (daily SMA 200 ± 1 ATR)"]]
        .map(([k, label, short, icon, title]) => el("button", { type: "button", class: "tv-chip", "aria-pressed": String(S.show[k]), title,
          onclick: () => { S.show[k] = !S.show[k]; savePrefs(); renderSeriesToggles(); requestDraw(); } },
          el("i", { class: icon }), el("span", { class: "tv-chip-l", text: label }), el("span", { class: "tv-chip-s", text: short }))));
    updateLegend();
  }

  function buildDrawSheet() {  // TradingView's Drawings menu, the tools that read prices
    const tool = (k) => el("button", { type: "button", class: "tv-draw-tool", "aria-pressed": String(S.draw.tool === k),
      onclick: () => { drawSheet.hidden = true; setTool(k); } }, svgIcon(TOOLS[k][1], 26), el("span", { text: TOOLS[k][0] }));
    put(drawSheet,
      el("div", { class: "tv-sheet-head" }, el("h3", { text: "Drawings" }),
        el("button", { type: "button", class: "tv-sheet-x", "aria-label": "Close", onclick: () => { drawSheet.hidden = true; } }, "×")),
      el("p", { class: "tv-group", text: "Trend lines" }), el("div", { class: "tv-draw-grid" }, tool("trend"), tool("ray"), tool("hline")),
      el("p", { class: "tv-group", text: "Fibonacci" }), el("div", { class: "tv-draw-grid" }, tool("fib")),
      el("p", { class: "tv-group", text: "Shapes" }), el("div", { class: "tv-draw-grid" }, tool("rect")),
      el("div", { class: "tv-draw-actions" },
        el("button", { type: "button", class: "tv-ai-btn", disabled: !S.draw.sel, onclick: () => { deleteSelected(); buildDrawSheet(); } }, "Delete selected"),
        el("button", { type: "button", class: "tv-ai-btn", disabled: !S.draw.items.length,
          onclick: (e) => {
            if (e.currentTarget.dataset.armed) { S.draw.items = []; S.draw.sel = null; saveDrawings(); requestDraw(); drawSheet.hidden = true; return; }
            e.currentTarget.dataset.armed = "1"; e.currentTarget.textContent = `Tap again to delete all ${S.draw.items.length}`;
          } }, "Clear all")),
      el("p", { class: "muted small tv-note", text: "Drag to draw (or tap two points). Tap a drawing to select it, drag its handles to move them, Delete to remove it. Drawings are kept for this ticker on this device." }));
  }

  function renderDrawButton() {
    if (!drawBtn) return;
    const t = S.draw.tool;
    drawBtn.replaceChildren(svgIcon("M3 21l3.5-1 11-11-2.5-2.5-11 11zM14.5 5.5l2.5 2.5M17 3l4 4", 16),
      el("span", { class: "tv-tool-l", text: t ? TOOLS[t][0] : S.draw.items.length ? `Draw · ${S.draw.items.length}` : "Draw" }));
    drawBtn.setAttribute("aria-pressed", String(!!t));
  }

  /* ── open / close / URL ────────────────────────────────────────────────── */
  function open(sym, opts = {}) {
    sym = String(sym || "").toUpperCase();
    if (!TICKER_RE.test(sym)) return;
    build();
    const code = LABEL[opts.code] ? opts.code : S.code;
    const same = S.open && S.sym === sym;
    if (opts.levels !== undefined) S.levels = opts.levels;
    else if (!same) S.levels = null;
    if (opts.info) S.info = opts.info;
    else if (!same) S.info = {};
    S.zone = zoneOf(S.info);
    if (!same) loadDrawings(sym);
    S.sym = sym;
    $("#tv-sym").textContent = sym;
    $("#tv-name").textContent = S.info.name || "";
    if (!S.open) {
      S.open = true;
      document.body.classList.add("tv-open");
      if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
      document.addEventListener("visibilitychange", onVisibility);
      window.addEventListener("pagehide", release);
      S.clock = setInterval(tick, 1000);
    }
    if (opts.push !== false) {
      const url = `?t=${encodeURIComponent(sym)}&tv=${encodeURIComponent(code)}`;
      if (location.search !== url) { history.pushState({ t: sym, tv: code }, "", url); S.pushed = true; }
    }
    document.title = `$${sym} ${LABEL[code]} · Trading view · The Lynch Pin`;
    resize();
    S.range = null;
    setInterval_(code, { keepURL: true, force: !same });
    wrap.focus({ preventScroll: true });
  }

  function close(fromUI) {
    if (!S.open) return;
    if (fromUI && S.pushed && new URLSearchParams(location.search).get("tv")) { history.back(); return; }  // popstate closes it
    S.open = false;
    S.pushed = false;
    S.token += 1;
    clearTimeout(S.timer);
    stopLive();
    clearInterval(S.clock); S.clock = 0;
    cancelAnimationFrame(S.anim);
    release();
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("pagehide", release);
    sheet.hidden = true;
    if (dlg.open) dlg.close();
    document.body.classList.remove("tv-open");
    const q = new URLSearchParams(location.search);
    if (q.get("tv")) { q.delete("tv"); history.replaceState(history.state, "", `?${q}`); }
    if (S.sym) document.title = `$${S.sym} · The Lynch Pin`;
  }

  function release() {  // the page closed its chart: its place goes to the next viewer at once
    try { fetch(`/api/tv/release?s=${S.sid}`, { keepalive: true, cache: "no-store" }).catch(() => {}); } catch (_) { /* old browser */ }
  }

  function fromURL() {
    const q = new URLSearchParams(location.search);
    const t = (q.get("t") || "").toUpperCase(), tv = q.get("tv");
    if (tv && TICKER_RE.test(t)) {
      if (!S.open || S.sym !== t) open(t, { code: LABEL[tv] ? tv : undefined, push: false });
      else if (LABEL[tv] && tv !== S.code) setInterval_(tv, { keepURL: true });
    } else if (S.open) close(false);
  }

  function onVisibility() {
    if (!S.open) return;
    clearTimeout(S.timer);
    if (document.visibilityState === "visible") { load(true); if (S.live) liveTick(S.liveToken); }  // back: fresh bars now
  }

  /* ── loading ────────────────────────────────────────────────────────────── */
  function setInterval_(code, { keepURL = false, force = false, after = null } = {}) {
    if (!LABEL[code]) return;
    const changed = code !== S.code || force;
    S.code = code;
    if (!after) S.range = null;
    savePrefs();
    markActive();
    if (!keepURL && S.open) {
      const url = `?t=${encodeURIComponent(S.sym)}&tv=${encodeURIComponent(code)}`;
      if (location.search !== url) history.replaceState({ t: S.sym, tv: code }, "", url);
    }
    document.title = `$${S.sym} ${LABEL[code]} · Trading view · The Lynch Pin`;
    if (!changed && S.data && S.data.code === code && S.data.sym === S.sym && S.data.ext === extOn(code) && !after) return;
    const hit = S.cache.get(cacheKey(S.sym, code));
    if (hit) { show(hit.data, hit.etag, { reset: true, after }); load(true, { after: null }); }
    else { S.data = null; S.etag = null; requestDraw(); message(`Loading ${LABEL[code]}…`, "loading"); load(false, { after }); }
  }

  async function load(revalidate, { after = null } = {}) {
    const token = ++S.token;
    clearTimeout(S.timer);
    const sym = S.sym, code = S.code, ext = extOn(code);
    let res;
    try {
      // no-cache: the browser revalidates its copy with the ETag, and the server answers 304 while unchanged
      res = await fetch(`/api/tv/${encodeURIComponent(sym)}?i=${encodeURIComponent(code)}${ext ? "&x=1" : ""}&s=${S.sid}`, { cache: "no-cache", headers: { Accept: "application/json" } });
    } catch (e) {
      if (token !== S.token) return;
      if (!revalidate || !S.data) message("Connection problem — retrying…", "error");
      return schedule(token, 5);
    }
    if (token !== S.token) return;
    const refreshIn = parseInt(res.headers.get("X-TV-Refresh") || "", 10);
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (_) { /* not JSON */ }
      if (token !== S.token) return;
      const wait = parseInt(res.headers.get("Retry-After") || "", 10) || (body && body.retry_after) || 10;
      const msg = (body && body.error) || `HTTP ${res.status}`;
      if (res.status === 404) { message(`No chart for $${sym}: ${msg}`, "error"); return; }
      if (res.status === 400) { message(msg, "error"); return; }
      countdown(token, wait, `${msg.charAt(0).toUpperCase()}${msg.slice(1)}.`, () => load(revalidate, { after }));
      return;
    }
    const etag = res.headers.get("ETag");
    status(res.headers.get("X-TV-Stale") === "1" ? "stale: Yahoo isn't answering" : res.headers.get("X-TV-Live") === "1" ? "live · delayed" : "market closed");
    if (etag && etag === S.etag && S.data && S.data.sym === sym && S.data.code === code && S.data.ext === ext) {
      res.body && res.body.cancel && res.body.cancel().catch(() => {});  // unchanged (a 304 the browser answered from its copy)
    } else {
      let j;
      try { j = await res.json(); } catch (_) { if (token === S.token) message("Unreadable chart data — retrying…", "error"); return schedule(token, 10); }
      if (token !== S.token) return;
      const d = prepare(j);
      S.cache.set(cacheKey(sym, code), { data: d, etag });
      if (S.cache.size > 24) S.cache.delete(S.cache.keys().next().value);
      show(d, etag, { reset: !revalidate || !S.data || S.data.code !== code || S.data.sym !== sym || S.data.ext !== ext, after });
    }
    schedule(token, isNum(refreshIn) ? refreshIn : MAX_POLL);
  }

  const cacheKey = (sym, code) => `${sym}|${code}|${extOn(code) ? "x" : ""}`;

  function schedule(token, secs) {
    clearTimeout(S.timer);
    if (token !== S.token || !S.open || document.visibilityState === "hidden") return;
    S.timer = setTimeout(() => { if (token === S.token) load(true); }, Math.min(MAX_POLL, Math.max(MIN_POLL, secs)) * 1000);
  }

  function countdown(token, secs, msg, then) {
    if (token !== S.token || !S.open) return;
    if (secs <= 0) { then(); return; }
    message(`${msg} Retrying in ${secs}s…`, "wait");
    S.timer = setTimeout(() => countdown(token, secs - 1, msg, then), 1000);
  }

  function message(text, kind) {
    if (!msgBox) return;
    msgBox.hidden = !text;
    msgBox.textContent = text || "";
    msgBox.className = `tv-msg ${kind || ""}`;
  }
  function status(text) { const s = $("#tv-status"); if (s && s.textContent !== text) s.textContent = text; }

  function show(d, etag, { reset, after }) {
    const prev = S.data;
    const atEnd = prev && S.view.right >= prev.n - 1.5;
    S.data = d;
    S.etag = etag;
    message("");
    if (reset || !prev) resetView();
    else if (atEnd) S.view.right += (d.n - 1) - (prev.n - 1);  // following the last bar: keep following it
    else {  // keep the same bars in view although bars came and went
      const t = prev.t[Math.max(0, Math.min(prev.n - 1, Math.round(S.view.right)))];
      S.view.right += lowerBound(d.t, t) - Math.round(S.view.right);
    }
    if (after) after();
    clampView();
    updateQuote();
    canvas.setAttribute("aria-label", `Candlestick chart of ${d.sym}, ${LABEL[d.code]} bars, last close ${fmtPrice(d.c[d.n - 1], d.dec)}`);
    requestDraw();
  }

  function lowerBound(arr, x) {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < x) lo = m + 1; else hi = m; }
    return lo;
  }

  function updateQuote() {
    const d = S.data;
    if (!d) return;
    if (d.name && !S.info.name) $("#tv-name").textContent = d.name;
    const last = d.c[d.n - 1];
    const px = isNum(d.price) ? d.price : last;
    let prev = isNum(d.prevClose) ? d.prevClose : null;
    if (prev === null && d.code === "1D" && d.n > 1) prev = d.c[d.n - 2];
    if (prev !== null) S.prevClose.set(d.sym, prev);  // weekly / monthly bars don't say: the last daily or intraday did
    else prev = S.prevClose.get(d.sym) || null;
    const kids = [el("span", { class: "tv-px", text: fmtPrice(px, d.dec) })];
    if (prev) {
      const ch = px - prev, pct = (ch / prev) * 100;
      kids.push(el("span", { class: ch >= 0 ? "green" : "red", text: ` ${ch >= 0 ? "+" : "−"}${fmtPrice(Math.abs(ch), d.dec)} (${ch >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%)` }));
    }
    if (d.currency) kids.push(el("span", { class: "muted small", text: ` ${d.currency}` }));
    put($("#tv-quote"), kids);
  }

  /* ── view: which bars are on screen ─────────────────────────────────────── */
  function defaultBarW() {
    const w = S.L ? S.L.plotW / S.dpr : 600;
    return Math.max(5, Math.min(9, w / 110));  // CSS px per bar: ~60 bars on a phone, ~130 on a laptop
  }
  function resetView() {
    if (!S.data) return;
    S.view.barW = defaultBarW() * S.dpr;
    S.view.right = S.data.n - 1 + 3;  // a few empty slots right of the last bar, as TradingView leaves
    S.hover = null;
    S.pinned = false;
  }
  function clampView() {
    const d = S.data;
    if (!d || !S.L) return;
    const slots = S.L.plotW / S.view.barW;
    S.view.barW = Math.max(0.5 * S.dpr, Math.min(48 * S.dpr, S.view.barW));
    S.view.right = Math.max(Math.min(d.n - 1, slots * 0.2), Math.min(d.n - 1 + slots * 0.6, S.view.right));
  }
  function applyRange(label, code, span) {
    const fit = () => {
      const d = S.data;
      if (!d || !d.n) return;
      let i0 = 0;
      const last = d.n - 1;
      if (span === "ytd") { const y = d.Y[last]; while (i0 < last && d.Y[i0] !== y) i0++; }
      else if (span && span.days) {
        let days = 1;
        i0 = last;
        while (i0 > 0) {
          const newDay = d.D[i0 - 1] !== d.D[i0] || d.M[i0 - 1] !== d.M[i0] || d.Y[i0 - 1] !== d.Y[i0];
          if (newDay && ++days > span.days) break;
          i0--;
        }
      } else if (span && span.months) {
        let y = d.Y[last], m = d.M[last] - span.months;
        while (m < 0) { m += 12; y -= 1; }
        const key = (yy, mm, dd) => (yy * 12 + mm) * 32 + dd;
        const target = key(y, m, d.D[last]);
        while (i0 < last && key(d.Y[i0], d.M[i0], d.D[i0]) < target) i0++;
      }
      const bars = last - i0 + 1;
      const slots = bars + Math.max(2, bars * 0.04);
      S.view.barW = S.L.plotW / slots;
      S.view.right = last + Math.max(2, bars * 0.04) - 0.5;
      clampView();
    };
    S.range = label;
    if (code === S.code && S.data && S.data.code === code && S.data.sym === S.sym) { fit(); markActive(); requestDraw(); return; }
    setInterval_(code, { after: fit });
    S.range = label;
    markActive();
  }

  /* ── layout & drawing ───────────────────────────────────────────────────── */
  function resize() {
    if (!wrap || !S.open) return;
    const r = wrap.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) return;  // not laid out yet (a just-opened dialog): no layout to keep
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const W = Math.max(1, Math.round(r.width * dpr)), H = Math.max(1, Math.round(r.height * dpr));
    const keepSlots = S.L && S.L.plotW > 0 && S.view.barW > 0 ? S.L.plotW / S.view.barW : null;
    if (canvas.width !== W || canvas.height !== H) {
      canvas.width = overlay.width = W;
      canvas.height = overlay.height = H;
    }
    S.dpr = dpr;
    const font = Math.round(11 * dpr);
    const axisW = Math.round(68 * dpr), timeH = Math.round(22 * dpr);
    const avail = H - timeH;
    const priceH = Math.round(avail * 0.62), rsiH = Math.round(avail * 0.16);
    S.L = { W, H, plotW: W - axisW, axisW, timeH, font, avail,
      panes: { price: { y: 0, h: priceH }, rsi: { y: priceH, h: rsiH }, macd: { y: priceH + rsiH, h: avail - priceH - rsiH } } };
    if (keepSlots && S.data) S.view.barW = S.L.plotW / keepSlots;  // a rotated phone keeps the same bars
    clampView();
    requestDraw();
  }

  function requestDraw(overlayOnly = false) {
    if (overlayOnly) S.overlayDirty = true; else S.dirty = S.overlayDirty = true;
    if (!S.anim) S.anim = requestAnimationFrame(frame);
  }
  function frame() {
    S.anim = 0;
    if (!S.open || !S.L) return;
    if (S.dirty) { S.dirty = false; draw(); }
    if (S.overlayDirty) { S.overlayDirty = false; drawOverlay(); updateLegend(); }
  }

  const xOf = (i) => S.L.plotW + (i - S.view.right - 0.5) * S.view.barW;   // bar centre, device px
  const iAt = (x) => Math.round(S.view.right + 0.5 + (x - S.L.plotW) / S.view.barW - 0.5);
  function visible() {
    const d = S.data;
    const i0 = Math.max(0, Math.floor(S.view.right - S.L.plotW / S.view.barW) - 1);
    const i1 = Math.min(d.n - 1, Math.ceil(S.view.right) + 1);
    return [i0, i1];
  }

  function niceStep(span, target) {
    const raw = span / Math.max(1, target);
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const m = raw / p;
    return (m < 1.5 ? 1 : m < 3 ? 2 : m < 7 ? 5 : 10) * p;
  }
  function decimalsFor(step, dec) { return Math.max(0, Math.min(dec + 2, Math.ceil(-Math.log10(step) + 0.001))); }

  function priceScale(i0, i1) {
    const d = S.data;
    let lo = Infinity, hi = -Infinity;
    for (let i = i0; i <= i1; i++) { if (d.l[i] < lo) lo = d.l[i]; if (d.h[i] > hi) hi = d.h[i]; }
    for (const k of SERIES.map(([k]) => k).concat(["bbu", "bbl"])) {
      if (!(k.startsWith("bb") ? S.show.bb : S.show[k])) continue;
      const a = d[k];
      for (let i = i0; i <= i1; i++) { const v = a[i]; if (v === v) { if (v < lo) lo = v; if (v > hi) hi = v; } }
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (hi - lo < Math.abs(hi) * 1e-4) { hi += Math.abs(hi) * 0.01 + 0.01; lo -= Math.abs(lo) * 0.01 + 0.01; }
    const pad = (hi - lo) * 0.08;
    return { lo: lo - pad, hi: hi + pad };
  }

  function draw() {
    const L = S.L, d = S.data, c = ctx;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.fillStyle = C.bg;
    c.fillRect(0, 0, L.W, L.H);
    if (!d || !d.n) return;
    const [i0, i1] = visible();
    const P = L.panes;
    const px = priceScale(i0, i1);
    const yP = (v) => P.price.y + (px.hi - v) / (px.hi - px.lo) * P.price.h;
    S.scale = { px, yP };
    const lw = Math.max(1, Math.round(S.dpr));
    c.font = `${L.font}px ui-monospace, "DejaVu Sans Mono", Menlo, monospace`;
    c.textBaseline = "middle";
    if (d.sess) drawExtHours(c, i0, i1);

    // time grid + axis labels (computed once, drawn across every pane)
    const ticks = timeTicks(i0, i1);
    c.fillStyle = C.grid;
    for (const tk of ticks) c.fillRect(Math.round(xOf(tk.i)), 0, lw, L.avail);
    // price grid
    const pStep = niceStep(px.hi - px.lo, P.price.h / (44 * S.dpr));
    const pDec = decimalsFor(pStep, d.dec);
    for (let v = Math.ceil(px.lo / pStep) * pStep; v <= px.hi; v += pStep) c.fillRect(0, Math.round(yP(v)), L.plotW, lw);

    c.save();
    c.beginPath(); c.rect(0, P.price.y, L.plotW, P.price.h); c.clip();
    if (S.show.zone) drawZone(c, yP, lw);
    if (S.show.bb) drawBands(c, i0, i1, yP);
    const shelf = S.show.shelf ? shelfData(i0, i1, px) : null;
    S.shelf = shelf;
    if (shelf) drawShelf(c, shelf, yP, lw);
    // volume, bottom fifth of the price pane
    let vmax = 0;
    for (let i = i0; i <= i1; i++) if (d.vol[i] > vmax) vmax = d.vol[i];
    if (vmax > 0) {
      const vh = P.price.h * 0.2, base = P.price.y + P.price.h;
      const up = new Path2D(), dn = new Path2D();
      const bw = Math.max(1, S.view.barW * 0.7);
      for (let i = i0; i <= i1; i++) {
        const h = d.vol[i] / vmax * vh;
        if (h <= 0) continue;
        (d.c[i] >= d.o[i] ? up : dn).rect(xOf(i) - bw / 2, base - h, bw, h);
      }
      c.fillStyle = C.upVol; c.fill(up);
      c.fillStyle = C.downVol; c.fill(dn);
    }
    if (S.show.levels) drawLevels(c, yP, lw);
    // moving averages
    c.lineWidth = Math.max(1, 1.3 * S.dpr);
    c.lineJoin = "round";
    for (const [k, , col] of SERIES) if (S.show[k]) line(c, d[k], i0, i1, yP, col);
    // candles: wicks and bodies batched by colour (two fills per colour, however many bars); with the paint on,
    // each bar takes its BUY / SELL paint colour instead of up / down
    const paint = S.show.paint && d.paint;
    const paths = new Map();
    const pathFor = (col) => { let p = paths.get(col); if (!p) paths.set(col, p = new Path2D()); return p; };
    const bw = S.view.barW * 0.72;
    const thin = bw < 1.6 * S.dpr;  // too narrow for a body: wicks only
    for (let i = i0; i <= i1; i++) {
      const x = xOf(i);
      const col = paint ? PAINT[d.paint[i]] || C.up : d.c[i] >= d.o[i] ? C.up : C.down;
      const path = pathFor(col);
      const yh = yP(d.h[i]), yl = yP(d.l[i]);
      path.rect(Math.round(x - lw / 2), yh, lw, Math.max(lw, yl - yh));
      if (!thin) {
        const yo = yP(d.o[i]), yc = yP(d.c[i]);
        const top = Math.min(yo, yc), h = Math.max(lw, Math.abs(yc - yo));
        path.rect(Math.round(x - bw / 2), top, Math.round(bw), h);
      }
    }
    for (const [col, path] of paths) { c.fillStyle = col; c.fill(path); }
    drawDrawings(c, yP, lw);
    if (paint) drawCallouts(c, i0, i1, yP);
    c.restore();

    // RSI pane
    const R = P.rsi;
    const yR = (v) => R.y + (1 - v / 100) * (R.h - 8 * S.dpr) + 4 * S.dpr;
    c.save();
    c.beginPath(); c.rect(0, R.y, L.plotW, R.h); c.clip();
    c.fillStyle = C.rsiBand;
    c.fillRect(0, yR(70), L.plotW, yR(30) - yR(70));
    c.strokeStyle = "rgba(179,136,255,.35)"; c.lineWidth = lw; c.setLineDash([4 * S.dpr, 4 * S.dpr]);
    for (const v of [70, 30]) { c.beginPath(); c.moveTo(0, Math.round(yR(v)) + 0.5); c.lineTo(L.plotW, Math.round(yR(v)) + 0.5); c.stroke(); }
    c.strokeStyle = "rgba(179,136,255,.18)";
    c.beginPath(); c.moveTo(0, Math.round(yR(50)) + 0.5); c.lineTo(L.plotW, Math.round(yR(50)) + 0.5); c.stroke();
    c.setLineDash([]);
    c.lineWidth = Math.max(1, 1.3 * S.dpr);
    line(c, d.rsi, i0, i1, yR, C.rsi);
    c.restore();

    // MACD pane
    const Mp = P.macd;
    let mlo = 0, mhi = 0;
    for (let i = i0; i <= i1; i++) for (const a of [d.macd, d.signal, d.hist]) { const v = a[i]; if (v === v) { if (v < mlo) mlo = v; if (v > mhi) mhi = v; } }
    if (mhi - mlo === 0) { mhi = 1; mlo = -1; }
    const mpad = (mhi - mlo) * 0.1;
    mhi += mpad; mlo -= mpad;
    const yM = (v) => Mp.y + (mhi - v) / (mhi - mlo) * Mp.h;
    S.scale.yR = yR; S.scale.yM = yM; S.scale.m = { lo: mlo, hi: mhi };
    c.save();
    c.beginPath(); c.rect(0, Mp.y, L.plotW, Mp.h); c.clip();
    const zero = yM(0);
    const hp = [new Path2D(), new Path2D(), new Path2D(), new Path2D()];  // up, up weakening, down, down weakening
    const hbw = Math.max(1, S.view.barW * 0.6);
    for (let i = i0; i <= i1; i++) {
      const v = d.hist[i];
      if (v !== v) continue;
      const prev = i > 0 ? d.hist[i - 1] : NaN;
      const k = v >= 0 ? (prev === prev && v < prev ? 1 : 0) : (prev === prev && v > prev ? 3 : 2);
      const y = yM(v);
      hp[k].rect(xOf(i) - hbw / 2, Math.min(y, zero), hbw, Math.max(lw, Math.abs(zero - y)));
    }
    [C.histUp, C.histUpWeak, C.histDown, C.histDownWeak].forEach((col, k) => { c.fillStyle = col; c.fill(hp[k]); });
    c.fillStyle = C.sep; c.fillRect(0, Math.round(zero), L.plotW, lw);
    c.lineWidth = Math.max(1, 1.3 * S.dpr);
    line(c, d.macd, i0, i1, yM, C.macd);
    line(c, d.signal, i0, i1, yM, C.signal);
    c.restore();

    // pane separators, axes
    c.fillStyle = C.sep;
    c.fillRect(0, R.y, L.W, lw);
    c.fillRect(0, Mp.y, L.W, lw);
    c.fillRect(0, L.avail, L.W, lw);
    c.fillRect(L.plotW, 0, lw, L.H);
    c.fillStyle = C.text;
    c.textAlign = "left";
    const ax = L.plotW + 6 * S.dpr;
    for (let v = Math.ceil(px.lo / pStep) * pStep; v <= px.hi; v += pStep) {
      const y = yP(v);
      if (y > P.price.y + L.font && y < P.price.y + P.price.h - L.font * 0.6) c.fillText(fmtPrice(v, pDec), ax, y);
    }
    for (const v of [70, 30]) c.fillText(String(v), ax, yR(v));
    const mStep = niceStep(mhi - mlo, Mp.h / (34 * S.dpr));
    const mDec = decimalsFor(mStep, 0);
    for (let v = Math.ceil(mlo / mStep) * mStep; v <= mhi; v += mStep) {
      const y = yM(v);
      if (y > Mp.y + L.font && y < Mp.y + Mp.h - L.font * 0.6) c.fillText(v.toFixed(mDec), ax, y);
    }
    // time axis labels
    c.textAlign = "center";
    for (const tk of ticks) {
      c.fillStyle = tk.strong ? C.white : C.text;
      c.font = `${tk.strong ? "700 " : ""}${L.font}px ui-monospace, "DejaVu Sans Mono", Menlo, monospace`;
      c.fillText(tk.label, xOf(tk.i), L.avail + L.timeH / 2);
    }
    c.font = `${L.font}px ui-monospace, "DejaVu Sans Mono", Menlo, monospace`;
    // the price axis' tags: the last price where it is, the levels stacked away from it so none overlap
    const last = d.n - 1, lc = d.c[last];
    const ly = yP(lc);
    const tags = (S.show.levels ? levelTags(yP) : []).concat(extraTags(yP));
    let fixed = null;
    if (ly > P.price.y && ly < P.price.y + P.price.h) {
      const col = lc >= d.o[last] ? C.up : C.down;
      c.strokeStyle = col; c.lineWidth = lw; c.setLineDash([2 * S.dpr, 3 * S.dpr]);
      c.beginPath(); c.moveTo(0, Math.round(ly) + 0.5); c.lineTo(L.plotW, Math.round(ly) + 0.5); c.stroke();
      c.setLineDash([]);
      fixed = { y: ly, text: fmtPrice(lc, d.dec), bg: col, fg: "#0B1422" };
    }
    for (const t of stackTags(tags, fixed, L.font + 6 * S.dpr)) tag(c, t.y, t.text, t.bg, t.fg);
    if (fixed) {
      tag(c, fixed.y, fixed.text, fixed.bg, fixed.fg);
      const left = barLeft();  // TradingView's countdown to the next candle, under the last price
      if (left !== null) tag(c, fixed.y + L.font + 6 * S.dpr, fmtLeft(left), "#2A2A2A", fixed.bg);
    }
    // pane titles
    c.textAlign = "left";
    c.fillStyle = C.dim;
    c.fillText("RSI 14", 6 * S.dpr, R.y + L.font);
    c.fillText("MACD 12 26 9", 6 * S.dpr, Mp.y + L.font);
  }

  function line(c, a, i0, i1, y, col) {
    c.strokeStyle = col;
    c.beginPath();
    let pen = false;
    for (let i = i0; i <= i1; i++) {
      const v = a[i];
      if (v !== v) { pen = false; continue; }
      const x = xOf(i), yy = y(v);
      if (pen) c.lineTo(x, yy); else { c.moveTo(x, yy); pen = true; }
    }
    c.stroke();
  }

  function tag(c, y, text, bg, fg) {
    const L = S.L, h = L.font + 6 * S.dpr;
    c.fillStyle = bg;
    c.fillRect(L.plotW + S.dpr, Math.round(y - h / 2), L.axisW - S.dpr, h);
    c.fillStyle = fg;
    c.textAlign = "left";
    c.fillText(text, L.plotW + 6 * S.dpr, y);
  }

  /* ── support / resistance from the analysis (ui/levels.py, the trade assistant's toolkit) ─────────────── */
  function levelList() {
    const lv = S.levels;
    if (!lv) return [];
    const out = [];
    (lv.resistance || []).forEach((x, i) => isNum(x.price) && out.push({ v: x.price, label: `R${i + 1}`, col: C.res, p: x.p_touch_1m }));
    (lv.support || []).forEach((x, i) => isNum(x.price) && out.push({ v: x.price, label: `S${i + 1}`, col: C.sup, p: x.p_touch_1m }));
    if (isNum(lv.poc)) out.push({ v: lv.poc, label: "POC", col: C.poc, dash: [6, 4] });
    if (isNum(lv.high_52w)) out.push({ v: lv.high_52w, label: "52W H", col: C.year, dash: [2, 4] });
    if (isNum(lv.low_52w)) out.push({ v: lv.low_52w, label: "52W L", col: C.year, dash: [2, 4] });
    return out;
  }
  function drawLevels(c, yP, lw) {
    const L = S.L, P = L.panes.price, lv = S.levels;
    if (!lv) return;
    const m = lv.ranges && lv.ranges["1m"];
    if (m && isNum(m["1sigma_lower"]) && isNum(m["1sigma_upper"])) {  // expected 1-month range (±1σ), shaded
      const a = yP(m["1sigma_upper"]), b = yP(m["1sigma_lower"]);
      c.fillStyle = C.range; c.fillRect(0, a, L.plotW, b - a);
      c.strokeStyle = C.rangeEdge; c.lineWidth = lw; c.setLineDash([3 * S.dpr, 5 * S.dpr]);
      for (const y of [a, b]) { c.beginPath(); c.moveTo(0, Math.round(y) + 0.5); c.lineTo(L.plotW, Math.round(y) + 0.5); c.stroke(); }
      c.setLineDash([]);
      c.fillStyle = C.rangeEdge; c.textAlign = "left";
      c.fillText("1M ±1σ", 6 * S.dpr, a + L.font * 0.8);
    }
    c.textAlign = "right";
    for (const x of levelList()) {
      const y = yP(x.v);
      if (y < P.y - 2 || y > P.y + P.h + 2) continue;
      c.strokeStyle = x.col; c.lineWidth = Math.max(1, 1.5 * S.dpr); c.globalAlpha = 0.85;
      c.setLineDash(x.dash ? x.dash.map((v) => v * S.dpr) : []);
      c.beginPath(); c.moveTo(0, Math.round(y) + 0.5); c.lineTo(L.plotW, Math.round(y) + 0.5); c.stroke();
      c.setLineDash([]); c.globalAlpha = 1;
      const text = isNum(x.p) ? `${x.label} · ${x.p.toFixed(0)}%` : x.label;  // % = P(touch within a month)
      const tw = c.measureText(text).width, th = L.font + 2 * S.dpr, ty = y - L.font * 0.75;
      c.fillStyle = "rgba(18,18,18,.78)";
      c.fillRect(L.plotW - tw - 10 * S.dpr, ty - th / 2, tw + 8 * S.dpr, th);
      c.fillStyle = x.col;
      c.fillText(text, L.plotW - 6 * S.dpr, ty);
    }
  }
  function levelTags(yP) {
    const P = S.L.panes.price, out = [];
    for (const x of levelList()) {
      const y = yP(x.v);
      if (y < P.y + 6 || y > P.y + P.h - 6) continue;
      out.push({ y, text: fmtPrice(x.v, S.data.dec), bg: x.col, fg: x.col === C.year || x.col === C.poc ? "#0B1422" : "#FFFFFF" });
    }
    return out;
  }
  /* Tags at least ``h`` apart: those above the fixed one (the last price) pushed up, those below pushed down. */
  function stackTags(tags, fixed, h) {
    const anchor = fixed ? fixed.y : -Infinity;
    const above = tags.filter((t) => t.y < anchor).sort((a, b) => b.y - a.y);
    const below = tags.filter((t) => t.y >= anchor).sort((a, b) => a.y - b.y);
    let prev = fixed ? fixed.y : null;
    for (const t of above) { if (prev !== null && t.y > prev - h) t.y = prev - h; prev = t.y; }
    prev = fixed ? fixed.y : null;
    for (const t of below) { if (prev !== null && t.y < prev + h) t.y = prev + h; prev = t.y; }
    const P = S.L.panes.price;
    return above.concat(below).filter((t) => t.y > P.y + h / 2 && t.y < P.y + P.h - h / 2);
  }

  /* ── extended hours, accumulation zone, Bollinger Bands, volume shelf, BUY / SELL callouts ─────────────── */
  function drawExtHours(c, i0, i1) {  // pre- and post-market bars on a lighter background, as TradingView shades them
    const d = S.data, L = S.L, w = S.view.barW;
    c.fillStyle = C.ext;
    for (let i = i0; i <= i1; i++) {
      if (d.sess[i] !== "e") continue;
      let j = i;
      while (j < i1 && d.sess[j + 1] === "e") j++;
      c.fillRect(xOf(i) - w / 2, 0, (j - i + 1) * w, L.avail);
      i = j;
    }
  }

  function drawZone(c, yP, lw) {  // the analysis' accumulation zone (SMA200 ± 1 ATR, daily): where it would buy
    const z = S.zone, L = S.L, P = L.panes.price;
    if (!z || !isNum(z[0]) || !isNum(z[1])) return;
    const a = yP(Math.max(z[0], z[1])), b = yP(Math.min(z[0], z[1]));
    if (b < P.y || a > P.y + P.h) {  // off screen: a pill in the corner says where it is and how far
      const below = a > P.y + P.h, last = S.data.c[S.data.n - 1];
      const dist = ((below ? Math.max(...z) : Math.min(...z)) / last - 1) * 100;
      const text = `${below ? "▼" : "▲"} Accumulation zone ${fmtPrice(Math.min(...z), 0)}–${fmtPrice(Math.max(...z), 0)} · ${dist > 0 ? "+" : ""}${dist.toFixed(0)}%`;
      const w = c.measureText(text).width + 14 * S.dpr, h = L.font + 10 * S.dpr, x = 6 * S.dpr;
      const y = below ? P.y + P.h - h - 6 * S.dpr : P.y + 6 * S.dpr;
      c.fillStyle = "rgba(30,122,71,.85)"; c.fillRect(x, y, w, h);
      c.fillStyle = C.white; c.textAlign = "left"; c.fillText(text, x + 7 * S.dpr, y + h / 2);
      return;
    }
    c.fillStyle = C.zone;
    c.fillRect(0, a, L.plotW, b - a);
    c.strokeStyle = C.zoneEdge; c.lineWidth = lw; c.setLineDash([6 * S.dpr, 4 * S.dpr]);
    for (const y of [a, b]) { c.beginPath(); c.moveTo(0, Math.round(y) + 0.5); c.lineTo(L.plotW, Math.round(y) + 0.5); c.stroke(); }
    c.setLineDash([]);
    c.fillStyle = C.zoneEdge; c.textAlign = "left";
    c.fillText(`Accumulation zone ${fmtPrice(Math.min(...z), 0)}–${fmtPrice(Math.max(...z), 0)}`, 6 * S.dpr, Math.max(a, P.y) + L.font);
  }

  function drawBands(c, i0, i1, yP) {
    const d = S.data;
    if (!d.bbu.length) return;
    const fill = new Path2D();
    let started = false, last = -1;
    for (let i = i0; i <= i1; i++) {
      if (!(d.bbu[i] === d.bbu[i])) continue;
      if (!started) { fill.moveTo(xOf(i), yP(d.bbu[i])); started = true; } else fill.lineTo(xOf(i), yP(d.bbu[i]));
      last = i;
    }
    if (started) {
      for (let i = last; i >= i0; i--) if (d.bbl[i] === d.bbl[i]) fill.lineTo(xOf(i), yP(d.bbl[i]));
      fill.closePath();
      c.fillStyle = C.bbFill; c.fill(fill);
    }
    c.lineWidth = Math.max(1, 1.1 * S.dpr);
    line(c, d.bbu, i0, i1, yP, C.bbU);
    line(c, d.bbm, i0, i1, yP, C.bbM);
    line(c, d.bbl, i0, i1, yP, C.bbL);
  }

  /* Volume by price over the bars on screen (TradingView's "visible range" profile): each bar's volume spread evenly
     over the rows its high-low covers; the point of control (POC) is the busiest row, the value area the rows around it
     that hold 70% of the volume. */
  function shelfData(i0, i1, px) {
    const d = S.data, rows = 48;
    const lo = px.lo, step = (px.hi - px.lo) / rows;
    const vol = new Float64Array(rows);
    for (let i = i0; i <= i1; i++) {
      const v = d.vol[i];
      if (!(v > 0)) continue;
      const r0 = Math.max(0, Math.floor((d.l[i] - lo) / step)), r1 = Math.min(rows - 1, Math.floor((d.h[i] - lo) / step));
      if (r1 < r0) continue;
      const share = v / (r1 - r0 + 1);
      for (let r = r0; r <= r1; r++) vol[r] += share;
    }
    let poc = 0, total = 0;
    for (let r = 0; r < rows; r++) { total += vol[r]; if (vol[r] > vol[poc]) poc = r; }
    if (!(total > 0)) return null;
    let a = poc, b = poc, acc = vol[poc];
    while (acc < 0.7 * total && (a > 0 || b < rows - 1)) {
      const below = a > 0 ? vol[a - 1] : -1, above = b < rows - 1 ? vol[b + 1] : -1;
      if (above >= below) acc += vol[++b]; else acc += vol[--a];
    }
    return { rows, lo, step, vol, max: vol[poc], poc, vaLo: a, vaHi: b, pocPrice: lo + (poc + 0.5) * step,
      vah: lo + (b + 1) * step, val: lo + a * step };
  }

  function drawShelf(c, sh, yP, lw) {
    const L = S.L, width = L.plotW * 0.24;
    for (let r = 0; r < sh.rows; r++) {
      if (!(sh.vol[r] > 0)) continue;
      const w = sh.vol[r] / sh.max * width;
      const y0 = yP(sh.lo + (r + 1) * sh.step), y1 = yP(sh.lo + r * sh.step);
      c.fillStyle = r === sh.poc ? "rgba(255,152,0,.42)" : r >= sh.vaLo && r <= sh.vaHi ? C.shelfVA : C.shelf;
      c.fillRect(L.plotW - w, y0 + lw * 0.5, w, Math.max(lw, y1 - y0 - lw));
    }
    const y = Math.round(yP(sh.pocPrice)) + 0.5;  // the POC across the pane, as on TradexWhisperer's charts
    c.strokeStyle = C.vpoc; c.globalAlpha = 0.8; c.lineWidth = lw;
    c.beginPath(); c.moveTo(0, y); c.lineTo(L.plotW, y); c.stroke();
    c.globalAlpha = 1;
  }

  function drawCallouts(c, i0, i1, yP) {  // the newest signals win when they would overlap
    const d = S.data, L = S.L, P = L.panes.price;
    const h = L.font + 8 * S.dpr, gap = 7 * S.dpr, arrow = 5 * S.dpr;
    let lastX = Infinity;
    c.font = `700 ${L.font}px ui-monospace, "DejaVu Sans Mono", Menlo, monospace`;
    c.textAlign = "center";
    for (let i = i1; i >= i0; i--) {
      const k = CALLOUT[d.paint[i]];
      if (!k) continue;
      const [text, bg, fg, side] = k;
      const x = xOf(i), w = c.measureText(text).width + 12 * S.dpr;
      if (lastX - x < w + 4 * S.dpr) continue;
      lastX = x;
      const tip = side > 0 ? yP(d.l[i]) + gap : yP(d.h[i]) - gap;
      const top = side > 0 ? tip + arrow : tip - arrow - h;
      if (top < P.y || top + h > P.y + P.h) continue;
      c.fillStyle = bg;
      c.beginPath();
      c.moveTo(x, tip); c.lineTo(x - arrow, side > 0 ? tip + arrow : tip - arrow); c.lineTo(x + arrow, side > 0 ? tip + arrow : tip - arrow);
      c.closePath(); c.fill();
      c.globalAlpha = 0.92;
      c.fillRect(x - w / 2, top, w, h);
      c.globalAlpha = 1;
      c.fillStyle = fg;
      c.fillText(text, x, top + h / 2 + S.dpr * 0.5);
    }
    c.font = `${L.font}px ui-monospace, "DejaVu Sans Mono", Menlo, monospace`;
  }

  function extraTags(yP) {  // the shelf's POC and horizontal-line drawings on the price axis
    const out = [], d = S.data;
    if (S.shelf) out.push({ y: yP(S.shelf.pocPrice), text: fmtPrice(S.shelf.pocPrice, d.dec), bg: C.vpoc, fg: "#10140A" });
    for (const it of S.draw.items) if (it.type === "hline") out.push({ y: yP(it.a.p), text: fmtPrice(it.a.p, d.dec), bg: "#F5B041", fg: "#10140A" });
    return out;
  }

  /* ── drawings: trend line, ray, horizontal line, Fibonacci retracement, rectangle. Anchors are (time, price), so a
     drawing stays put across zoom, pan and intervals; they are kept per ticker in this browser. ───────────────────── */
  const TOOLS = {
    trend: ["Trend line", "M4 20 20 4M4 20a1.5 1.5 0 1 0 0 .01M20 4a1.5 1.5 0 1 0 0 .01"],
    ray: ["Ray", "M4 20 22 2M4 20a1.5 1.5 0 1 0 0 .01"],
    hline: ["Horizontal line", "M2 12h20M12 12a1.5 1.5 0 1 0 0 .01"],
    fib: ["Fib retracement", "M3 5h18M3 9.5h18M3 14h18M3 19h18M4 19 20 5"],
    rect: ["Rectangle", "M5 5h14v14H5z"],
  };
  const FIB = [[0, "#9E9E9E"], [0.236, "#F44336"], [0.382, "#FF9800"], [0.5, "#4CAF50"], [0.618, "#26A69A"], [0.786, "#2196F3"], [1, "#9E9E9E"]];
  const DRAW_COL = "#FFD54F";
  const drawKey = (sym) => `lynchpin.tv.draw.${sym}`;

  function loadDrawings(sym) {
    S.draw.items = []; S.draw.sel = null; S.draw.pending = null;
    try {
      const arr = JSON.parse(localStorage.getItem(drawKey(sym)) || "[]");
      if (Array.isArray(arr)) S.draw.items = arr.filter((it) => it && TOOLS[it.type] && it.a && isNum(it.a.t) && isNum(it.a.p)
        && (it.type === "hline" || (it.b && isNum(it.b.t) && isNum(it.b.p)))).slice(0, 100);
    } catch (_) { /* private mode */ }
  }
  function saveDrawings() {
    try { localStorage.setItem(drawKey(S.sym), JSON.stringify(S.draw.items.map(({ type, a, b }) => ({ type, a, b })))); } catch (_) { /* full */ }
    renderDrawButton();
  }

  function barStep() {  // the recent spacing between bars, for times past either end of the loaded bars
    const d = S.data, n = d.n;
    return n > 1 ? (d.t[n - 1] - d.t[Math.max(0, n - 21)]) / Math.min(20, n - 1) : 60;
  }
  function idxOfTime(t) {
    const d = S.data, n = d.n;
    if (t >= d.t[n - 1]) return n - 1 + (t - d.t[n - 1]) / barStep();
    if (t <= d.t[0]) return (t - d.t[0]) / barStep();
    const i = lowerBound(d.t, t);
    return d.t[i] === t ? i : i - 1 + (t - d.t[i - 1]) / (d.t[i] - d.t[i - 1]);
  }
  function timeOfIdx(fi) {
    const d = S.data, n = d.n;
    if (fi >= n - 1) return d.t[n - 1] + (fi - (n - 1)) * barStep();
    if (fi <= 0) return d.t[0] + fi * barStep();
    const i = Math.floor(fi);
    return d.t[i] + (fi - i) * (d.t[i + 1] - d.t[i]);
  }
  const idxOfX = (x) => S.view.right + 0.5 + (x - S.L.plotW) / S.view.barW;
  function priceOfY(y) { const P = S.L.panes.price, px = S.scale.px; return px.hi - (y - P.y) / P.h * (px.hi - px.lo); }
  const anchorAt = (pt) => ({ t: timeOfIdx(idxOfX(pt.x)), p: priceOfY(pt.y) });
  function geom(it) {
    const yP = S.scale.yP, xa = xOf(idxOfTime(it.a.t)), ya = yP(it.a.p);
    const xb = it.b ? xOf(idxOfTime(it.b.t)) : xa, yb = it.b ? yP(it.b.p) : ya;
    return { xa, ya, xb, yb };
  }
  function fibLevels(it) { return FIB.map(([lv, col]) => [lv, col, it.b.p + (it.a.p - it.b.p) * lv]); }
  function rayEnd(g) {
    const L = S.L, dx = g.xb - g.xa, dy = g.yb - g.ya;
    if (Math.abs(dx) < 1e-9) return { x: g.xb, y: dy > 0 ? L.H : 0 };
    const k = ((dx > 0 ? L.plotW : 0) - g.xa) / dx;
    return { x: g.xa + dx * k, y: g.ya + dy * k };
  }

  function drawDrawings(c, yP, lw) {
    if (!S.scale) return;
    const L = S.L, items = S.draw.pending ? S.draw.items.concat([S.draw.pending]) : S.draw.items;
    for (const it of items) {
      const g = geom(it), sel = it === S.draw.sel || it === S.draw.pending;
      c.lineWidth = Math.max(1, (sel ? 2 : 1.5) * S.dpr);
      c.strokeStyle = DRAW_COL;
      if (it.type === "trend" || it.type === "ray") {
        const e = it.type === "ray" ? rayEnd(g) : { x: g.xb, y: g.yb };
        c.beginPath(); c.moveTo(g.xa, g.ya); c.lineTo(e.x, e.y); c.stroke();
      } else if (it.type === "hline") {
        c.beginPath(); c.moveTo(0, Math.round(g.ya) + 0.5); c.lineTo(L.plotW, Math.round(g.ya) + 0.5); c.stroke();
      } else if (it.type === "rect") {
        const x = Math.min(g.xa, g.xb), y = Math.min(g.ya, g.yb), w = Math.abs(g.xb - g.xa), h = Math.abs(g.yb - g.ya);
        c.fillStyle = "rgba(93,156,236,.12)"; c.fillRect(x, y, w, h);
        c.strokeStyle = "rgba(93,156,236,.85)"; c.strokeRect(x, y, w, h);
      } else if (it.type === "fib") {
        const x0 = Math.min(g.xa, g.xb), x1 = Math.max(g.xa, g.xb) + (Math.abs(g.xb - g.xa) < 4 ? L.plotW : 0);
        const lv = fibLevels(it);
        for (let k = 0; k < lv.length - 1; k++) {  // translucent bands between levels
          const ya = yP(lv[k][2]), yb = yP(lv[k + 1][2]);
          c.globalAlpha = 0.07; c.fillStyle = lv[k + 1][1];
          c.fillRect(x0, Math.min(ya, yb), x1 - x0, Math.abs(yb - ya));
        }
        c.globalAlpha = 1;
        c.textAlign = "left";
        for (const [lvl, col, price] of lv) {
          const y = Math.round(yP(price)) + 0.5;
          c.strokeStyle = col; c.lineWidth = Math.max(1, S.dpr);
          c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke();
          c.fillStyle = col;
          c.fillText(`${lvl} (${fmtPrice(price, S.data.dec)})`, x0 + 4 * S.dpr, y - L.font * 0.6);
        }
        c.strokeStyle = "rgba(255,213,79,.5)"; c.setLineDash([4 * S.dpr, 4 * S.dpr]);
        c.beginPath(); c.moveTo(g.xa, g.ya); c.lineTo(g.xb, g.yb); c.stroke(); c.setLineDash([]);
      }
      if (sel) {  // handles
        c.fillStyle = "#121212"; c.strokeStyle = DRAW_COL; c.lineWidth = Math.max(1, 1.5 * S.dpr);
        for (const [x, y] of it.type === "hline" ? [[g.xa, g.ya]] : [[g.xa, g.ya], [g.xb, g.yb]]) {
          c.beginPath(); c.arc(x, y, 4.5 * S.dpr, 0, Math.PI * 2); c.fill(); c.stroke();
        }
      }
    }
  }

  function distSeg(px_, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
    const t = l2 ? Math.max(0, Math.min(1, ((px_ - ax) * dx + (py - ay) * dy) / l2)) : 0;
    return Math.hypot(px_ - (ax + t * dx), py - (ay + t * dy));
  }
  function hitDrawing(pt) {  // → {item, handle: "a" | "b" | null} or null; the newest drawing first
    if (!S.scale) return null;
    const tol = 8 * S.dpr;
    for (let k = S.draw.items.length - 1; k >= 0; k--) {
      const it = S.draw.items[k], g = geom(it);
      if (Math.hypot(pt.x - g.xa, pt.y - g.ya) < tol * 1.4) return { item: it, handle: "a" };
      if (it.type !== "hline" && Math.hypot(pt.x - g.xb, pt.y - g.yb) < tol * 1.4) return { item: it, handle: "b" };
      let hit = false;
      if (it.type === "trend") hit = distSeg(pt.x, pt.y, g.xa, g.ya, g.xb, g.yb) < tol;
      else if (it.type === "ray") { const e = rayEnd(g); hit = distSeg(pt.x, pt.y, g.xa, g.ya, e.x, e.y) < tol; }
      else if (it.type === "hline") hit = Math.abs(pt.y - g.ya) < tol;
      else if (it.type === "rect") hit = pt.x >= Math.min(g.xa, g.xb) - tol && pt.x <= Math.max(g.xa, g.xb) + tol
        && pt.y >= Math.min(g.ya, g.yb) - tol && pt.y <= Math.max(g.ya, g.yb) + tol;
      else if (it.type === "fib") {
        const x0 = Math.min(g.xa, g.xb) - tol, x1 = Math.max(g.xa, g.xb) + tol;
        hit = pt.x >= x0 && pt.x <= x1 && fibLevels(it).some(([, , p]) => Math.abs(S.scale.yP(p) - pt.y) < tol);
      }
      if (hit) return { item: it, handle: null };
    }
    return null;
  }

  function setTool(tool) {
    S.draw.tool = tool && TOOLS[tool] ? tool : null;
    S.draw.pending = null;
    if (S.draw.tool) { S.draw.sel = null; status(`${TOOLS[S.draw.tool][0]}: ${S.draw.tool === "hline" ? "tap a price" : "drag, or tap two points"} · Esc cancels`); }
    renderDrawButton();
    requestDraw();
  }
  function finishDrawing() {
    const it = S.draw.pending;
    S.draw.pending = null;
    S.draw.tool = null;
    if (it) { S.draw.items.push(it); S.draw.sel = it; saveDrawings(); }
    status("");
    renderDrawButton();
    requestDraw();
  }
  function deleteSelected() {
    if (!S.draw.sel) return false;
    S.draw.items = S.draw.items.filter((x) => x !== S.draw.sel);
    S.draw.sel = null;
    saveDrawings();
    requestDraw();
    return true;
  }

  /* ── assess the setup with Claude / ChatGPT: a snapshot of the chart and a prompt written for the candles' horizon,
     like the Deep Dive Prompt is for the long-term case. ────────────────────────────────────────────────────────── */
  const HORIZONS = [
    [["1", "2", "3", "5"], "scalp", "a scalp / intraday trade held minutes to about an hour",
      "Stops are tight (beyond the last swing on this chart or about one bar-ATR); session timing matters: the open's first 30 minutes and the last hour are the most volatile, lunch the thinnest, pre- and post-market the least liquid."],
    [["10", "15", "30", "45", "60"], "intraday swing", "an intraday-to-short swing trade held hours to about three days",
      "Weigh the session's trend against the daily chart's; overnight gaps are a risk for anything held past the close."],
    [["120", "180", "240", "1D"], "swing", "a swing trade held days to several weeks",
      "Earnings dates, gaps and the weekly trend matter more than intraday noise; size the stop for the daily range."],
    [["1W", "1M", "3M", "6M", "12M"], "position", "a position held months to years",
      "Valuation and the business decide over this horizon; the chart is for timing entries and sizing."],
  ];
  const horizonOf = (code) => HORIZONS.find((h) => h[0].includes(code)) || HORIZONS[2];

  function lastSignal(d, i) {  // the newest BUY / SELL at or before bar i, and a TRIM after it
    if (!d.paint) return null;
    for (let k = i; k >= 0; k--) {
      const ch = d.paint[k];
      if (ch === "B" || ch === "P") {
        const trim = ch === "B" ? d.paint.lastIndexOf("L", i) : -1;
        return { kind: ch === "B" ? "BUY" : "SELL", ago: i - k, price: d.c[k], trimAgo: trim > k ? i - trim : null };
      }
    }
    return null;
  }

  function buildPrompt() {
    const d = S.data, i = d.n - 1, dec = d.dec, f = (v) => fmtPrice(v, dec);
    const [, hname, htext, hnote] = horizonOf(d.code);
    const label = LABEL[d.code];
    const vAvg = (() => { let s = 0, n = 0; for (let k = Math.max(0, i - 20); k < i; k++) if (d.vol[k] > 0) { s += d.vol[k]; n++; } return n ? s / n : NaN; })();
    const above = (v) => (isNum(v) ? `${f(v)} (price ${d.c[i] >= v ? "above" : "below"})` : "n/a");
    const pctB = isNum(d.bbu[i]) ? (d.c[i] - d.bbl[i]) / (d.bbu[i] - d.bbl[i]) : NaN;
    const bw = isNum(d.bbu[i]) ? (d.bbu[i] - d.bbl[i]) / d.bbm[i] * 100 : NaN;
    let bwMin = Infinity;
    for (let k = Math.max(0, i - 120); k <= i; k++) if (isNum(d.bbu[k])) bwMin = Math.min(bwMin, (d.bbu[k] - d.bbl[k]) / d.bbm[k] * 100);
    const sig = lastSignal(d, i);
    const L = [];
    L.push(`You are a disciplined professional trader. Assess the trading setup in the attached chart of ${d.sym}` +
      `${S.info.name || d.name ? ` (${S.info.name || d.name})` : ""}: ${label} candles${d.exchange ? `, ${d.exchange}` : ""}, ` +
      `last bar ${timeLabel(i)} (${d.tz || "exchange time"})${d.sess ? ", pre- and post-market included (lighter background)" : ", regular session"}. ` +
      `Treat it as ${htext}. The numbers below are exact; use the image for structure (trend, patterns, volume), not to read prices.`);
    L.push("", "Chart data (last bar):");
    const chg = i > 0 ? (d.c[i] / d.c[i - 1] - 1) * 100 : NaN;
    const extBar = d.sess && d.sess[i] === "e";
    const volText = d.vol[i] > 0 ? `volume ${fmtVol(d.vol[i])}${isNum(vAvg) && vAvg > 0 ? ` (${(d.vol[i] / vAvg).toFixed(2)}x the 20-bar average)` : ""}`
      : `volume not reported${extBar ? " (extended hours)" : ""}`;
    L.push(`- Price ${f(d.c[i])} (${isNum(chg) ? (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%" : "n/a"} on the bar${extBar ? ", a pre- / post-market bar" : ""}); O ${f(d.o[i])} H ${f(d.h[i])} L ${f(d.l[i])}; ${volText}`);
    if (extBar && isNum(d.price)) L.push(`- Last regular-session price: ${f(d.price)}`);
    L.push(`- Trend: SMA 50 ${above(d.sma50[i])}, SMA 100 ${above(d.sma100[i])}, SMA 200 ${above(d.sma200[i])}; EMA 8 ${above(d.ema8[i])}, EMA 21 ${above(d.ema21[i])}`);
    const hr = d.hist[i] - d.hist[i - 1];
    L.push(`- Momentum: RSI 14 ${isNum(d.rsi[i]) ? d.rsi[i].toFixed(1) : "n/a"}; MACD 12/26/9 ${isNum(d.macd[i]) ? d.macd[i].toFixed(dec + 1) : "n/a"} vs signal ${isNum(d.signal[i]) ? d.signal[i].toFixed(dec + 1) : "n/a"} (histogram ${isNum(d.hist[i]) ? d.hist[i].toFixed(dec + 1) : "n/a"}, ${hr > 0 ? "rising" : hr < 0 ? "falling" : "flat"})`);
    if (isNum(pctB)) L.push(`- Volatility: Bollinger (20, 2) ${f(d.bbu[i])} / ${f(d.bbm[i])} / ${f(d.bbl[i])}; %B ${pctB.toFixed(2)}; bandwidth ${bw.toFixed(1)}%` +
      (isNum(bwMin) && bw <= bwMin * 1.1 ? " (near its narrowest of the last 120 bars: a squeeze)" : ""));
    if (S.shelf) L.push(`- Volume shelf (bars on screen): point of control ${f(S.shelf.pocPrice)}, value area (70%) ${f(S.shelf.val)}–${f(S.shelf.vah)}`);
    if (sig) L.push(`- BUY / SELL signal: ${sig.kind === "BUY" ? "bullish" : "bearish"} since a ${sig.kind} ${sig.ago} bar${sig.ago === 1 ? "" : "s"} ago at ${f(sig.price)}` +
      (sig.trimAgo !== null ? `; TRIM (first pullback after overbought) ${sig.trimAgo} bar${sig.trimAgo === 1 ? "" : "s"} ago` : "") +
      ". (BUY: momentum turns up after an oversold stochastic, or a close above the prior high with the fast MACD rising; SELL: a close below the prior low with the fast MACD falling.)");
    const lv = S.levels;
    if (lv) {
      const lvl = (x, k) => `${k} ${f(x.price)}${isNum(x.p_touch_1m) ? ` (${x.p_touch_1m.toFixed(0)}% chance of a touch within a month)` : ""}`;
      const parts = (lv.resistance || []).map((x, k) => lvl(x, `R${k + 1}`)).concat((lv.support || []).map((x, k) => lvl(x, `S${k + 1}`)));
      if (parts.length) L.push(`- Support / resistance (daily, clustered pivots): ${parts.join("; ")}`);
      const ex = [];
      if (isNum(lv.poc)) ex.push(`3-month volume POC ${f(lv.poc)}`);
      if (isNum(lv.low_52w)) ex.push(`52-week range ${f(lv.low_52w)}–${f(lv.high_52w)}`);
      const m = lv.ranges && lv.ranges["1m"];
      if (m) ex.push(`1-month expected range (±1σ) ${f(m["1sigma_lower"])}–${f(m["1sigma_upper"])}`);
      if (ex.length) L.push(`- ${ex.join("; ")}`);
    }
    if (S.zone && isNum(S.zone[0])) L.push(`- Accumulation zone (daily SMA 200 ± 1 ATR): ${f(Math.min(...S.zone))}–${f(Math.max(...S.zone))}`);
    const dr = S.draw.items.map((it) => {
      if (it.type === "fib") return `Fibonacci retracement from ${f(it.a.p)} to ${f(it.b.p)}: ${fibLevels(it).map(([lv_, , p]) => `${lv_} ${f(p)}`).join(", ")}`;
      if (it.type === "hline") return `horizontal line at ${f(it.a.p)}`;
      if (it.type === "rect") return `box ${f(Math.min(it.a.p, it.b.p))}–${f(Math.max(it.a.p, it.b.p))}`;
      return `${it.type === "ray" ? "ray" : "trend line"} from ${f(it.a.p)} to ${f(it.b.p)}`;
    });
    if (dr.length) L.push(`- My drawings: ${dr.join("; ")}`);
    const st = S.info.stats;
    if (st && (hname === "swing" || hname === "position") && isNum(st.PEG)) {
      L.push(`- Long-term context (the Lynch Pin): PEG ${st.PEG.toFixed(2)}${isNum(st.Dev_SD) ? `, ${st.Dev_SD > 0 ? "+" : ""}${st.Dev_SD.toFixed(2)} SD vs its 5-year mean` : ""}` +
        `${isNum(st.Base) ? `; 5-year base-case ROI ${st.Base.toFixed(1)}%/yr (bull ${isNum(st.Bull) ? st.Bull.toFixed(1) : "?"}%, bear ${isNum(st.Bear) ? st.Bear.toFixed(1) : "?"}%)` : ""}`);
    }
    L.push("", `Horizon: ${hname}. ${hnote}`, "",
      "Answer in this order, concisely and with exact prices:",
      "1. Read: trend, momentum, volatility and volume on this timeframe, and what the next higher timeframe likely says.",
      "2. Bias: long, short or stand aside, with a conviction score out of 10.",
      `3. Plan for ${htext}: entry (zone or trigger), stop (where the setup is wrong), two targets, and the reward:risk of each.`,
      "4. Confirmation: what has to happen in the next few bars. Invalidation: what kills the setup.",
      "5. Risks: scheduled events (earnings, macro data), liquidity and timing, and how this setup typically fails.",
      "6. One-line verdict.",
      "If it is not a tradable setup on this timeframe, say so and why. Not financial advice: a second opinion for my own decision.");
    return L.join("\n");
  }

  function snapshot() {  // the chart as a PNG: a header with the ticker and the last bar's numbers above the canvas
    const d = S.data, i = d.n - 1, dpr = S.dpr, W = canvas.width, head = Math.round(58 * dpr), foot = Math.round(22 * dpr);
    const out = document.createElement("canvas");
    out.width = W; out.height = canvas.height + head + foot;
    const c = out.getContext("2d");
    c.fillStyle = C.bg; c.fillRect(0, 0, out.width, out.height);
    c.textBaseline = "middle"; c.textAlign = "left";
    const title = `$${d.sym} · ${LABEL[d.code]}${d.sess ? " · ext. hours" : ""} · ${timeLabel(i)}`;
    c.fillStyle = C.white; c.font = `800 ${Math.round(17 * dpr)}px system-ui, -apple-system, "Segoe UI", sans-serif`;
    const tw = c.measureText(title).width;
    if (tw > W - 24 * dpr) c.font = `800 ${Math.floor(17 * dpr * (W - 24 * dpr) / tw)}px system-ui, -apple-system, "Segoe UI", sans-serif`;
    c.fillText(title, 12 * dpr, 18 * dpr);
    const f = (v) => fmtPrice(v, d.dec);
    const stats = `O ${f(d.o[i])}  H ${f(d.h[i])}  L ${f(d.l[i])}  C ${f(d.c[i])}  Vol ${fmtVol(d.vol[i])}  RSI ${isNum(d.rsi[i]) ? d.rsi[i].toFixed(1) : "—"}  MACD ${isNum(d.macd[i]) ? d.macd[i].toFixed(d.dec) : "—"}`;
    let size = 12 * dpr;
    c.font = `${Math.round(size)}px ui-monospace, Menlo, monospace`;
    const room = W - 24 * dpr, need = c.measureText(stats).width;
    if (need > room) { size *= room / need; c.font = `${Math.max(6, Math.floor(size))}px ui-monospace, Menlo, monospace`; }  // a phone: fit it
    c.fillStyle = C.text;
    c.fillText(stats, 12 * dpr, 42 * dpr);
    c.drawImage(canvas, 0, head);
    c.fillStyle = C.dim; c.font = `${Math.round(11 * dpr)}px system-ui, sans-serif`;
    c.fillText("The Lynch Pin · Quant Portal — SMA 50/100/200, EMA 8/21, Bollinger 20/2, volume shelf, BUY/SELL signals, S/R levels", 12 * dpr, out.height - foot / 2);
    return out;
  }

  async function copyText(text, host) {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall back */ }
    }
    // plain-http LAN pages have no clipboard API: copy from a textarea, inside the dialog (outside a modal dialog
    // everything is inert, so a textarea under <body> can't take the focus the copy needs)
    const ta = el("textarea", { readonly: true, class: "dd-clip", "aria-hidden": "true", tabindex: "-1" });
    ta.value = text;
    (host || dlg).append(ta); ta.focus({ preventScroll: true }); ta.select(); ta.setSelectionRange(0, text.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }

  /* The chart's download button: the lightbox's (app.js DOWNLOAD_ICON); in the iPhone home-screen app, where a
     download would strand the page, the PNG goes to the share sheet ("Save Image") instead. */
  function chartDownload(url, blob, name) {
    const a = el("a", { class: "tv-ai-dl", href: url, download: name, title: "Download chart", "aria-label": "Download chart" },
      typeof DOWNLOAD_ICON !== "undefined" ? DOWNLOAD_ICON.cloneNode(true) : "⤓");
    a.addEventListener("click", (e) => {
      if (navigator.standalone !== true || !navigator.canShare) return;
      const file = new File([blob], name, { type: "image/png" });
      if (!navigator.canShare({ files: [file] })) return;
      e.preventDefault();
      navigator.share({ files: [file] }).catch(() => {});
    });
    return a;
  }

  let aiPanel = null, aiUrl = null;
  function openAssess() {
    if (!S.data || !S.L) return;
    const shot = snapshot(), prompt = buildPrompt();
    const [, hname, htext] = horizonOf(S.data.code);
    const name = `${S.data.sym}_${LABEL[S.data.code]}_setup.png`;
    if (aiUrl) URL.revokeObjectURL(aiUrl);
    const say = el("p", { class: "sr-only", role: "status", "aria-live": "polite" });
    const pre = el("pre", { class: "dd-text tv-ai-text", tabindex: "0", hidden: true, text: prompt });
    const showBtn = el("button", { type: "button", class: "btn-ghost", "aria-expanded": "false",
      onclick: () => { pre.hidden = !pre.hidden; showBtn.textContent = pre.hidden ? "Show" : "Hide"; showBtn.setAttribute("aria-expanded", String(!pre.hidden)); } }, "Show");
    const copyBtn = el("button", { type: "button", class: "btn-primary btn-sm",
      onclick: async () => {
        const ok = await copyText(prompt, aiPanel);
        copyBtn.textContent = ok ? "Copied ✓" : "Select & copy";
        say.textContent = ok ? "Prompt copied" : "Copy failed: the prompt is shown, select it";
        if (!ok) { pre.hidden = false; showBtn.textContent = "Hide"; window.getSelection().selectAllChildren(pre); }
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
      } }, "Copy");
    const dlBox = el("span", { class: "tv-ai-dlbox" });
    const img = el("img", { class: "tv-ai-img", alt: `Snapshot of the ${S.data.sym} ${LABEL[S.data.code]} chart`, src: shot.toDataURL("image/png") });
    shot.toBlob((blob) => {
      if (!blob) return;
      aiUrl = URL.createObjectURL(blob);  // a download, not an image load: blob: is fine (the preview needs data:)
      dlBox.append(chartDownload(aiUrl, blob, name));
    }, "image/png");
    put(aiPanel,
      el("div", { class: "card-title-row dd-head tv-ai-head" },
        el("h3", { text: "🔬 Trading setup prompt" }),
        el("div", { class: "dd-actions" }, copyBtn, showBtn, dlBox,
          el("button", { type: "button", class: "tv-sheet-x", "aria-label": "Close", onclick: () => { aiPanel.hidden = true; } }, "×"))),
      el("p", { class: "muted small", text: `${LABEL[S.data.code]} candles → ${hname}: ${htext}. Paste it into Claude or ChatGPT with the chart attached (⤓ saves it). ${prompt.split(/\s+/).length} words.` }),
      say, pre, img);
    sheet.hidden = true;
    aiPanel.hidden = false;
  }

  /* ── the countdown to the next candle, and LIVE: the last candle every 2 s ─────────────────────────────────── */
  function barLeft() {  // seconds until the last candle closes, or null (closed market, weekly / monthly bars)
    const d = S.data;
    if (!d || !d.n) return null;
    const now = Date.now() / 1000;
    let end = null;
    if (d.step) end = d.t[d.n - 1] + d.step;
    else if (d.code === "1D" && d.regular && now >= d.regular.start) end = d.regular.end;
    if (end === null || now >= end || end - now > (d.step || 86400)) return null;
    return end - now;
  }
  function fmtLeft(x) {
    const s_ = Math.max(0, Math.ceil(x)), h = Math.floor(s_ / 3600), m = Math.floor(s_ % 3600 / 60), sec = s_ % 60;
    return h ? `${h}:${pad2(m)}:${pad2(sec)}` : `${pad2(m)}:${pad2(sec)}`;
  }
  function tick() {  // every second while open: the countdown, and the next candle as soon as it is due
    if (!S.open || document.visibilityState === "hidden" || !S.data) return;
    const left = barLeft();
    if (left !== null) { S.wasCounting = true; requestDraw(); return; }
    if (S.wasCounting) {  // the candle just closed: fetch the next one (Yahoo has it a few seconds later)
      S.wasCounting = false;
      requestDraw();
      setTimeout(() => { if (S.open) load(true); }, 3000);
    }
  }

  function toggleLive() {
    if (S.live) { stopLive(); status(""); }
    else { S.live = true; S.liveToken = (S.liveToken || 0) + 1; liveTick(S.liveToken); status("live: every 2 s"); }
    markActive();
  }
  function stopLive() {
    S.live = false;
    S.liveToken = (S.liveToken || 0) + 1;
    clearTimeout(S.liveTimer);
    if (liveBtn) liveBtn.setAttribute("aria-pressed", "false");
  }
  async function liveTick(token) {
    clearTimeout(S.liveTimer);
    if (!S.live || token !== S.liveToken || !S.open || document.visibilityState === "hidden") return;
    const sym = S.sym, code = S.code, ext = extOn(code);
    let next = 2000;
    try {
      const res = await fetch(`/api/tv/${encodeURIComponent(sym)}/live?i=${encodeURIComponent(code)}${ext ? "&x=1" : ""}&s=${S.sid}`, { cache: "no-store" });
      if (token !== S.liveToken || sym !== S.sym) return;
      if (res.status === 429) {
        let msg = "live is full";
        try { msg = (await res.json()).error || msg; } catch (_) { /* not JSON */ }
        stopLive(); markActive(); status(""); message(`${msg.charAt(0).toUpperCase()}${msg.slice(1)}.`, "wait");
        setTimeout(() => { if (msgBox && msgBox.className.includes("wait")) message(""); }, 6000);
        return;
      }
      if (res.ok) {
        const p = await res.json();
        if (token !== S.liveToken) return;
        applyLive(p, code, ext);
        if (!p.open) { status("live: market closed"); next = 15000; } else status("live: every 2 s");
      } else next = 5000;
    } catch (_) { next = 5000; }
    S.liveTimer = setTimeout(() => liveTick(token), next);
  }
  function applyLive(p, code, ext) {  // today's 1-minute bars → the last candle, and new candles as they start
    const d = S.data;
    if (!d || !d.n || d.code !== code || d.ext !== ext || !Array.isArray(p.bars)) return;
    const n = d.n - 1, start = d.t[n];
    const end = d.step ? start + d.step : d.code === "1D" ? start + 20 * 3600 : Infinity;
    let h = d.h[n], l = d.l[n], c = d.c[n], vol = 0, seen = false;
    const later = new Map();  // candle start → [o, h, l, c, v] of the 1-minute bars past the last candle
    for (const [t, bo, bh, bl, bc, bv] of p.bars) {
      if (t < start) continue;
      if (t < end) { seen = true; h = Math.max(h, bh); l = Math.min(l, bl); c = bc; vol += bv || 0; continue; }
      if (!d.step) continue;
      const k = start + Math.floor((t - start) / d.step) * d.step;
      const b = later.get(k);
      if (b) { b[1] = Math.max(b[1], bh); b[2] = Math.min(b[2], bl); b[3] = bc; b[4] += bv || 0; }
      else later.set(k, [bo, bh, bl, bc, bv || 0]);
    }
    if (seen) {
      d.h[n] = h; d.l[n] = l; d.c[n] = c;
      if (d.step || d.code === "1D") d.vol[n] = Math.max(d.vol[n], vol);
    }
    for (const [t, b] of [...later.entries()].sort((a, b_) => a[0] - b_[0])) appendBar(d, t, b);
    const last = p.bars[p.bars.length - 1];
    if (last && (later.size || (!d.step && last[0] >= end))) {
      // the chart (and its indicators) is behind the market: ask for it again, at most every 20 s
      const now = Date.now();
      if (!S.liveReloadAt || now - S.liveReloadAt > 20000) { S.liveReloadAt = now; load(true); }
    }
    if (!seen && !later.size) return;
    if (d.regular && p.time >= d.regular.start && p.time < d.regular.end) d.price = d.c[d.n - 1];  // the header: regular hours
    if (later.size && S.view.right >= d.n - 1 - later.size - 1.5) S.view.right += later.size;  // following the last bar
    updateQuote();
    requestDraw();
  }

  function appendBar(d, t, [o, h, l, c, v]) {  // a candle the server hasn't built yet (indicators come with the next fetch)
    const n = d.n;
    const grow = (a, val) => { const b = new a.constructor(n + 1); b.set(a); b[n] = val; return b; };
    for (const k of ["sma50", "sma100", "sma200", "ema8", "ema21", "rsi", "macd", "signal", "hist", "bbu", "bbm", "bbl"]) d[k] = grow(d[k], NaN);
    d.t = grow(d.t, t); d.o = grow(d.o, o); d.h = grow(d.h, h); d.l = grow(d.l, l); d.c = grow(d.c, c); d.vol = grow(d.vol, v);
    const dm = Math.round((t - d.t[n - 1]) / 60);  // its calendar: the previous candle's, moved on
    let mins = d.mins[n - 1] + dm, D = d.D[n - 1], W = d.W[n - 1];
    if (mins >= 1440) { mins -= 1440; D += 1; W = (W + 1) % 7; }
    d.Y = grow(d.Y, d.Y[n - 1]); d.M = grow(d.M, d.M[n - 1]); d.D = grow(d.D, D); d.W = grow(d.W, W); d.mins = grow(d.mins, mins);
    if (d.paint) {  // the state carries on until the next fetch decides
      const bull = "gdLB".includes(d.paint[n - 1]);
      d.paint += bull ? (c >= o ? "g" : "d") : (c >= o ? "x" : "r");
    }
    if (d.sess) d.sess += d.sess[n - 1];
    d.n = n + 1;
  }

  /* ── time axis: calendar boundaries, the most significant first, never closer than ~90 px ───────────────── */
  function timeTicks(i0, i1) {
    const d = S.data, L = S.L;
    const minGap = 90 * S.dpr;
    const cands = [];
    const from = Math.max(i0, Math.floor(S.view.right - L.plotW / S.view.barW));
    for (let i = Math.max(1, from); i <= i1; i++) {
      let rank = 0, label = null, strong = false;
      if (d.Y[i] !== d.Y[i - 1]) { rank = 6; label = String(d.Y[i]); strong = true; }
      else if (d.M[i] !== d.M[i - 1]) { rank = d.M[i] % 3 === 0 ? 5 : 4; label = MONTHS[d.M[i]]; }
      else if (d.D[i] !== d.D[i - 1]) { rank = 3; label = d.intraday ? `${d.D[i]} ${MONTHS[d.M[i]]}` : String(d.D[i]); strong = d.intraday; }
      else if (d.intraday) {
        const m = d.mins[i];
        rank = m % 60 === 0 ? 2 : m % 30 === 0 ? 1.5 : m % 15 === 0 ? 1 : m % 5 === 0 ? 0.5 : 0;
        if (rank) label = `${pad2(Math.floor(m / 60))}:${pad2(m % 60)}`;  // round times only (no 15:54)
      }
      if (label && xOf(i) > 0 && xOf(i) < L.plotW) cands.push({ i, rank, label, strong });
    }
    cands.sort((a, b) => b.rank - a.rank || a.i - b.i);
    const taken = [];
    for (const k of cands) {
      const x = xOf(k.i);
      if (taken.every((t) => Math.abs(xOf(t.i) - x) >= minGap)) taken.push(k);
    }
    return taken;
  }

  function timeLabel(i) {
    const d = S.data;
    const date = `${DAYS[d.W[i]]} ${d.D[i]} ${MONTHS[d.M[i]]} '${String(d.Y[i]).slice(2)}`;
    if (d.intraday) return `${date} ${pad2(Math.floor(d.mins[i] / 60))}:${pad2(d.mins[i] % 60)}`;
    if (d.code === "1M" || d.code === "3M" || d.code === "6M" || d.code === "12M") return `${MONTHS[d.M[i]]} '${String(d.Y[i]).slice(2)}`;
    return date;
  }

  /* ── crosshair (overlay canvas) and the legend ─────────────────────────── */
  function drawOverlay() {
    const L = S.L, c = octx;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, L.W, L.H);
    const d = S.data, h = S.hover;
    if (!d || !h || !S.scale) return;
    const i = Math.max(0, Math.min(d.n - 1, iAt(h.x)));
    const x = Math.round(xOf(i)) + 0.5;
    const lw = Math.max(1, Math.round(S.dpr));
    c.strokeStyle = "rgba(224,224,224,.55)"; c.lineWidth = lw; c.setLineDash([4 * S.dpr, 4 * S.dpr]);
    c.beginPath(); c.moveTo(x, 0); c.lineTo(x, L.avail); c.stroke();
    let pane = null;
    for (const [k, p] of Object.entries(L.panes)) if (h.y >= p.y && h.y < p.y + p.h) pane = k;
    if (pane && h.x < L.plotW) {
      const y = Math.round(h.y) + 0.5;
      c.beginPath(); c.moveTo(0, y); c.lineTo(L.plotW, y); c.stroke();
      c.setLineDash([]);
      const p = L.panes[pane], sc = S.scale;
      let v;
      if (pane === "price") v = fmtPrice(sc.px.hi - (h.y - p.y) / p.h * (sc.px.hi - sc.px.lo), d.dec);
      else if (pane === "rsi") v = (100 * (1 - (h.y - p.y - 4 * S.dpr) / (p.h - 8 * S.dpr))).toFixed(1);
      else v = (sc.m.hi - (h.y - p.y) / p.h * (sc.m.hi - sc.m.lo)).toFixed(d.dec + 1);
      c.font = `${L.font}px ui-monospace, "DejaVu Sans Mono", Menlo, monospace`;
      c.textBaseline = "middle";
      tagOn(c, h.y, v);
    }
    c.setLineDash([]);
    // time tag
    c.font = `${L.font}px ui-monospace, "DejaVu Sans Mono", Menlo, monospace`;
    c.textBaseline = "middle";
    const text = timeLabel(i);
    const w = c.measureText(text).width + 12 * S.dpr;
    const tx = Math.max(w / 2, Math.min(L.plotW - w / 2, x));
    c.fillStyle = "#3A3A3A";
    c.fillRect(tx - w / 2, L.avail + lw, w, L.timeH - lw);
    c.fillStyle = C.white; c.textAlign = "center";
    c.fillText(text, tx, L.avail + L.timeH / 2);
  }
  function tagOn(c, y, text) {
    const L = S.L, h = L.font + 6 * S.dpr;
    c.fillStyle = "#3A3A3A";
    c.fillRect(L.plotW + S.dpr, Math.round(y - h / 2), L.axisW - S.dpr, h);
    c.fillStyle = C.white; c.textAlign = "left";
    c.fillText(text, L.plotW + 6 * S.dpr, y);
  }

  function signalSpan(d, i) {  // "BUY 3 bars ago" (and a TRIM since), when the paint is on
    if (!S.show.paint) return null;
    const sg = lastSignal(d, i);
    if (!sg) return null;
    return el("span", { class: `tv-l tv-sig tv-sig-${sg.kind.toLowerCase()}`, title: "The last BUY / SELL signal at this bar" },
      el("b", { text: sg.kind }), ` ${sg.ago ? `${sg.ago} bar${sg.ago === 1 ? "" : "s"} ago` : "now"}`,
      sg.trimAgo !== null ? el("b", { class: "tv-sig-trim", text: ` · TRIM ${sg.trimAgo || "now"}` }) : null);
  }

  function updateLegend() {
    const d = S.data;
    if (!legendBar) return;
    if (!d || !d.n) { put(legendBar); for (const b of legendSeries.querySelectorAll("b[data-val]")) b.textContent = ""; return; }
    const i = S.hover && S.L ? Math.max(0, Math.min(d.n - 1, iAt(S.hover.x))) : d.n - 1;
    const prev = i > 0 ? d.c[i - 1] : d.o[i];
    const ch = d.c[i] - prev, pct = prev ? ch / prev * 100 : 0;
    const cls = d.c[i] >= d.o[i] ? "green" : "red";
    const f = (v) => fmtPrice(v, d.dec);
    put(legendBar,
      el("span", { class: "tv-l" }, "O ", el("b", { class: cls, text: f(d.o[i]) })),
      el("span", { class: "tv-l" }, "H ", el("b", { class: cls, text: f(d.h[i]) })),
      el("span", { class: "tv-l" }, "L ", el("b", { class: cls, text: f(d.l[i]) })),
      el("span", { class: "tv-l" }, "C ", el("b", { class: cls, text: f(d.c[i]) })),
      el("span", { class: `tv-l ${ch >= 0 ? "green" : "red"}`, text: `${ch >= 0 ? "+" : "−"}${f(Math.abs(ch))} (${ch >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%)` }),
      el("span", { class: "tv-l" }, "Vol ", el("b", { text: fmtVol(d.vol[i]) })),
      el("span", { class: "tv-l tv-ind" }, "RSI ", el("b", { class: "tv-c-rsi", text: isNum(d.rsi[i]) ? d.rsi[i].toFixed(1) : "—" })),
      el("span", { class: "tv-l tv-ind" }, "MACD ", el("b", { class: "tv-c-macd", text: isNum(d.macd[i]) ? d.macd[i].toFixed(d.dec) : "—" }),
        " ", el("b", { class: "tv-c-signal", text: isNum(d.signal[i]) ? d.signal[i].toFixed(d.dec) : "—" })),
      signalSpan(d, i),
      S.hover ? el("span", { class: "tv-l muted", text: timeLabel(i) }) : null);
    for (const b of legendSeries.querySelectorAll("b[data-val]")) {
      const v = d[b.dataset.val][i];
      b.textContent = isNum(v) ? f(v) : "—";
    }
  }

  /* ── pointer: drag to pan (with inertia), pinch / wheel to zoom, hover / tap / long-press for the crosshair ── */
  function bindPointer() {
    const ptrs = new Map();
    let drag = null, pinch = null, press = 0, inertia = 0, ed = null;
    const pos = (e) => { const r = overlay.getBoundingClientRect(); return { x: (e.clientX - r.left) * S.dpr, y: (e.clientY - r.top) * S.dpr }; };
    const inPrice = (p) => { const P = S.L.panes.price; return p.x < S.L.plotW && p.y >= P.y && p.y < P.y + P.h; };
    // drawings first: a tool in hand draws; otherwise a press on a drawing selects it and drags a handle or the whole of it
    const drawDown = (p) => {
      if (!S.scale || !inPrice(p)) return false;
      if (S.draw.tool) {
        const at = anchorAt(p);
        if (!S.draw.pending) S.draw.pending = { type: S.draw.tool, a: at, b: S.draw.tool === "hline" ? null : { ...at } };
        else S.draw.pending.b = at;  // the second tap
        ed = { mode: "create", x0: p.x, y0: p.y, moved: false, second: S.draw.pending.b && (S.draw.pending.b.t !== S.draw.pending.a.t || S.draw.pending.b.p !== S.draw.pending.a.p) };
        requestDraw();
        return true;
      }
      const hit = hitDrawing(p);
      if (hit) {
        const it = hit.item;
        S.draw.sel = it;
        ed = { mode: hit.handle ? "handle" : "move", handle: hit.handle, item: it, i0: idxOfX(p.x), p0: priceOfY(p.y), moved: false,
          a0: { i: idxOfTime(it.a.t), p: it.a.p }, b0: it.b ? { i: idxOfTime(it.b.t), p: it.b.p } : null };
        renderDrawButton(); requestDraw();
        return true;
      }
      if (S.draw.sel) { S.draw.sel = null; renderDrawButton(); requestDraw(); }
      return false;
    };
    const drawMove = (p) => {
      if (!ed.moved && Math.hypot(p.x - (ed.x0 ?? p.x), p.y - (ed.y0 ?? p.y)) > 5 * S.dpr) ed.moved = true;
      if (ed.mode === "create") {
        const pd = S.draw.pending;
        if (!pd) return;
        if (pd.type === "hline") pd.a = anchorAt(p);
        else if (ed.moved || ed.second) pd.b = anchorAt(p);
      } else if (ed.mode === "handle") {
        ed.moved = true;
        ed.item[ed.handle] = anchorAt(p);
      } else {
        ed.moved = true;
        const di = idxOfX(p.x) - ed.i0, dp = priceOfY(p.y) - ed.p0;
        ed.item.a = { t: timeOfIdx(ed.a0.i + di), p: ed.a0.p + dp };
        if (ed.b0) ed.item.b = { t: timeOfIdx(ed.b0.i + di), p: ed.b0.p + dp };
      }
      requestDraw();
    };
    const drawUp = () => {
      const e_ = ed;
      ed = null;
      if (e_.mode === "create") {
        const pd = S.draw.pending;
        if (pd && (pd.type === "hline" || e_.moved || e_.second)) finishDrawing();
        else status(`${TOOLS[S.draw.tool][0]}: now tap the second point · Esc cancels`);
      } else if (e_.moved) saveDrawings();
    };
    const stopInertia = () => { cancelAnimationFrame(inertia); inertia = 0; };
    const zoomAt = (x, factor) => {
      if (!S.data) return;
      const idx = S.view.right + 0.5 + (Math.min(x, S.L.plotW) - S.L.plotW) / S.view.barW;
      S.view.barW *= factor;
      clampView();
      S.view.right = idx - 0.5 - (Math.min(x, S.L.plotW) - S.L.plotW) / S.view.barW;
      clampView();
      S.range = null; markActive();
      requestDraw();
    };
    overlay.addEventListener("pointerdown", (e) => {
      if (!S.data) return;
      stopInertia();
      overlay.setPointerCapture(e.pointerId);
      const p = pos(e);
      ptrs.set(e.pointerId, p);
      if (ptrs.size === 1 && drawDown(p)) { drag = null; return; }
      if (ptrs.size === 1) {
        drag = { x0: p.x, y0: p.y, right0: S.view.right, moved: false, touch: e.pointerType !== "mouse", hair: false, lx: p.x, lt: performance.now(), v: 0 };
        if (drag.touch) press = setTimeout(() => { if (drag && !drag.moved) { drag.hair = true; S.hover = p; S.pinned = true; requestDraw(true); } }, 350);
      } else if (ptrs.size === 2) {
        clearTimeout(press);
        drag = null;
        const [a, b] = [...ptrs.values()];
        pinch = { dist: Math.abs(a.x - b.x) || 1, mid: (a.x + b.x) / 2 };
      }
    });
    overlay.addEventListener("pointermove", (e) => {
      const p = pos(e);
      if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, p);
      if (ed && ptrs.has(e.pointerId)) { drawMove(p); return; }
      if (S.draw.tool && S.draw.pending && !ed && S.draw.pending.type !== "hline" && e.pointerType === "mouse") {
        S.draw.pending.b = anchorAt(p); requestDraw();  // after the first click, the line follows the mouse
      }
      if (pinch && ptrs.size >= 2) {
        const [a, b] = [...ptrs.values()];
        const dist = Math.abs(a.x - b.x) || 1;
        zoomAt(pinch.mid, dist / pinch.dist);
        pinch.dist = dist;
        return;
      }
      if (drag) {
        if (drag.hair) { S.hover = p; requestDraw(true); return; }
        if (!drag.moved && Math.hypot(p.x - drag.x0, p.y - drag.y0) > 5 * S.dpr) { drag.moved = true; clearTimeout(press); if (!S.pinned) { S.hover = null; requestDraw(true); } }
        if (drag.moved) {
          const now = performance.now();
          S.view.right = drag.right0 - (p.x - drag.x0) / S.view.barW;
          clampView();
          drag.v = 0.8 * drag.v + 0.2 * ((p.x - drag.lx) / Math.max(1, now - drag.lt));
          drag.lx = p.x; drag.lt = now;
          S.range = null;
          requestDraw();
        }
        return;
      }
      if (e.pointerType === "mouse" && !S.pinned) { S.hover = p; requestDraw(true); }
    });
    const end = (e) => {
      clearTimeout(press);
      if (ed) { ptrs.delete(e.pointerId); drawUp(); return; }
      ptrs.delete(e.pointerId);
      if (ptrs.size < 2) pinch = null;
      if (!drag || ptrs.size) return;
      const dd = drag;
      drag = null;
      if (dd.hair) return;  // the crosshair stays where the finger left it
      if (!dd.moved) {  // a tap: crosshair on / off (touch); a click pins / unpins it (mouse)
        if (dd.touch) { S.pinned = !S.pinned || !S.hover; S.hover = S.pinned ? pos(e) : null; }
        else S.pinned = false;
        requestDraw(true);
        return;
      }
      let v = dd.v * 16;  // px per frame
      if (Math.abs(v) < 2 * S.dpr || performance.now() - dd.lt > 80) { markActive(); return; }
      const step = () => {
        v *= 0.92;
        S.view.right -= v / S.view.barW;
        clampView();
        requestDraw();
        inertia = Math.abs(v) > 0.4 ? requestAnimationFrame(step) : 0;
      };
      inertia = requestAnimationFrame(step);
      markActive();
    };
    overlay.addEventListener("pointerup", end);
    overlay.addEventListener("pointercancel", end);
    overlay.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse" && !S.pinned) { S.hover = null; requestDraw(true); } });
    overlay.addEventListener("dblclick", () => { resetView(); clampView(); S.range = null; markActive(); requestDraw(); });
    overlay.addEventListener("wheel", (e) => {
      if (!S.data) return;
      e.preventDefault();
      stopInertia();
      const p = pos(e);
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      if (e.ctrlKey || Math.abs(e.deltaY) >= Math.abs(e.deltaX)) zoomAt(p.x, Math.exp(-e.deltaY * unit * (e.ctrlKey ? 0.01 : 0.0025)));
      else { S.view.right += e.deltaX * unit * S.dpr / S.view.barW; clampView(); S.range = null; requestDraw(); }
      if (e.pointerType === undefined && !S.pinned) { S.hover = p; requestDraw(true); }
    }, { passive: false });
  }

  function onKey(e) {
    if (!S.data) return;
    const k = e.key;
    if ((k === "Delete" || k === "Backspace") && deleteSelected()) { e.preventDefault(); return; }
    if (k === "ArrowLeft" || k === "ArrowRight") { S.view.right += (k === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 10 : 1); clampView(); }
    else if (k === "+" || k === "=") S.view.barW *= 1.25;
    else if (k === "-" || k === "_") S.view.barW /= 1.25;
    else if (k === "Home") S.view.right = Math.min(S.data.n - 1, S.L.plotW / S.view.barW * 0.95);
    else if (k === "End" || k === "0") resetView();
    else return;
    e.preventDefault();
    clampView();
    S.range = null;
    markActive();
    requestDraw();
  }

  /* ── the analysis' levels arrive (or change) while the chart is open ───── */
  function setLevels(sym, levels, info) {
    if (!S.open || String(sym).toUpperCase() !== S.sym) return;
    S.levels = levels || null;
    if (info) { S.info = Object.assign({}, S.info, info); if (info.name) $("#tv-name").textContent = info.name; }
    S.zone = zoneOf(S.info);
    requestDraw();
  }
  function zoneOf(info) {  // the analysis' accumulation zone [low, high], or null
    const z = info && info.technicals && info.technicals.accumulation_zone;
    return Array.isArray(z) && isNum(z[0]) && isNum(z[1]) ? z : null;
  }

  function init() {
    loadPrefs();
    window.addEventListener("popstate", fromURL);
    fromURL();
  }

  window.LynchTV = { init, open, close: () => close(true), setLevels, isOpen: () => S.open, _test: { prepare, timeTicks, S } };
})();
