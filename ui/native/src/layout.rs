//! The per-ticker PEG chart as a list of device-space draw operations, laid out the way matplotlib 3.11 lays out
//! LynchPinVisualizer.plot_ticker_distribution: a 12×7 in figure, auto limits with 5 % margins, MaxNLocator ticks,
//! ScalarFormatter labels, text boxes measured like Text._get_layout and one pass of tight_layout. Everything is
//! computed at the requested dpi, so the preview is drawn at its own size rather than shrunk from the 300-dpi PNG.
//!
//! matplotlib coordinates (pixels, y up) are used for layout and flipped to device coordinates (y down) when the
//! paths are built.
use std::sync::Arc;

use tiny_skia::{LineCap, LineJoin, Path, PathBuilder, Rect, Stroke, StrokeDash};

use crate::font::{Font, GlyphMask, Shaped};
use crate::spec::Chart;
use crate::ChartError;

#[derive(Clone, Copy, Debug)]
pub struct Rgba {
    pub r: f32,
    pub g: f32,
    pub b: f32,
    pub a: f32,
}

const fn hex(v: u32) -> Rgba {
    Rgba {
        r: ((v >> 16) & 0xff) as f32 / 255.0,
        g: ((v >> 8) & 0xff) as f32 / 255.0,
        b: (v & 0xff) as f32 / 255.0,
        a: 1.0,
    }
}

impl Rgba {
    const fn alpha(self, a: f32) -> Rgba {
        Rgba { a, ..self }
    }
    fn rgb255(self) -> [f64; 3] {
        [self.r as f64 * 255.0, self.g as f64 * 255.0, self.b as f64 * 255.0]
    }
}

// The visualizer's palette and rcParams
pub const BG: Rgba = hex(0x121212);
const SKY: Rgba = hex(0x5D9CEC);
const WHITE: Rgba = hex(0xFFFFFF);
const RED: Rgba = hex(0xFF4B2B);
const BOX_FACE: Rgba = hex(0x1A1A1A);
const BOX_EDGE: Rgba = hex(0x333333);
const LABEL: Rgba = hex(0xE0E0E0);
const TICK: Rgba = hex(0xB0B0B0);
const SPINE: Rgba = hex(0x333333);

const FIG_W_IN: f64 = 12.0;
const FIG_H_IN: f64 = 7.0;
const FONT_SIZE: f64 = 14.0; // rcParams font.size (tick labels, tight_layout pad)
const TITLE_SIZE: f64 = 20.0;
const BOX_SIZE: f64 = 16.0;
const TICK_LEN: f64 = 3.5;
const TICK_WIDTH: f64 = 0.8;
const TICK_PAD: f64 = 3.5;
const LABEL_PAD: f64 = 4.0;
const SPINE_WIDTH: f64 = 0.8;
const PATCH_WIDTH: f64 = 1.0; // patch.linewidth: box frames and fill_between edges
const TITLE_PAD: f64 = 35.0;
const SAMPLES: usize = 500;
const GLOW_LEVELS: usize = 100;

pub struct Fonts {
    pub sans: Arc<Font>,
    pub sans_bold: Arc<Font>,
    pub mono_bold: Arc<Font>,
}

pub enum Op {
    Fill(Path, Rgba),
    Stroke(Path, Rgba, Stroke),
    Rect(Rect, Rgba),
    /// Cached glyph coverage masks with the device pixel of their top-left corner.
    Glyphs(Vec<(Arc<GlyphMask>, i32, i32)>, Rgba),
    /// The face of a framed BoxStyle.Round box, filled by spans (its outline is stroked on top, so the
    /// anti-aliasing of its edge never shows).
    RoundBox(RoundBox, Rgba),
}

/// Device-space rounded box: edges and the radius of its quadratic corners.
#[derive(Clone, Copy, Debug)]
pub struct RoundBox {
    pub left: f64,
    pub top: f64,
    pub right: f64,
    pub bottom: f64,
    pub radius: f64,
}

/// One draw call plus the device rows it can touch, so a band skips what lies outside it.
pub struct DrawOp {
    pub op: Op,
    pub top: f32,
    pub bottom: f32,
}

/// The area under the bell curve: matplotlib draws it as one translucent fill plus 100 stacked white fills whose
/// opacity depends only on height. The stack collapses to one colour per device row, written straight into the
/// pixels (spans come from the curve), instead of 101 full-size alpha fills.
pub struct BellRow {
    pub rgb: [u8; 3],
    pub x0: f32,
    pub x1: f32,
}

pub struct Scene {
    pub width: u32,
    pub height: u32,
    pub bell_first_row: i64,
    pub bell: Vec<Option<BellRow>>,
    pub ops: Vec<DrawOp>,
}

#[derive(Clone, Copy, PartialEq)]
enum HAlign {
    Left,
    Center,
    Right,
}

#[derive(Clone, Copy, PartialEq)]
enum VAlign {
    Top,
    Baseline,
}

