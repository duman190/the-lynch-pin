"""Step 3: staged analysis, job manager, ticker/plot API."""
import datetime as dt
import http.client
import json
import os
import threading
import time

import pytest

from ui.analysis import TickerAnalyzer, format_stats
from ui.config import Settings
from ui.jobs import DayStore, JobManager, public_view
from ui.server import PortalApp, PortalServer, make_handler
from ui.tests import fakes


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    return s


def wait_final(jm, sym, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        snap = jm.request(sym)
        if snap["status"] in ("done", "nodata", "error"):
            return snap
        time.sleep(0.02)
    raise AssertionError(f"{sym} never finished: {snap}")


# ── analysis ────────────────────────────────────────────────────────────────
def test_format_stats_parses_and_derives_sd():
    st = format_stats(fakes.MSFT_ROW)
    assert st["growth_pct"] == 13.0 and st["Base"] == 13.5 and st["Bear"] == 12.1
    assert st["SD"] == pytest.approx(abs((1.68 - 1.72) / -0.19))
    assert st["display"]["Base"] == "13.5%"
    assert format_stats(dict(fakes.MSFT_ROW, Dev_SD=0.0))["SD"] is None


def test_full_pipeline(settings):
    events = []
    a = TickerAnalyzer(settings, backends=fakes.backends())
    r = a.run("MSFT", on_stage=lambda n, s, d: events.append((n, s)))
    assert r["status"] == "done" and r["name"] == "Microsoft Corporation" and r["price"] == 430.0
    assert r["stages"] == {s: "done" for s in ("stats", "grades", "technicals", "edge", "plot")}
    assert events[0] == ("stats", "running") and events[-1] == ("plot", "done")
    assert r["flagged"] is False
    assert r["income"]["grade"] == "A+"
    assert [i["signal"] for i in r["income"]["items"]] == ["good", "neutral", "good", "na", "bad"]
    assert r["credit"]["metrics"][3] == {"label": "Svc/FCF%", "value": None}
    assert r["technicals"]["accumulation_zone"] == [419.0, 440.0]
    assert r["edge"]["best_edge"] == "BULL"
    assert os.path.exists(r["plot_file"]) and r["plot_url"].startswith("/plots/MSFT.png?v=")
    assert "_ai_inputs" not in public_view(r) and "plot_file" not in public_view(r)


def test_flagged_row(settings):
    rows = dict(fakes.FakeEngine.rows, RISK=dict(fakes.MSFT_ROW, Ticker="RISK*"))
    infos = dict(fakes.FakeEngine.infos, RISK=fakes.MSFT_INFO)
    eng = type("E", (fakes.FakeEngine,), {"rows": rows, "infos": infos})
    r = TickerAnalyzer(settings, backends=fakes.backends(engine=eng)).run("RISK")
    assert r["flagged"] is True and r["status"] == "done"
    assert os.path.basename(r["plot_file"]) == "RISK_valuation.png"


@pytest.mark.parametrize("info,reason", [
    ({"quoteType": "ETF", "regularMarketPrice": 500.0}, "not an operating company"),
    (dict(fakes.MSFT_INFO, forwardPE=-3.0), "forward earnings"),
    (dict(fakes.MSFT_INFO, forwardPE=None), "forward earnings"),
    (fakes.MSFT_INFO, "growth estimate"),  # engine returns None
])
def test_nodata_still_runs_grades_and_technicals(settings, info, reason):
    eng = type("E", (fakes.FakeEngine,), {"infos": {"ZZZ": info}, "rows": {}})
    r = TickerAnalyzer(settings, backends=fakes.backends(engine=eng)).run("ZZZ")
    assert r["status"] == "nodata" and reason in r["reason"]
    assert r["stages"]["stats"] == "skipped" and r["stages"]["plot"] == "skipped"
    assert r["stages"]["technicals"] == "done" and r["technicals"]["signal"] == "BULLISH"


def test_unknown_symbol_skips_everything(settings):
    eng = type("E", (fakes.FakeEngine,), {"infos": {"QQQQQ": {"trailingPegRatio": None}}, "rows": {}})
    r = TickerAnalyzer(settings, backends=fakes.backends(engine=eng)).run("QQQQQ")
    assert r["status"] == "nodata" and r["reason"] == "unknown symbol"
    assert set(r["stages"].values()) == {"skipped"}


def test_empty_info_is_transient_error(settings):
    eng = type("E", (fakes.FakeEngine,), {"infos": {}, "rows": {}})
    r = TickerAnalyzer(settings, backends=fakes.backends(engine=eng)).run("MSFT")
    assert r["status"] == "error" and "unavailable" in r["reason"]


def test_stage_failure_is_isolated(settings):
    def boom(*a):
        raise RuntimeError("yahoo 429")
    r = TickerAnalyzer(settings, backends=fakes.backends(technicals=boom)).run("MSFT")
    assert r["status"] == "done" and r["stages"]["technicals"] == "error"
    assert r["stages"]["edge"] == "done" and r["stages"]["plot"] == "done"


# ── job manager ─────────────────────────────────────────────────────────────
def make_jm(settings, store=None, **kw):
    kw.setdefault("deadline", 30)
    return JobManager(settings, analyzer=TickerAnalyzer(settings, backends=kw.pop("backends", fakes.backends())),
                      store=store or DayStore(), **kw)


def test_request_runs_then_serves_from_store(settings):
    jm = make_jm(settings)
    first = jm.request("MSFT")
    assert first["status"] in ("queued", "running", "done") and first["cached"] is False
    snap = wait_final(jm, "MSFT")
    assert snap["status"] == "done" and snap["data"]["stats"]["PEG"] == 1.68
    jm._recent.clear()
    again = jm.request("MSFT")
    assert again["cached"] is True and again["status"] == "done"
    assert fakes.FakeEngine.calls.count("MSFT") >= 1
    n = fakes.FakeEngine.calls.count("MSFT")
    jm.request("MSFT")
    assert fakes.FakeEngine.calls.count("MSFT") == n  # no re-analysis
    jm.shutdown()


def test_inflight_dedup_and_queue_position(settings):
    gate = fakes.Gate()
    jm = make_jm(settings, backends=fakes.backends(edge=gate.wrap(fakes.EDGE)))
    a = jm.request("MSFT")
    assert gate.entered.wait(5)
    other = type("E", (fakes.FakeEngine,), {})  # noqa: F841
    fakes.FakeEngine.infos = dict(fakes.FakeEngine.infos, AAPL=fakes.MSFT_INFO)
    fakes.FakeEngine.rows = dict(fakes.FakeEngine.rows, AAPL=dict(fakes.MSFT_ROW, Ticker="AAPL"))
    b = jm.request("AAPL")
    assert b["status"] == "queued" and b["queue_position"] == 2
    again = jm.request("MSFT", refresh=True)  # refresh ignored while in flight
    assert again["status"] == "running" and again["stage"] == "edge"
    assert again["data"]["stages"]["grades"] == "done"  # progressive partial data
    assert a["ticker"] == "MSFT"
    gate.release.set()
    assert wait_final(jm, "MSFT")["status"] == "done"
    assert wait_final(jm, "AAPL")["status"] == "done"
    jm.shutdown()


def test_queue_full_returns_busy(settings):
    gate = fakes.Gate()
    jm = make_jm(settings, backends=fakes.backends(edge=gate.wrap(fakes.EDGE)), max_queue=1)
    fakes.FakeEngine.infos = dict(fakes.FakeEngine.infos, A=fakes.MSFT_INFO, B=fakes.MSFT_INFO)
    jm.request("MSFT")
    assert gate.entered.wait(5)
    assert jm.request("A")["status"] == "queued"
    busy = jm.request("B")
    assert busy["status"] == "busy" and busy["retry_after"] > 0
    gate.release.set()
    jm.shutdown()


def test_errors_are_not_cached_but_visible_to_pollers(settings):
    eng = type("E", (fakes.FakeEngine,), {"infos": {}, "rows": {}})
    store = DayStore()
    jm = make_jm(settings, store=store, backends=fakes.backends(engine=eng))
    snap = wait_final(jm, "MSFT")
    assert snap["status"] == "error" and "unavailable" in snap["error"]
    assert store.peek("MSFT") is None
    assert jm.request("MSFT")["status"] == "error"  # recent map, no immediate re-run
    assert jm.request("MSFT", refresh=True)["status"] in ("queued", "running", "error")
    jm.shutdown()


def test_nodata_is_cached(settings):
    eng = type("E", (fakes.FakeEngine,), {"infos": {"SPY": {"quoteType": "ETF", "regularMarketPrice": 1.0}},
                                           "rows": {}})
    store = DayStore()
    jm = make_jm(settings, store=store, backends=fakes.backends(engine=eng))
    assert wait_final(jm, "SPY")["status"] == "nodata"
    assert store.peek("SPY")["status"] == "nodata"
    jm.shutdown()


def test_watchdog_times_out_hung_job_and_recovers(settings):
    gate = fakes.Gate()
    calls = {"n": 0}

    def edge(*a):
        calls["n"] += 1
        if calls["n"] == 1:  # first job hangs until released
            gate.entered.set()
            gate.release.wait(10)
        return fakes.EDGE

    store = DayStore()
    jm = make_jm(settings, store=store, backends=fakes.backends(edge=edge), deadline=0.5)
    jm.request("MSFT")
    assert gate.entered.wait(5)
    snap = wait_final(jm, "MSFT")
    assert snap["status"] == "error" and "timed out" in snap["error"]
    fakes.FakeEngine.infos = dict(fakes.FakeEngine.infos, NVDA=fakes.MSFT_INFO)
    fakes.FakeEngine.rows = dict(fakes.FakeEngine.rows, NVDA=dict(fakes.MSFT_ROW, Ticker="NVDA"))
    assert wait_final(jm, "NVDA")["status"] == "done"  # fresh worker took over
    gate.release.set()
    time.sleep(0.3)
    assert store.peek("MSFT") is None  # orphan's late result discarded
    jm.shutdown()


def test_day_store_rollover():
    day = {"d": dt.date(2026, 9, 27)}
    s = DayStore(today=lambda: day["d"])
    s.put("MSFT", {"x": 1})
    assert s.get("MSFT") == {"x": 1}
    day["d"] = dt.date(2026, 9, 28)
    assert s.get("MSFT") is None


def test_plot_path_falls_back_to_disk(settings):
    jm = make_jm(settings, start=False)
    d = jm.analyzer.plot_dir()
    os.makedirs(d, exist_ok=True)
    fakes.FakeVisualizer(d).plot_ticker_distribution({"Ticker": "AMD"})
    assert jm.plot_path("AMD").endswith("AMD_valuation.png")
    assert jm.plot_path("AMD", preview=True).endswith(".jpg")
    assert jm.plot_path("NOPE") is None


# ── HTTP ────────────────────────────────────────────────────────────────────
@pytest.fixture
def api(settings):
    jm = make_jm(settings)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, jm
    httpd.shutdown()
    httpd.server_close()
    jm.shutdown()


def http_get(httpd, path):
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_ticker_api_end_to_end(api):
    httpd, jm = api
    r, body = http_get(httpd, "/api/ticker/msft")
    assert r.status == 200 and json.loads(body)["ticker"] == "MSFT"
    snap = wait_final(jm, "MSFT")
    data = snap["data"]
    r, png = http_get(httpd, data["plot_url"])
    assert r.status == 200 and r.getheader("Content-Type") == "image/png" and png[:4] == b"\x89PNG"
    r, jpg = http_get(httpd, data["plot_preview_url"])
    assert r.status == 200 and jpg[:2] == b"\xff\xd8"
    r, body = http_get(httpd, "/api/health")
    h = json.loads(body)
    assert h["features"]["search"] is True and h["cache"]["size"] == 1


@pytest.mark.parametrize("path", ["/api/ticker/", "/api/ticker/1ABC", "/api/ticker/TOOLONGTICKER",
                                  "/api/ticker/MS%20FT", "/api/ticker/MSFT/x", "/api/ticker/MSFT/ai/x",
                                  "/api/ticker/..%2F..%2Fetc"])
def test_ticker_api_rejects_bad_symbols(api, path):
    r, _ = http_get(api[0], path)
    assert r.status == 400


def test_plot_routes_404(api):
    for p in ["/plots/MSFT.png", "/plots/../x.png", "/plots/MSFT.gif", "/plots/1BAD.png"]:
        assert http_get(api[0], p)[0].status == 404


def test_busy_maps_to_429(settings):
    gate = fakes.Gate()
    jm = make_jm(settings, backends=fakes.backends(edge=gate.wrap(fakes.EDGE)), max_queue=0)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        r, body = http_get(httpd, "/api/ticker/MSFT")
        assert r.status == 429 and r.getheader("Retry-After") == "10"
        assert json.loads(body)["status"] == "busy"
    finally:
        gate.release.set()
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


@pytest.fixture(autouse=True)
def _reset_fakes():
    infos, rows = dict(fakes.FakeEngine.infos), dict(fakes.FakeEngine.rows)
    fakes.FakeEngine.calls = []
    yield
    fakes.FakeEngine.infos, fakes.FakeEngine.rows = infos, rows


def test_degraded_history_is_flagged_and_not_cached(settings):
    rows = {"MSFT": dict(fakes.MSFT_ROW, PEG=1.68, Mean=1.68, Dev_SD=0.0, Ticker="MSFT*")}
    eng = type("E", (fakes.FakeEngine,), {"rows": rows})
    store = DayStore()
    jm = make_jm(settings, store=store, backends=fakes.backends(engine=eng))
    snap = wait_final(jm, "MSFT")
    assert snap["status"] == "done" and snap["data"]["stats"]["history"] == "unavailable"
    assert store.peek("MSFT") is None  # a retry must recompute
    jm.shutdown()


def test_snapshot_is_isolated_from_worker_mutation(settings):
    gate = fakes.Gate()
    jm = make_jm(settings, backends=fakes.backends(edge=gate.wrap(fakes.EDGE)))
    jm.request("MSFT")
    assert gate.entered.wait(5)
    snap = jm.request("MSFT")
    before = dict(snap["data"]["stage_ms"])
    gate.release.set()
    wait_final(jm, "MSFT")
    assert snap["data"]["stage_ms"] == before  # worker kept writing into its own dict
    jm.shutdown()


# ── growth enrichment flag ──────────────────────────────────────────────────
def test_enrich_auto_follows_fmp_key(monkeypatch):
    s = Settings()
    assert s.enrich == "auto" and s.enrich_enabled is False
    monkeypatch.setenv("FMP_API_KEY", "k")
    assert s.enrich_enabled is True
    s.enrich = "off"
    assert s.enrich_enabled is False


@pytest.mark.parametrize("raw,mode", [("1", "on"), ("yes", "on"), ("0", "off"), ("off", "off"), ("auto", "auto"),
                                      ("junk", "auto")])
def test_enrich_env_values(monkeypatch, raw, mode):
    monkeypatch.setenv("LYNCH_UI_ENRICH", raw)
    assert Settings().enrich == mode


def test_cli_enrich_and_cache_defaults():
    from ui.server import parse_args
    assert (parse_args([])[0].enrich, parse_args([])[0].cache_capacity) == ("auto", 250)
    s, _ = parse_args(["--enrich", "on", "--cache-size", "40"])
    assert (s.enrich, s.cache_capacity) == ("on", 40)


def test_growth_marked_enriched_with_fmp_key(settings, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")
    fakes.FakeEngine.enrich_calls = []
    r = TickerAnalyzer(settings, backends=fakes.backends()).run("MSFT")
    assert fakes.FakeEngine.enrich_calls == [("MSFT", True)] and r["growth_enriched"] is True


def test_growth_marked_not_enriched_without_key(settings):
    fakes.FakeEngine.enrich_calls = []
    r = TickerAnalyzer(settings, backends=fakes.backends()).run("MSFT")
    assert fakes.FakeEngine.enrich_calls == [("MSFT", False)] and r["growth_enriched"] is False


def test_enrich_requested_but_fmp_had_no_estimates(settings, monkeypatch):
    class NoFMP(fakes.FakeEngine):
        def get_ticker_stats(self, enrich=False):
            row = super().get_ticker_stats(enrich)
            self._growth_sources = ["yahoo_peg"]  # FMP returned nothing usable
            return row

    monkeypatch.setenv("FMP_API_KEY", "k")
    r = TickerAnalyzer(settings, backends=fakes.backends(engine=NoFMP)).run("MSFT")
    assert r["growth_enriched"] is False
