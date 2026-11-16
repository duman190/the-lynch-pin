"""The portfolio 5Y backtest (ui/backtest.py, ui/native/src/backtest.rs, /api/portfolio/backtest): finished weeks only,
the weekly cache, the maths (against hand computations and between the native library and its numpy twin) and the
HTTP route."""
import datetime as _dt
import json
import math
import threading
import time

import numpy as np
import pytest

from ui import backtest as B
from ui.portfolio import PortfolioAnalyzer, parse_positions
from ui.tests.test_portfolio import BENCH, fake_plot, get, run  # noqa: F401 — fixtures below
from ui.tests.test_portfolio import jm, settings  # noqa: F401

native = pytest.mark.skipif(B.native() is None, reason="native backtest not built (ui/native)")
SUNDAY = _dt.date(2026, 11, 15)  # its last finished week began Monday 9 November


def yahoo(closes, start=_dt.date(2020, 11, 2), live=True, null_at=()):
    """Yahoo's weekly chart JSON: Monday bars from ``start``, plus a live bar of this week when ``live``."""
    t = [int(_dt.datetime(*(start + _dt.timedelta(weeks=i)).timetuple()[:3], 14).timestamp()) for i in range(len(closes))]
    adj = [None if i in null_at else c for i, c in enumerate(closes)]
    if live:
        t.append(int(_dt.datetime(2026, 11, 18, 15).timestamp()))  # Wednesday of the week under way
        adj.append(1e9)  # never used: the week is not over
    return json.dumps({"chart": {"result": [{"timestamp": t, "indicators": {
        "quote": [{"close": adj}], "adjclose": [{"adjclose": adj}]}}]}}).encode()


def walk(seed, n=315, drift=0.002, vol=0.03):
    rng = np.random.default_rng(seed)
    return list(100 * np.cumprod(1 + drift + vol * rng.standard_normal(n)))


SERIES = {s: walk(i) for i, s in enumerate(["MSFT", "KO", "VOO", "QQQ", "SCHD", "SMH", "IGV"])}


class Fetch:
    def __init__(self, series=SERIES, irx=4.0):
        self.series, self.irx, self.calls = series, irx, []
        self.lock = threading.Lock()

    def __call__(self, sym):
        with self.lock:
            self.calls.append(sym)
        if sym == "^IRX":
            return yahoo([self.irx] * 315)
        if sym not in self.series:
            raise B.NoData(f"{sym}: unknown symbol")
        return yahoo(self.series[sym])


def wait(bt, key, weights, timeout=10):
    t0 = time.time()
    r = bt.request(key, weights)
    while r["status"] == "running":
        assert time.time() - t0 < timeout
        time.sleep(0.01)
        r = bt.request(key, weights)
    return r


def test_last_closed_week():
    assert B.last_closed_week(_dt.date(2026, 11, 13)) == _dt.date(2026, 11, 2)   # Friday: the week isn't over
    assert B.last_closed_week(_dt.date(2026, 11, 14)) == _dt.date(2026, 11, 9)   # Saturday: a new point
    assert B.last_closed_week(SUNDAY) == _dt.date(2026, 11, 9)
    assert B.last_closed_week(_dt.date(2026, 11, 16)) == _dt.date(2026, 11, 9)


def test_parse_yahoo():
    s = B.parse_yahoo(yahoo([1.0, 2.0], live=False, null_at=(1,)))
    assert s["adj"] == [1.0, None] and len(s["t"]) == 2
    with pytest.raises(B.NoData):
        B.parse_yahoo(b'{"chart": {"result": null, "error": {"description": "No data found"}}}')


