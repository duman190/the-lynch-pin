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


@pytest.fixture
def night():
    """The pre-cache's own model (--cache-llm-url), e.g. Flash-Next on the switchboard's port 8002."""
    httpd, state = fake_lmstudio.serve(state={"delay": 0.0})
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()
    httpd.server_close()


def make(settings, rec, ai=True, cache=False):
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends(engine=fakes.AnyEngine)),
                    store=DayStore(), deadline=30, stats=rec, llm=L.LocalLLMClient(settings) if ai else None,
                    cache_llm=L.CacheLLMClient(settings) if cache else None)
    return jm


def chats(state):
    return [r for path, r in state.get("requests", []) if path == "/v1/chat/completions"]


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


def test_overviews_by_the_cache_llm_then_the_online_model_is_primed(settings, rec, lm, night):
    """--cache-llm-url writes the night's overviews, one at a time and outside the online model's minute; then the
    online model is primed (the switchboard swapped it out). Visitors' overviews stay on the online model."""
    from ui.server import PortalApp
    seed(rec, {"NVDA": 2, "AMD": 1})
    settings.cache_llm_base_url, settings.cache_llm_model = night[0], ""
    jm = make(settings, rec, cache=True)
    try:
        out = Precacher(jm, rec, count=100).run()
        assert out["analysed"] == 2 and out["ai"] == 2 and out["online_warm"]["ok"]
        assert [jm.lookup(s)["ai"]["backend"] for s in ("NVDA", "AMD")] == ["cache", "cache"]
        assert len(chats(night[1])) == 2 and all(r["messages"][-1]["content"] for r in chats(night[1]))  # no prime
        day = chats(lm[1])
        assert len(day) == 1 and day[0]["messages"][-1]["content"] == "" and day[0]["max_tokens"] == 1  # the prime
        q = jm.inference.status()
        assert q["since_start"] == {"cache": 2} and q["local_last_min"] == 0
        assert jm.warm("MU", ai=False) == ("done", None)
        assert jm.request_ai("MU")["backend"] == "local"  # a visitor at night: the online model
        health = PortalApp(settings, jobs=jm, llm=jm.llm).health()
        assert "cache_llm" in health["ai"] and "cache_llm" not in PortalApp._public_health(health)["ai"]
    finally:
        jm.shutdown()


def test_cache_llm_down_falls_back_to_the_online_model(settings, rec, lm):
    seed(rec, {"NVDA": 1})
    settings.cache_llm_base_url = "http://127.0.0.1:9"  # nothing listens on the discard port
    jm = make(settings, rec, cache=True)
    try:
        out = Precacher(jm, rec, count=100).run()
        assert out["ai"] == 1 and jm.lookup("NVDA")["ai"]["backend"] == "local" and out["online_warm"]["ok"]
    finally:
        jm.shutdown()


def test_no_cache_llm_no_extra_prime(settings, rec, lm):
    seed(rec, {"NVDA": 1})
    jm = make(settings, rec)
    try:
        out = Precacher(jm, rec, count=100).run()
        assert out["ai"] == 1 and "online_warm" not in out
        assert sum(r["messages"][-1]["content"] == "" for r in chats(lm[1])) == 1  # just the overview's own prime
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
