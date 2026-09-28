"""Step 5: daily LFU cache (250 tickers/day) and its integration with the job manager."""
import datetime as dt
import os
import threading
import time

import pytest

from ui.analysis import TickerAnalyzer
from ui.cache import DailyLFUCache
from ui.config import Settings
from ui.jobs import JobManager
from ui.tests import fakes


class Day:
    def __init__(self):
        self.d = dt.date(2026, 9, 27)

    def __call__(self):
        return self.d


def test_capacity_and_lfu_eviction_with_lru_tiebreak():
    c = DailyLFUCache(3)
    for k in "ABC":
        c.put(k, k.lower())
    c.get("A")
    c.get("A")
    c.get("B")  # freqs: A=3, B=2, C=1
    assert c.put("D", "d") == "C"  # least frequent
    assert c.put("E", "e") == "D"  # D (freq 1) is the only min-freq entry
    c.get("E")  # E=2 ties with B=2; B is older within freq 2
    assert c.put("F", "f") == "B"
    assert len(c) == 3 and set(c._items) == {"A", "E", "F"}


def test_lru_among_equal_frequency():
    c = DailyLFUCache(2)
    c.put("X", 1)
    c.put("Y", 2)
    c.get("X")
    c.get("Y")  # both freq 2; X was touched earlier → X is least recent
    assert c.put("Z", 3) == "X"
    assert "Y" in c and "Z" in c


def test_put_existing_keeps_frequency_and_never_evicts():
    c = DailyLFUCache(2)
    c.put("A", 1)
    c.put("B", 2)
    assert c.put("A", 10) is None and c.frequency("A") == 2 and len(c) == 2
    assert c.peek("A") == 10


def test_peek_and_update_do_not_touch_frequency_or_stats():
    c = DailyLFUCache(2)
    c.put("A", {"v": 1})
    before = c.stats()
    assert c.peek("A") == {"v": 1}
    assert c.update("A", lambda e: e.__setitem__("ai", "x")) is True
    assert c.peek("A")["ai"] == "x"
    assert c.update("NOPE", lambda e: None) is False
    after = c.stats()
    assert c.frequency("A") == 1 and (after["hits"], after["misses"]) == (before["hits"], before["misses"])


def test_hit_miss_stats_and_top():
    c = DailyLFUCache(10)
    assert c.get("MSFT") is None
    for i, k in enumerate(["MSFT", "AAPL", "NVDA", "AMD", "META", "GOOG"]):
        c.put(k, i)
    for _ in range(3):
        c.get("NVDA")
    c.get("AAPL")
    st = c.stats()
    assert (st["hits"], st["misses"], st["size"], st["capacity"], st["policy"]) == (4, 1, 6, 10, "lfu")
    assert st["top"][0] == ["NVDA", 4] and st["top"][1] == ["AAPL", 2] and len(st["top"]) == 5
    assert [k for k, _ in st["top"][2:]] == ["AMD", "GOOG", "META"]  # deterministic tie order


def test_default_capacity_is_250_tickers():
    s = Settings()
    assert s.cache_capacity == 250
    c = DailyLFUCache(s.cache_capacity)
    for i in range(300):
        c.put(f"T{i}", i)
    assert len(c) == 250 and c.stats()["evictions"] == 50
    assert "T299" in c and "T0" not in c


def test_rollover_clears_entries_counters_and_old_plot_dirs(tmp_path):
    day = Day()
    root = tmp_path / "plots"
    for d in ("2026-09-25", "2026-09-26", "2026-09-27"):
        (root / d / "previews").mkdir(parents=True)
        (root / d / "MSFT_valuation.png").write_bytes(b"x")
    c = DailyLFUCache(5, today=day, plots_root=str(root))
    assert sorted(os.listdir(root)) == ["2026-09-27"]  # first access sweeps older days only
    c.put("MSFT", 1)
    c.get("MSFT")
    c.get("NONE")
    day.d = dt.date(2026, 9, 28)
    (root / "2026-09-28").mkdir()
    assert c.get("MSFT") is None
    st = c.stats()
    assert st["size"] == 0 and st["day"] == "2026-09-28" and (st["hits"], st["misses"]) == (0, 1)
    assert st["total"]["hits"] == 1 and st["total"]["days"] == 1
    assert sorted(os.listdir(root)) == ["2026-09-28"]


def test_thread_safety_smoke():
    c = DailyLFUCache(50)
    errors = []

    def worker(n):
        try:
            for i in range(2000):
                k = f"K{(n * 7 + i) % 120}"
                if c.get(k) is None:
                    c.put(k, i)
                if i % 5 == 0:
                    c.update(k, lambda v: None)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(c) <= 50
    st = c.stats()
    assert st["size"] == len(c._items) == sum(len(b) for b in c._buckets.values())


def test_invalid_capacity():
    with pytest.raises(ValueError):
        DailyLFUCache(0)


