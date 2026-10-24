"""CVE-2026-111497 (Brotli "Node2Shell" / "HTTP2Shell"): the portal's outbound sessions (Yahoo, SEC, FMP, multpl.com)
never hand a reply to libcurl's bundled Brotli decoder (engine/http_session.py)."""
import gzip
import http.server
import threading
import zlib

import pytest
from curl_cffi import CurlOpt
from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import ContentDecodingError

from engine.http_session import BrowserSession, browser_session

BODY = b'{"ok": 1}'
REPLIES = {  # path → (Content-Encoding, body)
    "/br": ("br", b"notbrotl"),  # junk, not a crafted stream: a decoder that runs rejects it
    "/gzip": ("gzip", gzip.compress(BODY)),
    "/deflate": ("deflate", zlib.compress(BODY)),
    "/rawdeflate": ("deflate", zlib.compress(BODY)[2:-4]),
    "/plain": (None, BODY),
}


@pytest.fixture
def server():
    """Loopback server answering GET /<path> with REPLIES[path]; records each request's Accept-Encoding."""
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Accept-Encoding"))
            enc, body = REPLIES[self.path]
            self.send_response(200)
            if enc:
                self.send_header("Content-Encoding", enc)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    httpd.shutdown()
    httpd.server_close()


def test_a_plain_chrome_session_decodes_br_even_when_not_asked(server):
    """Why the module exists: impersonating Chrome sends br in a plain header, and libcurl decodes any encoding it
    knows — so taking br out of CURLOPT_ACCEPT_ENCODING alone changes nothing."""
    url, seen = server
    with Session(impersonate="chrome", curl_options={CurlOpt.ACCEPT_ENCODING: "gzip, deflate"}) as s:
        with pytest.raises(Exception, match=r"\(61\)"):  # CURLE_BAD_CONTENT_ENCODING: the Brotli decoder ran
            s.get(url + "/br", timeout=5)
    assert "br" in seen[-1]


def test_browser_session_refuses_br_without_decoding_it(server):
    url, seen = server
    with browser_session(headers={"X-Test": "1"}, curl_options={CurlOpt.MAXREDIRS: 3}) as s:
        with pytest.raises(ContentDecodingError, match="refusing Content-Encoding 'br'"):  # not curl's (61)
            s.get(url + "/br", timeout=5)
    assert seen[-1] == "gzip, deflate"


@pytest.mark.parametrize("path", ["/gzip", "/deflate", "/rawdeflate", "/plain"])
def test_browser_session_decodes_gzip_and_deflate_in_python(server, path):
    url, _ = server
    with browser_session() as s:
        r = s.get(url + path, timeout=5)
    assert r.content == BODY and r.json() == {"ok": 1}


def test_every_portal_session_is_a_browser_session():
    from engine import growth_estimator, lynch_pin_core
    from ui.tradingview import YahooFetcher
    for s in (lynch_pin_core.GLOBAL_SESSION, lynch_pin_core._SEC_SESSION, growth_estimator._SESSION,
              YahooFetcher().session()):
        assert isinstance(s, BrowserSession)
        assert s.curl_options.get(CurlOpt.HTTP_CONTENT_DECODING) == 0
        assert s.headers.get("Accept-Encoding") == "gzip, deflate"


def test_a_decompression_bomb_or_truncated_body_is_refused(monkeypatch):
    from engine import http_session
    monkeypatch.setattr(http_session, "MAX_DECODED", 1000)
    with pytest.raises(ContentDecodingError, match="more than"):
        http_session.DECODERS["gzip"](gzip.compress(b"0" * 5000))
    with pytest.raises(ContentDecodingError, match="truncated"):
        http_session.DECODERS["gzip"](gzip.compress(BODY)[:-6])
    assert http_session.DECODERS["deflate"](zlib.compress(b"0" * 1000)) == b"0" * 1000
