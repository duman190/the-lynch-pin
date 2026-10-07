"""Lynch Pin Quant Portal — stdlib HTTP server.

    python -m ui.server                 # http://127.0.0.1:8765 (this machine only)
    python -m ui.server --lan           # reachable from phones/PCs on the same private network
                                        # + the stats page on port 190 (ui/stats.py)
    python -m ui.server --public        # behind a tunnel on this machine; the stats page on port 190
                                        #   answers LAN / Tailscale clients only

No authentication: --lan exposes the portal to every device on your LAN (peer-IP and
Host-header checks refuse anything outside loopback / private / link-local ranges).
"""
import argparse
import datetime
import email.utils
import json
import math
import mimetypes
import os
import re
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

if __package__ in (None, ""):  # allow `python ui/server.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

try:
    import pandas as pd  # noqa: E402
except ImportError:  # pragma: no cover
    pd = None

from ui.config import CPU_WORKERS, WORKER_MB, REPO_ROOT, Settings, available_mb  # noqa: E402
from ui import netguard  # noqa: E402
from ui.netguard import _ip, is_allowed_host, is_local_client  # noqa: E402
from ui.stats import DEFAULT_REASON  # noqa: E402

# A visitor's action (stats page): a scan thread opened, or a ticker looked up (not /ai, /deepdive under it)
USER_ACTION_RE = re.compile(r"^/api/(?:scans|ticker)/[^/]+$")

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
STATIC_FILE_RE = re.compile(r"^(?:[a-z0-9_\-]+/)?[A-Za-z0-9_\-]+\.(?:html|css|js|png|jpg|jpeg|webp|svg|webmanifest|ico)$")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'"),
    "Cross-Origin-Resource-Policy": "same-origin",
}
mimetypes.add_type("application/manifest+json", ".webmanifest")


def _sanitize(o):
    """Coerce engine output into strict JSON: numpy/pandas → Python, NaN/inf → None,
    dates → ISO strings, non-string dict keys → str."""
    if o is None or isinstance(o, (str, bool, int)):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {(k if isinstance(k, str) else str(_sanitize(k))): _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_sanitize(v) for v in o]
    if isinstance(o, np.ndarray):
        return _sanitize(o.tolist())
    if isinstance(o, np.generic):
        return _sanitize(o.item())
    if pd is not None:
        if isinstance(o, pd.Timestamp):
            return None if pd.isna(o) else o.isoformat()
        if isinstance(o, (pd.Series, pd.DataFrame)):
            return _sanitize(o.to_dict())
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    return str(o)


