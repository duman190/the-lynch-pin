"""The trading view (``/api/tv/SYM?i=CODE``): candles of one ticker at any of 18 intervals, with its moving averages,
RSI and MACD, for the chart the Technicals card opens (ui/static/tradingview.js). The page draws our support /
resistance levels from the ticker's analysis on top.

Cost per viewer, cheapest first:

* **A chart someone already looked at** is bytes in memory: the gzipped JSON of every (ticker, interval) is kept
  until its Yahoo series is refreshed, and a page that already has it gets a 304 (ETag).
* **Another interval of a series in memory** is a build: ui/tvdata.py → the native library (ui/native/src/bars.rs),
  ~1-5 ms without the GIL, so HTTP threads build in parallel.
* **A series that is not in memory or has expired** is one Yahoo request, made by a small pool of fetcher threads
  (``tv_fetches`` at once, each keeping its connection to Yahoo alive: ~30 ms instead of ~250 ms with a new TLS
  handshake). Viewers of the same series share it: concurrent requests wait for the one fetch. Series expire
  after 30 s-10 min while the market is open (1m bars soonest), after 15 min while it is closed.

Yahoo is shared with the analyses (~12 Yahoo calls per cold lookup), so the trading view yields to them:

* **At most ``tv_viewers`` people use it at once.** A viewer is a page with the chart open (its ``s`` id), alive
  while it polls (every 30-60 s); one that stops polling for ``idle`` seconds frees its place. Past the limit the
  page gets 429 "trading view full" with when a place frees up. ui/README.md has the benchmark behind the default.
* **Chart fetches have a Yahoo budget:** at most ``tv_yahoo_rpm`` a minute for everyone (default 120, ~6% of the
  ~2100 Yahoo requests a minute the lookups make at full speed) and ``tv_viewer_rpm`` (20) a minute triggered by one
  visitor — its IP (an IPv6 /64 behind the tunnel), not the page's ``s`` id, which the page picks and can change —
  so no viewer behaviour — nor a script cycling through tickers — can take Yahoo's quota from the lookups. A live
  viewer's 2-second reads of its own ticker are covered by the live limit instead; a live read of any other ticker
  (a viewer switching tickers, or a script cycling through them) is a new fetch and pays like one.
  The budget is a token bucket (a burst of a quarter of a minute's worth, so a crowd arriving at once is served):
  past it, a fetch waits its turn up to 8 s while viewers see the last series in memory (marked stale) if there is
  one; beyond that it is refused for a few seconds. Cached charts and joining a fetch already under way cost
  nothing.
* **When Yahoo throttles** (429), chart fetches pause with the analyses' circuit breaker (ui/jobs.py): a chart
  fetch that gets a 429 opens it, and while it is open, viewers get the last series in memory (marked stale) or
  wait.
"""
import collections
import concurrent.futures
import gzip
import re
import threading
import time

from ui import tvdata

SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
YAHOO_CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
# Seconds a series stays fresh while the market is open; while it is closed, CLOSED_TTL (or until the open).
OPEN_TTL = {"1m": 30, "2m": 30, "5m": 60, "15m": 60, "60m": 120, "1d": 120, "1wk": 600, "1mo": 600}
CLOSED_TTL = 900
NODATA_TTL = 3600          # an unknown symbol is asked again after an hour
FAIL_TTL = 15              # a failed fetch (network, 5xx) is retried after this long
BUDGET_WAIT = 8.0          # s a fetch may wait for its share of the Yahoo budget before it is refused
FETCH_TIMEOUT = 20
SERIES_BYTES = 128 << 20   # raw Yahoo series kept in memory (~50-500 KB each)
CHART_BYTES = 64 << 20     # built charts kept (gzip, ~1-100 KB each)
PER_VISITOR = 2            # --public: open trading views per visitor IP (two tabs)
LIVE_TTL = 2.0             # s: the live last candle is read from Yahoo at most this often per ticker, for every live viewer
LIVE_IDLE = 10.0           # s: a live viewer that stopped polling (every 2 s) gives its live place up
LIVE_BARS = 60             # 1-minute bars sent to a live page (it updates its last candle from those after its start)
BASE_STEP = {"1m": 60, "2m": 120, "5m": 300, "15m": 900, "60m": 3600}