/// A laid-out text in matplotlib pixels (y up), relative to its anchor.
struct TextLayout {
    lines: Vec<(Shaped, f64, f64)>, // (glyphs, x, baseline y)
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
    size_px: f64,
}

impl TextLayout {
    fn at(&self, x: f64, y: f64) -> Placed<'_> {
        Placed { t: self, x, y }
    }
}

struct Placed<'a> {
    t: &'a TextLayout,
    x: f64,
    y: f64,
}

impl Placed<'_> {
    /// Window extent (matplotlib's get_window_extent, without the bbox patch).
    fn extent(&self) -> Bounds {
        Bounds { x0: self.x + self.t.x0, y0: self.y + self.t.y0, x1: self.x + self.t.x1, y1: self.y + self.t.y1 }
    }
}

#[derive(Clone, Copy, Debug)]
struct Bounds {
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
}

impl Bounds {
    fn union(self, o: Bounds) -> Bounds {
        Bounds { x0: self.x0.min(o.x0), y0: self.y0.min(o.y0), x1: self.x1.max(o.x1), y1: self.y1.max(o.y1) }
    }
}

/// Text._get_layout (matplotlib 3.11): lines are at least as tall as the font's typographic ascent/descent plus
/// half the line gap on each side (no gap for a single line); the width is the widest line's pen advance;
/// multi-line alignment follows the horizontal alignment.
fn layout_text(font: &Font, text: &str, size_pt: f64, dpi: f64, ha: HAlign, va: VAlign) -> TextLayout {
    let scale = font.scale(size_pt, dpi);
    let (min_a, min_d, gap) = font.line_metrics(scale);
    let lines: Vec<&str> = text.split('\n').collect();
    let half_gap = if lines.len() == 1 { 0.0 } else { gap / 2.0 };
    let mut thisy = 0.0;
    let mut descent = 0.0;
    let mut shaped = Vec::with_capacity(lines.len());
    for line in &lines {
        let s = font.shape(line, scale);
        let (w, h, d) = if line.is_empty() { (0.0, 0.0, 0.0) } else { s.whd() };
        let a = (h - d).max(min_a) + half_gap;
        let d = d.max(min_d) + half_gap;
        thisy -= a;
        shaped.push((s, w, thisy));
        thisy -= d;
        descent = d;
    }
    let width = shaped.iter().map(|(_, w, _)| *w).fold(0.0, f64::max);
    let ymin = shaped.last().map(|(_, _, y)| *y).unwrap_or(0.0) - descent;
    let offx = match ha {
        HAlign::Left => 0.0,
        HAlign::Center => width / 2.0,
        HAlign::Right => width,
    };
    let offy = match va {
        VAlign::Top => 0.0,
        VAlign::Baseline => ymin + descent,
    };
    let lines = shaped
        .into_iter()
        .map(|(s, w, y)| {
            let x = match ha {
                HAlign::Left => 0.0,
                HAlign::Center => width / 2.0 - w / 2.0,
                HAlign::Right => width - w,
            };
            (s, x - offx, y - offy)
        })
        .collect();
    TextLayout { lines, x0: -offx, y0: ymin - offy, x1: width - offx, y1: -offy, size_px: size_pt * dpi / 72.0 }
}

/// Python's float divmod (floor division with the remainder taking the divisor's sign).
fn py_divmod(x: f64, y: f64) -> (f64, f64) {
    let mut m = x % y;
    let mut d = (x - m) / y;
    if m != 0.0 {
        if (y < 0.0) != (m < 0.0) {
            m += y;
            d -= 1.0;
        }
    } else {
        m = 0.0f64.copysign(y);
    }
    let fd = if d != 0.0 {
        let f = d.floor();
        if d - f > 0.5 {
            f + 1.0
        } else {
            f
        }
    } else {
        0.0f64.copysign(x / y)
    };
    (fd, m)
}

/// MaxNLocator(nbins='auto', steps=[1, 2, 2.5, 5, 10]) — matplotlib's AutoLocator — for (vmin, vmax).
fn auto_ticks(vmin: f64, vmax: f64, tick_space: i64) -> Vec<f64> {
    let nbins = tick_space.clamp(1, 9) as f64;
    let dv = (vmax - vmin).abs();
    let meanv = (vmax + vmin) / 2.0;
    let offset = if meanv.abs() / dv < 100.0 { 0.0 } else { 10f64.powf(meanv.abs().log10().floor()).copysign(meanv) };
    let scale = 10f64.powf((dv / nbins).log10().floor());
    let (lo, hi) = (vmin - offset, vmax - offset);
    let base = [1.0, 2.0, 2.5, 5.0, 10.0];
    let mut steps: Vec<f64> = base[..4].iter().map(|s| 0.1 * s).collect();
    steps.extend_from_slice(&base);
    steps.push(10.0 * base[1]);
    let steps: Vec<f64> = steps.iter().map(|s| s * scale).collect();
    let raw_step = (hi - lo) / nbins;
    let istep = steps.iter().position(|&s| s >= raw_step).unwrap_or(steps.len() - 1);
    let mut ticks = Vec::new();
    for &step in steps[..=istep].iter().rev() {
        let best_vmin = py_divmod(lo, step).0 * step;
        let tol =
            if offset != 0.0 { (10f64.powf((offset.abs() / step).log10() - 12.0)).clamp(1e-10, 0.4999) } else { 1e-10 };
        let le = |x: f64| {
            let (d, m) = py_divmod(x, step);
            if (m / step - 1.0).abs() < tol {
                d + 1.0
            } else {
                d
            }
        };
        let ge = |x: f64| {
            let (d, m) = py_divmod(x, step);
            if (m / step).abs() < tol {
                d
            } else {
                d + 1.0
            }
        };
        let (low, high) = (le(lo - best_vmin), ge(hi - best_vmin));
        ticks.clear();
        let mut n = low;
        while n < high + 1.0 {
            ticks.push(n * step + best_vmin);
            n += 1.0;
        }
        if ticks.iter().filter(|&&t| t <= hi && t >= lo).count() >= 2 {
            break;
        }
    }
    ticks.iter().map(|t| t + offset).collect()
}

