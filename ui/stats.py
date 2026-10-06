"""Portal stats: traffic, ticker-query latency, rejections, caching, popular tickers, AI-overview speed and
daily / monthly active users.

    python -m ui.server --lan        # portal on :8765, stats on http://<lan-ip>:190
    python -m ui.server --public     # portal on 127.0.0.1:8765 for the tunnel, stats on http://<lan-ip>:190

The stats page is a second server (``ui.server.make_stats_handler``) that runs with ``--lan`` or
``--public`` (``--stats-port``, default 190) and answers LAN / Tailscale clients only, never the tunnel.
The portal records into a ``StatsRecorder``; the stats page reads its ``summary()``.

No lock on the request path. HTTP and worker threads bump per-minute counters with ``next()`` on an
``itertools.count`` (one C call, atomic under the GIL) and append finished ticker queries and AI
overviews to a deque (``deque.append`` is atomic). One flusher thread drains both into SQLite every
few seconds; it is the database's only writer. A minute's counters are harvested two minutes after
it ends, so an increment that raced the harvest would need a thread stalled for a whole minute between
its ``setdefault`` and its ``next``. The stats page reads through its own connection (WAL: readers
and the writer never block each other).

Visitors (DAU / MAU) are the source IPs of the requests the portal served: the peer, or behind a tunnel
on this machine the visitor's CF-Connecting-IP. A request marks ``(day, ip)`` in a dict used as a set (one
atomic store); the flusher writes each pair once as a keyed hash (HMAC-SHA256 with a random key kept in
the database), so the database never holds an IP.

Retention: rows older than ``retention_days`` (30) are deleted every hour, a rolling month; visitors are
kept ``visitor_days`` (365), a rolling year.
"""
import collections
import datetime as _dt
import hashlib
import hmac
import itertools
import math
import os
import secrets
import sqlite3
import threading
import time

RETENTION_DAYS = 30
VISITOR_DAYS = 365     # DAU / MAU history
MAU_DAYS = 30          # MAU = distinct visitors in the 30 days up to a day
FLUSH_EVERY = 5.0      # seconds between flushes
PRUNE_EVERY = 3600.0   # seconds between retention sweeps
GRACE_MINUTES = 2      # a minute's counters are written this many minutes after it ends

# Refused requests by reason → (HTTP status, label). "upstream" reasons are not requests to the portal:
# Yahoo answering the portal's own calls with 429 (the circuit breaker in ui/jobs.py pauses lookups).
REJECTIONS = {
    "outside_lan": (403, "Client outside the LAN"),
    "foreign_host": (403, "Foreign Host header"),
    "tunnel_only": (403, "Not through the tunnel"),
    "visitor_busy": (429, "One analysis per visitor"),
    "queue_full": (429, "Analysis queue full"),
    "ai_queue_full": (429, "AI queue full"),
    "bad_ticker": (400, "Invalid ticker"),
    "read_only": (405, "Write method"),
    "yahoo_429": (429, "Yahoo rate limit"),
}
UPSTREAM = ("yahoo_429",)
DEFAULT_REASON = {400: "bad_ticker", 403: "outside_lan", 405: "read_only", 429: "queue_full"}

# Where a ticker query's answer came from
SOURCES = ("cache", "fresh", "joined", "recent", "refresh")

