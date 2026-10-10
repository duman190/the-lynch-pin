"""US stock market valuation: the Shiller PE (a fake multpl.com page) and the S&P 500 forward PEG (fake constituents,
a fake Yahoo 429 counter); no network."""
import datetime
import json
import os
from zoneinfo import ZoneInfo

import pytest

from graphics import market_valuation as charts
from ui import valuation as val
from ui.config import Settings
from ui.server import PortalApp
from ui.tests.test_server import get, serve
from ui.valuation import MarketValuation, index_peg, load_reference, load_tickers, parse_multpl, valuation_day

PT = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _small_charts(monkeypatch):
    monkeypatch.setattr(charts, "DPI", 40)  # the layout is the same, the PNG is tiny and quick


def multpl_page(months=700):
    """A table like multpl.com's: header row, the current month's estimate († mark) on top, then a row per month."""
    rows = ['<tr><th class="left">Date</th><th class="right">Value</th></tr>',
            '<tr class="odd"><td class="left">Oct 9, 2026</td><td class="right">\n'
            '<abbr title="Value is a computed estimate">&#x2020;</abbr>\n41.73\n</td></tr>']
    day = datetime.date(2026, 10, 1)
    for i in range(months):
        rows.append(f'<tr class="{"even" if i % 2 else "odd"}"><td class="left">{day:%b} {day.day}, {day.year}</td>'
                    f'<td class="right">\n&#x2002;\n{15 + (i % 30) * 0.5:.2f}\n</td></tr>')
        day = (day - datetime.timedelta(days=1)).replace(day=1)
    return ('<html><body><div id="current">Current Shiller PE Ratio: 41.73 +0.12 (0.29%)</div>'
            '<table id="datatable"><tbody>' + "\n".join(rows) + "</tbody></table></body></html>")


def test_parse_multpl_reads_every_month_oldest_first():
    series = parse_multpl(multpl_page())
    assert len(series) == 701
    assert series[-1] == ("2026-10-09", 41.73)  # the estimate row, its † mark and tags ignored
    assert series[-2] == ("2026-10-01", 15.0)
    assert series[0][0] < series[1][0] and series[0][0].endswith("-01")


@pytest.mark.parametrize("page", ["", "<html>Service Unavailable</html>", multpl_page(months=100)])
def test_parse_multpl_refuses_a_page_without_the_monthly_table(page):
    with pytest.raises(ValueError):
        parse_multpl(page)


def test_index_peg_is_the_cap_weighted_harmonic_mean_of_pegs():
    rows = {"FAST": (100.0, 1.0, 30.0),   # P/E 30, growth 30%: forward earnings 3.33
            "SLOW": (100.0, 5.0, 15.0),   # P/E 15, growth 3%: forward earnings 6.67
            "LOSS": (50.0, None, -12.0),  # no PEG (losses)
            "GLITCH": (50.0, 0.01, 20.0), # outside PEG_RANGE: it would swamp the harmonic mean
            "GONE": None}                 # Yahoo gave nothing
    p = index_peg(rows)
    # index P/E = 200 / 10 = 20; earnings-weighted growth = (3.33 × 30 + 6.67 × 3) / 10 = 12 → 20 / 12 = 1.67,
    # where a plain cap-weighted mean of the PEGs would say 3.0
    assert p["peg"] == round(200 / (100 / 1.0 + 100 / 5.0), 4) == 1.6667
    assert p["fwd_pe"] == 20.0
    assert p["coverage"] == 0.6667               # 200 of the 300 answered
    assert (p["n"], p["answered"], p["total"]) == (2, 4, 5)
    assert index_peg({"X": None, "Y": (10.0, None, -5.0)}) is None


def test_reference_history_is_the_traced_yardeni_series():
    ref = load_reference()
    assert len(ref) >= 360 and ref[0][0] == "1995-03-15" and ref[-1] == ("2026-01-15", 0.70)
    vals = dict(ref)
    assert all(0.5 < v < 2.5 for v in vals.values())
    assert max(v for d, v in ref if "2020" <= d < "2021-04") == 2.4  # the chart's labelled peaks
    assert max(v for d, v in ref if "1999-06" <= d < "2001-07") == 2.0
    assert load_reference("/nonexistent.csv") == []


