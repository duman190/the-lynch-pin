"""Step 1/2: HTTP server, static assets, network guard."""
import http.client
import json
import os
import threading

import pytest

from ui.config import Settings
from ui.netguard import is_allowed_host, is_local_client
from ui.server import PortalApp, PortalServer, make_handler, to_json


def serve(app):
    httpd = PortalServer(("127.0.0.1", 0), make_handler(app))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


@pytest.fixture
def server(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    httpd = serve(PortalApp(s))
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def get(httpd, path, host=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    headers = {"Host": host} if host else {}
    conn.request(method, path, headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r, body


def test_index_served_with_security_headers(server):
    r, body = get(server, "/")
    assert r.status == 200
    assert b"Quant Portal" in body
    assert r.getheader("Content-Type").startswith("text/html")
    assert "default-src 'self'" in r.getheader("Content-Security-Policy")
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    assert r.getheader("X-Frame-Options") == "DENY"
    assert int(r.getheader("Content-Length")) == len(body)


def test_static_assets(server):
    for path, ctype in [("/static/app.css", "text/css"), ("/static/app.js", "javascript"),
                        ("/static/img/hero_wide.jpg", "image/jpeg"), ("/static/img/hero_square.webp", "image/webp"), ("/static/img/logo.png", "image/png"),
                        ("/manifest.webmanifest", "manifest+json"), ("/favicon.ico", "image/png")]:
        r, body = get(server, path)
        assert r.status == 200, path
        assert ctype in r.getheader("Content-Type"), path
        assert body


@pytest.mark.parametrize("path", ["/static/../config.py", "/static/..%2Fconfig.py", "/static/img/../../server.py",
                                  "/scan/../main.py", "/scan/secret.txt", "/scan/batch_results.csv",
                                  "/static/.cache/x.png", "/nope"])
def test_traversal_and_unknown_paths_rejected(server, path):
    r, _ = get(server, path)
    assert r.status == 404


def test_health(server):
    r, body = get(server, "/api/health")
    assert r.status == 200
    h = json.loads(body)
    assert h["ok"] is True
    assert h["features"] == {"search": False, "ai": False}
    assert r.getheader("Cache-Control") == "no-store"


def test_latest_scan_section_removed(server):
    _, body = get(server, "/")
    assert b"Latest scan" not in body and b'id="gallery"' not in body
    for path in ["/api/scan", "/scan/MSFT_valuation.png", "/scan/thumb/MSFT_valuation.png"]:
        assert get(server, path)[0].status == 404, path


def test_search_disabled_without_jobs(server):
    r, _ = get(server, "/api/ticker/MSFT")
    assert r.status == 404


def test_writes_refused(server):
    r, _ = get(server, "/api/health", method="POST")
    assert r.status == 405


def test_foreign_host_header_refused(server):
    r, _ = get(server, "/api/health", host="evil.example.com")
    assert r.status == 403
    r, _ = get(server, "/api/health", host="192.168.1.20:8765")
    assert r.status == 200


def test_head_has_no_body(server):
    r, body = get(server, "/api/health", method="HEAD")
    assert r.status == 200 and body == b""


@pytest.mark.parametrize("ip,ok", [
    ("127.0.0.1", True), ("::1", True), ("192.168.1.7", True), ("10.0.0.3", True), ("172.16.5.5", True),
    ("169.254.1.1", True), ("fe80::1", True), ("fd00::5", True), ("::ffff:192.168.0.2", True),
    ("8.8.8.8", False), ("2001:4860::8888", False), ("0.0.0.0", False), ("garbage", False), ("", False),
    ("0.0.0.1", False), ("240.0.0.1", False), ("198.18.0.1", False), ("192.0.2.1", False),
    ("2001:db8::1", False), ("100.64.0.1", False), ("172.32.0.1", False),
])
def test_is_local_client(ip, ok):
    assert is_local_client(ip) is ok


@pytest.mark.parametrize("host,ok", [
    ("localhost:8765", True), ("127.0.0.1:8765", True), ("192.168.1.9", True), ("[::1]:8765", True),
    ("my-desktop:8765", True), ("my-desktop.local", True), ("box.lan:80", True),
    ("evil.example.com", False), ("8.8.8.8", False), ("", False), ("a_b:1", False),
])
def test_is_allowed_host(host, ok):
    assert is_allowed_host(host) is ok


def test_allowed_nets_env_for_tailscale(monkeypatch):
    monkeypatch.setenv("LYNCH_UI_ALLOWED_NETS", "100.64.0.0/10, bogus")
    assert is_local_client("100.100.1.2")
    assert not is_local_client("8.8.8.8")


def test_allowed_hosts_env(monkeypatch):
    monkeypatch.setenv("LYNCH_UI_ALLOWED_HOSTS", "portal.example.org")
    assert is_allowed_host("portal.example.org:8765")


def test_to_json_handles_numpy_and_nan():
    import numpy as np
    out = json.loads(to_json({"a": np.float64(1.5), "b": float("nan"), "c": np.int64(3), "d": (1, 2),
                              "e": np.float32("inf")}))
    assert out == {"a": 1.5, "b": None, "c": 3, "d": [1, 2], "e": None}


def test_to_json_arrays_series_keys_dates():
    import datetime
    import numpy as np
    import pandas as pd
    out = json.loads(to_json({
        "arr": np.array([1.0, np.nan, 3.0]),
        "ser": pd.Series([1.5, np.nan], index=["x", "y"]),
        np.int64(7): "np-key",
        "ts": pd.Timestamp("2026-09-27"),
        "d": datetime.date(2026, 9, 27),
        "nested": [{"v": np.float32(2.5)}],
    }))
    assert out["arr"] == [1.0, None, 3.0]
    assert out["ser"] == {"x": 1.5, "y": None}
    assert out["7"] == "np-key"
    assert out["ts"].startswith("2026-09-27") and out["d"] == "2026-09-27"
    assert out["nested"] == [{"v": 2.5}]


def test_etag_revalidation_returns_304(server):
    r, body = get(server, "/static/app.js")
    etag = r.getheader("ETag")
    assert etag and r.getheader("Cache-Control") == "no-cache" and r.getheader("Last-Modified")
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("GET", "/static/app.js", headers={"If-None-Match": etag})
    r2 = conn.getresponse()
    assert r2.status == 304 and r2.read() == b"" and r2.getheader("ETag") == etag
    conn.request("GET", "/static/app.js", headers={"If-None-Match": '"stale"'})
    r3 = conn.getresponse()
    assert r3.status == 200 and r3.read() == body
    conn.close()
    r, _ = get(server, "/static/img/logo.png")
    assert "max-age=86400" in r.getheader("Cache-Control")


def test_uppercase_host_and_absolute_uri(server):
    r, _ = get(server, "/api/health", host="LOCALHOST:8765")
    assert r.status == 200
    port = server.server_address[1]
    r, _ = get(server, f"http://127.0.0.1:{port}/api/health", host="evil.example.com")
    assert r.status in (403, 404)  # never served under a foreign Host


def test_log_line_escapes_control_chars(server, capfd):
    import socket
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
        sock.sendall(b"GET /\x1b[31mred HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        while sock.recv(4096):
            pass
    err = capfd.readouterr().err
    assert "\x1b" not in err and "\\x1b" in err


def test_default_bind_is_loopback(monkeypatch):
    monkeypatch.delenv("LYNCH_UI_HOST", raising=False)
    monkeypatch.delenv("LYNCH_UI_LAN", raising=False)
    s = Settings()
    assert s.bind_host == "127.0.0.1"
    s.lan = True
    assert s.bind_host == "0.0.0.0"


def test_jpeg_preview_is_small_and_cached(tmp_path):
    import io
    from PIL import Image
    from ui.imaging import jpeg_preview
    src = tmp_path / "AAPL_valuation.png"
    Image.new("RGB", (3000, 1800), "#121212").save(src)
    out = jpeg_preview(str(src), str(tmp_path / "prev"), 900)
    with open(out, "rb") as f:
        body = f.read()
    assert body[:2] == b"\xff\xd8" and Image.open(io.BytesIO(body)).width == 900
    assert jpeg_preview(str(src), str(tmp_path / "prev"), 900) == out  # reused
    assert jpeg_preview(str(tmp_path / "missing.png"), str(tmp_path / "prev"), 900) is None


def test_concurrent_previews_share_one_render(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    from ui.imaging import jpeg_preview
    src = tmp_path / "NVDA_valuation.png"
    Image.new("RGB", (3000, 1800), "#5D9CEC").save(src)
    out_dir = tmp_path / "prev"
    with ThreadPoolExecutor(8) as ex:
        paths = list(ex.map(lambda _: jpeg_preview(str(src), str(out_dir), 1100), range(16)))
    assert len(set(paths)) == 1 and paths[0].endswith(".jpg")
    with open(paths[0], "rb") as f:
        assert f.read(2) == b"\xff\xd8"
    assert not [n for n in os.listdir(out_dir) if n.endswith(".part")]