def to_json(payload):
    return json.dumps(_sanitize(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")


class PortalApp:
    """Request-independent state: settings + (optional) analysis services."""

    def __init__(self, settings=None, jobs=None, llm=None, scans=None, socials=None, stats=None):
        self.settings = settings or Settings()
        self.stats = stats  # ui.stats.StatsRecorder (--lan / --public) or None
        self.jobs = jobs  # ui.jobs.JobManager (step 3+)
        self.llm = llm    # ui.llm.LocalLLMClient (step 4+)
        if scans is None:
            from ui.scans import ScanArchive
            scans = ScanArchive(self.settings.scans_dir, self.settings.cache_dir, limit=self.settings.scans_limit)
        self.scans = scans  # ui.scans.ScanArchive: the "Latest scans" widget
        if socials is None and self.settings.socials:
            from ui.socials import SocialFeed
            socials = SocialFeed(self.settings.cache_dir, self.settings.social_env_file,
                                 read_at=self.settings.social_read_at, tz=self.settings.social_tz).start()
        self.socials = socials  # ui.socials.SocialFeed: latest X posts (None = profile links only)
        self.started = time.time()

    @property
    def public(self):
        return self.settings.public

    def health(self):
        out = {"ok": True, "uptime_s": round(time.time() - self.started, 1), "lan": self.settings.lan,
               "benchmark": self.settings.benchmark, "verbose": self.settings.verbose,
               "features": {"search": self.jobs is not None, "ai": self.llm is not None, "scans": True,
                            "refresh": self.jobs is not None and self.jobs.allow_refresh}}
        if self.llm is not None:
            out["ai"] = self.llm.status(block=False)  # never block the page on the LLM probe
        if self.jobs is not None:
            out["cache"] = self.jobs.cache_stats()
            if self.settings.enrich_enabled:
                out["enrich"] = self._fmp_budget().status()  # FMP requests in the last 24 h vs the cap
            if getattr(self, "precacher", None) is not None:
                out["precache"] = self.precacher.last  # the last night's run (None until one has run)
        return self._public_health(out) if self.public else out

    def _fmp_budget(self):
        if getattr(self, "_fmp", None) is None:
            from ui.fmp_budget import FmpBudget
            self._fmp = FmpBudget(self.settings.fmp_budget_path, self.settings.fmp_limit)
        return self._fmp

    @staticmethod
    def _public_health(out):
        """What the page needs, minus what visitors have no business seeing: other visitors' tickers
        (cache top / running), this machine's LAN setting and the local model's address."""
        out.pop("lan", None)
        out.pop("enrich", None)  # this machine's FMP quota
        out.pop("precache", None)
        cache = out.get("cache")
        if cache:
            for k in ("top", "running", "ai_running"):
                cache.pop(k, None)
        ai = out.get("ai")
        if ai:
            out["ai"] = {k: ai[k] for k in ("available", "checking", "model", "model_short", "ctx", "reasoning")
                         if k in ai}
            if not ai.get("available"):
                out["ai"]["reason"] = "local model unavailable"
        return out


def make_handler(app):
    settings = app.settings

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LynchPinPortal/1.0"
        sys_version = ""
        timeout = 30  # idle keep-alive sockets from phones are closed after this
        # TCP_NODELAY: headers and body go out as two writes; with Nagle's algorithm on, the body waits
        # for the client's delayed ACK (~40-50 ms per keep-alive response). See ui/tests/test_benchmark.py.
        disable_nagle_algorithm = True

        # ── plumbing ─────────────────────────────────────────────────────────────
        def log_message(self, fmt, *args):  # quieter, single-line access log
            line = (fmt % args).encode("ascii", "backslashreplace").decode("ascii")
            line = "".join(c if c.isprintable() else "\\x%02x" % ord(c) for c in line)
            sys.stderr.write(f"[{self.log_date_time_string()}] {self.client_ip()} {line}\n")

        def send_response(self, code, message=None):
            self._code = code  # for the stats page
            super().send_response(code, message)

        def _begin(self):
            self._code = self._reason = None  # one handler serves every request of a keep-alive connection
            self._action = False  # set by _get for what a visitor did (counted as a request on the stats page)
            self._t0, self._t0_mono = time.time(), time.monotonic()

        _marked = None  # (visitor, hour) this connection last marked for DAU / MAU

        def _record(self):
            """Count this request on the stats page (--lan / --public): its minute, if refused why, and if served
            its visitor (DAU / MAU)."""
            if app.stats is not None and self._code is not None:
                refused = self._reason is not None or self._code in DEFAULT_REASON
                if self._action or refused:  # every refused request counts, under its reason
                    app.stats.hit(self._code, self._reason, ts=self._t0)
                if self._code < 400:
                    mark = (self._visitor(), int(self._t0 // 3600))
                    if mark != self._marked:  # a keep-alive connection's visitor: once an hour is enough
                        self._marked = mark
                        app.stats.visit(mark[0], ts=self._t0)

        def _visitor(self):
            """The source IP for DAU / MAU: the peer, or behind a tunnel on this machine (cloudflared) the
            visitor's CF-Connecting-IP. The header is only trusted from loopback, so the LAN can't set it."""
            peer = self.client_address[0]
            if peer.startswith("127.") or peer == "::1":
                cf = self.headers.get("CF-Connecting-IP")
                ip = _ip(cf.strip()) if cf else None  # parsing "" raises inside ipaddress: µs per request
                if ip is not None:
                    return str(ip)
            return peer

        def client_ip(self):
            """The visitor's IP. With --public the peer is always the local tunnel (cloudflared), which
            passes the real address in CF-Connecting-IP; the header is only trusted from loopback."""
            peer = self.client_address[0]
            headers = getattr(self, "headers", None)  # unset when the request line itself was malformed
            if app.public and headers is not None and (_ip(peer) is not None and _ip(peer).is_loopback):
                ip = _ip((headers.get("CF-Connecting-IP") or "").strip())
                if ip is not None:
                    return str(ip)
            return peer

        def _send(self, status, body, ctype, cache="no-store", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, payload, status=HTTPStatus.OK):
            self._send(status, to_json(payload), "application/json; charset=utf-8")

        def send_error_json(self, status, message):
            self.send_json({"error": message, "status": int(status)}, status)

        def send_file(self, path, cache):
            """Serve a file with ETag / Last-Modified validators (304 on a match)."""
            try:
                st = os.stat(path)
            except OSError:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            validators = {"ETag": etag, "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True)}
            inm = self.headers.get("If-None-Match")
            ims = self.headers.get("If-Modified-Since")
            fresh = False
            if inm is not None:
                fresh = etag in [t.strip() for t in inm.split(",")] or inm.strip() == "*"
            elif ims:
                try:
                    fresh = int(st.st_mtime) <= email.utils.parsedate_to_datetime(ims).timestamp()
                except (TypeError, ValueError, IndexError, OverflowError):
                    fresh = False
            if fresh:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", cache)
                for k, v in validators.items():
                    self.send_header(k, v)
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
                ctype += "; charset=utf-8"
            self._send(HTTPStatus.OK, body, ctype, cache=cache, extra=validators)

        def _refuse(self, message, hint, reason=None):
            self._reason = reason
            key = (self.client_address[0], message)
            if key not in _REFUSED:  # explain once per client, not on every request
                _REFUSED.add(key)
                sys.stderr.write(f"⛔ {self.client_address[0]}: {message} — {hint}\n")
            self.send_error_json(HTTPStatus.FORBIDDEN, message)
            return False

        def _guard(self):
            ip = self.client_address[0]
            if app.public:  # only the tunnel on this machine; any public host name
                if _ip(ip) is None or not _ip(ip).is_loopback:
                    return self._refuse("served through the local tunnel only (--public)",
                                        "point cloudflared at http://localhost:<port>", "tunnel_only")
                return True
            if not is_local_client(ip):
                return self._refuse("local network only",
                                    f"to allow it restart with --allow-net {ip}/32 (or its CIDR range)", "outside_lan")
            host = self.headers.get("Host", "")
            if not is_allowed_host(host):
                name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
                return self._refuse("unexpected Host header",
                                    f"Host was {host!r}; to allow it restart with --allow-host {name}", "foreign_host")
            return True

        # ── verbs ────────────────────────────────────────────────────────────────
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._begin()
            try:
                self._get()
            finally:
                self._record()

        def _get(self):
            if not self._guard():
                return
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            # The stats page counts what visitors do: open the portal, open a scan thread, look up a ticker (↻ Refresh
            # included). What the page then fetches on its own is not counted: its files and images, the scan list
            # and X feed, the AI overview and its stream, the deep-dive prompt, health checks and polls (?poll=1).
            poll = query.get("poll", ["0"])[0] == "1"
            self._action = path in ("/", "/index.html") or (bool(USER_ACTION_RE.match(path)) and not poll)
            try:
                self.route(path, query)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True  # client went away (phone locked mid-request)
            except Exception as e:  # never leak a traceback to the client
                sys.stderr.write(f"❌ {path}: {type(e).__name__}: {e}\n")
                try:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")
                except OSError:
                    self.close_connection = True

        def do_POST(self):
            self._begin()
            try:
                if self._guard():
                    self.send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "read-only portal")
            finally:
                self._record()

        do_PUT = do_DELETE = do_PATCH = do_POST

        # ── routes ───────────────────────────────────────────────────────────────
        def route(self, path, query):
            if path in ("/", "/index.html"):
                return self.send_file(os.path.join(settings.static_dir, "index.html"), "no-cache")
            if path == "/api/health":
                return self.send_json(app.health())
            if path.startswith("/api/ticker/"):
                return self.route_ticker(path[len("/api/ticker/"):], query)
            if path.startswith("/plots/"):
                return self.route_plot(path[len("/plots/"):])
            if path == "/api/scans":
                return self.send_json(app.scans.summaries())
            if path.startswith("/api/scans/"):
                scan = app.scans.get(path[len("/api/scans/"):])
                if scan is None:
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "no such scan")
                return self.send_json(scan)
            if path.startswith("/scans/"):
                return self.route_scan_image(path[len("/scans/"):])
            if path == "/api/socials":
                if app.socials is None:
                    from ui.socials import HANDLE
                    return self.send_json({"handle": HANDLE, "x": [], "refreshing": False})
                return self.send_json(app.socials.snapshot())
            if path.startswith("/social/"):
                img = app.socials.image_path(path[len("/social/"):]) if app.socials is not None else None
                if not img:
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return self.send_file(img, "public, max-age=86400")
            if path == "/api/cache":
                if app.jobs is None or app.public:
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "search disabled")
                return self.send_json(app.jobs.cache_stats())
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                if not STATIC_FILE_RE.match(rel):
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                # Code/styles change between releases → always revalidate (cheap 304 via ETag);
                # images are stable and may be cached for a day.
                cache = "no-cache" if rel.endswith((".js", ".css", ".html", ".webmanifest")) \
                    else "public, max-age=86400"
                return self.send_file(os.path.join(settings.static_dir, rel), cache)
            if path in ("/manifest.webmanifest", "/favicon.ico"):
                rel = "manifest.webmanifest" if path.endswith("webmanifest") else "img/favicon.png"
                return self.send_file(os.path.join(settings.static_dir, rel), "no-cache")
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

        def route_ticker(self, rest, query):
            if app.jobs is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "search disabled")
            parts = rest.split("/")
            sym = parts[0].strip().upper()
            if not TICKER_RE.match(sym) or parts[1:] not in ([], ["ai"], ["ai", "stream"], ["deepdive"]):
                return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid ticker symbol")
            if parts[1:] == ["deepdive"]:
                dd = app.jobs.deep_dive(sym)
                if dd is None:
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "analyse this ticker first")
                return self.send_json(dd)
            if len(parts) >= 2 and app.llm is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "AI disabled")
            if len(parts) == 3:
                return self.route_ai_stream(sym)
            if len(parts) == 2:
                snap = app.jobs.request_ai(sym, refresh=query.get("refresh", ["0"])[0] == "1")
                if snap.get("status") == "busy":
                    self._reason = snap.get("reason")
                    return self._send(HTTPStatus.TOO_MANY_REQUESTS, to_json(snap),
                                      "application/json; charset=utf-8",
                                      extra={"Retry-After": str(snap.get("retry_after", 15))})
                return self.send_json(snap)
            refresh, poll = query.get("refresh", ["0"])[0] == "1", query.get("poll", ["0"])[0] == "1"
            snap = app.jobs.request(sym, refresh=refresh, poll=poll, client=self.client_ip() if app.public else None)
            if snap.get("status") == "busy":
                self._reason = snap.get("reason")
                return self._send(HTTPStatus.TOO_MANY_REQUESTS, to_json(snap), "application/json; charset=utf-8",
                                  extra={"Retry-After": str(snap.get("retry_after", 10))})
            self.send_json(snap)
            if app.stats is not None and not poll and snap.get("status") in ("done", "nodata", "error"):
                # answered at once (cache, or a just-finished job); queued lookups are timed by the job
                app.stats.query(sym, "cache" if snap.get("cached") else "recent", snap["status"],
                                time.monotonic() - self._t0_mono, ts=self._t0)

        def route_ai_stream(self, sym):
            """Server-Sent Events: the AI overview token by token, with TTFT / tok/s metrics."""
            events = app.jobs.ai_events(sym)
            if events is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "no AI overview in progress")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")  # no Content-Length: the body ends when we close
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            self.wfile.write(b"retry: 2000\n\n")
            for event, data in events:
                if event == "ping":
                    chunk = b": ping\n\n"
                else:
                    chunk = b"event: " + event.encode("ascii") + b"\ndata: " + to_json(data) + b"\n\n"
                self.wfile.write(chunk)
                self.wfile.flush()

        def route_plot(self, name):
            """/plots/SYM.png = full 300-dpi chart (lightbox), /plots/SYM.jpg = inline preview."""
            if app.jobs is None or not name.endswith((".png", ".jpg")):
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            sym = name[:-4].upper()
            if not TICKER_RE.match(sym):
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            path = app.jobs.plot_path(sym, preview=name.endswith(".jpg"))
            if not path:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "no plot yet")
            return self.send_file(path, "private, max-age=86400")

        def route_scan_image(self, rest):
            """/scans/KIND/NAME.png = archived chart (lightbox), /scans/KIND/NAME.jpg = smaller preview.
            URLs carry ?v=<scan version>, so a browser may keep them; next week's scan gets a new URL."""
            kind, _, name = rest.partition("/")
            path = app.scans.image_path(kind, name)
            if not path:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return self.send_file(path, "public, max-age=604800")

    return Handler


