"""Trade Assistant — session-start momentum & levels scanner.

Prototype: one scan per invocation (pre-market or session open).
Supports historical --date for backtesting.
Live mode: real-time dashboard with auto-refresh.

Usage:
    python -m experimental.trade_assistant --ticker TSLA --index QQQ --sentiment bear --date 2024-12-20
    python -m experimental.trade_assistant --live INTC --index SMH --sentiment bull
"""

import argparse
import os
import sys
import time
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

from experimental.quant_engine import (
    expected_move,
    iv_from_options_chain,
    volume_profile,
    relative_strength,
    move_probability,
    order_flow_proxy,
    get_order_flow,
    find_levels,
)


def _fetch_history(symbol, end_date=None, days=252):
    """Fetch daily OHLCV. If end_date provided, fetches history ending on that date."""
    ticker = yf.Ticker(symbol)
    if end_date:
        end = pd.Timestamp(end_date) + timedelta(days=1)
        start = end - timedelta(days=int(days * 1.5))  # buffer for weekends
        hist = ticker.history(start=start, end=end, interval="1d")
    else:
        hist = ticker.history(period="1y", interval="1d")
    # Drop NaN rows that sometimes appear
    if not hist.empty:
        hist = hist.dropna(subset=['Close'])
    return ticker, hist


def _get_iv(ticker, current_price):
    """Extract ATM IV from options chain, skipping near-expiry (crushed IV)."""
    try:
        expirations = ticker.options
        if not expirations:
            return None, None
        # Skip expirations < 3 DTE (IV is crushed/unreliable)
        for exp in expirations:
            expiry_date = pd.Timestamp(exp)
            days_to_exp = max((expiry_date - pd.Timestamp.now()).days, 1)
            if days_to_exp < 3:
                continue
            chain = ticker.option_chain(exp)
            calls = chain.calls[['strike', 'impliedVolatility']].copy()
            puts = chain.puts[['strike', 'impliedVolatility']].copy()
            combined = pd.concat([calls, puts])
            iv = iv_from_options_chain(combined, current_price, days_to_exp)
            if iv and iv > 0.05:  # sanity: IV should be at least 5%
                return iv, days_to_exp
        return None, None
    except Exception:
        return None, None


def _historical_vol(close, window=20):
    """Annualized realized volatility from daily returns."""
    returns = np.log(close / close.shift(1)).dropna()
    if len(returns) < window:
        return returns.std() * np.sqrt(252) if len(returns) > 1 else 0.3
    return returns.tail(window).std() * np.sqrt(252)


def scan(ticker_symbol, index_symbol="QQQ", sentiment=None, date=None):
    """Run full trade assistant scan.

    Args:
        ticker_symbol: Stock to analyze (e.g. "TSLA").
        index_symbol: Reference index (e.g. "QQQ").
        sentiment: Optional bias override ("bull" or "bear").
        date: Optional date string (YYYY-MM-DD) for backtesting.

    Returns:
        dict with complete analysis.
    """
    # Fetch data
    ticker_obj, hist = _fetch_history(ticker_symbol, end_date=date)
    _, idx_hist = _fetch_history(index_symbol, end_date=date)

    if hist.empty or len(hist) < 50:
        return {"error": f"Insufficient data for {ticker_symbol}"}

    # Use real-time price from info if available (not stale history close)
    if not date:
        try:
            info = ticker_obj.info
            live_price = info.get("currentPrice") or info.get("regularMarketPrice")
            if live_price and live_price > 0:
                current_price = live_price
            else:
                current_price = hist['Close'].iloc[-1]
        except Exception:
            current_price = hist['Close'].iloc[-1]
    else:
        current_price = hist['Close'].iloc[-1]
    scan_date = hist.index[-1].strftime("%Y-%m-%d")

    # 1. Volatility & Expected Move
    iv, days_to_exp = (None, None) if date else _get_iv(ticker_obj, current_price)
    realized_vol = _historical_vol(hist['Close'])
    vol_used = iv if iv else realized_vol
    vol_source = "IV" if iv else "realized"
    em = expected_move(current_price, vol_used, days=1)

    # 2. Volume Profile
    vp = volume_profile(hist.tail(60))  # 3-month profile

    # 3. Relative Strength vs Index
    common_dates = hist.index.intersection(idx_hist.index)
    rs = None
    if len(common_dates) > 20:
        rs = relative_strength(
            hist.loc[common_dates, 'Close'],
            idx_hist.loc[common_dates, 'Close'],
        )

    # 4. Order Flow (L2 via Polygon if available, else OHLCV proxy)
    flow = get_order_flow(ticker_symbol, df=hist, date=scan_date, lookback=20)

    # 5. Statistical Levels
    levels = find_levels(hist.tail(120))  # 6-month for level detection

    # 6. Probability Calculations for key levels
    probabilities = {}
    if levels:
        for lvl in (levels.get('support', []) + levels.get('resistance', [])):
            prob = move_probability(current_price, lvl, vol_used, days=5)
            probabilities[lvl] = prob

    # 7. Synthesize Trade Idea
    idea = _synthesize(current_price, em, vp, rs, flow, levels, probabilities, sentiment, hist=hist, idx_hist=idx_hist)

    return {
        "ticker": ticker_symbol,
        "index": index_symbol,
        "scan_date": scan_date,
        "price": round(current_price, 2),
        "volatility": {"value": round(vol_used, 4), "source": vol_source},
        "expected_move": em,
        "volume_profile": {"poc": vp["poc"], "hvn": vp["hvn"].index.tolist()} if vp else None,
        "relative_strength": rs,
        "order_flow": flow,
        "levels": levels,
        "probabilities": probabilities,
        "trade_idea": idea,
    }


