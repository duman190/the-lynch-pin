"""Inference queue: local model up to the low watermark, Gemini up to the high one, then the Quick overview;
Gemini's request budget and client; one AI overview per ticker per day; the AI-route stats."""
import datetime as dt
import http.client
import json
import sqlite3
import threading
import time

import pytest

from ui import llm as L
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.gemini import GeminiBudget, GeminiClient, _retry_delay
from ui.inference_queue import InferenceQueue
from ui.jobs import DayStore, JobManager
from ui.server import PortalApp, PortalServer, make_handler, parse_args
from ui.stats import StatsRecorder
from ui.tests import fake_lmstudio, fakes

# 2026-10-07 12:00 Pacific (19:00 UTC)
NOON_PT = dt.datetime(2026, 10, 7, 19, 0, tzinfo=dt.timezone.utc).timestamp()


class Clock:
    def __init__(self, t=NOON_PT):
        self.t = t

    def __call__(self):
        return self.t


def server():
    httpd, state = fake_lmstudio.serve()
    return httpd, state, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture
def local():
    httpd, state, url = server()
    yield url, state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def remote():
    """A fake standing in for Gemini's OpenAI-compatible endpoint (GeminiClient posts to <base>/chat/completions)."""
    httpd, state, url = server()
    yield url + "/v1", state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def settings(tmp_path, local):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    s.llm_base_url = local[0]
    return s


