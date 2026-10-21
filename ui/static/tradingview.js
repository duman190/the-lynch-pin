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
  const QUICK = ["5", "15", "60", "240", "1D", "1W", "1M"];
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
  };
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
    show: { sma50: true, sma100: true, sma200: true, ema8: true, ema21: true, levels: true },
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
    } catch (_) { /* private mode */ }
  }
  function savePrefs() {
    try { localStorage.setItem(PREFS, JSON.stringify({ code: S.code, show: S.show })); } catch (_) { /* private mode */ }
  }

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
  let dlg, canvas, overlay, ctx, octx, wrap, msgBox, legendBar, legendSeries, sheet, intBtn, quickBox, rangeBox;

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
    quickBox = el("div", { class: "tv-quick", role: "group", "aria-label": "Interval" });
    rangeBox = el("div", { class: "tv-ranges", role: "group", "aria-label": "Date range" });
    for (const code of QUICK) quickBox.append(el("button", { type: "button", "data-code": code, onclick: () => setInterval_(code) }, LABEL[code]));
    for (const [label, code, span] of RANGES) rangeBox.append(el("button", { type: "button", "data-range": label, onclick: () => applyRange(label, code, span) }, label));
    legendBar = el("div", { class: "tv-ohlc", "aria-live": "off" });
    legendSeries = el("div", { class: "tv-series", role: "group", "aria-label": "Indicators shown" });
    canvas = el("canvas", { class: "tv-canvas", role: "img", "aria-label": "Candlestick chart" });
    overlay = el("canvas", { class: "tv-overlay", "aria-hidden": "true" });
    msgBox = el("div", { class: "tv-msg", role: "status", "aria-live": "polite", hidden: true });
    wrap = el("div", { class: "tv-chart", tabindex: "0", "aria-label": "Chart: drag to pan, wheel or pinch to zoom, arrows to move, double-click to reset" }, canvas, overlay, msgBox);
    sheet = el("div", { class: "tv-sheet", id: "tv-sheet", hidden: true, role: "dialog", "aria-label": "Interval and date range" });
    dlg = el("dialog", { class: "tv", id: "tv", "aria-labelledby": "tv-title" },
      el("header", { class: "tv-head" },
        el("div", { class: "tv-id" },
          el("h2", { class: "tv-title", id: "tv-title" }, el("span", { class: "sky" }, "$"), el("span", { id: "tv-sym" })),
          el("span", { class: "tv-name muted", id: "tv-name" })),
        el("div", { class: "tv-quote", id: "tv-quote" }),
        el("button", { type: "button", class: "tv-close", "aria-label": "Close the trading view", onclick: () => close(true) }, "×")),
      el("div", { class: "tv-toolbar" }, intBtn, quickBox, rangeBox, el("span", { class: "tv-status muted", id: "tv-status" })),
      el("div", { class: "tv-legend" }, legendBar, legendSeries),
      wrap, sheet);
    document.body.append(dlg);
    ctx = canvas.getContext("2d");
    octx = overlay.getContext("2d");
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); if (!sheet.hidden) toggleSheet(false); else close(true); });
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
        el("i", { class: "tv-lv-i" }), el("span", { class: "tv-chip-l", text: "S/R levels" }), el("span", { class: "tv-chip-s", text: "S/R" })));
    updateLegend();
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
    S.sym = sym;
    $("#tv-sym").textContent = sym;
    $("#tv-name").textContent = S.info.name || "";
    if (!S.open) {
      S.open = true;
      document.body.classList.add("tv-open");
      if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
      document.addEventListener("visibilitychange", onVisibility);
      window.addEventListener("pagehide", release);
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
    if (document.visibilityState === "visible") load(true);  // back: fresh bars (and this viewer's place) now
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
    if (!changed && S.data && S.data.code === code && S.data.sym === S.sym && !after) return;
    const hit = S.cache.get(`${S.sym}|${code}`);
    if (hit) { show(hit.data, hit.etag, { reset: true, after }); load(true, { after: null }); }
    else { S.data = null; S.etag = null; requestDraw(); message(`Loading ${LABEL[code]}…`, "loading"); load(false, { after }); }
  }

  async function load(revalidate, { after = null } = {}) {
    const token = ++S.token;
    clearTimeout(S.timer);
    const sym = S.sym, code = S.code;
    let res;
    try {
      // no-cache: the browser revalidates its copy with the ETag, and the server answers 304 while unchanged
      res = await fetch(`/api/tv/${encodeURIComponent(sym)}?i=${encodeURIComponent(code)}&s=${S.sid}`, { cache: "no-cache", headers: { Accept: "application/json" } });
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
    if (etag && etag === S.etag && S.data && S.data.sym === sym && S.data.code === code) {
      res.body && res.body.cancel && res.body.cancel().catch(() => {});  // unchanged (a 304 the browser answered from its copy)
    } else {
      let j;
      try { j = await res.json(); } catch (_) { if (token === S.token) message("Unreadable chart data — retrying…", "error"); return schedule(token, 10); }
      if (token !== S.token) return;
      const d = prepare(j);
      S.cache.set(`${sym}|${code}`, { data: d, etag });
      if (S.cache.size > 24) S.cache.delete(S.cache.keys().next().value);
      show(d, etag, { reset: !revalidate || !S.data || S.data.code !== code || S.data.sym !== sym, after });
    }
    schedule(token, isNum(refreshIn) ? refreshIn : MAX_POLL);
  }

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
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const W = Math.max(1, Math.round(r.width * dpr)), H = Math.max(1, Math.round(r.height * dpr));
    const keepSlots = S.L ? S.L.plotW / S.view.barW : null;
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
    for (const [k] of SERIES) {
      if (!S.show[k]) continue;
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
    // candles: wicks and bodies batched by colour (four fills, however many bars)
    const upW = new Path2D(), dnW = new Path2D(), upB = new Path2D(), dnB = new Path2D();
    const bw = S.view.barW * 0.72;
    const thin = bw < 1.6 * S.dpr;  // too narrow for a body: wicks only
    for (let i = i0; i <= i1; i++) {
      const x = xOf(i), isUp = d.c[i] >= d.o[i];
      const yh = yP(d.h[i]), yl = yP(d.l[i]);
      const wx = Math.round(x - lw / 2);
      (isUp ? upW : dnW).rect(wx, yh, lw, Math.max(lw, yl - yh));
      if (!thin) {
        const yo = yP(d.o[i]), yc = yP(d.c[i]);
        const top = Math.min(yo, yc), h = Math.max(lw, Math.abs(yc - yo));
        (isUp ? upB : dnB).rect(Math.round(x - bw / 2), top, Math.round(bw), h);
      }
    }
    c.fillStyle = C.up; c.fill(upW); c.fill(upB);
    c.fillStyle = C.down; c.fill(dnW); c.fill(dnB);
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
    const tags = S.show.levels ? levelTags(yP) : [];
    let fixed = null;
    if (ly > P.price.y && ly < P.price.y + P.price.h) {
      const col = lc >= d.o[last] ? C.up : C.down;
      c.strokeStyle = col; c.lineWidth = lw; c.setLineDash([2 * S.dpr, 3 * S.dpr]);
      c.beginPath(); c.moveTo(0, Math.round(ly) + 0.5); c.lineTo(L.plotW, Math.round(ly) + 0.5); c.stroke();
      c.setLineDash([]);
      fixed = { y: ly, text: fmtPrice(lc, d.dec), bg: col, fg: "#0B1422" };
    }
    for (const t of stackTags(tags, fixed, L.font + 6 * S.dpr)) tag(c, t.y, t.text, t.bg, t.fg);
    if (fixed) tag(c, fixed.y, fixed.text, fixed.bg, fixed.fg);
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
      S.hover ? el("span", { class: "tv-l muted", text: timeLabel(i) }) : null);
    for (const b of legendSeries.querySelectorAll("b[data-val]")) {
      const v = d[b.dataset.val][i];
      b.textContent = isNum(v) ? f(v) : "—";
    }
  }

  /* ── pointer: drag to pan (with inertia), pinch / wheel to zoom, hover / tap / long-press for the crosshair ── */
  function bindPointer() {
    const ptrs = new Map();
    let drag = null, pinch = null, press = 0, inertia = 0;
    const pos = (e) => { const r = overlay.getBoundingClientRect(); return { x: (e.clientX - r.left) * S.dpr, y: (e.clientY - r.top) * S.dpr }; };
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
    requestDraw();
  }

  function init() {
    loadPrefs();
    window.addEventListener("popstate", fromURL);
    fromURL();
  }

  window.LynchTV = { init, open, close: () => close(true), setLevels, isOpen: () => S.open, _test: { prepare, timeTicks, S } };
})();
