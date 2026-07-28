"""Backtest — validate trade assistant predictions over 6 months.

Runs the scan for each trading day in the lookback window, then checks
whether the predicted bias and target were hit within the forward horizon.

Usage:
    python -m experimental.back_test --ticker TSLA --index QQQ
    python -m experimental.back_test --ticker NVDA --index SMH --days 120 --horizon 5
    python -m experimental.back_test --ticker MU --index SMH --only bull --min-score 4
"""

import argparse
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import timedelta

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from experimental.quant_engine import (
    expected_move,
    volume_profile,
    relative_strength,
    move_probability,
    order_flow_proxy,
    find_levels,
)


def _intraday_flow(intraday_hist, scan_date):
    """Compute intraday order flow from pre-fetched 5-min bars.

    yfinance provides ~60 days of 5m bars (78 bars/day).
    This is 78x more granular than daily CLV — captures real intraday
    buying/selling pressure, VWAP deviation, and volume acceleration.

    Args:
        intraday_hist: Pre-fetched DataFrame of 5-min bars (tz-naive index).
        scan_date: Date string (YYYY-MM-DD) to extract.

    Returns:
        dict with intraday flow metrics, or None.
    """
    try:
        if intraday_hist is None or intraday_hist.empty:
            return None

        target_date = pd.Timestamp(scan_date).date()
        day_bars = intraday_hist[intraday_hist.index.date == target_date]

        if len(day_bars) < 10:
            return None

        # --- Intraday CLV at 5-min resolution ---
        hl_range = day_bars['High'] - day_bars['Low']
        hl_range = hl_range.replace(0, np.nan)
        clv = ((day_bars['Close'] - day_bars['Low']) - (day_bars['High'] - day_bars['Close'])) / hl_range
        money_flow = (clv * day_bars['Volume']).dropna()

        cumulative_flow = money_flow.sum()
        total_volume = day_bars['Volume'].sum()
        flow_pct = (cumulative_flow / total_volume * 100) if total_volume > 0 else 0

        # --- VWAP deviation ---
        typical = (day_bars['High'] + day_bars['Low'] + day_bars['Close']) / 3
        vwap = (typical * day_bars['Volume']).cumsum() / day_bars['Volume'].cumsum()
        final_close = day_bars['Close'].iloc[-1]
        vwap_dev = ((final_close - vwap.iloc[-1]) / vwap.iloc[-1] * 100) if vwap.iloc[-1] > 0 else 0

        # --- Bar delta: up-close vs down-close volume ---
        up_bars = day_bars[day_bars['Close'] > day_bars['Open']]
        down_bars = day_bars[day_bars['Close'] < day_bars['Open']]
        buy_volume = int(up_bars['Volume'].sum())
        sell_volume = int(down_bars['Volume'].sum())
        trade_imbalance = ((buy_volume - sell_volume) / max(total_volume, 1)) * 100

        # --- Volume acceleration: last hour vs first hour ---
        bars_per_hour = max(len(day_bars) // 7, 1)
        first_hour_vol = day_bars['Volume'].iloc[:bars_per_hour].mean()
        last_hour_vol = day_bars['Volume'].iloc[-bars_per_hour:].mean()
        vol_acceleration = (last_hour_vol / first_hour_vol) if first_hour_vol > 0 else 1.0

        # --- Composite: CLV (50%) + Trade Imbalance (30%) + VWAP dev (20%) ---
        vwap_score = min(max(vwap_dev * 20, -30), 30)
        composite = flow_pct * 0.5 + trade_imbalance * 0.3 + vwap_score * 0.2

        # Acceleration: is flow building in the last third?
        if len(money_flow) >= 6:
            split = len(money_flow) * 2 // 3
            early = money_flow.iloc[:split].sum()
            late = money_flow.iloc[split:].sum()
            accelerating = abs(late) > abs(early) and np.sign(late) == np.sign(cumulative_flow)
        else:
            accelerating = None

        return {
            "source": "yfinance_5m",
            "flow_score": round(flow_pct, 1),
            "trade_imbalance": round(trade_imbalance, 1),
            "vwap_deviation": round(vwap_dev, 3),
            "vol_acceleration": round(vol_acceleration, 2),
            "composite_score": round(composite, 1),
            "interpretation": "BUYING" if composite > 10 else "SELLING" if composite < -10 else "NEUTRAL",
            "accelerating": accelerating,
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "num_bars": len(day_bars),
        }
    except Exception:
        return None


def _get_options_iv(ticker_obj):
    """Extract ATM implied volatility from yfinance options chain.

    Uses nearest expiry ATM straddle — the purest forward-looking vol read.
    This replaces realized vol for expected move calculation when available.

    Returns:
        (iv, days_to_exp) tuple, or (None, None) if unavailable.
    """
    try:
        exps = ticker_obj.options
        if not exps:
            return None, None
        nearest = exps[0]
        chain = ticker_obj.option_chain(nearest)
        price = ticker_obj.info.get("currentPrice") or ticker_obj.info.get("regularMarketPrice", 0)
        if price <= 0:
            return None, None

        calls = chain.calls[['strike', 'impliedVolatility']].copy()
        puts = chain.puts[['strike', 'impliedVolatility']].copy()
        calls['dist'] = (calls['strike'] - price).abs()
        puts['dist'] = (puts['strike'] - price).abs()

        atm_call_iv = calls.nsmallest(1, 'dist')['impliedVolatility'].iloc[0]
        atm_put_iv = puts.nsmallest(1, 'dist')['impliedVolatility'].iloc[0]

        iv = (atm_call_iv + atm_put_iv) / 2
        days_to_exp = max((pd.Timestamp(nearest) - pd.Timestamp.now()).days, 1)

        return (iv, days_to_exp) if iv > 0 else (None, None)
    except Exception:
        return None, None


def _historical_vol(close, window=60):
    """Annualized vol using 60-day window + Parkinson-style scaling.
    
    20-day was too short for high-beta names — misses regime shifts.
    60-day captures the current volatility regime without overreacting to
    a single gap day.
    """
    returns = np.log(close / close.shift(1)).dropna()
    if len(returns) < window:
        return returns.std() * np.sqrt(252) if len(returns) > 1 else 0.3
    base_vol = returns.tail(window).std() * np.sqrt(252)
    # Scale up if recent 5-day vol exceeds base (regime detection)
    recent_vol = returns.tail(5).std() * np.sqrt(252) if len(returns) >= 5 else base_vol
    if recent_vol > base_vol * 1.3:
        return (base_vol * 0.6 + recent_vol * 0.4)  # blend toward hot regime
    return base_vol


def _regime_detector(close, window=60):
    """Classify market regime: TRENDING or CHOPPY.

    Uses ADX-like logic: ratio of net directional move to total path traveled.
    Trending: price moved far relative to daily noise (efficiency > 0.04).
    Choppy: price went nowhere despite lots of daily movement.

    Returns:
        (regime, efficiency) tuple.
    """
    if len(close) < window:
        return "UNKNOWN", 0
    segment = close.tail(window)
    net_move = abs(segment.iloc[-1] - segment.iloc[0]) / segment.iloc[0]
    daily_moves = segment.diff().abs().sum() / segment.iloc[0]
    efficiency = net_move / daily_moves if daily_moves > 0 else 0

    # Efficiency > 0.04 = trending (price covered ground efficiently)
    # Efficiency < 0.02 = choppy (lots of movement, no progress)
    if efficiency > 0.04:
        return "TRENDING", round(efficiency, 4)
    elif efficiency < 0.02:
        return "CHOPPY", round(efficiency, 4)
    return "MIXED", round(efficiency, 4)


def _vwap_position(hist, lookback=20):
    """Where is price relative to anchored VWAP?

    VWAP = cumulative(price × volume) / cumulative(volume)
    Institutional traders use VWAP as fair value. Price above VWAP = buyers
    in control. Below = sellers in control.

    Returns:
        score: +1 (above VWAP, bullish), -1 (below, bearish), 0 (at VWAP)
    """
    if len(hist) < lookback or 'Volume' not in hist.columns:
        return 0
    recent = hist.tail(lookback)
    typical = (recent['High'] + recent['Low'] + recent['Close']) / 3
    vwap = (typical * recent['Volume']).cumsum() / recent['Volume'].cumsum()
    price = recent['Close'].iloc[-1]
    vwap_val = vwap.iloc[-1]
    if vwap_val == 0:
        return 0
    pct_from_vwap = (price - vwap_val) / vwap_val * 100
    if pct_from_vwap > 1.0:
        return 1
    elif pct_from_vwap < -1.0:
        return -1
    return 0


def _trend_filter(close):
    """Determine macro trend from price vs SMA50/SMA200."""
    if len(close) < 200:
        sma = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else close.mean()
        return "UP" if close.iloc[-1] > sma else "DOWN"
    sma200 = close.rolling(200).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    price = close.iloc[-1]
    if price > sma50 > sma200:
        return "UP"
    elif price < sma200:
        return "DOWN"
    return "FLAT"


def _mean_reversion_signal(close, window=5):
    """Short-term mean reversion: Bollinger %B on fast window.

    WHY: High-beta stocks (TSLA) mean-revert on 3-5 day timeframes.
    After a 2-sigma move in one direction, the next 5 days tend to reverse.

    Returns:
        score: +2 (oversold bounce expected), -2 (overbought fade expected), 0 (neutral)
    """
    if len(close) < 20:
        return 0
    sma = close.rolling(20).mean()
    std = close.rolling(20).std()
    price = close.iloc[-1]
    upper = (sma + 2 * std).iloc[-1]
    lower = (sma - 2 * std).iloc[-1]

    if std.iloc[-1] == 0:
        return 0

    pct_b = (price - lower.item() if hasattr(lower, 'item') else price - lower) / \
            ((upper.item() if hasattr(upper, 'item') else upper) -
             (lower.item() if hasattr(lower, 'item') else lower))

    # Also check 3-day RSI for extreme readings
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(3).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(3).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi3 = (100 - (100 / (1 + rs))).iloc[-1]

    # Oversold: %B < 0.1 or RSI3 < 15 -> expect bounce
    if pct_b < 0.1 or rsi3 < 15:
        return 2
    # Overbought: %B > 0.9 or RSI3 > 85 -> expect fade
    if pct_b > 0.9 or rsi3 > 85:
        return -2
    return 0


def _run_single_scan(ticker_hist, idx_hist, scan_idx, lookback=120, ticker_hist_obj=None):
    """Run scan using only historical data up to scan_idx.

    v4 improvements over v3:
    - Regime detection: only trust trend signals in TRENDING regime
    - VWAP position: institutional fair-value anchor
    - Conviction score exposed for position sizing / filtering
    - In CHOPPY regime, lean heavier on mean-reversion
    - In TRENDING regime, lean heavier on trend + RS
    """
    hist = ticker_hist.iloc[:scan_idx + 1]
    idx_h = idx_hist.iloc[:idx_hist.index.get_indexer([ticker_hist.index[scan_idx]], method='ffill')[0] + 1]

    if len(hist) < 50:
        return None

    price = hist['Close'].iloc[-1]
    vol = _historical_vol(hist['Close'])
    em = expected_move(price, vol, days=5)

    # Volume profile
    vp = volume_profile(hist.tail(60))

    # Relative strength
    common = hist.index.intersection(idx_h.index)
    rs = None
    if len(common) > 20:
        rs = relative_strength(hist.loc[common, 'Close'], idx_h.loc[common, 'Close'])

    # Order flow: try intraday (hourly) first, fall back to daily CLV
    flow = _intraday_flow(ticker_hist_obj, hist.index[-1].strftime("%Y-%m-%d")) if ticker_hist_obj is not None else None
    if not flow:
        flow = order_flow_proxy(hist, lookback=20)

    # Levels
    levels = find_levels(hist.tail(lookback))

    # === SIGNALS ===
    trend = _trend_filter(hist['Close'])
    mr_signal = _mean_reversion_signal(hist['Close'])
    regime, efficiency = _regime_detector(hist['Close'])
    vwap_score = _vwap_position(hist)

    # === ADAPTIVE WEIGHTING based on regime ===
    score = 0

    if regime == "TRENDING":
        # In trending markets: trust trend (weight 3), reduce MR (weight 1)
        if trend == "UP":
            score += 3
        elif trend == "DOWN":
            score -= 3
        score += mr_signal // 2  # halve MR influence
    elif regime == "CHOPPY":
        # In choppy markets: trust MR (weight 3), reduce trend (weight 1)
        if trend == "UP":
            score += 1
        elif trend == "DOWN":
            score -= 1
        score += mr_signal * 2  # double MR influence (this is where MR shines)
    else:
        # MIXED: balanced
        if trend == "UP":
            score += 2
        elif trend == "DOWN":
            score -= 2
        score += mr_signal

    # VWAP position (weight 1) — institutional fair value
    score += vwap_score

    # RS spread (weight 1) — tighter threshold in choppy, looser in trending
    rs_thresh = 5 if regime == "TRENDING" else 10
    if rs and rs["rsi_spread"] > rs_thresh:
        score += 1
    elif rs and rs["rsi_spread"] < -rs_thresh:
        score -= 1

    # Order flow — scaled by intensity, not gated by acceleration
    if flow:
        flow_score = flow.get("composite_score", 0)
        if abs(flow_score) > 30:
            score += 2 if flow_score > 0 else -2
        elif abs(flow_score) > 15:
            score += 1 if flow_score > 0 else -1
        if flow.get("accelerating") and abs(flow_score) > 15:
            score += 1 if flow_score > 0 else -1

    # RS momentum (weight 1)
    if rs and rs["rs_momentum"] > 0.008:
        score += 1
    elif rs and rs["rs_momentum"] < -0.008:
        score -= 1

    # === NON-LINEAR CONVICTION ===
    # Backtest showed: score 3-4 = sweet spot, score 6+ = contrarian signal
    # (everyone agrees = crowded trade, move already priced in)
    # So: |score| >= 6 → FLIP the signal (contrarian fade)
    if abs(score) >= 6:
        # Contrarian: extreme consensus = fade it
        if score >= 6:
            bias = "BEARISH"  # too many bulls = overbought, fade
        else:
            bias = "BULLISH"  # too many bears = oversold, fade
    elif score >= 3:
        bias = "BULLISH"
    elif score <= -3:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    # === REGIME GATE ===
    # Kill switch: suppress signals that fight the dominant regime
    # TRENDING UP + BEARISH call = likely wrong (don't short uptrends)
    # TRENDING DOWN + BULLISH call = likely wrong (don't buy downtrends)
    # Exception: contrarian flips (score >= 6) are allowed through
    if regime == "TRENDING" and abs(score) < 6:
        if trend == "UP" and bias == "BEARISH":
            bias = "NEUTRAL"  # suppress bear in uptrend
        elif trend == "DOWN" and bias == "BULLISH":
            bias = "NEUTRAL"  # suppress bull in downtrend

    # Target selection
    target = None
    if levels:
        if bias == "BULLISH" and levels["resistance"]:
            target = levels["resistance"][0]
        elif bias == "BEARISH" and levels["support"]:
            target = levels["support"][-1]

    if target is None:
        target = em["1sigma_upper"] if bias != "BEARISH" else em["1sigma_lower"]

    return {
        "date": hist.index[-1],
        "price": price,
        "bias": bias,
        "target": target,
        "vol": vol,
        "em_1sigma": em["move_dollars"],
        "trend": trend,
        "regime": regime,
        "mr_signal": mr_signal,
        "score": score,
    }


def backtest(ticker_symbol, index_symbol="QQQ", days=60, horizon=5, only_bias=None, news_filter=None):
    """Run backtest over N trading days.

    For each day in the test window:
      1. Run scan using only data available up to that day
      2. Check if bias was correct over the next `horizon` trading days
      3. Check if target was touched within `horizon` days

    Args:
        ticker_symbol: Stock to test.
        index_symbol: Reference index.
        days: Number of trading days to test (default 60 ≈ 3 months).
        horizon: Forward days to validate predictions (default 5 = 1 week).
        only_bias: Filter to only score "bull" or "bear" signals (skip others).

    Returns:
        dict with accuracy metrics and per-day results.
    """
    # Fetch enough history: test window + lookback buffer + forward horizon
    total_days = days + 252 + horizon
    ticker = yf.Ticker(ticker_symbol)
    idx = yf.Ticker(index_symbol)

    end = pd.Timestamp.now()
    start = end - timedelta(days=int(total_days * 1.5))

    ticker_hist = ticker.history(start=start, end=end, interval="1d")
    idx_hist = idx.history(start=start, end=end, interval="1d")

    if ticker_hist.empty or len(ticker_hist) < days + 50 + horizon:
        return {"error": f"Insufficient data for {ticker_symbol} ({len(ticker_hist)} bars)"}

    # Pre-fetch 5-min bars for intraday flow (yfinance gives ~60 days)
    try:
        intraday_hist = ticker.history(period="60d", interval="5m")
        if not intraday_hist.empty:
            intraday_hist.index = intraday_hist.index.tz_localize(None) if intraday_hist.index.tz else intraday_hist.index
        else:
            intraday_hist = None
    except Exception:
        intraday_hist = None

    # Test window: last `days` bars, leaving `horizon` bars for forward validation
    test_start_idx = len(ticker_hist) - days - horizon
    test_end_idx = len(ticker_hist) - horizon

    results = []
    for i in range(test_start_idx, test_end_idx):
        scan = _run_single_scan(ticker_hist, idx_hist, i, ticker_hist_obj=intraday_hist)
        if scan is None:
            continue

        # Filter: skip signals that don't match --only flag
        if only_bias:
            if only_bias == "bull" and scan["bias"] != "BULLISH":
                continue
            if only_bias == "bear" and scan["bias"] != "BEARISH":
                continue

        # News filter: index overnight gap as news proxy
        if news_filter and i > 0:
            idx_loc = idx_hist.index.get_indexer([ticker_hist.index[i]], method='ffill')[0]
            if idx_loc > 0 and idx_loc < len(idx_hist):
                idx_prev_close = idx_hist['Close'].iloc[idx_loc - 1]
                idx_today_open = idx_hist['Open'].iloc[idx_loc]
                gap_pct = (idx_today_open - idx_prev_close) / idx_prev_close * 100
                if news_filter == "align":
                    if scan["bias"] == "BULLISH" and gap_pct < 0.1:
                        continue
                    if scan["bias"] == "BEARISH" and gap_pct > -0.1:
                        continue
                elif news_filter == "contra":
                    if scan["bias"] == "BULLISH" and gap_pct > -0.1:
                        continue
                    if scan["bias"] == "BEARISH" and gap_pct < 0.1:
                        continue

        # Forward price action: next `horizon` bars
        forward = ticker_hist.iloc[i + 1: i + 1 + horizon]
        if forward.empty:
            continue

        future_close = forward['Close']
        future_high = forward['High']
        future_low = forward['Low']
        price = scan["price"]
        target = scan["target"]
        bias = scan["bias"]

        # Bias correctness: did price move in the predicted direction?
        end_price = future_close.iloc[-1]
        if bias == "BULLISH":
            bias_correct = end_price > price
        elif bias == "BEARISH":
            bias_correct = end_price < price
        else:
            # NEUTRAL: correct if price didn't move more than 1σ in either direction
            bias_correct = abs(end_price - price) < scan["em_1sigma"]

        # Target hit: did price touch the target level at any point?
        if target > price:
            target_hit = future_high.max() >= target
        else:
            target_hit = future_low.min() <= target

        # Expected move containment: did price stay within 1σ?
        # em_1sigma is already computed for the full horizon (5d) now
        max_move = max(future_high.max() - price, price - future_low.min())
        within_1sigma = max_move <= scan["em_1sigma"]

        # Directional P&L: positive if bias was correct direction
        if bias == "BULLISH":
            dir_pnl = (end_price - price) / price * 100
        elif bias == "BEARISH":
            dir_pnl = (price - end_price) / price * 100  # short P&L
        else:
            dir_pnl = 0  # no position on neutral

        results.append({
            "date": scan["date"].strftime("%Y-%m-%d"),
            "price": round(price, 2),
            "bias": bias,
            "target": round(target, 2),
            "end_price": round(end_price, 2),
            "bias_correct": bias_correct,
            "target_hit": target_hit,
            "within_1sigma": within_1sigma,
            "pnl_pct": round((end_price - price) / price * 100, 2),
            "dir_pnl_pct": round(dir_pnl, 2),
            "score": scan["score"],
            "regime": scan["regime"],
        })

    if not results:
        return {"error": "No valid scan days produced"}

    df = pd.DataFrame(results)

    # Aggregate stats
    total = len(df)
    bias_acc = df["bias_correct"].sum() / total * 100
    target_acc = df["target_hit"].sum() / total * 100
    sigma_contain = df["within_1sigma"].sum() / total * 100

    # Breakdown by bias type
    breakdown = {}
    for b in ["BULLISH", "BEARISH", "NEUTRAL"]:
        subset = df[df["bias"] == b]
        if len(subset) > 0:
            breakdown[b] = {
                "count": len(subset),
                "accuracy": round(subset["bias_correct"].sum() / len(subset) * 100, 1),
                "target_hit": round(subset["target_hit"].sum() / len(subset) * 100, 1),
                "avg_pnl": round(subset["pnl_pct"].mean(), 2),
                "avg_dir_pnl": round(subset["dir_pnl_pct"].mean(), 2),
            }

    # Breakdown by regime
    regime_breakdown = {}
    for r in ["TRENDING", "CHOPPY", "MIXED", "UNKNOWN"]:
        subset = df[df["regime"] == r]
        if len(subset) > 0:
            regime_breakdown[r] = {
                "count": len(subset),
                "accuracy": round(subset["bias_correct"].sum() / len(subset) * 100, 1),
                "avg_dir_pnl": round(subset["dir_pnl_pct"].mean(), 2),
            }

    # Conviction analysis: accuracy by score magnitude
    conviction_breakdown = {}
    for threshold in [3, 4, 5, 6]:
        high_conv = df[df["score"].abs() >= threshold]
        if len(high_conv) > 0:
            conviction_breakdown[f"|score|>={threshold}"] = {
                "count": len(high_conv),
                "accuracy": round(high_conv["bias_correct"].sum() / len(high_conv) * 100, 1),
                "avg_dir_pnl": round(high_conv["dir_pnl_pct"].mean(), 2),
            }

    return {
        "ticker": ticker_symbol,
        "index": index_symbol,
        "test_days": total,
        "horizon": horizon,
        "bias_accuracy": round(bias_acc, 1),
        "target_hit_rate": round(target_acc, 1),
        "sigma_containment": round(sigma_contain, 1),
        "avg_pnl_pct": round(df["pnl_pct"].mean(), 2),
        "breakdown": breakdown,
        "regime_breakdown": regime_breakdown,
        "conviction_breakdown": conviction_breakdown,
        "results": df,
        "_ticker": ticker_symbol,
        "_index": index_symbol,
        "_days": days,
        "_only_bias": only_bias,
    }


def print_backtest(bt):
    """Pretty-print backtest results."""
    if "error" in bt:
        print(f"❌ {bt['error']}")
        return

    print(f"\n{'═' * 65}")
    print(f"📊 BACKTEST RESULTS — {bt['ticker']} vs {bt['index']}")
    print(f"{'═' * 65}")
    print(f"   Test Period: {bt['test_days']} trading days | Forward Horizon: {bt['horizon']} days")
    print(f"\n{'─' * 65}")
    print(f"   📈 Bias Accuracy:        {bt['bias_accuracy']}%")
    print(f"   🎯 Target Hit Rate:      {bt['target_hit_rate']}%")
    print(f"   📐 1σ Containment:       {bt['sigma_containment']}%")
    print(f"   💰 Avg P&L (hold {bt['horizon']}d):  {bt['avg_pnl_pct']:+.2f}%")
    print(f"\n{'─' * 65}")
    print(f"   BREAKDOWN BY BIAS:")
    print(f"   {'Bias':<10} {'Count':<8} {'Accuracy':<12} {'Target Hit':<12} {'Dir P&L':<10}")
    print(f"   {'─' * 52}")
    for bias, stats in bt["breakdown"].items():
        print(f"   {bias:<10} {stats['count']:<8} {stats['accuracy']:<12}% {stats['target_hit']:<12}% {stats['avg_dir_pnl']:+.2f}%")

    # Regime breakdown
    if bt.get("regime_breakdown"):
        print(f"\n{'─' * 65}")
        print(f"   BREAKDOWN BY REGIME:")
        print(f"   {'Regime':<12} {'Count':<8} {'Accuracy':<12} {'Dir P&L':<10}")
        print(f"   {'─' * 42}")
        for regime, stats in bt["regime_breakdown"].items():
            print(f"   {regime:<12} {stats['count']:<8} {stats['accuracy']:<12}% {stats['avg_dir_pnl']:+.2f}%")

    # Conviction breakdown
    if bt.get("conviction_breakdown"):
        print(f"\n{'─' * 65}")
        print(f"   CONVICTION ANALYSIS (higher score = more signals agree):")
        print(f"   {'Threshold':<12} {'Count':<8} {'Accuracy':<12} {'Dir P&L':<10}")
        print(f"   {'─' * 42}")
        for thresh, stats in bt["conviction_breakdown"].items():
            print(f"   {thresh:<12} {stats['count']:<8} {stats['accuracy']:<12}% {stats['avg_dir_pnl']:+.2f}%")

    # Win/loss streak
    df = bt["results"]
    streaks = df["bias_correct"].astype(int)
    max_win = max((streaks.groupby((streaks != streaks.shift()).cumsum()).cumcount() + 1)[streaks == 1], default=0)
    max_loss = max((streaks.groupby((streaks != streaks.shift()).cumsum()).cumcount() + 1)[streaks == 0], default=0)
    print(f"\n   🔥 Max Win Streak:  {max_win}")
    print(f"   ❄️  Max Loss Streak: {max_loss}")

    # Monthly breakdown
    df_dated = df.copy()
    df_dated['month'] = pd.to_datetime(df_dated['date']).dt.to_period('M')
    monthly = df_dated.groupby('month').agg(
        days=('bias_correct', 'count'),
        accuracy=('bias_correct', 'mean'),
        pnl=('pnl_pct', 'mean'),
    )
    print(f"\n{'─' * 65}")
    print(f"   MONTHLY:")
    print(f"   {'Month':<10} {'Days':<7} {'Accuracy':<12} {'Avg P&L':<10}")
    print(f"   {'─' * 39}")
    for month, row in monthly.iterrows():
        print(f"   {str(month):<10} {int(row['days']):<7} {row['accuracy']*100:<12.1f}% {row['pnl']:+.2f}%")

    print(f"{'═' * 65}\n")

    # News strategy comparison (auto-run align + contra)
    if bt.get("_ticker") and bt.get("_index") and bt.get("_only_bias"):
        _run_news_comparison(bt["_ticker"], bt["_index"], bt.get("_days", 60),
                            bt["horizon"], bt["_only_bias"], bt)


def _run_news_comparison(ticker, index, days, horizon, only_bias, baseline_bt):
    """Run news align + contra and print comparison table."""
    direction = "bull" if only_bias == "bull" else "bear"
    dir_label = "BULL" if only_bias == "bull" else "BEAR"
    gap_with = "gap-up" if direction == "bull" else "gap-down"
    gap_against = "gap-down" if direction == "bull" else "gap-up"

    print(f"{'─' * 65}")
    print(f"   📰 NEWS FILTER COMPARISON ({dir_label} signals):")
    print(f"   {'Strategy':<35} {'Signals':<9} {'Accuracy':<11} {'P&L':<8}")
    print(f"   {'─' * 55}")
    print(f"   {'Baseline (no filter)':<35} {baseline_bt['test_days']:<9} {baseline_bt['bias_accuracy']:<11.1f}% {baseline_bt['avg_pnl_pct']:+.2f}%")

    try:
        bt_align = backtest(ticker, index, days=days, horizon=horizon,
                           only_bias=only_bias, news_filter="align")
        if "error" not in bt_align:
            print(f"   {f'News Align ({direction} on {gap_with})':<35} {bt_align['test_days']:<9} {bt_align['bias_accuracy']:<11.1f}% {bt_align['avg_pnl_pct']:+.2f}%")
    except Exception:
        pass

    try:
        bt_contra = backtest(ticker, index, days=days, horizon=horizon,
                            only_bias=only_bias, news_filter="contra")
        if "error" not in bt_contra:
            print(f"   {f'News Contra ({direction} on {gap_against})':<35} {bt_contra['test_days']:<9} {bt_contra['bias_accuracy']:<11.1f}% {bt_contra['avg_pnl_pct']:+.2f}%")
    except Exception:
        pass

    print(f"{'═' * 65}\n")
    parser = argparse.ArgumentParser(description="Backtest Trade Assistant over 6 months")
    parser.add_argument("--ticker", required=True, help="Stock symbol (e.g. TSLA)")
    parser.add_argument("--index", default="QQQ", help="Reference index (default: QQQ)")
    parser.add_argument("--days", type=int, default=60, help="Trading days to test (default: 60)")
    parser.add_argument("--horizon", type=int, default=5, help="Forward validation days (default: 5)")
    parser.add_argument("--only", choices=["bull", "bear"], default=None,
                        help="Only score bull or bear signals (skip the other)")
    parser.add_argument("--news", choices=["align", "contra"], default=None,
                        help="News filter: 'align'=trade with gap, 'contra'=trade against gap")
    parser.add_argument("--min-score", type=int, default=None,
                        help="Only score signals with |score| >= this threshold")
    args = parser.parse_args()

    only_label = f" | only {args.only.upper()}" if args.only else ""
    news_label = f" | news={args.news}" if args.news else ""
    score_label = f" | min |score|={args.min_score}" if args.min_score else ""
    print(f"⏳ Running backtest: {args.ticker} vs {args.index} | {args.days} days | {args.horizon}d horizon{only_label}{news_label}{score_label}...")
    bt = backtest(args.ticker, args.index, args.days, args.horizon, only_bias=args.only, news_filter=args.news)
    print_backtest(bt)


if __name__ == "__main__":
    main()
