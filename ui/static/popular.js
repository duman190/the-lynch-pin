/* The Lynch Pin · Quant Portal — Most wanted: the tickers visitors looked up most in the last 7 and 30 days, from
   the portal's own stats (ui/stats.py, refreshed every 5 minutes on the server). Between the scans and the social
   links, on the home page only. A row opens that ticker's analysis (today's cache answers it when it holds it).
   Depends on app.js helpers ($, el, getJSON, navigate, currentView). Text only. */
"use strict";

(() => {
  const P = { loaded: 0 };
  const CARDS = [
    { key: "week", caption: "Weekly Top 10", sub: "last 7 days" },
    { key: "month", caption: "Monthly Top 10", sub: "last 30 days" },
  ];
  const count = new Intl.NumberFormat();

  function row(t, i, max) {
    const fill = el("span", { class: "pop-fill" });
    fill.style.width = `${(100 * t.n / max).toFixed(1)}%`;  // CSSOM: the CSP forbids style attributes
    const url = `/?t=${encodeURIComponent(t.ticker)}`;
    const name = typeof t.name === "string" ? t.name : "";
    return el("li", {},
      el("a", {
        class: "pop-row", href: url,
        "aria-label": `${i + 1}. ${t.ticker}${name ? `, ${name}` : ""}: ${t.pct}% of lookups (${t.n}). Open its analysis`,
        onclick: (e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;  // a new tab: the browser's
          e.preventDefault();
          navigate(url);
        },
      },
        el("span", { class: "pop-rank", "aria-hidden": "true" }, i + 1),
        el("span", { class: "pop-ticker", "aria-hidden": "true" }, t.ticker),
        el("span", { class: "pop-main", "aria-hidden": "true" },
          el("span", { class: "pop-name" }, name || " "),
          el("span", { class: "pop-track" }, fill)),
        el("span", { class: "pop-val", "aria-hidden": "true" }, `${t.pct}%`, el("small", {}, `${count.format(t.n)}×`))));
  }

  function card(c, w) {
    const top = (w && Array.isArray(w.top)) ? w.top : [];
    const max = Math.max(1, ...top.map((t) => t.n));
    const body = top.length
      ? el("ol", { class: "pop-list", "aria-label": `${c.caption}, ${c.sub}` }, top.map((t, i) => row(t, i, max)))
      : el("div", { class: "pop-empty", role: "status" }, "No lookups yet in this window");
    const total = w && w.total ? ` · ${count.format(w.total)} lookups` : "";
    return el("figure", { class: "widget val-card" },
      el("div", { class: "pop-panel" }, body),
      el("figcaption", {}, el("span", { class: "val-cap" }, c.caption),
        el("span", { class: "val-sub" }, `Most looked-up tickers, ${c.sub}${total}`)));
  }

  async function load() {
    if (Date.now() - P.loaded < 300000) return;  // the server recomputes every 5 minutes
    let d;
    try { d = await getJSON("/api/popular"); } catch (_) { return; }
    const any = d && d.enabled && CARDS.some((c) => d[c.key] && d[c.key].top && d[c.key].top.length);
    $("#popular").hidden = !any;
    if (!any) return;
    $("#pop-grid").replaceChildren(...CARDS.map((c) => card(c, d[c.key])));
    P.loaded = Date.now();
  }

  window.LynchPopular = {
    init() {
      window.addEventListener("popstate", () => { if (currentView() === "home") load(); });
      if (currentView() === "home") load();
    },
  };
})();