/// numpy.round(x, decimals) for decimals >= 0.
fn np_round(x: f64, decimals: i32) -> f64 {
    if decimals == 0 {
        return x.round_ties_even();
    }
    let p = 10f64.powi(decimals);
    (x * p).round_ties_even() / p
}

/// ScalarFormatter labels for ``locs`` with view interval (vmin, vmax). Charts that would need an offset or a
/// power of ten (never for a PEG, in practice) are left to matplotlib.
fn tick_labels(locs: &[f64], vmin: f64, vmax: f64) -> Result<Vec<String>, ChartError> {
    let visible: Vec<f64> = locs.iter().copied().filter(|&l| vmin <= l && l <= vmax).collect();
    if visible.len() >= 2 {
        let lmin = visible.iter().copied().fold(f64::INFINITY, f64::min);
        let lmax = visible.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        if !(lmin == lmax || (lmin <= 0.0 && 0.0 <= lmax)) {
            let (amin, amax) =
                if lmin.abs() <= lmax.abs() { (lmin.abs(), lmax.abs()) } else { (lmax.abs(), lmin.abs()) };
            let oom_max = amax.log10().ceil();
            let first_diff = |pred: &dyn Fn(f64, f64) -> bool| -> Result<f64, ChartError> {
                let mut oom = oom_max;
                for _ in 0..1000 {
                    let p = 10f64.powf(oom);
                    if pred(py_divmod(amin, p).0, py_divmod(amax, p).0) {
                        return Ok(oom + 1.0);
                    }
                    oom -= 1.0;
                }
                Err(ChartError::Unsupported("tick offset".into()))
            };
            let mut oom = first_diff(&|a, b| a != b)?;
            if (amax - amin) / 10f64.powf(oom) <= 1e-2 {
                oom = first_diff(&|a, b| b - a > 1.0)?;
            }
            if py_divmod(amax, 10f64.powf(oom)).0 >= 1000.0 {
                return Err(ChartError::Unsupported("tick labels need an offset".into()));
            }
        }
    }
    let vmax_abs = visible.iter().map(|l| l.abs()).fold(0.0, f64::max);
    if !visible.is_empty() && vmax_abs != 0.0 {
        let oom = vmax_abs.log10().floor();
        if oom <= -5.0 || oom >= 6.0 {
            return Err(ChartError::Unsupported("tick labels need scientific notation".into()));
        }
    }
    let mut all: Vec<f64> = locs.to_vec();
    if locs.len() < 2 {
        all.extend([vmin, vmax]);
    }
    let lmin = all.iter().copied().fold(f64::INFINITY, f64::min);
    let lmax = all.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let mut range = lmax - lmin;
    if range == 0.0 {
        range = all.iter().map(|l| l.abs()).fold(0.0, f64::max);
    }
    if range == 0.0 {
        range = 1.0;
    }
    let range_oom = range.log10().floor() as i32;
    let mut sigfigs = (3 - range_oom).max(0);
    let thresh = 1e-3 * 10f64.powi(range_oom);
    let check: &[f64] = if locs.len() < 2 { locs } else { &all };
    while sigfigs >= 0 {
        if check.iter().map(|&l| (l - np_round(l, sigfigs)).abs()).fold(0.0, f64::max) < thresh {
            sigfigs -= 1;
        } else {
            break;
        }
    }
    sigfigs += 1;
    let prec = sigfigs.max(0) as usize;
    Ok(locs
        .iter()
        .map(|&l| {
            let v = if l.abs() < 1e-8 { 0.0 } else { l };
            format!("{v:.prec$}").replace('-', "\u{2212}")
        })
        .collect())
}

/// Pixel snapping of Agg for straight horizontal/vertical paths: vertices land on pixel centres for odd widths.
fn snap(v: f64, width_px: f64) -> f64 {
    let odd = (width_px.round() as i64) % 2 != 0;
    (v + 0.5).floor() + if odd { 0.5 } else { 0.0 }
}

