/* The Lynch Pin · Quant Portal — Socials: the "Latest on X" widget next to the profile links.
   The server reads X once per scan day and caches it; this only renders /api/socials.
   Depends on app.js helpers ($, el, getJSON, currentView). Text only. */
"use strict";

(() => {
  const S = { loaded: 0, tries: 0, timer: null };
  const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

  function ago(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
    if (s < 86400) return `${Math.round(s / 3600)}h`;
    return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function metric(label, icon, n) {
    return typeof n === "number" ? el("span", { class: "xw-m", title: `${n} ${label}` },
      el("span", { "aria-hidden": "true" }, icon), el("span", { class: "sr-only" }, `${label}: `), compact.format(n)) : null;
  }

  function row(p, handle) {
    const text = (p.text || "").replace(/\n\s*\n+/g, "\n");  // blank lines would eat the 3-line preview
    return el("li", {},
      el("a", { class: "xw-post", href: p.url, target: "_blank", rel: "noopener noreferrer" },
        el("div", { class: "xw-main" },
          el("div", { class: "xw-meta" }, el("strong", {}, "The Lynch Pin"), ` @${handle} · ${ago(p.time)}`),
          el("p", { class: "xw-text" }, text),
          el("div", { class: "xw-metrics" }, metric("replies", "💬", p.replies), metric("reposts", "🔁", p.reposts),
            metric("likes", "♥", p.likes), metric("views", "📊", p.views))),
        p.image ? el("img", { class: "xw-img", src: p.image, alt: "", loading: "lazy", width: 640, height: 373, decoding: "async" }) : null));
  }

  async function load() {
    clearTimeout(S.timer);
    if (Date.now() - S.loaded < 60000) return;
    let d;
    try { d = await getJSON("/api/socials"); } catch (_) { return; }
    const posts = Array.isArray(d.x) ? d.x.filter((p) => typeof p.url === "string" && p.url.startsWith("https://x.com/")) : [];
    $("#x-widget").hidden = !posts.length;
    $("#x-posts").replaceChildren(...posts.map((p) => row(p, d.handle || "lynch_pin_quant")));
    if (d.refreshing && S.tries++ < 4) S.timer = setTimeout(load, 5000);  // the daily X read is in flight
    else S.loaded = Date.now();
  }

  window.LynchSocials = {
    init() {
      window.addEventListener("popstate", () => { if (currentView() === "home") load(); });
      if (currentView() === "home") load();
    },
    _test: { ago },
  };
})();
