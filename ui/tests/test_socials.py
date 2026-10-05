"""Socials: the cached "Latest on X" feed (fake X, no network) and the profile links."""
import json
import os
import time

import pytest
from PIL import Image

from ui import socials as soc
from ui.config import Settings
from ui.server import PortalApp
from ui.socials import SocialFeed, read_tokens
from ui.tests.test_server import get, serve

TOKENS = "".join(f'export {k}="{k.lower()}-value"\n' for k in soc.TOKEN_NAMES)


def post(i, image=True):
    return {"id": str(1000 + i), "text": f"$T{i}\n\n🤖: post {i}", "time": "2026-10-05T20:07:54+00:00",
            "url": f"https://x.com/{soc.HANDLE}/status/{1000 + i}", "likes": i, "replies": 1, "reposts": 0,
            "views": 10 * i, "image_src": f"https://pbs.twimg.com/media/{i}.jpg" if image else None}


class FakeX:
    def __init__(self, posts=None, fail=False):
        self.calls, self.posts, self.fail = [], posts if posts is not None else [post(1), post(2, image=False)], fail

    def __call__(self, tok, user_id=None):
        self.calls.append((dict(tok), user_id))
        if self.fail:
            raise RuntimeError("429 Too Many Requests")
        return "42", [dict(p) for p in self.posts]


def fake_download(src, dest):
    Image.new("RGB", (64, 64), (93, 156, 236)).save(dest, "JPEG")


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / "activate"
    p.write_text("# venv\nexport PATH=/x\n" + TOKENS)
    return str(p)


def wait_idle(feed):
    for _ in range(200):
        if not feed._running:
            return
        time.sleep(0.01)


def test_read_tokens_file_first_then_env(tmp_path, monkeypatch):
    p = tmp_path / "activate"
    p.write_text('export X_API_KEY="from file"\nexport OTHER=1\nexport X_API_SECRET=\n')
    monkeypatch.setenv("X_API_SECRET", "from env")
    monkeypatch.setenv("X_API_KEY", "env loses")
    tok = read_tokens(str(p))
    assert tok == {"X_API_KEY": "from file", "X_API_SECRET": "from env"}
    assert read_tokens(str(tmp_path / "missing")) == {"X_API_KEY": "env loses", "X_API_SECRET": "from env"}


def test_refresh_stores_posts_and_images(tmp_path, env_file):
    x = FakeX()
    feed = SocialFeed(str(tmp_path / "cache"), env_file, fetch=x, download=fake_download)
    feed.refresh()
    assert x.calls[0][0]["X_ACCESS_TOKEN"] == "x_access_token-value" and x.calls[0][1] is None
    snap = feed.snapshot()
    assert [p["url"] for p in snap["x"]] == [f"https://x.com/{soc.HANDLE}/status/1001", f"https://x.com/{soc.HANDLE}/status/1002"]
    assert snap["x"][0]["image"] == "/social/x_1001.jpg" and "image" not in snap["x"][1]
    assert snap["x"][0]["likes"] == 1 and snap["x"][0]["views"] == 10
    assert "image_src" not in json.dumps(snap) and "value" not in json.dumps(snap)  # no remote URLs, no tokens
    assert feed.image_path("x_1001.jpg").endswith("x_1001.jpg")
    for bad in ["x_1002.jpg", "../feed.json", "feed.json", "x_1001.png", "threads_1.jpg"]:
        assert feed.image_path(bad) is None, bad
    # a new server process starts from the disk cache, without calling X
    again = SocialFeed(str(tmp_path / "cache"), env_file, fetch=FakeX(fail=True), download=fake_download)
    assert [p["text"] for p in again.snapshot()["x"]] == [p["text"] for p in snap["x"]]
    wait_idle(again)


