/* The Lynch Pin · Quant Portal — client. No frameworks, no external requests. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

/** Build DOM safely (text only — never innerHTML with server data). */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) {
    const err = new Error((body && body.error) || `HTTP ${res.status}`);
    err.status = res.status;
    err.retryAfter = parseInt(res.headers.get("Retry-After") || "", 10) || null;
    throw err;
  }
  return body;
}

/* ── lightbox ─────────────────────────────────────────────────────────────── */
function openLightbox(src, alt, filename) {
  const dlg = $("#lightbox");
  const img = $("#lightbox-img");
  img.src = src;
  img.alt = alt || "";
  // a chart that can be saved gets a download icon next to the close button (see downloadLink)
  const dl = $("#lightbox-dl");
  dl.replaceChildren();
  dl.hidden = !filename;
  if (filename) dl.append(downloadLink(src, filename, alt, DOWNLOAD_ICON, "Download chart", { prefetch: true }));
  $("#lightbox-hint").hidden = true;
  if (dlg.open) return;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else window.open(src, "_blank", "noopener");
}
function initLightbox() {
  const dlg = $("#lightbox");
  $("#lightbox-close").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
}

/* ── chart download ──────────────────────────────────────────────────────── */
/* An iOS home-screen app has no browser chrome: following a download link strands the user on a file
   preview with no way back. There, hand the PNG to the share sheet ("Save Image") instead. iOS opens
   the share sheet only within the tap that asked for it, so the PNG must already be downloaded by then:
   the lightbox fetches it when it opens, and the tap shares the blob without waiting on anything. */
let pngFetch = { url: null, promise: null, blob: null };
function fetchPng(url) {
  if (pngFetch.url !== url) {
    const entry = { url, promise: null, blob: null };
    entry.promise = fetch(url)
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.blob(); })
      .then((blob) => { entry.blob = blob; return blob; });
    entry.promise.catch(() => { if (pngFetch === entry) pngFetch = { url: null, promise: null, blob: null }; });
    pngFetch = entry;
  }
  return pngFetch.promise;
}
/* When the share sheet can't open: show the image with "press and hold to save". The lightbox stays as it
   is when it already shows this image, download button included. */
function saveHint(url, alt) {
  const dlg = $("#lightbox");
  if (!(dlg.open && $("#lightbox-img").src === new URL(url, location.href).href)) openLightbox(url, alt);
  $("#lightbox-hint").hidden = false;
}
/* the tray-and-arrow glyph of a browser's download button */
const DOWNLOAD_ICON = (() => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M12 3v12m0 0-5-5m5 5 5-5M4 20h16");
  svg.append(path);
  return svg;
})();
function downloadLink(url, filename, alt, content, label, { prefetch = false } = {}) {
  const a = el("a", { href: url, download: filename }, content ? content.cloneNode(true) : "download PNG");
  if (label) { a.setAttribute("aria-label", label); a.title = label; }
  if (navigator.standalone !== true) return a;  // browsers honour the download attribute
  if (prefetch) {  // the open lightbox: download now (button dimmed meanwhile), share on the tap
    a.classList.add("is-loading");
    a.setAttribute("aria-disabled", "true");
    const done = () => { a.classList.remove("is-loading"); a.removeAttribute("aria-disabled"); };
    fetchPng(url).then(done, done);
  }
  a.addEventListener("pointerdown", () => { fetchPng(url).catch(() => {}); });  // links elsewhere: head start
  const share = (blob) => {
    const file = new File([blob], filename, { type: "image/png" });
    if (!navigator.canShare || !navigator.canShare({ files: [file] })) return saveHint(url, alt);
    navigator.share({ files: [file] }).catch((err) => {
      if (!(err && err.name === "AbortError")) saveHint(url, alt);  // AbortError: share sheet dismissed
    });
  };
  a.addEventListener("click", (e) => {
    e.preventDefault();
    if (a.classList.contains("is-loading")) return;  // a tap now would come too late for the share sheet
    const ready = pngFetch.url === url ? pngFetch.blob : null;
    if (ready) share(ready);  // still within the tap: the share sheet opens
    else fetchPng(url).then(share, () => saveHint(url, alt));  // not downloaded yet: iOS may refuse → hint
  });
  return a;
}

