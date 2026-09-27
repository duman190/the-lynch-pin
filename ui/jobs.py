"""Job model for ticker lookups: one analysis worker, a bounded FIFO queue, a per-job
watchdog and a pluggable result store.

Why one worker: pyplot and LynchPinVisualizer's rcParams are process-global, the engine's
lazy SEC CIK map is not thread-safe, and Yahoo / EDGAR punish parallel scraping. HTTP
threads only enqueue, poll snapshots and read the store.

Watchdog: an engine call can hang (yfinance retries, EDGAR, backtest downloads) and a Python
thread cannot be killed. When a running job exceeds ``deadline`` seconds it is marked
``error: timed out``, the worker *generation* is bumped and a fresh worker takes over the
queue; the orphaned thread's late results are discarded because its generation is stale.
"""
import collections
import copy
import datetime as _dt
import os
import threading
import time

from ui.analysis import STAGES, TickerAnalyzer

FINAL = ("done", "nodata", "error")


class DayStore:
    """Plain per-day dict (step 3). Replaced by the LFU cache in ui/cache.py (step 5)."""

    def __init__(self, today=_dt.date.today):
        self._today = today
        self._day = None
        self._data = {}
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def _roll(self):
        day = self._today()
        if day != self._day:
            self._day, self._data = day, {}

    def get(self, sym):
        with self._lock:
            self._roll()
            v = self._data.get(sym)
            if v is None:
                self.misses += 1
            else:
                self.hits += 1
            return v

    def peek(self, sym):
        with self._lock:
            self._roll()
            return self._data.get(sym)

    def put(self, sym, value):
        with self._lock:
            self._roll()
            self._data[sym] = value

    def update(self, sym, fn):
        with self._lock:
            self._roll()
            if sym not in self._data:
                return False
            fn(self._data[sym])
            return True

    def stats(self):
        with self._lock:
            self._roll()
            return {"size": len(self._data), "capacity": None, "hits": self.hits, "misses": self.misses,
                    "day": self._day.isoformat() if self._day else None, "policy": "day"}


def public_view(data):
    """Result dict without server-only keys (file paths, raw engine inputs)."""
    if not data:
        return data
    return {k: v for k, v in data.items() if not k.startswith("_") and k != "plot_file"}


class Job:
    __slots__ = ("sym", "status", "stage", "data", "error", "created", "started", "finished", "gen", "day")

    def __init__(self, sym, now, day=None):
        self.sym = sym
        self.status = "queued"
        self.stage = None
        self.data = {"ticker": sym, "stages": {s: "pending" for s in STAGES}}
        self.error = None
        self.created = now
        self.started = None
        self.finished = None
        self.gen = None
        self.day = day


class AIJob:
    __slots__ = ("sym", "entry", "status", "result", "error", "created", "started", "finished", "gen")

    def __init__(self, sym, entry, now):
        self.sym, self.entry = sym, entry
        self.status, self.result, self.error = "queued", None, None
        self.created, self.started, self.finished, self.gen = now, None, None, None


