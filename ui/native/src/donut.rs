//! The portfolio X-ray donut, a drop-in for `graphics.visualizer.LynchPinVisualizer.plot_portfolio`: market-value
//! weight wedges on a glowing ring, the weighted PEG in the hole and two stat boxes on the right. The figure is 7 in
//! tall and as wide as its content needs; every font is scaled by ``s = width / 12`` (the visualizer's own loop,
//! re-run here with the same fonts), so the chart reads like the 12-in per-ticker chart at the same display width.
//!
//! Python formats every string (graphics/visualizer.py's ``portfolio_text`` / ``slice_label``); this lays them
//! out and draws them.
use std::f64::consts::PI;

use serde::Deserialize;
use tiny_skia::{LineCap, LineJoin, Path, PathBuilder, Stroke};

use crate::layout::{
    draw_op, hex, layout_text, round_box, text_glyphs, Bounds, DrawOp, Fonts, HAlign, Op, Rgba, RoundBox, Scene,
    TextLayout, VAlign, BOX_EDGE, BOX_FACE, LABEL, PATCH_WIDTH, RED, SKY, TICK, WHITE,
};
use crate::ChartError;

const H_IN: f64 = 7.0; // canvas height
const MARGIN_IN: f64 = 0.3; // outer margin
const GAP_IN: f64 = 0.45; // donut → boxes
const RING_WIDTH: f64 = 0.42;
const R_IN: f64 = 1.0 - RING_WIDTH;
const R_AX: f64 = 1.06; // the axes' data half-extent: the halo reaches r ≈ 1.05
const BOX_PAD: f64 = 0.9; // boxstyle round,pad=0.9 (rounding size = pad), fractions of the font size
const BADGE_PAD: f64 = 0.4;
const GREEN: Rgba = hex(0x2ECC71);
const BASE_WIDTH_IN: f64 = 12.0;

#[derive(Deserialize, Debug, Clone)]
pub struct Slice {
    pub weight: f64,
    /// "MSFT\n42.0%" on the ring, "KO 2.1%" outside it (``small``).
    pub text: String,
    #[serde(default)]
    pub small: bool,
}

#[derive(Deserialize, Debug, Clone)]
pub struct Donut {
    pub title: String,
    /// Largest first, as ``_pie_slices`` returns them (small positions already folded into "Other (n)").
    pub slices: Vec<Slice>,
    /// The weighted PEG in the hole, e.g. "1.42" or "N/A".
    pub peg: String,
    /// " +0.35 SD " under it; None = no badge.
    #[serde(default)]
    pub badge: Option<String>,
    /// Red badge (above the historical mean), else green.
    #[serde(default)]
    pub badge_red: bool,
    pub stats_box: String,
    pub quality_box: String,
}

/// Where everything goes, in inches (independent of the dpi it is drawn at).
#[derive(Debug, Clone, Copy)]
pub struct Layout {
    pub width_in: f64,
    /// Font scale: width / 12.
    pub s: f64,
    /// Box font size before scaling (16 … 11 pt).
    pub box_pt: f64,
    pub box_gap_in: f64,
    pub title_h_in: f64,
    /// Visible donut diameter (the axes' side).
    pub diameter_in: f64,
}

/// A text's extent in inches (laid out at 72 dpi, where a pixel is a point).
fn measure(font: &crate::font::Font, text: &str, size_pt: f64) -> (f64, f64) {
    let t = layout_text(font, text, size_pt, 72.0, HAlign::Left, VAlign::Top);
    ((t.x1 - t.x0) / 72.0, (t.y1 - t.y0) / 72.0)
}

/// A stat box's frame (text + pad on every side), in inches.
fn box_size(fonts: &Fonts, text: &str, size_pt: f64) -> (f64, f64) {
    let (w, h) = measure(&fonts.mono_bold, text, size_pt);
    let pad = 2.0 * BOX_PAD * size_pt / 72.0;
    (w + pad, h + pad)
}

