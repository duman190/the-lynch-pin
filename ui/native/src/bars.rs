//! The trading view's data: Yahoo's chart JSON (`/v8/finance/chart/SYM`, as Yahoo sends it) → candles at the
//! chart's interval, with SMA 50/100/200, EMA 8/21, RSI 14 and MACD 12/26/9, written as the JSON the page draws
//! (and its gzip). ui/tvdata.py hands the raw response bytes over with ctypes, which drops the GIL for the call, so
//! the portal's HTTP threads build charts on every core while Python never touches a bar.
//!
//! Intervals Yahoo doesn't serve are built from one it does: 3m from 1m, 10m from 5m, 30m and 45m from 15m, 2H to
//! 4H from 1H, 3M / 6M / 12M from 1M. Intraday bars are bucketed from each session's open (Yahoo's
//! `tradingPeriods`, one per day with its own UTC offset, so DST changes nothing), as TradingView does: a 4H
//! chart of a US stock has a 9:30-13:30 and a 13:30-16:00 bar. The "live" bar Yahoo appends at the close
//! (16:00, or 16:00:01 on a weekly / monthly series) is folded into the bar it belongs to.
//!
//! Indicators follow Pine Script: an EMA is seeded with the SMA of its first `n` values, RSI uses Wilder's
//! moving average seeded the same way, and MACD's signal is the 9-EMA of the MACD line from its first value.
use serde::Deserialize;

#[derive(Deserialize)]
struct Envelope {
    chart: ChartBody,
}

#[derive(Deserialize)]
struct ChartBody {
    #[serde(default)]
    result: Option<Vec<ChartResult>>,
    #[serde(default)]
    error: Option<YahooError>,
}

#[derive(Deserialize)]
struct YahooError {
    #[serde(default)]
    code: Option<String>,
    #[serde(default)]
    description: Option<String>,
}

#[derive(Deserialize)]
struct ChartResult {
    meta: Meta,
    #[serde(default)]
    timestamp: Vec<i64>,
    #[serde(default)]
    indicators: Option<Indicators>,
}

#[derive(Deserialize)]
struct Indicators {
    #[serde(default)]
    quote: Vec<Quote>,
}

#[derive(Deserialize, Default)]
struct Quote {
    #[serde(default)]
    open: Vec<Option<f64>>,
    #[serde(default)]
    high: Vec<Option<f64>>,
    #[serde(default)]
    low: Vec<Option<f64>>,
    #[serde(default)]
    close: Vec<Option<f64>>,
    #[serde(default)]
    volume: Vec<Option<f64>>,
}

#[derive(Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
struct Meta {
    symbol: Option<String>,
    currency: Option<String>,
    exchange_name: Option<String>,
    full_exchange_name: Option<String>,
    exchange_timezone_name: Option<String>,
    gmtoffset: Option<i64>,
    price_hint: Option<u32>,
    regular_market_price: Option<f64>,
    regular_market_time: Option<i64>,
    regular_market_day_high: Option<f64>,
    regular_market_day_low: Option<f64>,
    regular_market_volume: Option<f64>,
    previous_close: Option<f64>,
    long_name: Option<String>,
    short_name: Option<String>,
    data_granularity: Option<String>,
    instrument_type: Option<String>,
    current_trading_period: Option<CurrentPeriods>,
    /// A list of days, each a list of sessions; with pre/post data, an object with a `regular` such list.
    trading_periods: Option<serde_json::Value>,
}

#[derive(Deserialize, Default)]
#[serde(default)]
struct CurrentPeriods {
    pre: Option<Period>,
    regular: Option<Period>,
    post: Option<Period>,
}

#[derive(Deserialize, Clone, Copy, Default, Debug)]
#[serde(default)]
struct Period {
    start: i64,
    end: i64,
}

