/* The Lynch Pin · Quant Portal — "Latest scans": an iOS-style widget carousel of the daily scans
   (oldest → newest, opening on the newest) and each scan's full thread at ?scan=KIND, laid out like X posts.
   Depends on app.js helpers ($, el, getJSON, openLightbox, downloadLink, navigate, applyView). Text only. */
"use strict";

(() => {
  const KIND_RE = /^[a-z0-9][a-z0-9_\-]{0,31}$/;
  const TOKEN_RE = /(\$[A-Z][A-Z0-9.\-]{0,9}\b|#[A-Za-z]\w*|@[A-Za-z_]\w*)/g;
  const S = { app: null, list: null, fetched: 0, active: -1, activeKind: null, token: 0, raf: 0 };

  const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── dates ──────────────────────────────────────────────────────────────── */
  function localDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  function relDay(iso) {
    const days = Math.round((new Date().setHours(0, 0, 0, 0) - localDate(iso).getTime()) / 86400000);
    return days === 0 ? "Today" : days === 1 ? "Yesterday" : null;
  }
  const fmt = (iso, opts) => localDate(iso).toLocaleDateString(undefined, opts);

  /* ── data ───────────────────────────────────────────────────────────────── */
  async function loadList() {
    if (S.list && Date.now() - S.fetched < 60000) return S.list;
    const body = await getJSON("/api/scans");
    S.list = (body && Array.isArray(body.scans) ? body.scans : []).filter((s) => KIND_RE.test(s.kind)).reverse();
    S.fetched = Date.now();
    return S.list;
  }

  /** Post text as nodes: $TICKERS of the scan link to their analysis, #tags and @handles in sky. */
  function richText(text, tickers) {
    const out = [];
    let last = 0;
    for (const m of text.matchAll(TOKEN_RE)) {
      if (m.index > last) out.push(text.slice(last, m.index));
      const tok = m[0], sym = tok.slice(1);
      const searchable = S.app && S.app.health && S.app.health.features.search;
      if (tok[0] === "$" && searchable && tickers.has(sym)) {
        out.push(el("a", { class: "x-tag", href: `?t=${encodeURIComponent(sym)}`, onclick: (e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey) return;
          e.preventDefault();
          window.LynchSearch.go(sym);
        } }, tok));
      } else out.push(el("span", { class: "x-tag" }, tok));
      last = m.index + tok.length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  /* ── home: widget carousel ──────────────────────────────────────────────── */
  function card(scan, i) {
    const rel = relDay(scan.date);
    const alt = `${scan.title} scan chart`;
    return el("a", {
      class: "scan-card", href: `?scan=${scan.kind}`, "data-i": i, role: "group", "aria-roledescription": "slide",
      "aria-label": `${scan.title}, ${fmt(scan.date, { weekday: "long", month: "long", day: "numeric" })}: open the thread`,
      onclick: (e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); openScan(scan.kind); },
    },
      el("div", { class: "sc-top" },
        el("div", {},
          el("div", { class: "sc-day" }, fmt(scan.date, { weekday: "long" })),
          el("div", { class: "sc-title" }, scan.title),
          scan.subtitle ? el("div", { class: "sc-sub" }, scan.subtitle) : null),
        el("div", { class: "sc-date" },
          el("span", { class: "sc-num" }, String(localDate(scan.date).getDate())),
          el("span", { class: "sc-mon" }, fmt(scan.date, { month: "short" })),
          rel ? el("span", { class: `sc-rel${rel === "Today" ? " today" : ""}` }, rel) : null)),
      el("p", { class: "sc-text" }, scan.text),
      scan.image ? el("img", { class: "sc-img", src: scan.image.src, alt, loading: i < S.list.length - 2 ? "lazy" : "eager",
        width: 1100, height: 642, decoding: "async" }) : null,
      el("div", { class: "sc-foot" },
        el("span", { class: "sc-tickers" }, scan.tickers.slice(0, 5).map((t) => `$${t}`).join(" ") +
          (scan.tickers.length > 5 ? ` +${scan.tickers.length - 5}` : "")),
        el("span", { class: "sc-open" }, `${scan.posts} posts ›`)));
  }

  function tab(scan, i) {
    const rel = relDay(scan.date);
    return el("button", { type: "button", class: "scan-tab", "data-i": i, onclick: () => scrollToCard(i) },
      el("span", { class: "st-date" }, rel || fmt(scan.date, { weekday: "short", month: "short", day: "numeric" })),
      el("span", { class: "st-name" }, scan.title));
  }

  function scrollToCard(i, smooth = true) {
    const track = $("#scan-track");
    const c = track.children[i];
    if (!c) return;
    track.scrollTo({ left: c.offsetLeft - track.children[0].offsetLeft, behavior: smooth && !reduced() ? "smooth" : "auto" });
    setActive(i, smooth);
  }

  function setActive(i, smooth = true) {
    if (i === S.active || !S.list[i]) return;
    S.active = i;
    S.activeKind = S.list[i].kind;
    const tabs = $("#scan-tabs").children;
    for (let k = 0; k < tabs.length; k++) {
      tabs[k].classList.toggle("on", k === i);
      tabs[k].setAttribute("aria-pressed", String(k === i));
    }
    const on = tabs[i];
    if (on) on.parentNode.scrollTo({ left: on.offsetLeft - (on.parentNode.clientWidth - on.offsetWidth) / 2, behavior: smooth && !reduced() ? "smooth" : "auto" });
    const dots = $("#scan-dots").children;
    for (let k = 0; k < dots.length; k++) dots[k].classList.toggle("on", k === i);
    $("#scan-prev").disabled = i === 0;
    $("#scan-next").disabled = i === S.list.length - 1;
  }

  /** The slide nearest the track's left edge is the active one (follows finger swipes and wheel scrolls). */
  function onScroll() {
    cancelAnimationFrame(S.raf);
    S.raf = requestAnimationFrame(() => {
      const track = $("#scan-track");
      const x = track.scrollLeft + track.children[0].offsetLeft;
      let best = 0, dist = Infinity;
      for (let k = 0; k < track.children.length; k++) {
        const d = Math.abs(track.children[k].offsetLeft - x);
        if (d < dist) { dist = d; best = k; }
      }
      if (track.scrollLeft + track.clientWidth >= track.scrollWidth - 4) best = track.children.length - 1;
      setActive(best);
    });
  }

  async function showHome() {
    const sec = $("#scans");
    let list;
    try { list = await loadList(); } catch (_) { list = []; }
    if (!list.length) { sec.hidden = true; return; }
    sec.hidden = false;
    const tabs = $("#scan-tabs"), track = $("#scan-track"), dots = $("#scan-dots");
    if (track.dataset.rendered !== String(S.fetched)) {
      put(tabs, list.map(tab));
      put(track, list.map(card));
      put(dots, list.map(() => el("span", { class: "dot" })));
      track.dataset.rendered = String(S.fetched);
      S.active = -1;
    }
    const keep = list.findIndex((s) => s.kind === S.activeKind);
    scrollToCard(keep >= 0 ? keep : list.length - 1, false);  // laid out already; no frame wait (paused in background tabs)
  }

  function put(node, kids) { node.replaceChildren(...kids); }

  /* ── ?scan=KIND: the whole thread, X style ──────────────────────────────── */
  function openScan(kind) {
    S.activeKind = kind;
    navigate(`?scan=${encodeURIComponent(kind)}`);
    window.scrollTo({ top: 0, behavior: "auto" });
  }

  function post(p, scan, tickers, i, n) {
    const alt = p.ticker ? `${p.ticker} PEG valuation deviation chart` : `${scan.title} scan chart`;
    const name = p.image ? decodeURIComponent(p.image.full.split("/").pop().split("?")[0]) : null;
    return el("li", { class: "x-post" },
      el("div", { class: "x-rail" },
        el("img", { class: "x-avatar", src: "/static/img/logo.png", alt: "", width: 44, height: 44 }),
        i < n - 1 ? el("span", { class: "x-line", "aria-hidden": "true" }) : null),
      el("article", { class: "x-body", "aria-label": p.ticker ? `$${p.ticker}` : i === 0 ? "Scan summary" : "Closing post" },
        el("header", { class: "x-head" },
          el("strong", {}, "The Lynch Pin"),
          el("span", { class: "x-meta" }, `· ${fmt(scan.date, { month: "short", day: "numeric" })}`),
          p.ticker && S.app.health && S.app.health.features.search
            ? el("button", { type: "button", class: "btn-ghost x-analyze", onclick: () => window.LynchSearch.go(p.ticker) }, `Analyze $${p.ticker} ›`)
            : null),
        el("p", { class: "x-text" }, richText(p.text, tickers)),
        p.image ? el("button", { type: "button", class: "plot-btn x-media", "aria-label": `Enlarge ${alt}`, onclick: () => openLightbox(p.image.full, alt) },
          el("img", { src: p.image.src, alt, loading: i > 1 ? "lazy" : "eager", width: 1100, height: 642, decoding: "async" })) : null,
        p.image ? el("p", { class: "x-dl muted small" }, "Tap to enlarge · ", downloadLink(p.image.full, name, alt)) : null));
  }

  function neighbours(kind) {
    const list = S.list || [];
    const i = list.findIndex((s) => s.kind === kind);
    if (i < 0) return null;
    const link = (s, cls, label) => s ? el("a", { class: `btn-ghost ${cls}`, href: `?scan=${s.kind}`, onclick: (e) => { e.preventDefault(); openScan(s.kind); } },
      label(s)) : el("span");
    return el("nav", { class: "thread-nav", "aria-label": "Other scans" },
      link(list[i - 1], "prev", (s) => `‹ ${relDay(s.date) || fmt(s.date, { weekday: "short" })} · ${s.title}`),
      link(list[i + 1], "next", (s) => `${relDay(s.date) || fmt(s.date, { weekday: "short" })} · ${s.title} ›`));
  }

  async function showThread(kind) {
    const sec = $("#thread");
    const token = ++S.token;
    sec.hidden = false;
    sec.replaceChildren(el("div", { class: "panel" }, el("div", { class: "shimmer", "aria-hidden": "true" }, el("span"), el("span"), el("span"))));
    let scan;
    try {
      [scan] = await Promise.all([getJSON(`/api/scans/${encodeURIComponent(kind)}`), loadList().catch(() => [])]);
    } catch (err) {
      if (token !== S.token) return;
      sec.replaceChildren(el("div", { class: "panel" }, el("h2", {}, "Scan not found"),
        el("p", { class: "muted" }, err.status === 404 ? "This scan is not in the archive (yet)." : `Could not load the scan: ${err.message}`),
        el("a", { class: "btn-ghost js-back", href: "/", onclick: backHome }, "‹ Latest scans")));
      return;
    }
    if (token !== S.token) return;
    S.activeKind = kind;
    const tickers = new Set(scan.posts.filter((p) => p.ticker).map((p) => p.ticker));
    const rel = relDay(scan.date);
    document.title = `${scan.title} scan · The Lynch Pin`;
    sec.replaceChildren(
      el("div", { class: "panel thread-head" },
        el("a", { class: "btn-ghost", href: "/", onclick: backHome }, "‹ Latest scans"),
        el("div", { class: "th-title" },
          el("h1", { id: "thread-title" }, scan.title),
          el("p", { class: "muted small" }, [scan.subtitle, `${rel ? rel + " · " : ""}${fmt(scan.date, { weekday: "long", month: "long", day: "numeric", year: "numeric" })}`,
            `${scan.posts.length} posts`].filter(Boolean).join(" · ")))),
      el("ol", { class: "panel x-thread" }, scan.posts.map((p, i) => post(p, scan, tickers, i, scan.posts.length))),
      neighbours(kind));
  }

  function backHome(e) {
    if (e && (e.metaKey || e.ctrlKey || e.shiftKey)) return;
    if (e) e.preventDefault();
    navigate("/");
    window.scrollTo({ top: 0, behavior: "auto" });
    $("#scans").scrollIntoView({ block: "start", behavior: "auto" });
  }

  /* ── routing ────────────────────────────────────────────────────────────── */
  function fromURL() {
    const kind = new URLSearchParams(location.search).get("scan");
    const view = currentView();
    if (view === "scan" && kind && KIND_RE.test(kind)) {
      showThread(kind);
      return;
    }
    S.token += 1;
    $("#thread").hidden = true;
    if (view === "home") {
      if (document.title.includes("scan ·")) document.title = "The Lynch Pin · Quant Portal";
      showHome();
    }
  }

  window.LynchScans = {
    init(appState) {
      S.app = appState;
      if (!appState.health || appState.health.features.scans === false) return;
      $("#scan-track").addEventListener("scroll", onScroll, { passive: true });
      $("#scan-prev").addEventListener("click", () => scrollToCard(Math.max(0, S.active - 1)));
      $("#scan-next").addEventListener("click", () => scrollToCard(Math.min(S.list.length - 1, S.active + 1)));
      window.addEventListener("popstate", fromURL);
      window.addEventListener("resize", () => { if (S.active >= 0) scrollToCard(S.active, false); });
      fromURL();
    },
    _test: { richText, relDay },
  };
})();