# ── integration with the job manager ────────────────────────────────────────
def wait_final(jm, sym, **kw):
    t0 = time.time()
    while time.time() - t0 < 10:
        s = jm.request(sym, poll=True, **kw)
        if s["status"] in ("done", "nodata", "error"):
            return s
        time.sleep(0.02)
    raise AssertionError(s)


@pytest.fixture
def jm(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    s.cache_capacity = 2
    for sym in ("AAPL", "NVDA"):
        fakes.FakeEngine.infos[sym] = fakes.MSFT_INFO
        fakes.FakeEngine.rows[sym] = dict(fakes.MSFT_ROW, Ticker=sym)
    fakes.FakeEngine.calls = []
    m = JobManager(s, analyzer=TickerAnalyzer(s, backends=fakes.backends()))
    yield m
    m.shutdown()
    for sym in ("AAPL", "NVDA"):
        fakes.FakeEngine.infos.pop(sym, None)
        fakes.FakeEngine.rows.pop(sym, None)


def test_job_manager_uses_lfu_by_default(jm):
    assert isinstance(jm.store, DailyLFUCache) and jm.store.capacity == 2
    assert jm.store._plots_root.endswith(os.path.join("cache", "plots"))


def test_repeat_lookup_skips_quant_plot_and_polls_do_not_count(jm):
    jm.request("MSFT")
    assert wait_final(jm, "MSFT")["status"] == "done"
    jm._recent.clear()
    freq_after_job = jm.store.frequency("MSFT")
    for _ in range(5):
        jm.request("MSFT", poll=True)
    assert jm.store.frequency("MSFT") == freq_after_job  # polls only peek
    snap = jm.request("MSFT")  # a genuine re-lookup
    assert snap["cached"] is True and jm.store.frequency("MSFT") == freq_after_job + 1
    assert fakes.FakeEngine.calls.count("MSFT") == 1  # quant pipeline ran once
    assert jm.cache_stats()["hits"] == 1


def test_eviction_forces_recompute_and_lfu_keeps_popular(jm):
    for sym in ("MSFT", "AAPL"):
        jm.request(sym)
        wait_final(jm, sym)
    jm._recent.clear()
    jm.request("MSFT")  # MSFT now more popular than AAPL
    jm.request("NVDA")
    wait_final(jm, "NVDA")
    assert "MSFT" in jm.store and "NVDA" in jm.store and "AAPL" not in jm.store
    jm._recent.clear()
    assert jm.request("AAPL")["cached"] is False  # evicted → re-analysed
    wait_final(jm, "AAPL")
    assert fakes.FakeEngine.calls.count("AAPL") == 2


def test_evicted_ticker_chart_still_served_from_disk(jm):
    jm.request("MSFT")
    wait_final(jm, "MSFT")
    jm.store._reset_entries()  # simulate eviction
    assert jm.plot_path("MSFT").endswith("MSFT_valuation.png")
    prev = jm.plot_path("MSFT", preview=True)
    assert os.path.basename(os.path.dirname(prev)) == "previews"


def test_job_crossing_midnight_is_not_cached(tmp_path):
    day = Day()
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    gate = fakes.Gate()
    m = JobManager(s, analyzer=TickerAnalyzer(s, backends=fakes.backends(edge=gate.wrap(fakes.EDGE)), today=day),
                   today=day)
    try:
        m.request("MSFT")
        assert gate.entered.wait(5)
        day.d = dt.date(2026, 9, 28)  # midnight while the job runs
        gate.release.set()
        t0 = time.time()
        while m._inflight and time.time() - t0 < 5:
            time.sleep(0.02)
        assert "MSFT" not in m.store
        assert m.request("MSFT", poll=True)["status"] == "expired"  # yesterday's recent job dropped
    finally:
        m.shutdown()


def test_poll_never_enqueues(jm):
    snap = jm.request("MSFT", poll=True)
    assert snap["status"] == "expired" and jm.cache_stats()["queue"] == 0 and not jm._inflight


def test_http_polls_do_not_touch_lfu(tmp_path):
    import http.client
    import json
    from ui.server import PortalApp, PortalServer, make_handler
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    m = JobManager(s, analyzer=TickerAnalyzer(s, backends=fakes.backends()))
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(s, jobs=m)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        return body
    try:
        get("/api/ticker/MSFT")
        t0 = time.time()
        while get("/api/ticker/MSFT?poll=1")["status"] != "done" and time.time() - t0 < 10:
            time.sleep(0.05)
        before = get("/api/cache")
        for _ in range(4):
            get("/api/ticker/MSFT?poll=1")
        assert get("/api/cache")["hits"] == before["hits"] and get("/api/cache")["top"] == before["top"]
        m._recent.clear()
        snap = get("/api/ticker/MSFT")
        after = get("/api/cache")
        assert snap["cached"] is True and after["hits"] == before["hits"] + 1
        assert after["capacity"] == 250 and after["policy"] == "lfu" and after["top"][0][0] == "MSFT"
    finally:
        httpd.shutdown()
        httpd.server_close()
        m.shutdown()