_REFUSED = set()


class StatsApp:
    """The stats server's state: the portal's recorder. The stats page has the portal's LAN guard (never
    the --public one), and its own requests are not recorded."""
    public = False
    stats = None

    def __init__(self, settings, recorder):
        self.settings, self.recorder = settings, recorder


def make_stats_handler(app):
    """The stats page: the portal's handler (guard, static files, headers) with its own routes."""
    settings = app.settings

    class StatsHandler(make_handler(app)):
        server_version = "LynchPinStats/1.0"

        def _guard(self):
            """LAN / Tailscale clients only. With --public, cloudflared runs on this machine and connects from
            loopback, so loopback is refused; anything carrying Cloudflare's headers is refused in any mode.
            The stats page stays off the internet even if a tunnel is pointed at it."""
            ip = _ip(self.client_address[0])
            if settings.public and (ip is None or ip.is_loopback):
                return self._refuse("LAN / Tailscale only",
                                    f"with --public, loopback is the tunnel's: open http://<lan-ip>:{settings.stats_port}")
            if self.headers.get("CF-Connecting-IP") or self.headers.get("CF-Ray"):
                return self._refuse("not through a tunnel", "the stats page is for the LAN / Tailscale only")
            return super()._guard()

        def route(self, path, query):
            if path in ("/", "/index.html"):
                return self.send_file(os.path.join(settings.static_dir, "stats.html"), "no-cache")
            if path == "/api/stats":
                try:
                    days = int(query.get("days", [app.recorder.retention_days])[0])
                except ValueError:
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "days must be a number")
                return self.send_json(dict(app.recorder.summary(days), mode="public" if settings.public else "lan",
                                           portal_port=None if settings.public else settings.port))
            if path.startswith("/static/") or path == "/favicon.ico":
                return super().route(path, query)
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    return StatsHandler