@pytest.mark.parametrize("now, day", [
    ("2026-10-09 17:59", "2026-10-08"),  # Friday before the run: Thursday's
    ("2026-10-09 18:00", "2026-10-09"),  # Friday's run is due
    ("2026-10-10 12:00", "2026-10-09"),  # the weekend keeps Friday's
    ("2026-10-11 23:00", "2026-10-09"),
    ("2026-10-12 09:00", "2026-10-09"),  # Monday morning: still Friday's
    ("2026-10-12 18:30", "2026-10-12"),
])
def test_valuation_day_is_the_latest_weekday_run(now, day):
    t = datetime.datetime.strptime(now, "%Y-%m-%d %H:%M").replace(tzinfo=PT)
    assert valuation_day("18:00", now=t) == day


@pytest.mark.parametrize("now, friday", [
    ("2026-10-09 17:59", "2026-10-02"),  # Friday before the run: last week's
    ("2026-10-09 18:00", "2026-10-09"),
    ("2026-10-14 12:00", "2026-10-09"),  # midweek: still last Friday's
])
def test_the_sweep_is_weekly_after_fridays_close(now, friday):
    t = datetime.datetime.strptime(now, "%Y-%m-%d %H:%M").replace(tzinfo=PT)
    assert valuation_day("18:00", now=t, weekday=val.PEG_WEEKDAY) == friday


def test_sp500_list_is_one_yahoo_ticker_per_company():
    tickers = load_tickers()
    assert 490 <= len(tickers) <= 510 and len(set(tickers)) == len(tickers)
    assert {"AAPL", "MSFT", "NVDA", "GOOGL", "BRK-B", "BF-B"} <= set(tickers)
    assert not {"GOOG", "FOX", "NWS", "BRK.B"} & set(tickers)  # a second share class would count a company twice


class FakeYahoo:
    """Constituents from a table; ``throttle`` names tickers whose first try trips a Yahoo 429."""
    def __init__(self, table, throttle=()):
        self.table, self.throttle, self.calls, self.hits = table, set(throttle), [], 0

    def fetch(self, sym):
        self.calls.append(sym)
        if sym in self.throttle:
            self.throttle.discard(sym)
            self.hits += 1
            return None
        if self.table.get(sym) == "boom":
            raise RuntimeError("engine blew up")
        return self.table.get(sym)

    def count(self):
        return self.hits


def make(tmp_path, table, throttle=(), shiller=None, reference="/nonexistent.csv", **kw):
    tickers = tmp_path / "sp500.txt"
    tickers.write_text("# test index\n" + "\n".join(table) + "\n\n")
    yahoo = FakeYahoo(table, throttle)
    mv = MarketValuation(str(tmp_path / "cache"), tickers_file=str(tickers), reference_file=reference, pause_s=0,
                         fetch_shiller=shiller or (lambda: parse_multpl(multpl_page())),
                         fetch_constituent=yahoo.fetch, yahoo_429s=yahoo.count, **kw)
    return mv, yahoo


TABLE = {"A": (300.0, 1.5, 30.0), "B": (100.0, 2.0, 10.0), "C": (100.0, 1.0, 20.0)}


def test_sweep_records_a_point_a_day_and_draws_the_chart(tmp_path, monkeypatch):
    monkeypatch.setattr(val, "THROTTLE_S", (0, 0))
    mv, yahoo = make(tmp_path, TABLE, throttle={"B"})
    p = mv.refresh_peg("2026-10-08")
    assert yahoo.calls == ["A", "B", "B", "C"]  # the throttled answer was thrown away and B asked again
    assert p["date"] == "2026-10-08" and p["n"] == 3 and p["coverage"] == 1.0
    assert p["peg"] == round(500 / (300 / 1.5 + 100 / 2.0 + 100 / 1.0), 4)
    mv.refresh_peg("2026-10-09")
    mv.refresh_peg("2026-10-09")  # a re-run replaces that day's point
    assert [q["date"] for q in mv.peg["history"]] == ["2026-10-08", "2026-10-09"]
    assert os.path.isfile(tmp_path / "cache" / "valuation" / "forward_peg.png")
    reread = MarketValuation(str(tmp_path / "cache"))  # kept on disk across restarts
    assert len(reread.peg["history"]) == 2 and reread.snapshot()["peg"]["points"] == 2
    assert mv.snapshot()["running"] == {}