class JobManager:
    def __init__(self, settings, analyzer=None, store=None, llm=None, deadline=300.0, max_queue=20,
                 clock=time.monotonic, today=_dt.date.today, start=True):
        self.settings = settings
        self.analyzer = analyzer or TickerAnalyzer(settings, today=today)
        self.store = store if store is not None else self._default_store(settings, today)
        self.llm = llm
        self.deadline = deadline
        self.max_queue = max_queue
        self._clock = clock
        self._today = today
        self._cv = threading.Condition()
        self._queue = collections.deque()
        self._inflight = {}   # sym → Job (queued or running)
        self._running = None
        self._recent = {}     # sym → finished Job, so pollers see the final state (errors aren't stored)
        self.recent_ttl = 300.0
        self._gen = 0
        # AI overview: a second single worker so LLM latency never blocks quant lookups
        self._ai_queue = collections.deque()
        self._ai_inflight = {}
        self._recent_ai = {}
        self._ai_running = None
        self._ai_gen = 0
        # ≤2 generate attempts (+connect), optional model autoload (≤300 s), probe + slack
        self.ai_deadline = 2 * (float(getattr(settings, "llm_timeout", 600)) + 3) + \
            (300 if getattr(settings, "llm_autoload", False) else 0) + 30
        self._stop = False
        self._threads = []
        if start:
            self._spawn_worker()
            if self.llm is not None:
                self._spawn_ai_worker()
            wd = threading.Thread(target=self._watchdog, name="lynch-watchdog", daemon=True)
            wd.start()
            self._threads.append(wd)

    @staticmethod
    def _default_store(settings, today):
        # previews used to live here (pre-step-5 layout); they are now under plots/<day>/previews
        import shutil
        shutil.rmtree(os.path.join(settings.cache_dir, "plot_previews"), ignore_errors=True)
        try:
            from ui.cache import DailyLFUCache
        except ImportError:
            return DayStore(today)
        return DailyLFUCache(settings.cache_capacity, today=today,
                             plots_root=os.path.join(settings.cache_dir, "plots"))

    # ── public API (HTTP threads) ─────────────────────────────────────────────
    def request(self, sym, refresh=False, poll=False):
        """Snapshot for ``sym``; enqueues an analysis when nothing usable is cached.

        A *lookup* (``poll=False``) counts as a cache use (LFU frequency, hit/miss); follow-up
        polls of the same lookup (``poll=True``) only peek, so polling never inflates frequencies."""
        with self._cv:
            job = self._inflight.get(sym)
            if job is not None:  # dedup; refresh is ignored while in flight
                return self._snapshot(job)
            now, today = self._clock(), self._today()
            for k in [k for k, j in self._recent.items()
                      if now - j.finished > self.recent_ttl or j.day != today]:
                del self._recent[k]
            if not refresh:
                hit = self.lookup(sym) if poll else self.store.get(sym)
                if hit is not None:
                    recent = self._recent.get(sym)
                    if poll and recent is not None and recent.data is hit:
                        return self._snapshot(recent)  # the poller's own just-finished job
                    return self._cached_snapshot(hit)
                if sym in self._recent:  # errors / uncacheable results are only kept here
                    return self._snapshot(self._recent[sym])
                if poll:
                    # a poll must never start work: the lookup it belongs to expired (tab hidden > TTL,
                    # midnight rollover or eviction) — the client re-issues a real lookup
                    return {"ticker": sym, "status": "expired", "error": "lookup expired — searching again"}
            if len(self._queue) >= self.max_queue:
                return {"ticker": sym, "status": "busy", "retry_after": 10,
                        "error": f"analysis queue is full ({self.max_queue} tickers) — try again shortly"}
            job = Job(sym, self._clock(), self._today())
            self._inflight[sym] = job
            self._queue.append(job)
            self._cv.notify_all()
            return self._snapshot(job)

    def lookup(self, sym):
        """Stored result for today (no stats side-effects), or None."""
        peek = getattr(self.store, "peek", None)
        return peek(sym) if peek else None

    def plot_path(self, sym, preview=False):
        sym = sym.replace("*", "")
        data = self.lookup(sym)
        path = data.get("plot_file") if data else None
        if not path or not os.path.exists(path):
            # After a restart the store is empty but today's chart is still on disk
            path = os.path.join(self.analyzer.plot_dir(self._today()), f"{sym}_valuation.png")
            if not os.path.exists(path):
                return None
        if preview:
            from ui.imaging import jpeg_preview
            # previews live next to the day's charts so the LFU day-rollover sweep removes both
            return jpeg_preview(path, os.path.join(os.path.dirname(path), "previews"), 1100)
        return path

    def request_ai(self, sym, refresh=False):
        """AI overview snapshot for ``sym`` (needs today's quant result); enqueues generation."""
        if self.llm is None:
            return {"ticker": sym, "status": "unavailable", "error": "AI overview disabled"}
        with self._cv:
            job = self._ai_inflight.get(sym)
            if job is not None:
                return self._ai_snapshot(job)
            entry = self.lookup(sym)
            if entry is None:  # not stored (evicted, or uncacheable degraded result) → last finished job
                rj = self._recent.get(sym)
                entry = rj.data if rj is not None and rj.status == "done" else None
            if entry is None:
                return {"ticker": sym, "status": "error", "need_quant": True,
                        "error": "no analysis for this ticker today — run the analysis again"}
            if entry.get("status") != "done" or not (entry.get("_ai_inputs") or {}).get("row"):
                return {"ticker": sym, "status": "unavailable", "reason": "no_garp",
                        "error": "AI overview needs GARP valuation data"}
            if not refresh and entry.get("ai"):
                return dict(entry["ai"], ticker=sym, cached=True)
            now = self._clock()
            for k in [k for k, j in self._recent_ai.items() if now - j.finished > self.recent_ttl]:
                del self._recent_ai[k]
            if not refresh and sym in self._recent_ai:
                return self._ai_snapshot(self._recent_ai[sym])
        st = self.llm.status()  # network probe (cached 15 s) — outside the lock
        if not st.get("available"):
            return {"ticker": sym, "status": "unavailable", "reason": "offline",
                    "error": st.get("reason") or "local model unavailable"}
        with self._cv:
            job = self._ai_inflight.get(sym)
            if job is None:
                if len(self._ai_queue) >= self.max_queue:
                    return {"ticker": sym, "status": "busy", "retry_after": 15, "error": "AI queue is full"}
                job = AIJob(sym, entry, self._clock())
                self._ai_inflight[sym] = job
                self._ai_queue.append(job)
                self._cv.notify_all()
            return self._ai_snapshot(job)

    def _ai_snapshot(self, job):
        now = self._clock()
        pos = 0
        if job.status == "queued":
            try:
                pos = self._ai_queue.index(job) + 1 + (1 if self._ai_running else 0)
            except ValueError:
                pos = None
        out = {"ticker": job.sym, "status": job.status, "queue_position": pos, "cached": False,
               "elapsed_s": round((job.finished or now) - (job.started or job.created), 1), "error": job.error}
        if job.result:
            out.update({k: v for k, v in job.result.items() if k not in ("status", "error")})
        return out

    def _spawn_ai_worker(self):
        self._ai_gen += 1
        t = threading.Thread(target=self._ai_worker, args=(self._ai_gen,), name=f"lynch-ai-{self._ai_gen}",
                             daemon=True)
        t.start()
        self._threads.append(t)

    def _ai_worker(self, gen):
        from ui.llm import LLMError, LLMUnavailable, ticker_narrative
        while True:
            with self._cv:
                while not self._ai_queue and not self._stop and gen == self._ai_gen:
                    self._cv.wait(1.0)
                if self._stop or gen != self._ai_gen:
                    return
                job = self._ai_queue.popleft()
                job.status, job.gen, job.started = "running", gen, self._clock()
                self._ai_running = job
            try:
                result = ticker_narrative(self.llm, job.entry, self.settings.benchmark)
            except LLMUnavailable as e:
                result = {"status": "unavailable", "error": str(e)}
            except LLMError as e:
                result = {"status": "error", "error": str(e)}
            except Exception as e:  # never let the AI worker die
                result = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            with self._cv:
                if job.gen != self._ai_gen or job.status != "running":
                    continue  # timed out meanwhile; result discarded
                job.finished = self._clock()
                job.status = result.get("status", "error")
                job.error = result.get("error")
                job.result = result
                if job.status == "done":
                    ai = {k: v for k, v in result.items() if k != "error"}
                    job.entry["ai"] = ai
                    # Attach to the cached entry only if it is still the analysis the narrative was
                    # written from (a quant ↻ Refresh meanwhile replaces the entry → it gets a fresh AI).
                    self.store.update(job.sym, lambda e: e.__setitem__("ai", ai) if e is job.entry else None)
                self._finish_ai(job)

    def _finish_ai(self, job):
        self._ai_inflight.pop(job.sym, None)
        self._recent_ai[job.sym] = job
        if self._ai_running is job:
            self._ai_running = None

    def cache_stats(self):
        st = dict(self.store.stats())
        with self._cv:
            st["queue"] = len(self._queue)
            st["running"] = self._running.sym if self._running else None
            st["ai_queue"] = len(self._ai_queue)
            st["ai_running"] = self._ai_running.sym if self._ai_running else None
        return st

    def shutdown(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    # ── snapshots ────────────────────────────────────────────────────────────
    def _queue_position(self, job):
        if job.status != "queued":
            return 0
        try:
            return self._queue.index(job) + 1 + (1 if self._running else 0)
        except ValueError:
            return None

    def _snapshot(self, job):
        now = self._clock()
        return {
            "ticker": job.sym, "status": job.status, "stage": job.stage, "cached": False,
            "queue_position": self._queue_position(job),
            "elapsed_s": round((job.finished or now) - (job.started or job.created), 1),
            "error": job.error, "data": public_view(job.data),
        }

    @staticmethod
    def _cached_snapshot(data):
        return {"ticker": data.get("ticker"), "status": data.get("status", "done"), "stage": None,
                "cached": True, "queue_position": 0, "elapsed_s": 0.0,
                "error": data.get("reason") if data.get("status") == "error" else None,
                "data": public_view(data)}

    # ── worker ───────────────────────────────────────────────────────────────
    def _spawn_worker(self):
        self._gen += 1
        t = threading.Thread(target=self._worker, args=(self._gen,), name=f"lynch-worker-{self._gen}",
                             daemon=True)
        t.start()
        self._threads.append(t)

    def _worker(self, gen):
        while True:
            with self._cv:
                while not self._queue and not self._stop and gen == self._gen:
                    self._cv.wait(1.0)
                if self._stop or gen != self._gen:
                    return
                job = self._queue.popleft()
                job.status, job.gen, job.started = "running", gen, self._clock()
                job.day = self._today()
                self._running = job
            self._run_job(job, gen)

    def _run_job(self, job, gen):
        def live():
            return job.gen == self._gen and job.status == "running"

        def on_stage(name, state, data):
            with self._cv:
                if live():
                    job.stage = name
                    job.data = copy.deepcopy({k: v for k, v in data.items() if k != "_ai_inputs"})

        try:
            result = self.analyzer.run(job.sym, on_stage=on_stage, cancelled=lambda: not live())
            err = None
        except Exception as e:
            result, err = None, f"{type(e).__name__}: {e}"
        with self._cv:
            if not live():  # timed out: the watchdog already finalised this job
                return
            job.finished = self._clock()
            if result is None:
                job.status, job.error = "error", err or "analysis failed"
            else:
                result.setdefault("generated_at", time.time())
                job.data = result
                job.status = result.get("status") if result.get("status") in FINAL else "error"
                job.error = result.get("reason") if job.status == "error" else None
                if self._today() != job.day:
                    # started yesterday: its chart lives in a plot dir the day rollover just swept
                    result["cacheable"] = False
                if job.status in ("done", "nodata") and result.get("cacheable", True):  # errors never cached
                    self.store.put(job.sym, result)
            job.stage = None
            self._finish(job)

    def _finish(self, job):
        """Lock held: move a finalised job from in-flight to the short-lived recent map."""
        self._inflight.pop(job.sym, None)
        self._recent[job.sym] = job
        if self._running is job:
            self._running = None

    def _watchdog(self):
        while True:
            with self._cv:
                if self._stop:
                    return
                job = self._running
                if job is not None and job.started is not None and \
                        self._clock() - job.started > self.deadline:
                    self._expire(job)
                aj = self._ai_running
                if aj is not None and aj.started is not None and self._clock() - aj.started > self.ai_deadline:
                    print(f"⏱️  {aj.sym}: AI overview exceeded {int(self.ai_deadline)}s — abandoning ai worker "
                          f"{aj.gen}, starting a fresh one")
                    aj.status, aj.finished = "error", self._clock()
                    aj.error = "local model timed out; LM Studio may still be busy — retry in a minute"
                    self._finish_ai(aj)
                    self._spawn_ai_worker()
                self._cv.wait(1.0)

    def _expire(self, job):
        """Called with the lock held: fail the hung job and hand the queue to a new worker.

        The hung thread cannot be killed; it stops at its next stage boundary (``cancelled``) and
        its result is discarded. Until then a refresh of the same symbol may overlap with it."""
        print(f"⏱️  {job.sym}: analysis exceeded {int(self.deadline)}s — abandoning worker "
              f"{job.gen}, starting a fresh one")
        job.status, job.error, job.finished = "error", f"timed out after {int(self.deadline)}s", self._clock()
        job.stage = None
        self._finish(job)
        self._spawn_worker()
        self._cv.notify_all()
