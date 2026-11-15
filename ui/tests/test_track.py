"""Valuation track record (ui/track.py): one valuation per ticker per week, and /api/ticker/SYM/track."""
import datetime as dt
import http.client
import json
import threading
import time

import pytest

from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.server import PortalApp, PortalServer, make_handler
from ui.tests import fakes
from ui.track import TrackRecord


class Day:
    def __init__(self, d):
        self.d = d

    def __call__(self):
        return self.d


def entry(price, peg=1.5, base="12.0%", status="done", **st):
    return {"status": status, "price": price, "currency": "USD", "generated_at": 1.0,
            "stats": dict({"PEG": peg, "Bear": "8.0%", "Base": base, "Bull": 16.5, "growth_pct": 14.0, "history": "ok"}, **st)}


def test_first_valuation_of_each_week_is_kept(tmp_path):
    today = Day(dt.date(2026, 11, 2))  # a Monday
    tr = TrackRecord(str(tmp_path / "t.sqlite3"), today=today)
    assert tr.record("MSFT", entry(400.0)) is True
    today.d = dt.date(2026, 11, 6)  # the same week: kept as it was
    assert tr.record("MSFT", entry(450.0)) is False
    today.d = dt.date(2026, 11, 9)  # the next week
    assert tr.record("MSFT", entry(440.0, peg=1.4, base=11.0, growth_pct=None, **{"5YGrowth": "12.5%"})) is True
    # nothing to keep: no data, no price, no PEG, a PEG history outage
    for bad in (entry(1.0, status="nodata"), entry(None), entry(5.0, peg=None), entry(5.0, history="unavailable"), None):
        assert tr.record("AAPL", bad) is False
    assert tr.rows("AAPL") == []
    rows = tr.rows("MSFT")
    assert rows == [{"date": "2026-11-09", "price": 440.0, "growth": 12.5, "peg": 1.4, "bear": 8.0, "base": 11.0,
                     "bull": 16.5},
                    {"date": "2026-11-02", "price": 400.0, "growth": 14.0, "peg": 1.5, "bear": 8.0, "base": 12.0,
                     "bull": 16.5}]
    rep = tr.report("MSFT", current=entry(480.0))  # today's analysis sets "since then"
    assert rep["current"] == {"price": 480.0, "date": "2026-11-09", "currency": "USD"}
    assert [r["since_pct"] for r in rep["rows"]] == [9.09, 20.0]
    rep = tr.report("MSFT")  # not analysed today: measured to the newest valuation
    assert rep["current"]["price"] == 440.0 and rep["rows"][-1]["since_pct"] == 10.0
    assert tr.report("NONE") == {"ticker": "NONE", "current": {"price": None, "date": None, "currency": "USD"},
                                 "rows": []}
    tr.close()
    again = TrackRecord(str(tmp_path / "t.sqlite3"), today=today)  # kept on disk
    assert len(again.rows("MSFT")) == 2


def test_a_record_from_before_the_growth_column_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite3")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE valuations (ticker TEXT NOT NULL, week TEXT NOT NULL, date TEXT NOT NULL, ts REAL NOT NULL, "
               "price REAL NOT NULL, peg REAL, bear REAL, base REAL, bull REAL, PRIMARY KEY (ticker, week)) WITHOUT ROWID")
    db.execute("INSERT INTO valuations VALUES ('MSFT', '2026-W44', '2026-10-26', 0, 400, 1.5, 8, 12, 16)")
    db.commit()
    db.close()
    tr = TrackRecord(path, today=Day(dt.date(2026, 11, 2)))
    assert tr.rows("MSFT")[0]["growth"] is None
    assert tr.record("MSFT", entry(410.0)) and tr.rows("MSFT")[0]["growth"] == 14.0


@pytest.fixture
def portal(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    track = TrackRecord(str(tmp_path / "track.sqlite3"))
    jm = JobManager(s, analyzer=TickerAnalyzer(s, backends=fakes.backends()), store=DayStore(), deadline=30,
                    track=track)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(s, jobs=jm)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, jm
    httpd.shutdown()
    httpd.server_close()
    jm.shutdown()


def get(httpd, path):
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = json.loads(r.read())
    c.close()
    return r, body


def test_an_analysis_lands_in_the_track_record(portal):
    httpd, jm = portal
    r, d = get(httpd, "/api/ticker/MSFT/track")
    assert r.status == 200 and d["rows"] == []  # never starts an analysis
    assert jm.lookup("MSFT") is None
    t0 = time.time()
    while jm.request("MSFT")["status"] not in ("done", "nodata", "error") and time.time() - t0 < 10:
        time.sleep(0.02)
    r, d = get(httpd, "/api/ticker/msft/track")
    entry = jm.lookup("MSFT")
    assert r.status == 200 and len(d["rows"]) == 1
    row = d["rows"][0]
    assert row["price"] == entry["price"] and row["peg"] == entry["stats"]["PEG"] and row["since_pct"] == 0.0
    assert row["growth"] is not None
    assert d["current"]["price"] == entry["price"]
    assert get(httpd, "/api/ticker/MSFT/track/x")[0].status == 400
