"""Stats page: lock-free recording, rolling retention, summaries, the portal's hooks and the LAN-only server."""
import datetime as dt
import http.client
import json
import socket
import sqlite3
import threading
import time

import pytest

from ui import llm as L
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.server import (PortalApp, PortalServer, StatsApp, build_app, make_handler, make_stats_handler, parse_args,
                       start_stats_server)
from ui.stats import GRACE_MINUTES, StatsRecorder, cdf
from ui.tests import fake_lmstudio, fakes

NOW = 1_780_000_000.0  # a fixed wall clock (2026-05-28)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    return s


@pytest.fixture
def rec(tmp_path):
    r = StatsRecorder(str(tmp_path / "stats.sqlite3"), clock=Clock(), start=False)
    yield r
    r.close()


def rows(rec, sql, *args):
    return rec._db.execute(sql, args).fetchall()


def serve(handler, server=PortalServer):
    httpd = server(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class LanPeer(PortalServer):
    """Every connection appears to come from a LAN device (the tests can only connect from loopback)."""
    peer = "192.168.1.20"

    def get_request(self):
        sock, addr = super().get_request()
        return sock, (self.peer, addr[1])


def get(httpd, path, host=None, method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    c.request(method, path, headers=dict(headers or {}, **({"Host": host} if host else {})))
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


# ── recorder ────────────────────────────────────────────────────────────────
def test_counters_lose_no_increment_across_threads(rec):
    """8 threads × 5000 requests in the same minute, no lock: every one is counted."""
    def hammer():
        for i in range(5000):
            rec.hit(200 if i % 10 else 429, "queue_full" if i % 10 == 0 else None)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rec.flush(final=True)
    assert rows(rec, "SELECT minute, requests FROM minutes") == [(int(NOW // 60), 40000)]
    assert rows(rec, "SELECT reason, n FROM rejections") == [("queue_full", 4000)]


def test_minute_is_written_after_the_grace_period(rec):
    rec.hit(200)
    rec.query("MSFT", "fresh", "done", 3.2)
    rec.flush()
    assert rows(rec, "SELECT COUNT(*) FROM minutes") == [(0,)]  # the current minute is still open
    assert rows(rec, "SELECT ticker, source, latency_s FROM queries") == [("MSFT", "fresh", 3.2)]  # rows go at once
    rec._clock.t += 60 * GRACE_MINUTES
    rec.flush()
    assert rows(rec, "SELECT requests FROM minutes") == [(1,)]


def test_upserts_add_to_a_minute_written_before(rec):
    rec.hit(200)
    rec.flush(final=True)
    rec.hit(200)
    rec.hit(403, "outside_lan")
    rec.flush(final=True)  # e.g. a restart within the same minute
    assert rows(rec, "SELECT requests FROM minutes") == [(3,)]
    assert rows(rec, "SELECT reason, n FROM rejections") == [("outside_lan", 1)]


class BrokenDB:
    def __enter__(self):
        raise sqlite3.OperationalError("disk I/O error")

    def __exit__(self, *exc):
        return False


def test_failed_write_is_retried(rec):
    rec.hit(200)
    rec.query("MSFT", "cache", "done", 0.001)
    real, rec._db = rec._db, BrokenDB()
    with pytest.raises(sqlite3.OperationalError):
        rec.flush(final=True)
    rec._db = real
    rec.flush(final=True)
    assert rows(rec, "SELECT requests FROM minutes") == [(1,)]
    assert rows(rec, "SELECT COUNT(*) FROM queries") == [(1,)]


def test_rolling_retention(rec):
    old, recent = NOW - 31 * 86400, NOW - 29 * 86400
    for ts in (old, recent):
        rec.hit(200, ts=ts)
        rec.hit(429, "queue_full", ts=ts)
        rec.query("MSFT", "fresh", "done", 1.0, ts=ts)
        rec.ai("MSFT", "done", {"ttft_s": 1.0, "tok_s": 50.0}, total_s=6.0, wait_s=0.0, ts=ts)
    rec.flush(final=True)  # a final flush prunes too
    for table, col in (("minutes", "minute * 60"), ("rejections", "minute * 60"), ("queries", "ts"), ("ai", "ts")):
        assert rows(rec, f"SELECT MIN({col}) >= ? FROM {table}", NOW - 30 * 86400) == [(1,)], table
        assert rows(rec, f"SELECT COUNT(*) FROM {table}") == [(1,)], table


def test_flusher_thread_writes_in_the_background(tmp_path):
    r = StatsRecorder(str(tmp_path / "s.sqlite3"), flush_every=0.05)
    try:
        r.query("AAPL", "fresh", "done", 2.0)
        deadline = time.time() + 5
        while time.time() < deadline and not r._db.execute("SELECT COUNT(*) FROM queries").fetchone()[0]:
            time.sleep(0.02)
        assert r.summary(1)["tickers"]["top"] == [{"ticker": "AAPL", "n": 1, "pct": 100.0}]
    finally:
        r.close()


def day_of(ts):
    return dt.datetime.fromtimestamp(ts).date().isoformat()


def test_visitors_counted_once_per_day_and_never_stored_as_ips(rec):
    for _ in range(3):
        rec.visit("192.168.1.20")
    rec.visit("192.168.1.21")
    rec.visit("192.168.1.20", ts=NOW - 86400)
    rec.flush()
    rec.visit("192.168.1.20")  # again after the flush: still one row
    rec.flush()
    assert rows(rec, "SELECT day, COUNT(*) FROM visitors GROUP BY day ORDER BY day") == \
        [(day_of(NOW - 86400), 1), (day_of(NOW), 2)]
    assert "192.168" not in "\n".join(rec._db.iterdump())  # a keyed hash, not the IP


def test_visits_racing_the_flusher_lose_nothing(tmp_path):
    """No lock: 4 threads mark 4000 visitors while the flusher pops them every millisecond."""
    r = StatsRecorder(str(tmp_path / "s.sqlite3"), flush_every=0.001)

    def go(k):
        for j in range(5000):
            r.visit(f"10.{k}.{j % 1000 // 250}.{j % 250}")

    threads = [threading.Thread(target=go, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r.close()
    db = sqlite3.connect(r.path)
    assert db.execute("SELECT COUNT(*) FROM visitors").fetchone() == (4000,)
    db.close()


def test_visitor_key_survives_a_restart(tmp_path):
    path = str(tmp_path / "s.sqlite3")
    for _ in range(2):
        r = StatsRecorder(path, clock=Clock(), start=False)
        r.visit("10.0.0.1")
        r.close()
    r = StatsRecorder(path, clock=Clock(), start=False)
    try:
        assert rows(r, "SELECT COUNT(*) FROM visitors") == [(1,)]  # the same visitor, not two
    finally:
        r.close()


def test_dau_and_rolling_mau(rec):
    day = 86400
    for k in range(40):
        rec.visit("10.0.0.1", ts=NOW - k * day)  # every day
    rec.visit("10.0.0.2", ts=NOW - 35 * day)       # once, 35 days ago
    rec.visit("10.0.0.3", ts=NOW)                  # today only
    rec.flush(final=True)
    v = rec.summary()["visitors"]
    assert (v["dau"], v["mau"]) == (2, 2)  # 10.0.0.2 left the 30-day window
    by = {d["day"]: d for d in v["days"]}
    assert (by[day_of(NOW - 35 * day)]["dau"], by[day_of(NOW - 35 * day)]["mau"]) == (2, 2)
    assert by[day_of(NOW - 6 * day)]["mau"] == 2 and by[day_of(NOW - 5 * day)]["mau"] == 1  # 30 days, then out
    assert v["since"] == day_of(NOW - 39 * day) and len(v["days"]) == 40
    assert v["peak_dau"] == {"n": 2, "day": day_of(NOW - 35 * day)} and v["peak_mau"]["n"] == 2


def test_short_history_spans_a_month_with_nulls_before_it(rec):
    rec.visit("10.0.0.1", ts=NOW - 2 * 86400)
    rec.flush(final=True)
    v = rec.summary()["visitors"]
    assert len(v["days"]) == 30 and v["days"][0]["dau"] is None and v["days"][-3]["dau"] == 1
    assert (v["dau"], v["mau"]) == (0, 1)
    assert rec.summary(1)["visitors"]["days"] == v["days"]  # not scoped by the range


def test_visitors_are_kept_a_year(rec):
    rec.visit("10.0.0.1", ts=NOW - 366 * 86400)
    rec.visit("10.0.0.1", ts=NOW - 364 * 86400)
    rec.query("MSFT", "fresh", "done", 1.0, ts=NOW - 364 * 86400)
    rec.flush(final=True)
    assert rows(rec, "SELECT COUNT(*) FROM visitors") == [(1,)]
    assert rows(rec, "SELECT COUNT(*) FROM queries") == [(0,)]  # everything else: a month


# ── summaries ───────────────────────────────────────────────────────────────
def test_cdf_nearest_rank_percentiles():
    c = cdf(range(1, 1001))
    assert (c["n"], c["min"], c["p50"], c["p90"], c["p99"], c["p999"], c["max"]) == (1000, 1, 500, 900, 990, 999, 1000)
    assert c["points"][-1] == [1000, 1.0] and len(c["points"]) < 300  # quantile grid above 400 samples
    small = cdf([3, 1, 2, 2, None, float("nan")])
    assert small["n"] == 4 and small["points"] == [[1, 0.25], [2, 0.75], [3, 1.0]]
    assert small["p999"] == 3
    assert cdf([]) == {"n": 0, "points": []}


def test_summary(rec):
    day = 86400
    y = NOW - day - 3600  # yesterday, outside a 24 h window
    for i in range(10):
        rec.hit(200, ts=NOW - 2 * day + i)  # one busy minute two days ago...
    rec.hit(200, ts=NOW - 600)          # ...and a quiet one today
    rec.hit(429, "visitor_busy", ts=NOW - 600)
    rec.upstream("yahoo_429", ts=NOW - 600)
    # yesterday: 2 of 4 lookups from the cache; today: 3 of 4
    for ts, sym, source in [(y, "NVDA", "cache"), (y, "NVDA", "cache"), (y, "AAPL", "fresh"),
                            (y, "MSFT", "joined"), (NOW - 60, "NVDA", "cache"), (NOW - 60, "NVDA", "cache"),
                            (NOW - 60, "AAPL", "cache"), (NOW - 60, "TSLA", "fresh")]:
        rec.query(sym, source, "done", 0.002 if source == "cache" else 9.0, ts=ts)
    rec.ai("NVDA", "done", {"ttft_s": 0.8, "tok_s": 70.0}, total_s=5.0, wait_s=0.5, model="qwen3.6-35b-a3b", ts=NOW - 60)
    rec.ai("AAPL", "error", {"ttft_s": 9.0, "tok_s": 1.0}, total_s=600.0, wait_s=0.0, ts=NOW - 60)
    rec.flush(final=True)

    s = rec.summary(7)
    assert s["days"] == 7 and s["retention_days"] == 30
    assert s["rpm"]["requests"]["n"] == 2 and s["rpm"]["requests"]["max"] == 10 and s["rpm"]["requests"]["min"] == 2
    # latency and the cold counts: only the lookups that waited for an analysis (fresh / joined / refresh)
    assert s["latency"]["n"] == 3 and s["latency"]["p50"] == 9.0 and s["latency"]["min"] == 9.0
    assert s["cold"]["total"] == 3 and s["cold"]["rpm"]["n"] == 2 and s["cold"]["rpm"]["max"] == 2
    assert s["sources"] == {"cache": 5, "fresh": 2, "joined": 1, "recent": 0, "refresh": 0}
    assert [d["rate"] for d in s["cache"]["days"]] == [50.0, 75.0] and s["cache"]["hit_rate"] == 62.5
    assert s["cache"]["cdf"]["n"] == 2
    assert s["tickers"]["distinct"] == 4 and s["tickers"]["top"][0] == {"ticker": "NVDA", "n": 4, "pct": 50.0}
    rj = s["rejections"]
    assert rj["requests"] == 12 and rj["total"] == 1 and rj["rate"] == round(100 / 12, 3)  # Yahoo isn't a request
    by = {r["reason"]: r for r in rj["reasons"]}
    assert by["visitor_busy"]["code"] == 429 and by["visitor_busy"]["label"] == "One analysis per visitor"
    assert by["yahoo_429"]["upstream"] and by["yahoo_429"]["pct"] is None
    assert len(rj["daily"]) == 8 and rj["daily"][-1]["n"] == 1
    ai = s["ai"]
    assert (ai["n"], ai["done"], ai["failed"], ai["models"]) == (2, 1, 1, ["qwen3.6-35b-a3b"])
    assert ai["ttft"]["p50"] == 0.8 and ai["speed"]["p50"] == 70.0 and ai["total"]["max"] == 5.0  # failures left out

    one = rec.summary(1)
    assert one["tickers"]["total"] == 4 and one["rpm"]["requests"]["n"] == 1
    assert one["cold"]["total"] == 1 and one["latency"]["n"] == 1
    assert rec.summary(365)["days"] == 30  # never past the retention window


# ── the portal's hooks ──────────────────────────────────────────────────────
def make_jm(settings, rec, **kw):
    return JobManager(settings, analyzer=TickerAnalyzer(settings, backends=kw.pop("backends", fakes.backends())),
                      store=DayStore(), deadline=30, stats=rec, **kw)


def drain(rec):
    out = []
    while rec._rows:
        out.append(rec._rows.popleft())
    return out


def test_job_manager_times_lookups_until_their_result(settings, rec):
    gate = fakes.Gate()
    jm = make_jm(settings, rec, backends=fakes.backends(edge=gate.wrap(fakes.EDGE)))
    try:
        jm.request("MSFT")
        assert gate.entered.wait(5)
        jm.request("MSFT")              # a second visitor joins the running analysis
        jm.request("MSFT", poll=True)   # polls are not lookups
        time.sleep(0.05)
        gate.release.set()
        deadline = time.time() + 5
        while time.time() < deadline and len(rec._rows) < 2:
            time.sleep(0.01)
        got = drain(rec)
        assert sorted(r[1][2] for r in got) == ["fresh", "joined"]
        assert all(r[0] == "queries" and r[1][1] == "MSFT" and r[1][3] == "done" for r in got)
        fresh = next(r[1] for r in got if r[1][2] == "fresh")
        joined = next(r[1] for r in got if r[1][2] == "joined")
        assert fresh[4] >= 0.05 and fresh[4] >= joined[4]
    finally:
        gate.release.set()
        jm.shutdown()


def test_busy_snapshots_carry_a_reason(settings, rec):
    gate = fakes.Gate()
    jm = make_jm(settings, rec, backends=fakes.backends(edge=gate.wrap(fakes.EDGE)), max_queue=0)
    try:
        assert jm.request("MSFT")["reason"] == "queue_full"
    finally:
        gate.release.set()
        jm.shutdown()


def test_yahoo_throttle_counts_as_an_upstream_rejection(settings, rec):
    jm = make_jm(settings, rec, start=False)
    from ui.jobs import Job
    jm._note_outcome(Job("MSFT", 0.0), throttled=True)
    rec.flush(final=True)
    assert rows(rec, "SELECT reason, n FROM rejections") == [("yahoo_429", 1)]


def test_ai_overviews_are_recorded(settings, rec):
    httpd, state = fake_lmstudio.serve()
    try:
        settings.llm_base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
        jm = make_jm(settings, rec, llm=L.LocalLLMClient(settings))
        deadline = time.time() + 10
        jm.request("MSFT")
        while jm.request("MSFT", poll=True)["status"] != "done" and time.time() < deadline:
            time.sleep(0.02)
        while jm.request_ai("MSFT")["status"] != "done" and time.time() < deadline:
            time.sleep(0.02)
        while not any(r[0] == "ai" for r in rec._rows) and time.time() < deadline:
            time.sleep(0.02)  # recorded just after the job is published
        jm._recent_ai.clear()
        assert jm.request_ai("MSFT")["cached"] is True  # a cached overview is not a generation
        jm.shutdown()
        ai = [r[1] for r in drain(rec) if r[0] == "ai"]
        assert len(ai) == 1
        ts, sym, status, ttft, tok_s, total, wait, tokens, model = ai[0]
        assert (sym, status, model) == ("MSFT", "done", "qwen3-30b-a3b")
        assert ttft is not None and total >= ttft and wait >= 0 and tokens > 0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_portal_counts_requests_rejections_and_cache_hits(settings, rec):
    jm = make_jm(settings, rec)
    httpd = serve(make_handler(PortalApp(settings, jobs=jm, stats=rec)))
    try:
        assert get(httpd, "/")[0].status == 200
        assert get(httpd, "/api/ticker/1BAD")[0].status == 400
        assert get(httpd, "/api/health", method="POST")[0].status == 405
        assert get(httpd, "/", host="evil.example.com")[0].status == 403
        get(httpd, "/api/ticker/MSFT")
        deadline = time.time() + 5
        while json.loads(get(httpd, "/api/ticker/MSFT?poll=1")[1])["status"] != "done" and time.time() < deadline:
            time.sleep(0.02)
        jm._recent.clear()
        r, body = get(httpd, "/api/ticker/MSFT")  # answered from the cache
        assert json.loads(body)["cached"] is True
        rec.flush(final=True)
        s = rec.summary(1)
        assert s["rejections"]["requests"] == 6  # the ?poll=1 requests aren't counted
        assert {r["reason"]: r["n"] for r in s["rejections"]["reasons"]} == \
            {"bad_ticker": 1, "read_only": 1, "foreign_host": 1}
        assert s["sources"]["fresh"] == 1 and s["sources"]["cache"] == 1
        assert s["tickers"]["top"] == [{"ticker": "MSFT", "n": 2, "pct": 100.0}]
        assert s["latency"]["n"] == 1 and s["latency"]["min"] < 1  # the fresh lookup; the cache hit isn't timed
        assert s["cold"]["total"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


def test_only_visitor_actions_are_counted_as_requests(settings, rec):
    """Opening the portal, opening a scan thread and looking up a ticker are a visitor's actions; the files,
    the scan list, the X feed, the AI overview and the deep-dive prompt the page then loads are not."""
    jm = make_jm(settings, rec)
    httpd = serve(make_handler(PortalApp(settings, jobs=jm, stats=rec)))
    try:
        assert get(httpd, "/")[0].status == 200                      # action
        for path in ("/static/app.css", "/static/app.js", "/static/img/logo.png", "/api/scans", "/api/socials",
                     "/manifest.webmanifest"):
            get(httpd, path)                                          # loaded by the page: not counted
        get(httpd, "/api/scans/nope")                                 # action (404: no such scan here)
        get(httpd, "/api/ticker/MSFT")                                # action
        deadline = time.time() + 5
        while json.loads(get(httpd, "/api/ticker/MSFT?poll=1")[1])["status"] != "done" and time.time() < deadline:
            time.sleep(0.02)
        get(httpd, "/api/ticker/MSFT/deepdive")                       # not counted
        get(httpd, "/api/ticker/MSFT/ai")                             # not counted
        rec.flush(final=True)
        s = rec.summary(1)
        assert s["rejections"]["requests"] == 3 and s["rejections"]["total"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


def test_page_polling_is_not_counted_as_requests(settings, rec):
    """Health checks and lookup / AI polls are the open page talking to the server, not a visitor's requests."""
    jm = make_jm(settings, rec)
    httpd = serve(make_handler(PortalApp(settings, jobs=jm, stats=rec)))
    try:
        get(httpd, "/api/ticker/MSFT")  # the lookup: counted
        deadline = time.time() + 5
        polls = 0
        while json.loads(get(httpd, "/api/ticker/MSFT?poll=1")[1])["status"] != "done" and time.time() < deadline:
            polls += 1
            time.sleep(0.02)
        for _ in range(5):
            assert get(httpd, "/api/health")[0].status == 200
        assert get(httpd, "/api/ticker/MSFT/ai?poll=1")[0].status in (200, 404)
        assert get(httpd, "/api/health", method="POST")[0].status == 405  # refused: still counted
        rec.flush(final=True)
        s = rec.summary(1)
        assert s["rejections"]["requests"] == 2 and s["rejections"]["total"] == 1
        assert s["cold"]["total"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


def test_portal_counts_visitors_by_source_ip(settings, rec):
    httpd = serve(make_handler(PortalApp(settings, stats=rec)))
    try:
        get(httpd, "/")                                                     # 127.0.0.1
        get(httpd, "/static/app.css")                                       # same visitor
        get(httpd, "/", headers={"CF-Connecting-IP": "203.0.113.7"})        # through the tunnel
        get(httpd, "/", headers={"CF-Connecting-IP": "not-an-ip"})          # ignored: the peer
        assert get(httpd, "/", host="evil.example.com")[0].status == 403    # refused: not a visitor
        assert get(httpd, "/nope", headers={"CF-Connecting-IP": "198.51.100.9"})[0].status == 404  # nor this
        rec.flush(final=True)
        ids = {v for (v,) in rows(rec, "SELECT visitor FROM visitors")}
        assert ids == {rec._visitor_id("127.0.0.1"), rec._visitor_id("203.0.113.7")}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cf_header_is_only_trusted_from_loopback(settings):
    handler = make_handler(PortalApp(settings))
    h = handler.__new__(handler)
    h.headers = {"CF-Connecting-IP": "203.0.113.7"}
    h.client_address = ("192.168.1.20", 50000)
    assert h._visitor() == "192.168.1.20"  # a LAN device can't claim another IP
    h.client_address = ("127.0.0.1", 50000)
    assert h._visitor() == "203.0.113.7"   # cloudflared on this machine


def test_portal_without_stats_records_nothing(settings):
    httpd = serve(make_handler(PortalApp(settings)))
    try:
        assert get(httpd, "/")[0].status == 200  # app.stats is None: no recorder, no error
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── the stats server ────────────────────────────────────────────────────────
@pytest.fixture
def stats_server(settings, rec):
    rec.query("NVDA", "cache", "done", 0.003)
    rec.flush(final=True)
    httpd = serve(make_stats_handler(StatsApp(settings, rec)))
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_stats_page_and_api(stats_server, settings, rec):
    r, body = get(stats_server, "/")
    assert r.status == 200 and b"Quant Portal" in body and b"/static/stats.js" in body
    assert "default-src 'self'" in r.getheader("Content-Security-Policy")
    for path in ("/static/stats.js", "/static/stats.css", "/static/app.css", "/static/img/logo.png", "/favicon.ico"):
        assert get(stats_server, path)[0].status == 200, path
    r, body = get(stats_server, "/api/stats?days=7")
    s = json.loads(body)
    assert r.status == 200 and r.getheader("Cache-Control") == "no-store"
    assert s["days"] == 7 and s["portal_port"] == settings.port and s["tickers"]["top"][0]["ticker"] == "NVDA"
    assert s["visitors"]["retention_days"] == 365 and s["visitors"]["days"] == []
    assert get(stats_server, "/api/stats?days=x")[0].status == 400
    for path in ("/api/health", "/api/ticker/MSFT", "/api/cache", "/nope"):  # nothing of the portal's
        assert get(stats_server, path)[0].status == 404, path
    assert get(stats_server, "/api/stats", method="POST")[0].status == 405
    rec.flush(final=True)
    assert rows(rec, "SELECT COUNT(*) FROM minutes") == [(0,)]  # the stats page's own requests aren't counted


def test_stats_server_is_lan_only(stats_server):
    assert get(stats_server, "/", host="evil.example.com")[0].status == 403
    assert get(stats_server, "/api/stats", host="192.168.1.20:190")[0].status == 200  # --lan: loopback is fine
    assert get(stats_server, "/", headers={"CF-Connecting-IP": "203.0.113.7"})[0].status == 403  # never via a tunnel


@pytest.mark.parametrize("server, headers, status", [
    (PortalServer, {}, 403),                                    # loopback = where cloudflared connects from
    (PortalServer, {"CF-Connecting-IP": "203.0.113.7"}, 403),
    (LanPeer, {}, 200),                                         # a device on the LAN / tailnet
    (LanPeer, {"CF-Ray": "8c1f2a-SJC"}, 403),                   # a tunnel elsewhere on the LAN
])
def test_public_stats_answer_lan_and_tailscale_only(settings, rec, server, headers, status):
    settings.public = True
    httpd = serve(make_stats_handler(StatsApp(settings, rec)), server)
    try:
        r, body = get(httpd, "/api/stats", host="192.168.1.5:190", headers=headers)
        assert r.status == status
        if status == 200:
            s = json.loads(body)
            assert s["mode"] == "public" and s["portal_port"] is None  # the portal isn't on the LAN
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_public_portal_counts_tunnel_visitors(settings, rec):
    settings.public = True
    httpd = serve(make_handler(PortalApp(settings, stats=rec)))
    try:
        for ip in ("203.0.113.7", "203.0.113.7", "198.51.100.9"):
            assert get(httpd, "/", headers={"CF-Connecting-IP": ip})[0].status == 200
        rec.flush(final=True)
        ids = {v for (v,) in rows(rec, "SELECT visitor FROM visitors")}
        assert ids == {rec._visitor_id("203.0.113.7"), rec._visitor_id("198.51.100.9")}
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── configuration ───────────────────────────────────────────────────────────
def test_stats_with_lan_or_public_only():
    s = Settings()
    assert s.stats_port == 190 and s.stats_days == 30 and not s.stats_enabled
    assert s.stats_bind_host == "127.0.0.1"
    s.lan = True
    assert s.stats_enabled and s.stats_bind_host == "0.0.0.0"
    s.lan, s.public = False, True
    assert s.stats_enabled and s.stats_bind_host == "0.0.0.0" and s.bind_host == "127.0.0.1"  # portal: loopback
    s.stats = False
    assert not s.stats_enabled


def test_cli_flags(monkeypatch):
    s, _ = parse_args(["--lan"])
    assert s.stats_enabled and s.stats_port == 190
    s, _ = parse_args(["--lan", "--stats-port", "8190"])
    assert s.stats_port == 8190
    s, _ = parse_args(["--lan", "--no-stats"])
    assert not s.stats_enabled
    s, _ = parse_args([])
    assert not s.stats_enabled  # loopback: no stats server
    s, _ = parse_args(["--public"])
    assert s.stats_enabled and s.bind_host == "127.0.0.1" and s.stats_bind_host == "0.0.0.0"
    with pytest.raises(SystemExit):
        parse_args(["--lan", "--stats-port", "8765"])
    monkeypatch.setenv("LYNCH_UI_STATS_PORT", "9190")
    monkeypatch.setenv("LYNCH_UI_STATS_DAYS", "45")
    monkeypatch.setenv("LYNCH_UI_STATS_VISITOR_DAYS", "400")
    s, _ = parse_args(["--lan"])
    assert (s.stats_port, s.stats_days, s.stats_visitor_days) == (9190, 45, 400)


@pytest.mark.parametrize("mode", ["lan", "public"])
def test_build_app_records_with_lan_or_public(settings, mode):
    assert build_app(settings, with_search=False, with_ai=False).stats is None
    setattr(settings, mode, True)
    app = build_app(settings, with_search=False, with_ai=False)
    try:
        assert isinstance(app.stats, StatsRecorder) and app.stats.path.endswith("stats.sqlite3")
    finally:
        app.stats.close()


def test_bind_failure_leaves_the_portal_running(settings, rec, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        settings.host, settings.stats_port = "127.0.0.1", busy.getsockname()[1]
        assert start_stats_server(settings, rec) is None
    assert "stats page not started" in capsys.readouterr().out


def test_stats_server_starts(settings, rec):
    settings.host, settings.stats_port = "127.0.0.1", 0
    httpd = start_stats_server(settings, rec)
    try:
        assert get(httpd, "/api/stats")[0].status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