def test_backtest_matches_a_hand_computation(tmp_path):
    fetch = Fetch()
    bt = B.Backtester(fetch=fetch, today=lambda: SUNDAY, cache_dir=str(tmp_path))
    r = wait(bt, "k", {"MSFT": 0.6, "KO": 0.4})
    assert r["status"] == "done", r
    assert r["end"] == "2026-11-09" and r["next_update"] == "2026-11-21"  # the live week is left out
    assert r["start"] == "2021-11-08" and len(r["weeks"]) == 262
    assert [s["sym"] for s in r["series"]] == ["Portfolio", *B.BENCHMARKS]
    assert r["rf"] == pytest.approx(0.04) and "4.00%" in r["notes"][0]
    i0 = r["weeks"].index("2021-11-08")
    first = (_dt.date(2021, 11, 8) - _dt.date(2020, 11, 2)).days // 7
    m, k = np.array(SERIES["MSFT"][first:first + 262]), np.array(SERIES["KO"][first:first + 262])
    pf = 0.6 * 1e4 * m / m[0] + 0.4 * 1e4 * k / k[0]
    got = np.array(r["series"][0]["values"])
    assert i0 == 0 and np.allclose(got, pf, atol=0.01)
    ret = pf[1:] / pf[:-1] - 1
    rf_w = 1.04 ** (1 / 52) - 1
    mt = r["series"][0]["metrics"]
    assert mt["total"] == pytest.approx(pf[-1] / pf[0] - 1, rel=1e-6)
    assert mt["vol"] == pytest.approx(ret.std(ddof=1) * math.sqrt(52), rel=1e-6)
    assert mt["sharpe"] == pytest.approx((ret - rf_w).mean() * 52 / (ret.std(ddof=1) * math.sqrt(52)), rel=1e-6)
    dd = np.sqrt(np.mean(np.minimum(ret - rf_w, 0) ** 2)) * math.sqrt(52)
    assert mt["sortino"] == pytest.approx((ret - rf_w).mean() * 52 / dd, rel=1e-6)
    assert mt["max_dd"] == pytest.approx((pf / np.maximum.accumulate(pf) - 1).min(), rel=1e-6)
    roll = pf[52:] / pf[:-52] - 1
    assert (mt["best_12m"], mt["worst_12m"]) == (pytest.approx(roll.max()), pytest.approx(roll.min()))
    assert mt["corr"] == 1.0 and mt["beta"] == 1.0
    voo = np.array(SERIES["VOO"][first:first + 262])
    vr = voo[1:] / voo[:-1] - 1
    vm = r["series"][1]["metrics"]
    assert vm["corr"] == pytest.approx(np.corrcoef(ret, vr)[0, 1], rel=1e-6)
    assert vm["beta"] == pytest.approx(np.cov(ret, vr)[0, 1] / vr.var(ddof=1), rel=1e-6)
    assert r["series"][1]["values"][0] == 10000.0


def test_cache_is_weekly_and_on_disk(tmp_path):
    day = [SUNDAY]
    fetch = Fetch()
    bt = B.Backtester(fetch=fetch, today=lambda: day[0], cache_dir=str(tmp_path))
    wait(bt, "a", {"MSFT": 1.0})
    n = len(fetch.calls)
    assert n == 7 and (tmp_path / "backtest" / "VOO.json").exists() and (tmp_path / "backtest" / "_IRX.json").exists()
    wait(bt, "b", {"KO": 0.5, "MSFT": 0.5})  # only KO is new
    assert fetch.calls[n:] == ["KO"]
    again = B.Backtester(fetch=fetch, today=lambda: day[0], cache_dir=str(tmp_path))  # a restart
    assert wait(again, "a", {"MSFT": 1.0})["status"] == "done" and len(fetch.calls) == n + 1
    day[0] = _dt.date(2026, 11, 17)  # Tuesday: still the same finished week
    assert wait(bt, "c", {"MSFT": 1.0})["status"] == "done" and len(fetch.calls) == n + 1
    day[0] = _dt.date(2026, 11, 21)  # Saturday: the week of the 16th is over → everything is fetched again
    r = wait(bt, "d", {"MSFT": 1.0})
    assert len(fetch.calls) == n + 1 + 7
    assert r["status"] == "done" and r["end"] == "2026-11-16"  # last week's bar, final now, is the new point


