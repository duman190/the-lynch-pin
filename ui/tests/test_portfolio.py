"""Portfolio Analyzer (ui/portfolio.py, /api/portfolio): positions in % or $ / shares, today's ticker analyses rolled up
like main.py --portfolio, the weight donut (native, like the per-ticker chart) and the X-ray's AI verdict."""
import http.client
import json
import os
import threading
import time

import numpy as np
import pytest
from PIL import Image

from engine.ai_research import PORTFOLIO_VERDICT_TASK, LynchPinResearcher
from engine.portfolio import weighted_metrics
from ui import fastchart
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.portfolio import (PortfolioAnalyzer, PortfolioError, market_weights, parse_positions, parse_verdict,
                          portfolio_key)
from ui.server import PortalApp, PortalServer, make_handler
from ui.tests import fakes

native = pytest.mark.skipif(not fastchart.donut_available(), reason="native renderer not built (ui/native)")

ROWS = {"MSFT": fakes.MSFT_ROW,
        "AAPL": dict(fakes.MSFT_ROW, Ticker="AAPL", PE=33.0, FwdPE=29.0, PEG=2.2, Mean=1.9, Dev_SD=0.6, Base="9.0%"),
        "KO": dict(fakes.MSFT_ROW, Ticker="KO", PE=24.0, FwdPE=21.0, PEG=3.1, Mean=2.8, Dev_SD=0.3, Base="7.5%")}
INFOS = {"MSFT": fakes.MSFT_INFO, "AAPL": dict(fakes.MSFT_INFO, currentPrice=200.0, longName="Apple Inc."),
         "KO": dict(fakes.MSFT_INFO, currentPrice=50.0, longName="Coca-Cola"),
         "SPY": dict(fakes.MSFT_INFO, currentPrice=500.0, forwardPE=None, longName="SPDR S&P 500")}
BENCH = [("QQQ (5.0Y)", 17.8), ("S&P 500 (5.0Y)", 14.1)]


class Engine(fakes.FakeEngine):
    infos, rows = INFOS, ROWS


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    return s


@pytest.fixture
def jm(settings):
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends(engine=Engine)),
                    store=DayStore(), deadline=30)
    yield jm
    jm.shutdown()


def run(pf, positions, timeout=10):
    snap = pf.analyze(positions)
    t0 = time.time()
    while snap["status"] == "running":
        assert time.time() - t0 < timeout, snap
        time.sleep(0.02)
        snap = pf.analyze(positions, poll=True)
    return snap


# ── input ───────────────────────────────────────────────────────────────────
def test_parse_percent_weights():
    p = parse_positions("msft:60, AAPL:40", "pct")
    assert list(p.items()) == [("MSFT", ("pct", 60.0)), ("AAPL", ("pct", 40.0))]
    assert parse_positions("MSFT:33.3,AAPL:33.3,KO:33.4", "pct")["KO"] == ("pct", 33.4)


@pytest.mark.parametrize("spec,mode,msg", [
    ("MSFT:60,AAPL:30", "pct", "add up to 90%"),
    ("", "pct", "at least one"),
    ("1BAD:100", "pct", "invalid ticker"),
    ("MSFT:abc", "pct", "invalid amount"),
    ("MSFT:$100", "pct", "% weight"),
    ("MSFT:100", "amt", "dollars ($) or shares"),
    ("MSFT:$0", "amt", "positive"),
    ("MSFT:$100,MSFT:2sh", "amt", "different units"),
    (",".join(f"T{i}:$1" for i in range(26)), "amt", "at most 25"),
    ("MSFT:100", "weird", "mode"),
])
def test_parse_rejects(spec, mode, msg):
    with pytest.raises(PortfolioError, match=msg.replace("(", r"\(").replace(")", r"\)").replace("$", r"\$")):
        parse_positions(spec, mode)


def test_amounts_dollars_and_shares():
    p = parse_positions("MSFT:$4300,AAPL:10sh,MSFT:$4300", "amt")
    assert p["MSFT"] == ("usd", 8600.0) and p["AAPL"] == ("sh", 10.0)
    w, unvalued = market_weights(p, {"AAPL": 200.0})
    assert list(w) == ["MSFT", "AAPL"] and w["MSFT"] == pytest.approx(8600 / 10600)
    assert not unvalued
    w, unvalued = market_weights(p, {})
    assert list(w) == ["MSFT"] and unvalued == {"AAPL": "no price to value the shares"}


