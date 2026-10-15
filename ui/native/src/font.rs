//! TrueType text without a text engine: glyphs are positioned with the font's advances and legacy `kern` pairs,
//! and their outlines (ttf-parser) are rasterised by tiny-skia into coverage masks cached per size and sub-pixel
//! position. Measurements follow matplotlib 3.11's Agg backend: the width is the pen advance and the height /
//! descent come from the ink of the glyphs, all in 1/64 px like FreeType's 26.6 numbers.
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use tiny_skia::{FillRule, Mask, Path, PathBuilder, Transform};
use ttf_parser::{Face, GlyphId, OutlineBuilder};

pub struct Glyph {
    advance: f64,
    /// Ink box in font units (x0, y0, x1, y1), y up; None for blanks.
    bbox: Option<(f64, f64, f64, f64)>,
    /// Outline in font units, y up; None for blanks.
    path: Option<Path>,
    /// Rasterised coverage per (scale, sub-pixel x, sub-pixel y): text is mostly the same few glyphs, so after
    /// the first chart drawing text is blitting.
    masks: Mutex<HashMap<(u64, u8, u8), Arc<GlyphMask>>>,
}

/// Anti-aliased coverage of one glyph, placed relative to the whole pixel that holds its origin.
pub struct GlyphMask {
    pub left: i32,
    pub top: i32,
    pub width: u32,
    pub height: u32,
    pub data: Vec<u8>,
}

/// Glyph origins are rounded to 1/SUBPIXELS px (FreeType-style subpixel positioning) to share masks.
const SUBPIXELS: f64 = 4.0;

impl Glyph {
    fn mask(&self, scale: f64, fx: u8, fy: u8) -> Option<Arc<GlyphMask>> {
        let (path, (bx0, by0, bx1, by1)) = (self.path.as_ref()?, self.bbox?);
        let key = (scale.to_bits(), fx, fy);
        let mut masks = self.masks.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(m) = masks.get(&key) {
            return Some(m.clone());
        }
        let (ox, oy) = (fx as f64 / SUBPIXELS, fy as f64 / SUBPIXELS);
        let left = (ox + bx0 * scale).floor() as i32 - 1;
        let top = (oy - by1 * scale).floor() as i32 - 1;
        let width = ((ox + bx1 * scale).ceil() as i32 + 1 - left).max(1) as u32;
        let height = ((oy - by0 * scale).ceil() as i32 + 1 - top).max(1) as u32;
        let mut mask = Mask::new(width, height)?;
        let s = scale as f32;
        let tf = Transform::from_row(s, 0.0, 0.0, -s, (ox - left as f64) as f32, (oy - top as f64) as f32);
        mask.fill_path(path, FillRule::Winding, true, tf);
        let m = Arc::new(GlyphMask { left, top, width, height, data: mask.take() });
        masks.insert(key, m.clone());
        Some(m)
    }
}

pub struct Font {
    face: Face<'static>,
    upem: f64,
    /// OS/2 typographic metrics (hhea as a fallback), font units; descent is positive.
    ascent: f64,
    descent: f64,
    line_gap: f64,
    glyphs: Mutex<HashMap<char, (GlyphId, Arc<Glyph>)>>,
}

/// A line of text laid out at one size: pen positions in px from the line's origin.
pub struct Shaped {
    pub glyphs: Vec<(Arc<Glyph>, f64)>,
    /// Pen advance of the whole line (matplotlib's text width).
    pub width: f64,
    /// Ink extent (bottom, top) in px, y up; None when the line has no ink.
    pub ink: Option<(f64, f64)>,
    scale: f64,
}

/// FreeType's 26.6 fixed point.
fn q64(v: f64) -> f64 {
    (v * 64.0).round() / 64.0
}

impl Font {
    fn parse(data: &'static [u8]) -> Result<Font, String> {
        let face = Face::parse(data, 0).map_err(|e| format!("bad font: {e}"))?;
        let upem = face.units_per_em() as f64;
        let (ascent, descent, line_gap) = match face.tables().os2 {
            Some(os2) => (
                os2.typographic_ascender() as f64,
                -(os2.typographic_descender() as f64),
                os2.typographic_line_gap() as f64,
            ),
            None => (face.ascender() as f64, -(face.descender() as f64), face.line_gap() as f64),
        };
        Ok(Font { face, upem, ascent, descent, line_gap, glyphs: Mutex::new(HashMap::new()) })
    }

    /// px per font unit at ``size_pt`` points and ``dpi``.
    pub fn scale(&self, size_pt: f64, dpi: f64) -> f64 {
        size_pt * dpi / 72.0 / self.upem
    }

    /// (ascent, descent, line gap) in px: matplotlib's minimum line box.
    pub fn line_metrics(&self, scale: f64) -> (f64, f64, f64) {
        (self.ascent * scale, self.descent * scale, self.line_gap * scale)
    }

