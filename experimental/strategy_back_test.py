"""Strategy Backtest — event-driven simulation of the rules the simulator
actually executes (entry, stop, target, breakeven, time stop, slippage).

WHY THIS EXISTS
---------------
`back_test.py` measures *bias accuracy*: did the close land on the right
side of the entry 5 days later?  That number has nothing to do with the
trade the simulator takes, which carries a stop ~0.4 ATR away and a target
~1.3 ATR away.  A stock can finish the week higher (bias "correct") and
still knock out a 0.4 ATR stop on day one — under a zero-drift random walk
that stop is touched ~82% of the time within 5 days, which is exactly the
live stop-out rate observed in tmp/simulator.json.

This module replays the *executed* rules on daily bars so exit variants can
be compared honestly:

  * entry at next session's open (+ slippage)         — no look-ahead
  * stop / target checked on bar Low/High, stop wins ties (conservative)
  * gap through a level fills at the open, not the level
  * breakeven arm at +1R (optional), time stop at N bars
  * market-regime gate, earnings-free (no lxml) and cooldown filters

Stage 1 builds a signal table (one row per ticker-day) using the same
scoring engine as the simulator (`_run_single_scan`) and caches it.
Stage 2 simulates any number of exit/filter variants on that table and
reports expectancy in R with a block-bootstrap confidence interval, so
"it made money" can be separated from "it made money by luck".

Usage:
    python -m experimental.strategy_backtest --sample 60 --days 250
    python -m experimental.strategy_backtest --tickers MU,NVDA,AAPL --days 250
    python -m experimental.strategy_backtest --sample 60 --days 250 --refresh
"""

import argparse
import os
import pickle
import random
import warnings
from datetime import timedelta

import numpy as np
import pandas as pd
import yfinance as yf

from experimental.back_test import _run_single_scan, _historical_vol as _bt_vol
from experimental.quant_engine import expected_move, find_levels

warnings.filterwarnings("ignore")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_BASE, "tmp")
SIGNALS_CACHE = os.path.join(CACHE_DIR, "strategy_signals.pkl")
PRICES_CACHE = os.path.join(CACHE_DIR, "strategy_prices.pkl")

SLIPPAGE_BPS = 5.0


# ─── Data ─────────────────────────────────────────────────────────────────────

def _load_universe():
    from experimental.simulator import _load_tickers
    return _load_tickers()


def download_prices(symbols, period="3y", refresh=False):
    """Batch-download daily OHLCV for many symbols; cache to disk."""
    cache = {}
    if os.path.exists(PRICES_CACHE) and not refresh:
        with open(PRICES_CACHE, "rb") as f:
            cache = pickle.load(f)
    missing = [s for s in symbols if s not in cache]
    if missing:
        raw = yf.download(missing, period=period, interval="1d", progress=False,
                          group_by="ticker", threads=True, auto_adjust=True)
        for s in missing:
            try:
                df = raw[s].dropna(subset=["Close"]) if len(missing) > 1 else raw.dropna(subset=["Close"])
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                if len(df) > 100:
                    cache[s] = df
            except Exception:
                continue
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(PRICES_CACHE, "wb") as f:
            pickle.dump(cache, f)
    return {s: cache[s] for s in symbols if s in cache}


# ─── Indicators ───────────────────────────────────────────────────────────────

def atr(df, n=14):
    """Wilder-style ATR (simple mean of true range) as a Series."""
    prev_close = df["Close"].shift()
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def legacy_stop_target(bias, price, levels, em_1d):
    """Replicate trade_assistant._synthesize stop/target geometry.

    Stop  = nearest support - 0.3 * 1-day EM (bull) / nearest resistance + 0.3 EM (bear)
    Target = nearest resistance (bull) / nearest support (bear), else 1-day 1σ.
    """
    move = em_1d["move_dollars"]
    sup = [s for s in (levels["support"] if levels else []) if s < price]
    res = [r for r in (levels["resistance"] if levels else []) if r > price]
    if bias == "BULLISH":
        target = res[0] if res else em_1d["1sigma_upper"]
        stop = round(sup[-1] - move * 0.3, 2) if sup else em_1d["1sigma_lower"]
        if target <= price:
            target = round(price + move, 2)
        if stop >= price:
            stop = round(price - move, 2)
    else:
        target = sup[-1] if sup else em_1d["1sigma_lower"]
        stop = round(res[0] + move * 0.3, 2) if res else em_1d["1sigma_upper"]
        if target >= price:
            target = round(price - move, 2)
        if stop <= price:
            stop = round(price + move, 2)
    return stop, target


