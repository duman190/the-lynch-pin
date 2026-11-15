"""Valuation track record: one valuation per ticker per week, kept for good, so a visitor can see how the
Lynch Pin's past calls have done ("View track record" under the Valuation card).

Every finished analysis (a visitor's or the nightly pre-cache's) offers its valuation; the first one of each
ISO week is kept (``INSERT OR IGNORE``), so a row never changes once written and "since then" always measures
from the price the call was made at. ~100 bytes a row: a thousand tickers for five years is ~25 MB.

    rows(sym)  → newest first: {date, price, peg, bear, base, bull}
"""
import datetime as _dt
import math
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS valuations (
    ticker TEXT NOT NULL, week TEXT NOT NULL, date TEXT NOT NULL, ts REAL NOT NULL,
    price REAL NOT NULL, peg REAL, bear REAL, base REAL, bull REAL,
    PRIMARY KEY (ticker, week)) WITHOUT ROWID;
"""
LIMIT = 260  # rows returned: five years of weeks


def _num(v):
    """A finite float from 12.3, "12.3%" or "12.3", else None."""
    if isinstance(v, str):
        v = v.strip().rstrip("%").strip()
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if math.isfinite(f) else None


class TrackRecord:
    def __init__(self, path, today=_dt.date.today, clock=None):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._today = today
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def record(self, sym, entry, ts=None):
        """Keeps ``entry`` (a finished analysis) as ``sym``'s valuation for this week, unless the week has one.
        Returns True when a row was written. Analyses without a price or a PEG (an ETF, an outage) are skipped."""
        if not entry or entry.get("status") != "done":
            return False
        st = entry.get("stats") or {}
        price, peg = _num(entry.get("price")), _num(st.get("PEG"))
        if price is None or price <= 0 or peg is None or st.get("history") == "unavailable":
            return False
        day = self._today()
        iso = day.isocalendar()
        week = f"{iso[0]}-W{iso[1]:02d}"
        ts = ts if ts is not None else (self._clock() if self._clock else entry.get("generated_at") or 0.0)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO valuations (ticker, week, date, ts, price, peg, bear, base, bull) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sym, week, day.isoformat(), float(ts), price, peg,
                 _num(st.get("Bear")), _num(st.get("Base")), _num(st.get("Bull"))))
            return cur.rowcount == 1

    def rows(self, sym, limit=LIMIT):
        with self._lock:
            got = self._db.execute(
                "SELECT date, price, peg, bear, base, bull FROM valuations WHERE ticker = ? "
                "ORDER BY week DESC LIMIT ?", (sym, int(limit))).fetchall()
        return [dict(zip(("date", "price", "peg", "bear", "base", "bull"), r)) for r in got]

    def report(self, sym, current=None):
        """/api/ticker/SYM/track: the rows, newest first, each with ``since_pct``, the share price's change
        from that valuation to ``current`` (today's analysis' price; else the newest row's)."""
        rows = self.rows(sym)
        now = _num((current or {}).get("price"))
        now_date = self._today().isoformat() if now else None
        if now is None and rows:
            now, now_date = rows[0]["price"], rows[0]["date"]
        for r in rows:
            r["since_pct"] = round(100 * (now / r["price"] - 1), 2) if now else None
        currency = (current or {}).get("currency") or "USD"
        return {"ticker": sym, "current": {"price": now, "date": now_date, "currency": currency}, "rows": rows}

    def close(self):
        with self._lock:
            self._db.close()