    fn glyph(&self, c: char) -> (GlyphId, Arc<Glyph>) {
        let mut cache = self.glyphs.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(g) = cache.get(&c) {
            return g.clone();
        }
        let id = self.face.glyph_index(c).unwrap_or(GlyphId(0));
        let advance = self.face.glyph_hor_advance(id).unwrap_or(0) as f64;
        let mut builder = Outline(PathBuilder::new());
        let bbox = self
            .face
            .outline_glyph(id, &mut builder)
            .map(|r| (r.x_min as f64, r.y_min as f64, r.x_max as f64, r.y_max as f64));
        let path = if bbox.is_some() { builder.0.finish() } else { None };
        let g = (id, Arc::new(Glyph { advance, bbox, path, masks: Mutex::new(HashMap::new()) }));
        cache.insert(c, g.clone());
        g
    }

    fn kerning(&self, left: GlyphId, right: GlyphId) -> f64 {
        let Some(kern) = self.face.tables().kern else { return 0.0 };
        kern.subtables
            .into_iter()
            .filter(|s| s.horizontal && !s.variable && !s.has_cross_stream)
            .find_map(|s| s.glyphs_kerning(left, right))
            .unwrap_or(0) as f64
    }

    pub fn shape(&self, text: &str, scale: f64) -> Shaped {
        let mut glyphs = Vec::with_capacity(text.len());
        let mut pen = 0.0;
        let mut ink: Option<(f64, f64)> = None;
        let mut prev: Option<GlyphId> = None;
        for c in text.chars() {
            let (id, g) = self.glyph(c);
            if let Some(p) = prev {
                pen += q64(self.kerning(p, id) * scale);
            }
            if let Some((_, y0, _, y1)) = g.bbox {
                let (b, t) = (q64(y0 * scale), q64(y1 * scale));
                ink = Some(match ink {
                    Some((lo, hi)) => (lo.min(b), hi.max(t)),
                    None => (b, t),
                });
            }
            let advance = q64(g.advance * scale);
            glyphs.push((g, pen));
            pen += advance;
            prev = Some(id);
        }
        Shaped { glyphs, width: pen, ink, scale }
    }
}

impl Shaped {
    /// matplotlib's (width, height, descent) of one line: pen width, ink height, ink depth below the baseline.
    pub fn whd(&self) -> (f64, f64, f64) {
        match self.ink {
            Some((lo, hi)) => (self.width, hi - lo, -lo),
            None => (self.width, 0.0, 0.0),
        }
    }

    /// Glyph masks for the line's origin (pen 0, baseline) at device (x, y), y down, with the device pixel of each
    /// mask's top-left corner.
    pub fn masks_at(&self, x: f64, y: f64, out: &mut Vec<(Arc<GlyphMask>, i32, i32)>) {
        let snap = |v: f64| {
            let i = v.floor();
            let f = ((v - i) * SUBPIXELS).round();
            if f >= SUBPIXELS {
                (i as i32 + 1, 0u8)
            } else {
                (i as i32, f as u8)
            }
        };
        for (g, pen) in &self.glyphs {
            let ((ix, fx), (iy, fy)) = (snap(x + pen), snap(y));
            if let Some(m) = g.mask(self.scale, fx, fy) {
                let (l, t) = (ix + m.left, iy + m.top);
                out.push((m, l, t));
            }
        }
    }
}

struct Outline(PathBuilder);

impl OutlineBuilder for Outline {
    fn move_to(&mut self, x: f32, y: f32) {
        self.0.move_to(x, y);
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.0.line_to(x, y);
    }
    fn quad_to(&mut self, x1: f32, y1: f32, x: f32, y: f32) {
        self.0.quad_to(x1, y1, x, y);
    }
    fn curve_to(&mut self, x1: f32, y1: f32, x2: f32, y2: f32, x: f32, y: f32) {
        self.0.cubic_to(x1, y1, x2, y2, x, y);
    }
    fn close(&mut self) {
        self.0.close();
    }
}

/// Fonts are parsed once per process and kept (a few hundred KB each); glyph outlines are cached on first use.
pub fn load(path: &str) -> Result<Arc<Font>, String> {
    static FONTS: OnceLock<Mutex<HashMap<String, Arc<Font>>>> = OnceLock::new();
    let fonts = FONTS.get_or_init(Default::default);
    if let Some(f) = fonts.lock().unwrap_or_else(|e| e.into_inner()).get(path) {
        return Ok(f.clone());
    }
    let data = std::fs::read(path).map_err(|e| format!("cannot read font {path}: {e}"))?;
    let data: &'static [u8] = Box::leak(data.into_boxed_slice());
    let font = Arc::new(Font::parse(data)?);
    let mut fonts = fonts.lock().unwrap_or_else(|e| e.into_inner());
    Ok(fonts.entry(path.to_string()).or_insert(font).clone())
}
