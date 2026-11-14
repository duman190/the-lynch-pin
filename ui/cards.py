"""Link preview cards: what X, Threads, iMessage, Slack or Discord show when someone shares a portal link.

Their crawlers don't run JavaScript, so the page's ``<head>`` carries the card itself (``link_meta``): Open
Graph and Twitter tags written by the server for ``/?t=MSFT``, ``/?scan=mags`` and the home page. A
ticker's image is ``/card/MSFT.jpg``, 1200×630 (the 1.91:1 X and Open Graph want), drawn here with Pillow
from today's cached analysis: ticker and name, PEG and its deviation from the stock's own 5-year mean, the
5Y ROI cases, growth and grades, and the PEG bell curve with today's PEG marked.

A card never starts an analysis: a crawler must not spend Yahoo or FMP calls. A ticker nobody analysed today
gets a generic card (the hero artwork, the ticker and what the analysis covers); people share a ticker after
looking it up, so its analysis is normally in the cache. Cards are kept in memory per (ticker, analysis), so a
crawler stampede draws each one once.
"""
import collections
import datetime as _dt
import html
import io
import os
import re
import threading

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = (18, 18, 18)
PANEL = (28, 28, 30)
WHITE = (240, 240, 240)
MUTED = (160, 164, 170)
BLUE = (93, 156, 236)      # the charts' sky blue
GREEN = (76, 199, 120)
RED = (255, 99, 88)
SITE = "The Lynch Pin"
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "img")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(?::\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](?::\d{1,5})?$")


# ── fonts ────────────────────────────────────────────────────────────────────
_fonts = {}


def _font(size, bold=False):
    """DejaVu Sans from the matplotlib install (always there: the charts use it), else Pillow's own."""
    key = (size, bold)
    if key not in _fonts:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        try:
            import matplotlib
            path = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf", name)
            _fonts[key] = ImageFont.truetype(path, size)
        except (ImportError, OSError):
            _fonts[key] = ImageFont.load_default(size)
    return _fonts[key]


def _fit(draw, text, size, max_w, bold=False, min_size=18):
    """The largest font (down to ``min_size``) that fits ``text`` in ``max_w``; then the text is cut with an ellipsis."""
    while size > min_size and draw.textlength(text, font=_font(size, bold)) > max_w:
        size -= 2
    f = _font(size, bold)
    if draw.textlength(text, font=f) > max_w:
        while text and draw.textlength(text + "…", font=f) > max_w:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, f


def _wrap(draw, text, font, max_w, max_lines):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            line = trial
            continue
        lines.append(line)
        line = word
        if len(lines) == max_lines:
            break
    if len(lines) < max_lines and line:
        lines.append(line)
    return lines[:max_lines]


# ── data → text ──────────────────────────────────────────────────────────────
def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _stats(entry):
    """The analysis' numbers the card shows, or None when there is no GARP valuation to show."""
    if not entry or entry.get("status") != "done" or not entry.get("stats"):
        return None
    st = entry["stats"]
    if _num(st.get("PEG")) is None or _num(st.get("Base")) is None:
        return None
    return st


def ticker_text(sym, entry):
    """(title, description) of a ticker's card."""
    st = _stats(entry)
    name = (entry or {}).get("name") or sym
    if st is None:
        reason = (entry or {}).get("reason") if (entry or {}).get("status") == "nodata" else None
        title = f"${sym} · GARP analysis | {SITE}"
        desc = (f"{name}: {reason}." if reason else
                f"{name}: PEG against its own 5-year history, 5Y bull / base / bear ROI, income grade, "
                "credit rating, technicals and an AI overview.")
        return title, desc + " Not financial advice."
    peg, base = st["PEG"], st["Base"]
    title = f"${sym} · {name}: PEG {peg:.2f}, 5Y base ROI {base:.1f}% | {SITE}"
    parts = []
    if _num(st.get("Dev_SD")) is not None and _num(st.get("Mean")) is not None and st.get("history") == "ok":
        parts.append(f"{st['Dev_SD']:+.1f} SD vs its 5Y mean PEG {st['Mean']:.2f}")
    if _num(st.get("Bull")) is not None and _num(st.get("Bear")) is not None:
        parts.append(f"5Y ROI bull / base / bear {st['Bull']:.0f} / {base:.0f} / {st['Bear']:.0f}%")
    if _num(st.get("growth_pct")) is not None:
        parts.append(f"5Y EPS growth {st['growth_pct']:.1f}%")
    grades = [f"Income {entry['income']['grade']}" if (entry.get("income") or {}).get("grade") else None,
              f"Credit {entry['credit']['rating']}" if (entry.get("credit") or {}).get("rating") else None]
    parts += [g for g in grades if g]
    return title, " · ".join(parts) + ". Not financial advice."