# ─── Stage 1: signal table ────────────────────────────────────────────────────

def build_signals(prices, index_map, days=250, min_score=3, max_score=5, log=print, symbols=None):
    """One row per ticker-day where the scoring engine emitted a directional
    bias.  Everything here is computable at the close of `date` — the trade
    is entered at the *next* open by the simulator stage.

    `prices` must contain the index series too; `symbols` restricts which
    keys are scanned (default: every key that has an index_map entry)."""
    rows = []
    symbols = symbols or [s for s in prices if s in index_map]
    for n, sym in enumerate(symbols):
        df = prices.get(sym)
        idx_sym = index_map.get(sym, "QQQ")
        idx_df = prices.get(idx_sym)
        if df is None or idx_df is None:
            continue
        a = atr(df)
        idx_sma50 = idx_df["Close"].rolling(50).mean()
        idx_sma20 = idx_df["Close"].rolling(20).mean()
        start = max(260, len(df) - days - 1)
        for i in range(start, len(df) - 1):
            try:
                scan = _run_single_scan(df, idx_df, i)
            except Exception:
                continue
            if not scan or scan["bias"] not in ("BULLISH", "BEARISH"):
                continue
            score = abs(scan["score"])
            if score < min_score or score > max_score:
                continue
            hist = df.iloc[: i + 1]
            price = float(hist["Close"].iloc[-1])
            # trade_assistant uses 20-day realized vol & 1-day EM for geometry
            rets = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
            vol20 = float(rets.tail(20).std() * np.sqrt(252)) if len(rets) >= 20 else 0.3
            em_1d = expected_move(price, vol20, days=1)
            levels = find_levels(hist.tail(120))
            stop_l, tgt_l = legacy_stop_target(scan["bias"], price, levels, em_1d)
            date = df.index[i]
            j = idx_df.index.get_indexer([date], method="ffill")[0]
            idx_close = float(idx_df["Close"].iloc[j])
            rows.append({
                "symbol": sym, "date": date, "i": i,
                "bias": scan["bias"], "score": score, "regime": scan["regime"],
                "price": price, "atr": float(a.iloc[i]) if not np.isnan(a.iloc[i]) else np.nan,
                "vol20": vol20,
                "stop_legacy": stop_l, "target_legacy": tgt_l,
                "sup": (levels["support"][-1] if levels and levels["support"] else np.nan),
                "res": (levels["resistance"][0] if levels and levels["resistance"] else np.nan),
                "idx_above_sma50": idx_close > float(idx_sma50.iloc[j]),
                "idx_above_sma20": idx_close > float(idx_sma20.iloc[j]),
            })
        if (n + 1) % 10 == 0:
            log(f"  signals: {n + 1}/{len(symbols)} tickers, {len(rows)} rows")
    sig = pd.DataFrame(rows)
    if sig.empty:
        return sig
    # Bias correctness 5 bars ahead — this is what back_test.py calls "edge".
    # Computed here so we can test whether it predicts executed-trade outcomes.
    fwd = []
    for r in sig.itertuples():
        df = prices[r.symbol]
        k = min(r.i + 5, len(df) - 1)
        end = float(df["Close"].iloc[k])
        fwd.append((end > r.price) if r.bias == "BULLISH" else (end < r.price))
    sig["bias_correct_5d"] = fwd
    # Rolling per-ticker/direction "edge" using ONLY prior signals whose 5-day
    # outcome was already known at signal time (no leakage).
    sig = sig.sort_values(["symbol", "date"]).reset_index(drop=True)
    edge = np.full(len(sig), np.nan)
    for (sym, bias), g in sig.groupby(["symbol", "bias"]):
        idxs = g.index.to_list()
        ii = g["i"].to_numpy()
        bc = g["bias_correct_5d"].to_numpy().astype(float)
        for pos, gi in enumerate(idxs):
            known = ii < ii[pos] - 5
            if known.sum() >= 10:
                edge[gi] = bc[known].mean() * 100
    sig["edge_prior"] = edge
    return sig


