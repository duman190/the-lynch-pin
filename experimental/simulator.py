"""Paper Trading Simulator — 24/7 daemon mode.

Runs continuously in background. Swing/trend-following style: positions are
held until the volatility-scaled stop, the trailing stop, or a time stop.

Each trading day:
  1. 7:30 AM PDT: Scan for setups, open positions (risk-based sizing)
  2. Market hours: Monitor positions — stop / trailing stop / time stop
  3. After close: Log daily summary, sleep until next trading day

Exit / entry model (v2 — see experimental/strategy_backtest.py and
experimental/STRATEGY_POSTMORTEM.md; numbers below are from 6,498 signals /
60 tickers / 250 days (seed 42).  A second sample (seed 7) reproduced the
diagnosis of the old rules but NOT the +0.17R of the new ones (+0.02R): v2
stops the bleeding, it is not a proven edge.):
  - Initial stop = STOP_ATR × ATR(14) from the fill.  The previous
    level-based stop sat a median 0.36 ATR away; under a zero-drift random
    walk such a stop is touched ~82% of the time within 5 days — exactly the
    live stop-out rate.  The signal was indistinguishable from a coin flip
    with that geometry (avg −0.22R vs −0.23R random direction).
  - No fixed price target.  A chandelier trail (TRAIL_ATR × ATR below the
    highest prior close for longs / above the lowest for shorts) is ratcheted
    once per day from *completed* daily bars, never loosened.  Target-based
    variants earned +0.04..+0.06R; the trail earned +0.17R (CI > 0).
  - Time stop after MAX_HOLD_DAYS trading days (20).  10-day holds cut the
    edge by more than half.
  - No breakeven arm (it lowered avg R from 0.173 to 0.157).
  - Market-regime gate: longs only when the ticker's reference index closes
    above its 50-day SMA, shorts only when below.  Removing the gate cut avg
    R to +0.10 and doubled drawdown.
  - The old MIN_EDGE gate (180-day "bias accuracy" backtest) is gone: its
    correlation with executed-trade R was 0.008 and gating on it did WORSE
    than random.  Removing it also makes the daily scan ~10x faster.
  - COOLDOWN_DAYS: a symbol that just stopped out is not re-entered for a
    while (22 live re-entries lost $165 combined).
  - Direction from the scoring engine's bias; score band 3-5 kept.

Risk model:
  - Each position risks RISK_PCT of total equity (entry-to-stop distance),
    capped at MAX_NOTIONAL_PCT of equity per position
  - SLIPPAGE_BPS applied per side so fills aren't fantasy mid-quotes

State persisted to tmp/simulator.json every cycle — survives restarts.

Usage:
    python -m experimental.simulator --start --balance 10000   # First time
    python -m experimental.simulator --continue                # Resume from saved state
    python -m experimental.simulator --status                  # Check state (no daemon)
    python -m experimental.simulator --history                 # Trade history (no daemon)
    python -m experimental.simulator --reset                   # Wipe everything

Background:
    nohup python -m experimental.simulator --start --balance 10000 &
    nohup python -m experimental.simulator --continue &
"""

import argparse
import json
import os
import sys
import time
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from experimental.trade_assistant import scan

warnings.filterwarnings("ignore")

PDT = ZoneInfo("America/Los_Angeles")
STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "simulator.json")
LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "simulator.pid")
MAX_POSITIONS = 10
RISK_PCT = 0.005          # 0.5% of total equity risked per trade (entry-to-stop)
MAX_NOTIONAL_PCT = 0.15   # cap on any single position's notional vs equity
MAX_HOLD_DAYS = 20        # trading-day time stop (10d halved the edge in backtest)
SLIPPAGE_BPS = 5.0        # slippage per side, in basis points
MIN_SCORE = 3
MAX_SCORE = 5  # History: score 6+ setups underperformed (-$139 on 15 trades) — likely over-extended moves
STOP_ATR = 2.0            # initial stop distance in ATR(14); 1.5 ATR -> avg R 0.11, 2.0 -> 0.17, 2.5 -> 0.15
TRAIL_ATR = 3.0           # chandelier trail distance in ATR(14) from best prior close
ATR_PERIOD = 14
REGIME_SMA = 50           # longs need index close > SMA50, shorts need < SMA50
COOLDOWN_DAYS = 10        # calendar days before re-entering a symbol after a stop-out
SCAN_HOUR = 7       # 7:30 AM PDT
SCAN_MINUTE = 30
CLOSE_HOUR = 12     # 12:00 PM PDT (1hr before market close) — used for daily summary
REFRESH_SECONDS = 30