/// matplotlib's BoxStyle.Round: corners are quadratic Béziers of radius ``pad``.
fn round_box(pb: &mut PathBuilder, flip: f64, b: Bounds, pad: f64) {
    let (x0, y0, x1, y1) = (b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad);
    let dr = pad;
    let p = |x: f64, y: f64| (x as f32, (flip - y) as f32);
    let pts = [
        p(x0 + dr, y0),
        p(x1 - dr, y0),
        p(x1, y0),
        p(x1, y0 + dr),
        p(x1, y1 - dr),
        p(x1, y1),
        p(x1 - dr, y1),
        p(x0 + dr, y1),
        p(x0, y1),
        p(x0, y1 - dr),
        p(x0, y0 + dr),
        p(x0, y0),
        p(x0 + dr, y0),
    ];
    pb.move_to(pts[0].0, pts[0].1);
    pb.line_to(pts[1].0, pts[1].1);
    pb.quad_to(pts[2].0, pts[2].1, pts[3].0, pts[3].1);
    pb.line_to(pts[4].0, pts[4].1);
    pb.quad_to(pts[5].0, pts[5].1, pts[6].0, pts[6].1);
    pb.line_to(pts[7].0, pts[7].1);
    pb.quad_to(pts[8].0, pts[8].1, pts[9].0, pts[9].1);
    pb.line_to(pts[10].0, pts[10].1);
    pb.quad_to(pts[11].0, pts[11].1, pts[12].0, pts[12].1);
    pb.close();
}

fn norm_pdf(x: f64, mean: f64, sd: f64) -> f64 {
    let z = (x - mean) / sd;
    (-z * z / 2.0).exp() / (2.0 * std::f64::consts::PI).sqrt() / sd
}

/// Everything around the axes that depends on where the axes are (tight_layout runs it twice).
struct Decor {
    ticks: Vec<(f64, String)>, // visible ticks: data x, label
    tick_text: Vec<TextLayout>,
    title: TextLayout,
    xlabel: TextLayout,
    left: TextLayout,
    right: Option<TextLayout>,
    badge: TextLayout,
}

struct Axes {
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
    xlim: (f64, f64),
    ylim: (f64, f64),
}

impl Axes {
    fn w(&self) -> f64 {
        self.x1 - self.x0
    }
    fn h(&self) -> f64 {
        self.y1 - self.y0
    }
    fn x(&self, v: f64) -> f64 {
        self.x0 + (v - self.xlim.0) / (self.xlim.1 - self.xlim.0) * self.w()
    }
    fn y(&self, v: f64) -> f64 {
        self.y0 + (v - self.ylim.0) / (self.ylim.1 - self.ylim.0) * self.h()
    }
}

pub struct Builder<'a> {
    chart: &'a Chart,
    fonts: &'a Fonts,
    dpi: f64,
    sd: f64,
    xs: Vec<f64>,
    ys: Vec<f64>,
    y_max: f64,
    marker_h: f64,
    xlim: (f64, f64),
    ylim: (f64, f64),
}

impl<'a> Builder<'a> {
    pub fn new(chart: &'a Chart, fonts: &'a Fonts, dpi: f64) -> Result<Self, ChartError> {
        let (peg, mean, z) = (chart.peg, chart.mean, chart.z);
        if !(peg.is_finite() && mean.is_finite() && z.is_finite() && dpi.is_finite() && dpi > 0.0) {
            return Err(ChartError::Unsupported("non-finite chart input".into()));
        }
        let sd = if z != 0.0 { ((peg - mean) / z).abs() } else { 0.5 };
        if !(sd.is_finite() && sd > 0.0) {
            return Err(ChartError::Unsupported("degenerate standard deviation".into()));
        }
        let (start, stop) = (mean - 4.0 * sd, mean + 4.0 * sd);
        let step = (stop - start) / (SAMPLES - 1) as f64;
        let mut xs: Vec<f64> = (0..SAMPLES).map(|i| i as f64 * step + start).collect();
        xs[SAMPLES - 1] = stop;
        let ys: Vec<f64> = xs.iter().map(|&x| norm_pdf(x, mean, sd)).collect();
        let y_max = ys.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        let marker_h = norm_pdf(peg, mean, sd);
        // autoscale: data limits + 5 % margins (the scatter / red line may sit beyond ±4 SD)
        let (dx0, dx1) = (start.min(peg), stop.max(peg));
        let (dy0, dy1) = (0.0f64, y_max.max(marker_h));
        let (mx, my) = ((dx1 - dx0) * 0.05, (dy1 - dy0) * 0.05);
        let (xlim, ylim) = ((dx0 - mx, dx1 + mx), (dy0 - my, dy1 + my));
        if !(xlim.1 > xlim.0 && ylim.1 > ylim.0 && y_max.is_finite()) {
            return Err(ChartError::Unsupported("degenerate axis limits".into()));
        }
        Ok(Builder { chart, fonts, dpi, sd, xs, ys, y_max, marker_h, xlim, ylim })
    }

