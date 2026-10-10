"""US stock market valuation (home page, between the ticker search and Latest scans): two S&P 500 charts drawn in the
Lynch Pin style (graphics/market_valuation.py), with a Peter Lynch quote under them (static/index.html).

* Rear view mirror: the Shiller PE (CAPE), monthly since 1881. Robert Shiller's data as multpl.com publishes it,
  extended to the current month. One request a day.
* Forward looking: the S&P 500 forward PEG. Its history, monthly from 1995 to January 2026, is Yardeni Research's
  (forward P/E over I/B/E/S's long-term growth consensus, which is not public), traced from a Yahoo Finance chart into
  ui/assets/sp500_peg_yardeni.csv. After it comes the Lynch Pin's own point each weekday: every constituent's PEG
  (Yahoo's "PEG Ratio (5yr expected)"), weighted by market cap as

      index PEG = Σ cap_i / Σ (cap_i / PEG_i)

  i.e. the index's P/E over the growth of its total earnings (Σ cap / Σ earnings, over the earnings-weighted mean
  growth), Yardeni's definition, whatever P/E Yahoo's PEG is built on. A plain cap-weighted mean of PEGs would let slow
  growers' high PEGs swamp it. Constituents without a PEG (losses, no growth estimate, a value outside PEG_RANGE) are
  left out, and each point keeps its coverage: the share of the index's market cap behind it. A sweep reads
  database/sp500.txt (one ticker per company) one at a time, Yahoo's quote and PEG (2 requests each, ~15 min), once a
  week: a slow-moving ratio needs no more.

A background thread fetches the Shiller PE at ``run_at`` (6 PM Pacific: after the close and the 1 PM scan) each
weekday, and sweeps the S&P 500 at that time each Friday; on startup it runs whatever was missed (a missed sweep is
dated the last weekday's close). Page views only read what is on disk. A failed run keeps the last good chart and is
retried later (an hour for the Shiller PE, three for the sweep).
"""
import datetime
import html
import json
import os
import re
import sys
import tempfile
import threading
import time

from ui.config import REPO_ROOT
from ui.socials import parse_hhmm

SHILLER_URL = "https://www.multpl.com/shiller-pe/table/by-month"
RUN_AT = "18:00"                 # weekdays, after the US close and after the 1 PM scan...
RUN_TZ = "America/Los_Angeles"   # ...Pacific time, whatever the server's clock says
PEG_WEEKDAY = 4                  # the S&P 500 sweep: weekly, after Friday's close
TICKERS_FILE = os.path.join(REPO_ROOT, "database", "sp500.txt")
REFERENCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "sp500_peg_yardeni.csv")
PEG_RANGE = (0.1, 50.0)          # a PEG outside it is a data glitch (a tiny one would swamp the harmonic mean)
RETRY_S = {"shiller": 3600, "peg": 3 * 3600}
PAUSE_S = 0.5                    # between two constituents: gentle on Yahoo, ~15 min a sweep
THROTTLE_S = (60.0, 900.0)       # after a Yahoo 429: first pause, longest (doubles per throttled retry)
MAX_THROTTLES = 6                # throttled retries in a row before the sweep gives up (~45 min of pauses)
BREAKER_WAIT_S = 30.0            # the portal's Yahoo circuit breaker is open: visitors' lookups go first
MIN_COVERAGE = 0.8               # a point needs constituents with a PEG holding 80% of the index's cap...
MIN_ANSWERED = 0.8               # ...and Yahoo answering for 80% of the names
MAX_POINTS = 5000                # ~20 years of weekdays
CHARTS = {"shiller": "shiller_pe", "peg": "forward_peg"}
PREVIEW_WIDTH = 1100
IMG_RE = re.compile(r"^(shiller_pe|forward_peg)\.(png|jpg)$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def valuation_day(run_at=RUN_AT, now=None, tz=RUN_TZ, weekday=None):
    """The weekday whose run is the latest due: today once ``run_at`` has passed in ``tz``, else the weekday before;
    with ``weekday`` (0 = Monday), the latest such day that is that weekday."""
    from zoneinfo import ZoneInfo
    h, m = parse_hhmm(run_at)
    now = now or datetime.datetime.now(ZoneInfo(tz))
    day = now.date() - datetime.timedelta(days=int((now.hour, now.minute) < (h, m)))
    while day.weekday() >= 5 or (weekday is not None and day.weekday() != weekday):
        day -= datetime.timedelta(days=1)
    return day.isoformat()


def load_tickers(path=TICKERS_FILE):
    """The constituents, one per line (blank lines and # comments skipped), de-duplicated in order."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            sym = line.split("#", 1)[0].strip().upper()
            if TICKER_RE.match(sym) and sym not in out:
                out.append(sym)
    return out


def load_reference(path=REFERENCE_FILE):
    """The forward PEG's history before the Lynch Pin's own points: [(ISO date mid-month, PEG)] oldest first, from a
    ``month,peg`` CSV (# comments); [] when the file is missing."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^(\d{4})-(\d{2}),\s*([\d.]+)\s*$", line)
                if m:
                    out.append((f"{m.group(1)}-{m.group(2)}-15", float(m.group(3))))
    except OSError:
        pass
    return sorted(out)


# ── the Shiller PE (multpl.com) ──────────────────────────────────────────────
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun",
                                      "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})", re.I)
_VALUE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d+)?)(?![\d.,])")


