//! The Portfolio X-ray's 5-year backtest (ui/backtest.py): weekly bars of the holdings and the benchmark ETFs, with
//! dividends reinvested (Yahoo's adjusted close) → the portfolio's buy-and-hold weekly value line, each
//! benchmark's, and the risk / return table (CAGR, volatility, Sharpe, Sortino, max drawdown, best / worst 12 months,
//! correlation and beta against the portfolio).
//!
//! The numbers must match ui/backtest.py's ``compute_python`` (ui/tests/test_backtest.py checks they do):
//!
//! * A week's value is its adjusted close (dividends and splits reinvested), kept when positive and finite.
//! * Bars are keyed by the Monday of their week (days since 1970-01-01); the last bar of a week wins (Yahoo's live
//!   bar of the current week). The backtest runs on the weeks every series has, from ``start_day``'s week on.
//! * ``start_value`` is invested at the first week's close in the given weights and held (no rebalancing): a
//!   holding's shares = weight × start_value / its first close; the portfolio's value is the sum of the holdings'.
//!   Each benchmark starts at ``start_value`` too.
//! * Weekly returns run close to close from the first week. ``rf`` (annual) becomes the weekly
//!   (1 + rf)^(1/52) − 1. Sharpe = mean excess return × 52 / (sample SD × √52); Sortino uses the downside
//!   deviation √(mean(min(0, excess)²)) × √52. Best / worst 12 months are rolling 52-week returns (the start value
//!   is the first week). Correlation and beta are of weekly returns, against the portfolio.
use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
pub struct Series {
    pub sym: String,
    pub t: Vec<i64>,
    pub adj: Vec<Option<f64>>,
}

#[derive(Deserialize)]
pub struct Request {
    pub holdings: Vec<Series>,
    pub weights: Vec<f64>,
    pub benchmarks: Vec<Series>,
    pub start_day: i64,
    #[serde(default)]
    pub rf: f64,
    #[serde(default = "default_start_value")]
    pub start_value: f64,
}

fn default_start_value() -> f64 {
    10_000.0
}

#[derive(Serialize, Default, Clone)]
pub struct Metrics {
    pub total: Option<f64>,
    pub cagr: Option<f64>,
    pub vol: Option<f64>,
    pub sharpe: Option<f64>,
    pub sortino: Option<f64>,
    pub max_dd: Option<f64>,
    pub best_12m: Option<f64>,
    pub worst_12m: Option<f64>,
    pub corr: Option<f64>,
    pub beta: Option<f64>,
}

#[derive(Serialize)]
pub struct Line {
    pub sym: String,
    pub c: Vec<f64>,
    pub metrics: Metrics,
}

#[derive(Serialize)]
pub struct Output {
    pub weeks: Vec<i64>,
    pub portfolio: Vec<f64>,
    pub metrics: Metrics,
    pub benchmarks: Vec<Line>,
    /// Each holding's first usable week (so the page can say which one shortened the window).
    pub first_weeks: Vec<i64>,
}

#[derive(Debug)]
pub enum BacktestError {
    NoData(String),
    Failed(String),
}

/// One series' adjusted closes by week (the Monday, in days since 1970-01-01).
struct Weekly {
    weeks: Vec<i64>,
    bars: Vec<f64>,
}

fn monday(t: i64) -> i64 {
    let day = t.div_euclid(86_400);
    day - (day + 3).rem_euclid(7) // 1970-01-01 was a Thursday
}

fn weekly(s: &Series) -> Result<Weekly, BacktestError> {
    if s.adj.len() != s.t.len() {
        return Err(BacktestError::Failed(format!("{}: series of different lengths", s.sym)));
    }
    let mut weeks: Vec<i64> = Vec::with_capacity(s.t.len());
    let mut bars: Vec<f64> = Vec::with_capacity(s.t.len());
    for (&t, a) in s.t.iter().zip(&s.adj) {
        let Some(a) = *a else { continue };
        if !(a > 0.0 && a.is_finite()) {
            continue;
        }
        let w = monday(t);
        match weeks.last() {
            Some(&last) if last == w => *bars.last_mut().unwrap() = a,
            Some(&last) if last > w => continue, // out of order: keep the series monotonic
            _ => {
                weeks.push(w);
                bars.push(a);
            }
        }
    }
    if weeks.is_empty() {
        return Err(BacktestError::NoData(format!("{}: no price history", s.sym)));
    }
    Ok(Weekly { weeks, bars })
}

