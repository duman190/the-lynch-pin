/* The Lynch Pin · Quant Portal — Portfolio mode. A switch over the search box turns the ticker field into
   position rows, a ticker box and an amount box each (% weights adding up to 100, or dollars / shares), with +
   to add a row; Analyze opens the portfolio page, /?pf=MSFT:40,AAPL:35,KO:25&m=pct, like a ticker's /?t=. The page is main.py's
   portfolio X-ray: each position is looked up like a ticker search (today's cached analysis when there is one),
   then /api/portfolio rolls them up (weighted valuation, growth, ROI and quality, the weight donut), the AI
   verdict comes in with its metrics (TTFT, speed, total time) and the Portfolio Deep Dive Prompt follows.
   Depends on app.js helpers ($, el, getJSON, openLightbox, navigate, currentView) and LynchSearch.copyText. */
"use strict";

(() => {
  const STORE_KEY = "lynchpin.portfolio.v2";
  const OLD_KEY = "lynchpin.portfolio";  // the first Portfolio Analyzer's rows
  const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
  const MAX_POSITIONS = 25;
  const AI_FINAL = new Set(["done", "error", "unavailable"]);
  const AI_METRICS = [["ttft", "TTFT"], ["speed", "Speed"], ["tokens", "Tokens"], ["thinking", "Thinking"], ["elapsed", "Time"]];
  const DEFAULT_TITLE = "The Lynch Pin · Quant Portal";
  const P = {
    app: null,
    on: false,     // the switch as saved: home shows it, a ticker page opens with it off, a portfolio page on
    mode: "pct",   // "pct" | "amt": what a bare number means
    rows: [],      // {sym, value, unit: "usd" | "sh"} as typed
    view: null,
    spec: null,    // the portfolio on the page ("SYM:VALUE,..." as in the URL)
    token: 0,      // the run in progress: a newer page cancels older polls
    timer: null,
    aiTimer: null,
    ai: null,      // the AI card's view
    dd: "",        // the deep dive prompt's text
  };

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const fx = (v, d = 1, suf = "") => (isNum(v) ? v.toFixed(d) + suf : "N/A");
  const signed = (v, d = 1, suf = "%") => (isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(d) + suf : "N/A");
  const pct = (w) => (isNum(w) ? (w * 100).toFixed(1) + "%" : "—");
  const nf = new Intl.NumberFormat();
  const verbose = () => !!(P.app && P.app.health && P.app.health.verbose);
  const num = (v) => String(+v.toFixed(6));
  const plural = (n) => `${n} position${n === 1 ? "" : "s"}`;

  /* ── saved switch, unit and positions (this browser only) ────────────────── */
  const blank = () => ({ sym: "", value: "", unit: "usd" });
  const cleanRows = (rows) => (Array.isArray(rows) ? rows : []).slice(0, MAX_POSITIONS).map((r) => ({
    sym: String((r && r.sym) || "").toUpperCase().slice(0, 10), value: String((r && r.value) || "").slice(0, 16),
    unit: r && r.unit === "sh" ? "sh" : "usd" }));
  function load() {
    try {
      const s = JSON.parse(localStorage.getItem(STORE_KEY) || "null") || JSON.parse(localStorage.getItem(OLD_KEY) || "null");
      if (s) {
        P.on = !!s.on;
        P.mode = s.mode === "amt" ? "amt" : "pct";
        P.rows = cleanRows(s.rows);
      }
    } catch (_) { /* private window / blocked storage: start empty */ }
    while (P.rows.length < 2) P.rows.push(blank());
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ on: P.on, mode: P.mode, rows: P.rows })); } catch (_) { /* ignore */ }
  }

  /* ── reading the rows ────────────────────────────────────────────────────── */

  /** The rows as typed → {ok, empty, message, positions: [{sym, value, unit: pct|usd|sh}], total, spec}. */
  function validate(rows, mode) {
    rows = rows.filter((r) => r.sym.trim() || String(r.value).trim());
    if (!rows.length) return { ok: false, empty: true };
    const bySym = new Map();
    for (const r of rows) {
      const sym = r.sym.trim().toUpperCase();
      const value = parseFloat(String(r.value).replace(/[,$%\s]/g, ""));
      const unit = mode === "pct" ? "pct" : r.unit === "sh" ? "sh" : "usd";
      if (!sym) return { ok: false, message: "A position is missing its ticker." };
      if (!TICKER_RE.test(sym)) return { ok: false, message: `“${sym}” is not a ticker.` };
      if (!(value > 0)) return { ok: false, message: `${sym}: enter a positive ${mode === "pct" ? "weight" : unit === "sh" ? "number of shares" : "dollar amount"}.` };
      const prev = bySym.get(sym);
      if (prev && prev.unit !== unit) return { ok: false, message: `${sym} is listed twice with different units.` };
      bySym.set(sym, { sym, unit, value: (prev ? prev.value : 0) + value });
    }
    const positions = [...bySym.values()];
    const total = positions.reduce((s, p) => s + p.value, 0);
    if (mode === "pct" && Math.abs(total - 100) > 0.5) {
      return { ok: false, positions, total, message: `Weights add up to ${+total.toFixed(2)}% — they need to add up to 100%.` };
    }
    const spec = positions.map((p) => `${p.sym}:${p.unit === "pct" ? num(p.value) : p.unit === "sh" ? num(p.value) + "sh" : "$" + num(p.value)}`).join(",");
    return { ok: true, positions, total, spec };
  }

  /** The URL's spec ("MSFT:40,AAPL:$5000,KO:10sh") → positions, or null. The server checks it again. */
  function fromSpec(spec, mode) {
    const out = [];
    for (const item of String(spec || "").split(",")) {
      const m = /^([A-Za-z][A-Za-z0-9.\-]{0,9}):(\$)?(\d+(?:\.\d+)?|\.\d+)(sh)?%?$/i.exec(item.trim());
      if (!m) return null;
      const unit = m[4] ? "sh" : m[2] ? "usd" : "pct";
      if ((mode === "pct") !== (unit === "pct")) return null;
      out.push({ sym: m[1].toUpperCase(), value: parseFloat(m[3]), unit });
    }
    return out.length && out.length <= MAX_POSITIONS ? out : null;
  }
  const fmtPos = (p) => (p.unit === "pct" ? `${num(p.value)}%` : p.unit === "sh" ? `${num(p.value)} sh` : `$${nf.format(+p.value.toFixed(2))}`);
  /** A readable URL: only what a query value cannot hold is escaped (the spec is tickers, numbers, $ : and ,). */
  const pageURL = (spec, mode) => `/?pf=${spec.replace(/[^A-Za-z0-9.\-:,$]/g, encodeURIComponent)}&m=${mode}`;

  /* ── the switch and the rows ─────────────────────────────────────────────── */
  /** Shows the ticker search or the position rows. Saved only when ``remember`` (a click on the switch). */
  function setSwitch(on, remember) {
    if (remember) { P.on = on; save(); }
    const sw = $("#pf-switch");
    sw.setAttribute("aria-checked", String(on));
    $(".pf-switch-state", sw).textContent = on ? "On" : "Off";
    $("#search-form").hidden = on;
    $("#pf-form").hidden = !on;
    $("#pf-mode").hidden = !on;
    $("#search-hint").hidden = on;
    $("#recent").hidden = on;
    if (on) { $("#suggest").hidden = true; renderRows(); }
  }

  function setMode(mode) {
    P.mode = mode;
    for (const b of document.querySelectorAll("#pf-mode button")) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  }

  const SAMPLE = [["MSFT", "40", "5000"], ["AAPL", "35", "10"], ["KO", "25", "2500"]];
  function rowView(r, i) {
    const ex = SAMPLE[i % SAMPLE.length];
    const sym = el("input", { type: "text", class: "pf-sym", value: r.sym, maxlength: 10, placeholder: ex[0],
      autocapitalize: "characters", autocorrect: "off", spellcheck: "false", enterkeyhint: "go",
      "aria-label": `Position ${i + 1} ticker` });
    sym.addEventListener("input", () => { r.sym = sym.value.toUpperCase(); save(); renderHint(); });
    const what = P.mode === "pct" ? "weight in percent" : r.unit === "sh" ? "number of shares" : "dollars";
    const val = el("input", { type: "text", inputmode: "decimal", class: "pf-val", value: r.value, maxlength: 16,
      placeholder: P.mode === "pct" ? ex[1] : r.unit === "sh" ? "10" : ex[2], enterkeyhint: "go",
      "aria-label": `Position ${i + 1} ${what}` });
    val.addEventListener("input", () => { r.value = val.value; save(); renderHint(); });
    let unit;
    if (P.mode === "pct") {
      unit = el("span", { class: "pf-unit", "aria-hidden": "true" }, "%");
    } else {
      unit = el("select", { class: "pf-unit-sel", "aria-label": `Position ${i + 1} unit` },
        el("option", { value: "usd", selected: r.unit !== "sh" }, "$"),
        el("option", { value: "sh", selected: r.unit === "sh" }, "shares"));
      unit.addEventListener("change", () => { r.unit = unit.value; save(); renderRows(); });
    }
    const del = el("button", { type: "button", class: "pf-del", "aria-label": `Remove position ${i + 1}`, title: "Remove",
      onclick: () => {
        P.rows.splice(i, 1);
        if (!P.rows.length) P.rows.push(blank());
        save();
        renderRows();
        const syms = document.querySelectorAll("#pf-rows .pf-sym");
        syms[Math.min(i, syms.length - 1)].focus();
      } }, "×");
    return el("li", { class: "pf-row" }, el("span", { class: "search-icon", "aria-hidden": "true" }, "$"), sym, val, unit, del);
  }

  function renderRows() {
    $("#pf-rows").replaceChildren(...P.rows.map(rowView));
    $("#pf-add").disabled = P.rows.length >= MAX_POSITIONS;
    renderHint();
  }

  function addRow() {
    if (P.rows.length >= MAX_POSITIONS) return;
    P.rows.push(blank());
    save();
    renderRows();
    const syms = document.querySelectorAll("#pf-rows .pf-sym");
    syms[syms.length - 1].focus();
  }

  function renderHint(showError = false) {
    const hint = $("#pf-hint");
    const v = validate(P.rows, P.mode);
    let msg, cls = "";
    if (v.ok) {
      msg = P.mode === "pct" ? `${plural(v.positions.length)} · total ${+v.total.toFixed(2)}% ✓`
        : `${plural(v.positions.length)} · weighted by market value (shares at today’s price)`;
      cls = "green";
    } else if (v.empty) {
      msg = P.mode === "pct" ? "A ticker and its % weight per row, adding up to 100%. + adds a row."
        : "A ticker and its dollars or shares per row; only the resulting weights are used. + adds a row.";
      if (showError) cls = "red";
    } else if (v.positions && !showError) {
      msg = `${plural(v.positions.length)} · total ${+v.total.toFixed(2)}% of 100%`;
    } else {
      msg = v.message;
      if (showError) cls = "red";
    }
    if (hint.textContent !== msg) hint.textContent = msg;
    hint.className = `hint pf-hint ${cls}`.trim();
  }

  function submit() {
    const v = validate(P.rows, P.mode);
    if (!v.ok) {
      renderHint(true);
      const bad = [...document.querySelectorAll("#pf-rows .pf-row")].find((li) => !$(".pf-sym", li).value.trim() || !$(".pf-val", li).value.trim());
      if (bad) (!$(".pf-sym", bad).value.trim() ? $(".pf-sym", bad) : $(".pf-val", bad)).focus();
      return;
    }
    const a = document.activeElement;
    if (a && a.closest && a.closest("#pf-rows") && window.matchMedia("(max-width: 700px)").matches) a.blur();  // drop the phone keyboard
    P.spec = null;  // Analyze again on the same portfolio runs it afresh
    navigate(pageURL(v.spec, P.mode));
    scrollToPage();
  }

  function scrollToPage() {
    const bar = document.querySelector(".topbar");
    const top = $("#portfolio-page").getBoundingClientRect().top + window.scrollY - (bar ? bar.getBoundingClientRect().height : 0) - 12;
    window.scrollTo({ top: Math.max(0, top), behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  /* ── the page: /?pf=...&m=... ────────────────────────────────────────────── */
  function stop() {
    P.token++;
    clearTimeout(P.timer);
    clearTimeout(P.aiTimer);
  }

  function fromURL() {
    const view = currentView();
    const entered = view !== P.view;
    P.view = view;
    if (view !== "portfolio") {
      if (P.spec !== null) { stop(); P.spec = null; hideDeepDive(); }
      if (entered) setSwitch(view === "ticker" ? false : P.on, false);
      return;
    }
    const q = new URLSearchParams(location.search);
    const spec = q.get("pf") || "";
    const mode = q.get("m") === "amt" ? "amt" : "pct";
    if (entered) setSwitch(true, false);
    $("#portfolio-page").hidden = false;  // (outside this view the CSS hides it)
    if (spec === P.spec) return;  // Back/Forward onto the page already on screen
    P.spec = spec;
    const positions = fromSpec(spec, mode);
    setMode(mode);
    if (positions) {
      P.rows = positions.map((p) => ({ sym: p.sym, value: num(p.value), unit: p.unit === "sh" ? "sh" : "usd" }));
      save();
    }
    renderRows();
    const syms = (positions || []).map((p) => "$" + p.sym);
    document.title = syms.length ? `💼 ${syms.slice(0, 4).join(" ")}${syms.length > 4 ? " …" : ""} · The Lynch Pin` : DEFAULT_TITLE;
    $("#pf-mode-tag").textContent = mode === "pct" ? "% weights" : "$ / shares · weighted by market value";
    $("#pf-chips").replaceChildren(...(positions || []).map((p) => el("li", {}, el("b", {}, p.sym), " ", fmtPos(p))));
    hideDeepDive();
    analyze(`m=${mode}&p=${encodeURIComponent(spec)}`);
  }

  function status(...kids) {
    $("#pf-result").replaceChildren(el("div", { class: "pf-status", role: "status" }, ...kids));
  }

  function analyze(query) {
    stop();
    const token = P.token;
    status(el("strong", {}, "Looking up the positions…"));
    const tick = async (poll) => {
      if (token !== P.token) return;
      let snap;
      try {
        snap = await getJSON(`/api/portfolio?${query}${poll ? "&poll=1" : ""}`);
      } catch (e) {
        if (token !== P.token) return;
        if (e.status === 429 || e.status >= 500) { P.timer = setTimeout(() => tick(true), (e.retryAfter || 5) * 1000); return; }
        status(el("strong", { class: "red" }, e.message || "Analysis failed."));
        return;
      }
      if (token !== P.token) return;
      if (snap.status === "running") {
        renderProgress(snap);
        P.timer = setTimeout(() => tick(true), Math.max(1500, (snap.retry_after || 0) * 1000));
        return;
      }
      if (snap.status === "done") return renderResult(snap, token);
      renderFailure(snap);
    };
    tick(false);
  }

  const STAGES = { stats: "valuation", grades: "grades", technicals: "technicals", edge: "6M edge", plot: "chart" };
  function renderProgress(snap) {
    const label = (p) => p.status === "queued" ? (p.queue_position ? `queued #${p.queue_position}` : "queued")
      : p.status === "running" ? (STAGES[p.stage] || "analysing") + "…"
      : p.status === "busy" ? "waiting" : p.status === "done" ? "✓" : p.status === "nodata" ? "no GARP data" : p.status;
    status(
      el("strong", {}, `Analysing ${snap.done} of ${snap.total} positions…`),
      el("ul", { class: "pf-progress" }, snap.positions.map((p) => el("li", { class: p.status },
        el("span", { class: "pf-psym" }, p.ticker), el("span", { class: "muted small" }, label(p))))),
      snap.positions.some((p) => p.note) ? el("p", { class: "muted small" }, snap.positions.find((p) => p.note).note) : null);
  }

  function renderFailure(snap) {
    const kids = [el("strong", { class: "red" }, snap.error || "No portfolio analysis.")];
    if (snap.positions) kids.push(positionsTable(snap.positions));
    status(...kids);
  }

  function statTable(pairs) {
    return el("dl", { class: "mono-table" }, pairs.map(([k, v, cls]) => [el("dt", { text: k }), el("dd", { class: cls || null }, v)]));
  }

  function positionsTable(positions) {
    const head = ["Ticker", "Weight", "PEG", "Dev", "Base ROI", "Grade"];
    return el("div", { class: "pf-table-wrap" }, el("table", { class: "pf-table" },
      el("caption", { class: "sr-only" }, "Positions by weight"),
      el("thead", {}, el("tr", {}, head.map((h) => el("th", { scope: "col" }, h)))),
      el("tbody", {}, positions.map((p) => el("tr", { class: p.garp ? null : "pf-nogarp" },
        el("th", { scope: "row" }, el("a", { href: `/?t=${encodeURIComponent(p.ticker)}`, title: p.name,
          onclick: (e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); navigate(`/?t=${encodeURIComponent(p.ticker)}`); window.scrollTo(0, 0); } }, p.ticker)),
        el("td", {}, pct(p.weight)),
        p.garp
          ? [el("td", {}, fx(p.peg, 2)), el("td", { class: isNum(p.dev_sd) ? (p.dev_sd < 0 ? "green" : "red") : null }, fx(p.dev_sd, 2)),
             el("td", {}, signed(p.base)), el("td", {}, `${p.income || "N/A"}/${p.credit || "NR"}`)]
          : el("td", { colspan: 4, class: "muted" }, p.reason || "no GARP data"))))));
  }

  function renderResult(r, token) {
    const m = r.metrics || {};
    const cov = isNum(m.Coverage) ? `${Math.round(m.Coverage * 100)}% · ${m.Positions} of ${m.TotalPositions}` : "N/A";
    const card = el("div", { class: "pf-grid" },
      el("section", { class: "widget pf-card", "aria-labelledby": "pf-metrics-title" },
        el("h3", { class: "sub-h", id: "pf-metrics-title" }, "Weighted GARP metrics"),
        el("div", { class: "peg-hero" },
          el("div", {},
            el("div", { class: "peg-num", text: fx(m.PEG, 2) }),
            el("div", { class: "peg-sub muted", text: `weighted PEG · median ${fx(m.MedianPEG, 2)} · hist. mean ${fx(m.Mean, 2)}` })),
          el("span", { class: `pf-dev ${isNum(m.Dev_SD) ? (m.Dev_SD > 0 ? "red" : "green") : "muted"}` }, isNum(m.Dev_SD) ? `${signed(m.Dev_SD, 2, "")} SD` : "N/A")),
        statTable([
          ["PE", fx(m.PE)], ["Fwd PE", fx(m.FwdPE)], ["2Y Fwd PE", fx(m["2YFwd"])],
          ["5Y Growth", fx(m.Growth, 1, "%")],
          ["Bull ROI", signed(m.Bull), isNum(m.Bull) && m.Bull < 0 ? "red" : null],
          ["Base ROI", signed(m.Base), isNum(m.Base) && m.Base < 0 ? "red" : null],
          ["Bear ROI", signed(m.Bear), isNum(m.Bear) && m.Bear < 0 ? "red" : null],
          ["Income grade", m.IncomeGrade || "N/A"], ["Credit rating", m.CreditRating || "NR"],
          ["GARP data", cov],
        ]),
        r.chart
          ? el("button", { type: "button", class: "btn-primary btn-chart pf-chart",
              onclick: () => openLightbox(r.chart.preview, "Portfolio X-ray: market-value weight donut with the weighted PEG, weighted GARP metrics, quality and index returns", "portfolio_allocation.png", r.chart.url) }, "View chart")
          : el("p", { class: "muted small" }, "The chart could not be drawn."),
        window.LynchBacktest ? el("button", { type: "button", class: "btn-ghost btn-track pf-backtest",
          onclick: () => window.LynchBacktest.open(r.key, (r.positions || []).filter((p) => isNum(p.weight)).slice(0, 4).map((p) => "$" + p.ticker).join(" ") + ((r.positions || []).length > 4 ? " …" : "")) },
          "5Y backtest") : null),
      el("section", { class: "widget pf-card", "aria-labelledby": "pf-pos-title" },
        el("h3", { class: "sub-h", id: "pf-pos-title" }, "Positions"),
        positionsTable(r.positions || [])),
      r.ai ? el("section", { class: "widget pf-card pf-ai", id: "pf-ai", "aria-labelledby": "pf-ai-title" }) : null);
    $("#pf-result").replaceChildren(card,
      el("p", { class: "muted small pf-note" }, "Weights = market value share. PE ratios are harmonic weighted means; growth, PEG and ROI are weighted averages. Quant scans, not financial advice."));
    if (r.chart) new Image().src = r.chart.preview;  // cached by the time View chart is clicked
    if (r.ai) {
      P.ai = aiView(r.key, token);
      pollAI(r.key, token);
    }
    loadDeepDive(r.key, token);
  }

  /* ── AI overview: the verdict, with the ticker overview's metrics bar ────── */
  function aiHead(ai) {
    const h = P.app && P.app.health && P.app.health.ai;
    const model = (ai && ai.model) || (h && h.model);
    const short = (h && h.model === model && h.model_short) || model;
    return el("div", { class: "card-title-row" }, el("h3", { class: "sub-h", id: "pf-ai-title" }, "🤖 AI overview"),
      model && verbose() ? el("span", { class: "chip", title: `Model ${model}`, text: short }) : null);
  }

  function aiSection(label, text, cls) {
    return el("div", { class: `ai-sec ${cls}` }, el("h3", {}, label), el("p", { class: "ai-text" }, text));
  }

  /** The AI card: head, metrics bar (TTFT · Speed · Tokens · Thinking · Time), status line and the verdict. */
  function aiView(key, token) {
    const box = $("#pf-ai");
    const dd = {};
    const metrics = el("dl", { class: "ai-metrics", "aria-label": "Generation metrics" },
      AI_METRICS.map(([k, lab]) => el("div", { class: `aim aim-${k}` }, el("dt", { text: lab }), (dd[k] = el("dd", { text: "—" })))));
    const line = el("p", { class: "ai-status", role: "status", "aria-live": "polite" }, "Asking the model for its verdict…");
    const body = el("div", { class: "pf-ai-body" });
    let head = aiHead(null);
    box.replaceChildren(head, metrics, line, body);
    box.setAttribute("aria-busy", "true");
    const set = (node, text) => { if (node.textContent !== text) node.textContent = text; };
    return {
      update(ai) {
        const h = aiHead(ai);
        if (h.textContent !== head.textContent) { head.replaceWith(h); head = h; }
        const m = ai.metrics || {};
        set(dd.ttft, isNum(m.ttft_s) ? `${m.ttft_s.toFixed(1)}s` : "—");
        set(dd.speed, isNum(m.tok_s) ? `${m.tok_s.toFixed(1)} tok/s` : "—");
        set(dd.tokens, isNum(m.tokens) && m.tokens ? nf.format(m.tokens) : "—");
        const off = (ai.reasoning || (P.app.health && P.app.health.ai && P.app.health.ai.reasoning)) === "off";
        set(dd.thinking, m.reasoning_tokens
          ? `${nf.format(m.reasoning_tokens)} tok${isNum(m.thinking_s) ? ` · ${m.thinking_s.toFixed(1)}s` : ""}`
          : (off ? "off" : "—"));
        set(dd.elapsed, isNum(m.elapsed_s) ? `${m.elapsed_s.toFixed(1)}s` : "—");
        const live = !AI_FINAL.has(ai.status);
        const kids = [];
        if (ai.verdict) kids.push(el("p", { class: "ai-text pf-verdict" }, ai.verdict));
        if (ai.bull) kids.push(aiSection("🐂 Bull", ai.bull, "ai-bull"));
        if (ai.bear) kids.push(aiSection("🐻 Bear", ai.bear, "ai-bear"));
        if (!(ai.verdict || ai.bull || ai.bear) && ai.text) kids.push(el("p", { class: "ai-text" }, ai.text));
        if (live) {
          set(line, ai.status === "queued" ? "Waiting for the AI model…"
            : ({ thinking: "🧠 Thinking…", writing: "✍️ Writing…" })[ai.phase] || "Processing the prompt…");
          line.className = "ai-status";
        } else if (ai.status === "done") {
          set(line, isNum(m.elapsed_s) ? `Done in ${m.elapsed_s.toFixed(1)}s` : "Done");
          line.className = "ai-status";
          kids.push(el("p", { class: "muted small" }, "Peter Lynch-style verdict from the numbers above. Not financial advice."));
          box.setAttribute("aria-busy", "false");
        } else {
          set(line, ai.error || "AI overview unavailable.");
          line.className = "ai-status red";
          kids.push(el("button", { type: "button", class: "btn-ghost", onclick: () => pollAI(key, token) }, "↻ Retry AI"));
          box.setAttribute("aria-busy", "false");
        }
        body.replaceChildren(...kids);
      },
    };
  }

  function pollAI(key, token) {
    clearTimeout(P.aiTimer);
    let first = true;
    const tick = async () => {
      if (token !== P.token) return;
      let ai;
      try { ai = await getJSON(`/api/portfolio/ai?k=${encodeURIComponent(key)}`); } catch (e) {
        if (token === P.token) P.ai.update({ status: "error", error: e.message });
        return;
      }
      if (token !== P.token) return;
      P.ai.update(ai);
      if (!AI_FINAL.has(ai.status)) P.aiTimer = setTimeout(tick, 1000);
      else if (!first) loadDeepDive(key, token);  // the prompt now carries the verdict
      first = false;
    };
    tick();
  }

  /* ── Portfolio Deep Dive Prompt ──────────────────────────────────────────── */
  function hideDeepDive() {
    $("#pf-deepdive").hidden = true;
    P.dd = "";
    setToggle(false);
  }

  async function loadDeepDive(key, token) {
    let d;
    try { d = await getJSON(`/api/portfolio/deepdive?k=${encodeURIComponent(key)}`); } catch (_) { return; }
    if (token !== P.token) return;
    P.dd = d.prompt || "";
    $("#pf-dd-text").textContent = P.dd;
    const ai = { included: "AI verdict included", pending: "the AI verdict is still being written",
      unavailable: "without an AI verdict" }[d.ai];
    $("#pf-dd-meta").textContent = ["An investment-committee brief on the whole portfolio for Claude, ChatGPT or Gemini: paste it into a chat with web search on.",
      isNum(d.words) ? `${nf.format(d.words)} words` : null, ai].filter(Boolean).join(" · ");
    $("#pf-deepdive").hidden = !P.dd;
  }

  function setToggle(show) {
    const pre = $("#pf-dd-text"), btn = $("#pf-dd-toggle");
    pre.hidden = !show;
    btn.textContent = show ? "Hide" : "Show";
    btn.setAttribute("aria-expanded", String(show));
  }

  function ddStatus(msg) {
    const s = $("#pf-dd-status");
    s.textContent = "";
    requestAnimationFrame(() => { s.textContent = msg; });
  }

  function initDeepDive() {
    const copyBtn = $("#pf-dd-copy");
    copyBtn.addEventListener("click", async () => {
      if (!P.dd) return;
      const ok = await window.LynchSearch.copyText(P.dd);
      if (ok) {
        copyBtn.textContent = "Copied ✓";
        ddStatus("Portfolio deep dive prompt copied to the clipboard.");
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
      } else {  // show it selected so a long-press / Ctrl+C copies it
        setToggle(true);
        const range = document.createRange();
        range.selectNodeContents($("#pf-dd-text"));
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        ddStatus("Copy is blocked by this browser: the prompt is selected, copy it manually.");
      }
    });
    $("#pf-dd-toggle").addEventListener("click", () => setToggle($("#pf-dd-text").hidden));
  }

  window.LynchPortfolio = {
    init(app) {
      P.app = app;
      if (!(app && app.health && app.health.features && app.health.features.search && window.LynchSearch)) return;
      load();
      $("#pf-switch").hidden = false;
      $("#pf-bar").hidden = false;
      setMode(P.mode);
      $("#pf-switch").addEventListener("click", () => {
        const on = $("#pf-switch").getAttribute("aria-checked") !== "true";
        setSwitch(on, true);
        if (!on) return $("#q").focus();
        const empty = [...document.querySelectorAll("#pf-rows .pf-sym")].find((i) => !i.value);
        if (empty) empty.focus();
      });
      $("#pf-add").addEventListener("click", addRow);
      for (const b of document.querySelectorAll("#pf-mode button")) {
        b.addEventListener("click", () => {
          if (P.mode === b.dataset.mode) return;
          setMode(b.dataset.mode);
          for (const r of P.rows) r.value = "";  // a % is not a dollar amount: the numbers start afresh
          save();
          renderRows();
        });
      }
      $("#pf-form").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
      initDeepDive();
      window.addEventListener("popstate", fromURL);
      fromURL();
    },
    _test: { validate, fromSpec, pageURL },
  };
})();