def parse_multpl(page):
    """[(ISO date, value)] oldest first from a multpl.com table page: a row per month, newest first, the current
    month's estimate on top. Tolerant of the markup inside a row (tags, entities, the † estimate mark); raises
    ValueError when the page doesn't hold such a table."""
    points = {}
    for row in _ROW_RE.findall(page):
        text = html.unescape(_TAG_RE.sub(" ", row))
        m = _DATE_RE.search(text)
        v = _VALUE_RE.search(text, m.end()) if m else None
        if not v:
            continue
        try:
            day = datetime.date(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        except ValueError:
            continue
        value = float(v.group(1))
        if 1 < value < 100:
            points.setdefault(day.isoformat(), value)
    if len(points) < 600:  # 50 years: anything less is not the monthly table
        raise ValueError(f"expected multpl.com's monthly table, found {len(points)} rows")
    return sorted(points.items())


def fetch_shiller(url=SHILLER_URL):
    from curl_cffi.requests import Session
    with Session(impersonate="chrome", timeout=30) as s:
        r = s.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"{url}: HTTP {r.status_code}")
    return parse_multpl(r.text)


# ── the S&P 500 forward PEG (Yahoo's PEGs, cap-weighted) ─────────────────────
def fetch_constituent(sym):
    """(market cap, Yahoo's PEG Ratio (5yr expected), forward PE) of one ticker; None where missing. yfinance reads the
    quote and the PEG in two requests, through the engine's browser-impersonating session."""
    import yfinance as yf
    from engine.lynch_pin_core import GLOBAL_SESSION
    info = yf.Ticker(sym, session=GLOBAL_SESSION).info or {}
    return info.get("marketCap"), info.get("trailingPegRatio") or info.get("pegRatio"), info.get("forwardPE")


def index_peg(rows):
    """The index's PEG = Σ cap / Σ (cap / PEG) from ``rows`` = {ticker: (cap, PEG, forward PE) or None for no answer},
    and its forward PE (Σ cap / Σ forward earnings, over the same constituents); None when none has a PEG."""
    answered = [r for r in rows.values() if r and r[0] and r[0] > 0]
    used = [r for r in answered if r[1] and PEG_RANGE[0] <= r[1] <= PEG_RANGE[1]]
    if not used:
        return None
    cap = sum(r[0] for r in used)
    with_pe = [r for r in used if len(r) > 2 and r[2] and r[2] > 0]
    fwd_pe = sum(r[0] for r in with_pe) / sum(r[0] / r[2] for r in with_pe) if with_pe else None
    return {"peg": round(cap / sum(c / p for c, p, *_ in used), 4), "fwd_pe": round(fwd_pe, 2) if fwd_pe else None,
            "coverage": round(cap / sum(r[0] for r in answered), 4),
            "n": len(used), "answered": len(answered), "total": len(rows)}


def _yahoo_429s():
    """Yahoo 429s seen by this process so far (ui/yahoo.py counts them once installed; the workers do the same)."""
    from ui import yahoo
    yahoo.install()
    return yahoo.rate_limit_events()