# ─── Stage 2: trade simulation ────────────────────────────────────────────────

def _slip(price, is_buy):
    s = SLIPPAGE_BPS / 10000.0
    return price * (1 + s) if is_buy else price * (1 - s)


def simulate_trade(df, i, bias, stop, target, max_hold=5, breakeven=True,
                   trail_atr=None, atr_series=None):
    """Replay one trade from the open of bar i+1.

    Returns dict(r, pnl_pct, reason, bars) or None if entry geometry breaks
    at the fill (same check the simulator makes at fill time).
    Conservative fills: gap through a level fills at the open; when High and
    Low straddle both stop and target in one bar, the stop is assumed first.
    """
    if i + 1 >= len(df):
        return None
    long = bias == "BULLISH"
    entry = _slip(float(df["Open"].iloc[i + 1]), is_buy=long)
    risk = (entry - stop) if long else (stop - entry)
    reward = (target - entry) if long else (entry - target)
    if risk <= 0 or reward <= 0:
        return None
    be_armed = False
    last = min(i + max_hold, len(df) - 1)
    for k in range(i + 1, last + 1):
        o, h, l, c = (float(df[col].iloc[k]) for col in ("Open", "High", "Low", "Close"))
        # gap through stop at the open (only on bars after the entry bar's open)
        if k > i + 1:
            if long and o <= stop:
                return _close(entry, _slip(o, False), risk, "STOP", k - i, long)
            if not long and o >= stop:
                return _close(entry, _slip(o, True), risk, "STOP", k - i, long)
            if long and o >= target:
                return _close(entry, _slip(o, False), risk, "TARGET", k - i, long)
            if not long and o <= target:
                return _close(entry, _slip(o, True), risk, "TARGET", k - i, long)
        hit_stop = l <= stop if long else h >= stop
        hit_tgt = h >= target if long else l <= target
        if hit_stop:
            return _close(entry, _slip(stop, not long), risk, "STOP", k - i, long)
        if hit_tgt:
            return _close(entry, _slip(target, not long), risk, "TARGET", k - i, long)
        # breakeven arm at +1R (applies from the next bar)
        if breakeven and not be_armed:
            if (long and h >= entry + risk) or (not long and l <= entry - risk):
                stop = entry
                be_armed = True
        # chandelier trail
        if trail_atr and atr_series is not None:
            a = float(atr_series.iloc[k])
            if long:
                stop = max(stop, c - trail_atr * a)
            else:
                stop = min(stop, c + trail_atr * a)
    c = float(df["Close"].iloc[last])
    return _close(entry, _slip(c, not long), risk, "TIME_STOP", last - i, long)


def _close(entry, exit_px, risk, reason, bars, long):
    pnl = (exit_px - entry) if long else (entry - exit_px)
    return {"r": pnl / risk, "pnl_pct": pnl / entry * 100, "reason": reason, "bars": bars}