def test_one_x_read_per_scan_day(tmp_path, env_file, monkeypatch):
    x = FakeX()
    feed = SocialFeed(str(tmp_path / "cache"), env_file, fetch=x, download=fake_download)
    first = feed.snapshot()
    assert first["refreshing"] is True and first["x"] == []
    wait_idle(feed)
    for _ in range(5):
        assert len(feed.snapshot()["x"]) == 2
    assert len(x.calls) == 1
    monkeypatch.setattr(soc, "scan_day", lambda: "2099-01-01")  # 3 PM the next day
    feed.snapshot()
    wait_idle(feed)
    assert len(x.calls) == 2 and x.calls[1][1] == "42"  # user id remembered: no second get_me


def test_old_images_are_removed(tmp_path, env_file):
    x = FakeX()
    feed = SocialFeed(str(tmp_path / "cache"), env_file, fetch=x, download=fake_download)
    feed.refresh()
    x.posts = [post(3)]
    feed.refresh()
    assert sorted(f for f in os.listdir(tmp_path / "cache" / "socials") if f.endswith(".jpg")) == ["x_1003.jpg"]


def test_failed_read_keeps_last_posts_and_waits_an_hour(tmp_path, env_file, monkeypatch):
    feed = SocialFeed(str(tmp_path / "cache"), env_file, fetch=FakeX(), download=fake_download)
    feed.refresh()
    failing = FakeX(fail=True)
    feed._fetch = failing
    monkeypatch.setattr(soc, "scan_day", lambda: "2099-01-01")
    feed.snapshot()
    wait_idle(feed)
    assert len(failing.calls) == 1 and len(feed.snapshot()["x"]) == 2
    wait_idle(feed)
    feed.snapshot()
    wait_idle(feed)
    assert len(failing.calls) == 1  # no retry within the hour


def test_failed_image_still_shows_the_post(tmp_path, env_file):
    def broken(src, dest):
        raise OSError("timeout")
    feed = SocialFeed(str(tmp_path / "cache"), env_file, fetch=FakeX(), download=broken)
    feed.refresh()
    assert [("image" in p) for p in feed.snapshot()["x"]] == [False, False]


def test_without_tokens_links_only(tmp_path):
    x = FakeX()
    feed = SocialFeed(str(tmp_path / "cache"), str(tmp_path / "missing"), fetch=x, download=fake_download)
    feed.refresh()
    assert x.calls == [] and feed.snapshot()["x"] == []


# ── HTTP ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def server(tmp_path, env_file):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    feed = SocialFeed(s.cache_dir, env_file, fetch=FakeX(), download=fake_download)
    feed.refresh()
    httpd = serve(PortalApp(s, socials=feed))
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_social_routes(server):
    r, body = get(server, "/api/socials")
    d = json.loads(body)
    assert r.status == 200 and d["handle"] == soc.HANDLE and len(d["x"]) == 2
    r, body = get(server, d["x"][0]["image"])
    assert r.status == 200 and r.getheader("Content-Type") == "image/jpeg"
    for path in ["/social/x_1002.jpg", "/social/feed.json", "/social/..%2Ffeed.json", "/social/x_9.jpg"]:
        assert get(server, path)[0].status == 404, path


def test_socials_off_serves_links_only(tmp_path):
    s = Settings()  # conftest sets LYNCH_UI_SOCIALS=0
    s.cache_dir = str(tmp_path / "cache")
    app = PortalApp(s)
    assert app.socials is None
    httpd = serve(app)
    try:
        assert json.loads(get(httpd, "/api/socials")[1]) == {"handle": soc.HANDLE, "x": [], "refreshing": False}
        assert get(httpd, "/social/x_1001.jpg")[0].status == 404
        page = get(httpd, "/")[1].decode()
        for url in ["https://x.com/lynch_pin_quant", "https://www.instagram.com/lynch_pin_quant/",
                    "https://www.threads.com/@lynch_pin_quant"]:
            assert f'href="{url}" target="_blank" rel="noopener noreferrer"' in page, url
        assert page.index('id="scans"') < page.index('id="socials"')
        assert get(httpd, "/static/socials.js")[0].status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