class MarketValuation:
    def __init__(self, cache_dir, run_at=RUN_AT, tz=RUN_TZ, tickers_file=TICKERS_FILE, reference_file=REFERENCE_FILE,
                 paused=None,
                 fetch_shiller=fetch_shiller, fetch_constituent=fetch_constituent, yahoo_429s=_yahoo_429s,
                 pause_s=PAUSE_S):
        from zoneinfo import ZoneInfo
        parse_hhmm(run_at)
        ZoneInfo(tz)  # unknown zone: fail at startup, not at 6 PM
        self.run_at, self.tz = run_at, tz
        self.dir = os.path.join(cache_dir, "valuation")
        self.preview_dir = os.path.join(self.dir, "previews")
        self.tickers_file = tickers_file
        self._paused = paused or (lambda: False)  # True while the portal's Yahoo circuit breaker is open
        self._fetch_shiller, self._fetch_constituent, self._429s = fetch_shiller, fetch_constituent, yahoo_429s
        self.pause_s = pause_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._running = {}    # job → progress, while it runs
        self._failed_at = {}  # job → when it last failed
        self.shiller = self._load("shiller.json")
        self.peg = self._load("peg.json")
        self.reference = load_reference(reference_file)  # the PEG's history before the first sweep

    # ── scheduling ───────────────────────────────────────────────────────────
    def start(self, every_s=60):
        """Redraws the stored charts (the style may have changed since), then checks every ``every_s`` seconds
        (cheap: no network) whether a run is due."""
        def loop():
            self.redraw()
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(every_s)
        threading.Thread(target=loop, name="valuation", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def tick(self):
        """Starts each job whose run is due, in its own thread (a sweep takes ~15 min). Returns the jobs started.
        The Shiller PE is due once per weekday; the sweep once its last point predates the latest Friday's run, and
        its point is dated the latest weekday's close."""
        day = valuation_day(self.run_at, tz=self.tz)
        due = {"shiller": day, "peg": valuation_day(self.run_at, tz=self.tz, weekday=PEG_WEEKDAY)}
        started = []
        for job in CHARTS:
            with self._lock:
                if job in self._running or (self._day(job) or "") >= due[job] \
                        or time.time() - self._failed_at.get(job, 0) < RETRY_S[job]:
                    continue
                self._running[job] = {}
            threading.Thread(target=self._run, args=(job, day), name=f"valuation-{job}", daemon=True).start()
            started.append(job)
        return started

    def _day(self, job):
        if job == "shiller":
            return self.shiller.get("day")
        history = self.peg.get("history") or []
        return history[-1]["date"] if history else None

    def _run(self, job, day):
        try:
            (self.refresh_shiller if job == "shiller" else self.refresh_peg)(day)
        except Exception as e:  # keep the last good chart; try again later
            if not self._stop.is_set():  # (a sweep cut short by the server stopping is not a failure)
                sys.stderr.write(f"⚠️  valuation: {job} failed: {type(e).__name__}: {str(e)[:200]}\n")
            self._failed_at[job] = time.time()
        finally:
            with self._lock:
                self._running.pop(job, None)

    # ── jobs ─────────────────────────────────────────────────────────────────
    def refresh_shiller(self, day=None):
        doc = {"day": day or valuation_day(self.run_at, tz=self.tz), "fetched": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "source": SHILLER_URL, "series": [[d, v] for d, v in self._fetch_shiller()]}
        self._save("shiller.json", doc)
        with self._lock:
            self.shiller = doc
        self._render("shiller")
        return doc

    def refresh_peg(self, day=None):
        """One sweep over the constituents → that day's point (replacing a point of the same day). Raises when it
        stopped early or covers too little of the index, and then nothing is recorded."""
        day = day or valuation_day(self.run_at, tz=self.tz)
        try:
            return self._record_peg(day)
        finally:  # still "running" until the point is on disk, so tick() never starts a second sweep for it
            with self._lock:
                self._running.pop("peg", None)

    def _record_peg(self, day):
        tickers = load_tickers(self.tickers_file)
        t0 = time.monotonic()
        print(f"📉 Valuation: S&P 500 forward PEG for {day}, {len(tickers)} constituents one at a time", flush=True)
        rows = self._sweep(tickers)
        point = index_peg(rows)
        if not point or point["coverage"] < MIN_COVERAGE or point["answered"] < MIN_ANSWERED * len(tickers):
            raise RuntimeError(f"too little of the index to record a point: {point}")
        point = dict(date=day, **point)
        history = [p for p in self.peg.get("history") or [] if p.get("date") != day] + [point]
        history.sort(key=lambda p: p["date"])
        doc = {"history": history[-MAX_POINTS:], "fetched": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        self._save("peg.json", doc)
        with self._lock:
            self.peg = doc
        self._render("peg")
        print(f"📉 Valuation: S&P 500 forward PEG {point['peg']:.2f}, {point['n']}/{point['total']} constituents, "
              f"{point['coverage']:.0%} of the index's cap, in {(time.monotonic() - t0) / 60:.0f} min", flush=True)
        return point

    def _sweep(self, tickers):
        """{ticker: (cap, PEG, forward PE) or None}, one ticker at a time; a throttled ticker is asked again."""
        rows, i, throttles, backoff = {}, 0, 0, THROTTLE_S[0]
        with self._lock:
            self._running["peg"] = {"done": 0, "total": len(tickers)}
        while i < len(tickers):
            if self._stop.is_set():
                raise RuntimeError("stopped")
            if self._paused():
                self._stop.wait(BREAKER_WAIT_S)
                continue
            sym, before = tickers[i], self._429s()
            try:
                row = self._fetch_constituent(sym)
            except Exception:  # a name Yahoo had nothing for (or a stray error): left out
                row = None
            if self._429s() > before:  # throttled: whatever came back is degraded, so ask again after a pause
                throttles += 1
                if throttles > MAX_THROTTLES:
                    raise RuntimeError(f"Yahoo kept answering 429 ({sym}, {i}/{len(tickers)} done)")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, THROTTLE_S[1])
                continue
            throttles, backoff = 0, THROTTLE_S[0]
            rows[sym] = row
            i += 1
            with self._lock:
                self._running["peg"] = {"done": i, "total": len(tickers)}
            self._stop.wait(self.pause_s)
        return rows

    # ── charts ───────────────────────────────────────────────────────────────
    def redraw(self):
        """Draws the charts of the stored data (at startup: a new release may draw them differently)."""
        for job in CHARTS:
            if self._day(job) or (job == "peg" and self.reference):
                try:
                    self._render(job)
                except Exception as e:
                    sys.stderr.write(f"⚠️  valuation: drawing the {job} chart failed: {type(e).__name__}: {e}\n")

    def _render(self, job):
        from graphics import market_valuation as charts
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".draw-", suffix=".png")
        os.close(fd)
        try:
            if job == "shiller":
                charts.plot_shiller_pe(self.shiller["series"], tmp)
            else:
                charts.plot_forward_peg(self.peg.get("history") or [], tmp, reference=self.reference)
            os.replace(tmp, os.path.join(self.dir, CHARTS[job] + ".png"))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _chart(self, job):
        """The chart's URLs, versioned by the file's mtime so a browser may keep them; None before the first."""
        name = CHARTS[job]
        try:
            v = int(os.path.getmtime(os.path.join(self.dir, name + ".png")))
        except OSError:
            return None
        return {"image": f"/valuation/{name}.png?v={v}", "preview": f"/valuation/{name}.jpg?v={v}"}

    def image_path(self, name):
        """/valuation/shiller_pe.png = the full chart (lightbox), .jpg = the page's preview; nothing else."""
        m = IMG_RE.match(name or "")
        png = os.path.join(self.dir, m.group(1) + ".png") if m else None
        if not png or not os.path.isfile(png):
            return None
        if m.group(2) == "png":
            return png
        from ui.imaging import jpeg_preview
        return jpeg_preview(png, self.preview_dir, PREVIEW_WIDTH)

    # ── the page ─────────────────────────────────────────────────────────────
    def snapshot(self):
        """What the page shows (never calls out)."""
        with self._lock:
            shiller, peg = self.shiller, self.peg
            running = {k: dict(v) for k, v in self._running.items()}
        out = {"enabled": True, "shiller": None, "peg": None, "running": running}
        series, urls = shiller.get("series") or [], self._chart("shiller")
        if series and urls:
            vals = [v for _, v in series]
            out["shiller"] = dict(value=series[-1][1], date=series[-1][0], since=series[0][0][:4],
                                  mean=round(sum(vals) / len(vals), 1), **urls)
        history, urls = peg.get("history") or [], self._chart("peg")
        if (history or self.reference) and urls:
            if history:  # the latest Lynch Pin point
                last = dict({k: history[-1].get(k) for k in ("date", "peg", "fwd_pe", "coverage", "n", "total")},
                            source="lynch_pin")
            else:  # before the first sweep: the history's last month
                last = {"date": self.reference[-1][0], "peg": self.reference[-1][1], "source": "yardeni"}
            out["peg"] = dict(last, since=(self.reference or [(history[0]["date"], 0)])[0][0][:4],
                              points=len(history), **urls)
        return out

    # ── storage ──────────────────────────────────────────────────────────────
    def _load(self, name):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                doc = json.load(f)
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, name, doc):
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".part")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(self.dir, name))