def run_variant(sig, prices, name, stop_mode="legacy", stop_atr=2.0, target_mode="legacy",
                target_r=2.0, min_rr=2.0, max_hold=5, breakeven=True, regime_gate=None,
                cooldown_days=0, trail_atr=None, min_edge=None, max_per_day=None,
                one_per_symbol=True):
    """Simulate one configuration over the signal table.

    stop_mode:   'legacy' (support − 0.3 EM) | 'atr' (entry ∓ stop_atr × ATR)
                 | 'atr_floor' (legacy, but never tighter than stop_atr × ATR)
    target_mode: 'legacy' (nearest level) | 'r' (target_r × risk) | 'none' (trail/time only)
    regime_gate: None | 'sma50' | 'sma20' — longs only when index above, shorts only below
    cooldown_days: skip a symbol for N calendar days after a stop-out
    min_edge:    require rolling prior bias accuracy ≥ this (mimics MIN_EDGE gate)
    max_per_day: cap number of entries per calendar day (portfolio realism)
    one_per_symbol: ignore new signals for a symbol while a trade in it is open
    """
    trades = []
    last_stop = {}
    per_day = {}
    open_until = {}   # symbol -> bar index at which the current trade exits
    for r in sig.sort_values("date").itertuples():
        if np.isnan(r.atr):
            continue
        if one_per_symbol and r.i < open_until.get(r.symbol, -1):
            continue
        if regime_gate:
            above = r.idx_above_sma50 if regime_gate == "sma50" else r.idx_above_sma20
            if (r.bias == "BULLISH") != bool(above):
                continue
        if min_edge is not None and (np.isnan(r.edge_prior) or r.edge_prior < min_edge):
            continue
        if cooldown_days and r.symbol in last_stop and (r.date - last_stop[r.symbol]).days < cooldown_days:
            continue
        if max_per_day and per_day.get(r.date, 0) >= max_per_day:
            continue
        df = prices[r.symbol]
        long = r.bias == "BULLISH"
        entry_est = r.price
        if stop_mode == "legacy":
            stop = r.stop_legacy
        elif stop_mode == "atr":
            stop = entry_est - stop_atr * r.atr if long else entry_est + stop_atr * r.atr
        else:  # atr_floor
            floor = entry_est - stop_atr * r.atr if long else entry_est + stop_atr * r.atr
            stop = min(r.stop_legacy, floor) if long else max(r.stop_legacy, floor)
        risk = abs(entry_est - stop)
        if target_mode == "legacy":
            target = r.target_legacy
        elif target_mode == "r":
            target = entry_est + target_r * risk if long else entry_est - target_r * risk
        else:
            target = entry_est + 1e9 if long else -1e9
        rr = abs(target - entry_est) / risk if risk > 0 else 0
        if target_mode != "none" and rr < min_rr:
            continue
        res = simulate_trade(df, r.i, r.bias, stop, target, max_hold=max_hold,
                             breakeven=breakeven, trail_atr=trail_atr, atr_series=atr(df) if trail_atr else None)
        if res is None:
            continue
        res.update({"symbol": r.symbol, "date": r.date, "bias": r.bias, "score": r.score,
                    "edge_prior": r.edge_prior, "rr_planned": rr})
        trades.append(res)
        open_until[r.symbol] = r.i + res["bars"]
        per_day[r.date] = per_day.get(r.date, 0) + 1
        if res["reason"] == "STOP":
            last_stop[r.symbol] = r.date
    return name, pd.DataFrame(trades)


# ─── Statistics ───────────────────────────────────────────────────────────────

def block_bootstrap_ci(series_by_date, n_boot=2000, block=5, seed=7):
    """95% CI for the mean of a date-ordered series using block bootstrap
    (adjacent trades share market days → not independent)."""
    x = np.asarray(series_by_date, dtype=float)
    if len(x) < block * 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(len(x) / block))
    starts = np.arange(0, len(x) - block + 1)
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.choice(starts, size=n_blocks, replace=True)
        sample = np.concatenate([x[s:s + block] for s in pick])[: len(x)]
        means[b] = sample.mean()
    return (np.percentile(means, 2.5), np.percentile(means, 97.5))


def summarize(name, tr):
    if tr.empty:
        return {"variant": name, "n": 0}
    tr = tr.sort_values("date")
    r = tr["r"].to_numpy()
    wins = r > 0
    gross_win = r[wins].sum() if wins.any() else 0.0
    gross_loss = -r[~wins].sum() if (~wins).any() else 0.0
    eq = np.cumsum(r)
    dd = (np.maximum.accumulate(eq) - eq).max() if len(eq) else 0.0
    lo, hi = block_bootstrap_ci(r)
    return {
        "variant": name, "n": len(r),
        "win%": round(wins.mean() * 100, 1),
        "avgR": round(r.mean(), 3),
        "R_ci95": f"[{lo:+.3f}, {hi:+.3f}]",
        "PF": round(gross_win / gross_loss, 2) if gross_loss > 0 else np.inf,
        "totalR": round(r.sum(), 1),
        "maxDD_R": round(dd, 1),
        "avg_pnl%": round(tr["pnl_pct"].mean(), 2),
        "stop%": round((tr["reason"] == "STOP").mean() * 100, 0),
        "tgt%": round((tr["reason"] == "TARGET").mean() * 100, 0),
        "avg_bars": round(tr["bars"].mean(), 1),
    }


