"""Latest scans: the per-kind scan archive (social/scan_archive.py), its index (ui/scans.py) and routes."""
import json
import os
import time

import pytest
from PIL import Image

from social.scan_archive import parse_log, save_scan, scan_kind, split_ai_overviews
from ui.config import Settings
from ui.scans import ScanArchive, scan_day
from ui.server import PortalApp
from ui.tests.test_server import get, serve


def png(path, color=(93, 156, 236)):
    Image.new("RGB", (360, 210), color).save(path)
    return str(path)


def make_scan(root, tmp, kind, day, tickers=("AMZN", "GOOGL"), ai=None, color=(93, 156, 236)):
    posts = [{"text": f"🚨 MARKET CLOSE: ${kind.upper()} #LynchPin Detector", "image": png(tmp / f"{kind}_bench.png"),
              "name": f"{kind}_benchmark.png"}]
    for t in tickers:
        posts.append({"ticker": t, "text": f"${t}\n\n🤖: {t} overview", "image": png(tmp / f"{t}_valuation.png", color)})
    posts.append({"text": "⚠️ DISCLAIMER"})
    return save_scan(str(root), kind, posts, day=day, ai=ai)


# ── archive (main.py side) ────────────────────────────────────────────────────
def test_scan_kind_per_run_type():
    assert scan_kind("MAGS", "database/mag7.txt") == "mags"
    assert scan_kind("QQQ", "database/nasdaq_100.txt") == "qqq"
    assert scan_kind("SPY", weekly=True) == "fintwit"
    assert scan_kind("SPY", portfolio=True) == "portfolio"
    assert scan_kind("SPY", "database/My Picks.txt") == "my-picks"


def test_each_kind_keeps_its_own_charts(tmp_path):
    root, src = tmp_path / "images", tmp_path / "src"
    src.mkdir()
    make_scan(root, src, "mags", "2026-10-05", ai={"sentiment": "cheap", "tickers": {"GOOGL": "A++"}})
    mags_googl = (root / "mags" / "GOOGL_valuation.png").read_bytes()
    # Saturday's GOOGL chart (same tmp/ file name) differs
    make_scan(root, src, "fintwit", "2026-10-10", tickers=("GOOGL", "HD"), color=(255, 0, 0))
    assert (root / "mags" / "GOOGL_valuation.png").read_bytes() == mags_googl
    assert (root / "fintwit" / "GOOGL_valuation.png").read_bytes() != mags_googl
    doc = json.loads((root / "mags" / "scan.json").read_text())
    assert doc["title"] == "MAGS" and doc["subtitle"] == "Magnificent 7" and doc["date"] == "2026-10-05"
    assert [p.get("image") for p in doc["posts"]] == ["mags_benchmark.png", "AMZN_valuation.png", "GOOGL_valuation.png", None]
    assert doc["ai"] == {"sentiment": "cheap", "tickers": {"GOOGL": "A++"}}
    assert json.loads((root / "fintwit" / "scan.json").read_text())["title"] == "X Favorite 100"


def test_next_run_of_a_kind_replaces_only_that_folder(tmp_path):
    root, src = tmp_path / "images", tmp_path / "src"
    src.mkdir()
    make_scan(root, src, "mags", "2026-09-28", tickers=("AMZN", "META"))
    make_scan(root, src, "qqq", "2026-09-29")
    make_scan(root, src, "mags", "2026-10-05", tickers=("NVDA",))
    assert sorted(os.listdir(root / "mags")) == ["NVDA_valuation.png", "mags_benchmark.png", "scan.json"]
    assert sorted(os.listdir(root)) == ["mags", "qqq"]  # no staging leftovers
    assert json.loads((root / "mags" / "scan.json").read_text())["date"] == "2026-10-05"


