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
function openLightbox(src, alt) {
  const dlg = $("#lightbox");
  const img = $("#lightbox-img");
  img.src = src;
  img.alt = alt || "";
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
   preview with no way back. There, hand the PNG to the share sheet ("Save Image") instead. */
let pngFetch = { url: null, promise: null };
function fetchPng(url) {
  if (pngFetch.url !== url) {
    const promise = fetch(url).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.blob(); });
    promise.catch(() => { if (pngFetch.promise === promise) pngFetch = { url: null, promise: null }; });
    pngFetch = { url, promise };
  }
  return pngFetch.promise;
}
function downloadLink(url, filename, alt) {
  const a = el("a", { href: url, download: filename }, "download PNG");
  if (navigator.standalone !== true) return a;  // browsers honour the download attribute
  a.addEventListener("pointerdown", () => { fetchPng(url).catch(() => {}); });  // head start: share needs a fresh tap
  a.addEventListener("click", async (e) => {
    e.preventDefault();
    try {
      const file = new File([await fetchPng(url)], filename, { type: "image/png" });
      if (!navigator.canShare || !navigator.canShare({ files: [file] })) throw new Error("file sharing unsupported");
      await navigator.share({ files: [file] });
    } catch (err) {
      if (err && err.name === "AbortError") return;  // share sheet dismissed
      openLightbox(url, alt);  // fallback: press and hold the image to save it
    }
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
    $("#search-section").hidden = !h.features.search;
    $("#ai-hint").hidden = !h.features.ai;
    if (h.features.ai && h.ai) {
      const ok = h.ai.available;
      const cls = h.ai.checking ? "busy" : ok ? (h.ai.warning ? "busy" : "on") : "off";
      const label = h.ai.checking ? "AI …" : ok ? `AI · ${h.ai.model_short || "local"}` : "AI offline";
      const tip = ok ? `Local model ${h.ai.model} (ctx ${h.ai.ctx}, reasoning ${h.ai.reasoning || "on"})${h.ai.warning ? " — " + h.ai.warning : ""}`
        : (h.ai.reason || "local model unavailable");
      setChip("#chip-ai", cls, label, tip);
      if (h.ai.checking) setTimeout(refreshHealth, 2500);  // background probe finishes shortly
    }
    if (h.cache) {
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
  setInterval(refreshHealth, 30000);
});
