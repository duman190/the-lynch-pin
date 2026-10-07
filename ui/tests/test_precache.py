"""Nightly pre-cache (ui/precache.py): the most looked-up tickers, one at a time with their AI overviews."""
import datetime as dt
import time

import pytest

from ui import llm as L
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.precache import Precacher
from ui.stats import StatsRecorder
from ui.tests import fake_lmstudio, fakes

NOW = time.time()


@pytest.fixture
def lm():
    httpd, state = fake_lmstudio.serve(state={"delay": 0.0})
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def settings(tmp_path, lm):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    s.llm_base_url = lm[0]
    return s


@pytest.fixture
def rec(tmp_path):
    r = StatsRecorder(str(tmp_path / "stats.sqlite3"), start=False)
    yield r
    r.close()


def seed(rec, counts, status="done", ts=NOW - 3600):
    for sym, n in counts.items():
        for _ in range(n):
            rec.query(sym, "fresh", status, 5.0, ts=ts)
    rec.flush(final=True)


def make(settings, rec, ai=True):
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends(engine=fakes.AnyEngine)),
                    store=DayStore(), deadline=30, stats=rec, llm=L.LocalLLMClient(settings) if ai else None)
    return jm


def test_top_tickers(rec):
    seed(rec, {"NVDA": 5, "AMD": 3, "TSM": 3, "MU": 1})
    seed(rec, {"OLD": 9}, ts=NOW - 31 * 86400)         # outside the month
    seed(rec, {"BAD": 9}, status="error")               # failed lookups don't qualify
    seed(rec, {"SPY": 2}, status="nodata")              # answered (no GARP data): qualifies
    rec.query("TSM", "fresh", "done", 5.0, ts=NOW - 60)  # tie with AMD: the more recent first
    rec.flush(final=True)
    assert rec.top_tickers(100) == ["NVDA", "TSM", "AMD", "SPY", "MU"]  # fewer than 100: what there is
    assert rec.top_tickers(2) == ["NVDA", "TSM"]


def test_run_is_sequential_with_ai_and_off_the_books(settings, rec, lm):
    seed(rec, {"NVDA": 3, "AMD": 2, "TSM": 1})
    before = rec.summary(30)
    jm = make(settings, rec)
    seen = []
    warm = jm.warm

    def checked(sym, **kw):  # nothing of the previous ticker may still be running
        assert not jm._inflight and not jm._ai_inflight, (sym, jm._inflight, jm._ai_inflight)
        seen.append(sym)
        return warm(sym, **kw)

    jm.warm = checked
    try:
        out = Precacher(jm, rec, count=100).run()
        assert seen == ["NVDA", "AMD", "TSM"]
        assert out["tickers"] == 3 and out["analysed"] == 3 and out["ai"] == 3
        for sym in seen:
            entry = jm.lookup(sym)
            assert entry["status"] == "done" and entry["ai"]["status"] == "done"
        rec.flush(final=True)
        after = rec.summary(30)
        assert after["tickers"]["total"] == before["tickers"]["total"] == 6  # not a visitor's lookup
        assert after["ai"]["n"] == 0                                         # nor a visitor's AI overview
        again = Precacher(jm, rec, count=100).run()                          # already cached: nothing re-run
        assert again["outcomes"] == {"cached": 3} and again["ai"] == 3
    finally:
        jm.shutdown()


def test_without_ai(settings, rec):
    seed(rec, {"NVDA": 2, "AMD": 1})
    jm = make(settings, rec, ai=False)
    try:
        assert jm.warm("NVDA", ai=True) == ("done", None)
        out = Precacher(jm, rec, count=1).run()
        assert out["tickers"] == 1 and out["outcomes"] == {"cached": 1} and out["ai"] is None
    finally:
        jm.shutdown()


def test_no_stats_yet(settings, rec):
    jm = make(settings, rec, ai=False)
    try:
        assert Precacher(jm, rec).run()["tickers"] == 0
    finally:
        jm.shutdown()


def test_runs_right_after_midnight():
    p = Precacher(None, None, now=lambda: dt.datetime(2026, 10, 7, 23, 59, 0))
    assert p.seconds_to_next_run() == 90.0  # 60 s to midnight + 30 s
    p = Precacher(None, None, now=lambda: dt.datetime(2026, 10, 8, 0, 0, 10))
    assert p.seconds_to_next_run() == 86420.0  # just missed: the next night


def test_flag(monkeypatch):
    from ui.server import parse_args
    assert Settings().precache == 100
    assert parse_args(["--precache", "0"])[0].precache == 0 and parse_args(["--precache", "40"])[0].precache == 40