def test_key_depends_on_weights_only():
    import datetime as dt
    day = dt.date(2026, 11, 15)
    a, _ = market_weights(parse_positions("MSFT:$6000,AAPL:$4000", "amt"), {})
    b, _ = market_weights(parse_positions("AAPL:40,MSFT:60", "pct"), {})
    assert portfolio_key(a, day) == portfolio_key(b, day)
    assert portfolio_key(a, day) != portfolio_key(a, dt.date(2026, 11, 16))


def test_parse_verdict():
    text = ("SENTIMENT: Priced for GARP, heavy in $MSFT — watch the cloud capex.\n\nPORTFOLIO:\n"
            "🐂 Bull: MSFT compounds at 13%.\nIt keeps going.\n\n🐻 Bear: Concentration risk.")
    v = parse_verdict(text)
    assert v["verdict"] == "Priced for GARP, heavy in MSFT — watch the cloud capex."
    assert v["bull"] == "MSFT compounds at 13%.\nIt keeps going." and v["bear"] == "Concentration risk."
    assert parse_verdict("SENTIMENT: halfway") == {"verdict": "halfway", "bull": None, "bear": None}


def test_portfolio_prompt_is_the_xrays_verdict():
    rows = [dict(ROWS["MSFT"], Weight=0.6), dict(ROWS["AAPL"], Weight=0.4)]
    p = LynchPinResearcher.build_portfolio_prompt(rows, "  Weighted PE: 30.0", ticker_briefs={"AAPL": "AAPL brief"})
    assert PORTFOLIO_VERDICT_TASK in p and "  Weighted PE: 30.0" in p
    assert "- MSFT [60.0% of portfolio]: PE 28.7" in p and "- AAPL [40.0% of portfolio]:\nAAPL brief" in p
    assert "SECTION 2" not in p  # no per-ticker posts: each position has its own AI overview
    # the scan's thread asks for the same Section 1
    assert PORTFOLIO_VERDICT_TASK in LynchPinResearcher.build_prompt(rows, portfolio_summary="x")


# ── roll-up ─────────────────────────────────────────────────────────────────
def fake_plot(calls):
    def plot(weights, wm, bench, out_dir, filename):
        calls.append((dict(weights), wm, bench))
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, filename)
        Image.new("RGB", (40, 20), "#121212").save(path)
        return path, None
    return plot


def test_rollup_matches_engine_and_uses_the_cache(settings, jm):
    calls = []
    pf = PortfolioAnalyzer(settings, jm, benchmarks=lambda: BENCH, plot=fake_plot(calls))
    r = run(pf, parse_positions("MSFT:$4300,AAPL:20sh,KO:$1700", "amt"))
    assert r["status"] == "done" and len(r["key"]) == 16
    w = {"MSFT": 4300 / 10000, "AAPL": 4000 / 10000, "KO": 1700 / 10000}
    assert [p["ticker"] for p in r["positions"]] == ["MSFT", "AAPL", "KO"]
    assert [p["weight"] for p in r["positions"]] == pytest.approx(list(w.values()))
    rows = [dict(ROWS[s], Weight=w[s]) for s in w]
    expect = weighted_metrics(rows, w, {s: fakes.INCOME for s in w}, {s: fakes.CREDIT for s in w})
    for k in ("PE", "FwdPE", "PEG", "Dev_SD", "Base", "Bull", "Bear"):
        assert r["metrics"][k] == pytest.approx(expect[k]), k
    assert r["metrics"]["IncomeGrade"] == "A+" and r["metrics"]["CreditRating"] == "AAA"
    assert calls[0][0] == pytest.approx(w) and calls[0][2] == BENCH
    assert r["chart"]["url"].startswith(f"/portfolio/{r['key']}.png?v=")
    assert pf.plot_path(r["key"]) and pf.plot_path("../../etc") is None
    assert "$" not in json.dumps(r)  # no dollar amounts leave the analyzer
    # a second look at the same weights: no new analyses, no new chart
    n = len(Engine.calls)
    again = run(pf, parse_positions("MSFT:43,AAPL:40,KO:17", "pct"))
    assert again["key"] == r["key"] and len(Engine.calls) == n and len(calls) == 1


