"""curl_cffi sessions that impersonate Chrome (Yahoo throttles other TLS fingerprints sooner) but never decompress a
response inside libcurl.

CVE-2026-111497 ("Node2Shell" / "HTTP2Shell"): the Brotli decoder in Google's brotli 1.0.3-1.2.0 can be made to
write out of bounds by a crafted ``Content-Encoding: br`` stream. curl_cffi's bundled libcurl-impersonate links it
(brotli/1.2.0 in curl_cffi 0.16.3, brotli/1.1.0 in 0.13.0) and decodes any encoding it knows, whatever the request
asked for; impersonating Chrome also sends ``Accept-Encoding: gzip, deflate, br, zstd`` as a plain header, so
dropping ``br`` from CURLOPT_ACCEPT_ENCODING changes nothing. So libcurl's decoding is switched off, the session
asks for gzip / deflate only, and decodes those in Python (zlib, not Brotli). Yahoo, SEC and multpl.com answer
Chrome's ``br`` with gzip anyway, so the bytes on the wire stay the same. A reply in any other encoding (br, zstd)
never meets a decoder: it fails as a ContentDecodingError. ``stream=True`` replies come back as sent.

Remove once curl_cffi ships libcurl-impersonate with brotli >= 1.2.1 (check: ``curl_cffi.Curl().version()``).
"""
import gzip
import zlib

from curl_cffi import CurlOpt
from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import ContentDecodingError

NO_DECODING = {CurlOpt.HTTP_CONTENT_DECODING: 0}
ACCEPT = {"Accept-Encoding": "gzip, deflate"}


def _inflate(data):
    """deflate as servers send it: zlib-wrapped (RFC 9110) or raw."""
    try:
        return zlib.decompress(data)
    except zlib.error:
        return zlib.decompress(data, -zlib.MAX_WBITS)


DECODERS = {"gzip": gzip.decompress, "x-gzip": gzip.decompress, "deflate": _inflate}


class BrowserSession(Session):
    """A curl_cffi Session whose gzip / deflate replies are decoded in Python and others are refused."""

    def request(self, *args, **kw):
        r = super().request(*args, **kw)
        enc = (r.headers.get("content-encoding") or "").strip().lower()
        if enc not in ("", "identity") and r.content and not kw.get("stream"):
            decode = DECODERS.get(enc)
            if decode is None:
                raise ContentDecodingError(f"refusing Content-Encoding {enc!r}: only gzip and deflate are decoded")
            try:
                r.content = decode(r.content)
            except (OSError, EOFError, zlib.error) as e:
                raise ContentDecodingError(f"bad {enc} body: {e}") from None
        return r


def browser_session(**kw):
    """A ``BrowserSession(impersonate="chrome", **kw)``: libcurl never decompresses its replies."""
    kw.setdefault("impersonate", "chrome")
    kw["headers"] = {**(kw.get("headers") or {}), **ACCEPT}
    kw["curl_options"] = {**(kw.get("curl_options") or {}), **NO_DECODING}  # CurlOpt keys: no dict(**)
    return BrowserSession(**kw)
