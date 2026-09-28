"""Lynch Pin Quant Portal — stdlib HTTP server.

    python -m ui.server                 # http://127.0.0.1:8765 (this machine only)
    python -m ui.server --lan           # reachable from phones/PCs on the same private network

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

from ui.config import REPO_ROOT, Settings  # noqa: E402
from ui.netguard import is_allowed_host, is_local_client  # noqa: E402

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
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

    def __init__(self, settings=None, jobs=None, llm=None):
        self.settings = settings or Settings()
        self.jobs = jobs  # ui.jobs.JobManager (step 3+)
        self.llm = llm    # ui.llm.LocalLLMClient (step 4+)
        self.started = time.time()

    def health(self):
        out = {"ok": True, "uptime_s": round(time.time() - self.started, 1), "lan": self.settings.lan,
               "benchmark": self.settings.benchmark,
               "features": {"search": self.jobs is not None, "ai": self.llm is not None}}
        if self.llm is not None:
            out["ai"] = self.llm.status(block=False)  # never block the page on the LLM probe
        if self.jobs is not None:
            out["cache"] = self.jobs.cache_stats()
        return out


def make_handler(app):
    settings = app.settings

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LynchPinPortal/1.0"
        sys_version = ""
        timeout = 30  # idle keep-alive sockets from phones are closed after this

        # ── plumbing ─────────────────────────────────────────────────────────────
        def log_message(self, fmt, *args):  # quieter, single-line access log
            line = (fmt % args).encode("ascii", "backslashreplace").decode("ascii")
            line = "".join(c if c.isprintable() else "\\x%02x" % ord(c) for c in line)
            sys.stderr.write(f"[{self.log_date_time_string()}] {self.client_address[0]} {line}\n")

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

        def _guard(self):
            if not is_local_client(self.client_address[0]):
                self.send_error_json(HTTPStatus.FORBIDDEN, "local network only")
                return False
            if not is_allowed_host(self.headers.get("Host", "")):
                self.send_error_json(HTTPStatus.FORBIDDEN, "unexpected Host header")
                return False
            return True

        # ── verbs ────────────────────────────────────────────────────────────────
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._guard():
                return
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
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
            if not self._guard():
                return
            self.send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "read-only portal")

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
            if path == "/api/cache":
                if app.jobs is None:
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
            if not TICKER_RE.match(sym) or parts[1:] not in ([], ["ai"], ["ai", "stream"]):
                return self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid ticker symbol")
            if len(parts) >= 2 and app.llm is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "AI disabled")
            if len(parts) == 3:
                return self.route_ai_stream(sym)
            if len(parts) == 2:
                snap = app.jobs.request_ai(sym, refresh=query.get("refresh", ["0"])[0] == "1")
                if snap.get("status") == "busy":
                    return self._send(HTTPStatus.TOO_MANY_REQUESTS, to_json(snap),
                                      "application/json; charset=utf-8",
                                      extra={"Retry-After": str(snap.get("retry_after", 15))})
                return self.send_json(snap)
            refresh = query.get("refresh", ["0"])[0] == "1"
            snap = app.jobs.request(sym, refresh=refresh, poll=query.get("poll", ["0"])[0] == "1")
            if snap.get("status") == "busy":
                return self._send(HTTPStatus.TOO_MANY_REQUESTS, to_json(snap), "application/json; charset=utf-8",
                                  extra={"Retry-After": str(snap.get("retry_after", 10))})
            return self.send_json(snap)

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

    return Handler


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_app(settings, with_search=True, with_ai=True):
    jobs = llm = None
    if with_ai:
        try:
            from ui.llm import LocalLLMClient
            llm = LocalLLMClient(settings)
        except ImportError:
            llm = None
    if with_search:
        try:
            from ui.jobs import JobManager
            jobs = JobManager(settings, llm=llm)
        except ImportError:
            jobs = None
    if jobs is None:
        llm = None  # AI overview hangs off a ticker job
    return PortalApp(settings, jobs=jobs, llm=llm)


def parse_args(argv=None):
    s = Settings()
    p = argparse.ArgumentParser(description="Lynch Pin Quant Portal (local web UI)")
    p.add_argument("--host", default=s.host, help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=s.port)
    p.add_argument("--lan", action="store_true", default=s.lan,
                   help="listen on all interfaces so phones on the same private network can connect")
    p.add_argument("--llm-url", default=s.llm_base_url, help="LM Studio base URL (default http://127.0.0.1:1234)")
    p.add_argument("--llm-model", default=s.llm_model, help="model id (default: first model the server lists)")
    p.add_argument("--llm-ctx", type=int, default=s.llm_ctx, help="context window in tokens (default 65536)")
    p.add_argument("--llm-max-tokens", type=int, default=s.llm_max_tokens)
    p.add_argument("--llm-reasoning", choices=("off", "on"), default=s.llm_reasoning if s.llm_reasoning in ("off", "on")
                   else "off", help="thinking for reasoning models: off sends reasoning_effort=none (default off)")
    p.add_argument("--llm-autoload", action="store_true", default=s.llm_autoload,
                   help="ask LM Studio to load the model with --llm-ctx before the first request")
    p.add_argument("--cache-size", type=int, default=s.cache_capacity, help="tickers cached per day (default 250)")
    p.add_argument("--enrich", choices=("auto", "on", "off"), default=s.enrich,
                   help="FMP growth enrichment: auto = on when FMP_API_KEY is set (default auto)")
    p.add_argument("--benchmark", default=s.benchmark, help="index for the 6M edge backtest (default SPY)")
    p.add_argument("--no-ai", action="store_true", help="disable the AI overview")
    a = p.parse_args(argv)
    s.host, s.port, s.lan = a.host, a.port, a.lan
    s.llm_base_url, s.llm_model, s.llm_ctx = a.llm_url.rstrip("/"), a.llm_model, a.llm_ctx
    s.llm_max_tokens, s.llm_autoload, s.llm_reasoning = a.llm_max_tokens, a.llm_autoload, a.llm_reasoning
    s.cache_capacity, s.benchmark = max(1, a.cache_size), a.benchmark.upper()
    s.enrich = a.enrich
    return s, a


def _lan_addresses():
    import socket
    addrs = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outbound interface
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in addrs if is_local_client(a))


def main(argv=None):
    settings, args = parse_args(argv)
    app = build_app(settings, with_ai=not args.no_ai)
    httpd = PortalServer((settings.bind_host, settings.port), make_handler(app))
    print(f"📈 Lynch Pin Quant Portal on http://{settings.bind_host}:{settings.port}", flush=True)
    if settings.lan:
        print("⚠️  --lan: listening on ALL interfaces with NO authentication. Any device on your private "
              "network can use the portal (public IPs and foreign Host headers are refused).", flush=True)
        for a in _lan_addresses():
            print(f"   📱 open http://{a}:{settings.port} on your phone", flush=True)
    if app.jobs is not None:
        print(f"📈 Growth enrichment: {'on' if settings.enrich_enabled else 'off'}", flush=True)
    if app.llm is not None:
        print(f"🧠 AI: {settings.llm_base_url} model={settings.llm_model or '(auto)'} ctx={settings.llm_ctx} "
              f"reasoning={settings.llm_reasoning}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if app.jobs is not None:
            app.jobs.shutdown()


if __name__ == "__main__":
    main()
