/* The Lynch Pin · Quant Portal — ticker search, progressive result rendering, AI overview
   (or, with the AI off, the rule-based Quick Overview).
   Depends on app.js helpers ($, el, getJSON, openLightbox, downloadLink). DOM is built with textContent only. */
"use strict";

(() => {
  const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
  const STAGE_LABELS = { stats: "Valuation", grades: "Grades", technicals: "Technicals", edge: "6M edge", plot: "Chart", ai: "AI overview" };
  const RECENT_KEY = "lynchpin.recent";
  const SVG_NS = "http://www.w3.org/2000/svg";

  const S = {
    app: null, sym: null, token: 0, timer: null, started: 0, rendered: new Set(),
    lastSnap: null, aiTimer: null, es: null,
  };

  /** replaceChildren that skips null/false (the DOM API would render them as "null"). */
  function put(node, ...kids) {
    node.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
  }

  /* ── formatting ─────────────────────────────────────────────────────────── */
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const fx = (v, d = 1, suf = "") => (isNum(v) ? v.toFixed(d) + suf : "N/A");
  const signed = (v, d = 0, suf = "%") => (isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(d) + suf : "N/A");
  function money(v, cur) {
    if (!isNum(v)) return "—";
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD", maximumFractionDigits: 2 }).format(v); }
    catch (_) { return `$${v.toFixed(2)}`; }
  }
  function bigMoney(v) {
    if (!isNum(v)) return null;
    for (const [n, s] of [[1e12, "T"], [1e9, "B"], [1e6, "M"]]) if (Math.abs(v) >= n) return `$${(v / n).toFixed(2)}${s}`;
    return `$${v.toFixed(0)}`;
  }

  /* ── recent lookups (localStorage, validated) ───────────────────────────── */
  function getRecent() {
    try {
      const arr = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
      return Array.isArray(arr) ? arr.filter((t) => typeof t === "string" && TICKER_RE.test(t)).slice(0, 8) : [];
    } catch (_) { return []; }
  }
  function pushRecent(sym) {
    const list = [sym, ...getRecent().filter((t) => t !== sym)].slice(0, 8);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (_) { /* private mode */ }
    renderRecent();
  }
  function renderRecent() {
    const box = $("#recent");
    const list = getRecent();
    put(box, ...list.map((t) => el("button", { type: "button", "aria-label": `Analyze ${t}`, onclick: () => go(t) }, `$${t}`)));
  }

  /* ── skeleton ───────────────────────────────────────────────────────────── */
  function stepper(stages) {
    const ol = el("ol", { class: "stepper", id: "stepper" });
    for (const s of stages) ol.append(el("li", { "data-stage": s, class: "pending" }, el("span", { class: "step-dot", "aria-hidden": "true" }), el("span", { text: STAGE_LABELS[s] })));
    return ol;
  }

  function skeleton(sym) {
    const res = $("#result");
    const stages = ["stats", "grades", "technicals", "edge", "plot"];
    const ai = !!(S.app.health && S.app.health.features.ai);
    if (ai) stages.push("ai");
    put(res, 
      el("div", { class: "panel result-head", id: "card-head" },
        el("div", { class: "rh-main" },
          el("h1", { class: "rh-ticker" }, el("span", { class: "sky" }, "$"), sym),
          el("div", { class: "rh-name muted", id: "rh-name", text: "Fetching quote…" })),
        el("div", { class: "rh-side" },
          el("div", { class: "rh-price", id: "rh-price" }),
          el("div", { class: "rh-badges", id: "rh-badges" }))),
      el("div", { class: "progress-row" },
        stepper(stages),
        el("p", { class: "status-line", id: "status-line", role: "status", "aria-live": "polite" }, "Queued…")),
      el("div", { class: "result-grid", id: "result-grid" },
        el("div", { class: "panel card card-valuation", id: "card-valuation" }, placeholder("Valuation")),
        el("figure", { class: "panel card card-plot", id: "card-plot" }, placeholder("PEG deviation chart")),
        el("div", { class: "panel card card-ai", id: "card-ai", hidden: !ai }, placeholder("AI overview")),
        el("div", { class: "panel card card-ai card-quick", id: "card-quick", hidden: ai }, placeholder("⚡ Quick overview")),
        el("div", { class: "panel card card-income", id: "card-income" }, placeholder("Income statement")),
        el("div", { class: "panel card card-credit", id: "card-credit" }, placeholder("Balance sheet")),
        el("div", { class: "panel card card-tech", id: "card-tech" }, placeholder("Technicals · 6M edge"))));
    res.hidden = false;
    res.setAttribute("aria-busy", "true");
  }

  function placeholder(title) {
    return [el("h2", { text: title }), el("div", { class: "shimmer", "aria-hidden": "true" }, el("span"), el("span"), el("span"))];
  }

  /* ── cards ──────────────────────────────────────────────────────────────── */
  function renderHead(d, snap) {
    const name = [d.name, d.sector, d.industry].filter(Boolean).join(" · ");
    $("#rh-name").textContent = name || d.ticker;
    const mc = bigMoney(d.market_cap);
    put($("#rh-price"), el("span", { class: "px", text: money(d.price, d.currency) }), mc ? el("span", { class: "muted small", text: ` mkt cap ${mc}` }) : null);
    const badges = [];
    const st = d.stats;
    if (st && st.history === "unavailable") {
      badges.push(el("span", { class: "badge badge-amber", title: `The 5-year PEG history could not be fetched; ${canRefresh() ? "refresh" : "search again"} to retry` }, "PEG history unavailable"));
    } else if (st && isNum(st.Dev_SD)) {
      badges.push(el("span", { class: `badge ${st.Dev_SD < 0 ? "badge-green" : "badge-red"}`, title: "Today's PEG vs its 5Y history, in standard deviations" }, `${st.Dev_SD > 0 ? "+" : ""}${st.Dev_SD.toFixed(2)} SD`));
    }
    if (d.flagged) badges.push(el("span", { class: "badge badge-amber", title: "Risk flag (*): growth > 99%, PEG ≥ 2.5, no SD, no trailing PE or base ROI < 9%" }, "⚠ risk flag"));
    if (d.status === "nodata") badges.push(el("span", { class: "badge badge-dim", text: "no GARP data" }));
    if (snap.cached) badges.push(el("span", { class: "badge badge-dim", title: "Served from today's cache" }, "⚡ cached"));
    if (canRefresh()) badges.push(el("button", { type: "button", class: "btn-ghost", title: "Re-run the analysis", "aria-label": `Refresh ${d.ticker}`, onclick: () => go(d.ticker, true) }, "↻ Refresh"));
    put($("#rh-badges"), ...badges);
  }

  /** ↻ Refresh is off with the AI overview off: a cached ticker stays cached until midnight. */
  function canRefresh() {
    const f = S.app.health && S.app.health.features;
    return !f || f.refresh !== false;
  }

  function bellSVG(dev) {
    const W = 320, H = 120, base = 100, top = 14;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "bell");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `PEG sits ${fx(dev, 2)} standard deviations from its historical mean`);
    const xOf = (z) => W / 2 + (z / 3.6) * (W / 2 - 8);
    const yOf = (z) => base - Math.exp(-z * z / 2) * (base - top);
    let d = `M ${xOf(-3.6)} ${base}`;
    for (let z = -3.6; z <= 3.6001; z += 0.08) d += ` L ${xOf(z).toFixed(1)} ${yOf(z).toFixed(1)}`;
    const mk = (tag, attrs) => { const n = document.createElementNS(SVG_NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); svg.append(n); return n; };
    mk("path", { d: d + ` L ${xOf(3.6)} ${base} Z`, class: "bell-fill" });
    for (let i = -3; i <= 3; i++) mk("line", { x1: xOf(i), x2: xOf(i), y1: base, y2: yOf(i), class: "bell-sd" });
    mk("path", { d, class: "bell-glow" });
    mk("path", { d, class: "bell-line" });
    mk("line", { x1: 4, x2: W - 4, y1: base, y2: base, class: "bell-axis" });
    if (isNum(dev)) {
      const z = Math.max(-3.4, Math.min(3.4, dev));
      mk("line", { x1: xOf(z), x2: xOf(z), y1: base, y2: yOf(z), class: "bell-marker" });
      mk("circle", { cx: xOf(z), cy: yOf(z), r: 6, class: "bell-dot" });
    }
    for (const [i, lab] of [[-2, "-2σ"], [0, "mean"], [2, "+2σ"]]) {
      const t = mk("text", { x: xOf(i), y: H - 4, class: "bell-lab", "text-anchor": "middle" });
      t.textContent = lab;
    }
    return svg;
  }

  function roiBars(st) {
    const rows = [["Bull", st.Bull, "bull"], ["Base", st.Base, "base"], ["Bear", st.Bear, "bear"]];
    const max = Math.max(30, ...rows.map((r) => Math.abs(r[1] || 0)));
    return el("div", { class: "roi", role: "list", "aria-label": "Projected 5-year annualised ROI" },
      rows.map(([lab, v, cls]) => el("div", { class: "roi-row", role: "listitem" },
        el("span", { class: "roi-lab", text: lab }),
        el("span", { class: "roi-track" }, el("span", { class: `roi-bar ${cls}${isNum(v) && v < 0 ? " neg" : ""}`, style: null, "data-w": isNum(v) ? Math.min(100, Math.abs(v) / max * 100).toFixed(1) : "0" })),
        el("span", { class: "roi-val", text: signed(v, 1) }))));
  }

  function statTable(pairs) {  // values may be strings or nodes
    return el("dl", { class: "mono-table" }, pairs.map(([k, v, cls]) => [el("dt", { text: k }), el("dd", { class: cls || null }, v)]));
  }

  /** "Enriched" / "Not enriched" tag in front of the 5Y growth value. */
  function growthTag(enriched) {
    if (typeof enriched !== "boolean") return null;
    return el("span", { class: `growth-tag ${enriched ? "tag-on" : "tag-off"}` }, enriched ? "Enriched" : "Not enriched");
  }

  function renderValuation(d) {
    const card = $("#card-valuation");
    const st = d.stats;
    if (!st) {
      put(card, el("h2", { text: "Valuation" }),
        el("p", { class: "muted", text: d.reason ? `No GARP valuation: ${d.reason.replace(/^no GARP data \(|\)$/g, "")}.` : "No GARP valuation available." }),
        el("p", { class: "muted small", text: "Grades, technicals and the 6M edge below still apply." }));
      return;
    }
    put(card, 
      el("h2", { text: "Valuation (PEG)" }),
      el("div", { class: "peg-hero" },
        el("div", {},
          el("div", { class: "peg-num", text: fx(st.PEG, 2) }),
          el("div", { class: "peg-sub muted", text: `hist. mean ${fx(st.Mean, 2)} · σ ${fx(st.SD, 2)}` })),
        st.history === "unavailable"
          ? el("p", { class: "hint-box", text: `PEG history unavailable (Yahoo/SEC data outage) — Mean/SD are placeholders. ${canRefresh() ? "Use ↻ Refresh" : "Search the ticker again"} to retry.` })
          : bellSVG(st.Dev_SD)),
      statTable([
        ["PE", fx(st.PE)], ["Fwd PE", fx(st.FwdPE)], ["2Y Fwd PE", fx(st["2YFwd"])],
        ["5Y Growth", [growthTag(d.growth_enriched), " ", st.display["5YGrowth"]]],
        ["Dev (SD)", fx(st.Dev_SD, 2), st.Dev_SD < 0 ? "green" : "red"],
      ]),
      el("h3", { class: "sub-h", text: "5Y ROI projection" }),
      roiBars(st));
    // widths via CSSOM (CSP forbids inline style attributes)
    requestAnimationFrame(() => card.querySelectorAll(".roi-bar").forEach((b) => { b.style.width = `${b.dataset.w}%`; }));
  }

  function renderPlot(d) {
    const fig = $("#card-plot");
    if (!d.plot_preview_url) {
      put(fig, el("h2", { text: "PEG deviation chart" }), el("p", { class: "muted", text: d.stages && d.stages.plot === "error" ? "Chart rendering failed." : "No chart — the PEG distribution needs GARP data." }));
      return;
    }
    const alt = `${d.ticker} PEG valuation deviation chart: bell curve of the 5-year PEG history with today's position, stats, income grade and credit rating`;
    put(fig, 
      el("button", { type: "button", class: "plot-btn", "aria-label": `Enlarge ${d.ticker} chart`, onclick: () => openLightbox(d.plot_url, alt) },
        el("img", { src: d.plot_preview_url, alt, width: 1568, height: 915, decoding: "async" })),
      el("figcaption", { class: "muted small" }, "Tap to enlarge · ", downloadLink(d.plot_url, `${d.ticker}_valuation.png`, alt)));
  }

  function renderIncome(d) {
    const card = $("#card-income");
    const inc = d.income;
    if (!inc) { put(card, el("h2", { text: "Income statement" }), el("p", { class: "muted", text: "Income statement data unavailable." })); return; }
    const mark = { good: ["✓", "green", "healthy vs revenue"], neutral: ["~", "sky", "in line"], bad: ["✗", "red", "bloating vs revenue"], na: [" ", "muted", "n/a"] };
    put(card, 
      el("div", { class: "card-title-row" }, el("h2", { text: "Income grade" }), el("span", { class: "grade", text: inc.grade || "N/A" })),
      el("table", { class: "mono-grid" },
        el("caption", { class: "sr-only", text: "Year-over-year growth per income statement line" }),
        el("tbody", {}, inc.items.filter((i) => isNum(i.growth)).map((i) => {
          const [m, cls, desc] = mark[i.signal] || mark.na;
          return el("tr", {}, el("td", { class: cls, "aria-label": desc, text: m }), el("th", { scope: "row", text: i.label }), el("td", { class: "num", text: signed(i.growth * 100) }));
        }))));
  }

  function renderCredit(d) {
    const card = $("#card-credit");
    const c = d.credit;
    if (!c) { put(card, el("h2", { text: "Balance sheet" }), el("p", { class: "muted", text: "Balance sheet data unavailable." })); return; }
    const hints = { "IntCov": "Operating income / interest", "ND/EBITDA": "Net debt / EBITDA", "Cash/Debt": "Cash / total debt", "Svc/FCF%": "Interest / free cash flow" };
    put(card, 
      el("div", { class: "card-title-row" }, el("h2", { text: "Credit rating" }), el("span", { class: "grade", text: c.rating || "NR" })),
      el("dl", { class: "mono-table" }, c.metrics.map((m) => [el("dt", { title: hints[m.label] || "", text: m.label }),
        el("dd", { text: isNum(m.value) ? (Math.abs(m.value) < 100 ? m.value.toFixed(1) : m.value.toFixed(0)) : "N/A" })])),
      el("p", { class: "muted small", text: "Synthetic rating (Damodaran interest-coverage method, notched for leverage & liquidity)." }));
  }

  function renderTech(d) {
    const card = $("#card-tech");
    const t = d.technicals, e = d.edge;
    const parts = [el("div", { class: "card-title-row" }, el("h2", { text: "Technicals" }),
      t ? el("span", { class: `grade sig-${String(t.signal).toLowerCase()}`, text: t.signal }) : null)];
    if (t) {
      const z = t.accumulation_zone;
      parts.push(statTable([["RSI (14)", fx(t.rsi, 0)], ["vs SMA200", signed(t.price_vs_sma200, 1)], ["ATR compr.", fx(t.atr_compression, 2)],
        ["Accum. zone", z && isNum(z[0]) ? `$${Math.round(z[0])}–$${Math.round(z[1])}` : "N/A"]]));
    } else parts.push(el("p", { class: "muted", text: "Price history unavailable." }));
    const lv = d.levels;
    if (lv) {
      const px = (v) => (isNum(v) ? `$${v.toFixed(2)}` : "N/A");
      const lvl = (x) => [px(x.price), isNum(x.p_touch_1m) ? el("span", { class: "muted small", text: ` ${x.p_touch_1m.toFixed(0)}%` }) : null];
      const rows = [];
      const res = lv.resistance || [], sup = lv.support || [];
      if (!res.length) rows.push(["R", el("span", { class: "muted", text: "none nearby" })]);
      res.slice().reverse().forEach((x, i, a) => rows.push([`R${a.length - i}`, lvl(x), "red"]));
      rows.push(["Now", px(lv.price)]);
      sup.forEach((x, i) => rows.push([`S${i + 1}`, lvl(x), "green"]));
      if (!sup.length) rows.push(["S", el("span", { class: "muted", text: "none nearby" })]);
      if (isNum(lv.poc)) rows.push(["POC (3M)", px(lv.poc)]);
      const m = lv.ranges && lv.ranges["1m"];
      if (m) rows.push(["1M range ±1σ", `${px(m["1sigma_lower"])}–${px(m["1sigma_upper"])}`]);
      rows.push(["52W range", `${px(lv.low_52w)}–${px(lv.high_52w)}`]);
      parts.push(el("h3", { class: "sub-h", title: "Support / resistance from clustered pivots (6M), volume point of control (3M), expected range from realised volatility. % = chance of touching the level within a month." }, "Price levels · P(touch) 1M"),
        statTable(rows));
    }
    parts.push(el("h3", { class: "sub-h", text: `6M directional edge vs ${d.benchmark || "SPY"}` }));
    if (e) {
      parts.push(statTable([
        ["Bull acc.", `${fx(e.bull_acc, 0)}% (${e.bull_n})`, e.best_edge === "BULL" && e.bull_acc > 55 ? "green" : null], ["Bull P&L", signed(e.bull_pnl, 2)],
        ["Bear acc.", `${fx(e.bear_acc, 0)}% (${e.bear_n})`, e.best_edge === "BEAR" && e.bear_acc > 55 ? "red" : null], ["Bear P&L", signed(e.bear_pnl, 2)],
        ["Edge", e.best_edge || "—"]]));
      let hint = "Neither side > 55% — low conviction for options income.";
      if (e.best_edge === "BULL" && e.bull_acc > 60) hint = "💡 BULL edge → sell cash-secured puts on dips.";
      else if (e.best_edge === "BEAR" && e.bear_acc > 60) hint = "💡 BEAR edge → sell covered calls on bounces.";
      else if (Math.max(e.bull_acc || 0, e.bear_acc || 0) > 55) hint = "Edge between 55–60% — modest conviction.";
      parts.push(el("p", { class: "hint-box", text: hint }));
    } else parts.push(el("p", { class: "muted", text: "Backtest unavailable." }));
    put(card, ...parts);
  }

  /* ── Quick overview (AI off): the AI card's three sections, built by fixed rules ── */
  function renderQuick(d) {
    const card = $("#card-quick");
    if (!card) return;
    const q = d.quick;
    if (!q) { card.hidden = true; return; }
    const sec = (cls, title, ...body) => el("section", { class: `ai-sec ${cls}` }, el("h3", { text: title }), ...body);
    const flags = q.stomach_test || [];
    const verdict = { realistic: ["badge-green", "realistic"], achievable: ["badge-dim", "achievable"], stretch: ["badge-red", "stretch"] }[q.dcf_verdict];
    put(card,
      el("div", { class: "card-title-row" }, el("h2", { text: "⚡ Quick overview" }),
        el("span", { class: "chip", title: "Computed from the numbers above by fixed rules; no AI model", text: "rule-based" })),
      sec("ai-overview", "Overview", el("p", { class: "ai-text", text: q.overview })),
      q.reverse_dcf ? sec("ai-dcf", "📊 Reverse 5Y DCF",
        el("p", { class: "ai-text", text: q.reverse_dcf }),
        verdict ? el("span", { class: `badge ${verdict[0]}`, title: "How demanding the base case's assumptions are (not whether the return is good)", text: `assumptions: ${verdict[1]}` }) : null) : null,
      sec("ai-stomach", "🐻 Stomach test — why it can underperform for 5 years",
        flags.length
          ? el("ul", { class: "quick-flags" }, flags.map((f) => el("li", { class: `qf qf-${f.level}` },
              el("span", { class: `badge ${f.level === "high" ? "badge-red" : "badge-amber"}`, text: f.level === "high" ? "risk" : "watch" }), " ", f.text)))
          : el("p", { class: "ai-text", text: "No rule-based red flags. The bear case has to come from outside these numbers: competition, regulation, execution." })),
      el("p", { class: "muted small", text: "Rule-based from the quant data above (thresholds: trailing PE > 50, forward PE > 40, growth > 40%, PEG ≥ 2.5, income grade or credit rating below A, …). Not financial advice." }));
    card.hidden = false;
  }

  /* ── AI overview: typed live over Server-Sent Events ─────────────────────── */
  const AI_SECTIONS = [
    ["overview", "Overview", "ai-overview"],
    ["reverse_dcf", "📊 Reverse 5Y DCF", "ai-dcf"],
    ["stomach_test", "🐻 Stomach test — why it can underperform for 5 years", "ai-stomach"],
  ];
  const AI_FINAL = new Set(["done", "error", "unavailable"]);
  const AI_METRICS = [["ttft", "TTFT"], ["speed", "Speed"], ["tokens", "Tokens"], ["thinking", "Thinking"], ["elapsed", "Time"]];
  const nf = new Intl.NumberFormat();

  function stopAI() {
    clearTimeout(S.aiTimer);
    if (S.es) { S.es.close(); S.es = null; }
  }

  function setAIStep(state) {
    const li = document.querySelector('#stepper li[data-stage="ai"]');
    if (li) li.className = state;
  }

  function aiHead(ai) {
    const h = S.app.health && S.app.health.ai;
    const model = (ai && ai.model) || (h && h.model);
    const short = (ai && ai.model_short) || (h && h.model_short) || model;
    return el("div", { class: "card-title-row" }, el("h2", { text: "🤖 AI overview" }),
      model ? el("span", { class: "chip", title: `Local model ${model}`, text: short }) : null);
  }

  /** Live AI card: metrics bar, status line, collapsible reasoning and three typing sections. */
  function aiLiveView() {
    const card = $("#card-ai");
    const dd = {};
    const metrics = el("dl", { class: "ai-metrics", "aria-label": "Generation metrics" },
      AI_METRICS.map(([k, lab]) => el("div", { class: `aim aim-${k}` }, el("dt", { text: lab }), (dd[k] = el("dd", { text: "—" })))));
    const status = el("p", { class: "ai-status", role: "status", "aria-live": "polite" }, "Starting the local model…");
    const rText = document.createTextNode("");
    const rPre = el("pre", { class: "ai-reasoning" }, rText);
    const rSum = el("summary", { text: "🧠 Model reasoning" });
    const rBox = el("details", { class: "ai-think", hidden: true }, rSum, rPre);
    const secs = {};
    const secEls = AI_SECTIONS.map(([key, title, cls]) => {
      const p = el("p", { class: "ai-text" });
      const sec = el("section", { class: `ai-sec ${cls}`, hidden: true }, el("h3", { text: title }), p);
      secs[key] = { sec, p };
      return sec;
    });
    let head = aiHead(null);
    put(card, head, metrics, status, rBox, secEls,
      el("p", { class: "muted small", text: "AI-generated from the quant data above. Not financial advice." }));
    card.setAttribute("aria-busy", "true");
    const cur = {};
    let lastStatus = "";
    const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };

    return {
      setHead(ai) { const h = aiHead(ai); head.replaceWith(h); head = h; },
      metrics(m, reset = false) {
        if (reset) {
          for (const k of Object.keys(cur)) delete cur[k];
          setText(rSum, "🧠 Model reasoning");
        }
        Object.assign(cur, m || {});
        setText(dd.ttft, isNum(cur.ttft_s) ? `${cur.ttft_s.toFixed(1)}s` : "—");
        setText(dd.speed, isNum(cur.tok_s) ? `${cur.tok_s.toFixed(1)} tok/s` : "—");
        setText(dd.tokens, isNum(cur.tokens) && cur.tokens ? nf.format(cur.tokens) : "—");
        const off = (cur.reasoning || (S.app.health && S.app.health.ai && S.app.health.ai.reasoning)) === "off";
        setText(dd.thinking, cur.reasoning_tokens
          ? `${nf.format(cur.reasoning_tokens)} tok${isNum(cur.thinking_s) ? ` · ${cur.thinking_s.toFixed(1)}s` : ""}`
          : (off ? "off" : "—"));
        setText(dd.elapsed, isNum(cur.elapsed_s) ? `${cur.elapsed_s.toFixed(1)}s` : "—");
        if (cur.reasoning_tokens) setText(rSum, `🧠 Model reasoning · ${nf.format(cur.reasoning_tokens)} tokens`);
      },
      tick(elapsed) { if (!isNum(cur.ttft_s) && isNum(elapsed)) this.metrics({ elapsed_s: elapsed }); },  // before 1st token
      status(msg) { if (msg && msg !== lastStatus) { lastStatus = msg; status.textContent = msg; } },
      phase(ph, queuePos, note) {
        const msgs = {
          queued: `Waiting for the local model — position ${queuePos || 1}…`,
          connecting: note ? `${note[0].toUpperCase()}${note.slice(1)}…` : "Processing the prompt…",
          thinking: "🧠 Thinking…",
          writing: "✍️ Writing…",
          reset: note ? `${note[0].toUpperCase()}${note.slice(1)}…` : "Retrying…",
        };
        this.status(msgs[ph]);
      },
      clear() {
        rText.data = "";
        rBox.hidden = true;
        for (const { sec, p } of Object.values(secs)) { p.textContent = ""; p.classList.remove("typing"); sec.hidden = true; }
      },
      reasoning(txt, replace = false) {
        if (replace) rText.data = txt || "";
        else if (txt) rText.appendData(txt);
        rBox.hidden = !rText.data;
        if (rBox.open && rPre.scrollHeight - rPre.scrollTop - rPre.clientHeight < 40) rPre.scrollTop = rPre.scrollHeight;
      },
      sections(sec, typing) {
        let last = null;
        for (const [key] of AI_SECTIONS) {
          const text = (sec && sec[key]) || "";
          const { sec: box, p } = secs[key];
          setText(p, text);
          box.hidden = !text;
          p.classList.remove("typing");
          if (text) last = key;
        }
        if (typing && last) secs[last].p.classList.add("typing");  // caret on the paragraph being written
      },
      finish() {
        for (const { p } of Object.values(secs)) p.classList.remove("typing");
        card.setAttribute("aria-busy", "false");
      },
    };
  }

  function renderAIError(ai) {
    const card = $("#card-ai");
    if (!card || card.hidden) return;
    const offline = ai.status === "unavailable" && ai.reason !== "no_garp";
    const base = (S.app.health && S.app.health.ai && S.app.health.ai.base_url) || "the configured LM Studio URL";
    const retry = el("button", { type: "button", class: "btn-ghost", onclick: () => (ai.need_quant ? go(S.sym, true) : startAI(S.sym, true)) },
      ai.need_quant ? "↻ Re-run analysis" : "↻ Retry AI");
    if (/timed out/.test(ai.error || "")) { retry.disabled = true; setTimeout(() => { retry.disabled = false; }, 30000); }
    put(card, aiHead(ai),
      el("p", { class: "muted", text: offline ? `AI offline — ${ai.error || "local model unavailable"}.` : (ai.error || "AI overview unavailable.") }),
      offline ? el("p", { class: "muted small", text: `Start LM Studio's server at ${base} and load a model; the quant analysis above does not need it.` }) : null,
      retry);
    card.setAttribute("aria-busy", "false");
  }

  function finishAI(ai, V) {
    stopAI();
    if (ai.status !== "done") {
      renderAIError(ai);
      setAIStep(ai.status === "unavailable" ? "skipped" : "error");
      loadDeepDive(S.sym, S.token);
      return;
    }
    V.setHead(ai);
    V.sections(ai.narrative || {}, false);
    V.metrics(Object.assign({ reasoning: ai.reasoning }, ai.metrics || {}));
    const m = ai.metrics || {};
    const took = isNum(m.elapsed_s) ? m.elapsed_s : ai.elapsed_s;
    V.status([isNum(took) ? `Done in ${took.toFixed(1)}s` : "Done", ai.cached ? "⚡ from today's cache" : null,
      ai.complete === false ? "partial reply" : null].filter(Boolean).join(" · "));
    V.finish();
    setAIStep("done");
    loadDeepDive(S.sym, S.token);
    if (typeof refreshHealth === "function") refreshHealth();
  }

  /** JSON snapshot (initial GET / polling fallback) → live view. */
  function applyLive(V, snap) {
    const lv = snap.live || {};
    V.phase(snap.status === "queued" ? "queued" : (lv.phase || "connecting"), snap.queue_position, lv.note);
    if (lv.metrics) V.metrics(lv.metrics);
    if (lv.sections) V.sections(lv.sections, true);
  }

  function pollAI(sym, token, V) {
    const tick = async () => {
      if (token !== S.token) return;
      try {
        const snap = await getJSON(`/api/ticker/${encodeURIComponent(sym)}/ai`);
        if (token !== S.token) return;
        if (AI_FINAL.has(snap.status)) { finishAI(snap, V); return; }
        applyLive(V, snap);
      } catch (e) {
        if (token !== S.token) return;
        if (e.status !== 429) { finishAI({ status: "error", error: e.message }, V); return; }
      }
      S.aiTimer = setTimeout(tick, document.hidden ? 3000 : 1000);
    };
    tick();
  }

  function streamAI(sym, token, V) {
    if (!("EventSource" in window)) { pollAI(sym, token, V); return; }
    const es = new EventSource(`/api/ticker/${encodeURIComponent(sym)}/ai/stream`);
    S.es = es;
    const on = (type, fn) => es.addEventListener(type, (ev) => {
      if (token !== S.token) { es.close(); return; }
      fn(JSON.parse(ev.data));
    });
    on("snapshot", (d) => {  // first event of every (re)connection: replace everything
      V.clear();
      V.reasoning(d.reasoning, true);
      if (d.sections) V.sections(d.sections, true);
      V.metrics(d.metrics);
      V.phase(d.phase, d.queue_position, d.note);
    });
    on("reset", (d) => { V.clear(); V.metrics(null, true); V.phase("reset", null, d.note); });
    on("delta", (d) => {
      V.reasoning(d.reasoning);
      if ("sections" in d) V.sections(d.sections, true);  // null = answer moved back to reasoning
      V.metrics(d.metrics);
      V.phase(d.phase, null, d.note);
    });
    on("state", (d) => {
      if (d.status === "queued") V.phase("queued", d.queue_position);
      else V.tick(d.elapsed_s);
    });
    for (const t of AI_FINAL) on(t, (d) => finishAI(d, V));
    es.onerror = () => {
      if (token !== S.token) { es.close(); return; }
      if (es.readyState === EventSource.CLOSED) {  // e.g. 404: the job finished between GET and connect
        if (S.es === es) S.es = null;
        pollAI(sym, token, V);
      }  // CONNECTING: the browser reconnects by itself and the server replays a snapshot
    };
  }

  async function startAI(sym, retry = false) {
    if (!S.app.health || !S.app.health.features.ai) return;
    stopAI();
    const token = S.token;
    setAIStep("running");
    const V = aiLiveView();
    let snap;
    try {
      snap = await getJSON(`/api/ticker/${encodeURIComponent(sym)}/ai${retry ? "?refresh=1" : ""}`);
    } catch (e) {
      if (token !== S.token) return;
      if (e.status === 429) {
        V.status(`${e.message} — retrying shortly…`);
        S.aiTimer = setTimeout(() => startAI(sym, retry), (e.retryAfter || 15) * 1000);
        return;
      }
      finishAI({ status: "error", error: e.message }, V);
      return;
    }
    if (token !== S.token) return;
    if (AI_FINAL.has(snap.status)) { finishAI(snap, V); return; }
    applyLive(V, snap);
    loadDeepDive(sym, token);  // quant data now; refreshed with the AI overview when it is done
    streamAI(sym, token, V);
  }

  /* ── Deep Dive Prompt ───────────────────────────────────────────────────── */
  const DD = { text: "" };

  async function loadDeepDive(sym, token) {
    try {
      const dd = await getJSON(`/api/ticker/${encodeURIComponent(sym)}/deepdive`);
      if (token !== S.token) return;
      DD.text = dd.prompt;
      $("#dd-text").textContent = dd.prompt;
      const ai = { included: "AI overview included", pending: "AI overview still being written; it is added when done",
        disabled: "no AI overview (disabled)", unavailable: "no AI overview" }[dd.ai] || "";
      $("#dd-meta").textContent = `A research brief for Claude, ChatGPT or Gemini: paste it into a chat with web search on. ` +
        `${dd.words.toLocaleString()} words · ${ai}.`;
      $("#deepdive").hidden = false;
    } catch (_) {
      if (token === S.token) $("#deepdive").hidden = true;
    }
  }

  function hideDeepDive() {
    DD.text = "";
    $("#deepdive").hidden = true;
  }

  function ddStatus(msg) {
    const s = $("#dd-status");
    s.textContent = "";
    requestAnimationFrame(() => { s.textContent = msg; });
  }

  /** Clipboard API needs a secure context (https / localhost); LAN and Tailscale URLs are plain http. */
  async function copyText(text) {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall back */ }
    }
    const ta = el("textarea", { readonly: true, class: "dd-clip", "aria-hidden": "true", tabindex: "-1" });
    ta.value = text;
    document.body.append(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);  // iOS Safari
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }

  function setToggle(show) {
    const pre = $("#dd-text"), btn = $("#dd-toggle");
    pre.hidden = !show;
    btn.textContent = show ? "Hide" : "Show";
    btn.setAttribute("aria-expanded", String(show));
  }

  function initDeepDive() {
    const copyBtn = $("#dd-copy");
    copyBtn.addEventListener("click", async () => {
      if (!DD.text) return;
      const ok = await copyText(DD.text);
      if (ok) {
        copyBtn.textContent = "Copied ✓";
        ddStatus("Deep dive prompt copied to the clipboard.");
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
      } else {  // show it selected so a long-press / Ctrl+C copies it
        setToggle(true);
        const range = document.createRange();
        range.selectNodeContents($("#dd-text"));
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        ddStatus("Copy is blocked by this browser: the prompt is selected, copy it manually.");
      }
    });
    $("#dd-toggle").addEventListener("click", () => setToggle($("#dd-text").hidden));
  }

  /* ── polling state machine ──────────────────────────────────────────────── */
  const CARD_FOR = { stats: [renderValuation], grades: [renderIncome, renderCredit], technicals: [], edge: [renderTech], plot: [renderPlot] };

  function applySnapshot(snap) {
    S.lastSnap = snap;
    const d = snap.data || {};
    const stages = d.stages || {};
    for (const [name, state] of Object.entries(stages)) {
      const li = document.querySelector(`#stepper li[data-stage="${name}"]`);
      if (li && li.className !== state) li.className = state;
    }
    if (!S.rendered.has("head") && (d.name || d.status === "nodata" || d.price !== undefined)) { renderHead(d, snap); S.rendered.add("head"); }
    for (const [stage, fns] of Object.entries(CARD_FOR)) {
      const st = stages[stage];
      if (S.rendered.has(stage) || !["done", "error", "skipped"].includes(st)) continue;
      fns.forEach((f) => f(d));
      S.rendered.add(stage);
    }
    // technicals card combines two stages; render once both are settled
    const line = $("#status-line");
    let msg;
    if (snap.status === "queued") msg = snap.note ? `${snap.note}…` : `Queued — position ${snap.queue_position || 1}…`;
    else if (snap.status === "running") msg = `Running ${STAGE_LABELS[snap.stage] || "analysis"}… ${Math.round(snap.elapsed_s || 0)}s`;
    else if (snap.status === "done") msg = snap.cached ? "Loaded from today's cache ⚡" : `Analysis complete in ${Math.round(snap.elapsed_s || 0)}s`;
    else if (snap.status === "nodata") msg = `No GARP valuation for ${snap.ticker}: ${d.reason || "insufficient data"}`;
    else msg = `Analysis failed: ${snap.error || d.reason || "unknown error"}`;
    if (line.textContent !== msg) line.textContent = msg;

    const final = ["done", "nodata", "error"].includes(snap.status);
    if (final) {
      renderHead(d, snap);  // refresh badges (cached / flagged) once
      for (const [stage, fns] of Object.entries(CARD_FOR)) if (!S.rendered.has(stage)) { fns.forEach((f) => f(d)); S.rendered.add(stage); }
      $("#result").setAttribute("aria-busy", "false");
      if (typeof refreshHealth === "function") refreshHealth();  // cache chip reflects this lookup now
      if (snap.status === "error" && !d.name) $("#rh-name").textContent = "—";
      if (!(S.app.health && S.app.health.features.ai)) renderQuick(d);
      if (snap.status === "done" && S.app.health && S.app.health.features.ai) startAI(snap.ticker);
      else {
        setAIStep("skipped");
        const c = $("#card-ai"); if (c) c.hidden = true;  // AI needs GARP data
        if (snap.status !== "error") loadDeepDive(snap.ticker, S.token);
      }
    }
    return final;
  }

  function countdown(token, secs, msg, then) {
    if (token !== S.token) return;
    if (secs <= 0) { then(); return; }
    $("#status-line").textContent = `${msg} Retrying in ${secs}s…`;
    S.timer = setTimeout(() => countdown(token, secs - 1, msg, then), 1000);
  }

  async function poll(token, sym, refresh) {
    if (token !== S.token) return;
    if (document.hidden) { S.timer = setTimeout(() => poll(token, sym, refresh), 1000); return; }
    let delay = Date.now() - S.started > 10000 ? 2000 : 1000;
    try {
      // first request = a lookup (counts in the LFU); follow-ups are polls of that same lookup
      const q = refresh ? "?refresh=1" : (S.polled ? "?poll=1" : "");
      const snap = await getJSON(`/api/ticker/${encodeURIComponent(sym)}${q}`);
      S.polled = true;
      if (token !== S.token) return;
      if (snap.status === "expired") {  // our lookup vanished (midnight / eviction): look it up again
        S.polled = false;
        S.timer = setTimeout(() => poll(token, sym, false), 500);
        return;
      }
      if (applySnapshot(snap)) return;
    } catch (e) {
      if (token !== S.token) return;
      if (e.status === 429) {  // queue full: keep the refresh intent until a request is accepted
        countdown(token, e.retryAfter || 10, e.message, () => poll(token, sym, refresh));
        return;
      } else if (e.status === 400) {
        $("#status-line").textContent = `“${sym}” is not a valid ticker symbol.`;
        $("#result").setAttribute("aria-busy", "false");
        return;
      } else {
        $("#status-line").textContent = `Connection problem (${e.message}) — retrying…`;
        delay = 3000;
      }
    }
    S.timer = setTimeout(() => poll(token, sym, false), delay);
  }

  function go(raw, refresh = false, push = true) {
    const sym = String(raw || "").trim().toUpperCase().replace(/^\$/, "");
    const input = $("#q");
    if (!TICKER_RE.test(sym)) {
      input.setCustomValidity("Enter a ticker symbol like MSFT or BRK.B");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    input.value = sym;
    S.token += 1;
    clearTimeout(S.timer);
    stopAI();
    hideDeepDive();
    S.sym = sym;
    S.started = Date.now();
    S.rendered = new Set();
    S.polled = false;
    skeleton(sym);
    pushRecent(sym);
    if (push) {
      const url = `?t=${encodeURIComponent(sym)}`;
      if (location.search !== url) history.pushState({ t: sym }, "", url);
    }
    document.title = `$${sym} · The Lynch Pin`;
    if (window.matchMedia("(max-width: 700px)").matches) input.blur();  // drop the phone keyboard
    $("#result").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    poll(S.token, sym, refresh);
  }

  function fromURL() {
    const t = new URLSearchParams(location.search).get("t");
    if (t && TICKER_RE.test(t.toUpperCase())) go(t, false, false);
    else {
      S.token += 1;
      clearTimeout(S.timer);
      stopAI();
      hideDeepDive();
      $("#result").hidden = true;
      document.title = "The Lynch Pin · Quant Portal";
    }
  }

  window.LynchSearch = {
    init(appState) {
      S.app = appState;
      if (!appState.health || !appState.health.features.search) return;
      initDeepDive();
      renderRecent();
      $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); go($("#q").value); });
      $("#q").addEventListener("input", (e) => e.target.setCustomValidity(""));
      window.addEventListener("popstate", fromURL);
      document.addEventListener("keydown", (e) => {
        if (e.key === "/" && document.activeElement !== $("#q") && !e.ctrlKey && !e.metaKey) { e.preventDefault(); $("#q").focus(); }
      });
      fromURL();
    },
    _test: { applySnapshot, go },
  };
})();
