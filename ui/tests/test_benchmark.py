"""Throughput benchmark: how many requests per second the portal serves with the AI overview off.

    python -m pytest ui/tests/test_benchmark.py -q                        # quick run, prints the table
    python ui/tests/test_benchmark.py --seconds 5 --concurrency 1,8,32,128  # longer standalone run
    python ui/tests/test_benchmark.py --scenarios ticker,page --concurrency 64

How it works
------------
* The portal runs in its **own process** (``--serve`` mode of this file), exactly as ``python -m ui.server``
  builds it — same handler, network guard, security headers, ETags, access log (written to a file) and
  daily LFU cache — but with the AI overview off (``llm=None``) and the offline fake engine from
  ``ui/tests/fakes.py``, so no Yahoo / SEC / LM Studio traffic is involved. 20 tickers are analysed
  before the clock starts, so ticker lookups are cache hits: the "typed the same ticker again" path.
* Load comes from separate client processes (``--load`` mode), each running keep-alive HTTP/1.1
  connections in threads, so the Python client and the server don't share a GIL. ``conc`` = total
  concurrent connections, spread across up to ``--procs`` processes.
* Keep-alive connections are opened before the clock starts (a READY / GO barrier across the client
  processes, like wrk), and the time each TCP connect took is reported as "conn max".
* Every cell reports requests/s, latency percentiles, response size, errors (anything but the expected
  status), the server process's CPU use and the slowest connect. A server at ~100 % CPU is saturated: the portal is one
  Python process, so its request handling is bound to one core by the GIL no matter how many clients.

Scenarios
---------
health      GET /api/health (small JSON: feature flags + cache stats)
ticker      GET /api/ticker/SYM, 20 cached tickers in rotation (the full analysis JSON)
deepdive    GET /api/ticker/SYM/deepdive (builds the ~1,400-word prompt on every request)
static      GET /static/search.js (37 KB, 200 with ETag)
revalidate  GET /static/search.js with If-None-Match (304: what a reload costs)
image       GET /static/img/hero_wide.webp (116 KB)
plot        GET /plots/SYM.jpg (chart preview; the fake charts are simpler, so smaller than real ones)
page        a first page view: index, CSS, JS, logo, hero, health, ticker, plot preview, deep dive
            (10 requests each; the table also shows page views/s)
connect     GET /api/health with a new TCP connection per request (Connection: close)

The pytest run is a smoke benchmark: short cells, every request must succeed and every cell must clear
a low floor (``LYNCH_BENCH_MIN_RPS``, default 50) so it doesn't flake on a slow machine. Use the
standalone mode for real numbers. Knobs: ``LYNCH_BENCH_SECONDS`` (default 0.75),
``LYNCH_BENCH_CONCURRENCY`` (default "1,16"), ``LYNCH_BENCH_SCENARIOS`` (default all).

It also guards two server settings the first run of this benchmark led to (both in ui/server.py):
* ``disable_nagle_algorithm = True`` on the handler. Responses are written as two sends (headers, then
  body); with Nagle's algorithm on, the body waited for the client's delayed ACK, ~50 ms per keep-alive
  response (20 req/s per connection with the server idle). Guard: p50 < 20 ms at one connection.
* ``request_queue_size = 128`` on PortalServer (socketserver's default is 5). A burst of new connections
  overflowed the listen queue and waited 1-7 s for SYN retries. Guard: every TCP connect < 900 ms.
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SYMS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "AMD", "NFLX",
        "CRM", "ORCL", "ADBE", "INTC", "QCOM", "CSCO", "TXN", "MU", "PLTR", "UBER"]
PAGE = ["/?t={s}", "/static/app.css", "/static/app.js", "/static/search.js", "/static/img/logo.png",
        "/static/img/hero_wide.webp", "/api/health", "/api/ticker/{s}", "/plots/{s}.jpg",
        "/api/ticker/{s}/deepdive"]


# ── scenarios ───────────────────────────────────────────────────────────────
def scenarios(etag):
    """name → dict(paths, headers, close, expect, stride). ``stride`` spreads threads over the path list."""
    per_sym = lambda tpl: [tpl.format(s=s) for s in SYMS]  # noqa: E731
    return {
        "health": dict(paths=["/api/health"]),
        "ticker": dict(paths=per_sym("/api/ticker/{s}")),
        "deepdive": dict(paths=per_sym("/api/ticker/{s}/deepdive")),
        "static": dict(paths=["/static/search.js"]),
        "revalidate": dict(paths=["/static/search.js"], headers={"If-None-Match": etag}, expect=[304]),
        "image": dict(paths=["/static/img/hero_wide.webp"]),
        "plot": dict(paths=per_sym("/plots/{s}.jpg")),
        "page": dict(paths=[p.format(s=s) for s in SYMS for p in PAGE], stride=len(PAGE)),
        "connect": dict(paths=["/api/health"], close=True),
    }


# ── load generator (child process; stdlib only so it starts fast) ───────────
def run_load(cfg, go):
    """Opens the connections, then waits for ``go()`` → start time (a barrier across processes)."""
    port, paths, seconds = cfg["port"], cfg["paths"], cfg["seconds"]
    headers = dict(cfg.get("headers") or {})
    close = bool(cfg.get("close"))
    if close:
        headers["Connection"] = "close"
    expect = set(cfg.get("expect") or [200])
    stride = int(cfg.get("stride") or 1)
    connected = threading.Barrier(cfg["threads"] + 1)
    started = threading.Event()
    clock = {}
    results = []
    lock = threading.Lock()

    def connect(conns):
        t0 = time.perf_counter()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.connect()
        conns.append(round((time.perf_counter() - t0) * 1000, 3))
        return conn

    def worker(k):
        lat, conns, n, err, nbytes, statuses = [], [], 0, 0, 0, {}
        i = ((cfg["offset"] + k) * stride) % len(paths)
        conn = None
        if not close:  # keep-alive: connect before the clock starts, like wrk
            try:
                conn = connect(conns)
            except OSError:
                conn = None
        connected.wait()
        started.wait()
        end = clock["start"] + seconds
        while time.time() < end:
            path = paths[i % len(paths)]
            i += 1
            t0 = time.perf_counter()
            try:
                if conn is None:
                    conn = connect(conns)
                conn.request("GET", path, headers=headers)
                r = conn.getresponse()
                body = r.read()
                dt = time.perf_counter() - t0
                if close:
                    conn.close()
                    conn = None
            except (OSError, http.client.HTTPException) as e:
                if time.time() < end:
                    err += 1
                    statuses[type(e).__name__] = statuses.get(type(e).__name__, 0) + 1
                if conn is not None:
                    conn.close()
                conn = None
                time.sleep(0.001)
                continue
            if time.time() > end:  # finished after the window: not counted
                break
            n += 1
            nbytes += len(body)
            lat.append(round(dt * 1000, 3))
            statuses[str(r.status)] = statuses.get(str(r.status), 0) + 1
            if r.status not in expect:
                err += 1
        if conn is not None:
            conn.close()
        with lock:
            results.append((n, err, nbytes, statuses, lat, conns))

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(cfg["threads"])]
    for t in threads:
        t.start()
    connected.wait()
    clock["start"] = go()
    started.set()
    for t in threads:
        t.join()
    out = {"n": 0, "errors": 0, "bytes": 0, "status": {}, "lat": [], "connect": []}
    for n, err, nbytes, statuses, lat, conns in results:
        out["n"] += n
        out["errors"] += err
        out["bytes"] += nbytes
        out["lat"].extend(lat)
        out["connect"].extend(conns)
        for k, v in statuses.items():
            out["status"][k] = out["status"].get(k, 0) + v
    return out


def load_main(cfg):
    """``--load`` mode: READY on stdout once connected, then ``GO <start>`` on stdin, then JSON results."""
    def go():
        print("READY", flush=True)
        line = sys.stdin.readline().split()
        start = float(line[1]) if len(line) == 2 and line[0] == "GO" else time.time()
        time.sleep(max(0.0, start - time.time()))
        return start
    print(json.dumps(run_load(cfg, go)), flush=True)


# ── benchmark server (child process) ────────────────────────────────────────
def serve(cache_dir):
    sys.path.insert(0, REPO_ROOT)
    import matplotlib
    matplotlib.use("Agg")
    from ui.analysis import TickerAnalyzer
    from ui.config import Settings
    from ui.jobs import JobManager
    from ui.server import PortalApp, PortalServer, make_handler
    from ui.tests import fakes

    for s in SYMS:
        fakes.FakeEngine.infos[s] = dict(fakes.MSFT_INFO, longName=f"{s} Inc.")
        fakes.FakeEngine.rows[s] = dict(fakes.MSFT_ROW, Ticker=s)
    settings = Settings()
    settings.cache_dir = cache_dir
    settings.host, settings.port, settings.lan = "127.0.0.1", 0, False
    jobs = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), llm=None,
                      allow_refresh=False)  # as build_app() does with --no-ai
    app = PortalApp(settings, jobs=jobs, llm=None)  # AI overview off
    httpd = PortalServer(("127.0.0.1", 0), make_handler(app))

    for s in SYMS:  # warm the daily cache and the chart previews before the clock starts
        jobs.request(s)
    t0 = time.time()
    while time.time() - t0 < 60:
        if all(jobs.request(s, poll=True)["status"] == "done" for s in SYMS):
            break
        time.sleep(0.02)
    for s in SYMS:
        jobs.plot_path(s, preview=True)

    def control():  # "cpu" → process CPU seconds; EOF → exit
        for line in sys.stdin:
            if line.strip() == "cpu":
                print(f"CPU {time.process_time():.6f}", flush=True)
        os._exit(0)

    threading.Thread(target=control, daemon=True).start()
    print(f"READY {httpd.server_address[1]}", flush=True)
    httpd.serve_forever()


class BenchServer:
    """Starts ``serve()`` in a subprocess; the access log goes to a file so the pipe never fills."""

    def __init__(self, workdir):
        self.log_path = os.path.join(workdir, "access.log")
        self._log = open(self.log_path, "w")
        self.proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve", workdir],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                                     text=True, bufsize=1, cwd=REPO_ROOT)
        line = self.proc.stdout.readline()
        if not line.startswith("READY"):
            self.close()
            raise RuntimeError(f"benchmark server failed to start: {line!r} (see {self.log_path})")
        self.port = int(line.split()[1])

    def cpu(self):
        self.proc.stdin.write("cpu\n")
        self.proc.stdin.flush()
        return float(self.proc.stdout.readline().split()[1])

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(5)
        except Exception:
            self.proc.kill()
        self._log.close()


# ── orchestration ───────────────────────────────────────────────────────────
def default_procs():
    return max(1, min((os.cpu_count() or 2) - 2, 8))  # leave cores for the server


def run_cell(server, name, spec, conc, seconds, procs):
    nproc = max(1, min(conc, procs))
    base, extra = divmod(conc, nproc)
    children, offset = [], 0
    for p in range(nproc):
        threads = base + (1 if p < extra else 0)
        cfg = dict(spec, port=server.port, seconds=seconds, threads=threads, offset=offset)
        offset += threads
        children.append(subprocess.Popen([sys.executable, os.path.abspath(__file__), "--load", json.dumps(cfg)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1))
    for c in children:  # barrier: every connection is open before the clock starts
        line = c.stdout.readline()
        if line.strip() != "READY":
            for k in children:
                k.kill()
            raise RuntimeError(f"load generator failed to start: {line!r}")
    start = time.time() + 0.05
    for c in children:
        c.stdin.write(f"GO {start:.6f}\n")
        c.stdin.flush()
    time.sleep(max(0.0, start - time.time()))
    cpu0, w0 = server.cpu(), time.time()
    outs = [json.loads(c.communicate(timeout=seconds + 60)[0]) for c in children]
    cpu1, w1 = server.cpu(), time.time()
    lat = sorted(x for o in outs for x in o["lat"])
    conn_ms = [x for o in outs for x in o["connect"]]
    n = sum(o["n"] for o in outs)
    status = {}
    for o in outs:
        for k, v in o["status"].items():
            status[k] = status.get(k, 0) + v

    def pct(q):
        return lat[min(len(lat) - 1, int(round(q * (len(lat) - 1))))] if lat else None

    return {"scenario": name, "conc": conc, "procs": nproc, "seconds": seconds, "requests": n,
            "rps": n / seconds, "p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99), "max": lat[-1] if lat else None,
            "connect_max": max(conn_ms) if conn_ms else None, "connects": len(conn_ms),
            "kb": (sum(o["bytes"] for o in outs) / n / 1024) if n else 0.0,
            "errors": sum(o["errors"] for o in outs), "status": status,
            "server_cpu": (cpu1 - cpu0) / (w1 - w0) if w1 > w0 else None}


def warm_up(server, spec):
    """One request per distinct path, so lazy imports and first-read costs stay out of the numbers."""
    for path in dict.fromkeys(spec["paths"]):
        server.get(path, spec.get("headers"))


def run_matrix(server, names, concs, seconds, procs):
    r, _ = server.get("/static/search.js")
    specs = scenarios(r.getheader("ETag"))
    rows = []
    for n in names:
        warm_up(server, specs[n])
        rows += [run_cell(server, n, specs[n], c, seconds, procs) for c in concs]
    return rows


def format_table(rows, seconds):
    head = (f"Lynch Pin portal throughput: AI overview off · {len(SYMS)} cached tickers · {seconds:g} s per cell · "
            f"Python {sys.version.split()[0]} · {os.cpu_count()} CPUs · {sys.platform}")
    cols = f"{'scenario':<11}{'conc':>5}{'req/s':>10}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'KB/resp':>9}" \
           f"{'errors':>8}{'srv CPU':>9}{'conn max':>10}"
    lines = [head, "", cols, "-" * len(cols)]
    prev = None
    for r in rows:
        if prev and r["scenario"] != prev:
            lines.append("")
        prev = r["scenario"]
        f = lambda v: f"{v:.1f}" if v is not None else "—"  # noqa: E731
        cpu = f"{r['server_cpu'] * 100:.0f}%" if r["server_cpu"] is not None else "—"
        line = (f"{r['scenario']:<11}{r['conc']:>5}{r['rps']:>10,.0f}{f(r['p50']):>9}{f(r['p95']):>9}{f(r['p99']):>9}"
                f"{r['kb']:>9.1f}{r['errors']:>8}{cpu:>9}{f(r.get('connect_max')):>10}")
        if r["scenario"] == "page":
            line += f"   ({r['rps'] / len(PAGE):,.0f} page views/s)"
        lines.append(line)
    lines += ["", "srv CPU = server process CPU time / wall time (all threads, user + system). Python code runs on",
              "one core at a time (GIL); values above 100% are socket I/O done outside the GIL. A high srv CPU",
              "means the server is the limit; a low one with high latency means it is waiting, not working."]
    for msg in regressions(rows):
        lines += ["", f"⚠ {msg}"]
    return "\n".join(lines)


NAGLE_P50_MS = 20      # p50 at one connection above this = the ~50 ms delayed-ACK stall is back
SYN_RETRY_MS = 900     # a TCP connect this slow = a dropped SYN retried after ~1 s: backlog overflow


def regressions(rows):
    """Messages for the two known performance traps (empty = both server settings are doing their job)."""
    out = []
    stalled = [r for r in rows if r["conc"] == 1 and r["scenario"] != "connect"
               and r["p50"] is not None and r["p50"] >= NAGLE_P50_MS]
    if stalled:
        out.append(f"~{min(r['p50'] for r in stalled):.0f} ms per keep-alive response at one connection "
                   f"({', '.join(r['scenario'] for r in stalled)}): Nagle's algorithm is holding response bodies "
                   "until the client's delayed ACK. Check disable_nagle_algorithm = True on the handler in ui/server.py.")
    slow = [r for r in rows if (r.get("connect_max") or 0) >= SYN_RETRY_MS]
    if slow:
        worst = max(slow, key=lambda r: r["connect_max"])
        out.append(f"slowest TCP connect {worst['connect_max']:.0f} ms ({worst['scenario']} @ {worst['conc']} "
                   "connections): the listen backlog overflowed and clients waited for SYN retries. Check "
                   "request_queue_size on PortalServer in ui/server.py.")
    return out


# ── pytest ──────────────────────────────────────────────────────────────────
def _env_list(name, default):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def test_ai_overview_is_off_on_the_benchmark_server(tmp_path):
    server = BenchServer(str(tmp_path))
    try:
        r, body = server.get("/api/health")
        h = json.loads(body)
        assert r.status == 200 and h["features"] == {"search": True, "ai": False, "refresh": False} and "ai" not in h
        assert h["cache"]["size"] == len(SYMS)  # warmed: every lookup below is a cache hit
        r, body = server.get("/api/ticker/MSFT/ai")
        assert r.status == 404 and json.loads(body)["error"] == "AI disabled"
        r, body = server.get("/api/ticker/MSFT/deepdive")
        assert r.status == 200 and json.loads(body)["ai"] == "disabled"
    finally:
        server.close()


def test_throughput_benchmark(tmp_path, capsys):
    seconds = float(os.environ.get("LYNCH_BENCH_SECONDS", "0.75"))
    concs = [int(c) for c in _env_list("LYNCH_BENCH_CONCURRENCY", "1,16")]
    min_rps = float(os.environ.get("LYNCH_BENCH_MIN_RPS", "50"))
    known = list(scenarios("x"))
    names = _env_list("LYNCH_BENCH_SCENARIOS", ",".join(known))
    assert set(names) <= set(known), f"unknown scenario(s): {set(names) - set(known)}"

    server = BenchServer(str(tmp_path))
    try:
        rows = run_matrix(server, names, concs, seconds, default_procs())
        r, _ = server.get("/api/health")
        healthy = r.status == 200
    finally:
        server.close()

    report = format_table(rows, seconds)
    with capsys.disabled():  # visible without -s
        print("\n\n" + report)
    (tmp_path / "benchmark.txt").write_text(report)

    assert healthy, "server stopped answering after the benchmark"
    for r in rows:
        where = f"{r['scenario']} @ {r['conc']} connections"
        assert r["requests"] > 0, f"{where}: no request completed"
        assert r["errors"] == 0, f"{where}: {r['errors']} errors, statuses {r['status']}"
        assert r["rps"] >= min_rps, f"{where}: {r['rps']:.0f} req/s < floor {min_rps:g}"
    assert not regressions(rows), "\n".join(regressions(rows))
    with open(server.log_path) as f:
        log = f.read()
    assert "Traceback" not in log and '" 500 ' not in log, "server errors in the access log"


# ── standalone ──────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="Lynch Pin portal throughput benchmark (AI overview off)")
    ap.add_argument("--seconds", type=float, default=3.0, help="duration of each cell (default 3)")
    ap.add_argument("--concurrency", default="1,8,32,128", help="comma-separated connection counts")
    ap.add_argument("--scenarios", default=",".join(scenarios("x")), help="comma-separated scenario names")
    ap.add_argument("--procs", type=int, default=default_procs(), help="max load-generator processes")
    ap.add_argument("--json", help="also write the raw results to this file")
    ap.add_argument("--serve", metavar="DIR", help=argparse.SUPPRESS)
    ap.add_argument("--load", metavar="CFG", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.load:
        load_main(json.loads(a.load))
        return
    if a.serve:
        serve(a.serve)
        return
    names = [x.strip() for x in a.scenarios.split(",") if x.strip()]
    concs = [int(x) for x in a.concurrency.split(",") if x.strip()]
    with tempfile.TemporaryDirectory(prefix="lynch-bench-") as tmp:
        server = BenchServer(tmp)
        try:
            rows = run_matrix(server, names, concs, a.seconds, a.procs)
        finally:
            server.close()
    print(format_table(rows, a.seconds))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