def _calc_rr(bias, price, entry, target, stop):
    """Calculate risk:reward from CURRENT PRICE perspective.

    For bulls: reward = target - price, risk = price - stop
    For bears: reward = price - target, risk = stop - price

    Uses current price (not entry) because that's what matters for
    the trader deciding whether to take the trade NOW.
    """
    if not all([price, target, stop]) or price <= 0:
        return None

    if bias == "BULLISH":
        reward = target - price
        risk = price - stop
    elif bias == "BEARISH":
        reward = price - target
        risk = stop - price
    else:
        return None

    if risk <= 0 or reward <= 0:
        return None

    return round(reward / risk, 2)


def _synthesize(price, em, vp, rs, flow, levels, probs, sentiment, hist=None, idx_hist=None):
    """Combine all signals into a single trade idea.
    Uses the same v5 scoring engine as the backtest for consistency.
    """
    from experimental.back_test import (
        _trend_filter, _mean_reversion_signal, _regime_detector, _vwap_position
    )

    # Compute full scoring signals from history
    score = 0
    trend = "FLAT"
    regime = "UNKNOWN"

    if hist is not None and len(hist) >= 50:
        trend = _trend_filter(hist['Close'])
        mr_signal = _mean_reversion_signal(hist['Close'])
        regime, efficiency = _regime_detector(hist['Close'])
        vwap_score = _vwap_position(hist)

        # Adaptive weighting based on regime
        if regime == "TRENDING":
            if trend == "UP":
                score += 3
            elif trend == "DOWN":
                score -= 3
            score += mr_signal // 2
        elif regime == "CHOPPY":
            if trend == "UP":
                score += 1
            elif trend == "DOWN":
                score -= 1
            score += mr_signal * 2
        else:
            if trend == "UP":
                score += 2
            elif trend == "DOWN":
                score -= 2
            score += mr_signal

        # VWAP
        score += vwap_score

        # RS spread
        rs_thresh = 5 if regime == "TRENDING" else 10
        if rs and rs["rsi_spread"] > rs_thresh:
            score += 1
        elif rs and rs["rsi_spread"] < -rs_thresh:
            score -= 1

        # Order flow — scaled by intensity, not gated by acceleration
        if flow:
            flow_score = flow.get("composite_score", 0)
            if abs(flow_score) > 30:  # extreme flow (strong institutional signal)
                score += 2 if flow_score > 0 else -2
            elif abs(flow_score) > 15:  # moderate flow
                score += 1 if flow_score > 0 else -1
            # Acceleration bonus
            if flow.get("accelerating") and abs(flow_score) > 15:
                score += 1 if flow_score > 0 else -1

        # RS momentum
        if rs and rs["rs_momentum"] > 0.008:
            score += 1
        elif rs and rs["rs_momentum"] < -0.008:
            score -= 1

        # Sentiment override (weight 1)
        if sentiment == "bull":
            score += 1
        elif sentiment == "bear":
            score -= 1

        # Non-linear conviction
        if abs(score) >= 6:
            bias = "BEARISH" if score >= 6 else "BULLISH"
        elif score >= 3:
            bias = "BULLISH"
        elif score <= -3:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        # Regime gate
        if regime == "TRENDING" and abs(score) < 6:
            if trend == "UP" and bias == "BEARISH":
                bias = "NEUTRAL"
            elif trend == "DOWN" and bias == "BULLISH":
                bias = "NEUTRAL"
    else:
        # Fallback: simple signal count (legacy)
        signals = []
        if rs and rs["rsi_spread"] > 5:
            signals.append("BULL")
        elif rs and rs["rsi_spread"] < -5:
            signals.append("BEAR")
        if flow and flow["interpretation"] == "BUYING":
            signals.append("BULL")
        elif flow and flow["interpretation"] == "SELLING":
            signals.append("BEAR")
        if sentiment == "bull":
            signals.append("BULL")
        elif sentiment == "bear":
            signals.append("BEAR")
        bull_count = signals.count("BULL")
        bear_count = signals.count("BEAR")
        score = bull_count - bear_count
        if bull_count > bear_count:
            bias = "BULLISH"
        elif bear_count > bull_count:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

    # Pick entry, target, stop from levels + expected move
    entry = price  # default: market order at current price
    target = None
    stop = None

    if bias == "BULLISH":
        # Target: nearest resistance ABOVE price, or 1σ upper
        valid_resistance = [r for r in (levels["resistance"] if levels else []) if r > price]
        target = valid_resistance[0] if valid_resistance else em["1sigma_upper"]

        # Stop: nearest support BELOW price minus buffer, or 1σ lower
        valid_support = [s for s in (levels["support"] if levels else []) if s < price]
        stop = round(valid_support[-1] - em["move_dollars"] * 0.3, 2) if valid_support else em["1sigma_lower"]

        # Entry: at support if close to it (within 1 EM), else at market
        if valid_support and (price - valid_support[-1]) < em["move_dollars"] * 1.5:
            entry = valid_support[-1]

    elif bias == "BEARISH":
        # Target: nearest support BELOW price, or 1σ lower
        valid_support = [s for s in (levels["support"] if levels else []) if s < price]
        target = valid_support[-1] if valid_support else em["1sigma_lower"]

        # Stop: nearest resistance ABOVE price plus buffer, or 1σ upper
        valid_resistance = [r for r in (levels["resistance"] if levels else []) if r > price]
        stop = round(valid_resistance[0] + em["move_dollars"] * 0.3, 2) if valid_resistance else em["1sigma_upper"]

        # Entry: at resistance if close to it (within 1 EM), else at market
        if valid_resistance and (valid_resistance[0] - price) < em["move_dollars"] * 1.5:
            entry = valid_resistance[0]

    else:
        # NEUTRAL: no directional trade
        target = em["1sigma_upper"]
        stop = em["1sigma_lower"]

    # FINAL SANITY — no exceptions
    if bias == "BULLISH":
        if target <= price:
            target = round(price + em["move_dollars"], 2)
        if stop >= price:
            stop = round(price - em["move_dollars"], 2)
    elif bias == "BEARISH":
        if target >= price:
            target = round(price - em["move_dollars"], 2)
        if stop <= price:
            stop = round(price + em["move_dollars"], 2)

    # Probability for the target
    target_prob = probs.get(target, {}).get("probability", "N/A") if target else "N/A"

    return {
        "bias": bias,
        "entry": entry,
        "target": target,
        "stop": stop,
        "target_probability": target_prob,
        "risk_reward": _calc_rr(bias, price, entry, target, stop),
        "signal_sources": [f"trend={trend}", f"regime={regime}", f"score={score}"],
        "score": score,
        # Alignment check: is the score direction consistent with the bias?
        "aligned": (bias == "BULLISH" and score > 0) or (bias == "BEARISH" and score < 0) or bias == "NEUTRAL",
    }