def test_sweep_too_thin_records_nothing(tmp_path):
    table = dict(TABLE, D=None, E=None, F="boom")  # half the names answered nothing
    mv, _ = make(tmp_path, table)
    with pytest.raises(RuntimeError, match="too little"):
        mv.refresh_peg("2026-10-09")
    assert mv.peg == {} and not os.path.exists(tmp_path / "cache" / "valuation" / "peg.json")
    assert mv.snapshot()["running"] == {}


def test_sweep_gives_up_when_yahoo_keeps_throttling(tmp_path, monkeypatch):
    monkeypatch.setattr(val, "THROTTLE_S", (0, 0))

    class Always(FakeYahoo):
        def fetch(self, sym):
            self.hits += 1
            return None
    mv, _ = make(tmp_path, TABLE)
    yahoo = Always(TABLE)
    mv._fetch_constituent, mv._429s = yahoo.fetch, yahoo.count
    with pytest.raises(RuntimeError, match="429"):
        mv.refresh_peg("2026-10-09")
    assert yahoo.hits == val.MAX_THROTTLES + 1 and mv.peg == {}


def test_sweep_waits_while_the_portals_breaker_is_open(tmp_path, monkeypatch):
    monkeypatch.setattr(val, "BREAKER_WAIT_S", 0)
    state = {"left": 3}

    def paused():
        state["left"] -= 1
        return state["left"] >= 0
    mv, yahoo = make(tmp_path, TABLE, paused=paused)
    mv.refresh_peg("2026-10-09")
    assert state["left"] < 0 and yahoo.calls == ["A", "B", "C"]


def test_tick_runs_whats_due_once_and_retries_failures_later(tmp_path, monkeypatch):
    mv, yahoo = make(tmp_path, TABLE)
    clock = {"now": "2026-10-14 09:00"}  # a Wednesday morning: Tuesday's Shiller PE, last Friday's sweep are due

    def day(run_at, now=None, tz=None, weekday=None):
        t = datetime.datetime.strptime(clock["now"], "%Y-%m-%d %H:%M").replace(tzinfo=PT)
        return valuation_day(run_at, now=t, weekday=weekday)
    monkeypatch.setattr(val, "valuation_day", day)
    started = []
    monkeypatch.setattr(mv, "_run", lambda job, d: started.append((job, d)))
    assert mv.tick() == ["shiller", "peg"]
    assert sorted(started) == [("peg", "2026-10-13"), ("shiller", "2026-10-13")]  # a missed sweep: Tuesday's close
    mv._running.clear()
    mv.refresh_shiller("2026-10-13")
    mv.refresh_peg("2026-10-13")
    assert mv.tick() == []  # both done
    clock["now"] = "2026-10-15 18:30"  # Thursday evening: a new Shiller PE, no sweep until Friday
    assert mv.tick() == ["shiller"]
    mv._running.clear()
    mv.refresh_shiller("2026-10-15")
    clock["now"] = "2026-10-16 18:30"  # Friday after the close: both
    assert mv.tick() == ["shiller", "peg"]
    mv._running.clear()
    mv.shiller["day"] = "2026-10-15"
    mv._failed_at["shiller"] = __import__("time").time()
    assert mv.tick() == ["peg"]  # the Shiller PE failed a moment ago: retried an hour later
    mv._running.clear()
    mv._failed_at["shiller"] -= val.RETRY_S["shiller"] + 1
    assert mv.tick() == ["shiller", "peg"]


def test_failed_run_keeps_the_last_chart(tmp_path):
    mv, _ = make(tmp_path, TABLE)
    mv.refresh_shiller("2026-10-08")
    mv._fetch_shiller = lambda: (_ for _ in ()).throw(RuntimeError("HTTP 503"))
    mv._running["shiller"] = {}
    mv._run("shiller", "2026-10-09")
    assert mv.shiller["day"] == "2026-10-08" and mv.snapshot()["shiller"]["value"] == 41.73
    assert "shiller" in mv._failed_at and mv._running == {}


def test_snapshot_and_images(tmp_path):
    mv, _ = make(tmp_path, TABLE)
    assert mv.snapshot() == {"enabled": True, "shiller": None, "peg": None, "running": {}}
    mv.refresh_shiller("2026-10-09")
    s = mv.snapshot()["shiller"]
    assert (s["value"], s["date"], s["since"]) == (41.73, "2026-10-09", "1968")
    assert s["image"].startswith("/valuation/shiller_pe.png?v=") and s["preview"].startswith("/valuation/shiller_pe.jpg?v=")
    assert mv.image_path("shiller_pe.png").endswith("shiller_pe.png")
    assert mv.image_path("shiller_pe.jpg").endswith(".jpg")
    for bad in ("forward_peg.png", "shiller.json", "../shiller_pe.png", "shiller_pe.gif", ""):
        assert mv.image_path(bad) is None, bad