/// What to build. `unit` × `count` is the chart's interval: ("minute", 240) = 4H, ("month", 3) = 3M.
#[derive(Deserialize)]
pub struct Request {
    #[serde(default)]
    pub interval: String,
    pub unit: String,
    #[serde(default = "one")]
    pub count: u32,
    /// Bars sent to the page (the most recent); indicators are computed over all of them first.
    #[serde(default = "default_max_bars")]
    pub max_bars: usize,
    /// 0 = no gzip, else the deflate level (1-9).
    #[serde(default = "default_gzip")]
    pub gzip: u8,
}

fn one() -> u32 {
    1
}
fn default_max_bars() -> usize {
    3000
}
fn default_gzip() -> u8 {
    2
}

#[derive(Debug)]
pub enum BarsError {
    /// Yahoo answered with an error (unknown symbol, no data): worth remembering for a while.
    NoData(String),
    Failed(String),
}

impl std::fmt::Display for BarsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BarsError::NoData(m) | BarsError::Failed(m) => write!(f, "{m}"),
        }
    }
}

/// The page's JSON, its gzip (empty without), and a few facts for the cache (meta JSON).
pub struct Built {
    pub body: Vec<u8>,
    pub gz: Vec<u8>,
    pub meta: Vec<u8>,
}

/// Candles as columns.
#[derive(Default, Debug, Clone, PartialEq)]
pub struct Bars {
    pub t: Vec<i64>,
    pub o: Vec<f64>,
    pub h: Vec<f64>,
    pub l: Vec<f64>,
    pub c: Vec<f64>,
    pub v: Vec<f64>,
}

impl Bars {
    fn with_capacity(n: usize) -> Self {
        Bars {
            t: Vec::with_capacity(n),
            o: Vec::with_capacity(n),
            h: Vec::with_capacity(n),
            l: Vec::with_capacity(n),
            c: Vec::with_capacity(n),
            v: Vec::with_capacity(n),
        }
    }
    pub fn len(&self) -> usize {
        self.t.len()
    }
    pub fn is_empty(&self) -> bool {
        self.t.is_empty()
    }
}

const DAY: i64 = 86_400;
/// Daily and longer bars are stamped at the session open or at local midnight; shifting them 6 h later before
/// taking the date keeps a bar stamped with last winter's offset (an hour off) on its own day.
const DATE_NUDGE: i64 = 6 * 3600;

/// Days since 1970-01-01 → (year, month 1-12): Howard Hinnant's civil_from_days.
fn civil(days: i64) -> (i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    (y, m)
}

/// The regular sessions of `tradingPeriods`, sorted by start.
fn sessions(v: &Option<serde_json::Value>) -> Vec<Period> {
    let mut out = Vec::new();
    fn collect(v: &serde_json::Value, out: &mut Vec<Period>) {
        match v {
            serde_json::Value::Array(items) => items.iter().for_each(|x| collect(x, out)),
            serde_json::Value::Object(m) => {
                if let (Some(s), Some(e)) = (m.get("start").and_then(|x| x.as_i64()), m.get("end").and_then(|x| x.as_i64()))
                {
                    out.push(Period { start: s, end: e });
                } else if let Some(r) = m.get("regular") {
                    collect(r, out);
                }
            }
            _ => {}
        }
    }
    if let Some(v) = v {
        collect(v, &mut out);
    }
    out.retain(|p| p.end > p.start);
    out.sort_by_key(|p| p.start);
    out.dedup_by_key(|p| p.start);
    out
}