def print_report(result):
    """Pretty-print the scan result to terminal."""
    if "error" in result:
        print(f"❌ {result['error']}")
        return

    r = result
    idea = r["trade_idea"]

    print(f"\n{'═' * 60}")
    print(f"🤖 TRADE ASSISTANT — {r['ticker']} vs {r['index']}")
    print(f"{'═' * 60}")
    print(f"📅 Scan Date: {r['scan_date']}")
    print(f"💰 Price: ${r['price']}")
    print(f"📊 Volatility: {r['volatility']['value']*100:.1f}% ({r['volatility']['source']})")

    em = r["expected_move"]
    print(f"\n📐 1-Day Expected Move: ±${em['move_dollars']} ({em['move_pct']}%)")
    print(f"   1σ Range: ${em['1sigma_lower']} — ${em['1sigma_upper']}")
    print(f"   2σ Range: ${em['2sigma_lower']} — ${em['2sigma_upper']}")

    if r["relative_strength"]:
        rs = r["relative_strength"]
        print(f"\n⚡ Relative Strength vs {r['index']}:")
        print(f"   RSI Spread: {rs['rsi_spread']:+.1f} | RS Momentum: {rs['rs_momentum']:+.4f}")
        print(f"   {r['ticker']} RSI: {rs['ticker_rsi']} | {r['index']} RSI: {rs['index_rsi']}")

    if r["order_flow"]:
        of = r["order_flow"]
        accel = "↑ accelerating" if of["accelerating"] else "↓ decelerating" if of["accelerating"] is False else ""
        source_label = "Polygon 1min" if of.get("source") == "polygon_1min" else "OHLCV"
        print(f"\n🌊 Order Flow [{source_label}]: {of['interpretation']} (score: {of['composite_score']:+.1f}) {accel}")
        if of.get("source") == "polygon_1min":
            print(f"   Trade Imbalance: {of['trade_imbalance']:+.1f}% | VWAP Dev: {of['vwap_deviation']:+.3f}% | Vol Accel: {of['vol_acceleration']:.2f}x")
            print(f"   Buy Vol: {of['buy_volume']:,} | Sell Vol: {of['sell_volume']:,} | Bars: {of['num_bars']}")
    if r["levels"]:
        lvl = r["levels"]
        print(f"\n🎯 Key Levels:")
        print(f"   Resistance: {', '.join(f'${l}' for l in lvl['resistance'])}")
        print(f"   Support:    {', '.join(f'${l}' for l in lvl['support'])}")

    if r["volume_profile"]:
        vp = r["volume_profile"]
        print(f"\n📊 Volume Profile POC: ${vp['poc']} | HVN: {', '.join(f'${h}' for h in vp['hvn'])}")

    print(f"\n{'─' * 60}")
    print(f"💡 TRADE IDEA — Bias: {idea['bias']}")
    print(f"   Entry:  ${idea['entry']}")
    print(f"   Target: ${idea['target']} (P(touch 5d): {idea['target_probability']}%)")
    print(f"   Stop:   ${idea['stop']}")
    if idea["risk_reward"]:
        print(f"   R:R     {idea['risk_reward']}:1")
    print(f"   Sources: {', '.join(idea['signal_sources']) or 'none (neutral)'}")
    print(f"{'═' * 60}\n")