SCHEMA = """
CREATE TABLE IF NOT EXISTS minutes (minute INTEGER PRIMARY KEY, requests INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rejections (minute INTEGER NOT NULL, reason TEXT NOT NULL, n INTEGER NOT NULL,
                                       PRIMARY KEY (minute, reason));
CREATE TABLE IF NOT EXISTS queries (ts REAL NOT NULL, ticker TEXT NOT NULL, source TEXT NOT NULL, status TEXT,
                                    latency_s REAL NOT NULL);
CREATE INDEX IF NOT EXISTS queries_ts ON queries (ts);
CREATE TABLE IF NOT EXISTS ai (ts REAL NOT NULL, ticker TEXT NOT NULL, status TEXT NOT NULL, ttft_s REAL,
                               tok_s REAL, total_s REAL, wait_s REAL, tokens INTEGER, model TEXT);
CREATE INDEX IF NOT EXISTS ai_ts ON ai (ts);
CREATE TABLE IF NOT EXISTS visitors (day TEXT NOT NULL, visitor TEXT NOT NULL, PRIMARY KEY (day, visitor)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def _bump(counters, key):
    """Atomic increment of ``counters[key]`` without a lock: both calls are single C calls under the GIL,
    and racing first increments of a new key all get the one count that ``setdefault`` stored."""
    c = counters.get(key)
    if c is None:
        c = counters.setdefault(key, itertools.count())
    next(c)


def _harvest(counters, upto):
    """Pop the counters whose minute is ``<= upto`` → [(key, total)]. Only the flusher calls this."""
    out = []
    for key in tuple(counters):
        if (key if isinstance(key, int) else key[0]) <= upto:
            c = counters.pop(key, None)
            if c is not None:
                out.append((key, next(c)))  # next() returns how many increments came before it
    return out


class StatsRecorder:
    """Collects the portal's metrics (any thread, lock-free) and keeps a rolling month in SQLite."""

    def __init__(self, path, retention_days=RETENTION_DAYS, flush_every=FLUSH_EVERY, clock=time.time,
                 start=True, visitor_days=VISITOR_DAYS):
        self.path = path
        self.retention_days = max(1, int(retention_days))
        self.visitor_days = max(MAU_DAYS, int(visitor_days))
        self.flush_every = flush_every
        self._clock = clock
        self._requests = {}   # minute → count of requests the portal answered
        self._rejected = {}   # (minute, reason) → count
        self._rows = collections.deque()  # ("queries" | "ai", row): appended by any thread, drained by the flusher
        self._seen = {}       # (day, ip) → None: a set of visitors not yet written (dict stores are atomic)
        self._day = (0.0, 0.0, None)  # (start, end, ISO date) of the local day, cached for visit()
        self._pending = None  # a batch whose write failed (the flusher's own state)
        self._pruned_at = -math.inf
        self._stop = threading.Event()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = self._connect()  # the writer's connection: used by the flusher (or close()) only
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        row = self._db.execute("SELECT v FROM meta WHERE k = 'visitor_key'").fetchone()
        if row is None:
            row = (secrets.token_hex(32),)
            self._db.execute("INSERT INTO meta VALUES ('visitor_key', ?)", row)
        self._key = bytes.fromhex(row[0])
        self._db.commit()
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._flusher, name="lynch-stats", daemon=True)
            self._thread.start()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10, check_same_thread=False)

    # ── recording (any thread) ───────────────────────────────────────────────
    def hit(self, status, reason=None, ts=None):
        """One request answered by the portal (any path); a refused one also counts under its reason."""
        minute = int((self._clock() if ts is None else ts) // 60)
        c = self._requests.get(minute)  # _bump(), inlined: this runs for every request
        if c is None:
            c = self._requests.setdefault(minute, itertools.count())
        next(c)
        if reason is None:
            reason = DEFAULT_REASON.get(status)
        if reason is not None:
            _bump(self._rejected, (minute, reason))

    def upstream(self, reason, ts=None):
        """An upstream refusal (Yahoo 429) — counted with the rejections, not as a portal request."""
        _bump(self._rejected, (int((self._clock() if ts is None else ts) // 60), reason))

    def visit(self, ip, ts=None):
        """A request the portal served, from ``ip``: counts it toward that day's active users."""
        self._seen[(self._day_of(self._clock() if ts is None else ts), ip)] = None

    def _day_of(self, ts):
        lo, hi, day = self._day
        if lo <= ts < hi:
            return day
        d = _dt.datetime.fromtimestamp(ts).date()
        day = d.isoformat()
        self._day = (_dt.datetime.combine(d, _dt.time()).timestamp(),  # one atomic store: racing threads
                     _dt.datetime.combine(d + _dt.timedelta(days=1), _dt.time()).timestamp(), day)  # agree
        return day

    def _visitor_id(self, ip):
        return hmac.new(self._key, ip.encode(), hashlib.sha256).hexdigest()[:16]

    def query(self, ticker, source, status, latency_s, ts=None):
        """A ticker lookup finished: where its answer came from and how long the visitor waited for it."""
        self._rows.append(("queries", (self._clock() if ts is None else ts, ticker, source, status,
                                       max(0.0, float(latency_s)))))

    def ai(self, ticker, status, metrics=None, total_s=None, wait_s=None, model=None, ts=None):
        """An AI overview finished (generated, not served from the cache)."""
        m = metrics or {}
        self._rows.append(("ai", (self._clock() if ts is None else ts, ticker, status, m.get("ttft_s"),
                                  m.get("tok_s"), total_s, wait_s, m.get("tokens"), model)))

    # ── writer ───────────────────────────────────────────────────────────────
    def _flusher(self):
        while not self._stop.wait(self.flush_every):
            try:
                self.flush()
            except sqlite3.Error as e:  # disk full, locked too long...: the batch is kept for the next flush
                print(f"⚠️  stats: {e}", flush=True)

    def flush(self, final=False):
        """Write what has been recorded; ``final`` also writes the current (partial) minute.
        Single writer: the flusher thread, or the caller once the flusher has stopped."""
        now = self._clock()
        upto = math.inf if final else int(now // 60) - GRACE_MINUTES
        batch = self._pending or {"minutes": [], "rejections": [], "queries": [], "ai": [], "visitors": []}
        batch["minutes"] += _harvest(self._requests, upto)
        batch["rejections"] += [(m, r, n) for (m, r), n in _harvest(self._rejected, upto)]
        for _ in range(len(self._rows)):  # popleft is atomic; rows appended meanwhile wait for the next flush
            table, row = self._rows.popleft()
            batch[table].append(row)
        seen = tuple(self._seen)
        for key in seen:  # a visit stored again after this pop is simply written again next time
            self._seen.pop(key, None)
        batch["visitors"] += [(day, self._visitor_id(ip)) for day, ip in seen]
        try:
            with self._db as db:
                db.executemany("INSERT INTO minutes VALUES (?, ?) ON CONFLICT (minute) "
                               "DO UPDATE SET requests = requests + excluded.requests", batch["minutes"])
                db.executemany("INSERT INTO rejections VALUES (?, ?, ?) ON CONFLICT (minute, reason) "
                               "DO UPDATE SET n = n + excluded.n", batch["rejections"])
                db.executemany("INSERT INTO queries VALUES (?, ?, ?, ?, ?)", batch["queries"])
                db.executemany("INSERT INTO ai VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch["ai"])
                db.executemany("INSERT OR IGNORE INTO visitors VALUES (?, ?)", batch["visitors"])
        except sqlite3.Error:
            self._pending = batch  # rolled back: retried with the next flush
            raise
        self._pending = None
        if final or now - self._pruned_at >= PRUNE_EVERY:
            self.prune(now)

    def prune(self, now=None):
        """Delete everything older than the retention windows (a month; a year for visitors)."""
        now = self._clock() if now is None else now
        cutoff = now - self.retention_days * 86400
        with self._db as db:
            db.execute("DELETE FROM visitors WHERE day <= ?", (_day(now - self.visitor_days * 86400),))
            db.execute("DELETE FROM minutes WHERE minute < ?", (int(cutoff // 60),))
            db.execute("DELETE FROM rejections WHERE minute < ?", (int(cutoff // 60),))
            db.execute("DELETE FROM queries WHERE ts < ?", (cutoff,))
            db.execute("DELETE FROM ai WHERE ts < ?", (cutoff,))
        self._pruned_at = now

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.flush_every + 5)
        try:
            self.flush(final=True)
        finally:
            self._db.close()

    # ── reader (stats page) ──────────────────────────────────────────────────
    def summary(self, days=None, now=None):
        """Everything the stats page draws, for the last ``days`` days (at most the retention window)."""
        days = self.retention_days if days is None else min(max(1, int(days)), self.retention_days)
        now = self._clock() if now is None else now
        since = now - days * 86400
        db = self._connect()
        try:
            out = summarize(db, since, now, days, self.retention_days)
            out["visitors"] = visitors(db, now, self.visitor_days)
            return out
        finally:
            db.close()


# ── summaries ────────────────────────────────────────────────────────────────
_GRID = sorted({i / 200 for i in range(1, 201)} | {0.99 + i / 1000 for i in range(1, 10)}
               | {0.999 + i / 10000 for i in range(1, 10)})
EXACT_CDF = 400  # up to this many samples the curve is every sample; above, quantiles on _GRID


def _sig(x, digits=4):
    if x is None or x == 0 or not math.isfinite(x):
        return x
    return round(x, max(0, digits - 1 - int(math.floor(math.log10(abs(x))))))


def cdf(values):
    """Empirical CDF of ``values``: nearest-rank percentiles and the curve as [[x, P(X ≤ x)], ...]."""
    v = sorted(float(x) for x in values if x is not None and math.isfinite(x))
    n = len(v)
    if not n:
        return {"n": 0, "points": []}

    def q(p):
        return v[min(n - 1, max(0, math.ceil(p * n - 1e-9) - 1))]

    if n <= EXACT_CDF:
        pts = {}
        for i, x in enumerate(v):
            pts[x] = (i + 1) / n  # ties: the step's top
        points = list(pts.items())
    else:
        points = [(q(p), p) for p in _GRID]
    return {"n": n, "min": _sig(v[0]), "max": _sig(v[-1]), "mean": _sig(sum(v) / n),
            "p50": _sig(q(.5)), "p90": _sig(q(.9)), "p99": _sig(q(.99)), "p999": _sig(q(.999)),
            "points": [[_sig(x), round(p, 5)] for x, p in points]}


def _day(ts):
    return _dt.datetime.fromtimestamp(ts).date().isoformat()


def summarize(db, since, now, days, retention_days=RETENTION_DAYS):
    m0 = int(since // 60)
    out = {"generated_at": now, "since": since, "days": days, "retention_days": retention_days,
           "tz": time.strftime("%Z", time.localtime(now))}

    # Requests per minute (minutes with traffic only: an idle night is not a 0-RPM sample)
    rpm = [r for (r,) in db.execute("SELECT requests FROM minutes WHERE minute >= ?", (m0,))]
    qpm = [n for (n,) in db.execute("SELECT COUNT(*) FROM queries WHERE ts >= ? GROUP BY CAST(ts / 60 AS INTEGER)",
                                    (since,))]
    out["rpm"] = {"requests": cdf(rpm), "queries": cdf(qpm)}

    # Ticker queries: latency, where answers came from, cache hit rate per day, popular tickers
    rows = db.execute("SELECT ts, ticker, source, latency_s FROM queries WHERE ts >= ?", (since,)).fetchall()
    out["latency"] = cdf([r[3] for r in rows])
    by_source = collections.Counter(r[2] for r in rows)
    out["sources"] = {s: by_source.get(s, 0) for s in SOURCES}
    per_day = collections.defaultdict(lambda: [0, 0])
    for ts, _, source, _ in rows:
        d = per_day[_day(ts)]
        d[0] += source == "cache"
        d[1] += 1
    cache_days = [{"day": d, "hits": h, "queries": n, "rate": round(100 * h / n, 2)}
                  for d, (h, n) in sorted(per_day.items())]
    hits = by_source.get("cache", 0)
    out["cache"] = {"days": cache_days, "cdf": cdf([d["rate"] for d in cache_days]),
                    "hit_rate": round(100 * hits / len(rows), 2) if rows else None}
    tickers = collections.Counter(r[1] for r in rows)
    out["tickers"] = {"total": len(rows), "distinct": len(tickers),
                      "top": [{"ticker": t, "n": n, "pct": round(100 * n / len(rows), 2)}
                              for t, n in tickers.most_common(10)]}

    # Rejections
    total_requests = sum(rpm)
    reasons = collections.Counter()
    daily = collections.Counter()
    for minute, reason, n in db.execute("SELECT minute, reason, n FROM rejections WHERE minute >= ?", (m0,)):
        reasons[reason] += n
        if reason not in UPSTREAM:
            daily[_day(minute * 60)] += n
    rejected = sum(n for r, n in reasons.items() if r not in UPSTREAM)
    out["rejections"] = {
        "total": rejected, "requests": total_requests,
        "rate": round(100 * rejected / total_requests, 3) if total_requests else None,
        "reasons": [{"reason": r, "code": REJECTIONS.get(r, (None, r))[0], "label": REJECTIONS.get(r, (None, r))[1],
                     "n": n, "upstream": r in UPSTREAM,
                     "pct": round(100 * n / total_requests, 3) if total_requests and r not in UPSTREAM else None}
                    for r, n in reasons.most_common()],
        "daily": [{"day": d, "n": daily.get(d, 0)} for d in _days(since, now)],
    }

    # AI overviews (generated ones; cached overviews cost nothing)
    ai = db.execute("SELECT status, ttft_s, tok_s, total_s, wait_s, model FROM ai WHERE ts >= ?", (since,)).fetchall()
    done = [r for r in ai if r[0] == "done"]
    models = collections.Counter(r[5] for r in done if r[5])
    out["ai"] = {"n": len(ai), "done": len(done), "failed": len(ai) - len(done),
                 "models": [m for m, _ in models.most_common(3)],
                 "ttft": cdf([r[1] for r in done]), "speed": cdf([r[2] for r in done]),
                 "total": cdf([r[3] for r in done]), "wait": cdf([r[4] for r in done])}
    return out


def visitors(db, now, keep_days=VISITOR_DAYS):
    """DAU and MAU (distinct visitors in the MAU_DAYS days up to it) for each day of the stored year.
    Days before the first recorded one are null; the series spans at least MAU_DAYS days."""
    today = _dt.datetime.fromtimestamp(now).date()
    first = today - _dt.timedelta(days=keep_days - 1)
    by_day = collections.defaultdict(list)
    for d, v in db.execute("SELECT day, visitor FROM visitors WHERE day BETWEEN ? AND ?",
                           (first.isoformat(), today.isoformat())):
        by_day[d].append(v)
    out = {"retention_days": keep_days, "mau_days": MAU_DAYS, "days": [], "dau": 0, "mau": 0,
           "peak_dau": None, "peak_mau": None, "since": min(by_day) if by_day else None}
    if not by_day:
        return out
    since = _dt.date.fromisoformat(out["since"])
    d = min(since, today - _dt.timedelta(days=MAU_DAYS - 1))
    window = collections.Counter()  # visitor → days seen within the MAU window
    while d <= today:
        day = d.isoformat()
        window.update(by_day.get(day, ()))
        for v in by_day.get((d - _dt.timedelta(days=MAU_DAYS)).isoformat(), ()):
            window[v] -= 1
            if not window[v]:
                del window[v]
        recorded = d >= since
        out["days"].append({"day": day, "dau": len(by_day.get(day, ())) if recorded else None,
                            "mau": len(window) if recorded else None})
        d += _dt.timedelta(days=1)
    last = out["days"][-1]
    out["dau"], out["mau"] = last["dau"], last["mau"]
    for key in ("dau", "mau"):
        best = max((x for x in out["days"] if x[key] is not None), key=lambda x: x[key])
        out["peak_" + key] = {"n": best[key], "day": best["day"]}
    return out


def _days(since, now):
    """Local calendar days from ``since`` to ``now``, oldest first."""
    d, end = _dt.datetime.fromtimestamp(since).date(), _dt.datetime.fromtimestamp(now).date()
    out = []
    while d <= end:
        out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out