/// Bucket key of every bar: bars with equal consecutive keys become one candle. For intraday buckets the key is
/// also the candle's open time.
fn bucket_keys(t: &[i64], unit: &str, count: u32, off: i64, periods: &[Period]) -> Result<Vec<i64>, BarsError> {
    let count = count.max(1) as i64;
    let date = |x: i64| (x + off + DATE_NUDGE).div_euclid(DAY);
    Ok(match unit {
        "minute" => {
            let step = 60 * count;
            if periods.is_empty() {
                // no sessions (a 24 h market): buckets on the local clock
                t.iter().map(|&x| (x + off).div_euclid(step) * step - off).collect()
            } else {
                let mut k = Vec::with_capacity(t.len());
                for &x in t {
                    // the session this bar is in: the last one starting at or before it
                    let i = periods.partition_point(|p| p.start <= x);
                    if i == 0 {
                        k.push((x + off).div_euclid(step) * step - off);
                        continue;
                    }
                    let p = periods[i - 1];
                    if x >= p.end + 3600 {
                        k.push((x + off).div_euclid(step) * step - off); // outside every session (not in RTH data)
                        continue;
                    }
                    let y = if x >= p.end { p.end - 1 } else { x }; // the closing print: into the last bar
                    k.push(p.start + (y - p.start).div_euclid(step) * step);
                }
                k
            }
        }
        "day" => t.iter().map(|&x| date(x).div_euclid(count)).collect(),
        // weeks start on Monday (1970-01-01 was a Thursday)
        "week" => t.iter().map(|&x| (date(x) + 3).div_euclid(7 * count)).collect(),
        "month" => t
            .iter()
            .map(|&x| {
                let (y, m) = civil(date(x));
                (y * 12 + m - 1).div_euclid(count)
            })
            .collect(),
        other => return Err(BarsError::Failed(format!("unknown unit {other:?}"))),
    })
}

/// Merges consecutive bars with the same key: first open, highest high, lowest low, last close, summed volume.
/// Intraday candles are stamped with their bucket's start, longer ones with their first bar's time.
fn resample(src: &Bars, keys: &[i64], stamp_with_key: bool) -> Bars {
    let mut out = Bars::with_capacity(src.len());
    let mut last_key = None;
    for i in 0..src.len() {
        if last_key == Some(keys[i]) {
            let j = out.len() - 1;
            out.h[j] = out.h[j].max(src.h[i]);
            out.l[j] = out.l[j].min(src.l[i]);
            out.c[j] = src.c[i];
            out.v[j] += src.v[i];
        } else {
            out.t.push(if stamp_with_key { keys[i] } else { src.t[i] });
            out.o.push(src.o[i]);
            out.h.push(src.h[i]);
            out.l.push(src.l[i]);
            out.c.push(src.c[i]);
            out.v.push(src.v[i]);
            last_key = Some(keys[i]);
        }
    }
    out
}

/// Simple moving average (NaN until `n` values).
pub fn sma(x: &[f64], n: usize) -> Vec<f64> {
    let mut out = vec![f64::NAN; x.len()];
    if n == 0 || x.len() < n {
        return out;
    }
    let mut sum: f64 = x[..n].iter().sum();
    out[n - 1] = sum / n as f64;
    for i in n..x.len() {
        sum += x[i] - x[i - n];
        out[i] = sum / n as f64;
    }
    out
}

/// Exponential moving average with α = 2 / (n + 1), seeded with the SMA of the first `n` values from the first
/// finite one (Pine's `ta.ema`).
pub fn ema(x: &[f64], n: usize) -> Vec<f64> {
    let mut out = vec![f64::NAN; x.len()];
    let Some(first) = x.iter().position(|v| v.is_finite()) else { return out };
    if n == 0 || x.len() < first + n {
        return out;
    }
    let alpha = 2.0 / (n as f64 + 1.0);
    let seed = first + n - 1;
    let mut e = x[first..=seed].iter().sum::<f64>() / n as f64;
    out[seed] = e;
    for i in seed + 1..x.len() {
        e = alpha * x[i] + (1.0 - alpha) * e;
        out[i] = e;
    }
    out
}

/// Wilder's RSI over `n` changes (Pine's `ta.rsi`): averages seeded with the SMA of the first `n` changes.
pub fn rsi(c: &[f64], n: usize) -> Vec<f64> {
    let mut out = vec![f64::NAN; c.len()];
    if n == 0 || c.len() <= n {
        return out;
    }
    let (mut up, mut down) = (0.0, 0.0);
    for i in 1..=n {
        let d = c[i] - c[i - 1];
        up += d.max(0.0);
        down += (-d).max(0.0);
    }
    up /= n as f64;
    down /= n as f64;
    let value = |up: f64, down: f64| if down == 0.0 { if up == 0.0 { 50.0 } else { 100.0 } } else { 100.0 - 100.0 / (1.0 + up / down) };
    out[n] = value(up, down);
    for i in n + 1..c.len() {
        let d = c[i] - c[i - 1];
        up = (up * (n as f64 - 1.0) + d.max(0.0)) / n as f64;
        down = (down * (n as f64 - 1.0) + (-d).max(0.0)) / n as f64;
        out[i] = value(up, down);
    }
    out
}