    fn pt(&self, v: f64) -> f64 {
        v * self.dpi / 72.0
    }

    fn decor(&self, ax: &Axes) -> Result<Decor, ChartError> {
        let f = self.fonts;
        let tick_space = (ax.w() * 72.0 / self.dpi / (FONT_SIZE * 3.0)).floor() as i64;
        let locs = auto_ticks(self.xlim.0, self.xlim.1, tick_space);
        let labels = tick_labels(&locs, self.xlim.0, self.xlim.1)?;
        let (a, b) = self.xlim;
        let tol = (b - a) * 1e-10;
        let ticks: Vec<(f64, String)> =
            locs.into_iter().zip(labels).filter(|(l, _)| a - tol <= *l && *l <= b + tol).collect();
        let tick_text = ticks
            .iter()
            .map(|(_, s)| layout_text(&f.sans, s, FONT_SIZE, self.dpi, HAlign::Center, VAlign::Top))
            .collect();
        let c = self.chart;
        Ok(Decor {
            ticks,
            tick_text,
            title: layout_text(&f.sans_bold, &c.title, TITLE_SIZE, self.dpi, HAlign::Left, VAlign::Baseline),
            xlabel: layout_text(&f.sans_bold, "PEG RATIO", FONT_SIZE, self.dpi, HAlign::Center, VAlign::Top),
            left: layout_text(&f.mono_bold, &c.left_box, BOX_SIZE, self.dpi, HAlign::Left, VAlign::Top),
            right: (!c.right_box.is_empty())
                .then(|| layout_text(&f.mono_bold, &c.right_box, BOX_SIZE, self.dpi, HAlign::Right, VAlign::Top)),
            badge: layout_text(&f.sans_bold, &c.badge, FONT_SIZE, self.dpi, HAlign::Center, VAlign::Baseline),
        })
    }

    /// Anchors of every text, in matplotlib pixels.
    fn anchors(&self, ax: &Axes, d: &Decor) -> Anchors {
        let label_top = ax.y0 - self.pt(TICK_LEN + TICK_PAD);
        let ticks: Vec<(f64, f64)> = d.ticks.iter().map(|(v, _)| (ax.x(*v), label_top)).collect();
        // xlabel: below the tick labels (and the spine with its ticks) by labelpad
        let mut bottom = ax.y0 - self.pt(TICK_LEN);
        for (t, (x, y)) in d.tick_text.iter().zip(&ticks) {
            bottom = bottom.min(t.at(*x, *y).extent().y0);
        }
        Anchors {
            title: (ax.x0, ax.y1 + self.pt(TITLE_PAD)),
            xlabel: (ax.x0 + ax.w() / 2.0, bottom - self.pt(LABEL_PAD)),
            ticks,
            left: (ax.x0 + 0.02 * ax.w(), ax.y0 + 1.02 * ax.h()),
            right: (ax.x0 + 0.98 * ax.w(), ax.y0 + 1.09 * ax.h()),
            badge: (ax.x(self.chart.peg), ax.y(self.marker_h * 1.12)),
        }
    }

    fn tight_bounds(&self, ax: &Axes, d: &Decor, an: &Anchors) -> Bounds {
        let mut b = Bounds { x0: ax.x0, y0: ax.y0, x1: ax.x1, y1: ax.y1 };
        b = b.union(d.title.at(an.title.0, an.title.1).extent());
        let xl = d.xlabel.at(an.xlabel.0, an.xlabel.1).extent();
        let cx = (xl.x0 + xl.x1) / 2.0; // for_layout_only: the xlabel never widens the layout
        b = b.union(Bounds { x0: cx, x1: cx, ..xl });
        for (t, (x, y)) in d.tick_text.iter().zip(&an.ticks) {
            b = b.union(t.at(*x, *y).extent());
        }
        b = b.union(d.left.at(an.left.0, an.left.1).extent());
        if let Some(r) = &d.right {
            b = b.union(r.at(an.right.0, an.right.1).extent());
        }
        b.union(d.badge.at(an.badge.0, an.badge.1).extent())
    }