def test_positions_without_garp_data_are_listed_not_weighted(settings, jm):
    pf = PortfolioAnalyzer(settings, jm, benchmarks=lambda: BENCH, plot=fake_plot([]))
    r = run(pf, parse_positions("MSFT:80,SPY:20", "pct"))
    assert r["status"] == "done"
    spy = next(p for p in r["positions"] if p["ticker"] == "SPY")
    assert spy["garp"] is False and spy["weight"] == pytest.approx(0.2) and spy["reason"] == "negative or absent forward earnings"
    assert r["metrics"]["Coverage"] == pytest.approx(0.8) and r["metrics"]["Positions"] == 1
    assert "Excluded" in r["summary"] and "SPY (20.0%)" in r["summary"]
    none = run(pf, parse_positions("SPY:100", "pct"))
    assert none["status"] == "nodata" and none["positions"][0]["ticker"] == "SPY"


class FakeLLM:
    def __init__(self, reply="SENTIMENT: Fairly priced.\n\nPORTFOLIO:\n🐂 Bull: Quality.\n\n🐻 Bear: Concentration."):
        self.reply, self.prompts = reply, []

    def status(self, **kw):
        return {"available": True, "model": "fake"}

    def generate(self, prompt, max_tokens=None, on_delta=None, cancelled=None, on_rewind=None):
        self.prompts.append(prompt)
        for chunk in (self.reply[:10], self.reply[10:]):
            on_delta(None, chunk, {})
        return self.reply, {"model": "fake-model"}


def wait_ai(pf, key, timeout=5):
    t0 = time.time()
    while True:
        ai = pf.request_ai(key)
        if ai["status"] in ("done", "error", "unavailable"):
            return ai
        assert time.time() - t0 < timeout, ai
        time.sleep(0.02)


def test_ai_verdict(settings, jm):
    llm = FakeLLM()
    pf = PortfolioAnalyzer(settings, jm, llm=llm, benchmarks=lambda: BENCH, plot=fake_plot([]))
    r = run(pf, parse_positions("MSFT:60,AAPL:40", "pct"))
    assert r["ai"] is True
    ai = wait_ai(pf, r["key"])
    assert ai["status"] == "done" and ai["model"] == "fake-model"
    assert (ai["verdict"], ai["bull"], ai["bear"]) == ("Fairly priced.", "Quality.", "Concentration.")
    assert len(llm.prompts) == 1 and PORTFOLIO_VERDICT_TASK in llm.prompts[0]
    assert "[60.0% of portfolio]" in llm.prompts[0] and "Microsoft Corporation" in llm.prompts[0]  # portal briefs
    assert wait_ai(pf, r["key"])["status"] == "done" and len(llm.prompts) == 1  # one overview per portfolio
    assert pf.request_ai("0123456789abcdef")["need_analysis"] is True


def test_ai_off_and_unavailable(settings, jm):
    pf = PortfolioAnalyzer(settings, jm, benchmarks=lambda: BENCH, plot=fake_plot([]))
    r = run(pf, parse_positions("MSFT:100", "pct"))
    assert r["ai"] is False and pf.request_ai(r["key"])["status"] == "unavailable"

    class Down(FakeLLM):
        def status(self, **kw):
            return {"available": False}
    pf = PortfolioAnalyzer(settings, jm, llm=Down(), benchmarks=lambda: BENCH, plot=fake_plot([]))
    r = run(pf, parse_positions("MSFT:100", "pct"))
    assert wait_ai(pf, r["key"])["status"] == "unavailable"


# ── HTTP ────────────────────────────────────────────────────────────────────
@pytest.fixture
def api(settings, jm):
    pf = PortfolioAnalyzer(settings, jm, llm=FakeLLM(), benchmarks=lambda: BENCH, plot=fake_plot([]))
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm, portfolio=pf)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def get(httpd, path):
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, json.loads(body) if r.getheader("Content-Type", "").startswith("application/json") else body


def test_api_end_to_end(api, capsys):
    r, snap = get(api, "/api/portfolio?m=amt&p=MSFT:%244300,AAPL:20sh")
    assert r.status == 200
    t0 = time.time()
    while snap["status"] == "running":
        assert time.time() - t0 < 10
        time.sleep(0.05)
        r, snap = get(api, "/api/portfolio?m=amt&p=MSFT:%244300,AAPL:20sh&poll=1")
    assert snap["status"] == "done", snap
    r, png = get(api, snap["chart"]["url"])
    assert r.status == 200 and r.getheader("Content-Type") == "image/png"
    r, jpg = get(api, snap["chart"]["preview"])
    assert r.status == 200 and r.getheader("Content-Type") == "image/jpeg"
    t0 = time.time()
    while True:
        r, ai = get(api, f"/api/portfolio/ai?k={snap['key']}")
        if ai["status"] == "done":
            break
        assert time.time() - t0 < 5, ai
        time.sleep(0.05)
    assert ai["verdict"] == "Fairly priced."
    log = capsys.readouterr().err
    assert "/api/portfolio?…" in log and "4300" not in log  # amounts never reach the access log