def edge_predictiveness(tr):
    """Does back_test-style 'edge' predict executed-trade R?  Bucket trades by
    the rolling prior accuracy and report mean R per bucket."""
    t = tr.dropna(subset=["edge_prior"]).copy()
    if t.empty:
        return pd.DataFrame()
    t["edge_bin"] = pd.cut(t["edge_prior"], [0, 45, 50, 55, 60, 65, 100])
    out = t.groupby("edge_bin", observed=True)["r"].agg(["count", "mean"]).round(3)
    out["corr(edge,R)"] = round(np.corrcoef(t["edge_prior"], t["r"])[0, 1], 3)
    return out


def random_entry_control(sig, prices, variant_kwargs, n_runs=20, seed=1):
    """Null model: keep every date/symbol/direction-count of the signal table
    but assign the *direction at random* (50/50), then run the same exits.

    If a variant's avg R is not clearly above the random-direction
    distribution, the profit comes from the exit structure + market drift,
    not from the scoring engine.  Returns (mean, 5th pct, 95th pct) of avg R
    across runs."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n_runs):
        shuffled = sig.copy()
        flip = rng.random(len(shuffled)) < 0.5
        shuffled["bias"] = np.where(flip, "BULLISH", "BEARISH")
        # legacy geometry is direction-dependent; recompute cheaply from levels
        long = shuffled["bias"] == "BULLISH"
        em = shuffled["price"] * shuffled["vol20"] / np.sqrt(252)
        sup = shuffled["sup"].fillna(shuffled["price"] - em)
        res = shuffled["res"].fillna(shuffled["price"] + em)
        shuffled["stop_legacy"] = np.where(long, sup - 0.3 * em, res + 0.3 * em)
        shuffled["target_legacy"] = np.where(long, res, sup)
        _, tr = run_variant(shuffled, prices, "ctrl", **variant_kwargs)
        if not tr.empty:
            out.append(tr["r"].mean())
    if not out:
        return (np.nan, np.nan, np.nan)
    return (float(np.mean(out)), float(np.percentile(out, 5)), float(np.percentile(out, 95)))


def direction_split(tr):
    if tr.empty:
        return ""
    g = tr.groupby("bias")["r"].agg(["count", "mean"]).round(3)
    return " | ".join(f"{b[:4]} n={int(c)} avgR={m:+.3f}" for b, (c, m) in g.iterrows())


DEFAULT_VARIANTS = [
    # name, kwargs
    ("A live rules (legacy stop, RR>=2, BE, 5d)", dict()),
    ("B A + regime gate (idx>SMA50 for longs)", dict(regime_gate="sma50")),
    ("C A + edge>=55 gate (as live)", dict(min_edge=55)),
    ("D ATR stop 2.0, target=level, RR>=1, BE", dict(stop_mode="atr", stop_atr=2.0, min_rr=1.0)),
    ("E ATR stop 2.0, target 2R, no BE, 10d", dict(stop_mode="atr", stop_atr=2.0, target_mode="r", target_r=2.0,
                                                 min_rr=0, breakeven=False, max_hold=10)),
    ("F E + regime gate sma50", dict(stop_mode="atr", stop_atr=2.0, target_mode="r", target_r=2.0,
                                    min_rr=0, breakeven=False, max_hold=10, regime_gate="sma50")),
    ("G F + cooldown 10d", dict(stop_mode="atr", stop_atr=2.0, target_mode="r", target_r=2.0,
                               min_rr=0, breakeven=False, max_hold=10, regime_gate="sma50", cooldown_days=10)),
    ("H ATR 2.0 chandelier trail 3ATR, 20d, gate", dict(stop_mode="atr", stop_atr=2.0, target_mode="none",
                                                        breakeven=False, max_hold=20, regime_gate="sma50",
                                                        trail_atr=3.0)),
    ("I ATR 1.5 stop, 1.5R target, gate, 10d", dict(stop_mode="atr", stop_atr=1.5, target_mode="r", target_r=1.5,
                                                    min_rr=0, breakeven=False, max_hold=10, regime_gate="sma50")),
]


def main():
    p = argparse.ArgumentParser(description="Event-driven backtest of the simulator's executed rules")
    p.add_argument("--tickers", default=None, help="Comma-separated symbols (default: random sample of universe)")
    p.add_argument("--sample", type=int, default=60, help="Random sample size from universe")
    p.add_argument("--days", type=int, default=250, help="Trading days of signals per ticker")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--refresh", action="store_true", help="Ignore caches")
    p.add_argument("--control-runs", type=int, default=20, help="Random-direction control runs per variant")
    args = p.parse_args()

    universe = _load_universe()
    index_map = {s: idx for s, idx in universe}
    if args.tickers:
        symbols = [s.strip().upper() for s in args.tickers.split(",")]
    else:
        random.seed(args.seed)
        symbols = random.sample([s for s, _ in universe], min(args.sample, len(universe)))
    indices = sorted(set(index_map.get(s, "QQQ") for s in symbols) | {"SPY"})

    print(f"Downloading {len(symbols)} tickers + {indices}...")
    prices = download_prices(symbols + indices, refresh=args.refresh)
    print(f"  got {len(prices)} series")

    key = (tuple(sorted(symbols)), args.days)
    sig = None
    if os.path.exists(SIGNALS_CACHE) and not args.refresh:
        with open(SIGNALS_CACHE, "rb") as f:
            cached = pickle.load(f)
        if cached.get("key") == key:
            sig = cached["sig"]
            print(f"  loaded {len(sig)} cached signals")
    if sig is None:
        print("Building signal table (same scoring engine as the simulator)...")
        sig = build_signals(prices, index_map, days=args.days, symbols=[s for s in symbols if s in prices])
        with open(SIGNALS_CACHE, "wb") as f:
            pickle.dump({"key": key, "sig": sig}, f)
    if sig.empty:
        print("No signals.")
        return
    print(f"\n{len(sig)} directional signals (score 3-5) across {sig.symbol.nunique()} tickers, "
          f"{sig.date.min().date()} → {sig.date.max().date()}")
    print(f"  bias accuracy (back_test.py's 'edge' metric): {sig.bias_correct_5d.mean() * 100:.1f}%")
    print(f"  median legacy stop distance: {(abs(sig.price - sig.stop_legacy) / sig.atr).median():.2f} ATR | "
          f"median legacy target: {(abs(sig.target_legacy - sig.price) / sig.atr).median():.2f} ATR")

    rows = []
    results = {}
    for name, kw in DEFAULT_VARIANTS:
        n, tr = run_variant(sig, prices, name, **kw)
        results[n] = tr
        rows.append(summarize(n, tr))
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print("\n" + out.to_string(index=False))

    print("\nDoes the 'edge' gate predict executed-trade outcome? (variant A trades)")
    print(edge_predictiveness(results[DEFAULT_VARIANTS[0][0]]).to_string())

    print("\nLong vs short split per variant:")
    for name, _ in DEFAULT_VARIANTS:
        print(f"  {name[:44]:<44} {direction_split(results[name])}")

    print("\nRandom-direction control (same dates/symbols/exits, coin-flip direction, 20 runs):")
    print(f"  {'variant':<44} {'signal avgR':>12} {'random mean':>12} {'random 5-95%':>20}  verdict")
    for name, kw in DEFAULT_VARIANTS:
        tr = results[name]
        if tr.empty or len(tr) < 50:
            continue
        m, lo, hi = random_entry_control(sig, prices, kw, n_runs=args.control_runs)
        real = tr["r"].mean()
        verdict = "SIGNAL ADDS VALUE" if real > hi else ("no better than coin flip" if real >= lo else "WORSE than coin flip")
        print(f"  {name[:44]:<44} {real:>+12.3f} {m:>+12.3f} {f'[{lo:+.3f}, {hi:+.3f}]':>20}  {verdict}")

    best = max(rows, key=lambda x: x.get("avgR", -9) if x["n"] > 30 else -9)
    print(f"\nBest by avg R (n>30): {best['variant']}  avgR={best['avgR']} CI={best['R_ci95']}")
    print("A variant is only 'statistically winning' when the CI lower bound is > 0.")


if __name__ == "__main__":
    main()
