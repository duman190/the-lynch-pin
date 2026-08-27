"""Paper Trading Simulator — 24/7 daemon mode.

Runs continuously in background. Swing-style: positions are held until
target, stop, or a time stop — NOT force-closed at end of day (targets are
multi-day levels; an intraday horizon made the R/R fictional).

Each trading day:
  1. 7:30 AM PDT: Scan for setups, open positions (risk-based sizing)
  2. Market hours: Monitor positions — target / stop / breakeven / time stop
  3. After close: Log daily summary, sleep until next trading day

Risk model:
  - Each position risks RISK_PCT of total equity (entry-to-stop distance),
    capped at MAX_NOTIONAL_PCT of equity per position
  - Stop moves to breakeven once the trade reaches +1R
  - Time stop closes any position held MAX_HOLD_DAYS trading days
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

from experimental.trade_assistant import scan, _calc_rr
from experimental.back_test import backtest

warnings.filterwarnings("ignore")

PDT = ZoneInfo("America/Los_Angeles")
STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "simulator.json")
LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "simulator.pid")
MAX_POSITIONS = 10
RISK_PCT = 0.005          # 0.5% of total equity risked per trade (entry-to-stop)
MAX_NOTIONAL_PCT = 0.15   # cap on any single position's notional vs equity
MAX_HOLD_DAYS = 5         # trading-day time stop for swing positions
SLIPPAGE_BPS = 5.0        # slippage per side, in basis points
MIN_SCORE = 3
MAX_SCORE = 5  # History: score 6+ setups underperformed (-$139 on 15 trades) — likely over-extended moves
MIN_RR = 2.0   # Minimum risk:reward — filter applied at scan AND re-checked at fill
MIN_EDGE = 55.0
TARGET_PROXIMITY_PCT = 0.0  # Exact target hit, no proximity buffer
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

def _rank_setups(setups):
    """Rank setups: risk/reward first, score as tiebreaker (score >= MIN_SCORE
    is already enforced upstream). History showed edge*score ranking was
    counterproductive — high-edge/high-score setups underperformed, while
    R/R was the honest selector once recomputed at fill time."""
    return sorted(setups, key=lambda x: (x["rr"], x["score"]), reverse=True)


def _fill_rr(direction, price, target, stop):
    """Recompute risk:reward at the ACTUAL fill price.

    Scan-time R/R goes stale — the fill happens minutes after the scan and
    the price may have drifted through the stop/target, or compressed the
    ratio below MIN_RR. Returns the true R/R, or None if the setup is no
    longer valid (geometry broken or R/R below floor)."""
    bias = "BULLISH" if direction == "bull" else "BEARISH"
    rr = _calc_rr(bias, price, price, target, stop)
    if rr is None or rr < MIN_RR:
        return None
    return rr


def _slip(price, direction, side):
    """Apply slippage: fills are always slightly worse than the quote.

    bull entry = buy (pay up), bull exit = sell (receive less);
    bear entry = short sell (receive less), bear exit = cover (pay up)."""
    s = SLIPPAGE_BPS / 10000.0
    if (direction == "bull") == (side == "entry"):
        return price * (1 + s)
    return price * (1 - s)


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


def _maybe_breakeven(pos, price):
    """Move stop to entry once the trade reaches +1R unrealized.
    Converts intraday winners that round-trip into scratches instead of
    losers. Returns True if the stop was moved."""
    entry = pos["entry_price"]
    risk = pos.get("initial_risk") or abs(entry - pos["stop"])
    if risk <= 0:
        return False
    if pos["direction"] == "bull":
        if price >= entry + risk and pos["stop"] < entry:
            pos["stop"] = entry
            return True
    else:
        if price <= entry - risk and pos["stop"] > entry:
            pos["stop"] = entry
            return True
    return False


def scan_and_open(state):
    """Scan universe for setups and open positions."""
    tickers = _load_tickers()
    total = len(tickers)
    setups = []
    open_symbols = {p["symbol"] for p in state["positions"]}

    _log(f"Scanning {total} tickers (score {MIN_SCORE}-{MAX_SCORE}, R/R >= {MIN_RR}, edge >= {MIN_EDGE}%)...")

    for i, (sym, idx) in enumerate(tickers):
        if sym in open_symbols:
            continue
        if (i + 1) % 20 == 0:
            _log(f"  [{i+1}/{total}]...")

        try:
            bt = backtest(sym, idx, days=180)
            if "error" in bt:
                continue

            bull = bt["breakdown"].get("BULLISH", {})
            bear = bt["breakdown"].get("BEARISH", {})
            bull_acc = bull.get("accuracy", 0)
            bear_acc = bear.get("accuracy", 0)

            if bull_acc >= MIN_EDGE and bull_acc > bear_acc:
                direction, edge, edge_pnl = "bull", bull_acc, bull.get("avg_dir_pnl", 0)
            elif bear_acc >= MIN_EDGE and bear_acc > bull_acc:
                direction, edge, edge_pnl = "bear", bear_acc, bear.get("avg_dir_pnl", 0)
            else:
                continue

            result = scan(sym, idx, direction)
            if "error" in result:
                continue

            idea = result["trade_idea"]
            score = abs(idea["score"])
            if score < MIN_SCORE or score > MAX_SCORE:
                continue
            if direction == "bull" and idea["bias"] != "BULLISH":
                continue
            if direction == "bear" and idea["bias"] != "BEARISH":
                continue

            rr = idea.get("risk_reward")
            if not rr or rr < MIN_RR:
                continue

            setups.append({
                "symbol": sym, "index": idx, "direction": direction,
                "price": result["price"], "target": idea["target"],
                "stop": idea["stop"], "score": score, "edge": edge,
                "edge_pnl": edge_pnl, "rr": rr,
            })
        except Exception:
            continue

    _log(f"Scan complete. Found {len(setups)} setups.")

    # Rank: R/R first, score tiebreaker (min score/RR enforced during scan)
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
        # Re-validate R/R at the actual (slipped) fill price — the quote may
        # have drifted since the scan, breaking the geometry or the ratio.
        fill_rr = _fill_rr(setup["direction"], fill, setup["target"], setup["stop"])
        if fill_rr is None:
            _log(f"  SKIP {setup['symbol']} @ ${fill:.2f} | stale setup "
                 f"(scan R/R {setup['rr']:.2f} no longer valid at fill)")
            continue
        size = _position_size(equity, balance, fill, setup["stop"])
        if size < equity * 0.01:  # too small to matter / out of cash
            continue
        shares = size / fill
        position = {
            "symbol": setup["symbol"], "index": setup["index"],
            "direction": setup["direction"], "entry_price": round(fill, 2),
            "shares": round(shares, 4), "target": round(setup["target"], 2),
            "stop": round(setup["stop"], 2),
            "initial_risk": round(abs(fill - setup["stop"]), 4),
            "score": setup["score"], "edge": setup["edge"], "rr": fill_rr,
            "size": size,
            "opened_at": datetime.now().isoformat(),
        }
        state["positions"].append(position)
        balance -= size
        opened += 1
        arrow = "LONG" if setup["direction"] == "bull" else "SHORT"
        _log(f"  OPEN {arrow} {setup['symbol']} @ ${fill:.2f} (${size:,.0f}) | "
             f"T: ${setup['target']:.2f} S: ${setup['stop']:.2f} | "
             f"R/R: {fill_rr:.2f} Edge: {setup['edge']:.0f}% Score: {setup['score']}")

    state["balance"] = round(balance, 2)
    state["last_scan_date"] = _now_pdt().strftime("%Y-%m-%d")
    _save_state(state)
    _log(f"Opened {opened} positions. Balance: ${state['balance']:.2f}")


def check_positions(state):
    """Check all positions for target/stop/time-stop triggers, arming
    breakeven stops along the way. Returns number closed."""
    closed_indices = []
    now = datetime.now()

    for i, pos in enumerate(state["positions"]):
        price = _get_price(pos["symbol"])
        if not price:
            continue

        # Move stop to entry once trade reaches +1R
        if _maybe_breakeven(pos, price):
            _log(f"  [=] {pos['symbol']} reached +1R — stop moved to breakeven "
                 f"(${pos['stop']:.2f})")

        entry = pos["entry_price"]
        target = pos["target"]
        stop = pos["stop"]
        direction = pos["direction"]

        if direction == "bull":
            hit_target = price >= target
            hit_stop = price <= stop
        else:
            hit_target = price <= target
            hit_stop = price >= stop

        close_reason = None
        if hit_target:
            close_reason = "TARGET"
        elif hit_stop:
            close_reason = "STOP"
        elif _trading_days_held(pos["opened_at"], now) >= MAX_HOLD_DAYS:
            close_reason = "TIME_STOP"

        if close_reason:
            exit_price = _slip(price, direction, "exit")
            if direction == "bull":
                pnl_pct = (exit_price - entry) / entry * 100
            else:
                pnl_pct = (entry - exit_price) / entry * 100
            pnl_dollars = pos["size"] * pnl_pct / 100
            state["balance"] += pos["size"] + pnl_dollars
            state["history"].append({
                **pos, "exit_price": round(exit_price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_dollars": round(pnl_dollars, 2),
                "close_reason": close_reason,
                "closed_at": now.isoformat(),
            })
            closed_indices.append(i)
            icon = "+" if pnl_pct >= 0 else "-"
            _log(f"  [{icon}] CLOSED {pos['symbol']} ({close_reason}) | "
                 f"P&L: {pnl_pct:+.2f}% (${pnl_dollars:+.2f})")

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
        ret = (state["balance"] + total_invested - state["starting_balance"]) / state["starting_balance"] * 100
        print(f"  Return:    {ret:+.2f}%")
    print(f"  Last Scan: {state.get('last_scan_date', 'never')}")

    if state["positions"]:
        print(f"\n  {'Symbol':<6} {'Dir':<6} {'Entry':>7} {'Target':>7} {'Stop':>7} {'Edge':>5} {'Score':>5}")
        print(f"  {'-' * 50}")
        for pos in state["positions"]:
            d = "LONG" if pos["direction"] == "bull" else "SHORT"
            print(f"  {pos['symbol']:<6} {d:<6} ${pos['entry_price']:>6.2f} "
                  f"${pos['target']:>6.2f} ${pos['stop']:>6.2f} {pos['edge']:>4.0f}% {pos['score']:>4}")
    print()


def print_history(state):
    if not state["history"]:
        print("\nNo trade history yet.")
        return
    print(f"\n{'=' * 80}")
    print(f"  TRADE HISTORY ({len(state['history'])} trades)")
    print(f"{'=' * 80}")
    print(f"  {'Date':<12} {'Sym':<6} {'Dir':<6} {'Entry':>7} {'Exit':>7} {'P&L':>7} {'$':>8} {'Reason':<10}")
    print(f"  {'-' * 72}")
    for t in state["history"]:
        d = "LONG" if t["direction"] == "bull" else "SHORT"
        print(f"  {t['closed_at'][:10]:<12} {t['symbol']:<6} {d:<6} "
              f"${t['entry_price']:>6.2f} ${t['exit_price']:>6.2f} "
              f"{t['pnl_pct']:>+6.2f}% ${t['pnl_dollars']:>+7.2f} {t['close_reason']:<10}")
    total_pnl = sum(t["pnl_dollars"] for t in state["history"])
    wins = len([t for t in state["history"] if t["pnl_pct"] > 0])
    print(f"  {'-' * 72}")
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
    _log(f"Swing mode: positions close at target/stop/breakeven or after "
         f"{MAX_HOLD_DAYS} trading days | risk {RISK_PCT*100:.1f}%/trade | "
         f"slippage {SLIPPAGE_BPS:.0f}bps/side")
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
