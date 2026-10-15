//! Draws a [`Scene`] into a horizontal band of rows. Bands of one image can be drawn on different threads: each
//! is a `PixmapMut` over its own slice of the shared RGBA buffer, offset by its first row. The canvas is opaque
//! (the dark background is drawn first), so premultiplied and straight RGBA coincide and the hand-written loops
//! below blend RGB and leave alpha at 255.
use tiny_skia::{Color, FillRule, Paint, PixmapMut, Shader, Transform};

use crate::layout::{Op, Rgba, RoundBox, Scene, BG};

fn paint(c: Rgba) -> Paint<'static> {
    Paint {
        shader: Shader::SolidColor(Color::from_rgba(c.r, c.g, c.b, c.a).unwrap_or(Color::BLACK)),
        anti_alias: true,
        ..Paint::default()
    }
}

fn rgb8(c: Rgba) -> [u8; 3] {
    [(c.r * 255.0).round() as u8, (c.g * 255.0).round() as u8, (c.b * 255.0).round() as u8]
}

/// dst + (src - dst) · a / 255, rounded.
#[inline(always)]
fn lerp(dst: u8, src: u8, a: u32) -> u8 {
    let d = dst as i32;
    let v = d * 255 + (src as i32 - d) * a as i32;
    ((v + 127) / 255) as u8
}

/// Draws rows ``y0 .. y0 + rows`` of ``scene`` into ``buf`` (RGBA, ``rows`` × width).
pub fn draw_band(scene: &Scene, buf: &mut [u8], y0: u32, rows: u32) {
    let w = scene.width as usize;
    let stride = w * 4;
    let bg = rgb8(BG);
    let bg_px = [bg[0], bg[1], bg[2], 255];
    for px in buf[..rows as usize * stride].chunks_exact_mut(4) {
        px.copy_from_slice(&bg_px);
    }

    // The area under the curve: one colour per row between the curve's flanks
    for r in y0..y0 + rows {
        let i = r as i64 - scene.bell_first_row;
        let Some(Some(row)) = (i >= 0).then(|| scene.bell.get(i as usize)).flatten() else { continue };
        let (x0, x1) = (row.x0.max(0.0), row.x1.min(w as f32));
        if x1 <= x0 {
            continue;
        }
        let line = &mut buf[(r - y0) as usize * stride..][..stride];
        let (p0, p1) = (x0.floor() as usize, (x1.ceil() as usize).min(w));
        let px = [row.rgb[0], row.rgb[1], row.rgb[2], 255];
        let (full0, full1) = (x0.ceil() as usize, (x1.floor() as usize).max(x0.ceil() as usize));
        for p in line[full0 * 4..full1 * 4].chunks_exact_mut(4) {
            p.copy_from_slice(&px);
        }
        for p in [p0, p1.saturating_sub(1)] {
            if p >= full0 && p < full1 {
                continue;
            }
            let cov = ((p as f32 + 1.0).min(x1) - (p as f32).max(x0)).clamp(0.0, 1.0);
            let a = (cov * 255.0).round() as u32;
            for c in 0..3 {
                line[p * 4 + c] = lerp(bg[c], row.rgb[c], a);
            }
        }
    }

    let Some(mut pm) = PixmapMut::from_bytes(buf, w as u32, rows) else { return };
    let tf = Transform::from_translate(0.0, -(y0 as f32));
    let (top, bottom) = (y0 as f32, (y0 + rows) as f32);
    for d in &scene.ops {
        if d.bottom < top || d.top > bottom {
            continue;
        }
        match &d.op {
            Op::Fill(p, c) => pm.fill_path(p, &paint(*c), FillRule::Winding, tf, None),
            Op::Stroke(p, c, s) => pm.stroke_path(p, &paint(*c), s, tf, None),
            Op::Rect(r, c) => pm.fill_rect(*r, &paint(*c), tf, None),
            Op::Glyphs(glyphs, c) => blit_glyphs(pm.data_mut(), w, y0 as i32, rows as i32, glyphs, *c),
            Op::RoundBox(b, c) => fill_round_box(pm.data_mut(), w, y0, rows, b, *c),
        }
    }
}

fn blit_glyphs(
    data: &mut [u8],
    w: usize,
    y0: i32,
    rows: i32,
    glyphs: &[(std::sync::Arc<crate::font::GlyphMask>, i32, i32)],
    c: Rgba,
) {
    let src = rgb8(c);
    let alpha = (c.a * 255.0).round() as u32;
    for (m, left, top) in glyphs {
        let (mw, mh) = (m.width as i32, m.height as i32);
        let (ry0, ry1) = ((*top).max(y0), (top + mh).min(y0 + rows));
        let (cx0, cx1) = ((*left).max(0), (left + mw).min(w as i32));
        if ry0 >= ry1 || cx0 >= cx1 {
            continue;
        }
        for y in ry0..ry1 {
            let mrow = &m.data[((y - top) * mw) as usize..][..mw as usize];
            let drow = &mut data[(y - y0) as usize * w * 4..][..w * 4];
            for x in cx0..cx1 {
                let cov = mrow[(x - left) as usize] as u32;
                if cov == 0 {
                    continue;
                }
                let a = if alpha == 255 { cov } else { (cov * alpha + 127) / 255 };
                let o = x as usize * 4;
                for k in 0..3 {
                    drow[o + k] = lerp(drow[o + k], src[k], a);
                }
            }
        }
    }
}

/// Fills a rounded box row by row: each row is a span between the box's (possibly curved) sides, blended with
/// one alpha; only the two end pixels and the top/bottom rows get partial coverage.
fn fill_round_box(data: &mut [u8], w: usize, y0: u32, rows: u32, b: &RoundBox, c: Rgba) {
    let src = rgb8(c);
    let alpha = c.a as f64;
    let r0 = (b.top.floor().max(y0 as f64)) as i64;
    let r1 = (b.bottom.ceil().min((y0 + rows) as f64)) as i64;
    for r in r0..r1 {
        let rf = r as f64;
        let vc = ((rf + 1.0).min(b.bottom) - rf.max(b.top)).clamp(0.0, 1.0);
        if vc <= 0.0 {
            continue;
        }
        let yc = (rf + 0.5).clamp(b.top, b.bottom);
        let v = (yc - b.top).min(b.bottom - yc);
        // corners are quadratic Béziers with the control point on the box corner: x = t²·r, y = (1 - t)²·r
        let inset = if v < b.radius && b.radius > 0.0 { (1.0 - (v / b.radius).sqrt()).powi(2) * b.radius } else { 0.0 };
        let (xl, xr) = ((b.left + inset).max(0.0), (b.right - inset).min(w as f64));
        if xr <= xl {
            continue;
        }
        let line = &mut data[(r as u32 - y0) as usize * w * 4..][..w * 4];
        let (f0, f1) = (xl.ceil() as usize, (xr.floor() as usize).max(xl.ceil() as usize));
        let a = (alpha * vc * 255.0).round() as u32;
        for p in line[f0 * 4..f1 * 4].chunks_exact_mut(4) {
            for k in 0..3 {
                p[k] = lerp(p[k], src[k], a);
            }
        }
        for p in [xl.floor() as usize, (xr.ceil() as usize).saturating_sub(1)] {
            if (p >= f0 && p < f1) || p >= w {
                continue;
            }
            let hc = ((p as f64 + 1.0).min(xr) - (p as f64).max(xl)).clamp(0.0, 1.0);
            let a = (alpha * vc * hc * 255.0).round() as u32;
            for k in 0..3 {
                line[p * 4 + k] = lerp(line[p * 4 + k], src[k], a);
            }
        }
    }
}