    pub fn build(&self) -> Result<Scene, ChartError> {
        let (fw, fh) = (FIG_W_IN * self.dpi, FIG_H_IN * self.dpi);
        // tight_layout: one pass from the default subplot (0.125, 0.11)–(0.9, 0.88)
        let mut ax =
            Axes { x0: 0.125 * fw, y0: 0.11 * fh, x1: 0.9 * fw, y1: 0.88 * fh, xlim: self.xlim, ylim: self.ylim };
        {
            let d = self.decor(&ax)?;
            let an = self.anchors(&ax, &d);
            let tb = self.tight_bounds(&ax, &d, &an);
            let pad = self.pt(1.08 * FONT_SIZE);
            let left = (ax.x0 - tb.x0).max(0.0) + pad;
            let right = (tb.x1 - ax.x1).max(0.0) + pad;
            let top = (tb.y1 - ax.y1).max(0.0) + pad;
            let bottom = (ax.y0 - tb.y0).max(0.0) + pad;
            if left + right < fw && top + bottom < fh {
                ax.x0 = left;
                ax.x1 = fw - right;
                ax.y0 = bottom;
                ax.y1 = fh - top;
            }
        }
        let d = self.decor(&ax)?;
        let an = self.anchors(&ax, &d);

        let flip = fh;
        let dev = |x: f64, y: f64| (x as f32, (flip - y) as f32);
        let mut ops: Vec<DrawOp> = Vec::with_capacity(48);
        let mut push = |op: Op| {
            // Strokes become filled outlines here, once, rather than in every band that draws them
            let op = match op {
                Op::Stroke(p, c, s) => {
                    let dashed = s.dash.as_ref().and_then(|d| p.dash(d, 1.0));
                    let solid = Stroke { dash: None, ..s.clone() };
                    match dashed.as_ref().unwrap_or(&p).stroke(&solid, 1.0) {
                        Some(outline) => Op::Fill(outline, c),
                        None => Op::Stroke(p, c, s),
                    }
                }
                op => op,
            };
            let (top, bottom) = match &op {
                Op::Fill(p, _) => (p.bounds().top(), p.bounds().bottom()),
                Op::Stroke(p, _, s) => (p.bounds().top() - s.width, p.bounds().bottom() + s.width),
                Op::Rect(r, _) => (r.top(), r.bottom()),
                Op::RoundBox(b, _) => (b.top as f32, b.bottom as f32),
                Op::Glyphs(g, _) => (
                    g.iter().map(|(_, _, t)| *t).min().unwrap_or(0) as f32,
                    g.iter().map(|(m, _, t)| t + m.height as i32).max().unwrap_or(0) as f32,
                ),
            };
            ops.push(DrawOp { op, top, bottom });
        };
        let line = |pts: &[(f32, f32)]| -> Option<Path> {
            let mut pb = PathBuilder::new();
            pb.move_to(pts[0].0, pts[0].1);
            for p in &pts[1..] {
                pb.line_to(p.0, p.1);
            }
            pb.finish()
        };
        let stroke = |width_pt: f64, cap: LineCap, join: LineJoin| Stroke {
            width: self.pt(width_pt) as f32,
            line_cap: cap,
            line_join: join,
            ..Stroke::default()
        };

        // zorder 1.5 — the x axis: tick marks, tick labels, axis label. Ticks are markers: Agg rounds their
        // position to whole pixels and snaps the marker path itself.
        let tick_w = self.pt(TICK_WIDTH);
        let tick_y = (flip - ax.y0).round();
        let (t0, t1) = (snap(0.0, tick_w), snap(self.pt(TICK_LEN), tick_w));
        for (i, (v, _)) in d.ticks.iter().enumerate() {
            let x = ax.x(*v).round() + snap(0.0, tick_w);
            let hw = tick_w / 2.0;
            let r = Rect::from_ltrb((x - hw) as f32, (tick_y + t0) as f32, (x + hw) as f32, (tick_y + t1) as f32);
            if let Some(r) = r {
                push(Op::Rect(r, TICK));
            }
            let (ax_, ay) = an.ticks[i];
            if let Some(g) = text_glyphs(&d.tick_text[i], ax_, ay, flip) {
                push(Op::Glyphs(g, TICK));
            }
        }
        if let Some(g) = text_glyphs(&d.xlabel, an.xlabel.0, an.xlabel.1, flip) {
            push(Op::Glyphs(g, LABEL.alpha(0.7)));
        }

        // zorder 2.5 — bottom spine
        let sw = self.pt(SPINE_WIDTH);
        let sy = snap(flip - ax.y0, sw);
        let (sx0, sx1) = (snap(ax.x0, sw), snap(ax.x1, sw));
        if let Some(r) = Rect::from_ltrb(sx0 as f32, (sy - sw / 2.0) as f32, sx1 as f32, (sy + sw / 2.0) as f32) {
            push(Op::Rect(r, SPINE));
        }

        // zorder 3 — SD lines (sharp core + faint glow), butt caps, snapped
        let (mean, sd) = (self.chart.mean, self.sd);
        for i in -3..=3 {
            let xv = mean + i as f64 * sd;
            let h = norm_pdf(xv, mean, sd);
            for (w_pt, alpha) in [(1.2, 0.5), (6.0, 0.05)] {
                let w = self.pt(w_pt);
                let x = snap(ax.x(xv), w);
                let (yt, yb) = (snap(flip - ax.y(h), w), snap(flip - ax.y(0.0), w));
                if let Some(r) = Rect::from_ltrb((x - w / 2.0) as f32, yt as f32, (x + w / 2.0) as f32, yb as f32) {
                    push(Op::Rect(r, WHITE.alpha(alpha)));
                }
            }
        }
        // zorder 3 — title
        if let Some(g) = text_glyphs(&d.title, an.title.0, an.title.1, flip) {
            push(Op::Glyphs(g, SKY));
        }

        // zorder 4–6 — the bell curve: blue glow, white glow, white line with projecting caps. matplotlib joins
        // the 500 segments round; the curve turns by a few degrees at most between samples, where a miter join
        // differs from a round one by hundredths of a pixel and costs a fraction of it to rasterise.
        let curve: Vec<(f32, f32)> = self.xs.iter().zip(&self.ys).map(|(&x, &y)| dev(ax.x(x), ax.y(y))).collect();
        if let Some(path) = line(&curve) {
            for (w, c) in [(22.0, SKY.alpha(0.12)), (12.0, WHITE.alpha(0.15)), (4.0, WHITE)] {
                push(Op::Stroke(path.clone(), c, stroke(w, LineCap::Square, LineJoin::Miter)));
            }
        }

        // zorder 7 — red dashed marker line (lines.dashed_pattern 3.7/1.6 × linewidth, butt caps)
        let rw = self.pt(2.5);
        let rx = snap(ax.x(self.chart.peg), rw);
        let ry0 = snap(flip - ax.y(0.0), rw);
        let ry1 = snap(flip - ax.y(self.marker_h), rw);
        if let Some(p) = line(&[(rx as f32, ry0 as f32), (rx as f32, ry1 as f32)]) {
            let mut s = stroke(2.5, LineCap::Butt, LineJoin::Round);
            s.dash = StrokeDash::new(vec![self.pt(3.7 * 2.5) as f32, self.pt(1.6 * 2.5) as f32], 0.0);
            push(Op::Stroke(p, RED, s));
        }

        // zorder 9 — marker dot (draw_markers rounds the centre to whole pixels)
        let (mx, my) = ((ax.x(self.chart.peg)).round(), (flip - ax.y(self.marker_h)).round());
        if let Some(c) = PathBuilder::from_circle(mx as f32, my as f32, self.pt(180f64.sqrt() / 2.0) as f32) {
            push(Op::Fill(c.clone(), RED));
            push(Op::Stroke(c, WHITE, stroke(2.0, LineCap::Butt, LineJoin::Round)));
        }

        // zorder 9 — the two stat boxes; zorder 10 — the SD badge
        let boxed =
            |t: &TextLayout, (x, y): (f64, f64), pad: f64, face: Rgba, edge: Option<Rgba>, ops: &mut dyn FnMut(Op)| {
                let (b, pad) = (t.at(x, y).extent(), pad * t.size_px);
                let mut pb = PathBuilder::new();
                round_box(&mut pb, flip, b, pad);
                if let Some(p) = pb.finish() {
                    match edge {
                        Some(e) => {
                            let rb = RoundBox {
                                left: b.x0 - pad,
                                top: flip - (b.y1 + pad),
                                right: b.x1 + pad,
                                bottom: flip - (b.y0 - pad),
                                radius: pad,
                            };
                            ops(Op::RoundBox(rb, face));
                            ops(Op::Stroke(p, e, stroke(PATCH_WIDTH, LineCap::Butt, LineJoin::Miter)));
                        }
                        None => ops(Op::Fill(p, face)),
                    }
                }
            };
        boxed(&d.left, an.left, 1.2, BOX_FACE.alpha(0.9), Some(BOX_EDGE.alpha(0.9)), &mut push);
        if let Some(g) = text_glyphs(&d.left, an.left.0, an.left.1, flip) {
            push(Op::Glyphs(g, LABEL));
        }
        if let Some(r) = &d.right {
            boxed(r, an.right, 1.2, BOX_FACE.alpha(0.9), Some(BOX_EDGE.alpha(0.9)), &mut push);
            if let Some(g) = text_glyphs(r, an.right.0, an.right.1, flip) {
                push(Op::Glyphs(g, LABEL));
            }
        }
        boxed(&d.badge, an.badge, 0.4, RED, None, &mut push);
        if let Some(g) = text_glyphs(&d.badge, an.badge.0, an.badge.1, flip) {
            push(Op::Glyphs(g, WHITE));
        }

        let width = fw.round().max(1.0) as u32;
        let height = fh.ceil().max(1.0) as u32;
        let (bell_first_row, bell) = self.bell(&ax, &curve, flip, height);
        Ok(Scene { width, height, bell_first_row, bell, ops })
    }