fn mean(x: &[f64]) -> f64 {
    x.iter().sum::<f64>() / x.len() as f64
}

/// Sample covariance (n − 1).
fn cov(x: &[f64], y: &[f64]) -> f64 {
    let (mx, my) = (mean(x), mean(y));
    x.iter().zip(y).map(|(a, b)| (a - mx) * (b - my)).sum::<f64>() / (x.len() as f64 - 1.0)
}

fn fin(v: f64) -> Option<f64> {
    if v.is_finite() {
        Some(v)
    } else {
        None
    }
}

/// Week-over-week returns of a value series.
fn returns(values: &[f64]) -> Vec<f64> {
    values.windows(2).map(|w| w[1] / w[0] - 1.0).collect()
}

fn metrics(values: &[f64], years: f64, rf_w: f64) -> Metrics {
    let n = values.len();
    let r = returns(values);
    let (first, last) = (values[0], values[n - 1]);
    let total = last / first - 1.0;
    let cagr = if years > 0.0 { (last / first).powf(1.0 / years) - 1.0 } else { f64::NAN };
    let ex: Vec<f64> = r.iter().map(|x| x - rf_w).collect();
    let mean_ex = mean(&ex);
    let vol = if r.len() > 1 { cov(&r, &r).sqrt() * 52f64.sqrt() } else { f64::NAN };
    let sharpe = if vol > 0.0 { mean_ex * 52.0 / vol } else { f64::NAN };
    let down = (ex.iter().map(|x| x.min(0.0).powi(2)).sum::<f64>() / ex.len() as f64).sqrt() * 52f64.sqrt();
    let sortino = if down > 0.0 { mean_ex * 52.0 / down } else { f64::NAN };
    let mut peak = first;
    let mut max_dd = 0.0f64;
    for &v in values {
        peak = peak.max(v);
        max_dd = max_dd.min(v / peak - 1.0);
    }
    let (mut best, mut worst) = (f64::NAN, f64::NAN);
    if n > 52 {
        for i in 0..n - 52 {
            let x = values[i + 52] / values[i] - 1.0;
            if !(x <= best) {
                best = x;
            }
            if !(x >= worst) {
                worst = x;
            }
        }
    }
    Metrics {
        total: fin(total),
        cagr: fin(cagr),
        vol: fin(vol),
        sharpe: fin(sharpe),
        sortino: fin(sortino),
        max_dd: fin(max_dd),
        best_12m: fin(best),
        worst_12m: fin(worst),
        corr: None,
        beta: None,
    }
}

pub fn run(req: &Request) -> Result<Output, BacktestError> {
    if req.holdings.is_empty() || req.holdings.len() != req.weights.len() {
        return Err(BacktestError::Failed("one weight per holding".into()));
    }
    let wsum: f64 = req.weights.iter().sum();
    if !(wsum > 0.0) || req.weights.iter().any(|w| !(w.is_finite() && *w >= 0.0)) {
        return Err(BacktestError::Failed("weights must be positive".into()));
    }
    let start_value = if req.start_value > 0.0 { req.start_value } else { 10_000.0 };
    let holdings: Vec<Weekly> = req.holdings.iter().map(weekly).collect::<Result<_, _>>()?;
    let benches: Vec<Weekly> = req.benchmarks.iter().map(weekly).collect::<Result<_, _>>()?;
    let from = monday(req.start_day * 86_400);

    // the weeks every series has, from the start week on
    let mut weeks: Vec<i64> = holdings[0].weeks.iter().copied().filter(|&w| w >= from).collect();
    for s in holdings.iter().skip(1).chain(benches.iter()) {
        weeks.retain(|w| s.weeks.binary_search(w).is_ok());
    }
    if weeks.len() < 2 {
        return Err(BacktestError::NoData("not enough weeks of shared price history".into()));
    }
    let pick = |s: &Weekly| -> Vec<f64> { weeks.iter().map(|w| s.bars[s.weeks.binary_search(w).unwrap()]).collect() };
    let n = weeks.len();
    let mut pc = vec![0.0; n];
    for (s, &w) in holdings.iter().zip(&req.weights) {
        let bars = pick(s);
        let shares = w / wsum * start_value / bars[0];
        for (v, b) in pc.iter_mut().zip(&bars) {
            *v += shares * b;
        }
    }
    let years = (weeks[n - 1] - weeks[0]) as f64 / 365.25;
    let rf_w = (1.0 + req.rf).powf(1.0 / 52.0) - 1.0;
    let mut pm = metrics(&pc, years, rf_w);
    pm.corr = Some(1.0);
    pm.beta = Some(1.0);
    let pr = returns(&pc);
    let lines = req
        .benchmarks
        .iter()
        .zip(&benches)
        .map(|(src, s)| {
            let bars = pick(s);
            let k = start_value / bars[0];
            let c: Vec<f64> = bars.iter().map(|b| b * k).collect();
            let mut m = metrics(&c, years, rf_w);
            let br = returns(&c);
            let (vp, vb) = (cov(&pr, &pr), cov(&br, &br));
            let cpb = cov(&pr, &br);
            m.corr = fin(cpb / (vp * vb).sqrt());
            m.beta = fin(cpb / vb);
            Line { sym: src.sym.clone(), c, metrics: m }
        })
        .collect();
    Ok(Output {
        weeks,
        portfolio: pc,
        metrics: pm,
        benchmarks: lines,
        first_weeks: holdings.iter().map(|s| s.weeks[0]).collect(),
    })
}