def start_stats_server(settings, recorder):
    """The stats page on all interfaces, port ``settings.stats_port`` (LAN / Tailscale clients only), in a
    thread. Returns the server, or None when the port can't be bound (the portal runs on without it)."""
    try:
        httpd = PortalServer((settings.stats_bind_host, settings.stats_port),
                             make_stats_handler(StatsApp(settings, recorder)))
    except OSError as e:
        hint = " (ports below 1024 need root on Linux: try --stats-port 8190)" if isinstance(e, PermissionError) else ""
        print(f"⚠️  stats page not started on port {settings.stats_port}: {e.strerror or e}{hint}", flush=True)
        return None
    threading.Thread(target=httpd.serve_forever, name="lynch-stats-http", daemon=True).start()
    return httpd


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Listen backlog. socketserver's default of 5 overflows when a page load or a few devices open many
    # connections at once; dropped SYNs are retried after 1-3 s. (The OS may cap it, e.g. somaxconn.)
    request_queue_size = 128

    def handle_error(self, request, client_address):
        """A phone locking or a tab closing mid-request is not an error worth a traceback."""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError,
                                          ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def build_app(settings, with_search=True, with_ai=True):
    jobs = llm = stats = None
    if settings.stats_enabled:
        from ui.stats import StatsRecorder
        stats = StatsRecorder(settings.stats_path, retention_days=settings.stats_days,
                              visitor_days=settings.stats_visitor_days)
    if with_ai:
        try:
            from ui.llm import LocalLLMClient
            llm = LocalLLMClient(settings)
        except ImportError:
            llm = None
    if with_search:
        try:
            from ui.jobs import JobManager
            workers = settings.analysis_workers(with_ai=llm is not None)
            jobs = JobManager(settings, llm=llm, workers=workers, processes=workers > 1,
                              allow_refresh=llm is not None,  # --no-ai: cached tickers stay cached
                              max_per_client=1 if settings.public else None,  # --public: one per visitor
                              stats=stats)
        except ImportError:
            jobs = None
    if jobs is None:
        llm = None  # AI overview hangs off a ticker job
    return PortalApp(settings, jobs=jobs, llm=llm, stats=stats)