def wait(fn, pred, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if pred(v):
            return v
        time.sleep(0.02)
    raise AssertionError(v)


# ── the watermarks ───────────────────────────────────────────────────────────
def test_local_then_gemini_then_paused_until_the_minute_frees_up(tmp_path):
    wall, mono = Clock(), Clock(1000.0)
    q = InferenceQueue(2, GeminiBudget(str(tmp_path / "g.sqlite3"), rpm=3, rpd=100, clock=wall), clock=mono)
    assert q.high == 5
    routes = [q.admit(f"T{i}", local_ok=True, local_pending=0)[0] for i in range(5)]
    assert routes == ["local", "local", "gemini", "gemini", "gemini"]
    route, wait_s = q.admit("T5", local_ok=True, local_pending=0)
    assert route is None and 1 <= wait_s <= 60  # above the high watermark: paused
    mono.t += 30
    wall.t += 30
    assert q.admit("T6", True, 0)[0] is None
    mono.t += 31  # the first minute's local admissions age out
    wall.t += 31
    assert q.admit("T7", True, 0)[0] == "local"
    st = q.status()
    assert st["low"] == 2 and st["high"] == 5 and st["paused_last_min"] == 1 and st["gemini"]["today"] == 3
    assert st["since_start"] == {"local": 3, "gemini": 3, "paused": 2}


def test_a_slow_local_model_never_builds_more_than_its_share_of_backlog(tmp_path):
    q = InferenceQueue(3, GeminiBudget(str(tmp_path / "g.sqlite3"), rpm=15), clock=Clock(0.0))
    assert q.admit("A", local_ok=True, local_pending=3)[0] == "gemini"  # its minute isn't full, its queue is


def test_offline_local_model_sends_its_share_to_gemini_and_without_gemini_pauses(tmp_path):
    q = InferenceQueue(4, GeminiBudget(str(tmp_path / "g.sqlite3"), rpm=1), clock=Clock(0.0))
    assert q.admit("A", local_ok=False, local_pending=0)[0] == "gemini"
    assert q.admit("B", local_ok=False, local_pending=0)[0] is None
    alone = InferenceQueue(4, None, clock=Clock(0.0))
    assert alone.admit("A", local_ok=False, local_pending=0) == (None, 60) and alone.high == 4


def test_warm_uses_the_local_model_only_and_is_never_paused(tmp_path):
    q = InferenceQueue(1, GeminiBudget(str(tmp_path / "g.sqlite3"), rpm=5), clock=Clock(0.0))
    assert q.admit("A", True, 0)[0] == "local"
    assert q.admit("B", True, 5, warm=True)[0] == "local"  # full minute, long queue: the pre-cache still runs
    assert q.admit("C", False, 0, warm=True)[0] is None    # local model down: no Gemini for the pre-cache
    assert q.status()["since_start"] == {"local": 2} and q.budget.status()["today"] == 0


# ── Gemini's budget ──────────────────────────────────────────────────────────
def test_budget_per_minute_per_pacific_day_and_across_restarts(tmp_path):
    path, clock = str(tmp_path / "g.sqlite3"), Clock()
    b = GeminiBudget(path, rpm=2, rpd=3, clock=clock)
    assert b.try_acquire("A") and b.try_acquire("B") and not b.try_acquire("C")  # 2 a minute
    assert b.retry_after() == 60
    clock.t += 61
    assert GeminiBudget(path, rpm=2, rpd=3, clock=clock).status()["today"] == 2  # a restart keeps the day
    assert b.try_acquire("C") and not b.try_acquire("D")  # 3 a day
    assert 0 < b.retry_after() <= 12 * 3600  # until midnight Pacific
    clock.t = NOON_PT + 12 * 3600 + 60  # 00:01 Pacific the next day
    assert b.try_acquire("E") and b.status()["today"] == 1


def test_budget_cooldown_after_a_429(tmp_path):
    clock = Clock()
    b = GeminiBudget(str(tmp_path / "g.sqlite3"), rpm=15, rpd=975, clock=clock)
    b.cooldown(25.7)
    assert not b.try_acquire("A") and b.retry_after() == 26
    clock.t += 26
    assert b.try_acquire("A") and b.status()["today"] == 1 and b.status()["last_min"] == 1
    b.cooldown(30, whole_day=True)
    clock.t += 60
    assert not b.try_acquire("B")  # the per-day quota ran out: closed until midnight Pacific


def test_retry_delay_is_read_from_googles_429():
    assert _retry_delay('{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "25s"}') == 25.0
    assert _retry_delay("Quota exceeded ... Please retry in 25.7226743s.") == 25.7226743
    assert _retry_delay("no hint") is None


# ── Gemini's client ──────────────────────────────────────────────────────────
def gemini_client(settings, remote, tmp_path, rpm=15, rpd=975):
    budget = GeminiBudget(str(tmp_path / "g.sqlite3"), rpm=rpm, rpd=rpd)
    return GeminiClient(settings, "test-key", budget=budget, base_url=remote[0])


def test_gemini_streams_with_its_key_and_reasoning_off(settings, remote, tmp_path):
    c = gemini_client(settings, remote, tmp_path)
    assert c.status()["available"] and c.status()["model"] == "gemini-flash-lite-latest"
    text, meta = c.generate(L.build_portal_messages(TickerAnalyzer(settings, backends=fakes.backends()).run("MSFT")))
    assert "compounder" in text and meta["metrics"]["ttft_s"] is not None and meta["reasoning"] == "off"
    path, req = remote[1]["requests"][-1]
    assert path == "/v1/chat/completions" and req["model"] == "gemini-flash-lite-latest"
    # Flash-Lite doesn't think and refuses the switch (a refused request still counts): never sent
    assert "reasoning_effort" not in req and req["max_tokens"] <= 2048 and req["stream"] is True
    assert remote[1]["auth"] == "Bearer test-key"
    assert [p for p, _ in remote[1]["requests"]] == ["/v1/chat/completions"]  # no priming request: it would count


def test_gemini_overloaded_or_rate_limited_is_unavailable(settings, remote, tmp_path):
    c = gemini_client(settings, remote, tmp_path)
    remote[1]["http_errors"] = [(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})]
    with pytest.raises(L.LLMUnavailable, match="overloaded"):
        c.generate("- MSFT: x")
    remote[1]["http_errors"] = [(429, [{"error": {"code": 429, "message": "Quota exceeded. Please retry in 25.7s.",
                                                  "details": [{"retryDelay": "25s"}]}}])]
    with pytest.raises(L.LLMUnavailable, match="retry in 25s"):  # RetryInfo's retryDelay wins
        c.generate("- MSFT: x")
    assert c.budget.status()["cooldown_s"] >= 25 and not c.budget.try_acquire("X")
    remote[1]["http_errors"] = [(429, {"error": {"message": "Resource has been exhausted"}})]  # no delay named
    c.budget._cooldown_until = 0
    with pytest.raises(L.LLMUnavailable, match="retry in 30s"):
        c.generate("- MSFT: x")
    remote[1]["http_errors"] = [(429, {"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}})]
    with pytest.raises(L.LLMUnavailable, match="daily quota"):
        c.generate("- MSFT: x")
    assert c.budget.status()["today"] >= c.budget.rpd


def test_gemini_needs_a_key_and_can_be_switched_off(settings):
    assert GeminiClient.from_settings(settings) is None  # conftest clears GEMINI_API_KEY
    g = GeminiClient.from_settings(settings, api_key="k")
    assert g is not None and g.budget.rpm == 15 and g.budget.rpd == 975
    settings.gemini = False
    assert GeminiClient.from_settings(settings, api_key="k") is None


def test_cli_sets_the_watermarks():
    s, _ = parse_args(["--llm-rpm", "12", "--gemini-rpm", "10", "--gemini-rpd", "500", "--gemini-model", "m"])
    assert (s.llm_rpm, s.gemini_rpm, s.gemini_rpd, s.gemini_model, s.gemini) == (12, 10, 500, "m", True)
    assert parse_args(["--no-gemini"])[0].gemini is False
    assert Settings().llm_rpm == 24 and Settings().gemini_model == "gemini-flash-lite-latest"


# ── the job manager ──────────────────────────────────────────────────────────
TICKERS = ("MSFT", "AAPL", "NVDA", "KO")


@pytest.fixture
def more_tickers(monkeypatch):
    for sym in TICKERS[1:]:
        monkeypatch.setitem(fakes.FakeEngine.infos, sym, dict(fakes.MSFT_INFO, longName=sym))
        monkeypatch.setitem(fakes.FakeEngine.rows, sym, dict(fakes.MSFT_ROW, Ticker=sym))


def make(settings, gemini=None, rec=None, llm=None):
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), store=DayStore(),
                    llm=llm or L.LocalLLMClient(settings), gemini=gemini, stats=rec)
    for sym in TICKERS:
        if sym in fakes.FakeEngine.rows:
            wait(lambda: jm.request(sym, poll=bool(jm.lookup(sym))), lambda s: s["status"] == "done")
    return jm


def test_job_manager_routes_by_watermark(settings, local, remote, tmp_path, more_tickers):
    settings.llm_rpm = 1
    rec = StatsRecorder(str(tmp_path / "stats.sqlite3"), start=False)
    jm = make(settings, gemini_client(settings, remote, tmp_path, rpm=1), rec)
    try:
        assert jm.request_ai("MSFT")["backend"] == "local"
        assert jm.request_ai("AAPL")["backend"] == "gemini"
        paused = jm.request_ai("NVDA")
        assert paused["status"] == "unavailable" and paused["reason"] == "busy" and paused["fallback"] == "quick"
        assert paused["retry_after"] >= 1 and "NVDA" not in jm._ai_inflight
        for sym, backend in (("MSFT", "local"), ("AAPL", "gemini")):
            done = wait(lambda: jm.request_ai(sym), lambda s: s["status"] == "done")
            assert done["backend"] == backend and done["complete"] and done["attempts"] == 1
        assert len(local[1]["requests"]) >= 1 and len(remote[1]["requests"]) == 1  # Gemini: one request, no retry
        inf = jm.cache_stats()["inference"]
        assert (inf["low"], inf["high"], inf["local_last_min"], inf["paused_last_min"]) == (1, 2, 1, 1)
        assert jm.cache_stats()["gemini_workers"] == 1
        rec.flush(final=True)
        assert rec.summary()["ai_routes"] == {"local": 1, "gemini": 1, "quick": 1, "total": 3,
                                              "quick_reasons": {"busy": 1}}
    finally:
        jm.shutdown()
        rec.close()


def test_gemini_failure_falls_back_to_quick_and_can_be_retried(settings, local, remote, tmp_path, more_tickers):
    settings.llm_rpm = 0  # everything to Gemini
    remote[1]["http_errors"] = [(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})]
    jm = make(settings, gemini_client(settings, remote, tmp_path))
    try:
        jm.request_ai("MSFT")
        failed = wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] not in ("queued", "running"))
        assert failed["status"] == "unavailable" and "overloaded" in failed["error"]
        assert jm.request_ai("MSFT")["status"] == "unavailable"  # the same failure for a few minutes, no new request
        jm.request_ai("MSFT", refresh=True)  # ↻ Retry AI
        assert wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done")["backend"] == "gemini"
    finally:
        jm.shutdown()