/// The request JSON → the output JSON.
pub fn run_json(req: &[u8]) -> Result<Vec<u8>, BacktestError> {
    let req: Request = serde_json::from_slice(req).map_err(|e| BacktestError::Failed(format!("bad request: {e}")))?;
    let out = run(&req)?;
    serde_json::to_vec(&out).map_err(|e| BacktestError::Failed(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn series(sym: &str, closes: &[f64], div_factor: f64) -> Series {
        let t: Vec<i64> = (0..closes.len() as i64).map(|i| (4 + 7 * i) * 86_400 + 14 * 3600).collect(); // Mondays
        Series { sym: sym.into(), t, adj: closes.iter().map(|c| Some(c * div_factor)).collect() }
    }

    #[test]
    fn monday_keys() {
        assert_eq!(monday(4 * 86_400), 4); // 1970-01-05 was a Monday
        assert_eq!(monday(10 * 86_400 + 3600), 4); // Sunday the 11th → the same week
        assert_eq!(monday(11 * 86_400), 11);
    }

    #[test]
    fn single_holding_equals_its_benchmark() {
        let closes: Vec<f64> = (0..120).map(|i| 100.0 * (1.0 + 0.002 * i as f64) + (i % 5) as f64).collect();
        let req = Request {
            holdings: vec![series("AAA", &closes, 0.9)],
            weights: vec![1.0],
            benchmarks: vec![series("AAA", &closes, 0.9)],
            start_day: 0,
            rf: 0.03,
            start_value: 10_000.0,
        };
        let out = run(&req).unwrap();
        assert_eq!(out.weeks.len(), 120);
        assert!((out.portfolio[0] - 10_000.0).abs() < 1e-6);
        let b = &out.benchmarks[0];
        for (a, c) in out.portfolio.iter().zip(&b.c) {
            assert!((a - c).abs() < 1e-6);
        }
        assert!((b.metrics.corr.unwrap() - 1.0).abs() < 1e-9 && (b.metrics.beta.unwrap() - 1.0).abs() < 1e-9);
        assert_eq!(b.metrics.sharpe, out.metrics.sharpe);
        assert!(out.metrics.max_dd.unwrap() <= 0.0 && out.metrics.best_12m.unwrap() >= out.metrics.worst_12m.unwrap());
    }

    #[test]
    fn rejects_bad_requests() {
        let s = series("A", &[1.0, 2.0], 1.0);
        let req = Request { holdings: vec![s], weights: vec![], benchmarks: vec![], start_day: 0, rf: 0.0, start_value: 1.0 };
        assert!(matches!(run(&req), Err(BacktestError::Failed(_))));
        let req = Request {
            holdings: vec![series("A", &[1.0, 2.0, 3.0], 1.0)],
            weights: vec![1.0],
            benchmarks: vec![],
            start_day: 100_000,
            rf: 0.0,
            start_value: 1.0,
        };
        assert!(matches!(run(&req), Err(BacktestError::NoData(_))));
    }
}