# ── drawing ──────────────────────────────────────────────────────────────────
def _badge_panel(img, draw):
    """The right-hand panel of a card without an analysis: the Lynch Pin badge."""
    px0, py0, px1, py1 = 700, 40, W - 40, H - 76
    draw.rounded_rectangle([px0, py0, px1, py1], radius=18, fill=PANEL, outline=(52, 52, 56), width=2)
    try:
        badge = Image.open(os.path.join(STATIC, "logo.png")).convert("RGBA")
        side = min(px1 - px0, py1 - py0) - 40
        badge = badge.resize((side, side), Image.LANCZOS)
        img.paste(badge, (px0 + (px1 - px0 - side) // 2, py0 + (py1 - py0 - side) // 2), badge)
    except OSError:
        pass


def _brand(img, draw, x=56, y=44):
    try:
        logo = Image.open(os.path.join(STATIC, "logo-132.png")).convert("RGBA").resize((52, 52), Image.LANCZOS)
        img.paste(logo, (x, y), logo)
    except OSError:
        pass
    draw.text((x + 66, y + 2), "THE LYNCH PIN", font=_font(24, True), fill=WHITE)
    draw.text((x + 66, y + 30), "Quant Portal · Growth at a Reasonable Price", font=_font(17), fill=MUTED)


def _footer(draw, *parts):
    """The bottom line, right-aligned: X lays the link's domain over the image's bottom-left corner."""
    draw.rectangle([0, H - 8, W, H], fill=BLUE)
    text = " · ".join(p for p in parts if p)
    draw.text((W - 56 - draw.textlength(text, font=_font(18)), H - 50), text, font=_font(18), fill=MUTED)


def _jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True, progressive=True)
    return buf.getvalue()


def render_generic(sym=None):
    """The card of the home page (``sym`` None) or of a ticker without today's analysis."""
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    _badge_panel(img, draw)
    _brand(img, draw)
    if sym:
        text, f = _fit(draw, f"${sym}", 110, 600, bold=True)
        draw.text((56, 140), text, font=f, fill=WHITE)
        draw.text((58, 272), "GARP analysis", font=_font(34, True), fill=BLUE)
        blurb = ("PEG against its own 5-year history, 5Y bull / base / bear ROI, income grade, "
                 "credit rating and an AI overview.")
        y = 330
    else:
        draw.text((56, 150), "Find growth at a", font=_font(58, True), fill=WHITE)
        draw.text((56, 222), "reasonable price.", font=_font(58, True), fill=BLUE)
        blurb = ("Any US stock's PEG against its own 5-year history, 5Y ROI cases, quality grades "
                 "and an AI overview.")
        y = 320
    for line in _wrap(draw, blurb, _font(28), 600, 4):
        draw.text((56, y), line, font=_font(28), fill=(205, 208, 214))
        y += 40
    _footer(draw, "Quant scans, not financial advice")
    return _jpeg(img)


def _draw_bell(img, box, peg, mean, sd, dev):
    """The PEG bell curve the charts draw (the stock's 5-year PEG distribution, today's PEG marked with its
    deviation), drawn here at 3x and scaled down for smooth edges. Today's PEG is always in frame."""
    import math
    x0, y0, x1, y1 = box
    k = 3
    bw, bh = (x1 - x0) * k, (y1 - y0) * k
    layer = Image.new("RGB", (bw, bh), PANEL)
    d = ImageDraw.Draw(layer)
    lo, hi = mean - 3 * sd, mean + 3 * sd
    lo, hi = min(lo, peg - 0.6 * sd), max(hi, peg + 0.6 * sd)  # a far-off PEG widens the axis
    pad_l, pad_r, top, base = 24 * k, 24 * k, 70 * k, bh - 70 * k
    px = lambda v: pad_l + (v - lo) / (hi - lo) * (bw - pad_l - pad_r)  # noqa: E731
    pdf = lambda v: math.exp(-0.5 * ((v - mean) / sd) ** 2)  # noqa: E731 (peak = 1)
    py = lambda v: base - pdf(v) * (base - top)  # noqa: E731
    xs = [lo + (hi - lo) * i / 400 for i in range(401)]
    curve = [(px(v), py(v)) for v in xs]
    # the area under the curve: a vertical gradient, lighter at the top like the charts' stacked fills
    grad = Image.new("RGB", (bw, bh))
    gd = ImageDraw.Draw(grad)
    for yy in range(top, base + 1):
        f = (yy - top) / max(1, base - top)
        gd.line([(0, yy), (bw, yy)], fill=(int(120 - 78 * f), int(158 - 90 * f), int(210 - 104 * f)))
    mask = Image.new("L", (bw, bh))
    ImageDraw.Draw(mask).polygon(curve + [(curve[-1][0], base), (curve[0][0], base)], fill=255)
    layer.paste(grad, (0, 0), mask)
    # 1 and 2 SD guides
    for m in (-2, -1, 0, 1, 2):
        v = mean + m * sd
        if lo <= v <= hi:
            d.line([(px(v), py(v)), (px(v), base)], fill=(150, 170, 200) if m else (190, 205, 225), width=k)
    d.line(curve, fill=(70, 110, 170), width=12 * k, joint="curve")  # glow
    d.line(curve, fill=WHITE, width=4 * k, joint="curve")
    d.line([(pad_l, base), (bw - pad_r, base)], fill=(70, 70, 76), width=k)
    # axis labels: the mean and +-1 / 2 SD in PEG
    lab = _font(15 * k)
    for m in (-2, -1, 0, 1, 2):
        v = mean + m * sd
        if lo <= v <= hi and v > 0:
            t = f"{v:.2f}"
            d.text((px(v) - d.textlength(t, font=lab) / 2, base + 10 * k), t, font=lab, fill=MUTED)
    t = "PEG RATIO · 5-YEAR HISTORY"
    d.text(((bw - d.textlength(t, font=_font(14 * k, True))) / 2, base + 36 * k), t, font=_font(14 * k, True),
           fill=(120, 124, 130))
    # today's PEG: dashed red line, dot, badge
    mx, my = px(peg), py(peg)
    yy = base
    while yy > my:
        d.line([(mx, yy), (mx, max(my, yy - 8 * k))], fill=RED, width=2 * k)
        yy -= 14 * k
    r = 9 * k
    d.ellipse([mx - r, my - r, mx + r, my + r], fill=RED, outline=WHITE, width=2 * k)
    badge = f"{dev:+.1f} SD" if dev is not None else f"PEG {peg:.2f}"
    bf = _font(20 * k, True)
    tw = d.textlength(badge, font=bf)
    bx = min(max(mx - tw / 2 - 12 * k, 8 * k), bw - tw - 32 * k)
    by = max(my - 58 * k, 8 * k)
    d.rounded_rectangle([bx, by, bx + tw + 24 * k, by + 36 * k], radius=8 * k, fill=RED)
    d.text((bx + 12 * k, by + 5 * k), badge, font=bf, fill=WHITE)
    img.paste(layer.resize((x1 - x0, y1 - y0), Image.LANCZOS), (x0, y0))


def render_ticker(sym, entry):
    """A ticker's card from today's analysis (``render_generic`` when it has no GARP valuation)."""
    st = _stats(entry)
    if st is None:
        return render_generic(sym)
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # the PEG bell curve on the right, in a panel (the Lynch Pin badge when there is no 5-year history)
    dev, mean, sd = _num(st.get("Dev_SD")), _num(st.get("Mean")), _num(st.get("SD"))
    history = st.get("history") == "ok" and dev is not None and mean is not None and sd
    if history:
        px0, py0, px1, py1 = 640, 40, W - 40, H - 76
        draw.rounded_rectangle([px0, py0, px1, py1], radius=18, fill=PANEL, outline=(52, 52, 56), width=2)
        _draw_bell(img, (px0 + 3, py0 + 3, px1 - 3, py1 - 3), st["PEG"], mean, sd, dev)
    else:
        _badge_panel(img, draw)

    _brand(img, draw)
    text, f = _fit(draw, f"${sym}", 96, 560, bold=True)
    draw.text((56, 118), text, font=f, fill=WHITE)
    name, f = _fit(draw, entry.get("name") or sym, 28, 560)
    draw.text((58, 228), name, font=f, fill=MUTED)

    def tile(x, y, label, value, color=WHITE, sub=None):
        draw.text((x, y), label, font=_font(17, True), fill=MUTED)
        draw.text((x, y + 24), value, font=_font(46, True), fill=color)
        if sub:
            draw.text((x, y + 80), sub, font=_font(17), fill=MUTED)

    tile(56, 286, "PEG", f"{st['PEG']:.2f}", WHITE,
         f"{dev:+.1f} SD vs 5Y mean {mean:.2f}" if history else "no 5Y history")
    base = st["Base"]
    bull, bear = _num(st.get("Bull")), _num(st.get("Bear"))
    tile(330, 286, "5Y BASE ROI", f"{base:.1f}%", GREEN if base >= 9 else RED,
         f"bull {bull:.0f}% · bear {bear:.0f}%" if bull is not None and bear is not None else None)

    facts = []
    if _num(st.get("growth_pct")) is not None:
        facts.append(f"5Y growth {st['growth_pct']:.1f}%")
    if (entry.get("income") or {}).get("grade"):
        facts.append(f"Income {entry['income']['grade']}")
    if (entry.get("credit") or {}).get("rating"):
        facts.append(f"Credit {entry['credit']['rating']}")
    if facts:
        line, f = _fit(draw, "  ·  ".join(facts), 24, 560)
        draw.text((56, 420), line, font=f, fill=(205, 208, 214))
    if history:
        verdict = ("Cheap vs its own history" if dev <= -1 else "Below its usual PEG" if dev < 0
                   else "Above its usual PEG" if dev < 1 else "Expensive vs its own history")
        draw.text((56, 462), verdict, font=_font(24, True), fill=GREEN if dev < 0 else RED)

    price = _num(entry.get("price"))
    when = _dt.datetime.fromtimestamp(entry["generated_at"]).strftime("%b %d, %Y") \
        if _num(entry.get("generated_at")) else None
    _footer(draw, f"${price:,.2f}" if price is not None else None, when, "Quant scan, not financial advice")
    return _jpeg(img)


# ── cache ────────────────────────────────────────────────────────────────────
class CardCache:
    """Rendered cards in memory, keyed by (ticker, analysis time): a re-analysis gets a new card."""

    def __init__(self, limit=256):
        self.limit = limit
        self._lock = threading.Lock()
        self._items = collections.OrderedDict()

    def get(self, key, render):
        with self._lock:
            body = self._items.get(key)
            if body is not None:
                self._items.move_to_end(key)
                return body
        body = render()  # outside the lock: two crawlers at once may both draw it, which is harmless
        with self._lock:
            self._items[key] = body
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
        return body


def card_version(entry):
    """What a ticker card's URL carries, so a new analysis gets a new image URL (crawlers cache by URL)."""
    st = _stats(entry)
    return f"{int(entry['generated_at'])}" if st is not None and _num(entry.get("generated_at")) else "g"


# ── <head> ───────────────────────────────────────────────────────────────────
def origin(host, https):
    """The absolute origin for the card's URLs from the request's Host (already vetted by the server's
    guard), or None when the header is unusable."""
    host = (host or "").strip()
    if not HOST_RE.match(host):
        return None
    return f"{'https' if https else 'http'}://{host}"


def meta_tags(title, description, image, url, image_alt):
    """The Open Graph + Twitter tags for a page; every value HTML-escaped."""
    e = lambda v: html.escape(v, quote=True)  # noqa: E731
    tags = [("property", "og:type", "website"), ("property", "og:site_name", SITE),
            ("property", "og:title", title), ("property", "og:description", description),
            ("property", "og:url", url), ("property", "og:image", image),
            ("property", "og:image:width", str(W)), ("property", "og:image:height", str(H)),
            ("property", "og:image:alt", image_alt),
            ("name", "twitter:card", "summary_large_image"), ("name", "twitter:title", title),
            ("name", "twitter:description", description), ("name", "twitter:image", image),
            ("name", "twitter:image:alt", image_alt), ("name", "description", description)]
    return "\n".join(f'  <meta {a}="{e(k)}" content="{e(v)}">' for a, k, v in tags) + "\n"


def link_meta(base, query, lookup=None, scans=None, ticker_re=None):
    """The ``<head>`` tags for the portal page at ``/?query``. ``lookup(sym)`` = today's cached analysis
    (no side effects); ``scans`` = the scan archive. ``base`` = the absolute origin."""
    sym = ((query.get("t") or [""])[0] or "").strip().upper()
    if sym and ticker_re is not None and ticker_re.match(sym):
        entry = lookup(sym) if lookup else None
        title, desc = ticker_text(sym, entry)
        return meta_tags(title, desc, f"{base}/card/{sym}.jpg?v={card_version(entry)}", f"{base}/?t={sym}",
                         f"{sym} GARP valuation card: PEG, deviation from its 5-year mean and 5Y ROI cases")
    kind = (query.get("scan") or [""])[0]
    if kind and scans is not None:
        doc = scans.get(kind)
        if doc:
            head = doc["posts"][0]
            first = " ".join((head.get("text") or "").split())
            desc = (first[:220].rsplit(" ", 1)[0] + "…") if len(first) > 220 else first
            image = (base + head["image"]["src"]) if head.get("image") else f"{base}/card/home.jpg"
            return meta_tags(f"{doc['title']} scan · {doc['date']} | {SITE}", desc or doc.get("subtitle") or SITE,
                             image, f"{base}/?scan={kind}", f"{doc['title']} scan chart")
    return meta_tags(f"{SITE} · Quant Portal",
                     "Find growth at a reasonable price: any US stock's PEG against its own 5-year history, "
                     "5Y ROI cases, quality grades and an AI overview. Not financial advice.",
                     f"{base}/card/home.jpg", f"{base}/", "The Lynch Pin Quant Portal")