class Series:
    __slots__ = ("raw", "fetched", "ttl", "version", "nodata", "live")

    def __init__(self, raw, fetched, version, nodata=None):
        self.raw, self.fetched, self.version, self.nodata = raw, fetched, version, nodata
        self.ttl = None   # set from the market's hours by the first build (its meta)
        self.live = False


class Chart:
    __slots__ = ("version", "gz", "meta", "etag")

    def __init__(self, version, gz, meta, etag):
        self.version, self.gz, self.meta, self.etag = version, gz, meta, etag


class Reply:
    """What the HTTP handler sends: ``status`` 200 (``gz``), 304, 404 (no data), 429 (full), 502 / 503."""
    __slots__ = ("status", "gz", "etag", "headers", "error", "reason", "retry_after")

    def __init__(self, status, gz=None, etag=None, headers=None, error=None, reason=None, retry_after=None):
        self.status, self.gz, self.etag, self.headers = status, gz, etag, headers or {}
        self.error, self.reason, self.retry_after = error, reason, retry_after

    def body(self, accept_gzip):
        return self.gz if accept_gzip else gzip.decompress(self.gz)


class YahooFetcher:
    """GET of Yahoo's chart JSON on a curl_cffi session that impersonates Chrome, as yfinance does (Yahoo throttles
    other TLS fingerprints sooner). curl_cffi keeps one connection per thread, so the service's fetcher threads
    reuse theirs. Returns (status, body)."""

    def __init__(self):
        self._session = None
        self._lock = threading.Lock()

    def session(self):
        if self._session is None:
            with self._lock:
                if self._session is None:
                    from curl_cffi import requests as cr
                    self._session = cr.Session(impersonate="chrome")
        return self._session

    def __call__(self, sym, base):
        r = self.session().get(YAHOO_CHART.format(sym=sym), params=tvdata.query_params(base), timeout=FETCH_TIMEOUT)
        return r.status_code, r.content


