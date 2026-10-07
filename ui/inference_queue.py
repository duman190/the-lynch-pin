"""Inference queue for the portal's AI overview: decides who writes each new overview, from the last minute's load.

Each new AI overview request (a ticker without one today) is admitted against two watermarks on the AI request
rate over the last minute:

  up to the low watermark    → the local model. ``low`` (--llm-rpm, default 24: Qwen3.6-35B-A3B on Splash with 4
                               streams, measured with ui/tests/inference_bench.py; 4 for the 27B); at most ``low``
                               are waiting or being written at once, so a model slower than its setting never builds
                               more than about a minute of backlog.
  low … high watermark       → offloaded to Gemini (ui/gemini.py): ``gemini_rpm`` a minute (Google's free-tier
                               limit, 15) and ``gemini_rpd`` a Pacific day. high = low + gemini_rpm.
  above the high watermark   → paused: no AI for this request. The visitor gets the rule-based Quick overview and a
                               ↻ Retry AI button; ``retry_after`` says when a slot frees up.

A local model that is offline counts as full, so its share goes to Gemini; with neither, the Quick overview.
The nightly pre-cache (``warm``) only ever uses the local model and is never paused by the window (it runs one
ticker at a time); its requests still count, so visitors see the load.
"""
import collections
import math
import threading
import time


class InferenceQueue:
    WINDOW = 60.0
    BACKLOG_WAIT = 10  # retry hint (s) when the local model is behind rather than its minute full

    def __init__(self, low, budget=None, clock=time.monotonic):
        self.low = max(0, int(low))
        self.budget = budget  # ui.gemini.GeminiBudget or None (local only)
        self._clock = clock
        self._lock = threading.Lock()
        self._local = collections.deque()   # local admissions in the last minute
        self._paused = collections.deque()  # requests turned away in the last minute
        self.counts = collections.Counter()  # since start: local / gemini / paused

    @property
    def high(self):
        return self.low + (self.budget.rpm if self.budget is not None else 0)

    def _prune(self, now):
        for q in (self._local, self._paused):
            while q and q[0] <= now - self.WINDOW:
                q.popleft()

    def admit(self, symbol, local_ok, local_pending, warm=False):
        """Route one new AI overview → ``(route, retry_after)``: route is "local", "gemini" or None (paused).

        ``local_ok``: the local model answers its status probe. ``local_pending``: local overviews queued or being
        written right now."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if local_ok and (warm or (len(self._local) < self.low and local_pending < self.low)):
                self._local.append(now)
                self.counts["local"] += 1
                return "local", 0
            if not warm and self.budget is not None and self.budget.try_acquire(symbol):
                self.counts["gemini"] += 1
                return "gemini", 0
            if not warm:
                self._paused.append(now)
                self.counts["paused"] += 1
            return None, self._retry_after(now, local_ok)

    def _retry_after(self, now, local_ok):
        """Lock held: seconds until a new request would likely be admitted."""
        waits = []
        if local_ok and self.low:
            full = len(self._local) >= self.low
            waits.append(self._local[0] + self.WINDOW - now if full else self.BACKLOG_WAIT)
        if self.budget is not None:
            waits.append(self.budget.retry_after())
        return max(1, int(math.ceil(min(waits)))) if waits else 60

    def status(self):
        """For /api/health: the watermarks and the last minute's traffic."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            out = {"low": self.low, "high": self.high, "local_last_min": len(self._local),
                   "paused_last_min": len(self._paused), "since_start": dict(self.counts)}
        if self.budget is not None:
            out["gemini"] = self.budget.status()
        return out