@pytest.mark.parametrize("path,status", [
    ("/api/portfolio?m=pct&p=MSFT:50", 400), ("/api/portfolio?m=amt&p=MSFT:1", 400),
    ("/api/portfolio/ai?k=nothex", 400), ("/portfolio/0123456789abcdef.png", 404), ("/portfolio/../x.png", 404),
])
def test_api_rejects(api, path, status):
    r, body = get(api, path)
    assert r.status == status


def test_api_without_search(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(s)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert get(httpd, "/api/portfolio?m=pct&p=MSFT:100")[0].status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── the donut ───────────────────────────────────────────────────────────────
WEIGHTS = {"MSFT": 0.42, "AAPL": 0.25, "NVDA": 0.15, "GOOGL": 0.10, "KO": 0.03, "PEP": 0.025, "T": 0.025}
WM = {"PE": 28.1, "FwdPE": 24.3, "2YFwd": 21.0, "PEG": 1.42, "MedianPEG": 1.38, "Dev_SD": 0.35, "Growth": 15.2,
      "Bull": 18.0, "Base": 12.4, "Bear": 4.2, "IncomeGrade": "A", "CreditRating": "AA"}


def test_donut_text_is_the_visualizers():
    from graphics.visualizer import LynchPinVisualizer
    viz = LynchPinVisualizer.__new__(LynchPinVisualizer)
    d = fastchart.donut_text(WEIGHTS, WM, BENCH)
    t = viz.portfolio_text(WEIGHTS, WM, BENCH)
    assert (d["title"], d["peg"], d["badge"], d["stats_box"], d["quality_box"]) == \
        (t["title"], t["peg"], t["badge"], t["stats_box"], t["quality_box"])
    assert [s["text"] for s in d["slices"]] == ["MSFT\n42.0%", "AAPL\n25.0%", "NVDA\n15.0%", "GOOGL\n10.0%",
                                                "Other (3)\n8.0%"]
    assert d["badge_red"] is True and not any(s["small"] for s in d["slices"])
    lone = fastchart.donut_text({"MSFT": 0.98, "KO": 0.02}, dict(WM, Dev_SD=-0.4), BENCH)
    assert lone["slices"][1] == {"weight": 0.02, "text": "KO 2.0%", "small": True} and lone["badge_red"] is False


@native
@pytest.mark.parametrize("weights,wm", [(WEIGHTS, WM), ({"MSFT": 0.98, "KO": 0.02}, dict(WM, Dev_SD=-0.4)),
                                        ({"MSFT": 1.0}, dict(WM, Dev_SD=None, PEG=None))])
def test_native_donut_matches_matplotlib(tmp_path, weights, wm):
    from graphics.visualizer import LynchPinVisualizer
    ours, timings = fastchart.plot_portfolio(weights, wm, BENCH, str(tmp_path / "native"), "p.png")
    assert timings is not None and timings["png_bytes"] > 0  # drawn natively, not by the matplotlib fallback
    theirs = LynchPinVisualizer(output_dir=str(tmp_path / "mpl")).plot_portfolio(weights, wm, BENCH, filename="p.png")
    a, b = Image.open(ours).convert("RGB"), Image.open(theirs).convert("RGB")
    assert a.size[1] == b.size[1] == 2100 and abs(a.size[0] - b.size[0]) <= 6, (a.size, b.size)
    w = min(a.size[0], b.size[0])
    diff = np.abs(np.asarray(a, dtype=np.int16)[:, :w] - np.asarray(b, dtype=np.int16)[:, :w]).max(axis=2)
    assert diff.mean() < 3.0, diff.mean()  # same layout: anti-aliasing and sub-pixel placement differ
    assert (diff > 96).mean() < 0.01
    from ui import imaging
    preview = imaging.jpeg_preview(ours, imaging.plot_preview_dir(ours), imaging.PLOT_PREVIEW_WIDTH)
    with Image.open(preview) as im:
        assert im.size[0] == imaging.PLOT_PREVIEW_WIDTH


def test_donut_falls_back_to_matplotlib(tmp_path, monkeypatch):
    def broken(spec):
        raise fastchart.NativeChartError("boom")
    monkeypatch.setattr(fastchart, "render", broken)
    path, timings = fastchart.plot_portfolio(WEIGHTS, WM, BENCH, str(tmp_path), "p.png")
    assert timings is None
    with Image.open(path) as im:
        assert im.size[1] == 2100
