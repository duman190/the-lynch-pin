"""Renders the Quant Portal artwork into ui/static/img/ from the Lynch Pin logo.

Run once (or whenever the logo changes):

    python -m ui.assets.make_hero

Outputs (the originals in tmp/ are only read, never modified):
  hero_wide.jpg/.webp    2400x900  desktop hero: logo badge + glow rings + candles + PEG bell curve + title
  hero_square.jpg/.webp  1200x1200 phone hero: same composition, stacked
  logo.png         512x512  circular logo with transparent corners
  icon-192.png / icon-512.png / apple-touch-icon.png   home-screen icons
  icon-512-maskable.png  badge at 78% on #121212 (Android adaptive-icon safe zone)
  banner.jpg       resized copy of the X banner (about section)

The drawing vocabulary is copied from graphics/visualizer.py so the portal looks like
the charts it serves: #121212 canvas, #5D9CEC sky-blue fill with a white top glow,
a bold white curve with blue halo, white SD gridlines and the #FF4B2B red marker + badge.
"""
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import scipy.stats as stats  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_LOGO = os.path.join(ROOT, "tmp", "x_logo.jpeg")
SRC_BANNER = os.path.join(ROOT, "tmp", "x_banner.png")
OUT = os.path.join(ROOT, "ui", "static", "img")

BG = "#121212"
SKY = "#5D9CEC"
WHITE = "#FFFFFF"
RED = "#FF4B2B"
GRID = "#252525"