# ─── Live Mode ────────────────────────────────────────────────────────────────

def _detect_source(ticker_symbol):
    """Detect which database file contains this ticker."""
    db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database")
    for fname in os.listdir(db_dir):
        if not fname.endswith(".txt"):
            continue
        with open(os.path.join(db_dir, fname)) as f:
            tickers = [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]
        if ticker_symbol.upper() in tickers:
            return fname.replace(".txt", "")
    return None


def _get_backtest_stats(ticker_symbol, index_symbol, sentiment):
    """Run 180-day backtest and return accuracy/P&L for the relevant bias."""
    try:
        from experimental.back_test import backtest
        only = sentiment if sentiment else None
        bt = backtest(ticker_symbol, index_symbol, days=180, only_bias=only)
        if "error" in bt:
            return None
        # Get stats for the relevant bias direction
        bias_key = "BULLISH" if sentiment == "bull" else "BEARISH" if sentiment == "bear" else None
        if bias_key and bias_key in bt["breakdown"]:
            bd = bt["breakdown"][bias_key]
            return {"accuracy": bd["accuracy"], "avg_pnl": bd["avg_dir_pnl"], "days": bt["test_days"]}
        return {"accuracy": bt["bias_accuracy"], "avg_pnl": bt["avg_pnl_pct"], "days": bt["test_days"]}
    except Exception:
        return None