/// plot_portfolio's layout loop: the boxes fill the span between the title's bottom and the ring's bottom (their
/// font steps down from 16 pt only when they would not fit); the width follows from the box width and the font
/// scale from the width — iterated until the two agree.
pub fn layout(d: &Donut, fonts: &Fonts) -> Result<Layout, ChartError> {
    if d.slices.is_empty() {
        return Err(ChartError::Unsupported("no positions".into()));
    }
    if d.slices.iter().any(|s| !(s.weight.is_finite() && s.weight >= 0.0)) {
        return Err(ChartError::Unsupported("non-finite weight".into()));
    }
    if d.slices.iter().map(|s| s.weight).sum::<f64>() <= 0.0 {
        return Err(ChartError::Unsupported("weights sum to zero".into()));
    }
    let mut s = 1.0;
    let mut out = None;
    for _ in 0..6 {
        let (_, title_h) = measure(&fonts.sans_bold, &d.title, 20.0 * s);
        let ring_top = H_IN - MARGIN_IN - title_h - 0.2;
        let diameter = ring_top - MARGIN_IN;
        let (mut tb, mut lb, mut gap, mut used) = ((0.0, 0.0), (0.0, 0.0), 0.0, 16.0);
        for fs in [16.0, 15.0, 14.0, 13.0, 12.0, 11.0] {
            tb = box_size(fonts, &d.stats_box, fs * s);
            lb = box_size(fonts, &d.quality_box, fs * s);
            gap = ((H_IN - MARGIN_IN - title_h) - MARGIN_IN - tb.1 - lb.1) / 2.0;
            used = fs;
            if gap >= 0.1 {
                break;
            }
        }
        let width = MARGIN_IN + diameter + GAP_IN + tb.0.max(lb.0) + MARGIN_IN;
        out = Some(Layout { width_in: width, s, box_pt: used, box_gap_in: gap, title_h_in: title_h, diameter_in: diameter });
        let s_new = width / BASE_WIDTH_IN;
        if (s_new - s).abs() < 0.005 {
            break;
        }
        s = s_new;
    }
    let l = out.expect("layout ran");
    if !(l.width_in.is_finite() && l.diameter_in > 0.0) {
        return Err(ChartError::Unsupported("degenerate layout".into()));
    }
    Ok(l)
}

/// A filled ring sector between radii ``r0`` < ``r1`` from ``t0`` to ``t1`` degrees (counter-clockwise), as one
/// polygon: the outer arc forward, the inner arc back. ``to`` maps data → device coordinates.
fn sector(to: &dyn Fn(f64, f64) -> (f32, f32), r0: f64, r1: f64, t0: f64, t1: f64) -> Option<Path> {
    let sweep = t1 - t0;
    let n = ((sweep.abs() * 2.0).ceil() as usize).max(2); // half-degree steps: < 0.01 px off the arc at 300 dpi
    let mut pb = PathBuilder::new();
    for i in 0..=n {
        let a = (t0 + sweep * i as f64 / n as f64) * PI / 180.0;
        let (x, y) = to(r1 * a.cos(), r1 * a.sin());
        if i == 0 {
            pb.move_to(x, y);
        } else {
            pb.line_to(x, y);
        }
    }
    for i in (0..=n).rev() {
        let a = (t0 + sweep * i as f64 / n as f64) * PI / 180.0;
        let (x, y) = to(r0 * a.cos(), r0 * a.sin());
        pb.line_to(x, y);
    }
    pb.close();
    pb.finish()
}

/// A circle of radius ``r`` (data units) as a closed polyline, for stroking.
fn circle(to: &dyn Fn(f64, f64) -> (f32, f32), r: f64) -> Option<Path> {
    let n = 720;
    let mut pb = PathBuilder::new();
    for i in 0..n {
        let a = 2.0 * PI * i as f64 / n as f64;
        let (x, y) = to(r * a.cos(), r * a.sin());
        if i == 0 {
            pb.move_to(x, y);
        } else {
            pb.line_to(x, y);
        }
    }
    pb.close();
    pb.finish()
}

