"""Company-name search (ui/names.py) and its endpoint, /api/suggest."""
import http.client
import json
import os
import threading
import time

import pytest

from ui import names
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.server import PortalApp, PortalServer, make_handler
from ui.tests import fakes

# A slice of the SEC's company_tickers.json, best-ranked (largest) first, as the SEC orders it
SEC = [
    {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    {"cik_str": 1018724, "ticker": "AMZN", "title": "AMAZON COM INC"},
    {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    {"cik_str": 19617, "ticker": "JPM", "title": "JPMORGAN CHASE & CO"},
    {"cik_str": 21344, "ticker": "KO", "title": "COCA COLA CO"},
    {"cik_str": 2488, "ticker": "AMD", "title": "ADVANCED MICRO DEVICES INC"},
    {"cik_str": 723125, "ticker": "MU", "title": "MICRON TECHNOLOGY INC"},
    {"cik_str": 6951, "ticker": "AMAT", "title": "APPLIED MATERIALS INC /DE"},
    {"cik_str": 37996, "ticker": "F", "title": "FORD MOTOR CO"},
    {"cik_str": 1418135, "ticker": "APLE", "title": "Apple Hospitality REIT, Inc."},
    {"cik_str": 1474098, "ticker": "SYRE", "title": "Spyre Therapeutics, Inc."},
] + [{"cik_str": 9_000_000 + i, "ticker": f"Z{i:03d}", "title": f"FILLER {i} INC"} for i in range(300)] + [
    {"cik_str": 1591890, "ticker": "FORD", "title": "Forward Industries, Inc."},
]


@pytest.fixture
def index(tmp_path):
    ix = names.NameIndex(str(tmp_path), fetch=lambda: SEC)
    ix.load(SEC)
    return ix


def tickers(results):
    return [r["ticker"] for r in results]


def test_names_and_tickers_rank_as_people_expect(index):
    assert tickers(index.search("microsoft")) == ["MSFT"]
    assert tickers(index.search("micro"))[:3] == ["MSFT", "AMD", "MU"]  # by market value
    assert tickers(index.search("apple"))[:2] == ["AAPL", "APLE"]
    assert tickers(index.search("coca cola")) == ["KO"]
    assert tickers(index.search("Coca-Cola")) == ["KO"]
    assert tickers(index.search("jp morgan")) == ["JPM"]  # the SEC writes JPMORGAN
    assert tickers(index.search("applied mat")) == ["AMAT"]
    assert tickers(index.search("berkshire")) == ["BRK-B"]
    assert tickers(index.search("brk.b")) == ["BRK-B"]
    assert tickers(index.search("$nvda"))[0] == "NVDA"
    assert tickers(index.search("nv"))[0] == "NVDA"


def test_an_exact_ticker_wins_unless_a_far_larger_company_has_the_name(index):
    assert tickers(index.search("FORD"))[:2] == ["F", "FORD"]  # Ford Motor, then Forward Industries
    assert tickers(index.search("F"))[0] == "F"
    assert tickers(index.search("MU"))[0] == "MU"


def test_one_suggestion_per_company_and_unlisted_tickers(index):
    assert tickers(index.search("alphabet")) == ["GOOGL"]  # not GOOG as well
    assert tickers(index.search("GOOG"))[0] == "GOOG"  # unless it is typed
    assert index.search("ZZZZ") == [{"ticker": "ZZZZ", "name": None, "match": "ticker"}]
    spy = index.search("SPY")
    assert tickers(spy) == ["SYRE", "SPY"] and spy[-1]["name"] is None  # an ETF: offered last, as itself
    assert tickers(index.search("shop.to"))[0] == "SHOP.TO"  # an exchange suffix: first
    assert index.search("") == [] and index.search("x" * 61) == []
    assert len(index.search("filler")) == names.LIMIT


def test_cached_tickers_are_marked_with_yahoos_name(index):
    cache = {"MSFT": {"name": "Microsoft Corporation"}}
    r = index.search("micro", cached=cache.get)
    assert r[0] == {"ticker": "MSFT", "name": "Microsoft Corporation", "match": "name", "cached": True}
    assert "cached" not in r[1]


def test_pretty_names():
    assert names.pretty("MICROSOFT CORP") == "Microsoft Corp"
    assert names.pretty("APPLIED MATERIALS INC /DE") == "Applied Materials Inc"
    assert names.pretty("JPMORGAN CHASE & CO") == "JPMorgan Chase & Co"
    assert names.pretty("COCA-COLA EUROPACIFIC PARTNERS plc") == "Coca-Cola Europacific Partners plc"
    assert names.pretty("Meta Platforms, Inc.") == "Meta Platforms, Inc."  # not all caps: as the SEC writes it


def test_the_list_is_cached_on_disk_and_refreshed_in_the_background(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return SEC
    ix = names.NameIndex(str(tmp_path), fetch=fetch)
    ix.search("apple")  # the first use starts the download in the background
    t0 = time.time()
    while not ix.size and time.time() - t0 < 5:
        time.sleep(0.02)
    assert ix.size == len(SEC)  # every ticker (GOOG and GOOGL too: one company, two tickers)
    assert os.path.exists(ix.path) and calls == [1]
    again = names.NameIndex(str(tmp_path), fetch=fetch)  # a restart reads the file: no download
    assert tickers(again.search("microsoft")) == ["MSFT"] and calls == [1]

    def broken():
        raise OSError("SEC unreachable")
    cold = names.NameIndex(str(tmp_path / "none"), fetch=broken)
    cold.ensure()
    time.sleep(0.2)
    assert cold.size == 0  # no list: the box takes tickers as before
    assert cold.search("apple") == [{"ticker": "APPLE", "name": None, "match": "ticker"}]
    assert cold.search("coca cola") == []


@pytest.fixture
def portal(tmp_path, index):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    jm = JobManager(s, analyzer=TickerAnalyzer(s, backends=fakes.backends()), store=DayStore(), deadline=30)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(s, jobs=jm, names=index)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, jm
    httpd.shutdown()
    httpd.server_close()
    jm.shutdown()


def get(httpd, path):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r, body


def test_suggest_endpoint_marks_cached_tickers_and_starts_nothing(portal):
    httpd, jm = portal
    t0 = time.time()
    while jm.request("MSFT")["status"] not in ("done", "nodata", "error") and time.time() - t0 < 10:
        time.sleep(0.02)
    calls = len(fakes.FakeEngine.calls)
    r, body = get(httpd, "/api/suggest?q=micro")
    d = json.loads(body)
    assert r.status == 200 and d["q"] == "micro" and d["names"] > 0
    assert d["results"][0]["ticker"] == "MSFT" and d["results"][0]["cached"] is True
    assert d["results"][0]["name"] == fakes.FakeEngine.infos["MSFT"]["longName"]  # Yahoo's name, from the cache
    assert "cached" not in d["results"][1]
    assert len(fakes.FakeEngine.calls) == calls  # suggestions never analyse anything
    assert json.loads(get(httpd, "/api/suggest?q=")[1])["results"] == []
    assert json.loads(get(httpd, "/api/suggest?q=" + "a" * 70)[1])["results"] == []