def _live_price(ticker_obj):
    """Get real-time price — uses pre/post market when available."""
    try:
        info = ticker_obj.info
        # Priority: pre-market (4am-9:30am) > post-market (4pm-8pm) > regular
        pre = info.get("preMarketPrice")
        post = info.get("postMarketPrice")
        reg = info.get("currentPrice") or info.get("regularMarketPrice")

        # Determine which session we're in based on timestamps
        pre_time = info.get("preMarketTime") or 0
        post_time = info.get("postMarketTime") or 0
        reg_time = info.get("regularMarketTime") or 0

        # Use the most recent valid price
        candidates = []
        if pre and pre > 0:
            candidates.append((pre_time, pre, "pre"))
        if post and post > 0:
            candidates.append((post_time, post, "post"))
        if reg and reg > 0:
            candidates.append((reg_time, reg, "reg"))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1], candidates[0][2]
    except Exception:
        pass
    try:
        fi = ticker_obj.fast_info
        p = fi.last_price
        if p and p > 0:
            return p, "reg"
    except Exception:
        pass
    return None, None


def _trade_file():
    """Path to persistent trade state file."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "live_trades.json")


def _load_trade(ticker_symbol):
    """Load saved trade entry from disk."""
    import json
    path = _trade_file()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            trades = json.load(f)
        return trades.get(ticker_symbol.upper())
    except Exception:
        return None


def _save_trade(ticker_symbol, entry_price, target, stop, sentiment):
    """Persist trade entry to disk so it survives restarts."""
    import json
    path = _trade_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    trades = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                trades = json.load(f)
        except Exception:
            pass
    trades[ticker_symbol.upper()] = {
        "entry": entry_price, "target": target, "stop": stop,
        "sentiment": sentiment, "opened": datetime.now().isoformat()
    }
    with open(path, "w") as f:
        json.dump(trades, f, indent=2)


def live_mode(ticker_symbol, index_symbol="QQQ", sentiment=None, refresh=5,
             entry=None, target_override=None, stop_override=None, log_mode=False):
    """Real-time dashboard that refreshes every N seconds.

    Static data (levels, backtest) computed once on startup.
    Dynamic data (price, EM, R:R, score) refreshed each cycle.
    Persists entry/target/stop across sessions.
    Supports pre-market and after-hours prices.

    log_mode: Instead of clearing terminal, appends timestamped lines to
              tmp/live_{TICKER}.log — designed for nohup background use.
    """
    import warnings
    warnings.filterwarnings("ignore")

    print(f"\n⏳ Initializing live mode for {ticker_symbol}...")

    # --- Static data (computed once) ---
    source = _detect_source(ticker_symbol)
    print(f"   Loading 1Y history + levels...", end="", flush=True)
    result = scan(ticker_symbol, index_symbol, sentiment)
    if "error" in result:
        print(f"\n❌ {result['error']}")
        return
    print(" ✓")

    levels = result["levels"]
    support = levels["support"] if levels else []
    resistance = levels["resistance"] if levels else []

    # Backtest stats
    print(f"   Running 180-day backtest...", end="", flush=True)
    bt_stats = _get_backtest_stats(ticker_symbol, index_symbol, sentiment)
    print(" ✓" if bt_stats else " (no data)")

    # Load or set trade entry
    saved = _load_trade(ticker_symbol)
    if entry:
        entry_price = entry
    elif saved:
        entry_price = saved["entry"]
        # Restore overrides from saved trade if not explicitly provided
        if not target_override and saved.get("target"):
            target_override = saved["target"]
        if not stop_override and saved.get("stop"):
            stop_override = saved["stop"]
        if not sentiment and saved.get("sentiment"):
            sentiment = saved["sentiment"]
        print(f"   📂 Loaded saved trade: entry ${entry_price:.2f} (opened {saved['opened'][:10]})")
    else:
        entry_price = result["price"]

    # Apply user overrides for target/stop
    if target_override:
        # Insert into levels for display
        pass
    if stop_override:
        pass

    # Save trade state
    t_save = target_override or (result["trade_idea"]["target"] if result["trade_idea"]["target"] else None)
    s_save = stop_override or (result["trade_idea"]["stop"] if result["trade_idea"]["stop"] else None)
    _save_trade(ticker_symbol, entry_price, t_save, s_save, sentiment)

    # Cache objects for refresh
    ticker_obj = yf.Ticker(ticker_symbol)
    _, hist = _fetch_history(ticker_symbol)
    _, idx_hist = _fetch_history(index_symbol)

    print(f"\n🟢 Live mode active — refreshing every {refresh}s (Ctrl+C to exit)")
    if log_mode:
        log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", f"live_{ticker_symbol}.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        print(f"   📝 Logging to: {log_path}")
    print()
    time.sleep(1)

    last_levels_date = datetime.now().date()

    try:
        while True:
            # --- Refresh levels daily (new bar = new levels) ---
            today = datetime.now().date()
            if today > last_levels_date:
                try:
                    _, hist = _fetch_history(ticker_symbol)
                    _, idx_hist = _fetch_history(index_symbol)
                    new_levels = find_levels(hist.tail(120))
                    if new_levels:
                        old_s = support
                        old_r = resistance
                        levels = new_levels
                        support = levels["support"]
                        resistance = levels["resistance"]
                        last_levels_date = today
                        if log_mode and (old_s != support or old_r != resistance):
                            with open(log_path, "a") as lf:
                                lf.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] LEVELS UPDATED | S: {support} | R: {resistance}\n")
                except Exception:
                    pass

            # --- Dynamic data ---
            # Recreate Ticker each cycle to bust yfinance's info cache
            ticker_obj = yf.Ticker(ticker_symbol)
            price, session = _live_price(ticker_obj)
            if not price:
                price = hist['Close'].iloc[-1]
                session = "stale"

            # Session label
            session_label = {"pre": "🌅 PRE", "post": "🌙 AH", "reg": "🔴 LIVE", "stale": "⏸️ STALE"}.get(session, "🔴 LIVE")

            # Volatility & EM
            realized_vol = _historical_vol(hist['Close'])
            iv, _ = _get_iv(ticker_obj, price)
            vol_used = iv if iv else realized_vol
            em = expected_move(price, vol_used, days=1)

            # Re-score with current price context
            idea = _synthesize(price, em, result.get("volume_profile"),
                               result.get("relative_strength"), result.get("order_flow"),
                               levels, result.get("probabilities", {}), sentiment,
                               hist=hist, idx_hist=idx_hist)

            score = idea["score"]
            bias = idea["bias"]
            # Use overrides if provided, else model's levels
            target = target_override if target_override else idea["target"]
            stop = stop_override if stop_override else idea["stop"]
            rr = _calc_rr(bias if not sentiment else ("BULLISH" if sentiment == "bull" else "BEARISH"),
                          price, entry_price, target, stop)

            # Trend label
            trend_str = idea["signal_sources"][0].split("=")[1] if idea["signal_sources"] else "?"
            trend_icon = {"UP": "🟢 UP", "DOWN": "🔴 DOWN", "FLAT": "➡️ NEUT"}.get(trend_str, f"➡️ {trend_str}")

            # Score annotation
            score_note = "✅ ACTIONABLE" if abs(score) >= 3 else "⚠️ WEAK"

            # Leverage
            acc = bt_stats["accuracy"] if bt_stats else 0
            pnl_bt = bt_stats["avg_pnl"] if bt_stats else 0
            if rr and rr < 1.0:
                leverage = "🚫 bad R:R"
            elif acc >= 75 and pnl_bt >= 4 and 4 <= abs(score) <= 5:
                leverage = "🔥 2x"
            elif acc >= 70 and pnl_bt >= 4 and abs(score) >= 4:
                leverage = "✅ 2x"
            elif acc >= 70 and pnl_bt >= 3 and abs(score) >= 3:
                leverage = "1.5x"
            elif acc >= 65 and abs(score) >= 3:
                leverage = "1x"
            else:
                leverage = "⏸️ skip"

            # P&L from entry
            entry_pnl = (price - entry_price) / entry_price * 100
            if sentiment == "bear":
                entry_pnl = -entry_pnl

            # Target/stop proximity alerts
            alerts = []
            if target and abs(price - target) / price < 0.01:
                alerts.append("🎯 NEAR TARGET")
            if stop and abs(price - stop) / price < 0.01:
                alerts.append("🚨 NEAR STOP")

            # --- Render ---
            now = datetime.now().strftime("%H:%M:%S")
            now_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            lines = []
            lines.append(f"{session_label} — {now} (refresh: {refresh}s)")
            if alerts:
                lines.append(f"  {'  '.join(alerts)}")
            lines.append("─" * 55)
            lines.append(f" {ticker_symbol} | {bias} | Score: {score:+d} {score_note} | Leverage: {leverage}")
            lines.append(f"   Price: ${price:.2f} | Trend: {trend_icon} | EM: ±{em['move_pct']}%/day")
            if bt_stats:
                lines.append(f"   180d: {bt_stats['accuracy']:.0f}% accuracy, {bt_stats['avg_pnl']:+.2f}% avg P&L ({source or '?'})")
            if target and stop:
                rr_str = f"{rr}:1" if rr else "N/A"
                lines.append(f"   Target: ${target:.2f} | Stop: ${stop:.2f} | R:R: {rr_str}")
            if support:
                lines.append(f"   Support: {', '.join(f'${s:.2f}' for s in support[:4])}")
            if resistance:
                lines.append(f"   Resistance: {', '.join(f'${r:.2f}' for r in resistance[:4])}")
            lines.append("─" * 55)
            pnl_icon = "🟢" if entry_pnl >= 0 else "🔴"
            lines.append(f"   {pnl_icon} Trade P&L: {entry_pnl:+.2f}% (entry: ${entry_price:.2f})")

            if log_mode:
                sup_str = '/'.join(f'{s:.2f}' for s in support[:3])
                res_str = '/'.join(f'{r:.2f}' for r in resistance[:3])
                with open(log_path, "a") as lf:
                    lf.write(f"[{now_full}] ${price:.2f} | P&L: {entry_pnl:+.2f}% | Score: {score:+d} | R:R: {rr or 'N/A'}:1 | S:{sup_str} | R:{res_str} | {session}\n")
            else:
                os.system("clear" if os.name != "nt" else "cls")
                print("\n".join(lines))
            time.sleep(refresh)

    except KeyboardInterrupt:
        print(f"\n\n⏹️  Live mode stopped. Trade saved — will resume on next --live {ticker_symbol}")


def main():
    parser = argparse.ArgumentParser(description="Quant Trade Assistant — session scan")
    parser.add_argument("--ticker", default=None, help="Stock symbol (e.g. TSLA)")
    parser.add_argument("--live", default=None, metavar="TICKER", help="Live dashboard mode (e.g. --live INTC)")
    parser.add_argument("--index", default="QQQ", help="Reference index (default: QQQ)")
    parser.add_argument("--sentiment", choices=["bull", "bear"], default=None,
                        help="Optional directional bias override")
    parser.add_argument("--date", default=None, help="Backtest date (YYYY-MM-DD)")
    parser.add_argument("--refresh", type=int, default=5, help="Live mode refresh interval in seconds (default: 5)")
    parser.add_argument("--entry", type=float, default=None, help="Your entry price (persisted across sessions)")
    parser.add_argument("--target", type=float, default=None, help="Your target price override")
    parser.add_argument("--stop", type=float, default=None, help="Your stop price override")
    parser.add_argument("--clear", action="store_true", help="Clear saved trade for this ticker")
    parser.add_argument("--log", action="store_true", help="Log mode: append timestamped snapshots to tmp/live_{TICKER}.log (for nohup)")
    args = parser.parse_args()

    if args.live:
        sym = args.live.upper()
        if args.clear:
            import json
            path = _trade_file()
            if os.path.exists(path):
                with open(path) as f:
                    trades = json.load(f)
                if sym in trades:
                    del trades[sym]
                    with open(path, "w") as f:
                        json.dump(trades, f, indent=2)
                    print(f"✅ Cleared saved trade for {sym}")
            return
        live_mode(sym, args.index, args.sentiment, args.refresh,
                  entry=args.entry, target_override=args.target, stop_override=args.stop,
                  log_mode=args.log)
    elif args.ticker:
        result = scan(args.ticker, args.index, args.sentiment, args.date)
        print_report(result)
    else:
        parser.error("Either --ticker or --live is required")


if __name__ == "__main__":
    main()