def parse_args(argv=None):
    s = Settings()
    p = argparse.ArgumentParser(description="Lynch Pin Quant Portal (local web UI)")
    p.add_argument("--host", default=s.host, help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=s.port)
    p.add_argument("--allow-net", action="append", default=[], metavar="CIDR",
                   help="extra client network(s) beyond private LAN + Tailscale (repeatable, comma-separated)")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="extra Host name(s) to accept, e.g. a reverse-proxy name (repeatable)")
    p.add_argument("--lan", action="store_true", default=s.lan,
                   help="listen on all interfaces so phones on the same private network can connect")
    p.add_argument("--llm-url", default=s.llm_base_url, help="LM Studio base URL (default http://127.0.0.1:1234)")
    p.add_argument("--llm-model", default=s.llm_model, help="model id (default: first model the server lists)")
    p.add_argument("--llm-ctx", type=int, default=s.llm_ctx, help="context window in tokens (default 65536)")
    p.add_argument("--llm-max-tokens", type=int, default=s.llm_max_tokens)
    p.add_argument("--llm-reasoning", choices=("off", "on"), default=s.llm_reasoning if s.llm_reasoning in ("off", "on")
                   else "off", help="thinking for reasoning models: off sends reasoning_effort=none (default off)")
    p.add_argument("--llm-parallel", type=int, default=s.llm_parallel,
                   help="AI overviews generated at once (default 1; see ui/README.md, Local AI tuning)")
    p.add_argument("--llm-autoload", action="store_true", default=s.llm_autoload,
                   help="ask LM Studio to load the model with --llm-ctx before the first request")
    p.add_argument("--cache-size", type=int, default=s.cache_capacity, help="tickers cached per day (default 500)")
    p.add_argument("--enrich", choices=("auto", "on", "off"), default=s.enrich,
                   help="FMP growth enrichment: auto = on when FMP_API_KEY is set (default auto)")
    p.add_argument("--fmp-limit", type=int, default=s.fmp_limit, metavar="N",
                   help="FMP requests the portal may make in any rolling 24 h (default 225: the free plan's 250 a "
                        "day minus 25 for the daily scans); past it lookups are not enriched")
    p.add_argument("--precache", type=int, default=s.precache, metavar="N",
                   help="right after the cache resets at midnight, analyse the N most looked-up tickers of the "
                        "last 30 days, one at a time with their AI overviews (default 100; 0 = off; needs the "
                        "stats page, i.e. --lan or --public)")
    p.add_argument("--benchmark", default=s.benchmark, help="index for the 6M edge backtest (default SPY)")
    p.add_argument("--no-ai", action="store_true", help="disable the AI overview")
    p.add_argument("-v", "--verbose", action="store_true", default=s.verbose,
                   help="show the model name, token counts, thinking setting and cache chip on the page "
                        "(default: only an AI on/off dot)")
    p.add_argument("--public", action="store_true", default=s.public,
                   help="serve the internet through a tunnel on this machine (e.g. cloudflared → localhost): any "
                        "Host name, visitor IPs from CF-Connecting-IP, one analysis at a time per visitor, "
                        "no /api/cache. The portal listens on loopback only (not with --lan); the stats page "
                        "(--stats-port) answers LAN / Tailscale clients only")
    p.add_argument("--social-read-at", default=s.social_read_at, metavar="HH:MM",
                   help="time of the daily Latest on X read, Pacific time (LYNCH_UI_SOCIAL_TZ; default 09:00, "
                        "before the 1 PM scan posts)")
    p.add_argument("--stats-port", type=int, default=s.stats_port,
                   help="port of the stats page, with --lan or --public, for LAN / Tailscale clients only "
                        "(default 190)")
    p.add_argument("--no-stats", action="store_true", help="no stats page and nothing recorded for it")
    p.add_argument("--workers", type=int, default=s.workers,
                   help="tickers analysed at the same time, one process each (default 0 = auto: "
                        f"1 with the AI overview, {CPU_WORKERS} without, fewer if free RAM is short)")
    a = p.parse_args(argv)
    s.host, s.port, s.lan = a.host, a.port, a.lan
    s.llm_base_url, s.llm_model, s.llm_ctx = a.llm_url.rstrip("/"), a.llm_model, a.llm_ctx
    s.llm_max_tokens, s.llm_autoload, s.llm_reasoning = a.llm_max_tokens, a.llm_autoload, a.llm_reasoning
    s.llm_parallel = max(1, a.llm_parallel)
    s.cache_capacity, s.benchmark = max(1, a.cache_size), a.benchmark.upper()
    s.enrich = a.enrich
    s.fmp_limit = max(0, a.fmp_limit)
    s.precache = max(0, a.precache)
    s.workers = max(0, a.workers)
    s.public = a.public
    s.verbose = a.verbose
    s.stats, s.stats_port = s.stats and not a.no_stats, a.stats_port
    if s.stats_enabled and s.stats_port == s.port:
        p.error("--stats-port must differ from --port")
    try:
        from zoneinfo import ZoneInfo
        from ui.socials import parse_hhmm
        parse_hhmm(a.social_read_at)
        ZoneInfo(s.social_tz)
    except (ValueError, KeyError) as e:
        p.error(f"--social-read-at: {e}")
    s.social_read_at = a.social_read_at
    if s.public and (s.lan or s.host not in LOOPBACK_HOSTS):
        p.error("--public listens on 127.0.0.1 only (the tunnel runs on this machine): drop --lan / --host")
    return s, a


