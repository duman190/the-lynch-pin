//! The render request ui/fastchart.py sends as JSON. Python formats every string the chart shows (with the same
//! f-strings graphics/visualizer.py uses); this crate only lays them out and draws.
use serde::Deserialize;

#[derive(Deserialize, Debug, Clone)]
pub struct Spec {
    /// Full-size PNG (the lightbox), written atomically. None = skip.
    #[serde(default)]
    pub png: Option<String>,
    #[serde(default = "default_dpi")]
    pub png_dpi: f64,
    /// Inline JPEG preview, drawn at its own size instead of shrinking the PNG. None = skip.
    #[serde(default)]
    pub jpeg: Option<String>,
    #[serde(default = "default_jpeg_width")]
    pub jpeg_width: u32,
    #[serde(default = "default_jpeg_quality")]
    pub jpeg_quality: u8,
    /// Worker threads for the PNG (bands drawn and compressed in parallel); 0 = one per core, at most 8.
    #[serde(default)]
    pub threads: usize,
    pub fonts: FontPaths,
    pub chart: Chart,
}

/// TrueType files matplotlib would pick (DejaVu, bundled with matplotlib).
#[derive(Deserialize, Debug, Clone)]
pub struct FontPaths {
    pub sans: String,
    pub sans_bold: String,
    pub mono_bold: String,
}

/// The per-ticker PEG distribution chart (LynchPinVisualizer.plot_ticker_distribution).
#[derive(Deserialize, Debug, Clone)]
pub struct Chart {
    pub title: String,
    pub peg: f64,
    pub mean: f64,
    pub z: f64,
    /// The red marker's badge, e.g. " -0.19 SD ".
    pub badge: String,
    /// Valuation + technicals box (top left), lines joined by '\n'.
    pub left_box: String,
    /// Income grade + credit rating box (top right); empty = no box.
    #[serde(default)]
    pub right_box: String,
}

fn default_dpi() -> f64 {
    300.0
}

fn default_jpeg_width() -> u32 {
    1100
}

fn default_jpeg_quality() -> u8 {
    85
}