DATABASES = [
    ("database/smh.txt", "SMH"),
    ("database/mag7.txt", "QQQ"),
    ("database/igv.txt", "IGV"),
    ("database/nasdaq_100.txt", "QQQ"),
    ("database/schd.txt", "SCHD"),
    ("database/fintwit_100.txt", "SPY"),
]


# ─── State Management ─────────────────────────────────────────────────────────

def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return _default_state(10000.0)


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _default_state(balance):
    return {
        "balance": balance,
        "starting_balance": balance,
        "positions": [],
        "history": [],
        "last_scan_date": None,
        "created": datetime.now().isoformat(),
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _acquire_lock():
    """Single-instance guard. Two daemons trading against the same state file
    corrupt results (duplicate scans, double fills). Returns True if acquired."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # raises if pid is dead
            return False if pid != os.getpid() else True
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale or unreadable lock — take it over
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def _now_pdt():
    return datetime.now(PDT)


def _is_trading_day():
    return _now_pdt().weekday() < 5


def _is_scan_time():
    """True if it's past 7:30 AM PDT on a trading day."""
    now = _now_pdt()
    return now.hour > SCAN_HOUR or (now.hour == SCAN_HOUR and now.minute >= SCAN_MINUTE)


def _is_market_open():
    """True if between 6:30 AM and 1:00 PM PDT on a trading day."""
    now = _now_pdt()
    if now.weekday() >= 5:
        return False
    after_open = now.hour > 6 or (now.hour == 6 and now.minute >= 30)
    before_close = now.hour < CLOSE_HOUR
    return after_open and before_close


def _is_past_close():
    """True if past 1:00 PM PDT on a trading day."""
    now = _now_pdt()
    if now.weekday() >= 5:
        return False
    return now.hour >= CLOSE_HOUR


def _get_price(symbol):
    try:
        t = yf.Ticker(symbol)
        info = t.info
        return info.get("currentPrice") or info.get("regularMarketPrice")
    except Exception:
        return None


def _load_tickers():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    all_tickers = []
    for db_file, idx in DATABASES:
        path = os.path.join(base, db_file)
        if os.path.exists(path):
            with open(path) as f:
                tickers = [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]
            for t in tickers:
                all_tickers.append((t, idx))
    seen = set()
    unique = []
    for t, idx in all_tickers:
        if t not in seen:
            seen.add(t)
            unique.append((t, idx))
    return unique


def _log(msg):
    """Print with timestamp."""
    now = _now_pdt().strftime("%m/%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


# ─── Core Logic ───────────────────────────────────────────────────────────────

def _atr(hist, period=ATR_PERIOD):
    """ATR over completed daily bars (simple mean of true range)."""
    if hist is None or len(hist) < period + 1:
        return None
    prev_close = hist["Close"].shift()
    tr = pd.concat([
        hist["High"] - hist["Low"],
        (hist["High"] - prev_close).abs(),
        (hist["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    val = tr.tail(period).mean()
    return float(val) if val and val > 0 else None


def _completed_daily_bars(symbol, period="6mo"):
    """Daily OHLCV excluding today's partial bar — the trail and ATR must only
    use information that was final at the prior close."""
    try:
        hist = yf.Ticker(symbol).history(period=period, interval="1d")
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    hist = hist.dropna(subset=["Close"])
    today = _now_pdt().date()
    return hist[hist.index.date < today]


def _index_regime(index_symbol, cache=None):
    """'UP' if the index's last completed close is above its SMA(REGIME_SMA),
    'DOWN' if below, None if unavailable. Cached per scan."""
    if cache is not None and index_symbol in cache:
        return cache[index_symbol]
    hist = _completed_daily_bars(index_symbol, period="1y")
    regime = None
    if hist is not None and len(hist) >= REGIME_SMA:
        close = hist["Close"]
        regime = "UP" if close.iloc[-1] > close.rolling(REGIME_SMA).mean().iloc[-1] else "DOWN"
    if cache is not None:
        cache[index_symbol] = regime
    return regime


def _direction_allowed(direction, regime):
    """Regime gate: longs only in an UP index regime, shorts only in DOWN.
    Backtest: gate lifted avg R from +0.10 to +0.17 and halved drawdown."""
    if regime is None:
        return False
    return (direction == "bull") == (regime == "UP")


def _days_to_earnings(symbol):
    """Calendar days until the next earnings date via yf calendar (free, no
    lxml). None if unknown. Used only as a ranking tiebreak — trades that
    carried through a report averaged +0.71R vs +0.00R otherwise, mostly
    from trail convexity across the gap."""
    try:
        cal = yf.Ticker(symbol).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None
        today = _now_pdt().date()
        future = [(d - today).days for d in dates if hasattr(d, "year") and d >= today]
        return min(future) if future else None
    except Exception:
        return None


def _rank_setups(setups):
    """Rank: earnings inside the hold window first (structural convexity
    edge), then score 3-4 over 5 (score-5 setups averaged +0.05R vs +0.21R),
    then stronger ATR-normalised trend distance."""
    def key(s):
        dte = s.get("days_to_earnings")
        soon = dte is not None and dte <= MAX_HOLD_DAYS * 7 // 5
        return (soon, s["score"] <= 4, s.get("trend_strength", 0.0))
    return sorted(setups, key=key, reverse=True)


def _slip(price, direction, side):
    """Apply slippage: fills are always slightly worse than the quote.

    bull entry = buy (pay up), bull exit = sell (receive less);
    bear entry = short sell (receive less), bear exit = cover (pay up)."""
    s = SLIPPAGE_BPS / 10000.0
    if (direction == "bull") == (side == "entry"):
        return price * (1 + s)
    return price * (1 - s)


def _initial_stop(direction, fill, atr):
    """Volatility-scaled initial stop: STOP_ATR × ATR from the fill."""
    return round(fill - STOP_ATR * atr, 2) if direction == "bull" else round(fill + STOP_ATR * atr, 2)


def _position_size(equity, cash, entry, stop):
    """Risk-based sizing: notional such that an entry-to-stop loss costs
    RISK_PCT of equity, capped at MAX_NOTIONAL_PCT of equity and available
    cash. Equal notional (the old model) let stop distance dictate dollar
    risk — trades lost anywhere from -0.75% to -4.1%."""
    if entry <= 0:
        return 0.0
    risk_frac = abs(entry - stop) / entry
    if risk_frac <= 0:
        return 0.0
    size = equity * RISK_PCT / risk_frac
    return round(min(size, equity * MAX_NOTIONAL_PCT, cash), 2)


def _trading_days_held(opened_iso, now=None):
    """Weekdays elapsed since the position opened (trading-day approximation)."""
    start = datetime.fromisoformat(opened_iso).date()
    end = (now or datetime.now()).date()
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _ratchet_trail(pos, best_close, atr):
    """Chandelier trail: for a long, stop = max(stop, best_close − TRAIL_ATR×ATR);
    for a short, stop = min(stop, best_close + TRAIL_ATR×ATR). Never loosens.
    Returns True if the stop moved."""
    if atr is None or atr <= 0 or best_close is None:
        return False
    if pos["direction"] == "bull":
        candidate = round(best_close - TRAIL_ATR * atr, 2)
        if candidate > pos["stop"]:
            pos["stop"] = candidate
            return True
    else:
        candidate = round(best_close + TRAIL_ATR * atr, 2)
        if candidate < pos["stop"]:
            pos["stop"] = candidate
            return True
    return False


def _update_trailing_stops(state, today_str):
    """Once per trading day (before monitoring) ratchet every position's trail
    from *completed* daily bars since entry. Mirrors the backtest, which
    updates the trail on each bar close and applies it from the next bar."""
    moved = 0
    for pos in state["positions"]:
        if pos.get("trail_date") == today_str:
            continue
        hist = _completed_daily_bars(pos["symbol"])
        if hist is None or hist.empty:
            continue
        opened = datetime.fromisoformat(pos["opened_at"]).date()
        since = hist[hist.index.date >= opened]
        if since.empty:
            pos["trail_date"] = today_str
            continue
        best = float(since["Close"].max() if pos["direction"] == "bull" else since["Close"].min())
        atr = _atr(hist)
        if _ratchet_trail(pos, best, atr):
            moved += 1
            _log(f"  [~] {pos['symbol']} trail ratcheted to ${pos['stop']:.2f} "
                 f"(best close ${best:.2f}, ATR ${atr:.2f})")
        pos["trail_date"] = today_str
    if moved:
        _save_state(state)
    return moved


def _in_cooldown(symbol, history, now=None):
    """True if `symbol` stopped out within COOLDOWN_DAYS calendar days."""
    now = now or datetime.now()
    for t in reversed(history):
        if t["symbol"] != symbol or t.get("close_reason") != "STOP":
            continue
        closed = datetime.fromisoformat(t["closed_at"])
        return (now - closed).days < COOLDOWN_DAYS
    return False


def scan_and_open(state):
    """Scan universe for setups and open positions."""
    tickers = _load_tickers()
    total = len(tickers)
    setups = []
    open_symbols = {p["symbol"] for p in state["positions"]}
    regime_cache = {}

    _log(f"Scanning {total} tickers (score {MIN_SCORE}-{MAX_SCORE}, stop {STOP_ATR}xATR, "
         f"trail {TRAIL_ATR}xATR, index>SMA{REGIME_SMA} gate)...")

    for i, (sym, idx) in enumerate(tickers):
        if sym in open_symbols or _in_cooldown(sym, state["history"]):
            continue
        if (i + 1) % 20 == 0:
            _log(f"  [{i+1}/{total}]...")

        try:
            regime = _index_regime(idx, regime_cache)
            if regime is None:
                continue

            result = scan(sym, idx)
            if "error" in result:
                continue

            idea = result["trade_idea"]
            score = abs(idea["score"])
            if score < MIN_SCORE or score > MAX_SCORE:
                continue
            if idea["bias"] == "BULLISH":
                direction = "bull"
            elif idea["bias"] == "BEARISH":
                direction = "bear"
            else:
                continue
            if not _direction_allowed(direction, regime):
                continue

            hist = _completed_daily_bars(sym)
            atr = _atr(hist)
            if atr is None:
                continue
            price = result["price"]
            trend_strength = (price - hist["Close"].rolling(50).mean().iloc[-1]) / atr
            if direction == "bear":
                trend_strength = -trend_strength

            setups.append({
                "symbol": sym, "index": idx, "direction": direction,
                "price": price, "atr": round(atr, 4), "score": score,
                "regime": regime, "trend_strength": round(float(trend_strength), 2),
                "days_to_earnings": _days_to_earnings(sym),
                "ref_level": idea["target"],
            })
        except Exception:
            continue

    _log(f"Scan complete. Found {len(setups)} setups.")

    setups = _rank_setups(setups)
    max_to_open = MAX_POSITIONS - len(state["positions"])

    balance = state["balance"]
    equity = balance + sum(p["size"] for p in state["positions"])

    opened = 0
    for setup in setups:
        if opened >= max_to_open:
            break
        quote = _get_price(setup["symbol"])
        if not quote:
            continue
        fill = round(_slip(quote, setup["direction"], "entry"), 4)
        stop = _initial_stop(setup["direction"], fill, setup["atr"])
        size = _position_size(equity, balance, fill, stop)
        if size < equity * 0.01:  # too small to matter / out of cash
            continue
        shares = size / fill
        position = {
            "symbol": setup["symbol"], "index": setup["index"],
            "direction": setup["direction"], "entry_price": round(fill, 2),
            "shares": round(shares, 4), "target": setup["ref_level"],
            "stop": stop, "initial_stop": stop,
            "initial_risk": round(abs(fill - stop), 4), "atr": setup["atr"],
            "score": setup["score"], "regime": setup["regime"],
            "days_to_earnings": setup["days_to_earnings"],
            "size": size,
            "opened_at": datetime.now().isoformat(),
        }
        state["positions"].append(position)
        balance -= size
        opened += 1
        arrow = "LONG" if setup["direction"] == "bull" else "SHORT"
        dte = setup["days_to_earnings"]
        _log(f"  OPEN {arrow} {setup['symbol']} @ ${fill:.2f} (${size:,.0f}) | "
             f"S: ${stop:.2f} ({STOP_ATR:.0f}xATR ${setup['atr']:.2f}) | "
             f"Score: {setup['score']} Idx: {setup['regime']} "
             f"Earn: {f'{dte}d' if dte is not None else 'n/a'}")

    state["balance"] = round(balance, 2)
    state["last_scan_date"] = _now_pdt().strftime("%Y-%m-%d")
    _save_state(state)
    _log(f"Opened {opened} positions. Balance: ${state['balance']:.2f}")


def check_positions(state):
    """Check all positions for stop / trailing-stop / time-stop triggers.
    Returns number closed."""
    closed_indices = []
    now = datetime.now()

    for i, pos in enumerate(state["positions"]):
        price = _get_price(pos["symbol"])
        if not price:
            continue

        entry = pos["entry_price"]
        stop = pos["stop"]
        direction = pos["direction"]

        hit_stop = price <= stop if direction == "bull" else price >= stop

        close_reason = None
        if hit_stop:
            close_reason = "TRAIL" if stop != pos.get("initial_stop", stop) else "STOP"
        elif _trading_days_held(pos["opened_at"], now) >= MAX_HOLD_DAYS:
            close_reason = "TIME_STOP"

        if close_reason:
            exit_price = _slip(price, direction, "exit")
            if direction == "bull":
                pnl_pct = (exit_price - entry) / entry * 100
            else:
                pnl_pct = (entry - exit_price) / entry * 100
            pnl_dollars = pos["size"] * pnl_pct / 100
            risk = pos.get("initial_risk") or abs(entry - pos.get("initial_stop", stop))
            r_mult = (pnl_pct / 100 * entry) / risk if risk else 0.0
            state["balance"] += pos["size"] + pnl_dollars
            state["history"].append({
                **pos, "exit_price": round(exit_price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_dollars": round(pnl_dollars, 2),
                "r_multiple": round(r_mult, 2),
                "close_reason": close_reason,
                "closed_at": now.isoformat(),
            })
            closed_indices.append(i)
            icon = "+" if pnl_pct >= 0 else "-"
            _log(f"  [{icon}] CLOSED {pos['symbol']} ({close_reason}) | "
                 f"P&L: {pnl_pct:+.2f}% (${pnl_dollars:+.2f}) = {r_mult:+.2f}R")

    for i in sorted(closed_indices, reverse=True):
        state["positions"].pop(i)

    if closed_indices:
        state["balance"] = round(state["balance"], 2)
        _save_state(state)

    return len(closed_indices)


def print_status(state):
    """Print current state summary."""
    total_invested = sum(p["size"] for p in state["positions"])
    print(f"\n{'=' * 60}")
    print(f"  PAPER TRADING SIMULATOR")
    print(f"{'=' * 60}")
    print(f"  Cash:      ${state['balance']:,.2f}")
    print(f"  Invested:  ${total_invested:,.2f} ({len(state['positions'])} positions)")
    print(f"  Trades:    {len(state['history'])}")
    if state["history"]:
        wins = [t for t in state["history"] if t["pnl_pct"] > 0]
        total_pnl = sum(t["pnl_dollars"] for t in state["history"])
        print(f"  Win Rate:  {len(wins)}/{len(state['history'])} ({len(wins)/len(state['history'])*100:.0f}%)")
        print(f"  Total P&L: ${total_pnl:+,.2f}")
        rs = [t["r_multiple"] for t in state["history"] if "r_multiple" in t]
        if rs:
            print(f"  Avg R:     {sum(rs)/len(rs):+.2f}R over {len(rs)} trades")
        ret = (state["balance"] + total_invested - state["starting_balance"]) / state["starting_balance"] * 100
        print(f"  Return:    {ret:+.2f}%")
    print(f"  Last Scan: {state.get('last_scan_date', 'never')}")

    if state["positions"]:
        print(f"\n  {'Symbol':<6} {'Dir':<6} {'Entry':>7} {'Stop':>7} {'Init':>7} {'ATR':>6} {'Score':>5}")
        print(f"  {'-' * 50}")
        for pos in state["positions"]:
            d = "LONG" if pos["direction"] == "bull" else "SHORT"
            print(f"  {pos['symbol']:<6} {d:<6} ${pos['entry_price']:>6.2f} "
                  f"${pos['stop']:>6.2f} ${pos.get('initial_stop', pos['stop']):>6.2f} "
                  f"${pos.get('atr', 0):>5.2f} {pos['score']:>4}")
    print()


def print_history(state):
    if not state["history"]:
        print("\nNo trade history yet.")
        return
    print(f"\n{'=' * 80}")
    print(f"  TRADE HISTORY ({len(state['history'])} trades)")
    print(f"{'=' * 80}")
    print(f"  {'Date':<12} {'Sym':<6} {'Dir':<6} {'Entry':>7} {'Exit':>7} {'P&L':>7} {'$':>8} {'R':>6} {'Reason':<10}")
    print(f"  {'-' * 79}")
    for t in state["history"]:
        d = "LONG" if t["direction"] == "bull" else "SHORT"
        r = f"{t['r_multiple']:+.2f}" if "r_multiple" in t else "   -"
        print(f"  {t['closed_at'][:10]:<12} {t['symbol']:<6} {d:<6} "
              f"${t['entry_price']:>6.2f} ${t['exit_price']:>6.2f} "
              f"{t['pnl_pct']:>+6.2f}% ${t['pnl_dollars']:>+7.2f} {r:>6} {t['close_reason']:<10}")
    total_pnl = sum(t["pnl_dollars"] for t in state["history"])
    wins = len([t for t in state["history"] if t["pnl_pct"] > 0])
    print(f"  {'-' * 79}")
    print(f"  Total: ${total_pnl:+,.2f} | Win Rate: {wins}/{len(state['history'])}")


# ─── Daemon Loop ──────────────────────────────────────────────────────────────

def run_daemon(state):
    """Main daemon loop. Runs forever until killed."""
    if not _acquire_lock():
        _log(f"ERROR: another simulator daemon is already running "
             f"(see {LOCK_FILE}). Two daemons corrupt the shared state file. Exiting.")
        sys.exit(1)

    _log(f"Daemon started. Balance: ${state['balance']:,.2f} | "
         f"{len(state['positions'])} open positions")
    _log(f"Will scan at {SCAN_HOUR}:{SCAN_MINUTE:02d} AM PDT on trading days")
    _log(f"Trend mode: stop {STOP_ATR:.1f}xATR, trail {TRAIL_ATR:.1f}xATR, time stop "
         f"{MAX_HOLD_DAYS} trading days | risk {RISK_PCT*100:.1f}%/trade | "
         f"slippage {SLIPPAGE_BPS:.0f}bps/side | index>SMA{REGIME_SMA} gate")
    _log(f"State saved to: {STATE_FILE}")
    print()

    while True:
        try:
            now = _now_pdt()
            today_str = now.strftime("%Y-%m-%d")

            # --- Phase 1: Scan & Open (once per day at 7:30 AM PDT) ---
            if (_is_trading_day() and _is_scan_time()
                    and state.get("last_scan_date") != today_str
                    and len(state["positions"]) < MAX_POSITIONS):
                scan_and_open(state)

            # --- Phase 2: Monitor positions during market hours ---
            if state["positions"] and _is_market_open():
                # Ratchet chandelier trails once per day from completed bars
                _update_trailing_stops(state, today_str)
                closed = check_positions(state)
                if closed:
                    _log(f"Balance: ${state['balance']:,.2f} | "
                         f"{len(state['positions'])} remaining")

            # --- Phase 3: Daily summary (once, after close) ---
            if (_is_trading_day() and _is_past_close()
                    and state.get("last_summary_date") != today_str):
                today_trades = [t for t in state["history"]
                                if t["closed_at"][:10] == today_str
                                and t["close_reason"] != "SESSION_END"]
                invested = sum(p["size"] for p in state["positions"])
                if today_trades or state["positions"]:
                    day_pnl = sum(t["pnl_dollars"] for t in today_trades)
                    day_wins = len([t for t in today_trades if t["pnl_pct"] > 0])
                    _log(f"DAY DONE: {day_wins}/{len(today_trades)} wins closed | "
                         f"P&L: ${day_pnl:+.2f} | Cash: ${state['balance']:,.2f} | "
                         f"{len(state['positions'])} open (${invested:,.0f})")
                state["last_summary_date"] = today_str
                _save_state(state)

            # Save state periodically
            _save_state(state)

            # Sleep interval: shorter during market hours
            if _is_market_open() and state["positions"]:
                time.sleep(REFRESH_SECONDS)
            else:
                # Off-hours: check every 5 minutes
                time.sleep(300)

        except KeyboardInterrupt:
            _save_state(state)
            _log(f"Daemon stopped. Balance: ${state['balance']:,.2f} | "
                 f"{len(state['positions'])} positions open.")
            break
        except Exception as e:
            _log(f"ERROR: {e}")
            _save_state(state)
            time.sleep(60)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Paper Trading Simulator (daemon)")
    parser.add_argument("--start", action="store_true", help="Start fresh with --balance, then run daemon")
    parser.add_argument("--continue", dest="cont", action="store_true", help="Resume from saved state, run daemon")
    parser.add_argument("--balance", type=float, default=10000.0, help="Starting balance (default: $10,000)")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    parser.add_argument("--history", action="store_true", help="Print trade history and exit")
    parser.add_argument("--reset", action="store_true", help="Wipe everything")
    parser.add_argument("--close-all", action="store_true", help="Force close all positions and exit")
    args = parser.parse_args()

    if args.reset:
        state = _default_state(10000.0)
        _save_state(state)
        print("Done. Reset to $10,000.")
        return

    state = _load_state()

    if args.status:
        print_status(state)
        return

    if args.history:
        print_history(state)
        return

    if args.close_all:
        for pos in state["positions"]:
            price = _get_price(pos["symbol"]) or pos["entry_price"]
            if pos["direction"] == "bull":
                pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
            else:
                pnl_pct = (pos["entry_price"] - price) / pos["entry_price"] * 100
            pnl_dollars = pos["size"] * pnl_pct / 100
            state["balance"] += pos["size"] + pnl_dollars
            state["history"].append({
                **pos, "exit_price": round(price, 2), "pnl_pct": round(pnl_pct, 2),
                "pnl_dollars": round(pnl_dollars, 2), "close_reason": "MANUAL",
                "closed_at": datetime.now().isoformat(),
            })
            print(f"  Closed {pos['symbol']} | P&L: {pnl_pct:+.2f}%")
        state["positions"] = []
        state["balance"] = round(state["balance"], 2)
        _save_state(state)
        print(f"Balance: ${state['balance']:.2f}")
        return

    if args.start:
        # Close any stale positions from previous run
        if state["positions"]:
            for pos in state["positions"]:
                price = _get_price(pos["symbol"]) or pos["entry_price"]
                if pos["direction"] == "bull":
                    pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
                else:
                    pnl_pct = (pos["entry_price"] - price) / pos["entry_price"] * 100
                pnl_dollars = pos["size"] * pnl_pct / 100
                state["history"].append({
                    **pos, "exit_price": round(price, 2), "pnl_pct": round(pnl_pct, 2),
                    "pnl_dollars": round(pnl_dollars, 2), "close_reason": "SESSION_END",
                    "closed_at": datetime.now().isoformat(),
                })
            state["positions"] = []
        state["balance"] = args.balance
        state["starting_balance"] = args.balance
        state["last_scan_date"] = None
        _save_state(state)
        run_daemon(state)
        return

    if args.cont:
        run_daemon(state)
        return

    # Default: print status
    print_status(state)


if __name__ == "__main__":
    main()