    /// Per-row colour and span of the filled area under the curve (see [`BellRow`]).
    fn bell(&self, ax: &Axes, curve: &[(f32, f32)], flip: f64, height: u32) -> (i64, Vec<Option<BellRow>>) {
        let bg = BG.rgb255();
        let sky = SKY.rgb255();
        let base = flip - ax.y(0.0); // device row of y = 0
        let hw = self.pt(PATCH_WIDTH) / 2.0; // fill_between edges are stroked 1 pt wide in the fill colour
        let levels: Vec<(f64, f64)> = (0..GLOW_LEVELS)
            .map(|k| {
                let level = self.y_max * k as f64 / (GLOW_LEVELS - 1) as f64;
                ((level / self.y_max).powi(4) * 0.05, flip - ax.y(level))
            })
            .collect();
        let peak = curve.iter().map(|p| p.1 as f64).fold(f64::INFINITY, f64::min);
        let first = peak.floor().max(0.0) as i64;
        let last = ((base + hw).ceil() as i64).min(height as i64 - 1);
        let top_i = curve
            .iter()
            .enumerate()
            .min_by(|a, b| a.1 .1.partial_cmp(&b.1 .1).unwrap_or(std::cmp::Ordering::Equal))
            .map(|(i, _)| i)
            .unwrap_or(0);
        let (cx0, cx1) = (curve[0].0, curve[curve.len() - 1].0);
        let end_y = curve[0].1.max(curve[curve.len() - 1].1) as f64;
        let mix = |c: &mut [f64; 3], to: [f64; 3], a: f64| {
            if a > 0.0 {
                for i in 0..3 {
                    c[i] += (to[i] - c[i]) * a;
                }
            }
        };
        let overlap = |r: f64, lo: f64, hi: f64| ((r + 1.0).min(hi) - r.max(lo)).clamp(0.0, 1.0);
        let crossing = |a: (f32, f32), b: (f32, f32), yc: f64| -> f32 {
            let (ay, by) = (a.1 as f64, b.1 as f64);
            if (by - ay).abs() < 1e-12 {
                return b.0;
            }
            (a.0 as f64 + (b.0 - a.0) as f64 * (yc - ay) / (by - ay)) as f32
        };
        let mut rows = Vec::with_capacity((last - first + 1).max(0) as usize);
        for r in first..=last {
            let rf = r as f64;
            let yc = rf + 0.5;
            let span = if yc >= end_y {
                Some((cx0, cx1))
            } else if yc < peak {
                None
            } else {
                // left flank rises (device y falls) up to the peak, the right flank falls back
                let li = curve[..=top_i].iter().position(|p| (p.1 as f64) <= yc);
                let ri = curve[top_i..].iter().rposition(|p| (p.1 as f64) <= yc).map(|i| i + top_i);
                match (li, ri) {
                    (Some(li), Some(ri)) => {
                        let xl = if li == 0 { curve[0].0 } else { crossing(curve[li - 1], curve[li], yc) };
                        let xr = if ri + 1 >= curve.len() { cx1 } else { crossing(curve[ri], curve[ri + 1], yc) };
                        Some((xl, xr))
                    }
                    _ => None,
                }
            };
            let Some((x0, x1)) = span else {
                rows.push(None);
                continue;
            };
            let mut c = bg;
            mix(&mut c, sky, 0.45 * (base - rf).clamp(0.0, 1.0));
            mix(&mut c, sky, 0.45 * overlap(rf, base - hw, base + hw));
            for &(a, e) in &levels[1..] {
                mix(&mut c, [255.0; 3], a * (e - rf).clamp(0.0, 1.0));
                mix(&mut c, [255.0; 3], a * overlap(rf, e - hw, e + hw));
            }
            let rgb = [c[0].round() as u8, c[1].round() as u8, c[2].round() as u8];
            rows.push(Some(BellRow { rgb, x0, x1 }));
        }
        (first, rows)
    }
}