def _lan_addresses():
    """This machine's LAN and Tailscale addresses (UDP connect sends no packet; it only picks the
    interface that would route to the target: a private LAN address, and Tailscale's MagicDNS IP)."""
    import socket
    addrs = set()
    for target in ("10.255.255.255", "100.100.100.100"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 1))
                addrs.add(s.getsockname()[0])
        except OSError:
            pass
    return sorted(a for a in addrs if is_local_client(a) and not a.startswith("127."))


def _interrupt(signum, frame):
    raise KeyboardInterrupt  # SIGTERM (kill, launchd) stops like Ctrl-C: the finally block flushes the stats


def main(argv=None):
    settings, args = parse_args(argv)
    signal.signal(signal.SIGTERM, _interrupt)
    bad = netguard.allow(args.allow_net, args.allow_host)
    bad += netguard.parse_nets([os.environ.get("LYNCH_UI_ALLOWED_NETS", "")])[1]
    if bad:
        print(f"⚠️  ignoring invalid network(s): {', '.join(bad)} (expected CIDR like 100.0.0.0/8)", flush=True)
    app = build_app(settings, with_ai=not args.no_ai)
    httpd = PortalServer((settings.bind_host, settings.port), make_handler(app))
    stats_httpd = start_stats_server(settings, app.stats) if app.stats is not None else None
    print(f"📈 Lynch Pin Quant Portal on http://{settings.bind_host}:{settings.port}", flush=True)
    lan_addrs = _lan_addresses() if settings.lan or stats_httpd is not None else []
    if stats_httpd is not None:
        where = os.path.relpath(settings.stats_path, REPO_ROOT)
        print(f"📊 Stats page on http://{settings.stats_bind_host}:{settings.stats_port} (LAN / Tailscale only"
              f"{', never through the tunnel' if settings.public else ''}; rolling {settings.stats_days}-day window "
              f"in {settings.stats_path if where.startswith('..') else where})", flush=True)
        for a in lan_addrs:
            print(f"   📊 stats: http://{a}:{settings.stats_port} {'over Tailscale' if a.startswith('100.') else 'on your Wi-Fi'}",
                  flush=True)
    if settings.lan:
        print("⚠️  --lan: listening on ALL interfaces with NO authentication. Any device on your private "
              "network or tailnet can use the portal (public IPs and foreign Host headers are refused).", flush=True)
        for a in lan_addrs:
            where = "over Tailscale" if a.startswith("100.") else "on your Wi-Fi"
            print(f"   📱 open http://{a}:{settings.port} {where}", flush=True)
    if settings.public:
        print("🌐 --public: only the local tunnel can connect; any Host name; visitor IPs from CF-Connecting-IP; "
              "one analysis at a time per visitor; /api/cache off. No login: put Cloudflare Access in front "
              "to restrict who can use it.", flush=True)
    nets, hosts = netguard.extra_allowed()
    if nets or hosts:
        print(f"🔓 Also allowing: {', '.join(nets + hosts)}", flush=True)
    if app.jobs is not None:
        print(f"📈 Growth enrichment: {'on, up to ' + str(settings.fmp_limit) + ' FMP requests per 24 h' if settings.enrich_enabled else 'off'}", flush=True)
        print(f"📈 Analysis workers: {app.jobs.workers}"
              f"{' (one process each)' if app.jobs.processes else ''}", flush=True)
        free = available_mb()
        if settings.workers == 0 and app.llm is None and free is not None and app.jobs.workers < CPU_WORKERS:
            print(f"⚠️  only {free} MB of RAM free: {app.jobs.workers} workers instead of {CPU_WORKERS} "
                  f"(~{WORKER_MB} MB each). Free memory (e.g. unload the LM Studio model) or set --workers.",
                  flush=True)
    precacher = None
    if app.jobs is not None and app.stats is not None and settings.precache:
        from ui.precache import Precacher
        precacher = app.precacher = Precacher(app.jobs, app.stats, settings.precache).start()
        print(f"🌙 Pre-cache: each night after the cache resets, the {settings.precache} most looked-up tickers "
              f"of the last 30 days{' with their AI overviews' if app.llm is not None else ''}, one at a time",
              flush=True)
    if app.llm is not None:
        print(f"🧠 AI: {settings.llm_base_url} model={settings.llm_model or '(auto)'} ctx={settings.llm_ctx} "
              f"reasoning={settings.llm_reasoning} parallel={settings.llm_parallel}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if stats_httpd is not None:
            stats_httpd.shutdown()
            stats_httpd.server_close()
        if precacher is not None:
            precacher.stop()
        if app.jobs is not None:
            app.jobs.shutdown()
        if app.stats is not None:
            app.stats.close()  # writes the last minutes


if __name__ == "__main__":
    main()