def test_split_ai_overviews_is_anchored_to_line_start():
    bulk = "$ON:\n🤖: ON is cheap; unlike $AMD it\n\n$AMD\n🤖: AMD text"
    assert split_ai_overviews(bulk, ["ON", "AMD", "MSFT"]) == {"ON": "🤖: ON is cheap; unlike $AMD it", "AMD": "🤖: AMD text"}


LOG = """🚀 Executing: /venv/bin/python main.py --src database/mag7.txt --top 4 --excl-bad --post --post_threads
📰 Sentiment: MAGS looks cheap.

--- 📝 PREVIEW OF POST ---
MAIN TWEET:
🚨 MARKET CLOSE: $MAGS #LynchPin Detector

1️⃣ AMZN: PEG 1.2

[Attach: tmp/benchmark_comparison.png]

REPLY TWEET (AMZN):
$AMZN

🤖: AMZN overview.
[Attach: tmp/AMZN_valuation.png]

REPLY TWEET (BRK-B):
$BRK-B

🤖: BRK overview.
[Attach: tmp/BRK-B_valuation.png]

FOOTER:
@grok Which one?

⚠️ DISCLAIMER: DYOR. 🫶
--------------------------

🚀 Posting Market Analysis Thread to X...
"""


def test_parse_log_recovers_the_thread():
    argv, posts = parse_log(LOG)
    assert "--src" in argv and "database/mag7.txt" in argv
    assert [p.get("ticker") for p in posts] == [None, "AMZN", "BRK-B", None]
    assert posts[0]["image"] == "tmp/benchmark_comparison.png"
    assert posts[0]["text"].endswith("1️⃣ AMZN: PEG 1.2")
    assert posts[1] == {"ticker": "AMZN", "text": "$AMZN\n\n🤖: AMZN overview.", "image": "tmp/AMZN_valuation.png"}
    assert posts[3]["text"].startswith("@grok") and "image" not in posts[3]
    assert parse_log("no preview here") is None


# ── index (portal side) ───────────────────────────────────────────────────────
@pytest.fixture
def week(tmp_path):
    """Eight scans on eight days (the oldest MAGS run is replaced by a newer one → 7 + an extra kind)."""
    root, src = tmp_path / "images", tmp_path / "src"
    src.mkdir()
    days = {"qqq": "2026-09-29", "schd": "2026-09-30", "smh": "2026-10-01", "igv": "2026-10-02",
            "fintwit": "2026-10-03", "portfolio": "2026-10-04", "mags": "2026-10-05", "sp-extra": "2026-09-27"}
    for kind, day in days.items():
        make_scan(root, src, kind, day)
    return root


def test_summaries_newest_first_limited_to_seven(week, tmp_path):
    arc = ScanArchive(str(week), str(tmp_path / "cache"))
    s = arc.summaries()["scans"]
    assert [x["kind"] for x in s] == ["mags", "portfolio", "fintwit", "igv", "smh", "schd", "qqq"]
    assert [x["title"] for x in s][:3] == ["MAGS", "Portfolio X-Ray", "X Favorite 100"]
    head = s[0]
    assert head["text"].startswith("🚨 MARKET CLOSE") and head["tickers"] == ["AMZN", "GOOGL"] and head["posts"] == 4
    assert head["image"]["src"].startswith("/scans/mags/mags_benchmark.jpg?v=")
    assert head["image"]["full"].startswith("/scans/mags/mags_benchmark.png?v=")


def test_index_is_cached_and_reloaded_when_a_scan_changes(week, tmp_path):
    arc = ScanArchive(str(week), str(tmp_path / "cache"), check_s=3600)
    assert arc.summaries()["scans"][0]["kind"] == "mags"
    src = tmp_path / "src2"
    src.mkdir()
    make_scan(week, src, "qqq", "2026-10-06")
    assert arc.summaries()["scans"][0]["kind"] == "mags"  # cached until the next check
    arc.check_s = 0
    time.sleep(0.01)
    assert arc.summaries()["scans"][0]["kind"] == "qqq"


