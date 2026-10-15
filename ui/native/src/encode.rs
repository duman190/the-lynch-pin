//! PNG and JPEG writers. The PNG is compressed in parallel the way pigz does it: every band of rows is filtered
//! and deflated on its own thread into a raw deflate segment that ends on a byte boundary (sync flush), the
//! segments are concatenated into one zlib stream and their Adler-32 checksums combined.
use std::io::Write;

use miniz_oxide::deflate::core::{
    compress, create_comp_flags_from_zip_params, CompressionStrategy, CompressorOxide, TDEFLFlush, TDEFLStatus,
};

/// One band's compressed IDAT payload.
pub struct Segment {
    data: Vec<u8>,
    adler: u32,
    len: usize,
}

const MOD_ADLER: u32 = 65521;

fn adler32(data: &[u8]) -> u32 {
    let (mut a, mut b) = (1u32, 0u32);
    for chunk in data.chunks(5552) {
        for &x in chunk {
            a += x as u32;
            b += a;
        }
        a %= MOD_ADLER;
        b %= MOD_ADLER;
    }
    (b << 16) | a
}

/// zlib's adler32_combine: the checksum of A‖B from those of A and B.
fn adler32_combine(a1: u32, a2: u32, len2: usize) -> u32 {
    let rem = (len2 % MOD_ADLER as usize) as u32;
    let mut sum1 = a1 & 0xffff;
    let mut sum2 = ((rem as u64 * sum1 as u64) % MOD_ADLER as u64) as u32;
    sum1 += (a2 & 0xffff) + MOD_ADLER - 1;
    sum2 += ((a1 >> 16) & 0xffff) + ((a2 >> 16) & 0xffff) + MOD_ADLER - rem;
    if sum1 >= MOD_ADLER {
        sum1 -= MOD_ADLER;
    }
    if sum1 >= MOD_ADLER {
        sum1 -= MOD_ADLER;
    }
    if sum2 >= MOD_ADLER << 1 {
        sum2 -= MOD_ADLER << 1;
    }
    if sum2 >= MOD_ADLER {
        sum2 -= MOD_ADLER;
    }
    sum1 | (sum2 << 16)
}

/// Filters ``rows`` rows of an opaque RGBA band (width ``w``) to RGB scanlines and deflates them into a segment
/// that ends on a byte boundary (``last``: ends the stream). Rows use the Up filter, which beats per-row
/// adaptive choice on these charts (flat background, a vertically shaded fill) at a quarter of the cost; the
/// band's first row uses Sub, so a band never needs the rows of the band above it.
pub fn png_segment(rgba: &[u8], w: usize, rows: usize, last: bool) -> Segment {
    let bpl = w * 3;
    let mut filtered = vec![0u8; rows * (bpl + 1)];
    for (y, out) in filtered.chunks_exact_mut(bpl + 1).enumerate() {
        let cur = &rgba[y * w * 4..(y + 1) * w * 4];
        let (kind, body) = out.split_at_mut(1);
        if y == 0 {
            kind[0] = 1; // Sub
            let mut left = [0u8; 3];
            for (d, s) in body.chunks_exact_mut(3).zip(cur.chunks_exact(4)) {
                for c in 0..3 {
                    d[c] = s[c].wrapping_sub(left[c]);
                }
                left = [s[0], s[1], s[2]];
            }
        } else {
            kind[0] = 2; // Up
            let prev = &rgba[(y - 1) * w * 4..y * w * 4];
            for ((d, s), p) in body.chunks_exact_mut(3).zip(cur.chunks_exact(4)).zip(prev.chunks_exact(4)) {
                d[0] = s[0].wrapping_sub(p[0]);
                d[1] = s[1].wrapping_sub(p[1]);
                d[2] = s[2].wrapping_sub(p[2]);
            }
        }
    }
    let adler = adler32(&filtered);
    let flags = create_comp_flags_from_zip_params(1, 0, CompressionStrategy::RLE as i32);
    let mut c = Box::new(CompressorOxide::new(flags));
    let mut out = vec![0u8; filtered.len() / 2 + 1024];
    let (mut pos_in, mut pos_out) = (0, 0);
    let flush = if last { TDEFLFlush::Finish } else { TDEFLFlush::Sync };
    loop {
        let (status, n_in, n_out) = compress(&mut c, &filtered[pos_in..], &mut out[pos_out..], flush);
        pos_in += n_in;
        pos_out += n_out;
        match status {
            TDEFLStatus::Done => break,
            TDEFLStatus::Okay if pos_in >= filtered.len() && pos_out < out.len() => break,
            TDEFLStatus::Okay => {
                let grow = out.len();
                out.resize(out.len() + grow, 0);
            }
            _ => break,
        }
    }
    out.truncate(pos_out);
    Segment { data: out, adler, len: filtered.len() }
}

fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], body: &[u8]) {
    out.extend_from_slice(&(body.len() as u32).to_be_bytes());
    let start = out.len();
    out.extend_from_slice(kind);
    out.extend_from_slice(body);
    let crc = crc32fast::hash(&out[start..]);
    out.extend_from_slice(&crc.to_be_bytes());
}

/// Assembles an 8-bit RGB PNG from the bands' segments (in row order).
pub fn png_file(w: u32, h: u32, dpi: f64, segments: &[Segment]) -> Vec<u8> {
    let total: usize = segments.iter().map(|s| s.data.len()).sum();
    let mut out = Vec::with_capacity(total + 128);
    out.extend_from_slice(b"\x89PNG\r\n\x1a\n");
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&w.to_be_bytes());
    ihdr.extend_from_slice(&h.to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ihdr);
    let ppm = (dpi / 0.0254).round() as u32; // pixels per metre, as matplotlib writes
    let mut phys = Vec::with_capacity(9);
    phys.extend_from_slice(&ppm.to_be_bytes());
    phys.extend_from_slice(&ppm.to_be_bytes());
    phys.push(1);
    chunk(&mut out, b"pHYs", &phys);
    let mut idat = Vec::with_capacity(total + 6);
    idat.extend_from_slice(&[0x78, 0x01]);
    let mut adler = 1u32;
    for s in segments {
        idat.extend_from_slice(&s.data);
        adler = adler32_combine(adler, s.adler, s.len);
    }
    idat.extend_from_slice(&adler.to_be_bytes());
    chunk(&mut out, b"IDAT", &idat);
    chunk(&mut out, b"IEND", &[]);
    out
}

/// Baseline JPEG with optimised Huffman tables: as small as Pillow's progressive preview, and faster to make.
pub fn jpeg(rgba: &[u8], w: u32, h: u32, quality: u8) -> Result<Vec<u8>, String> {
    let rgb: Vec<u8> = rgba.chunks_exact(4).flat_map(|p| [p[0], p[1], p[2]]).collect();
    let mut out = Vec::with_capacity(rgb.len() / 8);
    let mut enc = jpeg_encoder::Encoder::new(&mut out, quality);
    enc.set_optimized_huffman_tables(true);
    enc.encode(&rgb, w as u16, h as u16, jpeg_encoder::ColorType::Rgb).map_err(|e| format!("jpeg: {e}"))?;
    Ok(out)
}

/// Writes ``bytes`` next to ``path`` and renames it into place, so readers never see a partial file.
pub fn write_atomic(path: &str, bytes: &[u8]) -> Result<(), String> {
    let nanos =
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
    let tmp = format!("{path}.{}.{nanos}.part", std::process::id());
    let res =
        std::fs::File::create(&tmp).and_then(|mut f| f.write_all(bytes)).and_then(|_| std::fs::rename(&tmp, path));
    res.map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("cannot write {path}: {e}")
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adler_combine_matches_whole() {
        let data: Vec<u8> = (0..100_000u32).map(|i| (i * 7 + i / 13) as u8).collect();
        let (a, b) = data.split_at(37_123);
        assert_eq!(adler32_combine(adler32(a), adler32(b), b.len()), adler32(&data));
    }
}
