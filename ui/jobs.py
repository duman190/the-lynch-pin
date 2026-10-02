"""Job model for ticker lookups: ``workers`` analysis workers, a bounded FIFO queue, a per-job
watchdog and a pluggable result store.

Workers: one by default (the server's choice with the AI overview on). With ``processes=True`` each worker
runs its analyses in its own child process (ui/workers.py), so several users' tickers are analysed
at once: pyplot, the engine's globals and the GIL are per process. In-process workers (threads) are
for tests and single-worker setups; the chart stage is serialised there (ui/analysis.py). HTTP
threads only enqueue, poll snapshots and read the store.

Watchdog: an engine call can hang (yfinance retries, EDGAR, backtest downloads). When a running job
exceeds ``deadline`` seconds it is marked ``error: timed out``, its worker is retired (its token
leaves the live set) and a fresh worker takes its place in the pool; the retired worker's late
results are discarded. A worker process is killed; a worker thread cannot be, so it stops at its
next stage boundary.

Yahoo rate limit (a circuit breaker): when an analysis reports ``rate_limited`` (ui/yahoo.py counts
Yahoo's 429s), no new job starts for ``backoff`` seconds. A lookup that got nothing back is
re-queued at the front instead of failing (up to ``RATE_LIMIT_RETRIES`` times); a partial one is
returned but never cached. After the pause a single *probe* job runs; the others wait until it
comes back clean. A throttled probe doubles the pause (30 s → 10 min). Queued snapshots carry a note
and ``retry_after`` so the browser can say why it is waiting.
"""
import collections
import copy
import datetime as _dt
import os
import threading
import time

from ui.analysis import STAGES, TickerAnalyzer

FINAL = ("done", "nodata", "error")
RATE_LIMIT_BACKOFF = (30.0, 600.0)  # first pause, longest pause (doubles per throttled probe)
RATE_LIMIT_RETRIES = 2              # re-queues of a lookup that Yahoo throttled before it gives up


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
    __slots__ = ("sym", "status", "stage", "data", "error", "created", "started", "finished", "gen", "day",
                 "retries", "probe")

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
        self.retries = 0     # times re-queued after Yahoo throttled it
        self.probe = False   # the one job allowed to test whether Yahoo is back


class AIStream:
    """Live state of one AI generation: written by the AI worker (it is the ``sink`` handed to
    ``ui.llm.ticker_narrative``), read by SSE handlers and JSON pollers.

    Reasoning is append-only (readers track an index); the answer is re-parsed into its three
    sections on read, so a label that arrives mid-stream moves text between sections cleanly.
    """
    REASONING_TAIL = 20000  # chars of reasoning replayed to a (re)connecting client

    def __init__(self):
        self.cv = threading.Condition()
        self.version = 0
        self.attempt = 0
        self.note = None
        self.phase = "queued"  # queued | connecting | thinking | writing | done | error | unavailable
        self.reasoning = []
        self.content = ""
        self.metrics = {}
        self.final = None
        self.started_at = None
        self._parsed = (-1, None)

    def _bump(self):
        self.version += 1
        self.cv.notify_all()

    # sink API ─────────────────────────────────────────────────────────────
    def begin(self, attempt, note=None):
        with self.cv:
            self.attempt, self.note = attempt, note
            self.reasoning, self.content, self.metrics = [], "", {}
            self.phase = "connecting"
            self.started_at = time.monotonic()
            self._bump()

    def rewind(self, moved):
        """The answer typed so far was reasoning (prefilled <think>): move it to the reasoning box."""
        with self.cv:
            if moved:
                self.reasoning.append(moved)
            self.content = ""
            self.phase = "thinking"
            self._bump()

    def delta(self, reasoning, content, metrics):
        with self.cv:
            if reasoning:
                self.reasoning.append(reasoning)
            if content:
                self.content += content
            self.phase = "writing" if self.content.strip() else ("thinking" if self.reasoning else "connecting")
            self.metrics = metrics
            self._bump()

    def finish(self, payload):
        with self.cv:
            if self.final is None:
                self.final = payload
                self.phase = payload.get("status", "error")
                self._bump()

    # readers (hold cv) ────────────────────────────────────────────────────
    def sections(self):
        from ui.llm import parse_sections
        if self._parsed[0] != self.version:
            self._parsed = (self.version, parse_sections(self.content, partial=True) if self.content else None)
        return self._parsed[1]

    def live(self):
        with self.cv:
            return {"phase": self.phase, "attempt": self.attempt, "note": self.note, "sections": self.sections(),
                    "metrics": dict(self.metrics), "reasoning_tokens": self.metrics.get("reasoning_tokens", 0)}


