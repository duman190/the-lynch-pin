/* The Lynch Pin · Portal stats: reads /api/stats (ui/stats.py) and draws every chart as inline SVG.
   No chart library: the page's CSP only runs this site's own scripts. */
"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const COLOR = { s1: "#5596E8", s2: "#D95926", hl: "#F5B041", grid: "#2A2A2A", axis: "#3A3A3A", surface: "#1A1A1A" };
  const REFRESH_MS = 60000;
  const $ = (sel, root = document) => root.querySelector(sel);
  const main = $("#main");
  const tip = $("#st-tip");
  let days = 30;
  let data = null;

  // ── formatting ──────────────────────────────────────────────────────────────
  const nf = new Intl.NumberFormat("en-US");
  const sig = (x, d = 3) => +Number(x).toPrecision(d);
  function num(n) {
    if (n == null) return "–";
    const a = Math.abs(n);
    if (a >= 1e6) return sig(n / 1e6) + "M";
    if (a >= 1e4) return sig(n / 1e3) + "K";
    return nf.format(a >= 100 ? Math.round(n) : sig(n));
  }
  function dur(s) {
    if (s == null) return "–";
    if (s <= 0) return "0 s";
    if (s < 1e-3) return sig(s * 1e6) + " µs";
    if (s < 1) return sig(s * 1e3) + " ms";
    if (s < 60) return sig(s) + " s";
    const m = Math.floor(s / 60), r = Math.round(s - m * 60);
    return r === 60 ? `${m + 1}m 00s` : `${m}m ${String(r).padStart(2, "0")}s`;
  }
  const pct = (x) => (x == null ? "–" : (x === 0 || x === 100 ? x : x >= 1 ? x.toFixed(1) : sig(x, 2)) + "%");
  const durTick = (s) => (s >= 60 ? sig(s) + " s" : dur(s));  // an axis reads 100 s, not 1m 40s
  const pctTick = (x) => x + "%";
  const rate = (x) => (x == null ? "–" : sig(x) + " tok/s");
  const plain = (x) => (x == null ? "–" : num(x));
  const shortDay = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });

  // ── DOM helpers (labels are data: always textContent) ──────────────────────
  function svg(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function h(tag, cls, text, parent) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  }
  const r1 = (v) => Math.round(v * 10) / 10;

  // ── tooltip ─────────────────────────────────────────────────────────────────
  function showTip(x, y, head, rows) {
    tip.textContent = "";
    h("div", "tt-head", head, tip);
    for (const row of rows) {
      const line = h("div", "tt-row", null, tip);
      if (row.color) h("span", "tt-key", null, line).style.background = row.color;
      h("b", null, row.value, line);
      if (row.label) h("span", null, row.label, line);
    }
    tip.hidden = false;
    const w = tip.offsetWidth, ht = tip.offsetHeight;
    let left = x + 14, top = y - ht - 12;
    if (left + w > innerWidth - 8) left = x - w - 14;
    if (top < 8) top = y + 18;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.min(innerHeight - ht - 8, top) + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  // ── scales ──────────────────────────────────────────────────────────────────
  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const mag = 10 ** Math.floor(Math.log10(raw || 1));
    const f = raw / mag;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  }
  function scale(domain, range, log) {
    const [d0, d1] = log ? domain.map(Math.log10) : domain;
    const [r0, r1_] = range;
    const f = (v) => {
      let t = log ? Math.log10(Math.max(v, domain[0])) : v;
      t = Math.min(Math.max(t, d0), d1);
      return r0 + ((t - d0) / (d1 - d0 || 1)) * (r1_ - r0);
    };
    f.invert = (px) => {
      const t = d0 + ((px - r0) / (r1_ - r0 || 1)) * (d1 - d0);
      return log ? 10 ** t : t;
    };
    return f;
  }
  function xAxis(series, opts) {
    const xs = [];
    for (const s of series) for (const [x] of s.cdf.points || []) xs.push(x);
    if (opts.domain) return { domain: opts.domain, log: false, ticks: linTicks(opts.domain[0], opts.domain[1]) };
    const max = Math.max(...xs), pos = xs.filter((x) => x > 0), minPos = pos.length ? Math.min(...pos) : 0;
    const log = minPos > 0 && max / minPos >= 100;
    if (log) {  // ends snapped to 1-2-5 steps, ticks on the decades
      const steps = [1, 2, 5, 10];
      const e0 = Math.floor(Math.log10(minPos)), e1 = Math.floor(Math.log10(max));
      const lo = Math.max(...steps.map((k) => k * 10 ** e0).filter((v) => v <= minPos * 1.0001));
      const hi = Math.min(...steps.map((k) => k * 10 ** e1).filter((v) => v >= max * 0.9999));
      const ticks = [];
      for (let t = 10 ** Math.ceil(Math.log10(lo) - 1e-9); t <= hi * 1.0001; t *= 10) ticks.push(sig(t, 6));
      return { domain: [lo, hi], log, ticks };
    }
    const min = Math.min(...xs);
    let lo = min > 0.35 * max ? min : 0;
    const step0 = niceStep((max - lo) || max || 1, 4);
    lo = Math.floor(lo / step0) * step0;
    const hi = Math.max(lo + step0, Math.ceil(max / step0) * step0);
    return { domain: [lo, hi], log, ticks: linTicks(lo, hi) };
  }
  function linTicks(lo, hi) {
    const step = niceStep(hi - lo, 4), out = [];
    for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-6; t += step) out.push(sig(t, 6));
    return out;
  }
  function probAt(points, x) {  // P(X ≤ x) on the step curve
    let lo = 0, hi = points.length - 1, p = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (points[mid][0] <= x) { p = points[mid][1]; lo = mid + 1; } else hi = mid - 1;
    }
    return p;
  }

  // ── CDF chart ───────────────────────────────────────────────────────────────
  function empty(host, text) {
    host.textContent = "";
    h("div", "st-empty", text, host);
  }

  function cdfChart(card, series, opts) {
    const host = $(".st-chart", card);
    series = series.filter((s) => s.cdf && s.cdf.n);
    if (!series.length) return empty(host, opts.emptyText || "No data in this window yet");
    host.textContent = "";
    const W = host.clientWidth, H = host.clientHeight;
    const m = { l: 42, r: 14, t: opts.markers && opts.markers.some((k) => k.hl) ? 26 : 10, b: 24 };
    const ax = xAxis(series, opts);
    const X = scale(ax.domain, [m.l, W - m.r], ax.log), Y = scale([0, 1], [H - m.b, m.t], false);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, tabindex: "0", role: "img",
      "aria-label": `${opts.title}: ` + series.map((s) => `${s.name || ""} median ${opts.fmt(s.cdf.p50)}, ` +
        `p99 ${opts.fmt(s.cdf.p99)}, p99.9 ${opts.fmt(s.cdf.p999)}`).join("; ") }, host);

    // grid + axes (hairline, recessive)
    for (const p of [0, 0.25, 0.5, 0.75, 1]) {
      svg("line", { x1: m.l, x2: W - m.r, y1: r1(Y(p)), y2: r1(Y(p)), stroke: p ? COLOR.grid : COLOR.axis, "stroke-width": 1 }, root);
      svg("text", { x: m.l - 8, y: r1(Y(p)) + 4, "text-anchor": "end" }, root).textContent = p * 100 + "%";
    }
    const ticks = ax.ticks.filter((t) => t >= ax.domain[0] && t <= ax.domain[1]);
    const every = Math.ceil(ticks.length / Math.max(2, Math.floor((W - m.l - m.r) / 52)));
    const tickFmt = opts.tick || opts.fmt;
    ticks.forEach((t, i) => {
      const x = r1(X(t));
      svg("line", { x1: x, x2: x, y1: m.t, y2: H - m.b, stroke: COLOR.grid, "stroke-width": 1 }, root);
      if (i % every) return;
      const anchor = x > W - m.r - 24 ? "end" : x < m.l + 12 ? "start" : "middle";  // edge ticks stay inside
      svg("text", { x, y: H - m.b + 16, "text-anchor": anchor }, root).textContent = tickFmt(t);
    });

    // curves: a light wash under a single curve, then 2px step lines
    series.forEach((s, i) => {
      let d = `M${r1(X(ax.domain[0]))},${r1(Y(0))}`;
      for (const [x, p] of s.cdf.points) d += `H${r1(X(x))}V${r1(Y(p))}`;
      d += `H${r1(X(ax.domain[1]))}`;
      if (series.length === 1) svg("path", { d: d + `V${r1(Y(0))}Z`, fill: s.color, "fill-opacity": 0.08 }, root);
      svg("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
        "data-i": i }, root);
    });

    // percentile markers (first series): a dot on the curve; the highlighted one gets a rule and a label
    const s0 = series[0];
    for (const mk of opts.markers || []) {
      const v = s0.cdf[mk.key];
      if (v == null) continue;
      const x = r1(X(v)), y = r1(Y(mk.p));
      if (mk.hl) {
        svg("line", { x1: x, x2: x, y1: m.t - 4, y2: H - m.b, stroke: COLOR.hl, "stroke-width": 1.5 }, root);
        const g = svg("g", {}, root);
        const label = svg("text", { class: "hl-label", y: m.t - 9, "text-anchor": "middle" }, g);
        label.textContent = `${mk.label} ${opts.fmt(v)}`;
        const tw = label.getComputedTextLength() + 14;
        const cx = Math.min(Math.max(x, m.l + tw / 2), W - m.r - tw / 2 + 10);
        label.setAttribute("x", r1(cx));
        g.insertBefore(svg("rect", { x: r1(cx - tw / 2), y: m.t - 22, width: r1(tw), height: 18, rx: 9, fill: COLOR.hl }), label);
      } else {
        svg("text", { x: x + 8, y: y + 14 }, root).textContent = mk.label;
      }
      svg("circle", { cx: x, cy: y, r: 4, fill: mk.hl ? COLOR.hl : s0.color, stroke: COLOR.surface, "stroke-width": 2 }, root);
    }

    // hover / focus layer: a crosshair that follows the pointer, P(X ≤ x) for every series
    const cross = svg("line", { y1: m.t, y2: H - m.b, stroke: "#8E8E8E", "stroke-width": 1, visibility: "hidden" }, root);
    const dots = series.map((s) => svg("circle", { r: 4, fill: s.color, stroke: COLOR.surface, "stroke-width": 2, visibility: "hidden" }, root));
    const hit = svg("rect", { x: m.l, y: m.t, width: Math.max(0, W - m.l - m.r), height: Math.max(0, H - m.t - m.b), fill: "transparent" }, root);
    let kx = null;
    function at(px, clientX, clientY) {
      px = Math.min(Math.max(px, m.l), W - m.r);
      kx = px;
      const x = X.invert(px);
      cross.setAttribute("x1", r1(px)); cross.setAttribute("x2", r1(px)); cross.setAttribute("visibility", "visible");
      const rows = series.map((s, i) => {
        const p = probAt(s.cdf.points, x);
        dots[i].setAttribute("cx", r1(px)); dots[i].setAttribute("cy", r1(Y(p))); dots[i].setAttribute("visibility", "visible");
        return { color: s.color, value: pct(p * 100), label: (s.name ? s.name + " " : "") + "at or below" };
      });
      showTip(clientX, clientY, `${opts.title}: ${opts.fmt(x)}`, rows);
    }
    function off() {
      cross.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
      hideTip();
    }
    hit.addEventListener("pointermove", (e) => {
      const b = root.getBoundingClientRect();
      at((e.clientX - b.left) * (W / b.width), e.clientX, e.clientY);
    });
    hit.addEventListener("pointerleave", off);
    root.addEventListener("blur", off);
    root.addEventListener("focus", () => {
      const b = root.getBoundingClientRect();
      const px = X(s0.cdf.p50);
      at(px, b.left + px * (b.width / W), b.top + Y(0.5) * (b.height / H));
    });
    root.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const b = root.getBoundingClientRect();
      const px = (kx ?? X(s0.cdf.p50)) + (e.key === "ArrowRight" ? 1 : -1) * (W - m.l - m.r) / 40;
      at(px, b.left + px * (b.width / W), b.top + Y(0.5) * (b.height / H));
    });
  }

  // percentile strip = the chart's table view
  function pcts(card, series, fmt, hlKey) {
    const host = $(".st-pcts", card);
    host.textContent = "";
    for (const s of series) {
      if (!s.cdf || !s.cdf.n) continue;
      if (s.name) h("span", "p-series", s.name, host);
      for (const [key, label] of [["p50", "p50"], ["p90", "p90"], ["p99", "p99"], ["p999", "p99.9"], ["max", "max"]]) {
        const chip = h("span", key === hlKey ? "p-hl" : null, label + " ", host);
        h("b", null, fmt(s.cdf[key]), chip);
      }
      const n = h("span", null, "n ", host);
      h("b", null, num(s.cdf.n), n);
    }
  }

  // ── part of a whole (SVG donut): slices clockwise from 12 o'clock, a 2px surface gap between them; the
  //    legend beside it names each slice with its count and share (the chart's table view) ──
  function pieChart(card, slices, center) {
    const host = $(".st-pie", card);
    host.textContent = "";
    const total = slices.reduce((a, s) => a + s.value, 0);
    if (!total) return empty(host, "Nothing in this window yet");
    const R = 74, W = 30, C = 2 * Math.PI * R, GAP = 2;  // ring radius (mid-stroke), width, circumference
    const root = svg("svg", { viewBox: "0 0 200 200", role: "img", tabindex: "0",
      "aria-label": slices.map((s) => `${s.name}: ${num(s.value)} (${pct(100 * s.value / total)})`).join(", ") }, host);
    svg("circle", { cx: 100, cy: 100, r: R, fill: "none", stroke: COLOR.grid, "stroke-width": W }, root);
    const shown = slices.filter((s) => s.value > 0);
    let at = 0;
    for (const s of shown) {
      const len = C * s.value / total;
      const gap = shown.length > 1 ? Math.min(GAP, len / 2) : 0;
      const arc = svg("circle", { cx: 100, cy: 100, r: R, fill: "none", stroke: s.color, "stroke-width": W,
        "stroke-dasharray": `${r1(len - gap)} ${r1(C)}`, "stroke-dashoffset": r1(-at),
        transform: "rotate(-90 100 100)", class: "pie-slice" }, root);
      const tipRows = () => [{ color: s.color, value: num(s.value), label: `${pct(100 * s.value / total)} of ${center}` }];
      arc.addEventListener("pointermove", (e) => showTip(e.clientX, e.clientY, s.name, tipRows()));
      arc.addEventListener("pointerleave", hideTip);
      at += len;
    }
    const t1 = svg("text", { x: 100, y: 98, "text-anchor": "middle", class: "pie-total" }, root);
    t1.textContent = num(total);
    const t2 = svg("text", { x: 100, y: 118, "text-anchor": "middle", class: "pie-label" }, root);
    t2.textContent = center;
    root.addEventListener("focus", () => {
      const r = root.getBoundingClientRect();
      showTip(r.right, r.top + r.height / 2, `${num(total)} ${center}`,
        slices.map((s) => ({ color: s.color, value: num(s.value), label: `${s.name} · ${pct(100 * s.value / total)}` })));
    });
    root.addEventListener("blur", hideTip);
    const ul = h("ul", "pie-legend", null, host);
    for (const s of slices) {
      const li = h("li", null, null, ul);
      h("i", "pie-key", null, li).style.background = s.color;
      const txt = h("span", "pie-name", s.name, li);
      if (s.note) h("small", "muted", s.note, txt);
      h("b", null, num(s.value), li);
      h("span", "pie-pct", pct(100 * s.value / total), li);
    }
  }

  // ── daily time series (SVG): one value per day, nulls before recording began ──
  function dayChart(card, days, key, title) {
    const host = $(".st-chart", card);
    const pts = days.map((d, i) => [i, d[key]]).filter((p) => p[1] != null);
    if (!pts.length) return empty(host, "No visitors recorded yet");
    host.textContent = "";
    const W = host.clientWidth, H = host.clientHeight, m = { l: 42, r: 34, t: 12, b: 24 };
    const max = Math.max(1, ...pts.map((p) => p[1]));
    const step = Math.max(1, niceStep(max, 4)), top = Math.ceil(max / step) * step;
    const X = scale([0, Math.max(1, days.length - 1)], [m.l, W - m.r], false), Y = scale([0, top], [H - m.b, m.t], false);
    const last = pts[pts.length - 1];
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, tabindex: "0", role: "img",
      "aria-label": `${title}: ${num(last[1])} on ${days[last[0]].day}, peak ${num(max)}` }, host);
    for (let t = 0; t <= top; t += step) {
      svg("line", { x1: m.l, x2: W - m.r, y1: r1(Y(t)), y2: r1(Y(t)), stroke: t ? COLOR.grid : COLOR.axis, "stroke-width": 1 }, root);
      svg("text", { x: m.l - 8, y: r1(Y(t)) + 4, "text-anchor": "end" }, root).textContent = num(t);
    }
    // x labels: month starts over a long span, weekly over a short one
    const long = days.length > 75;
    let lastX = -1e9;
    days.forEach((d, i) => {
      const dt = new Date(d.day + "T12:00:00");
      const mark = long ? dt.getDate() === 1 : (days.length - 1 - i) % 7 === 0;
      const x = X(i);
      if (!mark || x - lastX < 46 || x > W - m.r - 10) return;
      lastX = x;
      svg("line", { x1: r1(x), x2: r1(x), y1: m.t, y2: H - m.b, stroke: COLOR.grid, "stroke-width": 1 }, root);
      svg("text", { x: r1(x), y: H - m.b + 16, "text-anchor": "middle" }, root).textContent = long
        ? dt.toLocaleDateString("en-US", { month: "short" }) + (dt.getMonth() === 0 ? ` '${String(dt.getFullYear()).slice(2)}` : "")
        : shortDay(d.day);
    });
    let d = "";
    pts.forEach(([i, v], k) => { d += `${k ? "L" : "M"}${r1(X(i))},${r1(Y(v))}`; });
    svg("path", { d: d + `L${r1(X(last[0]))},${r1(Y(0))}L${r1(X(pts[0][0]))},${r1(Y(0))}Z`, fill: COLOR.s1, "fill-opacity": 0.08 }, root);
    svg("path", { d, fill: "none", stroke: COLOR.s1, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);
    svg("circle", { cx: r1(X(last[0])), cy: r1(Y(last[1])), r: 4, fill: COLOR.s1, stroke: COLOR.surface, "stroke-width": 2 }, root);
    svg("text", { x: r1(X(last[0])) + 8, y: r1(Y(last[1])) + 4, class: "end-label" }, root).textContent = num(last[1]);

    const cross = svg("line", { y1: m.t, y2: H - m.b, stroke: "#8E8E8E", "stroke-width": 1, visibility: "hidden" }, root);
    const dot = svg("circle", { r: 4, fill: COLOR.s1, stroke: COLOR.surface, "stroke-width": 2, visibility: "hidden" }, root);
    const hit = svg("rect", { x: m.l, y: m.t, width: Math.max(0, W - m.l - m.r), height: Math.max(0, H - m.t - m.b), fill: "transparent" }, root);
    let ki = last[0];
    function at(i, clientX, clientY) {
      i = Math.min(Math.max(Math.round(i), pts[0][0]), last[0]);
      ki = i;
      const v = days[i][key], x = r1(X(i));
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x); dot.setAttribute("cy", r1(Y(v ?? 0))); dot.setAttribute("visibility", "visible");
      showTip(clientX, clientY, shortDay(days[i].day), [{ color: COLOR.s1, value: num(v), label: title }]);
    }
    function off() { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); }
    hit.addEventListener("pointermove", (e) => {
      const b = root.getBoundingClientRect();
      at(X.invert((e.clientX - b.left) * (W / b.width)), e.clientX, e.clientY);
    });
    hit.addEventListener("pointerleave", off);
    root.addEventListener("blur", off);
    const key_ = (e) => {
      if (e.type === "keydown" && e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (e.type === "keydown") e.preventDefault();
      const i = e.type === "focus" ? last[0] : ki + (e.key === "ArrowRight" ? 1 : -1);
      const b = root.getBoundingClientRect();
      at(i, b.left + X(i) * (b.width / W), b.top + Y(days[Math.min(Math.max(i, pts[0][0]), last[0])][key] ?? 0) * (b.height / H));
    };
    root.addEventListener("focus", key_);
    root.addEventListener("keydown", key_);
  }

  function dayStrip(card, v, key) {
    const host = $(".st-pcts", card);
    host.textContent = "";
    const vals = v.days.map((d) => d[key]).filter((x) => x != null);
    if (!vals.length) return;
    const last30 = vals.slice(-30);
    const items = [["today", num(v[key])], ["peak", `${num(v["peak_" + key].n)} · ${shortDay(v["peak_" + key].day)}`],
                   ["30-day avg", num(last30.reduce((a, b) => a + b, 0) / last30.length)], ["days", num(vals.length)]];
    for (const [label, value] of items) {
      const chip = h("span", null, label + " ", host);
      h("b", null, value, chip);
    }
  }

  // ── bars (HTML) ─────────────────────────────────────────────────────────────
  function bars(ol, rows) {
    ol.textContent = "";
    const max = Math.max(...rows.map((r) => r.value), 0) || 1;
    for (const r of rows) {
      const li = h("li", r.upstream ? "b-up" : null, null, ol);
      const label = h("span", "b-label", r.label, li);
      if (r.sub) h("small", null, r.sub, label);
      const track = h("span", "b-track", null, li);
      h("span", "b-fill", null, track).style.width = (100 * r.value / max).toFixed(2) + "%";
      const val = h("span", "b-val", r.text, li);
      if (r.small) h("small", null, r.small, val);
      li.title = r.title || "";
    }
  }

  // ── daily columns (SVG) ─────────────────────────────────────────────────────
  function columns(host, daily) {
    host.textContent = "";
    if (!daily.length) return;
    const W = host.clientWidth, H = host.clientHeight, m = { l: 30, r: 4, t: 8, b: 20 };
    const max = Math.max(1, ...daily.map((d) => d.n));
    const step = Math.max(1, niceStep(max, 3)), top = Math.ceil(max / step) * step;
    const Y = scale([0, top], [H - m.b, m.t], false);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": "Rejected requests per day: " + daily.map((d) => `${d.day} ${d.n}`).join(", ") }, host);
    for (let t = 0; t <= top; t += step) {
      svg("line", { x1: m.l, x2: W - m.r, y1: r1(Y(t)), y2: r1(Y(t)), stroke: t ? COLOR.grid : COLOR.axis, "stroke-width": 1 }, root);
      svg("text", { x: m.l - 6, y: r1(Y(t)) + 3, "text-anchor": "end" }, root).textContent = num(t);
    }
    const slot = (W - m.l - m.r) / daily.length, bw = Math.max(2, Math.min(24, slot - 2));
    const label = new Set([0, daily.length - 1, Math.floor((daily.length - 1) / 2)]);
    daily.forEach((d, i) => {
      const x = m.l + i * slot + (slot - bw) / 2;
      const g = svg("g", { class: "col" }, root);
      if (d.n > 0) {
        const y = Y(d.n), ht = Y(0) - y, r = Math.min(4, bw / 2, ht);
        svg("path", { class: "bar", fill: COLOR.s1,
          d: `M${r1(x)},${r1(Y(0))}V${r1(y + r)}Q${r1(x)},${r1(y)} ${r1(x + r)},${r1(y)}H${r1(x + bw - r)}` +
             `Q${r1(x + bw)},${r1(y)} ${r1(x + bw)},${r1(y + r)}V${r1(Y(0))}Z` }, g);
      }
      if (label.has(i)) svg("text", { x: r1(x + bw / 2), y: H - 4, "text-anchor": "middle" }, root).textContent = shortDay(d.day);
      const hit = svg("rect", { x: r1(m.l + i * slot), y: m.t, width: r1(slot), height: r1(H - m.t - m.b), fill: "transparent" }, g);
      hit.addEventListener("pointermove", (e) => showTip(e.clientX, e.clientY, shortDay(d.day),
        [{ color: COLOR.s1, value: num(d.n), label: d.n === 1 ? "request refused" : "requests refused" }]));
      hit.addEventListener("pointerleave", hideTip);
    });
  }

  // ── page ────────────────────────────────────────────────────────────────────
  function tiles(d) {
    const host = $("#tiles");
    host.textContent = "";
    const today = d.cache.days.length ? d.cache.days[d.cache.days.length - 1] : null;
    const items = [
      // cold = the lookup waited for an analysis; cache hits are answered in milliseconds
      { label: "Cold lookups", value: num(d.cold.total), sub: `of ${num(d.tickers.total)} ticker queries` },
      { label: "Peak cold RPM", value: num(d.cold.rpm.max), sub: `p50 ${num(d.cold.rpm.p50)}/min` },
      { label: "Ticker queries", value: num(d.tickers.total), sub: `${num(d.tickers.distinct)} tickers` },
      { label: "Cold latency p99.9", value: dur(d.latency.p999), sub: `p50 ${dur(d.latency.p50)}`, hl: true },
      { label: "Cache hit rate", value: pct(d.cache.hit_rate), sub: today ? `latest day ${pct(today.rate)}` : "–" },
      { label: "Rejected", value: pct(d.rejections.rate), sub: `${num(d.rejections.total)} requests` },
      { label: "DAU today", value: num(d.visitors.dau), sub: d.visitors.peak_dau ? `peak ${num(d.visitors.peak_dau.n)}` : "–" },
      { label: "MAU (30 d)", value: num(d.visitors.mau),
        sub: d.visitors.mau ? `DAU/MAU ${pct(100 * d.visitors.dau / d.visitors.mau)}` : "–" },
    ];
    for (const t of items) {
      const tile = h("div", "st-tile" + (t.hl ? " t-hl" : ""), null, host);
      h("span", "t-label", t.label, tile);
      h("span", "t-value", t.value, tile);
      h("span", "t-sub", t.sub, tile);
    }
  }

  function render(d) {
    const span = d.days === 1 ? "Last 24 hours" : `Last ${d.days} days`;
    const at = new Date(d.generated_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    $("#st-sub").textContent = `${span}${d.mode === "public" ? " · public portal (visitors by CF-Connecting-IP)" : ""}` +
      ` · updated ${at} ${d.tz || ""} · refreshes every minute`;
    $("#st-retention").textContent = `Local network only · data kept ${d.retention_days} days, ` +
      `visitors ${d.visitors.retention_days} days (rolling)`;
    tiles(d);

    const lookups = d.tickers.total || 0, requests = d.rejections.requests || 0;
    pieChart($("#c-mix"), [
      { name: "Ticker lookups", value: Math.min(lookups, requests), color: COLOR.s2 },
      { name: "Other actions", note: "portal visits, scans opened", value: Math.max(0, requests - lookups), color: COLOR.s1 },
    ], "actions");

    const lat = $("#c-latency");
    cdfChart(lat, [{ color: COLOR.s1, cdf: d.latency }], { title: "Latency", fmt: dur, tick: durTick,
      markers: [{ key: "p50", p: 0.5, label: "p50" }, { key: "p999", p: 0.999, label: "p99.9", hl: true }] });
    pcts(lat, [{ cdf: d.latency }], dur, "p999");

    const cache = $("#c-cache");
    cdfChart(cache, [{ color: COLOR.s1, cdf: d.cache.cdf }], { title: "Daily hit rate", fmt: pct, tick: pctTick, domain: [0, 100],
      markers: [{ key: "p50", p: 0.5, label: "p50" }] });
    pcts(cache, [{ cdf: d.cache.cdf }], pct);
    let mix = $(".st-mix", cache);
    if (!mix) mix = h("p", "muted small st-foot st-mix", null, cache);
    const q = d.tickers.total || 0, s = d.sources;
    mix.textContent = q ? `Lookups answered from: cache ${pct(100 * s.cache / q)} · new analysis ${pct(100 * (s.fresh + s.refresh) / q)}` +
      ` · joined one in progress ${pct(100 * s.joined / q)} · recent result ${pct(100 * s.recent / q)}` : "";

    const tk = $("#c-tickers");
    if (d.tickers.top.length) {
      bars($(".st-bars", tk), d.tickers.top.map((t) => ({ label: t.ticker, value: t.pct, text: pct(t.pct), small: num(t.n),
        title: `${t.ticker}: ${num(t.n)} of ${num(d.tickers.total)} queries` })));
    } else {
      $(".st-bars", tk).textContent = "";
      h("li", "st-none", "No ticker queries in this window yet", $(".st-bars", tk));
    }
    $(".st-foot", tk).textContent = d.tickers.total
      ? `${num(d.tickers.total)} queries · ${num(d.tickers.distinct)} distinct tickers` : "";

    const rj = $("#c-reject");
    const reasons = d.rejections.reasons;
    if (reasons.length) {
      bars($(".st-bars", rj), reasons.map((r) => ({ label: r.label, sub: r.upstream ? `${r.code} · upstream, not a request` : `HTTP ${r.code}`,
        value: r.n, text: num(r.n), small: r.pct == null ? "" : pct(r.pct), upstream: r.upstream,
        title: r.upstream ? "Yahoo Finance refusing the portal's own calls (the lookup queue pauses)" : `${pct(r.pct)} of all requests` })));
    } else {
      $(".st-bars", rj).textContent = "";
      h("li", "st-none", "No requests refused in this window", $(".st-bars", rj));
    }
    columns($(".st-cols", rj), d.rejections.daily);

    const v = d.visitors;
    $("#visitors-sub").textContent = `Distinct source IPs over the last 12 months (kept ${v.retention_days} days, ` +
      `so the range switch doesn't apply)` + (v.since ? ` · recording since ${shortDay(v.since)}` : "");
    dayChart($("#c-dau"), v.days, "dau", "active users");
    dayStrip($("#c-dau"), v, "dau");
    dayChart($("#c-mau"), v.days, "mau", "active in 30 days");
    dayStrip($("#c-mau"), v, "mau");

    const ai = d.ai;
    $("#ai-sub").textContent = ai.n
      ? `${num(ai.done)} overview${ai.done === 1 ? "" : "s"} generated · ${num(ai.failed)} failed` +
        (ai.models.length ? ` · ${ai.models.join(", ")}` : "") +
        ` · queue wait p50 ${dur(ai.wait.p50)}, p90 ${dur(ai.wait.p90)} · cached overviews cost nothing and are not counted`
      : "No AI overviews generated in this window (the portal may be running with --no-ai)";
    for (const [id, key, fmt, title] of [["#c-ttft", "ttft", dur, "TTFT"], ["#c-speed", "speed", rate, "Speed"],
                                         ["#c-total", "total", dur, "Total time"]]) {
      const card = $(id);
      cdfChart(card, [{ color: COLOR.s1, cdf: ai[key] }], { title, fmt, tick: key === "speed" ? plain : durTick,
        markers: [{ key: "p50", p: 0.5, label: "p50" }],
        emptyText: "No AI overviews in this window" });
      pcts(card, [{ cdf: ai[key] }], fmt);
    }
  }

  async function load() {
    main.classList.add("loading");
    const chip = $("#chip-live");
    try {
      const r = await fetch(`/api/stats?days=${days}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      data = await r.json();
      render(data);
      chip.className = "chip on";
    } catch (e) {
      chip.className = "chip off";
      $("#st-sub").textContent = `Stats unavailable (${e.message}) — retrying every minute`;
    } finally {
      main.classList.remove("loading");
    }
  }

  function setDays(n, push) {
    days = n;
    for (const b of document.querySelectorAll(".st-range button")) b.setAttribute("aria-pressed", String(+b.dataset.days === n));
    if (push) history.replaceState(null, "", n === 30 ? location.pathname : `?days=${n}`);
    load();
  }

  for (const b of document.querySelectorAll(".st-range button")) b.addEventListener("click", () => setDays(+b.dataset.days, true));
  let pending = 0;
  addEventListener("resize", () => {
    if (!data || pending) return;
    pending = requestAnimationFrame(() => { pending = 0; render(data); });
  });
  addEventListener("scroll", hideTip, { passive: true });
  setInterval(() => { if (document.visibilityState === "visible") load(); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(); });
  const q = +new URLSearchParams(location.search).get("days");
  setDays([1, 7, 30].includes(q) ? q : 30, false);
})();