def test_peg_chart_exists_before_the_first_sweep(tmp_path):
    mv, _ = make(tmp_path, TABLE, reference=val.REFERENCE_FILE)
    mv.redraw()
    d = mv.snapshot()["peg"]
    assert (d["peg"], d["date"], d["source"], d["since"], d["points"]) == (0.7, "2026-01-15", "yardeni", "1995", 0)
    mv.refresh_peg("2026-10-09")
    d = mv.snapshot()["peg"]
    assert d["source"] == "lynch_pin" and d["date"] == "2026-10-09" and d["since"] == "1995" and d["points"] == 1


def test_charts_draw_with_and_without_the_history(tmp_path):
    series = parse_multpl(multpl_page(months=1900))  # back to 1868, past the 1929 / 1987 / 1999 markers
    charts.plot_shiller_pe(series, str(tmp_path / "s.png"))
    ref = load_reference()
    one = [{"date": "2026-10-09", "peg": 1.48, "coverage": 0.99, "n": 492, "total": 500}]
    many = [dict(one[0], date=(datetime.date(2026, 1, 1) + datetime.timedelta(days=i)).isoformat(), peg=1 + i / 400)
            for i in range(280)]
    charts.plot_forward_peg([], str(tmp_path / "p0.png"), reference=ref)    # day one: the traced history only
    charts.plot_forward_peg(one, str(tmp_path / "p1.png"), reference=ref)   # + the first Lynch Pin point
    charts.plot_forward_peg(many, str(tmp_path / "p2.png"))                 # no history file
    charts.plot_forward_peg(one, str(tmp_path / "p3.png"))
    assert all(os.path.getsize(tmp_path / f) > 1000 for f in ("s.png", "p0.png", "p1.png", "p2.png", "p3.png"))


# ── HTTP ──────────────────────────────────────────────────────────────────────
def test_routes(tmp_path):
    mv, _ = make(tmp_path, TABLE)
    mv.refresh_shiller("2026-10-09")
    mv.refresh_peg("2026-10-09")
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    httpd = serve(PortalApp(s, valuation=mv))
    try:
        r, body = get(httpd, "/api/valuation")
        d = json.loads(body)
        assert r.status == 200 and d["enabled"] and d["peg"]["n"] == 3 and d["shiller"]["value"] == 41.73
        for key, ctype in (("image", "image/png"), ("preview", "image/jpeg")):
            r, _ = get(httpd, d["peg"][key])
            assert r.status == 200 and r.getheader("Content-Type") == ctype
            assert "max-age=604800" in r.getheader("Cache-Control")
        for path in ("/valuation/peg.json", "/valuation/..%2Fpeg.json", "/valuation/other.png"):
            assert get(httpd, path)[0].status == 404, path
        assert json.loads(get(httpd, "/api/health")[1])["features"]["valuation"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_off_by_default_in_tests_and_the_section_sits_between_search_and_scans(tmp_path):
    s = Settings()  # conftest sets LYNCH_UI_VALUATION=0
    s.cache_dir = str(tmp_path / "cache")
    app = PortalApp(s)
    assert app.valuation is None
    httpd = serve(app)
    try:
        assert json.loads(get(httpd, "/api/valuation")[1]) == {"enabled": False}
        assert get(httpd, "/valuation/shiller_pe.png")[0].status == 404
        page = get(httpd, "/")[1].decode()
        assert page.index('id="search-section"') < page.index('id="valuation"') < page.index('id="scans"')
        assert "or that meets your other criteria for investment.</p>" in page and "— Peter Lynch" in page
        assert "Buffett" not in page
        assert get(httpd, "/static/valuation.js")[0].status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_flags(monkeypatch):
    from ui.server import parse_args
    monkeypatch.setenv("LYNCH_UI_VALUATION", "1")  # the default outside tests
    s, _ = parse_args(["--valuation-at", "17:15"])
    assert s.valuation and s.valuation_at == "17:15"
    s, _ = parse_args(["--no-valuation"])
    assert not s.valuation
    with pytest.raises(SystemExit):
        parse_args(["--valuation-at", "25:00"])
