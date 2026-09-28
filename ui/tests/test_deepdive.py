"""Deep Dive Prompt + price levels."""
import http.client
import json
import threading
import time

import numpy as np
import pandas as pd
import pytest

from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.deepdive import build_deep_dive
from ui.jobs import DayStore, JobManager
from ui.levels import compute_levels
from ui.server import PortalApp, PortalServer, make_handler
from ui.tests import fakes


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    return s


def analysed(settings, **kw):
    return TickerAnalyzer(settings, backends=fakes.backends(**kw)).run("MSFT")


AI = {"status": "done", "model": "qwen3.8-27b-splash", "model_short": "qwen3.8-27b-splash",
      "narrative": {"overview": "A sleep-well compounder.", "reverse_dcf": "13.5% base ROI requires 13%/yr.",
                    "stomach_test": "AI capex could compress margins."}}


# ── price levels ────────────────────────────────────────────────────────────
def synthetic_history(n=252, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)) + 0.08 * np.sin(np.arange(n) / 9))
    idx = pd.bdate_range("2025-09-01", periods=n)
    spread = np.abs(rng.normal(0, 0.8, n)) + 0.3
    return pd.DataFrame({"Open": close, "High": close + spread, "Low": close - spread, "Close": close,
                         "Volume": rng.integers(1_000_000, 5_000_000, n)}, index=idx)


def test_compute_levels_shape_and_ordering():
    hist = synthetic_history()
    lv = compute_levels(hist)
    price = lv["price"]
    assert price == round(float(hist["Close"].iloc[-1]), 2)
    assert all(x["price"] < price for x in lv["support"]) and all(x["price"] > price for x in lv["resistance"])
    sup = [x["price"] for x in lv["support"]]
    res = [x["price"] for x in lv["resistance"]]
    assert sup == sorted(sup, reverse=True) and res == sorted(res)  # nearest first
    assert lv["support"] or lv["resistance"]
    assert all(0 <= x["p_touch_1m"] <= 100 for x in lv["support"] + lv["resistance"])
    m = lv["ranges"]["1m"]
    assert m["2sigma_lower"] < m["1sigma_lower"] < price < m["1sigma_upper"] < m["2sigma_upper"]
    assert lv["ranges"]["1w"]["move_pct"] < m["move_pct"]
    assert lv["low_52w"] <= price <= lv["high_52w"] and lv["sma200"] and lv["poc"] and len(lv["hvn"]) == 3
    json.dumps(lv)  # plain JSON types only


def test_compute_levels_needs_history():
    assert compute_levels(synthetic_history(30)) is None


def test_levels_are_part_of_the_technicals_stage(settings):
    r = analysed(settings)
    assert r["levels"]["support"][0]["price"] == 419.5 and r["stages"]["technicals"] == "done"


def test_levels_failure_does_not_break_technicals(settings):
    def boom(t, price=None):
        raise RuntimeError("yahoo down")
    r = analysed(settings, levels=boom)
    assert r["levels"] is None and r["stages"]["technicals"] == "done" and r["technicals"]["signal"] == "BULLISH"


# ── prompt ──────────────────────────────────────────────────────────────────
def test_prompt_role_workflow_loop_and_report(settings):
    p = build_deep_dive(analysed(settings), AI, "included")
    assert p.startswith("You are a senior portfolio manager at a long-only fundamental hedge fund")
    assert "Microsoft Corporation ($MSFT)" in p and "underperform the S&P 500" in p
    for must in ("last two quarterly earnings releases", "earnings call transcripts", "10-Q",
                 "FICO", "antitrust", "Meta", "capex", "Red flags", "Repeat this step until another pass would not change",
                 "INVEST, WATCHLIST or PASS", "Price levels to watch", "Signals and news to watch",
                 "cite each fact with its source and date"):
        assert must in p, must
    # the steps come before the attached data, the data before the AI note, the notes last
    order = [p.index(x) for x in ("## How to work", "## Report format", "=== LYNCH PIN DATA", "=== LOCAL AI OVERVIEW",
                                  "=== NOTES ON THE DATA")]
    assert order == sorted(order)


def test_prompt_attaches_quant_data_and_levels(settings):
    p = build_deep_dive(analysed(settings), AI, "included")
    for must in ("Price: $430.00 · Market cap: $3.20T", "PEG 1.68 · historical mean 1.72", "Dev -0.19 SD (cheaper",
                 "Trailing PE 28.7 (GAAP EPS last 12 months ≈ $14.98) · Forward PE 21.8 (next fiscal year consensus EPS ≈ $19.72)",
                 "Bull +15.0% · Base +13.5% · Bear +12.1%", "Grade A+", "Revenue: +18%\n",
                 "G&A: +40% (costs outgrowing revenue)", "Synthetic credit rating AAA", "Interest coverage (x): 50.9",
                 "TECHNICALS · BULLISH", "Accumulation zone (SMA200 ± 1 ATR): $419.00 - $440.00",
                 "Resistance, nearest first: $445.80 (P(touch) 1M 48%)",
                 "Support, nearest first: $419.50 (P(touch) 1M 62%), $401.20 (P(touch) 1M 31%)",
                 "point of control $425.10", "1-month expected range: ±1σ $399.00 - $461.00",
                 "52-week high $468.00", "Bull signals: 60% accurate (22 signals)", "Stronger side: BULL",
                 "Yahoo consensus only"):
        assert must in p, must