struct Anchors {
    title: (f64, f64),
    xlabel: (f64, f64),
    ticks: Vec<(f64, f64)>,
    left: (f64, f64),
    right: (f64, f64),
    badge: (f64, f64),
}

/// The glyph masks of a text anchored at (x, y) (matplotlib pixels).
fn text_glyphs(t: &TextLayout, x: f64, y: f64, flip: f64) -> Option<Vec<(Arc<GlyphMask>, i32, i32)>> {
    let mut out = Vec::new();
    for (s, lx, ly) in &t.lines {
        s.masks_at(x + lx, flip - (y + ly), &mut out);
    }
    (!out.is_empty()).then_some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ticks_match_matplotlib() {
        // xlim of the MSFT fixture (probed from matplotlib 3.11)
        let t = auto_ticks(0.793684210526315, 2.646315789473685, 19);
        assert_eq!(t.len(), 9);
        assert!((t[0] - 0.75).abs() < 1e-12 && (t[8] - 2.75).abs() < 1e-12);
        let l = tick_labels(&t, 0.793684210526315, 2.646315789473685).unwrap();
        assert_eq!(l[1], "1.00");
        assert_eq!(l[4], "1.75");
    }

    #[test]
    fn labels_use_unicode_minus_and_trim_digits() {
        let t = auto_ticks(-2.2, 1.3, 19);
        let l = tick_labels(&t, -2.2, 1.3).unwrap();
        assert!(l.iter().any(|s| s.starts_with('\u{2212}')), "{l:?}");
        assert!(l.iter().all(|s| !s.contains('.') || s.split('.').nth(1).unwrap().len() == 1), "{l:?}");
    }

    #[test]
    fn divmod_is_pythons() {
        assert_eq!(py_divmod(-0.3, 0.25), (-2.0, 0.2));
        assert_eq!(py_divmod(7.0, 2.0), (3.0, 1.0));
    }
}