class TradingView:
    def __init__(self, settings, fetch=None, jobs=None, stats=None, clock=time.monotonic, wall=time.time,
                 build=None):
        self.limit = max(0, int(getattr(settings, "tv_viewers", 0) or 0))
        self.idle = float(getattr(settings, "tv_idle", 75))
        self.public = bool(getattr(settings, "public", False))
        self.force_open = bool(getattr(settings, "tv_force_open", False))  # benchmarks: market-hours TTLs at night
        self.fetch = fetch or YahooFetcher()
        self.jobs = jobs    # ui.jobs.JobManager: the Yahoo circuit breaker shared with the analyses
        self.stats = stats  # ui.stats.StatsRecorder: Yahoo 429s (upstream)
        self._build = build or tvdata.build
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        self._leases = {}   # (ip, sid) → last request (clock)
        self._series = collections.OrderedDict()   # (sym, base) → Series, LRU
        self._series_bytes = 0
        self._charts = collections.OrderedDict()   # (sym, code) → Chart, LRU
        self._chart_bytes = 0
        self._inflight = {}  # (sym, base) → Future of the fetch
        self._failed = {}    # (sym, base) → (until, message): a failed fetch is not retried at once
        self._version = 0
        self.yahoo_rpm = max(0, int(getattr(settings, "tv_yahoo_rpm", 0) or 0))     # 0 = no budget
        self.viewer_rpm = max(0, int(getattr(settings, "tv_viewer_rpm", 0) or 0))   # 0 = no per-viewer cap
        # Yahoo budget: a token bucket, yahoo_rpm a minute with a burst of a quarter of that. A fetch past it waits for
        # its token (the bucket goes into debt) up to BUDGET_WAIT seconds; beyond that it is refused.
        self._burst = max(4.0, self.yahoo_rpm / 4)
        self._tokens, self._refilled = self._burst, clock()
        self._fetched = collections.deque()  # clock of each chart fetch in the last minute (for /api/health)
        self._viewer_fetched = {}            # visitor IP → deque of the same (per-viewer cap)
        self._sleep = time.sleep
        # live last candle: a few viewers at a time (each costs up to 30 Yahoo requests a minute for its ticker)
        self.live_limit = max(0, int(getattr(settings, "tv_live", 2) or 0))
        self._live_leases = {}  # (ip, sid) → last live poll (clock)
        self._live_bound = {}   # (ip, sid) → the (sym, ext) its last paid live read was for: re-reads of it are free
        self._live = {}         # (sym, ext) → (fetched clock, payload bytes gzip)
        self._live_inflight = {}
        workers = max(1, int(getattr(settings, "tv_fetches", 4) or 4))
        self._pool = concurrent.futures.ThreadPoolExecutor(workers, thread_name_prefix="lynch-tv-yahoo")
        self.fetch_workers = workers
        self.counters = collections.Counter()  # requests, fetches, builds, not_modified, refused, yahoo_429…
        self._counters_lock = threading.Lock()

    def _count(self, name):
        with self._counters_lock:
            self.counters[name] += 1

    # ── viewers ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _sweep(self, now):
        for k in [k for k, t in self._leases.items() if now - t > self.idle]:
            del self._leases[k]

    def admit(self, ip, sid):
        """None if this viewer may use the trading view now (and it holds a place until it goes idle), else a
        refusal Reply. ``sid`` identifies one open chart (a page); without one the viewer is its IP."""
        key = (ip, sid or "")
        now = self._clock()
        with self._lock:
            self._sweep(now)
            if key in self._leases or not self.limit:
                if self.limit:
                    self._leases[key] = now
                return None
            if self.public and sum(1 for k in self._leases if k[0] == ip) >= PER_VISITOR:
                self._count("refused")
                return Reply(429, error=f"{PER_VISITOR} trading views at a time per visitor — close one first",
                             reason="tv_visitor", retry_after=int(self.idle))
            if len(self._leases) >= self.limit:
                wait = max(1, int(self.idle - (now - min(self._leases.values()))) + 1)
                self._count("refused")
                return Reply(429, error=f"the trading view is full: {self.limit} people are using it",
                             reason="tv_full", retry_after=wait)
            self._leases[key] = now
            return None

    def release(self, ip, sid):
        with self._lock:
            self._leases.pop((ip, sid or ""), None)
            self._live_leases.pop((ip, sid or ""), None)
            self._live_bound.pop((ip, sid or ""), None)

    def viewers(self):
        with self._lock:
            self._sweep(self._clock())
            return len(self._leases)

    # ── series (Yahoo) ───────────────────────────────────────────────────────────────────────────────────────────
    def _paused(self):
        """Seconds until Yahoo may be asked again (the analyses' circuit breaker), 0 = now."""
        if self.jobs is None:
            return 0
        try:
            return int(self.jobs.yahoo_cooldown())
        except AttributeError:
            return 0

    def _ttl(self, key, meta):
        """(seconds the series stays fresh, market open) from the session in Yahoo's meta. With extended hours
        (key "15m:ext") the market is open from the pre-market's start to the post-market's end."""
        base, _, ext = key.partition(":")
        if self.force_open:
            return OPEN_TTL[base], True
        now = self._wall()
        reg = meta.get("regular") or {}
        start, end = reg.get("start"), reg.get("end")
        if ext:
            start = (meta.get("pre") or {}).get("start", start)
            end = (meta.get("post") or {}).get("end", end)
        if not (isinstance(start, int) and isinstance(end, int)):
            return OPEN_TTL[base], True
        if start <= now < end + 60:
            step = BASE_STEP.get(base)
            if step:  # fresh bars right after each bar closes: a series expires at the next bar boundary (+3 s)
                nxt = start + -(-(now - start) // step) * step
                return int(max(3, min(OPEN_TTL[base], nxt - now + 3))), True
            return OPEN_TTL[base], True
        if now < start:  # before today's open: fresh bars as soon as it opens
            return int(max(OPEN_TTL[base], min(CLOSED_TTL, start - now + 5))), False
        return CLOSED_TTL, False

    def _fresh(self, s, now):
        ttl = s.ttl if s.ttl is not None else OPEN_TTL.get("1m")
        return now - s.fetched < (NODATA_TTL if s.nodata else ttl)

    def _budget(self, viewer, now):
        """Lock held. Seconds the new Yahoo fetch for ``viewer`` must wait for its share of the budget (it is counted
        now), or a refusal Reply."""
        for q in [self._fetched] + list(self._viewer_fetched.values()):
            while q and now - q[0] >= 60:
                q.popleft()
        for k in [k for k, q in self._viewer_fetched.items() if not q]:
            del self._viewer_fetched[k]
        mine = self._viewer_fetched.get(viewer)
        if self.viewer_rpm and viewer is not None and mine is not None and len(mine) >= self.viewer_rpm:
            self._count("refused_rate")
            return Reply(429, error=f"more than {self.viewer_rpm} new charts in a minute — slow down a little",
                         reason="tv_rate", retry_after=int(60 - (now - mine[0])) + 1)
        wait = 0.0
        if self.yahoo_rpm:
            rate = self.yahoo_rpm / 60.0
            self._tokens = min(self._burst, self._tokens + (now - self._refilled) * rate)
            self._refilled = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / rate
                if wait > BUDGET_WAIT:
                    self._count("over_budget")
                    return Reply(503, error="the trading view has used its share of Yahoo Finance for now — "
                                            "the analyses come first", reason="tv_budget", retry_after=int(wait) + 1)
                self._count("budget_waits")
            self._tokens -= 1
        self._fetched.append(now)
        if viewer is not None:
            self._viewer_fetched.setdefault(viewer, collections.deque()).append(now)
        return wait

    def series(self, sym, base, viewer=None):
        """(Series or None, stale, error Reply or None): fresh from memory, else fetched once for every waiter."""
        key = (sym, base)
        now = self._clock()
        with self._lock:
            s = self._series.get(key)
            if s is not None and self._fresh(s, now):
                self._series.move_to_end(key)
                return s, False, None
            failed = self._failed.get(key)
            wait = self._paused()
            if (failed and failed[0] > now) or wait:
                if s is not None:
                    return s, True, None  # stale beats nothing
                if wait:
                    return None, False, Reply(503, error=f"Yahoo Finance is rate-limiting this server — retrying in "
                                                         f"{wait} s", reason="yahoo_paused", retry_after=wait)
                return None, False, Reply(502, error=failed[1], retry_after=int(failed[0] - now) + 1)
            fut = self._inflight.get(key)
            if fut is None:  # the first asker fetches; the others wait for the same fetch
                wait = self._budget(viewer, now)
                if isinstance(wait, Reply):
                    return (s, True, None) if s is not None else (None, False, wait)
                start = now + wait  # when its token is due: a fetch that leaves the pool's queue later doesn't wait again
                if wait and s is not None:  # over budget: refresh in the background, show what we have meanwhile
                    self._inflight[key] = self._pool.submit(self._fetch, key, start)
                    return s, True, None
                fut = self._inflight[key] = self._pool.submit(self._fetch, key, start)
        try:
            fut.result(timeout=FETCH_TIMEOUT + BUDGET_WAIT + 10)
        except Exception:
            pass  # _fetch stored the outcome before the future resolved
        with self._lock:
            s2 = self._series.get(key)
            if s2 is not None and s2 is not s:
                return s2, False, None
            failed = self._failed.get(key)
            if s is not None:
                return s, True, None
            msg = failed[1] if failed else "Yahoo Finance did not answer"
            if failed and failed[2] == 429:
                return None, False, Reply(503, error=msg, reason="yahoo_paused", retry_after=self._paused() or 30)
            return None, False, Reply(502, error=msg, retry_after=FAIL_TTL)

    def _fetch(self, key, start=None):
        """One Yahoo request (fetcher thread), not before ``start`` (clock; when the budget's token for it is due). The
        series, or why it failed, is stored before the future resolves, so every waiter finds it."""
        if start is not None:
            wait = start - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._count("fetches")
        status, body, err = None, None, None
        try:
            status, body = self.fetch(*key)
        except Exception as e:  # network
            err = f"Yahoo Finance unreachable ({type(e).__name__})"
        now = self._clock()
        throttled = status == 429
        with self._lock:
            self._inflight.pop(key, None)
            if err is None and status == 200:
                self._store(key, Series(body, now, self._next_version()))
                self._failed.pop(key, None)
            elif err is None and status == 404:
                self._store(key, Series(body, now, self._next_version(), nodata="no chart data for this symbol"))
                self._failed.pop(key, None)
            else:
                err = err or ("Yahoo Finance is rate-limiting this server" if throttled
                              else f"Yahoo Finance answered {status}")
                self._failed[key] = (now + FAIL_TTL, err, status)
        if throttled:
            self._count("yahoo_429")
            if self.jobs is not None and hasattr(self.jobs, "yahoo_throttled"):
                self.jobs.yahoo_throttled(f"trading view {key[0]} {key[1]}")
            elif self.stats is not None:
                self.stats.upstream("yahoo_429")

    def _next_version(self):
        self._version += 1
        return self._version

    def _store(self, key, s):
        old = self._series.pop(key, None)
        if old is not None:
            self._series_bytes -= len(old.raw)
        self._series[key] = s
        self._series_bytes += len(s.raw)
        while self._series_bytes > SERIES_BYTES and len(self._series) > 1:
            _, gone = self._series.popitem(last=False)
            self._series_bytes -= len(gone.raw)

    # ── charts ───────────────────────────────────────────────────────────────────────────────────────────────────
    def chart(self, sym, code, ip=None, sid=None, if_none_match=None, ext=False):
        """The trading view's reply for one (ticker, interval), with pre- and post-market bars when ``ext``."""
        self._count("requests")
        if code not in tvdata.INTERVALS:
            return Reply(400, error="unknown interval")
        refused = self.admit(ip, sid)
        if refused is not None:
            return refused
        base = tvdata.series_key(code, ext)  # "15m", or "15m:ext" with extended hours (intraday only)
        ext = base.endswith(":ext")
        s, stale, err = self.series(sym, base, viewer=ip or "")  # the per-viewer cap is per visitor, whatever its sid
        if err is not None:
            return err
        if s.nodata:
            return Reply(404, error=s.nodata)
        key = (sym, code, ext)
        with self._lock:
            ch = self._charts.get(key)
            if ch is not None and ch.version == s.version:
                self._charts.move_to_end(key)
        if ch is None or ch.version != s.version:
            try:
                built = self._build(s.raw, code, ext=ext)  # native: GIL released
            except tvdata.NoData as e:
                return Reply(404, error=f"no chart data for this symbol ({e})")
            except tvdata.BuildError as e:
                return Reply(502, error=f"could not read Yahoo's chart ({e})")
            self._count("builds")
            tag = f"{sym}-{code}{'-x' if ext else ''}-{s.version}"
            ch = Chart(s.version, built.gz or gzip.compress(built.body, 2), built.meta, f'W/"{tag}"')
            with self._lock:
                if s.ttl is None:
                    s.ttl, s.live = self._ttl(base, built.meta)
                old = self._charts.pop(key, None)
                if old is not None:
                    self._chart_bytes -= len(old.gz)
                self._charts[key] = ch
                self._chart_bytes += len(ch.gz)
                while self._chart_bytes > CHART_BYTES and len(self._charts) > 1:
                    _, gone = self._charts.popitem(last=False)
                    self._chart_bytes -= len(gone.gz)
        now = self._clock()
        ttl = s.ttl or OPEN_TTL[base.partition(":")[0]]
        age = max(0, int(now - s.fetched))
        headers = {"X-TV-Age": str(age), "X-TV-Refresh": str(max(5, int(ttl - (now - s.fetched)) + 1)),
                   "X-TV-Live": "1" if s.live else "0"}
        if stale:
            headers["X-TV-Stale"] = "1"
        if if_none_match and ch.etag in [t.strip() for t in if_none_match.split(",")]:
            self._count("not_modified")
            return Reply(304, etag=ch.etag, headers=headers)
        return Reply(200, gz=ch.gz, etag=ch.etag, headers=headers)

    # ── live: the last candle every 2 s ─────────────────────────────────────────────────────────────────────────
    def live(self, sym, ip=None, sid=None, ext=False):
        """Today's last 1-minute bars and price for the page's live last candle (gzipped JSON), shared by every live
        viewer of ``sym`` and read from Yahoo at most every LIVE_TTL seconds. At most ``live_limit`` live viewers."""
        self._count("live_requests")
        refused = self.admit(ip, sid)  # a live viewer is a viewer too
        if refused is not None:
            return refused
        key, now = (ip, sid or ""), self._clock()
        with self._lock:
            for k in [k for k, t in self._live_leases.items() if now - t > LIVE_IDLE]:
                del self._live_leases[k]
                self._live_bound.pop(k, None)
            if key not in self._live_leases and len(self._live_leases) >= self.live_limit:
                self._count("live_refused")
                wait = max(1, int(LIVE_IDLE - (now - min(self._live_leases.values(), default=now))) + 1)
                return Reply(429, error=f"live is in use ({self.live_limit} at a time: it spends Yahoo's quota) — the chart "
                                        "still refreshes every bar", reason="tv_live", retry_after=wait)
            self._live_leases[key] = now
            skey = (sym, bool(ext))
            hit = self._live.get(skey)
            if hit is not None and now - hit[0] < LIVE_TTL:
                return Reply(200, gz=hit[1])
            wait = self._paused()
            if wait:
                if hit is not None:
                    return Reply(200, gz=hit[1], headers={"X-TV-Stale": "1"})
                return Reply(503, error=f"Yahoo Finance is rate-limiting this server — retrying in {wait} s",
                             reason="yahoo_paused", retry_after=wait)
            fut = self._live_inflight.get(skey)
            if fut is None:
                if self._live_bound.get(key) != skey:  # a new lease, or another ticker: a fetch like a chart's
                    paid = self._budget(ip or "", now)
                    if isinstance(paid, Reply):
                        return Reply(200, gz=hit[1], headers={"X-TV-Stale": "1"}) if hit is not None else paid
                    self._live_bound[key] = skey
                fut = self._live_inflight[skey] = self._pool.submit(self._live_fetch, skey)
        try:
            fut.result(timeout=FETCH_TIMEOUT + 5)
        except Exception:
            pass
        with self._lock:
            hit = self._live.get(skey)
        if hit is None:
            return Reply(502, error="Yahoo Finance did not answer", retry_after=5)
        return Reply(200, gz=hit[1], headers={"X-TV-Stale": "1"} if self._clock() - hit[0] > 3 * LIVE_TTL else {})

    def _live_fetch(self, skey):
        import json
        sym, ext = skey
        self._count("live_fetches")
        payload = None
        status = None
        try:
            status, body = self.fetch(sym, "live:ext" if ext else "live")
            if status == 200:
                payload = live_payload(json.loads(body), ext)
        except Exception:
            payload = None
        with self._lock:
            self._live_inflight.pop(skey, None)
            if payload is not None:
                self._live[skey] = (self._clock(), gzip.compress(json.dumps(payload).encode(), 2))
                while len(self._live) > 64:
                    self._live.pop(next(iter(self._live)))
        if status == 429:
            self._count("yahoo_429")
            if self.jobs is not None and hasattr(self.jobs, "yahoo_throttled"):
                self.jobs.yahoo_throttled(f"trading view live {sym}")

    def status(self):
        with self._lock:
            self._sweep(self._clock())
            out = {"viewers": len(self._leases), "limit": self.limit,
                   "native": tvdata.native() is not None,
                   "series": len(self._series), "series_mb": round(self._series_bytes / 1048576, 1),
                   "charts": len(self._charts), "charts_mb": round(self._chart_bytes / 1048576, 1),
                   "fetching": len(self._inflight), "fetch_workers": self.fetch_workers,
                   "yahoo_last_min": sum(1 for t in self._fetched if self._clock() - t < 60),
                   "live": sum(1 for t in self._live_leases.values() if self._clock() - t <= LIVE_IDLE),
                   "live_limit": self.live_limit,
                   "yahoo_rpm": self.yahoo_rpm}
        with self._counters_lock:
            out.update(self.counters)
        return out

    def shutdown(self):
        self._pool.shutdown(wait=False)


def live_payload(chart, ext):
    """Yahoo's 1-minute chart JSON → {price, time, open, bars: [[t, o, h, l, c, v], …]} (the last LIVE_BARS)."""
    r = (chart.get("chart", {}).get("result") or [None])[0]
    if not r:
        return None
    m = r.get("meta") or {}
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    price, ptime = m.get("regularMarketPrice"), m.get("regularMarketTime")
    bars = []
    for i in range(max(0, len(ts) - LIVE_BARS), len(ts)):
        o, h, lo, c, v = (col[i] if i < len(col) else None for col in cols)
        if c is None and i == len(ts) - 1 and isinstance(price, (int, float)):
            c = price  # Yahoo's bar in progress can come without its close
        if None in (o, h, lo, c):
            continue
        bars.append([ts[i], o, max(h, o, c), min(lo, o, c), c, v or 0])
    cur = m.get("currentTradingPeriod") or {}
    start = (cur.get("pre") if ext else cur.get("regular")) or {}
    end = (cur.get("post") if ext else cur.get("regular")) or {}
    now = time.time()
    is_open = isinstance(start.get("start"), int) and isinstance(end.get("end"), int) and start["start"] <= now < end["end"]
    if bars:  # with extended hours the newest trade is the newest bar's close, not the regular session's price
        price, ptime = (bars[-1][4], bars[-1][0]) if ext or not isinstance(price, (int, float)) else (price, ptime)
    return {"price": price, "time": ptime, "open": bool(is_open), "bars": bars}