def test_prompt_points_to_52w_extremes_when_no_pivot_level(settings):
    lv = dict(fakes.LEVELS, resistance=[], support=[])
    p = build_deep_dive(analysed(settings, levels=lambda t, price=None: lv))
    assert "Resistance, nearest first: none from the last 6 months' pivots; the next reference is the 52-week high $468.00" in p
    assert "the next reference is the 52-week low $344.00" in p


def test_prompt_attaches_ai_overview_or_says_why_not(settings):
    data = analysed(settings)
    p = build_deep_dive(data, AI, "included")
    assert "LOCAL AI OVERVIEW (qwen3.8-27b-splash" in p and "Overview: A sleep-well compounder." in p
    assert "Stomach test: AI capex could compress margins." in p
    assert "still being written" in build_deep_dive(data, None, "pending")
    assert "not available" in build_deep_dive(data, None, "unavailable")


def test_prompt_for_nodata_ticker(settings):
    eng = type("E", (fakes.FakeEngine,), {"infos": {"RIVN": dict(fakes.MSFT_INFO, forwardPE=-4.0,
                                                                   longName="Rivian")}, "rows": {}})
    data = TickerAnalyzer(settings, backends=fakes.backends(engine=eng)).run("RIVN")
    p = build_deep_dive(data, None, "unavailable")
    assert data["status"] == "nodata" and "No GARP valuation: no GARP data (negative or absent forward earnings)" in p
    assert "Rivian ($RIVN)" in p and "PRICE LEVELS TO WATCH" in p and "Support, nearest first" in p


def test_enriched_growth_is_named(settings, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")
    assert "enriched: Yahoo + FMP analyst estimates" in build_deep_dive(analysed(settings))


# ── jobs + HTTP ─────────────────────────────────────────────────────────────
def wait(fn, pred, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if pred(v):
            return v
        time.sleep(0.02)
    raise AssertionError(v)


def test_deepdive_endpoint(settings):
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), store=DayStore())
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        return r.status, body
    try:
        assert get("/api/ticker/MSFT/deepdive")[0] == 404  # not analysed yet
        assert get("/api/ticker/MSFT/deepdive/x")[0] == 400
        get("/api/ticker/MSFT")
        wait(lambda: jm.request("MSFT", poll=True), lambda s: s["status"] == "done")
        st, dd = get("/api/ticker/MSFT/deepdive")
        assert st == 200 and dd["ai"] == "disabled" and dd["words"] > 600 and "=== LYNCH PIN DATA: $MSFT" in dd["prompt"]
        jm.store.update("MSFT", lambda e: e.__setitem__("ai", AI))
        st, dd = get("/api/ticker/MSFT/deepdive")
        assert dd["ai"] == "included" and "A sleep-well compounder." in dd["prompt"]
        assert "_ai_inputs" not in dd["prompt"] and "plot_file" not in dd["prompt"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


def test_deepdive_ai_pending_while_generating(settings):
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), store=DayStore(),
                    llm=object(), start=False)
    data = analysed(settings)
    jm.store.put("MSFT", data)
    assert jm.deep_dive("MSFT")["ai"] == "unavailable"
    jm._ai_inflight["MSFT"] = object()
    assert jm.deep_dive("MSFT")["ai"] == "pending"


def test_prompt_explains_both_multiples_and_cash(settings):
    p = build_deep_dive(analysed(settings), AI, "included")
    assert "The forward PE is the valuation anchor" in p and "Don't rebuild the forward multiple from a single quarter" in p
    assert "Trailing PE = price / GAAP EPS of the last 12 months" in p
    assert "marketable securities and long-term investments are excluded" in p
    assert "State the S&P 500 return you assume" in p and "the expected annual return if bought there" in p
    assert "sources you read in full" in p
    assert "likely include one-off gains" not in p  # MSFT: trailing EPS below forward, nothing to flag


def test_prompt_flags_gain_inflated_trailing_eps(settings):
    # GOOGL-like: trailing PE 17.2 on gain-inflated GAAP EPS, forward PE 22.7 on next-year consensus
    eng = type("E", (fakes.FakeEngine,), {"rows": {"MSFT": dict(fakes.MSFT_ROW, PE=17.2, FwdPE=22.7)},
                                           "infos": {"MSFT": dict(fakes.MSFT_INFO, currentPrice=342.75)}})
    p = build_deep_dive(analysed(settings, engine=eng))
    assert "trailing earnings likely include one-off gains, so the trailing PE understates the multiple" in p
    assert "(GAAP EPS last 12 months ≈ $19.93)" in p and "(next fiscal year consensus EPS ≈ $15.10)" in p
