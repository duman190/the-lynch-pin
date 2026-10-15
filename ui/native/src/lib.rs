//! Native renderer for the portal's per-ticker PEG chart, a drop-in for
//! `graphics.visualizer.LynchPinVisualizer.plot_ticker_distribution` (ui/fastchart.py builds the request and
//! loads this library with ctypes; ctypes drops the GIL for the call).
//!
//! The chart is laid out like matplotlib does it (layout.rs), rasterised with tiny-skia (NEON / SSE SIMD) in
//! horizontal bands on several threads, and written as a 300-dpi PNG (bands deflated in parallel) plus a JPEG
//! preview drawn at its own size on another thread.
pub mod encode;
pub mod font;
pub mod layout;
pub mod raster;
pub mod spec;

use std::time::Instant;

pub use spec::Spec;

#[derive(Debug)]
pub enum ChartError {
    /// An input this renderer does not draw like matplotlib would (e.g. a degenerate distribution): the caller
    /// should fall back to matplotlib.
    Unsupported(String),
    Failed(String),
}

impl std::fmt::Display for ChartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ChartError::Unsupported(m) => write!(f, "unsupported: {m}"),
            ChartError::Failed(m) => write!(f, "{m}"),
        }
    }
}

/// Milliseconds per step of one render (the PNG and the preview run concurrently). The PNG's draw and encode
/// figures are summed over its bands (CPU time); ``png_ms`` is its wall time.
#[derive(Debug, Default, serde::Serialize)]
pub struct Timings {
    pub layout_ms: f64,
    pub png_ms: f64,
    pub png_draw_cpu_ms: f64,
    pub png_encode_cpu_ms: f64,
    pub png_write_ms: f64,
    pub png_bytes: usize,
    pub jpeg_ms: f64,
    pub jpeg_bytes: usize,
    pub total_ms: f64,
    pub threads: usize,
    pub bands: usize,
}

fn ms(t: Instant) -> f64 {
    t.elapsed().as_secs_f64() * 1000.0
}

fn fonts(spec: &Spec) -> Result<layout::Fonts, ChartError> {
    let load = |p: &str| font::load(p).map_err(ChartError::Failed);
    Ok(layout::Fonts {
        sans: load(&spec.fonts.sans)?,
        sans_bold: load(&spec.fonts.sans_bold)?,
        mono_bold: load(&spec.fonts.mono_bold)?,
    })
}

/// Draws ``scene`` into a fresh RGBA buffer using ``threads`` bands.
pub fn draw(scene: &layout::Scene, threads: usize) -> Vec<u8> {
    let (w, h) = (scene.width as usize, scene.height as usize);
    let mut buf = vec![0u8; w * h * 4];
    let bands = band_rows(h, threads);
    std::thread::scope(|s| {
        let mut rest: &mut [u8] = &mut buf;
        for &(y0, y1) in &bands {
            let (mine, tail) = std::mem::take(&mut rest).split_at_mut((y1 - y0) * w * 4);
            rest = tail;
            if y1 == h {
                raster::draw_band(scene, mine, y0 as u32, (y1 - y0) as u32); // the last band on this thread
            } else {
                s.spawn(move || raster::draw_band(scene, mine, y0 as u32, (y1 - y0) as u32));
            }
        }
    });
    buf
}

fn band_rows(h: usize, n: usize) -> Vec<(usize, usize)> {
    let n = n.clamp(1, h.max(1));
    let per = h.div_ceil(n);
    (0..n).map(|i| (i * per, ((i + 1) * per).min(h))).filter(|(a, b)| a < b).collect()
}

fn thread_count(requested: usize) -> usize {
    if requested > 0 {
        return requested.min(64);
    }
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).min(8)
}

/// The PNG of ``scene``: ``threads`` workers take bands of rows off a queue, draw each into its own buffer and
/// deflate it straight away (bands are independent, see encode::png_segment); the segments are then joined.
/// Twice as many bands as workers keeps the cores busy although bands differ in cost (text, boxes, curve).
fn png(scene: &layout::Scene, dpi: f64, threads: usize, tm: &mut Timings) -> Vec<u8> {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;
    let (w, h) = (scene.width as usize, scene.height as usize);
    let bands = band_rows(h, threads * 2);
    let next = AtomicUsize::new(0);
    let done: Vec<Mutex<Option<(encode::Segment, f64, f64)>>> = bands.iter().map(|_| Mutex::new(None)).collect();
    std::thread::scope(|s| {
        let work = || loop {
            let i = next.fetch_add(1, Ordering::Relaxed);
            let Some(&(y0, y1)) = bands.get(i) else { break };
            let t = Instant::now();
            let mut buf = vec![0u8; (y1 - y0) * w * 4];
            raster::draw_band(scene, &mut buf, y0 as u32, (y1 - y0) as u32);
            let drawn = ms(t);
            let t = Instant::now();
            let seg = encode::png_segment(&buf, w, y1 - y0, y1 == h);
            *done[i].lock().unwrap_or_else(|e| e.into_inner()) = Some((seg, drawn, ms(t)));
        };
        for _ in 1..threads.min(bands.len()) {
            s.spawn(work);
        }
        work();
    });
    let mut segments = Vec::with_capacity(bands.len());
    for d in done {
        let (seg, drawn, encoded) = d.into_inner().unwrap_or_else(|e| e.into_inner()).expect("band not drawn");
        tm.png_draw_cpu_ms += drawn;
        tm.png_encode_cpu_ms += encoded;
        segments.push(seg);
    }
    tm.bands = bands.len();
    encode::png_file(scene.width, scene.height, dpi, &segments)
}