class AIJob:
    __slots__ = ("sym", "entry", "status", "result", "error", "created", "started", "finished", "gen", "stream")

    def __init__(self, sym, entry, now):
        self.sym, self.entry = sym, entry
        self.status, self.result, self.error = "queued", None, None
        self.created, self.started, self.finished, self.gen = now, None, None, None
        self.stream = AIStream()


class JobManager:
    def __init__(self, settings, analyzer=None, store=None, llm=None, deadline=300.0, max_queue=20,
                 clock=time.monotonic, today=_dt.date.today, start=True, workers=1, processes=False,
                 backends_spec=None, allow_refresh=True):
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
        self._running = {}    # worker token → its running Job
        self._recent = {}     # sym → finished Job, so pollers see the final state (errors aren't stored)
        self.recent_ttl = 300.0
        # ↻ Refresh re-runs a cached ticker. Off (--no-ai): a cached ticker stays until the cache clears at
        # midnight (or is evicted), so lookups never re-spend Yahoo calls on what is already known.
        self.allow_refresh = allow_refresh
        self.workers = max(1, int(workers))
        self.processes = processes          # analyse in child processes (ui/workers.py)
        self._backends_spec = backends_spec  # "module:function" building the child's backends (tests)
        self._live = set()    # tokens of the workers that own the queue; a timed-out worker's is dropped
        self._token = 0
        # Yahoo rate-limit circuit breaker (see module docstring)
        self._limited = False
        self._backoff = 0.0
        self._cooldown_until = 0.0
        self._probing = False
        self.rate_limit_hits = 0
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
            for _ in range(self.workers):
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
        refresh = refresh and self.allow_refresh
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

    def deep_dive(self, sym):
        """Deep Dive Prompt for today's analysis of ``sym`` (with the AI overview once it is written)."""
        from ui.deepdive import build_deep_dive
        with self._cv:
            entry = self.lookup(sym)
            if entry is None:
                rj = self._recent.get(sym)
                entry = rj.data if rj is not None and rj.status in ("done", "nodata") else None
            if entry is None or entry.get("status") not in ("done", "nodata"):
                return None
            ai = entry.get("ai")
            if ai is not None:
                state = "included"
            elif self.llm is None:
                state = "disabled"
            elif sym in self._ai_inflight:
                state = "pending"
            else:
                done = self._recent_ai.get(sym)
                if done is not None and done.status == "done" and done.entry is entry and done.result:
                    ai, state = done.result, "included"
                else:
                    state = "unavailable"
        prompt = build_deep_dive(entry, ai, state)
        return {"ticker": sym, "prompt": prompt, "ai": state, "words": len(prompt.split()), "chars": len(prompt)}

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
        elif job.status in ("queued", "running"):
            out["live"] = job.stream.live()  # partial answer + metrics for clients without SSE
        return out

    # ── live AI stream (Server-Sent Events) ──────────────────────────────────
    def ai_events(self, sym, heartbeat=10.0, coalesce=0.08):
        """Event generator for the AI job of ``sym`` (in flight or just finished), or None.

        Yields ``(event, data)``: ``snapshot`` first (full state, so reconnects are seamless), then
        ``delta`` (new reasoning text, re-parsed sections when the answer changed, metrics),
        ``reset`` (a retry started), ``state`` (queue position / phase ticks), ``ping`` (keep-alive)
        and finally one of ``done`` / ``error`` / ``unavailable`` carrying the JSON snapshot.
        """
        with self._cv:
            job = self._ai_inflight.get(sym) or self._recent_ai.get(sym)
        if job is None:
            return None
        return self._ai_event_loop(job, heartbeat, coalesce)

    def _ai_event_loop(self, job, heartbeat, coalesce):
        st = job.stream
        clock = time.monotonic
        deadline = clock() + self.ai_deadline + 60  # never hold an HTTP thread forever
        with st.cv:
            seen, attempt, r_idx = st.version, st.attempt, len(st.reasoning)
            reasoning = "".join(st.reasoning)[-AIStream.REASONING_TAIL:]
            content_seen = st.content
            snap = {"phase": st.phase, "attempt": st.attempt, "note": st.note, "reasoning": reasoning,
                    "sections": st.sections(), "metrics": dict(st.metrics)}
            final = st.final
        with self._cv:
            snap.update(queue_position=self._ai_snapshot(job).get("queue_position"), ticker=job.sym)
        yield "snapshot", snap
        if final is not None:
            yield final.get("status", "error"), final
            return
        last_out, last_state = clock(), None
        while clock() < deadline and not self._stop:
            with st.cv:
                if st.version == seen and st.final is None:
                    st.cv.wait(1.0)
            time.sleep(coalesce)  # let a few tokens accumulate: ~12 events/s instead of one per token
            with st.cv:
                changed = st.version != seen
                seen = st.version
                final = st.final
                events = []
                if st.attempt != attempt:
                    if attempt >= 1:  # a real retry: the client must drop attempt 1's text
                        events.append(("reset", {"attempt": st.attempt, "note": st.note}))
                    attempt, r_idx, content_seen = st.attempt, 0, ""  # (0 → 1 is just the first start)
                if changed and final is None:
                    delta = {"phase": st.phase, "note": st.note, "metrics": dict(st.metrics),
                             "reasoning": "".join(st.reasoning[r_idx:])}
                    r_idx = len(st.reasoning)
                    if st.content != content_seen:
                        content_seen = st.content
                        delta["sections"] = st.sections()
                    events.append(("delta", delta))
            for ev in events:
                yield ev
                last_out = clock()
            if final is not None:
                yield final.get("status", "error"), final
                return
            if not events:
                with self._cv:
                    snap = self._ai_snapshot(job)
                state = (job.status, snap.get("queue_position"))
                if state != last_state or job.status == "running":
                    last_state = state
                    started = st.started_at  # same clock as the meter: time since this attempt began
                    elapsed = round(clock() - started, 1) if started else snap.get("elapsed_s")
                    yield "state", {"status": job.status, "phase": st.phase, "queue_position": state[1],
                                    "elapsed_s": elapsed}
                    last_out = clock()
                elif clock() - last_out >= heartbeat:
                    yield "ping", None
                    last_out = clock()
        yield "error", {"ticker": job.sym, "status": "error", "error": "AI stream closed — reload to continue"}

    def _spawn_ai_worker(self):
        self._ai_gen += 1
        t = threading.Thread(target=self._ai_worker, args=(self._ai_gen,), name=f"lynch-ai-{self._ai_gen}",
                             daemon=True)
        t.start()
        self._threads.append(t)

    def _ai_worker(self, gen):
        from ui.llm import LLMCancelled, LLMError, LLMUnavailable, ticker_narrative
        while True:
            with self._cv:
                while not self._ai_queue and not self._stop and gen == self._ai_gen:
                    self._cv.wait(1.0)
                if self._stop or gen != self._ai_gen:
                    return
                job = self._ai_queue.popleft()
                job.status, job.gen, job.started = "running", gen, self._clock()
                self._ai_running = job
            def cancelled(job=job, gen=gen):
                return self._stop or gen != self._ai_gen or job.status != "running"

            try:
                result = ticker_narrative(self.llm, job.entry, self.settings.benchmark, sink=job.stream,
                                          cancelled=cancelled)
            except LLMCancelled:
                continue  # watchdog / shutdown already finalised the job
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
        """Lock held: retire a finalised AI job and publish its final snapshot to stream readers."""
        self._ai_inflight.pop(job.sym, None)
        self._recent_ai[job.sym] = job
        if self._ai_running is job:
            self._ai_running = None
        job.stream.finish(self._ai_snapshot(job))

    def cache_stats(self):
        st = dict(self.store.stats())
        with self._cv:
            st["queue"] = len(self._queue)
            st["running"] = ", ".join(sorted(j.sym for j in self._running.values())) or None
            st["workers"] = self.workers
            st["rate_limited"] = self._limited
            st["rate_limit_retry_s"] = self._retry_after() if self._limited else 0
            st["rate_limit_hits"] = self.rate_limit_hits
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
        snap = {
            "ticker": job.sym, "status": job.status, "stage": job.stage, "cached": False,
            "queue_position": self._queue_position(job),
            "elapsed_s": round((job.finished or now) - (job.started or job.created), 1),
            "error": job.error, "data": public_view(job.data),
        }
        if job.status == "queued" and self._limited:
            wait = self._retry_after()
            snap["retry_after"] = wait
            snap["note"] = (f"Yahoo Finance is rate-limiting this server — retrying in {wait} s" if wait
                            else "Yahoo Finance was rate-limiting this server — checking whether it is back")
        return snap

    # ── Yahoo rate-limit circuit breaker (lock held) ─────────────────────────
    def _retry_after(self):
        return max(0, int(round(self._cooldown_until - self._clock())))

    def _may_start(self):
        """A queued job may start now: always, unless Yahoo is throttling us (then one probe at a time,
        after the pause)."""
        if not self._queue:
            return False
        return not self._limited or (not self._probing and self._clock() >= self._cooldown_until)

    def _note_outcome(self, job, throttled):
        if throttled:
            self.rate_limit_hits += 1
            if not self._limited:
                self._limited, self._backoff = True, RATE_LIMIT_BACKOFF[0]
                print(f"⏳ Yahoo Finance rate limit ({job.sym}): pausing new lookups for {int(self._backoff)} s",
                      flush=True)
            elif job.probe:
                self._backoff = min(self._backoff * 2, RATE_LIMIT_BACKOFF[1])
                print(f"⏳ Yahoo Finance still rate-limiting ({job.sym}): pausing {int(self._backoff)} s", flush=True)
            self._cooldown_until = max(self._cooldown_until, self._clock() + self._backoff)
        elif job.probe and self._limited:
            self._limited, self._backoff = False, 0.0
            print(f"✅ Yahoo Finance answering again ({job.sym}): resuming all workers", flush=True)
        if job.probe:
            self._probing = job.probe = False

    @staticmethod
    def _cached_snapshot(data):
        return {"ticker": data.get("ticker"), "status": data.get("status", "done"), "stage": None,
                "cached": True, "queue_position": 0, "elapsed_s": 0.0,
                "error": data.get("reason") if data.get("status") == "error" else None,
                "data": public_view(data)}

    # ── worker ───────────────────────────────────────────────────────────────
    def _spawn_worker(self):
        self._token += 1
        token = self._token
        self._live.add(token)
        t = threading.Thread(target=self._worker, args=(token,), name=f"lynch-worker-{token}", daemon=True)
        t.start()
        self._threads.append(t)

    def _new_runner(self):
        from ui.workers import ProcessRunner
        return ProcessRunner(self.settings, self._backends_spec)

    def _worker(self, token):
        runner = self._new_runner() if self.processes else None  # warm before the first job arrives
        try:
            while True:
                with self._cv:
                    while not self._may_start() and not self._stop and token in self._live:
                        self._cv.wait(1.0)
                    if self._stop or token not in self._live:
                        return
                    job = self._queue.popleft()
                    if self._limited:
                        job.probe = self._probing = True
                    job.status, job.gen, job.started = "running", token, self._clock()
                    job.day = self._today()
                    self._running[token] = job
                if runner is not None and not runner.alive():  # crashed during the previous job
                    runner.close()
                    runner = self._new_runner()
                self._run_job(job, runner)
        finally:
            if runner is not None:
                runner.close()

    def _run_job(self, job, runner=None):
        def live():
            return job.gen in self._live and job.status == "running"

        def on_stage(name, state, data):
            with self._cv:
                if live():
                    job.stage = name
                    job.data = copy.deepcopy({k: v for k, v in data.items() if k != "_ai_inputs"})

        run = runner.run if runner is not None else self.analyzer.run
        try:
            result = run(job.sym, on_stage=on_stage, cancelled=lambda: not live())
            err = None
        except Exception as e:
            result, err = None, f"{type(e).__name__}: {e}"
        with self._cv:
            if not live():  # timed out: the watchdog already finalised this job
                return
            throttled = bool(result and result.get("rate_limited"))
            self._note_outcome(job, throttled)
            if throttled and result.get("status") == "error" and job.retries < RATE_LIMIT_RETRIES:
                # nothing came back: wait for Yahoo at the front of the queue instead of failing
                job.retries += 1
                job.status, job.stage, job.started, job.error = "queued", None, None, None
                job.data = {"ticker": job.sym, "stages": {s: "pending" for s in STAGES}}
                if self._running.get(job.gen) is job:
                    del self._running[job.gen]
                self._queue.appendleft(job)
                self._cv.notify_all()
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
                if throttled and job.status == "error":
                    job.error = "Yahoo Finance is rate-limiting this server — try again in a few minutes"
            job.stage = None
            self._finish(job)
            self._cv.notify_all()  # a clean probe reopens the queue for every worker

    def _finish(self, job):
        """Lock held: move a finalised job from in-flight to the short-lived recent map."""
        self._inflight.pop(job.sym, None)
        self._recent[job.sym] = job
        if self._running.get(job.gen) is job:
            del self._running[job.gen]

    def _watchdog(self):
        while True:
            with self._cv:
                if self._stop:
                    return
                for job in list(self._running.values()):
                    if job.started is not None and self._clock() - job.started > self.deadline:
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
        """Called with the lock held: fail the hung job and replace its worker.

        A worker process is killed by its thread within a poll interval. A worker thread cannot be
        killed; it stops at its next stage boundary (``cancelled``) and its result is discarded.
        Until then a refresh of the same symbol may overlap with it."""
        print(f"⏱️  {job.sym}: analysis exceeded {int(self.deadline)}s — abandoning worker "
              f"{job.gen}, starting a fresh one")
        job.status, job.error, job.finished = "error", f"timed out after {int(self.deadline)}s", self._clock()
        job.stage = None
        if job.probe:  # a hung probe proves nothing: let the next job probe
            self._probing = job.probe = False
        self._live.discard(job.gen)
        self._finish(job)
        self._spawn_worker()
        self._cv.notify_all()