def test_errors_and_short_histories(tmp_path):
    bt = B.Backtester(fetch=Fetch(), today=lambda: SUNDAY)
    r = wait(bt, "x", {"NOPE": 1.0})
    assert r["status"] == "error" and "NOPE" in r["error"]
    young = dict(SERIES, NEWCO=[None] * 200 + walk(9, 115))
    bt = B.Backtester(fetch=Fetch(young), today=lambda: SUNDAY)
    r = wait(bt, "y", {"MSFT": 0.5, "NEWCO": 0.5})
    assert r["status"] == "done" and r["start"] > "2021-11-08" and "$NEWCO" in r["notes"][0]

    class NoIrx(Fetch):
        def __call__(self, sym):
            if sym == "^IRX":
                raise B.BacktestError("down")
            return super().__call__(sym)
    bt = B.Backtester(fetch=NoIrx(), today=lambda: SUNDAY)
    r = wait(bt, "z", {"MSFT": 1.0})
    assert r["status"] == "done" and r["rf"] == 0.0 and "unavailable" in r["notes"][0]


@native
def test_native_matches_numpy():
    series = {s: dict(B.parse_yahoo(yahoo(v, null_at=(5, 77))), sym=s) for s, v in SERIES.items()}
    req = {"holdings": [series["MSFT"], series["KO"]], "weights": [0.7, 0.3],
           "benchmarks": [series[b] for b in B.BENCHMARKS], "start_day": (_dt.date(2021, 11, 13) - _dt.date(1970, 1, 1)).days,
           "rf": 0.035, "start_value": 1e4}
    a, b = B.compute_native(req), B.compute_python(req)
    assert a["weeks"] == b["weeks"] and a["first_weeks"] == b["first_weeks"]
    assert np.allclose(a["portfolio"], b["portfolio"], rtol=1e-12)
    for ma, mb in [(a["metrics"], b["metrics"])] + [(x["metrics"], y["metrics"]) for x, y in zip(a["benchmarks"], b["benchmarks"])]:
        assert ma.keys() == mb.keys()
        for k in ma:
            assert ma[k] == pytest.approx(mb[k], rel=1e-9, abs=1e-12), k
    with pytest.raises(B.NoData):
        B.compute_native(dict(req, start_day=30000))
    with pytest.raises(B.BacktestError):
        B.compute_native(dict(req, weights=[1.0]))


def test_python_twin_without_the_library(tmp_path, monkeypatch):
    bt = B.Backtester(fetch=Fetch(), today=lambda: SUNDAY, use_native=False)
    r = wait(bt, "p", {"MSFT": 1.0})
    assert r["status"] == "done" and r["native"] is False


# ── through the portfolio page ──────────────────────────────────────────────────────────────────────────────────
def test_portfolio_backtest_route(settings, jm, capsys):  # noqa: F811
    from ui.server import PortalApp, PortalServer, make_handler
    bt = B.Backtester(fetch=Fetch(dict(SERIES, AAPL=walk(11))), today=lambda: SUNDAY)
    pf = PortfolioAnalyzer(settings, jm, benchmarks=lambda: BENCH, plot=fake_plot([]), backtester=bt)
    assert pf.backtest("0123456789abcdef") is None
    snap = run(pf, parse_positions("MSFT:60,AAPL:40", "pct"))
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm, portfolio=pf)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        t0 = time.time()
        while True:
            r, body = get(httpd, f"/api/portfolio/backtest?k={snap['key']}")
            assert r.status == 200
            if body["status"] != "running":
                break
            assert time.time() - t0 < 10
            time.sleep(0.02)
        assert body["status"] == "done" and body["series"][0]["sym"] == "Portfolio" and len(body["weeks"]) == 262
        assert get(httpd, "/api/portfolio/backtest?k=nothex")[0].status == 400
        assert get(httpd, "/api/portfolio/backtest?k=0123456789abcdef")[0].status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "/api/portfolio/backtest?…" in capsys.readouterr().err