/* ── views: home (search + Latest scans), ?t=SYM (ticker), ?scan=KIND (thread) ── */
function currentView() {
  const q = new URLSearchParams(location.search);
  return q.get("t") ? "ticker" : q.get("scan") ? "scan" : "home";
}
/** Shows what belongs to the URL's view: body[data-view] drives the CSS, the Home button leaves home. */
function applyView() {
  const v = currentView();
  document.body.dataset.view = v;
  $("#home-btn").hidden = v === "home";
  return v;
}
/** Same-page navigation: push the URL, then let every module react as on Back/Forward. */
function navigate(url) {
  if (location.pathname + location.search !== url) history.pushState({}, "", url);
  window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
}
function goHome() {
  navigate("/");
  window.scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}
function initViews() {
  window.addEventListener("popstate", applyView);
  for (const a of document.querySelectorAll(".js-home")) {
    a.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;  // new tab / window
      e.preventDefault();
      goHome();
    });
  }
  applyView();
}

/* ── health / feature chips ──────────────────────────────────────────────── */
const state = { health: null };

function setChip(id, cls, label, title) {
  const chip = $(id);
  chip.hidden = false;
  chip.classList.remove("on", "off", "busy");
  if (cls) chip.classList.add(cls);
  const lbl = $(".chip-label", chip);
  if (lbl.textContent !== label) lbl.textContent = label;  // avoid needless re-announcements
  if (chip.title !== (title || "")) chip.title = title || "";
}

async function refreshHealth() {
  try {
    const h = await getJSON("/api/health");
    state.health = h;
    // without -v the page shows no model name, token counts, thinking setting or cache chip (app.css)
    document.body.classList.toggle("verbose", !!h.verbose);
    $("#search-section").hidden = !h.features.search;
    $("#ai-hint").hidden = !h.features.ai;
    if (h.features.ai && h.ai) {
      const ok = h.ai.available;
      const cls = h.ai.checking ? "busy" : ok ? (h.ai.warning ? "busy" : "on") : "off";
      const label = !h.verbose ? "AI" : h.ai.checking ? "AI …" : ok ? `AI · ${h.ai.model_short || "local"}` : "AI offline";
      const tip = !h.verbose ? (h.ai.checking ? "AI: checking…" : ok ? "AI overview: on" : "AI overview: offline")
        : ok ? `Local model ${h.ai.model} (ctx ${h.ai.ctx}, reasoning ${h.ai.reasoning || "on"})${h.ai.warning ? " — " + h.ai.warning : ""}`
        : (h.ai.reason || "local model unavailable");
      setChip("#chip-ai", cls, label, tip);
      if (h.ai.checking) setTimeout(refreshHealth, 2500);  // background probe finishes shortly
    }
    $("#chip-cache").hidden = !h.verbose;
    if (h.cache && h.verbose) {
      setChip("#chip-cache", null, h.cache.capacity ? `Cache ${h.cache.size}/${h.cache.capacity}` : `Cache ${h.cache.size}`,
        `Today's LFU cache (${h.cache.day}): ${h.cache.hits} hits, ${h.cache.misses} misses, ${h.cache.evictions || 0} evictions` +
        (h.cache.top && h.cache.top.length ? ` · most used: ${h.cache.top.map(([t, f]) => `${t}×${f}`).join(", ")}` : ""));
    }
    return h;
  } catch (_) {
    return null;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  initLightbox();
  initViews();
  await refreshHealth();
  if (window.LynchSearch) window.LynchSearch.init(state);
  if (window.LynchScans) window.LynchScans.init(state);
  if (window.LynchSocials) window.LynchSocials.init();
  setInterval(refreshHealth, 30000);
});