/// Draws ``d`` laid out as ``l`` at ``dpi``.
pub fn build(d: &Donut, l: &Layout, fonts: &Fonts, dpi: f64) -> Result<Scene, ChartError> {
    if !(dpi.is_finite() && dpi > 0.0) {
        return Err(ChartError::Unsupported("bad dpi".into()));
    }
    let (fw, fh) = (l.width_in * dpi, H_IN * dpi);
    let flip = fh;
    let pt = |v: f64| v * dpi / 72.0;
    let s = l.s;
    let dia = l.diameter_in * dpi;
    let (cx, cy) = (MARGIN_IN * dpi + dia / 2.0, MARGIN_IN * dpi + dia / 2.0); // matplotlib pixels, y up
    let unit = dia / (2.0 * R_AX);
    let to = move |x: f64, y: f64| ((cx + x * unit) as f32, (flip - (cy + y * unit)) as f32);
    let mut ops: Vec<DrawOp> = Vec::with_capacity(64);
    let mut push = |op: Op| ops.push(draw_op(op));
    let stroke = |width_pt: f64, join: LineJoin| Stroke {
        width: pt(width_pt) as f32,
        line_cap: LineCap::Butt,
        line_join: join,
        ..Stroke::default()
    };

    // 1. glowing ring: a soft blue and a white halo on both rims
    for r in [1.0, R_IN] {
        if let Some(c) = circle(&to, r) {
            push(Op::Stroke(c.clone(), SKY.alpha(0.12), stroke(22.0, LineJoin::Round)));
            push(Op::Stroke(c, WHITE.alpha(0.15), stroke(12.0, LineJoin::Round)));
        }
    }

    // 2. wedges, clockwise from 12 o'clock (pie(startangle=90, counterclock=False)); one hue fading with rank
    let total: f64 = d.slices.iter().map(|s| s.weight).sum();
    let n = d.slices.len();
    let mut theta = 90.0;
    let mut mids = Vec::with_capacity(n);
    for (i, sl) in d.slices.iter().enumerate() {
        let sweep = 360.0 * sl.weight / total;
        let (t0, t1) = (theta - sweep, theta);
        theta -= sweep;
        mids.push((t0 + t1) / 2.0);
        if sweep <= 0.0 {
            continue;
        }
        let alpha = if n > 1 { 0.95 - (0.95 - 0.40) * i as f64 / (n - 1) as f64 } else { 0.95 };
        if let Some(p) = sector(&to, R_IN, 1.0, t0, t1) {
            push(Op::Fill(p.clone(), SKY.alpha(alpha as f32)));
            push(Op::Stroke(p, WHITE, stroke(2.5, LineJoin::Miter)));
        }
    }

    // 3. airy white glow on the outer rim: 20 rings fading in towards the edge
    let glow_start = R_IN + RING_WIDTH * 0.7;
    for k in 0..20 {
        let level = glow_start + (1.0 - glow_start) * k as f64 / 20.0;
        let intensity = ((level - glow_start) / (1.0 - glow_start)).powi(2);
        if intensity <= 0.0 {
            continue;
        }
        if let Some(p) = sector(&to, level, 1.0, 0.0, 360.0) {
            push(Op::Fill(p, WHITE.alpha((intensity * 0.15) as f32)));
        }
    }

    // text anchored at its centre (ha=va='center'), in matplotlib pixels
    let centred = |t: &TextLayout, x: f64, y: f64| (x, y + (t.y1 - t.y0) / 2.0);

    // 4. labels: "TICKER\n12.3%" on the wedge; a thin slice gets "TICKER 2.1%" just outside the ring
    for (sl, mid) in d.slices.iter().zip(&mids) {
        let a = mid * PI / 180.0;
        let (font, size, r, color) = if sl.small {
            (&fonts.sans, 9.0, 1.14, TICK)
        } else {
            (&fonts.sans_bold, if sl.weight / total >= 0.08 { 12.0 } else { 10.0 }, R_IN + RING_WIDTH / 2.0, WHITE)
        };
        let t = layout_text(font, &sl.text, size * s, dpi, HAlign::Center, VAlign::Top);
        let (x, y) = centred(&t, cx + r * a.cos() * unit, cy + r * a.sin() * unit);
        if let Some(g) = text_glyphs(&t, x, y, flip) {
            push(Op::Glyphs(g, color));
        }
    }

    // 5. the hole: weighted PEG, its caption and the SD badge
    let hole = [(d.peg.as_str(), 30.0, 0.12, WHITE), ("WEIGHTED PEG", 11.0, -0.16, TICK)];
    for (text, size, y, color) in hole {
        let t = layout_text(&fonts.sans_bold, text, size * s, dpi, HAlign::Center, VAlign::Top);
        let (x, y) = centred(&t, cx, cy + y * unit);
        if let Some(g) = text_glyphs(&t, x, y, flip) {
            push(Op::Glyphs(g, color));
        }
    }
    if let Some(badge) = d.badge.as_deref() {
        let t = layout_text(&fonts.sans_bold, badge, 14.0 * s, dpi, HAlign::Center, VAlign::Top);
        let (x, y) = centred(&t, cx, cy - 0.38 * unit);
        let b = Bounds { x0: x + t.x0, y0: y + t.y0, x1: x + t.x1, y1: y + t.y1 };
        let pad = BADGE_PAD * t.size_px;
        let mut pb = PathBuilder::new();
        round_box(&mut pb, flip, b, pad, pad);
        if let Some(p) = pb.finish() {
            push(Op::Fill(p, if d.badge_red { RED } else { GREEN }));
        }
        if let Some(g) = text_glyphs(&t, x, y, flip) {
            push(Op::Glyphs(g, WHITE));
        }
    }

    // 6. title, top left
    let title = layout_text(&fonts.sans_bold, &d.title, 20.0 * s, dpi, HAlign::Left, VAlign::Top);
    let title_y = (H_IN - MARGIN_IN) * dpi;
    if let Some(g) = text_glyphs(&title, MARGIN_IN * dpi, title_y, flip) {
        push(Op::Glyphs(g, SKY));
    }

    // 7. the stat boxes, right-aligned to the margin: the upper one box_gap under the title, the lower one standing
    // on the ring's bottom edge; lines are left-aligned inside them
    let right = fw - MARGIN_IN * dpi;
    let title_bottom = title_y - l.title_h_in * dpi;
    let boxes = [(&d.stats_box, title_bottom - l.box_gap_in * dpi, true), (&d.quality_box, MARGIN_IN * dpi, false)];
    for (text, edge_y, top) in boxes {
        let t = layout_text(&fonts.mono_bold, text, l.box_pt * s, dpi, HAlign::Left, VAlign::Top);
        let pad = BOX_PAD * t.size_px;
        let (w, h) = (t.x1 - t.x0, t.y1 - t.y0);
        let x = right - pad - w;
        let y = if top { edge_y - pad } else { edge_y + pad + h };
        let b = Bounds { x0: x + t.x0, y0: y + t.y0, x1: x + t.x1, y1: y + t.y1 };
        let rb = RoundBox {
            left: b.x0 - pad,
            top: flip - (b.y1 + pad),
            right: b.x1 + pad,
            bottom: flip - (b.y0 - pad),
            radius: pad,
        };
        push(Op::RoundBox(rb, BOX_FACE.alpha(0.9)));
        let mut pb = PathBuilder::new();
        round_box(&mut pb, flip, b, pad, pad);
        if let Some(p) = pb.finish() {
            push(Op::Stroke(p, BOX_EDGE.alpha(0.9), stroke(PATCH_WIDTH, LineJoin::Miter)));
        }
        if let Some(g) = text_glyphs(&t, x, y, flip) {
            push(Op::Glyphs(g, LABEL));
        }
    }

    let width = fw.round().max(1.0) as u32;
    let height = fh.ceil().max(1.0) as u32;
    Ok(Scene { width, height, bell_first_row: 0, bell: Vec::new(), ops })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fonts() -> Option<Fonts> {
        // matplotlib's bundled DejaVu, found through the portal's venv (skipped when it is not there)
        let base = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../venv-portal/lib");
        let ttf = std::fs::read_dir(&base)
            .ok()?
            .filter_map(|e| e.ok())
            .map(|e| e.path().join("site-packages/matplotlib/mpl-data/fonts/ttf"))
            .find(|p| p.exists())?;
        let load = |n: &str| crate::font::load(ttf.join(n).to_str()?).ok();
        Some(Fonts {
            sans: load("DejaVuSans.ttf")?,
            sans_bold: load("DejaVuSans-Bold.ttf")?,
            mono_bold: load("DejaVuSansMono-Bold.ttf")?,
        })
    }

    fn sample() -> Donut {
        Donut {
            title: "PORTFOLIO X-RAY: WEIGHT & WEIGHTED GARP METRICS".into(),
            slices: vec![
                Slice { weight: 0.6, text: "MSFT\n60.0%".into(), small: false },
                Slice { weight: 0.38, text: "AAPL\n38.0%".into(), small: false },
                Slice { weight: 0.02, text: "KO 2.0%".into(), small: true },
            ],
            peg: "1.42".into(),
            badge: Some(" +0.35 SD ".into()),
            badge_red: true,
            stats_box: "Portfolio:  3 positions\n-----------------------\n- W. PE:           28.7".into(),
            quality_box: "Quality (weighted)\n-----------------------\n- Income Grade:      A+\n\n- SPY        14.0%".into(),
        }
    }

    #[test]
    fn width_follows_the_content_and_scale_converges() {
        let Some(f) = fonts() else { return };
        let l = layout(&sample(), &f).unwrap();
        assert!(l.width_in > 8.0 && l.width_in < 14.0, "{l:?}");
        assert!((l.width_in / BASE_WIDTH_IN - l.s).abs() < 0.01, "{l:?}");
        assert!(l.box_gap_in >= 0.1);
        let scene = build(&sample(), &l, &f, 100.0).unwrap();
        assert_eq!(scene.height, 700);
        assert_eq!(scene.width, (l.width_in * 100.0).round() as u32);
        assert!(scene.ops.len() > 20);
    }

    #[test]
    fn degenerate_inputs_are_left_to_matplotlib() {
        let Some(f) = fonts() else { return };
        let mut d = sample();
        d.slices.clear();
        assert!(matches!(layout(&d, &f), Err(ChartError::Unsupported(_))));
        let mut d = sample();
        d.slices[0].weight = f64::NAN;
        assert!(matches!(layout(&d, &f), Err(ChartError::Unsupported(_))));
    }
}
