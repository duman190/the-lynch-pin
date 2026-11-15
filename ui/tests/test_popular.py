"""The portal's "Most wanted" section: the tickers looked up most this week and this month (/api/popular)."""
import http.client
import json
import threading

import pytest

from ui import names
from ui.config import Settings
from ui.server import PortalApp, PortalServer, make_handler
from ui.stats import StatsRecorder
from ui.tests.test_names import SEC

NOW = 1_780_000_000.0
DAY = 86400


@pytest.fixture
def rec(tmp_path):
    r = StatsRecorder(str(tmp_path / "stats.sqlite3"), clock=lambda: NOW, start=False)
    for ts, sym, status in (
            [(NOW - 60, "NVDA", "done")] * 3 + [(NOW - 2 * DAY, "MSFT", "done")] * 2 + [(NOW - 3 * DAY, "SPY", "nodata")]
            + [(NOW - 20 * DAY, "MSFT", "done")] * 4 + [(NOW - 60, "ZZZZ", "error")] * 9  # errors: no ticker
            + [(NOW - 40 * DAY, "AMD", "done")] * 9):  # past the month
        r.query(sym, "fresh", status, 1.0, ts=ts)
    r.flush(final=True)
    yield r
    r.close()


def test_week_and_month_windows(rec):
    p = rec.popular(n=10)
    assert p["week"]["days"] == 7 and p["week"]["total"] == 6
    assert p["week"]["top"] == [{"ticker": "NVDA", "n": 3, "pct": 50.0}, {"ticker": "MSFT", "n": 2, "pct": 33.3},
                                {"ticker": "SPY", "n": 1, "pct": 16.7}]
    assert p["month"]["total"] == 10 and [t["ticker"] for t in p["month"]["top"]] == ["MSFT", "NVDA", "SPY"]
    assert len(rec.popular(n=2)["month"]["top"]) == 2


def test_endpoint_names_tickers_and_is_reused(tmp_path, rec):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    ix = names.NameIndex(str(tmp_path), fetch=lambda: SEC)
    ix.load(SEC)
    app = PortalApp(s, stats=rec, names=ix)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get():
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            c.request("GET", "/api/popular")
            r = c.getresponse()
            body = json.loads(r.read())
            c.close()
            return r, body
        r, d = get()
        assert r.status == 200 and r.getheader("Cache-Control") == "public, max-age=300" and d["enabled"]
        top = d["week"]["top"]
        assert (top[0]["ticker"], top[0]["name"]) == ("NVDA", "NVIDIA Corp")  # the SEC's name, prettified
        assert top[-1]["name"] is None  # SPY: no SEC row
        rec.query("AMD", "fresh", "done", 1.0, ts=NOW)
        rec.flush(final=True)
        assert get()[1] == d  # reused for POPULAR_TTL seconds
        app._popular = None
        assert get()[1]["week"]["total"] == 7
        assert app.health()["features"]["popular"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert PortalApp(s, names=ix).popular() == {"enabled": False}  # no stats recorder (a local portal)
