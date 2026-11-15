/* The Lynch Pin · Quant Portal — Track record: a ticker's past valuations, one a week (ui/track.py), in a
   scrollable table: date, price, 5Y EPS growth forecast, PEG, 5Y bear / base / bull ROI, and the share price's change since then.
   Opened by "View track record" under the Valuation card. Depends on app.js helpers ($, el, getJSON). */
"use strict";

(() => {
  const T = { token: 0 };
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const fx = (v, d) => (isNum(v) ? v.toFixed(d) : "–");
  const signed = (v) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}%` : "–");

  function price(v, cur) {
    if (!isNum(v)) return "–";
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD", maximumFractionDigits: 2 }).format(v);
    } catch (_) { return v.toFixed(2); }
  }

  function day(iso, short = false) {
    const t = Date.parse(`${iso}T12:00:00`);
    return Number.isFinite(t) ? new Date(t).toLocaleDateString(undefined, short
      ? { year: "2-digit", month: "numeric", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" }) : iso;
  }

  function roi(r) {
    const n = (v, cls) => el("span", { class: cls }, fx(v, 1));
    const sep = () => el("span", { class: "tr-sep" }, "/");
    return el("td", { class: "tr-roi" }, n(r.bear, "red"), sep(), n(r.base, "sky"), sep(), n(r.bull, "green"),
      el("span", { class: "tr-unit" }, "%"));
  }

  function table(d) {
    const cur = d.current && d.current.currency;
    const head = el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Date"), el("th", { scope: "col" }, "Price"),
      el("th", { scope: "col", title: "The 5-year EPS growth forecast behind that day's PEG" }, "5Y growth"), el("th", { scope: "col" }, "PEG"),
      el("th", { scope: "col" }, "Bear / Base / Bull ROI"), el("th", { scope: "col" }, "Since then")));
    const body = el("tbody", {}, d.rows.map((r) => el("tr", {},
      el("th", { scope: "row" }, el("span", { class: "d-long" }, day(r.date)), el("span", { class: "d-short" }, day(r.date, true))),
      el("td", {}, el("span", { class: "d-long" }, price(r.price, cur)), el("span", { class: "d-short" }, fx(r.price, 2))),
      el("td", {}, fx(r.growth, 1), isNum(r.growth) ? el("span", { class: "tr-unit" }, "%") : null),
      el("td", {}, fx(r.peg, 2)),
      roi(r),
      el("td", { class: `tr-since ${isNum(r.since_pct) ? (r.since_pct > 0 ? "green" : r.since_pct < 0 ? "red" : "") : ""}` }, signed(r.since_pct)))));
    return el("table", { class: "track-table" }, head, body);
  }

  async function open(sym) {
    const dlg = $("#track");
    const token = ++T.token;
    $("#track-title").textContent = `$${sym} track record`;
    $("#track-sub").textContent = "Loading past valuations…";
    $("#track-body").replaceChildren();
    if (!dlg.open && typeof dlg.showModal === "function") dlg.showModal();
    let d;
    try { d = await getJSON(`/api/ticker/${encodeURIComponent(sym)}/track`); } catch (e) {
      if (token === T.token) $("#track-sub").textContent = `Track record unavailable: ${e.message}`;
      return;
    }
    if (token !== T.token) return;
    const rows = Array.isArray(d.rows) ? d.rows : [];
    const now = d.current || {};
    $("#track-sub").textContent = rows.length
      ? `${rows.length} weekly valuation${rows.length === 1 ? "" : "s"} · “Since then” is the share price's change to ${price(now.price, now.currency)}${now.date ? ` (${day(now.date)})` : ""}`
      : "No valuations recorded yet.";
    $("#track-body").replaceChildren(rows.length ? table(d)
      : el("p", { class: "muted track-empty" }, "The track record keeps one valuation a week from the first time this ticker is analysed: check back next week."));
    $("#track-body").scrollTop = 0;
  }

  function init() {
    const dlg = $("#track");
    if (!dlg) return;
    $("#track-close").addEventListener("click", () => dlg.close());
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  }

  window.LynchTrack = { open };
  document.addEventListener("DOMContentLoaded", init);
})();
