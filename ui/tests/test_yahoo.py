"""Yahoo call hygiene (ui/yahoo.py) and the job manager's rate-limit circuit breaker (ui/jobs.py)."""
import datetime as dt
import time

import pandas as pd
import pytest

from ui import jobs as jobs_mod
from ui import yahoo
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.tests import fakes


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    return s


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    yahoo.clear()
    monkeypatch.setattr(yahoo, "_shared", set())
    monkeypatch.setattr(fakes.ThrottledEngine, "budget", 0)
    monkeypatch.setattr(jobs_mod, "RATE_LIMIT_BACKOFF", (0.3, 1.2))


class FakeTicker:
    def __init__(self, sym):
        self.ticker = sym


def counting_history():
    calls = []

    def history(tk, *args, **kwargs):
        calls.append((tk.ticker, args, kwargs))
        return pd.DataFrame({"Close": [1.0, 2.0, 3.0]})
    return history, calls


# ── history de-duplication ──────────────────────────────────────────────────
def test_same_bars_are_downloaded_once_and_handed_out_as_copies():
    history, calls = counting_history()
    a = yahoo.cached_history(history, FakeTicker("ORCL"), period="1y", interval="1d")
    a.loc[0, "Close"] = 99.0  # a caller editing its frame in place...
    b = yahoo.cached_history(history, FakeTicker("ORCL"), period="1y", interval="1d")
    assert len(calls) == 1 and b.loc[0, "Close"] == 1.0  # ...does not corrupt the cached copy
    yahoo.cached_history(history, FakeTicker("ORCL"), period="5y", interval="1mo")
    yahoo.cached_history(history, FakeTicker("ADBE"), period="1y", interval="1d")
    assert len(calls) == 3 and yahoo.stats == {"hits": 1, "misses": 3}


def test_benchmark_index_is_shared_across_lookups_and_start_end_match_by_day(monkeypatch):
    history, calls = counting_history()
    clock = {"t": 1000.0}
    monkeypatch.setattr(yahoo.time, "monotonic", lambda: clock["t"])
    yahoo._shared.add("SPY")
    now = dt.datetime(2026, 10, 2, 15, 0)
    yahoo.cached_history(history, FakeTicker("SPY"), start=now - dt.timedelta(days=700), end=now)
    clock["t"] += 300  # five minutes later, another ticker's backtest
    later = now + dt.timedelta(minutes=5)
    yahoo.cached_history(history, FakeTicker("SPY"), start=later - dt.timedelta(days=700), end=later)
    yahoo.cached_history(history, FakeTicker("ORCL"), period="1y")
    clock["t"] += 90  # past the same-ticker TTL, inside the shared one
    yahoo.cached_history(history, FakeTicker("ORCL"), period="1y")
    yahoo.cached_history(history, FakeTicker("SPY"), start=later - dt.timedelta(days=700), end=later)
    assert [c[0] for c in calls] == ["SPY", "ORCL", "ORCL"]
    clock["t"] += yahoo.SHARED_TTL
    yahoo.cached_history(history, FakeTicker("SPY"), start=later - dt.timedelta(days=700), end=later)
    assert [c[0] for c in calls] == ["SPY", "ORCL", "ORCL", "SPY"]


def test_failures_are_not_cached():
    calls = []

    def history(tk, *a, **k):
        calls.append(1)
        return pd.DataFrame()
    for _ in range(2):
        assert yahoo.cached_history(history, FakeTicker("ORCL"), period="1y").empty
    assert len(calls) == 2


def test_install_patches_yfinance_and_counts_rate_limits(monkeypatch):
    from yfinance.base import TickerBase
    from yfinance.exceptions import YFRateLimitError
    monkeypatch.setattr(yahoo, "_installed", False)
    monkeypatch.setattr(TickerBase, "history", TickerBase.history)  # restored after the test
    monkeypatch.setattr(YFRateLimitError, "__init__", YFRateLimitError.__init__)
    yahoo.install(("spy",))
    yahoo.install(("SPY",))  # idempotent
    assert TickerBase.history.__name__ == "patched_history" and "SPY" in yahoo._shared
    before = yahoo.rate_limit_events()
    with pytest.raises(YFRateLimitError):
        raise YFRateLimitError()
    assert yahoo.rate_limit_events() == before + 1