/// MACD line (EMA fast − EMA slow), its signal (EMA of the line) and the histogram (line − signal).
pub fn macd(c: &[f64], fast: usize, slow: usize, signal: usize) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let (f, s) = (ema(c, fast), ema(c, slow));
    let line: Vec<f64> = f.iter().zip(&s).map(|(a, b)| a - b).collect();
    let sig = ema(&line, signal);
    let hist = line.iter().zip(&sig).map(|(a, b)| a - b).collect();
    (line, sig, hist)
}

/// Yahoo's response → its candles (rows with a missing price dropped, highs / lows widened to cover open and
/// close) and its meta.
fn parse(raw: &[u8]) -> Result<(Meta, Bars), BarsError> {
    let env: Envelope = serde_json::from_slice(raw).map_err(|e| BarsError::Failed(format!("bad Yahoo JSON: {e}")))?;
    let mut results = env.chart.result.unwrap_or_default();
    if results.is_empty() {
        let msg = env
            .chart
            .error
            .map(|e| format!("{}: {}", e.code.unwrap_or_default(), e.description.unwrap_or_default()))
            .unwrap_or_else(|| "no data".into());
        return Err(BarsError::NoData(msg));
    }
    let r = results.swap_remove(0);
    let q = r.indicators.and_then(|mut i| if i.quote.is_empty() { None } else { Some(i.quote.swap_remove(0)) });
    let q = q.unwrap_or_default();
    let n = r.timestamp.len();
    let mut bars = Bars::with_capacity(n);
    let at = |v: &Vec<Option<f64>>, i: usize| v.get(i).copied().flatten().filter(|x| x.is_finite());
    let meta = &r.meta;
    for i in 0..n {
        let (mut o, mut h, mut l, mut c) = (at(&q.open, i), at(&q.high, i), at(&q.low, i), at(&q.close, i));
        let mut v = at(&q.volume, i);
        // The bar of the session in progress (or just closed) can come without its close: Yahoo has it in the meta
        if i + 1 == n && c.is_none() && meta.regular_market_time.is_some_and(|mt| mt >= r.timestamp[i]) {
            if let Some(px) = meta.regular_market_price.filter(|x| x.is_finite()) {
                c = Some(px);
                h = h.or(meta.regular_market_day_high).or(c);
                l = l.or(meta.regular_market_day_low).or(c);
                o = o.or(c);
                v = v.or(meta.regular_market_volume);
            }
        }
        let (Some(o), Some(h), Some(l), Some(c)) = (o, h, l, c) else {
            continue;
        };
        if bars.t.last().is_some_and(|&prev| r.timestamp[i] < prev) {
            continue; // out of order: never seen, but a candle must not go back in time
        }
        bars.t.push(r.timestamp[i]);
        bars.o.push(o);
        bars.h.push(h.max(o).max(c));
        bars.l.push(l.min(o).min(c));
        bars.c.push(c);
        bars.v.push(v.unwrap_or(0.0).max(0.0));
    }
    Ok((r.meta, bars))
}

/// Candles of `req`'s interval from Yahoo's response, with its meta.
pub fn candles(raw: &[u8], req: &Request) -> Result<(Bars, MetaOut), BarsError> {
    let (meta, bars) = parse(raw)?;
    let off = meta.gmtoffset.unwrap_or(0);
    let periods = if req.unit == "minute" { sessions(&meta.trading_periods) } else { Vec::new() };
    let keys = bucket_keys(&bars.t, &req.unit, req.count, off, &periods)?;
    let out = resample(&bars, &keys, req.unit == "minute");
    let cur = meta.current_trading_period.unwrap_or_default();
    let m = MetaOut {
        symbol: meta.symbol,
        name: meta.long_name.or(meta.short_name),
        currency: meta.currency,
        exchange: meta.full_exchange_name.or(meta.exchange_name),
        tz: meta.exchange_timezone_name,
        gmtoffset: off,
        decimals: meta.price_hint.unwrap_or(2).min(8),
        price: meta.regular_market_price,
        prev_close: meta.previous_close,
        market_time: meta.regular_market_time,
        granularity: meta.data_granularity,
        instrument: meta.instrument_type,
        regular: cur.regular,
        pre: cur.pre,
        post: cur.post,
        bars_in: bars.len(),
    };
    Ok((out, m))
}