def test_offline_local_model_hands_everything_to_gemini(settings, remote, tmp_path, more_tickers):
    settings.llm_base_url = "http://127.0.0.1:9"
    jm = make(settings, gemini_client(settings, remote, tmp_path))
    try:
        jm.request_ai("MSFT")
        assert wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done")["backend"] == "gemini"
    finally:
        jm.shutdown()


def test_one_overview_per_ticker(settings, local, more_tickers):
    local[1]["delay"] = 0.01
    jm = make(settings)
    try:
        n0 = len(local[1].setdefault("requests", []))
        snaps = []
        threads = [threading.Thread(target=lambda: snaps.append(jm.request_ai("MSFT"))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(snaps) == 8 and len(jm._ai_inflight) <= 1  # every visitor joined the one generation
        wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done")
        chats = [r for p, r in local[1]["requests"][n0:] if r.get("max_tokens", 0) > 1]  # leave the 1-token prime out
        assert len(chats) == 1
        jm._recent_ai.clear()
        again = jm.request_ai("MSFT", refresh=True)  # ↻ Retry on a complete overview: served from the cache
        assert again["cached"] is True
        assert len([r for p, r in local[1]["requests"][n0:] if r.get("max_tokens", 0) > 1]) == 1
        assert jm.inference.status()["since_start"] == {"local": 1}
    finally:
        jm.shutdown()


class Scripted:
    """A local model that answers from a script; the first reply is a usable partial (no retry inside the job)."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def status(self, **kw):
        return {"available": True, "model": "scripted"}

    def autoload_pending(self):
        return False

    def generate(self, prompt, **kw):
        self.calls += 1
        return self.replies.pop(0), {"finish_reason": "stop", "model": "scripted", "metrics": {}}


def test_a_partial_overview_can_be_retried_a_complete_one_cannot(settings, more_tickers):
    c = Scripted(["🤖: short but fine", fake_lmstudio.canned_reply("- MSFT: x")])
    jm = make(settings, llm=c)
    try:
        jm.request_ai("MSFT")
        part = wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done")
        assert part["complete"] is False and c.calls == 1
        jm._recent_ai.clear()
        assert jm.request_ai("MSFT")["cached"] is True  # shown to everyone, with ↻ Retry AI on the page
        jm.request_ai("MSFT", refresh=True)
        full = wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done" and s.get("complete"))
        assert full["complete"] is True and c.calls == 2
        jm._recent_ai.clear()
        assert jm.request_ai("MSFT", refresh=True)["cached"] is True and c.calls == 2
    finally:
        jm.shutdown()


def test_precache_never_spends_gemini(settings, remote, tmp_path, more_tickers):
    settings.llm_rpm = 0
    jm = make(settings, gemini_client(settings, remote, tmp_path))
    try:
        assert jm.warm("MSFT", timeout=10) == ("cached", "done")  # local model, although its share is 0
        assert remote[1].get("requests") is None and jm.gemini.budget.status()["today"] == 0
    finally:
        jm.shutdown()


def test_paused_ai_is_a_normal_answer_and_health_shows_the_queue(settings, remote, tmp_path, more_tickers):
    settings.llm_rpm, settings.gemini_rpm = 0, 1
    jm = make(settings, gemini_client(settings, remote, tmp_path, rpm=1))
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm, llm=jm.llm)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        return r.status, body
    try:
        assert get("/api/ticker/MSFT/ai")[1]["backend"] == "gemini"
        st, body = get("/api/ticker/AAPL/ai")
        assert st == 200 and body["status"] == "unavailable" and body["fallback"] == "quick"  # not a 429 to retry on
        h = get("/api/health")[1]
        assert h["ai"]["offload"] == "gemini-flash-lite-latest" and h["cache"]["inference"]["high"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


# ── stats ────────────────────────────────────────────────────────────────────
def test_stats_keep_ai_routes_for_the_retention_window(tmp_path):
    clock = Clock()
    rec = StatsRecorder(str(tmp_path / "s.sqlite3"), clock=clock, start=False)
    try:
        rec.ai_route("MSFT", "local", "done")
        rec.ai_route("AAPL", "gemini", "done")
        rec.ai_route("NVDA", "quick", "busy")
        rec.ai_route("KO", "quick", "unavailable", ts=NOON_PT - 31 * 86400)  # past the 30-day window
        rec.flush(final=True)
        assert rec.summary()["ai_routes"] == {"local": 1, "gemini": 1, "quick": 1, "total": 3,
                                              "quick_reasons": {"busy": 1}}
        assert rec._db.execute("SELECT COUNT(*) FROM ai_routes").fetchone()[0] == 3  # pruned
    finally:
        rec.close()


def test_stats_database_from_before_gemini_gets_the_backend_column(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ai (ts REAL NOT NULL, ticker TEXT NOT NULL, status TEXT NOT NULL, ttft_s REAL, "
               "tok_s REAL, total_s REAL, wait_s REAL, tokens INTEGER, model TEXT)")
    db.execute("INSERT INTO ai VALUES (1, 'MSFT', 'done', 1, 2, 3, 0, 9, 'm')")
    db.commit()
    db.close()
    rec = StatsRecorder(path, clock=Clock(), start=False)
    try:
        rec.ai("AAPL", "done", {"ttft_s": 1.0}, total_s=2.0, wait_s=0.0, model="g", backend="gemini")
        rec.flush(final=True)
        assert rec._db.execute("SELECT ticker, backend FROM ai ORDER BY ts").fetchall()[-1] == ("AAPL", "gemini")
    finally:
        rec.close()