/// Renders the chart described by ``spec`` to the requested files.
pub fn render(spec: &Spec) -> Result<Timings, ChartError> {
    let t0 = Instant::now();
    let fonts = fonts(spec)?;
    let threads = thread_count(spec.threads);
    let mut tm = Timings { threads, ..Timings::default() };

    let full = match &spec.png {
        Some(_) => Some(layout::Builder::new(&spec.chart, &fonts, spec.png_dpi)?.build()?),
        None => None,
    };
    tm.layout_ms = ms(t0);

    let preview = std::thread::scope(|s| -> Result<(f64, usize), ChartError> {
        let jpeg_job = spec.jpeg.as_ref().map(|path| {
            let fonts = &fonts;
            s.spawn(move || -> Result<(f64, usize), ChartError> {
                let t = Instant::now();
                let dpi = spec.jpeg_width as f64 / 12.0;
                let scene = layout::Builder::new(&spec.chart, fonts, dpi)?.build()?;
                let buf = draw(&scene, 1);
                let bytes =
                    encode::jpeg(&buf, scene.width, scene.height, spec.jpeg_quality).map_err(ChartError::Failed)?;
                encode::write_atomic(path, &bytes).map_err(ChartError::Failed)?;
                Ok((ms(t), bytes.len()))
            })
        });
        if let (Some(scene), Some(path)) = (&full, &spec.png) {
            let t = Instant::now();
            let png = png(scene, spec.png_dpi, threads, &mut tm);
            tm.png_ms = ms(t);
            let t = Instant::now();
            encode::write_atomic(path, &png).map_err(ChartError::Failed)?;
            tm.png_write_ms = ms(t);
            tm.png_bytes = png.len();
        }
        match jpeg_job {
            Some(j) => j.join().map_err(|_| ChartError::Failed("preview thread panicked".into()))?,
            None => Ok((0.0, 0)),
        }
    })?;
    (tm.jpeg_ms, tm.jpeg_bytes) = preview;
    tm.total_ms = ms(t0);
    Ok(tm)
}

fn write_c(buf: *mut u8, cap: usize, msg: &str) {
    if buf.is_null() || cap == 0 {
        return;
    }
    let n = msg.len().min(cap - 1);
    // SAFETY: the caller hands us a writable buffer of ``cap`` bytes.
    unsafe {
        std::ptr::copy_nonoverlapping(msg.as_ptr(), buf, n);
        *buf.add(n) = 0;
    }
}

/// C entry point. ``spec`` is UTF-8 JSON (see spec.rs) of ``len`` bytes. Returns 0 on success (``out`` gets the
/// timings as JSON), 1 on failure and 2 for an input left to matplotlib (``out`` gets the reason).
///
/// # Safety
/// ``spec`` must point to ``len`` readable bytes and ``out`` to ``out_cap`` writable bytes (or be null).
#[no_mangle]
pub unsafe extern "C" fn lynch_chart_render(spec: *const u8, len: usize, out: *mut u8, out_cap: usize) -> i32 {
    let result = std::panic::catch_unwind(|| {
        // SAFETY: guaranteed by the caller (see above).
        let bytes = unsafe { std::slice::from_raw_parts(spec, len) };
        let spec: Spec = serde_json::from_slice(bytes).map_err(|e| ChartError::Failed(format!("bad spec: {e}")))?;
        render(&spec)
    });
    match result {
        Ok(Ok(t)) => {
            write_c(out, out_cap, &serde_json::to_string(&t).unwrap_or_default());
            0
        }
        Ok(Err(ChartError::Unsupported(m))) => {
            write_c(out, out_cap, &m);
            2
        }
        Ok(Err(ChartError::Failed(m))) => {
            write_c(out, out_cap, &m);
            1
        }
        Err(_) => {
            write_c(out, out_cap, "renderer panicked");
            1
        }
    }
}

/// Bumped whenever the C interface or the spec changes incompatibly.
#[no_mangle]
pub extern "C" fn lynch_chart_abi() -> u32 {
    1
}