pub struct MetaOut {
    symbol: Option<String>,
    name: Option<String>,
    currency: Option<String>,
    exchange: Option<String>,
    tz: Option<String>,
    gmtoffset: i64,
    decimals: u32,
    price: Option<f64>,
    prev_close: Option<f64>,
    market_time: Option<i64>,
    granularity: Option<String>,
    instrument: Option<String>,
    regular: Option<Period>,
    pre: Option<Period>,
    post: Option<Period>,
    bars_in: usize,
}

// ── JSON writer: numbers rounded to a fixed number of decimals, written by hand (no float formatting) ─────────
struct Json {
    buf: Vec<u8>,
}

const POW10: [f64; 11] = [1.0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10];

impl Json {
    fn raw(&mut self, s: &str) {
        self.buf.extend_from_slice(s.as_bytes());
    }
    fn uint(&mut self, mut n: u64) {
        let mut tmp = [0u8; 20];
        let mut i = tmp.len();
        loop {
            i -= 1;
            tmp[i] = b'0' + (n % 10) as u8;
            n /= 10;
            if n == 0 {
                break;
            }
        }
        self.buf.extend_from_slice(&tmp[i..]);
    }
    fn int(&mut self, n: i64) {
        if n < 0 {
            self.buf.push(b'-');
        }
        self.uint(n.unsigned_abs());
    }
    /// `x` rounded to `d` decimals, trailing zeros dropped; null when not finite.
    fn num(&mut self, x: f64, d: u32) {
        let d = d.min(10);
        let scaled = (x * POW10[d as usize]).round();
        if !scaled.is_finite() || scaled.abs() >= 9.0e15 {
            self.raw("null");
            return;
        }
        let m = scaled as i64;
        if m == 0 {
            self.buf.push(b'0');
            return;
        }
        if m < 0 {
            self.buf.push(b'-');
        }
        let m = m.unsigned_abs();
        let p = 10u64.pow(d);
        self.uint(m / p);
        let mut frac = m % p;
        if frac != 0 {
            let mut digits = d;
            while frac % 10 == 0 {
                frac /= 10;
                digits -= 1;
            }
            self.buf.push(b'.');
            let s = frac.to_string();
            for _ in s.len()..digits as usize {
                self.buf.push(b'0');
            }
            self.raw(&s);
        }
    }
    fn opt_num(&mut self, x: Option<f64>, d: u32) {
        match x {
            Some(v) => self.num(v, d),
            None => self.raw("null"),
        }
    }
    fn opt_int(&mut self, x: Option<i64>) {
        match x {
            Some(v) => self.int(v),
            None => self.raw("null"),
        }
    }
    fn string(&mut self, s: Option<&str>) {
        match s {
            Some(s) => serde_json::to_writer(&mut self.buf, s).unwrap_or(()),
            None => self.raw("null"),
        }
    }
    fn key(&mut self, k: &str) {
        self.buf.push(b'"');
        self.raw(k);
        self.raw("\":");
    }
    fn array_f(&mut self, k: &str, xs: &[f64], d: u32) {
        self.key(k);
        self.buf.push(b'[');
        for (i, &x) in xs.iter().enumerate() {
            if i > 0 {
                self.buf.push(b',');
            }
            self.num(x, d);
        }
        self.raw("],");
    }
    fn period(&mut self, k: &str, p: Option<Period>) {
        self.key(k);
        match p {
            Some(p) => {
                self.raw("{\"start\":");
                self.int(p.start);
                self.raw(",\"end\":");
                self.int(p.end);
                self.raw("}");
            }
            None => self.raw("null"),
        }
        self.raw(",");
    }
}

