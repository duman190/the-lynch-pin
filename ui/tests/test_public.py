"""--public: served to the internet through a local tunnel (cloudflared) — one analysis per visitor."""
import http.client
import json
import threading
import time

import pytest

from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.server import PortalApp, PortalServer, make_handler, parse_args
from ui.tests import fakes


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    return s


@pytest.fixture
def gate():
    g = fakes.Gate()
    yield g
    g.release.set()


def serve(settings, gate, public=True, max_per_client=1):
    settings.public = public
    b = fakes.backends(engine=fakes.AnyEngine, edge=gate.wrap(fakes.EDGE))
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=b), store=DayStore(), workers=3,
                    deadline=30, max_per_client=max_per_client if public else None)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, jm


def get(httpd, path, ip=None, host="lynch.example.com"):
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    headers = {"Host": host}
    if ip is not None:
        headers["CF-Connecting-IP"] = ip
    c.request("GET", path, headers=headers)
    r = c.getresponse()
    body = json.loads(r.read() or b"null")
    c.close()
    return r, body


def stop(httpd, jm):
    httpd.shutdown()
    httpd.server_close()
    jm.shutdown()


# ── flag ────────────────────────────────────────────────────────────────────
def test_public_flag_stays_on_loopback():
    s, _ = parse_args(["--public"])
    assert s.public is True and s.host == "127.0.0.1" and s.lan is False
    for bad in (["--public", "--lan"], ["--public", "--host", "0.0.0.0"], ["--public", "--host", "192.168.1.5"]):
        with pytest.raises(SystemExit):
            parse_args(bad)


def test_public_env(monkeypatch):
    monkeypatch.setenv("LYNCH_UI_PUBLIC", "1")
    assert parse_args([])[0].public is True


# ── one analysis per visitor ────────────────────────────────────────────────
def test_one_analysis_at_a_time_per_visitor(settings, gate):
    httpd, jm = serve(settings, gate)
    try:
        r, a = get(httpd, "/api/ticker/MSFT", ip="203.0.113.5")  # public host name is fine
        assert r.status == 200 and a["status"] in ("queued", "running")
        assert gate.entered.wait(5)
        r, busy = get(httpd, "/api/ticker/AAPL", ip="203.0.113.5")
        assert r.status == 429 and r.getheader("Retry-After") == "5"
        assert busy["status"] == "busy" and "one analysis at a time per visitor" in busy["error"] and "MSFT" in busy["error"]
        r, other = get(httpd, "/api/ticker/AAPL", ip="198.51.100.7")  # another visitor
        assert r.status == 200 and other["status"] in ("queued", "running")
        r, same = get(httpd, "/api/ticker/MSFT", ip="198.51.100.9")  # joining a running lookup is free
        assert r.status == 200 and same["status"] == "running"
        r, poll = get(httpd, "/api/ticker/MSFT?poll=1", ip="203.0.113.5")  # polls are never limited
        assert r.status == 200
        gate.release.set()
        t0 = time.time()
        while get(httpd, "/api/ticker/MSFT?poll=1", ip="203.0.113.5")[1]["status"] != "done":
            assert time.time() - t0 < 10
            time.sleep(0.02)
        r, nxt = get(httpd, "/api/ticker/NVDA", ip="203.0.113.5")  # free again once it finished
        assert r.status == 200 and nxt["status"] in ("queued", "running", "done")
        r, cached = get(httpd, "/api/ticker/MSFT", ip="203.0.113.5")  # cache hits are never limited
        assert r.status == 200 and cached["cached"] is True
    finally:
        stop(httpd, jm)


@pytest.mark.parametrize("header", [None, "", "not-an-ip", "1.2.3.4, 5.6.7.8", "1.2.3.4 <script>"])
def test_missing_or_garbage_cf_ip_falls_back_to_the_tunnel_address(settings, gate, header):
    httpd, jm = serve(settings, gate)
    try:
        assert get(httpd, "/api/ticker/MSFT", ip=header)[0].status == 200
        assert gate.entered.wait(5)
        r, busy = get(httpd, "/api/ticker/AAPL", ip=header)  # same (tunnel) identity → limited
        assert r.status == 429 and busy["status"] == "busy"
    finally:
        stop(httpd, jm)


def test_without_public_cf_header_is_ignored_and_nothing_is_limited(settings, gate):
    httpd, jm = serve(settings, gate, public=False)
    try:
        assert get(httpd, "/api/ticker/MSFT", ip="203.0.113.5")[0].status == 403  # foreign Host refused
        assert get(httpd, "/api/ticker/MSFT", ip="203.0.113.5", host="localhost")[0].status == 200
        assert gate.entered.wait(5)
        r, b = get(httpd, "/api/ticker/AAPL", ip="203.0.113.5", host="localhost")
        assert r.status == 200 and b["status"] in ("queued", "running")
    finally:
        stop(httpd, jm)


# ── less metadata ───────────────────────────────────────────────────────────
def test_public_health_hides_other_visitors_and_local_details(settings, gate):
    httpd, jm = serve(settings, gate)
    try:
        get(httpd, "/api/ticker/MSFT", ip="203.0.113.5")
        assert gate.entered.wait(5)
        r, h = get(httpd, "/api/health", ip="198.51.100.7")
        assert r.status == 200 and h["features"]["search"] is True and "lan" not in h
        assert "top" not in h["cache"] and "running" not in h["cache"] and "size" in h["cache"]
        assert get(httpd, "/api/cache", ip="198.51.100.7")[0].status == 404
    finally:
        stop(httpd, jm)


def test_public_health_strips_the_local_model_address():
    s = Settings()
    s.public = True

    class LLM:
        def status(self, block=False):
            return {"available": False, "reason": "LM Studio not reachable at http://127.0.0.1:1234",
                    "model": "qwen/qwen3", "model_short": "qwen3", "ctx": 65536, "warning": "x"}
    h = PortalApp(s, jobs=None, llm=LLM()).health()
    assert h["ai"] == {"available": False, "model": "qwen/qwen3", "model_short": "qwen3", "ctx": 65536,
                       "reason": "local model unavailable"}