# ── analyzer ────────────────────────────────────────────────────────────────
def test_throttled_quote_is_a_rate_limited_error(settings):
    fakes.ThrottledEngine.budget = 1
    r = TickerAnalyzer(settings, backends=fakes.backends(engine=fakes.ThrottledEngine)).run("ORCL")
    assert r["status"] == "error" and r["rate_limited"] is True and r["cacheable"] is False
    assert "rate-limiting" in r["reason"]


def test_throttled_stage_makes_a_partial_result_uncacheable(settings):
    def technicals(t):
        yahoo.note_rate_limit()
        return None
    r = TickerAnalyzer(settings, backends=fakes.backends(technicals=technicals)).run("MSFT")
    assert r["status"] == "done" and r["rate_limited"] is True and r["cacheable"] is False
    clean = TickerAnalyzer(settings, backends=fakes.backends()).run("MSFT")
    assert "rate_limited" not in clean and clean.get("cacheable", True)


# ── circuit breaker ─────────────────────────────────────────────────────────
def make_jm(settings, store, workers=1, **backends):
    b = fakes.backends(engine=fakes.ThrottledEngine, **backends)
    return JobManager(settings, analyzer=TickerAnalyzer(settings, backends=b), store=store, workers=workers,
                      deadline=30)


def poll_until(jm, sym, pred, timeout=10):
    t0, snap = time.time(), None
    while time.time() - t0 < timeout:
        snap = jm.request(sym, poll=True)
        if pred(snap):
            return snap
        time.sleep(0.01)
    raise AssertionError(f"{sym}: condition never met, last {snap}")


def test_throttled_lookup_waits_and_retries_instead_of_failing(settings):
    fakes.ThrottledEngine.budget = 2  # Yahoo throttles the first two attempts
    store = DayStore()
    jm = make_jm(settings, store)
    try:
        jm.request("ORCL")
        waiting = poll_until(jm, "ORCL", lambda s: s["status"] == "queued" and s.get("note"))
        assert "rate-limiting" in waiting["note"] and waiting["retry_after"] >= 0
        assert jm.cache_stats()["rate_limited"] is True
        done = poll_until(jm, "ORCL", lambda s: s["status"] in ("done", "error"))
        assert done["status"] == "done" and store.peek("ORCL") is not None
        st = jm.cache_stats()
        assert st["rate_limited"] is False and st["rate_limit_hits"] == 2 and st["rate_limit_retry_s"] == 0
    finally:
        jm.shutdown()


def test_gives_up_after_retries_without_caching(settings):
    fakes.ThrottledEngine.budget = 10
    store = DayStore()
    jm = make_jm(settings, store)
    try:
        jm.request("ORCL")
        snap = poll_until(jm, "ORCL", lambda s: s["status"] in ("done", "error"))
        assert snap["status"] == "error" and "try again in a few minutes" in snap["error"]
        assert store.peek("ORCL") is None and jm.cache_stats()["rate_limit_hits"] == 3
    finally:
        jm.shutdown()


def test_one_probe_at_a_time_while_throttled(settings):
    fakes.ThrottledEngine.budget = 1
    gate = fakes.Gate()
    jm = make_jm(settings, DayStore(), workers=3, edge=gate.wrap(fakes.EDGE))
    try:
        jm.request("ORCL")  # throttled → pause, re-queued at the front
        poll_until(jm, "ORCL", lambda s: s["status"] == "queued" and s.get("note"))
        jm.request("ADBE")
        jm.request("INTU")
        assert gate.entered.wait(5)  # the probe (ORCL, first in line) reached the edge stage
        st = jm.cache_stats()
        assert st["running"] == "ORCL" and st["queue"] == 2  # three idle workers, yet one probe only
        assert "checking whether it is back" in jm.request("ADBE", poll=True)["note"]
        gate.release.set()
        for sym in ("ORCL", "ADBE", "INTU"):
            assert poll_until(jm, sym, lambda s: s["status"] in ("done", "error"))["status"] == "done"
        assert jm.cache_stats()["rate_limited"] is False
    finally:
        gate.release.set()
        jm.shutdown()


def test_throttled_probe_doubles_the_pause(settings):
    fakes.ThrottledEngine.budget = 2
    jm = make_jm(settings, DayStore())
    seen = []
    try:
        jm.request("ORCL")
        t0 = time.time()
        while time.time() - t0 < 10:
            with jm._cv:
                if jm._backoff and (not seen or seen[-1] != jm._backoff):
                    seen.append(jm._backoff)
            if jm.request("ORCL", poll=True)["status"] == "done":
                break
            time.sleep(0.01)
        assert seen == [0.3, 0.6]
    finally:
        jm.shutdown()