def circular_logo(size):
    """Logo cropped to its round badge with transparent corners (RGBA)."""
    img = Image.open(SRC_LOGO).convert("RGBA")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((6, 6, size * 4 - 6, size * 4 - 6), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    img.putalpha(mask)
    return img


def _candles(ax, x0, x1, y0, y1, n=34, seed=7):
    """Faint candlestick backdrop (like the badge's own background)."""
    rng = np.random.default_rng(seed)
    closes = np.cumsum(rng.normal(0.15, 1.0, n))
    opens = np.r_[closes[0] - 0.5, closes[:-1]]
    highs = np.maximum(opens, closes) + rng.uniform(0.2, 1.1, n)
    lows = np.minimum(opens, closes) - rng.uniform(0.2, 1.1, n)
    lo, hi = lows.min(), highs.max()
    scale = lambda v: y0 + (v - lo) / (hi - lo) * (y1 - y0)  # noqa: E731
    xs = np.linspace(x0, x1, n)
    w = (x1 - x0) / n * 0.55
    for x, o, c, h, low in zip(xs, opens, closes, highs, lows):
        col = SKY if c >= o else "#8A8A8A"
        ax.vlines(x, scale(low), scale(h), color=col, lw=1.2, alpha=0.28, zorder=1)
        ax.add_patch(plt.Rectangle((x - w / 2, scale(min(o, c))), w, max(abs(scale(c) - scale(o)), 0.004),
                                   facecolor=col, edgecolor="none", alpha=0.30, zorder=1))


def _bell(ax, cx, width, base, height, z=-2.1):
    """Glowing PEG bell curve with the red current-PEG marker, in figure-fraction coords."""
    x = np.linspace(-4, 4, 500)
    y = stats.norm.pdf(x)
    y = y / y.max()
    X = cx + x / 8 * width
    Y = base + y * height
    ax.fill_between(X, base, Y, color=SKY, alpha=0.45, zorder=3)
    for level in np.linspace(0, 1, 60):
        ax.fill_between(X, base + level * height, Y, where=(y > level),
                        color=WHITE, alpha=(level ** 4.0) * 0.06, zorder=3)
    for i in range(-3, 4):
        xp = cx + i / 8 * width
        h = base + stats.norm.pdf(i) / stats.norm.pdf(0) * height
        ax.vlines(xp, base, h, color=WHITE, lw=1.1, alpha=0.5, zorder=4)
        ax.vlines(xp, base, h, color=WHITE, lw=6, alpha=0.05, zorder=4)
    ax.plot(X, Y, color=WHITE, lw=3.2, zorder=7)
    ax.plot(X, Y, color=WHITE, lw=10, alpha=0.15, zorder=6)
    ax.plot(X, Y, color=SKY, lw=20, alpha=0.12, zorder=5)
    ax.hlines(base, X[0], X[-1], color="#333333", lw=1.2, zorder=2)
    mx = cx + z / 8 * width
    my = base + stats.norm.pdf(z) / stats.norm.pdf(0) * height
    ax.vlines(mx, base, my, color=RED, linestyle="--", lw=2.2, zorder=8)
    ax.scatter([mx], [my], color=RED, s=150, zorder=9, edgecolor=WHITE, lw=2)
    ax.text(mx, my + height * 0.14, f" {z} SD ", color=WHITE, fontweight="bold", fontsize=13,
            ha="center", zorder=10, bbox=dict(facecolor=RED, edgecolor="none", boxstyle="round,pad=0.4"))


def _grid(ax, fig_w, fig_h, step=0.035):
    for gx in np.arange(0, 1, step * fig_h / fig_w):
        ax.axvline(gx, color=GRID, lw=0.8, alpha=0.55, zorder=0)
    for gy in np.arange(0, 1, step):
        ax.axhline(gy, color=GRID, lw=0.8, alpha=0.55, zorder=0)


def _glow_rings(ax, cx, cy, r, aspect):
    """Concentric sky-blue halo rings around the badge (radius in axes-height units)."""
    from matplotlib.patches import Ellipse
    for k, a in [(1.34, 0.05), (1.24, 0.08), (1.15, 0.12), (1.07, 0.22)]:
        ax.add_patch(Ellipse((cx, cy), 2 * r * k / aspect, 2 * r * k, facecolor="none",
                             edgecolor=SKY, lw=2.0 if k < 1.1 else 1.2, alpha=a, zorder=11))
    ax.add_patch(Ellipse((cx, cy), 2 * r * 1.02 / aspect, 2 * r * 1.02, facecolor="none",
                         edgecolor=WHITE, lw=2.5, alpha=0.9, zorder=13))
    for k in np.linspace(1.0, 1.45, 14):  # soft blue bloom
        ax.add_patch(Ellipse((cx, cy), 2 * r * k / aspect, 2 * r * k, facecolor=SKY,
                             edgecolor="none", alpha=0.012, zorder=10))
    # Orbit ticks: a quant "dial" around the badge
    for ang in np.linspace(0, 2 * np.pi, 72, endpoint=False):
        r0, r1 = r * 1.19, r * (1.24 if int(round(ang / (2 * np.pi) * 72)) % 6 == 0 else 1.21)
        ax.plot([cx + r0 * np.cos(ang) / aspect, cx + r1 * np.cos(ang) / aspect],
                [cy + r0 * np.sin(ang), cy + r1 * np.sin(ang)], color=SKY, lw=1.0, alpha=0.35, zorder=11)


def render(path, w_px, h_px, layout):
    dpi = 200
    fig = plt.figure(figsize=(w_px / dpi, h_px / dpi), dpi=dpi, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor(BG)
    aspect = w_px / h_px
    _grid(ax, w_px, h_px)

    L = layout
    _candles(ax, *L["candles"])
    _bell(ax, *L["bell"])
    cx, cy, r = L["logo"]
    _glow_rings(ax, cx, cy, r, aspect)
    logo = circular_logo(int(2 * r * h_px))
    ax.imshow(np.asarray(logo), extent=(cx - r / aspect, cx + r / aspect, cy - r, cy + r), zorder=12,
              interpolation="lanczos", aspect="auto")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    for t in L["texts"]:
        ax.text(*t[:3], **t[3])
    fig.savefig(path, dpi=dpi, facecolor=BG)
    plt.close(fig)
    # Soft vignette so the edges melt into the page background
    img = Image.open(path).convert("RGB")
    vig = Image.new("L", img.size, 0)
    ImageDraw.Draw(vig).rectangle((int(w_px * 0.03), int(h_px * 0.05), int(w_px * 0.97), int(h_px * 0.95)), fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(min(w_px, h_px) * 0.05))
    final = Image.composite(img, Image.new("RGB", img.size, BG), vig)
    os.remove(path)  # the PNG was only the matplotlib canvas; ship JPEG + WebP
    base = os.path.splitext(path)[0]
    final.save(base + ".jpg", "JPEG", quality=88, optimize=True, progressive=True)
    final.save(base + ".webp", "WEBP", quality=86, method=6)


def main():
    if not os.path.exists(SRC_LOGO):
        sys.exit(f"logo not found: {SRC_LOGO}")
    os.makedirs(OUT, exist_ok=True)
    mono = dict(family="monospace", fontweight="bold")
    render(os.path.join(OUT, "hero_wide.png"), 2400, 900, {
        "candles": (0.02, 0.30, 0.18, 0.78),
        "bell": (0.845, 0.27, 0.20, 0.48, -2.1),
        "logo": (0.20, 0.50, 0.40),
        "texts": [
            (0.365, 0.64, "THE LYNCH PIN", dict(color=WHITE, fontsize=34, fontweight="black", zorder=14)),
            (0.367, 0.52, "QUANT PORTAL", dict(color=SKY, fontsize=22, fontweight="bold", zorder=14)),
            (0.367, 0.41, "Growth at a Reasonable Price", dict(color="#B0B0B0", fontsize=12, zorder=14)),
            (0.367, 0.25, " PEG = P/E ÷ Growth ", dict(color="#E0E0E0", fontsize=12, zorder=14, bbox=dict(
                facecolor="#1A1A1A", edgecolor="#333333", boxstyle="round,pad=0.6"), **mono)),
        ],
    })
    render(os.path.join(OUT, "hero_square.png"), 1200, 1200, {
        "candles": (0.04, 0.96, 0.60, 0.92),
        "bell": (0.50, 0.86, 0.05, 0.15, -2.1),
        "logo": (0.50, 0.64, 0.23),
        "texts": [
            (0.50, 0.27, "QUANT PORTAL", dict(color=SKY, fontsize=24, fontweight="bold", ha="center", zorder=14)),
        ],
    })
    logo = circular_logo(512)
    logo.save(os.path.join(OUT, "logo.png"), optimize=True)
    for size, name in [(192, "icon-192.png"), (512, "icon-512.png"), (180, "apple-touch-icon.png")]:
        bg = Image.new("RGBA", (size, size), BG)
        bg.alpha_composite(logo.resize((size, size), Image.LANCZOS))
        bg.convert("RGB").save(os.path.join(OUT, name), optimize=True)
    mask_bg = Image.new("RGBA", (512, 512), BG)
    inner = int(512 * 0.78)
    mask_bg.alpha_composite(logo.resize((inner, inner), Image.LANCZOS), ((512 - inner) // 2, (512 - inner) // 2))
    mask_bg.convert("RGB").save(os.path.join(OUT, "icon-512-maskable.png"), optimize=True)
    logo.resize((64, 64), Image.LANCZOS).save(os.path.join(OUT, "favicon.png"), optimize=True)
    if os.path.exists(SRC_BANNER):
        b = Image.open(SRC_BANNER).convert("RGB")
        b.thumbnail((1600, 1600), Image.LANCZOS)
        b.save(os.path.join(OUT, "banner.jpg"), quality=86, optimize=True)
    print(f"✅ artwork written to {OUT}")


if __name__ == "__main__":
    main()
