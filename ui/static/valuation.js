/* The Lynch Pin · Quant Portal — US stock market valuation, under the ticker search: the S&P 500 Shiller PE (rear
   view mirror) and forward PEG (forward looking), with a Peter Lynch quote under them. The server redraws the Shiller
   PE each weekday and the PEG each Friday; this only renders /api/valuation. Depends on app.js helpers ($, el, getJSON, openLightbox,
   currentView). Text only. */
"use strict";

(() => {
  const V = { loaded: 0, timer: null };
  const num = (v, digits) => (typeof v === "number" ? v.toFixed(digits) : "–");
  const CARDS = [
    {
      key: "shiller", caption: "Rear view mirror", file: "sp500_shiller_pe.png",
      sub: (d) => `S&P 500 Shiller PE (CAPE), monthly since ${d.since || 1881}`,
      alt: (d) => `S&P 500 Shiller PE (CAPE): ${num(d.value, 1)} on ${d.date}, against a mean of ${num(d.mean, 1)} since ${d.since || 1881}.`,
      wait: (run) => (run ? "Fetching the Shiller PE…" : "The Shiller PE is fetched once a weekday."),
    },
    {
      key: "peg", caption: "Forward looking", file: "sp500_forward_peg.png",
      sub: (d) => `S&P 500 price ÷ 5Y expected EPS growth, since ${d.since || 1995}`,
      alt: (d) => `S&P 500 forward PEG since ${d.since || 1995}: ${num(d.peg, 2)} on ${d.date}, against Peter Lynch's fair value of 1.0.`,
      wait: (run) => (run ? "Computing the S&P 500 forward PEG…" : "The S&P 500 forward PEG is computed after Friday's close."),
    },
  ];

  function caption(c, d) {
    return el("figcaption", {}, el("span", { class: "val-cap" }, c.caption), el("span", { class: "val-sub" }, c.sub(d || {})));
  }

  function card(c, d, run) {
    if (!d || typeof d.image !== "string" || typeof d.preview !== "string") {  // no chart yet: say what is coming
      const progress = run && run.total ? `${run.done || 0} of ${run.total} constituents` : null;
      return el("figure", { class: "widget val-card" },
        el("div", { class: "val-pending", role: "status" }, el("strong", {}, c.wait(run)),
          progress ? el("span", { class: "small" }, progress) : null),
        caption(c));
    }
    const alt = c.alt(d);
    return el("figure", { class: "widget val-card" },
      el("button", { type: "button", class: "val-chart", "aria-label": `Enlarge: ${alt}`, onclick: () => openLightbox(d.image, alt, c.file) },
        el("img", { src: d.preview, alt, width: 1100, height: 688, decoding: "async" })),
      caption(c, d));
  }

  async function load() {
    clearTimeout(V.timer);
    if (Date.now() - V.loaded < 60000) return;
    let d;
    try { d = await getJSON("/api/valuation"); } catch (_) { return; }
    const running = (d && d.running) || {};
    const any = d && d.enabled && (d.shiller || d.peg || running.shiller || running.peg);
    $("#valuation").hidden = !any;
    if (!any) return;
    $("#val-grid").replaceChildren(...CARDS.map((c) => card(c, d[c.key], running[c.key])));
    // a first chart on its way: check again in a minute (the sweep shows its progress meanwhile)
    if (CARDS.some((c) => !d[c.key] && running[c.key])) V.timer = setTimeout(load, 60000);
    else V.loaded = Date.now();
  }

  window.LynchValuation = {
    init() {
      window.addEventListener("popstate", () => { if (currentView() === "home") load(); });
      if (currentView() === "home") load();
    },
  };
})();
