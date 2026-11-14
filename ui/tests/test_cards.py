"""Link preview cards (ui/cards.py): the Open Graph / Twitter tags the page carries and the card images."""
import http.client
import io
import re
import threading
import time

import pytest
from PIL import Image

from ui import cards
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.server import TICKER_RE, PortalApp, PortalServer, make_handler
from ui.tests import fakes


@pytest.fixture
def portal(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    jm = JobManager(s, analyzer=TickerAnalyzer(s, backends=fakes.backends()), store=DayStore(), deadline=30)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(s, jobs=jm)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, jm
    httpd.shutdown()
    httpd.server_close()
    jm.shutdown()


def get(httpd, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    conn.request("GET", path, headers={"Host": host} if host else {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r, body


def analyse(jm, sym):
    t0 = time.time()
    while time.time() - t0 < 10:
        if jm.request(sym)["status"] in ("done", "nodata", "error"):
            return jm.lookup(sym)
        time.sleep(0.02)
    raise AssertionError(f"{sym} never finished")


def meta(html):
    return {m.group(1): m.group(2) for m in re.finditer(r'<meta (?:property|name)="([^"]+)" content="([^"]*)"', html)}


def test_a_shared_ticker_link_carries_its_card(portal):
    httpd, jm = portal
    entry = analyse(jm, "MSFT")
    r, body = get(httpd, "/?t=msft")
    tags = meta(body.decode())
    assert r.status == 200 and tags["twitter:card"] == "summary_large_image"
    assert tags["og:title"].startswith("$MSFT · ") and "PEG 1.68" in tags["og:title"]
    assert "5Y base ROI 13.5%" in tags["og:title"] and "Not financial advice" in tags["og:description"]
    port = httpd.server_address[1]
    assert tags["og:url"] == f"http://127.0.0.1:{port}/?t=MSFT"  # absolute: crawlers need it
    assert tags["og:image"] == f"http://127.0.0.1:{port}/card/MSFT.jpg?v={int(entry['generated_at'])}"
    assert tags["twitter:image"] == tags["og:image"]
    assert body.count(b"</head>") == 1 and body.index(b'og:title') < body.index(b"</head>")


def test_card_image_is_drawn_from_the_cached_analysis_and_never_starts_one(portal):
    httpd, jm = portal
    calls = len(fakes.FakeEngine.calls)
    r, body = get(httpd, "/card/AAPL.jpg")  # nobody analysed it: the generic card, no lookup
    assert r.status == 200 and r.getheader("Content-Type") == "image/jpeg"
    assert len(fakes.FakeEngine.calls) == calls and jm.lookup("AAPL") is None
    assert r.getheader("Cross-Origin-Resource-Policy") == "cross-origin"  # embeddable by chat apps
    assert r.getheader("Cache-Control") == "public, max-age=3600"
    assert Image.open(io.BytesIO(body)).size == (1200, 630)

    analyse(jm, "MSFT")
    r, body = get(httpd, "/card/MSFT.jpg")
    assert r.status == 200 and Image.open(io.BytesIO(body)).size == (1200, 630)
    assert get(httpd, "/card/home.jpg")[0].status == 200
    for bad in ("/card/MSFT.png", "/card/..%2Fsecret.jpg", "/card/TOOLONGTICKER1.jpg"):
        assert get(httpd, bad)[0].status == 404


def test_unanalysed_ticker_scan_and_home_have_their_own_tags(portal, tmp_path):
    httpd, _ = portal
    tags = meta(get(httpd, "/?t=AAPL")[1].decode())
    assert tags["og:title"] == "$AAPL · GARP analysis | The Lynch Pin"
    assert tags["og:image"].endswith("/card/AAPL.jpg?v=g")
    tags = meta(get(httpd, "/")[1].decode())
    assert tags["og:title"] == "The Lynch Pin · Quant Portal" and tags["og:image"].endswith("/card/home.jpg")
    tags = meta(get(httpd, "/?t=<script>")[1].decode())  # not a ticker: the home page's card
    assert tags["og:title"] == "The Lynch Pin · Quant Portal"


def test_tags_are_escaped_and_a_bad_host_is_dropped():
    class Scans:
        def get(self, kind):
            return {"title": 'Q"Q<Q', "date": "2026-11-10", "subtitle": "Nasdaq 100",
                    "posts": [{"text": "MARKET CLOSE <b>& more</b>", "image": {"src": "/scans/qqq/q.jpg?v=1"}}]}
    html = cards.link_meta("https://lynch.example.com", {"scan": ["qqq"]}, scans=Scans(), ticker_re=TICKER_RE)
    tags = meta(html)
    assert tags["og:title"] == "Q&quot;Q&lt;Q scan · 2026-11-10 | The Lynch Pin"
    assert tags["og:description"] == "MARKET CLOSE &lt;b&gt;&amp; more&lt;/b&gt;"
    assert tags["og:image"] == "https://lynch.example.com/scans/qqq/q.jpg?v=1"
    assert cards.origin("lynch.example.com", https=True) == "https://lynch.example.com"
    assert cards.origin("127.0.0.1:8765", https=False) == "http://127.0.0.1:8765"
    assert cards.origin('evil.com"><script>', https=True) is None
    assert cards.origin("", https=True) is None


def test_ticker_text_and_versions():
    entry = {"status": "done", "name": "Microsoft", "generated_at": 1794680189.5,
             "stats": {"PEG": 1.74, "Mean": 1.79, "SD": 0.2, "Dev_SD": -0.25, "Base": 15.4, "Bull": 17.0,
                       "Bear": 14.0, "growth_pct": 14.0, "history": "ok"},
             "income": {"grade": "A+"}, "credit": {"rating": "AAA"}}
    title, desc = cards.ticker_text("MSFT", entry)
    assert title == "$MSFT · Microsoft: PEG 1.74, 5Y base ROI 15.4% | The Lynch Pin"
    assert desc == ("-0.2 SD vs its 5Y mean PEG 1.79 · 5Y ROI bull / base / bear 17 / 15 / 14% · "
                    "5Y EPS growth 14.0% · Income A+ · Credit AAA. Not financial advice.")
    assert cards.card_version(entry) == "1794680189"
    nodata = {"status": "nodata", "name": "SPDR S&P 500", "reason": "no GARP data (ETF)"}
    assert cards.ticker_text("SPY", nodata)[1] == "SPDR S&P 500: no GARP data (ETF). Not financial advice."
    assert cards.card_version(nodata) == "g" and cards.card_version(None) == "g"
    # every layout draws: a far-off PEG, no 5-year history, a very long name
    for st in ({"Dev_SD": -4.0, "SD": 0.1, "Mean": 1.0, "PEG": 0.6}, {"Dev_SD": 0.0, "SD": 0.0, "history": "unavailable"}):
        e = dict(entry, name="A" * 120, stats=dict(entry["stats"], **st))
        assert Image.open(io.BytesIO(cards.render_ticker("MSFT", e))).size == (1200, 630)


def test_card_cache_draws_each_card_once():
    c = cards.CardCache(limit=2)
    drawn = []
    draw = lambda k: (drawn.append(k), k.encode())[1]  # noqa: E731
    assert c.get(("A", "1"), lambda: draw("A")) == b"A"
    assert c.get(("A", "1"), lambda: draw("A")) == b"A"
    c.get(("B", "1"), lambda: draw("B"))
    c.get(("C", "1"), lambda: draw("C"))  # evicts A
    c.get(("A", "1"), lambda: draw("A"))
    assert drawn == ["A", "B", "C", "A"]