def test_scan_day_rolls_over_at_3pm():
    import datetime as dt
    assert scan_day(dt.datetime(2026, 10, 5, 14, 59)) == "2026-10-04"
    assert scan_day(dt.datetime(2026, 10, 5, 15, 0)) == "2026-10-05"


def test_malformed_scans_and_paths_are_ignored(week, tmp_path):
    (week / "broken").mkdir()
    (week / "broken" / "scan.json").write_text("{nope")
    (week / "Bad Kind").mkdir()
    (week / "Bad Kind" / "scan.json").write_text(json.dumps({"date": "2026-10-09", "posts": [{"text": "x"}]}))
    arc = ScanArchive(str(week), str(tmp_path / "cache"))
    kinds = {x["kind"] for x in arc.summaries()["scans"]}
    assert "broken" not in kinds and "Bad Kind" not in kinds
    assert arc.get("broken") is None and arc.get("../mags") is None
    assert arc.image_path("mags", "AMZN_valuation.png").endswith(os.path.join("mags", "AMZN_valuation.png"))
    for kind, name in [("mags", "../qqq/scan.json"), ("mags", "scan.json"), ("mags", "HD_valuation.png"),
                       ("..", "AMZN_valuation.png"), ("mags", "AMZN_valuation.gif")]:
        assert arc.image_path(kind, name) is None, (kind, name)


def test_full_scan_carries_posts_and_ai(tmp_path):
    root, src = tmp_path / "images", tmp_path / "src"
    src.mkdir()
    make_scan(root, src, "portfolio", "2026-10-04", ai={"sentiment": "s", "portfolio": "🐂 Bull", "tickers": {"AMZN": "t"}})
    scan = ScanArchive(str(root), str(tmp_path / "cache")).get("portfolio")
    assert scan["title"] == "Portfolio X-Ray"
    assert scan["ai"] == {"sentiment": "s", "portfolio": "🐂 Bull", "tickers": {"AMZN": "t"}}
    assert [p.get("ticker") for p in scan["posts"]] == [None, "AMZN", "GOOGL", None]
    assert scan["posts"][1]["image"]["full"].startswith("/scans/portfolio/AMZN_valuation.png?v=")


# ── HTTP ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def server(week, tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    s.scans_dir = str(week)
    httpd = serve(PortalApp(s))
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_scan_routes(server):
    r, body = get(server, "/api/scans")
    assert r.status == 200
    scans = json.loads(body)["scans"]
    assert len(scans) == 7 and scans[0]["kind"] == "mags"
    r, body = get(server, "/api/scans/mags")
    assert r.status == 200 and json.loads(body)["posts"][1]["ticker"] == "AMZN"
    r, body = get(server, scans[0]["image"]["full"])
    assert r.status == 200 and r.getheader("Content-Type") == "image/png" and body.startswith(b"\x89PNG")
    assert "max-age" in r.getheader("Cache-Control")
    r, body = get(server, scans[0]["image"]["src"])
    assert r.status == 200 and r.getheader("Content-Type") == "image/jpeg" and body[:2] == b"\xff\xd8"
    for path in ["/api/scans/nope", "/api/scans/..%2Fmags", "/scans/mags/scan.json", "/scans/mags/../qqq/qqq_benchmark.png",
                 "/scans/nope/AMZN_valuation.png", "/scans/mags/HD_valuation.png"]:
        assert get(server, path)[0].status == 404, path


def test_page_has_latest_scans_and_home_button(server):
    _, body = get(server, "/")
    page = body.decode()
    assert 'id="scans"' in page and "Latest scans" in page and 'id="home-btn"' in page and 'id="thread"' in page
    assert page.index('id="search-section"') < page.index('id="scans"')  # under the ticker search
    r, body = get(server, "/static/scans.js")
    assert r.status == 200 and b"LynchScans" in body
    r, body = get(server, "/api/health")
    assert json.loads(body)["features"]["scans"] is True
