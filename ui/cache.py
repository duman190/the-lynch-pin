"""Daily LFU cache for analysed tickers (step 5).

Holds up to ``capacity`` (portal default 250) ticker results for the current day so re-typing a symbol
skips the quant pipeline, the chart render and the AI overview. Eviction is least-frequently-used
with least-recently-used as the tie-break, all O(1):

    key  → [value, freq]
    freq → OrderedDict(key → None)   (insertion order = recency within that frequency)
    min_freq tracks the bucket to evict from

The cache belongs to one calendar day: the first access on a new day clears every entry, resets the
per-day counters and deletes previous days' chart directories under ``<cache_dir>/plots/``.
"""
import collections
import datetime as _dt
import os
import shutil
import threading


class DailyLFUCache:
    def __init__(self, capacity=250, today=_dt.date.today, plots_root=None):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = int(capacity)
        self._today = today
        self._plots_root = plots_root
        self._lock = threading.RLock()
        self._day = None
        self._reset_entries()
        self._totals = {"hits": 0, "misses": 0, "evictions": 0, "days": 0}
        with self._lock:
            self._roll()  # start-up: bind to today and sweep charts left over from previous days

    # ── internals (lock held) ────────────────────────────────────────────────
    def _reset_entries(self):
        self._items = {}
        self._buckets = collections.defaultdict(collections.OrderedDict)
        self._min_freq = 0
        self.hits = self.misses = self.evictions = 0

    def _roll(self):
        day = self._today()
        if day == self._day:
            return
        first = self._day is None
        self._day = day
        self._reset_entries()
        if not first:
            self._totals["days"] += 1
        self._sweep_old_plots(day)

    def _sweep_old_plots(self, day):
        root = self._plots_root
        if not root or not os.path.isdir(root):
            return
        keep = day.isoformat()
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if name != keep and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)

    def _touch(self, key):
        """Move ``key`` one frequency bucket up (it becomes the most recent there)."""
        entry = self._items[key]
        f = entry[1]
        bucket = self._buckets[f]
        del bucket[key]
        if not bucket:
            del self._buckets[f]
            if self._min_freq == f:
                self._min_freq = f + 1
        entry[1] = f + 1
        self._buckets[f + 1][key] = None

    def _evict(self):
        bucket = self._buckets[self._min_freq]
        victim, _ = bucket.popitem(last=False)  # least recent within the least frequent
        if not bucket:
            del self._buckets[self._min_freq]
        del self._items[victim]
        # _min_freq may now point at a deleted bucket; the only caller (put) resets it to 1
        self.evictions += 1
        self._totals["evictions"] += 1
        return victim

    # ── public API ───────────────────────────────────────────────────────────
    def get(self, key):
        """Value for ``key`` (counts as a use: frequency +1, hit/miss stats) or None."""
        with self._lock:
            self._roll()
            entry = self._items.get(key)
            if entry is None:
                self.misses += 1
                self._totals["misses"] += 1
                return None
            self._touch(key)
            self.hits += 1
            self._totals["hits"] += 1
            return entry[0]

    def peek(self, key):
        """Value for ``key`` without touching frequency or stats."""
        with self._lock:
            self._roll()
            entry = self._items.get(key)
            return entry[0] if entry else None

    def put(self, key, value):
        """Insert or replace. Replacing (a ↻ Refresh re-analysis) counts as a use (freq + 1); inserting at capacity
        evicts the least-frequently-used entry (LRU among ties). Returns the evicted key or None."""
        with self._lock:
            self._roll()
            if key in self._items:
                self._items[key][0] = value
                self._touch(key)
                return None
            victim = self._evict() if len(self._items) >= self.capacity else None
            self._items[key] = [value, 1]
            self._buckets[1][key] = None
            self._min_freq = 1
            return victim

    def update(self, key, fn):
        """Atomically apply ``fn(value)`` to a cached entry without changing its frequency.
        Returns False when the key is absent (evicted or rolled over)."""
        with self._lock:
            self._roll()
            entry = self._items.get(key)
            if entry is None:
                return False
            fn(entry[0])
            return True

    def __len__(self):
        with self._lock:
            self._roll()
            return len(self._items)

    def __contains__(self, key):
        with self._lock:
            self._roll()
            return key in self._items

    def frequency(self, key):
        with self._lock:
            self._roll()
            entry = self._items.get(key)
            return entry[1] if entry else 0

    def stats(self, top=5):
        with self._lock:
            self._roll()
            ranked = sorted(self._items.items(), key=lambda kv: (-kv[1][1], kv[0]))[:top]
            return {"size": len(self._items), "capacity": self.capacity, "hits": self.hits, "misses": self.misses,
                    "evictions": self.evictions, "day": self._day.isoformat() if self._day else None,
                    "policy": "lfu", "top": [[k, e[1]] for k, e in ranked], "total": dict(self._totals)}
