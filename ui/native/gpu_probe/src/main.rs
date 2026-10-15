//! `cargo run --release -- SPEC.json` (spec from `python -m ui.fastchart --spec SPEC.json`), on a Mac.
//!
//! Would drawing the chart with Metal beat the CPU renderer? Whatever a GPU renderer does, it has to submit a
//! render pass over the 3600×2100 canvas and read the 30 MB image back for the PNG / JPEG encoders, which run on
//! the CPU. This times exactly that floor (an empty render pass that only clears the texture, and the read-back)
//! against the CPU drawing the real chart, both on this machine. A real Metal renderer would add tessellation,
//! uploads and shaders on top of the floor, so if the floor alone is not faster than the CPU draw, Metal cannot win.
use std::time::Instant;

use lynch_chart::layout::{Builder, Fonts};

fn min_ms(runs: usize, mut f: impl FnMut()) -> f64 {
    (0..runs)
        .map(|_| {
            let t = Instant::now();
            f();
            t.elapsed().as_secs_f64() * 1e3
        })
        .fold(f64::INFINITY, f64::min)
}

fn main() {
    let Some(path) = std::env::args().nth(1) else {
        eprintln!("usage: gpu_probe SPEC.json   (python -m ui.fastchart --spec SPEC.json writes one)");
        std::process::exit(2);
    };
    let spec: lynch_chart::Spec = serde_json::from_slice(&std::fs::read(&path).expect("read spec")).expect("spec");
    let load = |p: &str| lynch_chart::font::load(p).expect("font");
    let fonts = Fonts {
        sans: load(&spec.fonts.sans),
        sans_bold: load(&spec.fonts.sans_bold),
        mono_bold: load(&spec.fonts.mono_bold),
    };
    let scene = Builder::new(&spec.chart, &fonts, 300.0).expect("chart").build().expect("layout");
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).min(8);
    let _ = lynch_chart::draw(&scene, threads); // warm up: glyph masks, page faults
    let one = min_ms(20, || drop(lynch_chart::draw(&scene, 1)));
    let many = min_ms(20, || drop(lynch_chart::draw(&scene, threads)));
    println!(
        "CPU draw of the chart ({}×{}): {one:.2} ms on 1 thread, {many:.2} ms on {threads}",
        scene.width, scene.height
    );
    gpu(scene.width as u64, scene.height as u64, many);
}

#[cfg(not(target_os = "macos"))]
fn gpu(_w: u64, _h: u64, _cpu_ms: f64) {
    println!("Metal: not available on this OS (run this on the Mac)");
}

#[cfg(target_os = "macos")]
fn gpu(w: u64, h: u64, cpu_ms: f64) {
    use metal::*;
    let Some(device) = Device::system_default() else {
        println!("Metal: no device");
        return;
    };
    let queue = device.new_command_queue();
    let desc = TextureDescriptor::new();
    desc.set_width(w);
    desc.set_height(h);
    desc.set_pixel_format(MTLPixelFormat::RGBA8Unorm);
    desc.set_storage_mode(MTLStorageMode::Shared); // unified memory: the CPU reads it without a blit
    desc.set_usage(MTLTextureUsage::RenderTarget);
    let tex = device.new_texture(&desc);
    let mut pixels = vec![0u8; (w * h * 4) as usize];
    let pass = || {
        objc::rc::autoreleasepool(|| {
            let rpd = RenderPassDescriptor::new();
            let att = rpd.color_attachments().object_at(0).expect("attachment");
            att.set_texture(Some(&tex));
            att.set_load_action(MTLLoadAction::Clear);
            att.set_clear_color(MTLClearColor::new(0.07, 0.07, 0.07, 1.0));
            att.set_store_action(MTLStoreAction::Store);
            let cb = queue.new_command_buffer();
            cb.new_render_command_encoder(rpd).end_encoding();
            cb.commit();
            cb.wait_until_completed();
        })
    };
    pass(); // warm up
    let submit = min_ms(20, pass);
    let region = MTLRegion::new_2d(0, 0, w, h);
    let readback = min_ms(20, || tex.get_bytes(pixels.as_mut_ptr().cast(), w * 4, region, 0));
    let floor = submit + readback;
    println!(
        "Metal on {}: render pass {submit:.2} ms + read-back {readback:.2} ms = {floor:.2} ms floor",
        device.name()
    );
    if floor >= cpu_ms {
        println!("→ the GPU's unavoidable round-trip alone is not faster than the CPU drawing the whole chart: keep the CPU renderer");
    } else {
        println!(
            "→ a Metal renderer could save at most {:.2} ms of drawing per chart (less its real drawing work); \
             the PNG / JPEG encoding stays on the CPU",
            cpu_ms - floor
        );
    }
}
