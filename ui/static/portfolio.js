/* The Lynch Pin · Quant Portal — Portfolio Analyzer, under the ticker search: main.py's portfolio X-ray for positions
   typed in as % weights (adding up to 100) or as dollars / shares. Each position is looked up like a ticker search
   (today's cached analysis when there is one), then /api/portfolio rolls them up: weighted valuation, growth, ROI and
   quality, the weight donut ("View chart") and the AI verdict. Depends on app.js helpers ($, el, getJSON,
   openLightbox, navigate, currentView). Text only. */
"use strict";

(() => {
  const STORE_KEY = "lynchpin.portfolio";
  const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
  const MAX_POSITIONS = 25;
  const AI_FINAL = new Set(["done", "error", "unavailable"]);
  const P = {
    mode: "pct",  // "pct" | "amt"
    rows: [],     // {sym, value, unit: "usd" | "sh"}
    token: 0,     // the run in progress (a newer Analyze or Close cancels older polls)
    timer: null,
    aiTimer: null,
  };

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const fx = (v, d = 1, suf = "") => (isNum(v) ? v.toFixed(d) + suf : "N/A");
  const signed = (v, d = 1, suf = "%") => (isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(d) + suf : "N/A");
  const pct = (w) => (isNum(w) ? (w * 100).toFixed(1) + "%" : "—");

  /* ── saved positions (this browser only) ─────────────────────────────── */
  function load() {
    try {
      const s = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
      if (s && Array.isArray(s.rows)) {
        P.mode = s.mode === "amt" ? "amt" : "pct";
        P.rows = s.rows.slice(0, MAX_POSITIONS).map((r) => ({
          sym: String(r.sym || "").toUpperCase().slice(0, 10), value: String(r.value || "").slice(0, 16),
          unit: r.unit === "sh" ? "sh" : "usd" }));
      }
    } catch (_) { /* private window / blocked storage: start empty */ }
    if (!P.rows.length) P.rows = [blank(), blank()];
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ mode: P.mode, rows: P.rows })); } catch (_) { /* ignore */ }
  }
  function blank() { return { sym: "", value: "", unit: "usd" }; }

  /* ── the form ─────────────────────────────────────────────────────────── */
  function filled() { return P.rows.filter((r) => r.sym.trim() || String(r.value).trim()); }

  /** {ok, message, query} for the positions as typed. */
  function validate() {
    const rows = filled();
    if (!rows.length) return { ok: false, message: "Add at least one position." };
    const parts = [];
    let total = 0;
    const seen = new Set();
    for (const r of rows) {
      const sym = r.sym.trim().toUpperCase();
      const v = parseFloat(String(r.value).replace(/[,$%\s]/g, ""));
      if (!TICKER_RE.test(sym)) return { ok: false, message: sym ? `“${sym}” is not a ticker.` : "A position is missing its ticker." };
      if (!(v > 0)) return { ok: false, message: `${sym}: enter a positive ${P.mode === "pct" ? "weight" : r.unit === "sh" ? "number of shares" : "dollar amount"}.` };
      if (seen.has(sym)) return { ok: false, message: `${sym} is listed twice.` };
      seen.add(sym);
      total += v;
      const num = String(+v.toFixed(6));
      parts.push(`${sym}:${P.mode === "pct" ? num : r.unit === "sh" ? num + "sh" : "$" + num}`);
    }
    if (P.mode === "pct" && Math.abs(total - 100) > 0.5) {
      return { ok: false, total, message: `Weights add up to ${+total.toFixed(2)}% — they need to add up to 100%.` };
    }
    return { ok: true, total, query: `m=${P.mode}&p=${encodeURIComponent(parts.join(","))}` };
  }

  function updateTotal() {
    const v = validate();
    const tot = $("#pf-total");
    const n = filled().length;
    if (P.mode === "pct") {
      const sum = filled().reduce((s, r) => s + (parseFloat(String(r.value).replace(/[,%\s]/g, "")) || 0), 0);
      tot.textContent = `Total ${+sum.toFixed(2)}% of 100%`;
      tot.className = `pf-total ${Math.abs(sum - 100) <= 0.5 ? "green" : "muted"}`;
    } else {
      tot.textContent = `${n} position${n === 1 ? "" : "s"} · weighted by market value`;
      tot.className = "pf-total muted";
    }
    $("#pf-go").disabled = !v.ok;
    $("#pf-error").textContent = v.ok || !n ? "" : v.message;
  }

  function rowView(r, i) {
    const sym = el("input", { type: "text", class: "pf-sym", value: r.sym, maxlength: 10, placeholder: "MSFT",
      autocapitalize: "characters", autocorrect: "off", spellcheck: "false", "aria-label": `Position ${i + 1} ticker` });
    sym.addEventListener("input", () => { r.sym = sym.value.toUpperCase(); save(); updateTotal(); });
    const val = el("input", { type: "text", inputmode: "decimal", class: "pf-val", value: r.value, maxlength: 16,
      placeholder: P.mode === "pct" ? "25" : r.unit === "sh" ? "10" : "5000",
      "aria-label": `Position ${i + 1} ${P.mode === "pct" ? "weight in percent" : r.unit === "sh" ? "shares" : "dollars"}` });
    val.addEventListener("input", () => { r.value = val.value; save(); updateTotal(); });
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
      onclick: () => { P.rows.splice(i, 1); if (!P.rows.length) P.rows.push(blank()); save(); renderRows(); } }, "×");
    return el("li", { class: "pf-row" }, sym, el("span", { class: "pf-amount" }, val, unit), del);
  }

  function renderRows() {
    $("#pf-rows").replaceChildren(...P.rows.map(rowView));
    $("#pf-add").disabled = P.rows.length >= MAX_POSITIONS;
    for (const b of document.querySelectorAll("#pf-mode button")) b.setAttribute("aria-pressed", String(b.dataset.mode === P.mode));
    $("#pf-mode-hint").textContent = P.mode === "pct"
      ? "Each position’s share of the portfolio, adding up to 100%."
      : "Dollars or a number of shares per position (shares are valued at today’s price). Only the resulting weights are used.";
    updateTotal();
  }

  /* ── running an analysis ─────────────────────────────────────────────── */
  function stop() {
    P.token++;
    clearTimeout(P.timer);
    clearTimeout(P.aiTimer);
  }

  function status(...kids) {
    $("#pf-result").replaceChildren(el("div", { class: "pf-status", role: "status" }, ...kids));
  }

  async function analyze() {
    const v = validate();
    if (!v.ok) return updateTotal();
    stop();
    const token = P.token;
    $("#pf-result").hidden = false;
    status(el("strong", {}, "Looking up the positions…"));
    const tick = async (poll) => {
      if (token !== P.token) return;
      let snap;
      try {
        snap = await getJSON(`/api/portfolio?${v.query}${poll ? "&poll=1" : ""}`);
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
          : el("p", { class: "muted small" }, "The chart could not be drawn.")),
      el("section", { class: "widget pf-card", "aria-labelledby": "pf-pos-title" },
        el("h3", { class: "sub-h", id: "pf-pos-title" }, "Positions"),
        positionsTable(r.positions || [])),
      r.ai ? el("section", { class: "widget pf-card pf-ai", id: "pf-ai", "aria-labelledby": "pf-ai-title", "aria-live": "polite" },
        el("h3", { class: "sub-h", id: "pf-ai-title" }, "🤖 AI overview"),
        el("p", { class: "ai-status muted", id: "pf-ai-status" }, "Asking the model for its verdict…")) : null);
    $("#pf-result").replaceChildren(card,
      el("p", { class: "muted small pf-note" }, "Weights = market value share. PE ratios are harmonic weighted means; growth, PEG and ROI are weighted averages. Quant scans, not financial advice."));
    if (r.chart) new Image().src = r.chart.preview;  // cached by the time View chart is clicked
    if (r.ai) pollAI(r.key, token);
  }

  function aiSection(label, text, cls) {
    return el("div", { class: `ai-sec ${cls}` }, el("h3", {}, label), el("p", { class: "ai-text" }, text));
  }

  function renderAI(ai, live) {
    const box = $("#pf-ai");
    if (!box) return;
    const kids = [el("h3", { class: "sub-h", id: "pf-ai-title" }, "🤖 AI overview")];
    const any = ai.verdict || ai.bull || ai.bear;
    if (ai.verdict) kids.push(el("p", { class: "ai-text pf-verdict" }, ai.verdict));
    if (ai.bull) kids.push(aiSection("🐂 Bull", ai.bull, "ai-bull"));
    if (ai.bear) kids.push(aiSection("🐻 Bear", ai.bear, "ai-bear"));
    if (!any && ai.text) kids.push(el("p", { class: "ai-text" }, ai.text));
    if (live) kids.push(el("p", { class: "ai-status muted", role: "status" }, ai.status === "queued" ? "Waiting for the model…" : "Writing…"));
    else if (ai.status !== "done") {
      kids.push(el("p", { class: "ai-status red" }, ai.error || "AI overview unavailable."),
        el("button", { type: "button", class: "btn-ghost", onclick: () => { renderAI({ status: "queued" }, true); pollAI(box.dataset.key, P.token); } }, "↻ Retry AI"));
    } else {
      kids.push(el("p", { class: "muted small" }, "Peter Lynch-style verdict from the numbers above. Not financial advice."));
    }
    box.replaceChildren(...kids);
  }

  function pollAI(key, token) {
    const box = $("#pf-ai");
    if (box) box.dataset.key = key;
    const tick = async () => {
      if (token !== P.token) return;
      let ai;
      try { ai = await getJSON(`/api/portfolio/ai?k=${encodeURIComponent(key)}`); } catch (e) {
        if (token === P.token) renderAI({ status: "error", error: e.message }, false);
        return;
      }
      if (token !== P.token) return;
      const live = !AI_FINAL.has(ai.status);
      renderAI(ai, live);
      if (live) P.aiTimer = setTimeout(tick, 1000);
    };
    tick();
  }

  /* ── open / close ────────────────────────────────────────────────────── */
  function open() {
    const sec = $("#portfolio");
    sec.hidden = false;
    $("#pf-open").setAttribute("aria-expanded", "true");
    renderRows();
    const first = $("#pf-rows .pf-sym");
    if (first && !first.value) first.focus();
    sec.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }
  function close() {
    stop();
    $("#portfolio").hidden = true;
    $("#pf-open").setAttribute("aria-expanded", "false");
    $("#pf-open").focus();
  }

  window.LynchPortfolio = {
    init(app) {
      if (!(app && app.health && app.health.features && app.health.features.search)) return;
      load();
      $("#pf-open").hidden = false;
      $("#pf-open").addEventListener("click", () => ($("#portfolio").hidden ? open() : close()));
      $("#pf-close").addEventListener("click", close);
      $("#pf-add").addEventListener("click", () => {
        if (P.rows.length >= MAX_POSITIONS) return;
        P.rows.push(blank());
        save();
        renderRows();
        const syms = document.querySelectorAll("#pf-rows .pf-sym");
        syms[syms.length - 1].focus();
      });
      for (const b of document.querySelectorAll("#pf-mode button")) {
        b.addEventListener("click", () => {
          if (P.mode === b.dataset.mode) return;
          P.mode = b.dataset.mode;
          for (const r of P.rows) r.value = "";  // a % is not a dollar amount: start the numbers afresh
          save();
          renderRows();
        });
      }
      $("#pf-form").addEventListener("submit", (e) => { e.preventDefault(); analyze(); });
      window.addEventListener("popstate", () => { if (currentView() !== "home") stop(); });
    },
  };
})();