/// `x` rounded to `digits` significant digits (volumes: 15926991 → 15900000).
pub fn significant(x: f64, digits: i32) -> f64 {
    if !x.is_finite() || x == 0.0 {
        return x;
    }
    let e = x.abs().log10().floor() as i32 - (digits - 1);
    if e <= 0 {
        return x.round();
    }
    let p = 10f64.powi(e);
    (x / p).round() * p
}

/// gzip (RFC 1952) of `data` at deflate `level`.
pub fn gzip(data: &[u8], level: u8) -> Vec<u8> {
    let deflated = miniz_oxide::deflate::compress_to_vec(data, level.clamp(1, 9));
    let mut out = Vec::with_capacity(deflated.len() + 18);
    out.extend_from_slice(&[0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff]);
    out.extend_from_slice(&deflated);
    out.extend_from_slice(&crc32fast::hash(data).to_le_bytes());
    out.extend_from_slice(&(data.len() as u32).to_le_bytes());
    out
}

/// The whole pipeline: Yahoo's response → the page's JSON (+ gzip) and the cache's meta.
pub fn build(raw: &[u8], req: &Request) -> Result<Built, BarsError> {
    let t0 = std::time::Instant::now();
    let (bars, m) = candles(raw, req)?;
    let close = &bars.c;
    let (sma50, sma100, sma200) = (sma(close, 50), sma(close, 100), sma(close, 200));
    let (ema8, ema21) = (ema(close, 8), ema(close, 21));
    let rsi14 = rsi(close, 14);
    let (line, sig, _) = macd(close, 12, 26, 9);
    let n = bars.len();
    let skip = n.saturating_sub(req.max_bars.max(1));
    let d = m.decimals;
    let mut j = Json { buf: Vec::with_capacity(64 + (n - skip) * 15 * 9) };
    j.raw("{\"v\":1,");
    j.key("sym");
    j.string(m.symbol.as_deref());
    j.raw(",");
    j.key("interval");
    j.string(Some(&req.interval));
    j.raw(",");
    j.key("name");
    j.string(m.name.as_deref());
    j.raw(",");
    j.key("currency");
    j.string(m.currency.as_deref());
    j.raw(",");
    j.key("exchange");
    j.string(m.exchange.as_deref());
    j.raw(",");
    j.key("tz");
    j.string(m.tz.as_deref());
    j.raw(",");
    j.key("gmtoffset");
    j.int(m.gmtoffset);
    j.raw(",");
    j.key("decimals");
    j.uint(d as u64);
    j.raw(",");
    j.key("intraday");
    j.raw(if req.unit == "minute" { "true," } else { "false," });
    j.key("price");
    j.opt_num(m.price, d);
    j.raw(",");
    j.key("prev_close");
    j.opt_num(m.prev_close, d);
    j.raw(",");
    j.key("market_time");
    j.opt_int(m.market_time);
    j.raw(",");
    j.period("regular", m.regular);
    j.key("n");
    j.uint((n - skip) as u64);
    j.raw(",");
    j.key("total");
    j.uint(n as u64);
    j.raw(",");
    // times as deltas (the first one absolute): a column of "3600" compresses to nothing, epoch seconds don't
    j.key("dt");
    j.buf.push(b'[');
    let mut prev = 0;
    for (i, &t) in bars.t[skip..].iter().enumerate() {
        if i > 0 {
            j.buf.push(b',');
        }
        j.int(t - prev);
        prev = t;
    }
    j.raw("],");
    // A line on a chart needs ~4 significant digits, so the averages get the price's decimals and the MACD one more
    // (the page draws the histogram as MACD - signal); volume is drawn as bars and read as "15.9M": 3 digits.
    j.array_f("o", &bars.o[skip..], d);
    j.array_f("h", &bars.h[skip..], d);
    j.array_f("l", &bars.l[skip..], d);
    j.array_f("c", &bars.c[skip..], d);
    let vol: Vec<f64> = bars.v[skip..].iter().map(|&v| significant(v, 3)).collect();
    j.array_f("vol", &vol, 0);
    j.array_f("sma50", &sma50[skip..], d);
    j.array_f("sma100", &sma100[skip..], d);
    j.array_f("sma200", &sma200[skip..], d);
    j.array_f("ema8", &ema8[skip..], d);
    j.array_f("ema21", &ema21[skip..], d);
    j.array_f("rsi", &rsi14[skip..], 1);
    j.array_f("macd", &line[skip..], d + 1);
    j.array_f("signal", &sig[skip..], d + 1);
    j.buf.pop(); // the last array's comma
    j.raw("}");
    let body = j.buf;
    let gz = if req.gzip > 0 { gzip(&body, req.gzip) } else { Vec::new() };

    let mut mj = Json { buf: Vec::with_capacity(512) };
    mj.raw("{");
    mj.key("bars");
    mj.uint((n - skip) as u64);
    mj.raw(",");
    mj.key("bars_in");
    mj.uint(m.bars_in as u64);
    mj.raw(",");
    mj.key("last_t");
    mj.opt_int(bars.t.last().copied());
    mj.raw(",");
    mj.key("market_time");
    mj.opt_int(m.market_time);
    mj.raw(",");
    mj.period("regular", m.regular);
    mj.period("pre", m.pre);
    mj.period("post", m.post);
    mj.key("granularity");
    mj.string(m.granularity.as_deref());
    mj.raw(",");
    mj.key("instrument");
    mj.string(m.instrument.as_deref());
    mj.raw(",");
    mj.key("body_bytes");
    mj.uint(body.len() as u64);
    mj.raw(",");
    mj.key("gz_bytes");
    mj.uint(gz.len() as u64);
    mj.raw(",");
    mj.key("build_us");
    mj.uint(t0.elapsed().as_micros() as u64);
    mj.raw("}");
    Ok(Built { body, gz, meta: mj.buf })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: f64, b: f64) -> bool {
        (a.is_nan() && b.is_nan()) || (a - b).abs() < 1e-9
    }

    #[test]
    fn civil_dates() {
        assert_eq!(civil(0), (1970, 1));
        assert_eq!(civil(20_454), (2026, 1)); // 2026-01-01
        assert_eq!(civil(20_453), (2025, 12));
        assert_eq!(civil(-1), (1969, 12));
    }

    #[test]
    fn indicators_match_definitions() {
        let x: Vec<f64> = (1..=30).map(|i| i as f64).collect();
        let s = sma(&x, 3);
        assert!(s[1].is_nan() && close(s[2], 2.0) && close(s[29], 29.0));
        let e = ema(&x, 3);
        assert!(e[1].is_nan() && close(e[2], 2.0) && close(e[3], 0.5 * 4.0 + 0.5 * 2.0));
        let r = rsi(&x, 14);
        assert!(r[13].is_nan() && close(r[14], 100.0));
        let flat = vec![5.0; 20];
        assert!(close(rsi(&flat, 14)[19], 50.0));
        let y: Vec<f64> = (0..40).map(|i| (i as f64 * 0.7).sin() * 10.0 + 100.0).collect();
        let (line, sig, hist) = macd(&y, 12, 26, 9);
        assert!(line[24].is_nan() && line[25].is_finite() && sig[32].is_nan() && sig[33].is_finite());
        assert!(close(hist[39], line[39] - sig[39]) && hist[32].is_nan());
    }

    #[test]
    fn numbers_are_rounded_by_hand() {
        let mut j = Json { buf: Vec::new() };
        for (x, d) in [(910.4199829101562, 2), (-0.5, 2), (0.004, 2), (1.0, 2), (1234.5678, 0), (0.0001234, 6)] {
            j.num(x, d);
            j.buf.push(b' ');
        }
        j.num(f64::NAN, 2);
        assert_eq!(String::from_utf8(j.buf).unwrap(), "910.42 -0.5 0 1 1235 0.000123 null");
    }
}
