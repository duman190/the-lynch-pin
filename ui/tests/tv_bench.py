"""Trading view benchmark: how many people can use the per-ticker chart at once, and what they cost the cold lookups.

    python ui/tests/tv_bench.py record                     # Yahoo's 8 series for MU → ui/.cache/tv_fixtures (8 requests)
    python ui/tests/tv_bench.py offline                    # the server against those fixtures: no Yahoo, CPU only
    python ui/tests/tv_bench.py offline --python           # the same with the numpy builder (no native library)
    python ui/tests/tv_bench.py live --viewers 0,25,50,100 # real Yahoo: cold lookups/min with N chart viewers alongside

**offline** starts a portal whose trading view reads the fixtures instead of Yahoo (every ticker gets MU's bars, built
separately per ticker) and measures it from client processes:
* ``max``: closed loop, ``--conns`` connections asking back to back, each request a (ticker, interval) seen before
  (cached bytes / 304s): the HTTP ceiling.
* ``build``: closed loop, every request a (ticker, interval) never seen: a build each (fixture fetch + bars + gzip).
* ``viewers``: open loop, ``--viewers`` people each on their own ticker, as in ``live`` below.

**live** starts ``python -m ui.server --no-ai`` (fresh cache, FMP off, ``LYNCH_UI_TV_FORCE_OPEN=1``: market-hours
refresh rates at any hour, the worst case; the shipped viewer limit and Yahoo budget unless ``--unlimited``) once per
viewer count, then runs ``--count`` cold lookups from
``--clients`` threads (cold_bench.py's loop, Nasdaq-100) while N viewers use the trading view, each on its own S&P 500
ticker outside the Nasdaq-100 (nothing shared: the worst case for Yahoo). A viewer opens the chart, switches interval
every ~12 s (exponential, weighted towards 1D / 4H / 1H / 15m / 5m), and polls in between as the page does (every
15-45 s, ETag). Reported: cold lookups/min, their latency, the chart's latency, Yahoo requests and 429s.

Results go to tmp/tv_bench_<mode>.json.
"""
import argparse
import http.client
import json
import multiprocessing as mp
import os
import random
import secrets
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

FIXTURES = os.path.join(REPO, "ui", ".cache", "tv_fixtures")
# what a viewer switches to (TradingView's popular intervals weigh more)
WEIGHTS = {"1D": 25, "240": 12, "60": 12, "15": 10, "5": 10, "1W": 8, "1": 5, "30": 3, "120": 3, "3": 2, "10": 2,
           "45": 2, "180": 2, "2": 1, "1M": 1, "3M": 1, "6M": 0.5, "12M": 0.5}
SWITCH_EVERY = 12.0   # s, mean of an exponential think time
POLL = (15, 45)       # the page's poll bounds (tradingview.js MIN_POLL / MAX_POLL)


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def summary(lat):
    return {"n": len(lat), "p50_ms": round(pct(lat, 50) * 1000, 1) if lat else None,
            "p90_ms": round(pct(lat, 90) * 1000, 1) if lat else None,
            "p99_ms": round(pct(lat, 99) * 1000, 1) if lat else None,
            "max_ms": round(max(lat) * 1000, 1) if lat else None}


# ── record ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def record(sym):
    from ui import tvdata
    from ui.tradingview import YahooFetcher
    os.makedirs(FIXTURES, exist_ok=True)
    f = YahooFetcher()
    for base in tvdata.BASES:
        status, body = f(sym, base)
        assert status == 200, (base, status)
        with open(os.path.join(FIXTURES, f"{base}.json"), "wb") as fh:
            fh.write(body)
        print(f"{base:4} {len(body) // 1024:4} KB")


# ── the offline server ──────────────────────────────────────────────────────────────────────────────────────────────
def serve_offline(port, python_builder, fetch_ms):
    """A portal with only the trading view, which fetches the fixtures instead of Yahoo."""
    os.environ.update(LYNCH_UI_SOCIALS="0", LYNCH_UI_VALUATION="0")
    from ui import tvdata
    from ui.config import Settings
    from ui.server import PortalApp, PortalServer, make_handler
    from ui.tradingview import TradingView
    s = Settings()
    s.port, s.tv_viewers, s.tv_force_open = port, 0, True
    s.tv_yahoo_rpm = s.tv_viewer_rpm = 0  # the fixtures cost nothing: no budget
    raw = {b: open(os.path.join(FIXTURES, f"{b}.json"), "rb").read() for b in tvdata.BASES}

    def fetch(sym, base):
        if fetch_ms:
            time.sleep(fetch_ms / 1000)
        return 200, raw[base]

    build = (lambda r, code: tvdata.build(r, code, use_native=False)) if python_builder else None
    tv = TradingView(s, fetch=fetch, build=build)
    app = PortalApp(s, tv=tv)
    httpd = PortalServer(("127.0.0.1", port), make_handler(app))
    print(f"offline trading view on {port} ({'numpy' if python_builder else 'native'} builder)", flush=True)
    import sys as _s
    _s.stderr = open(os.devnull, "w")  # no access log
    httpd.serve_forever()


def wait_up(base, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"{base} did not come up")


def health(base):
    with urllib.request.urlopen(base + "/api/health", timeout=10) as r:
        return json.loads(r.read())


# ── clients ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class Conn:
    """One keep-alive connection, as a browser tab holds one."""

    def __init__(self, port):
        self.port = port
        self.c = None

    def get(self, path, headers=None):
        for attempt in (0, 1):
            try:
                if self.c is None:
                    self.c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
                self.c.request("GET", path, headers=headers or {})
                r = self.c.getresponse()
                body = r.read()
                return r.status, dict(r.getheaders()), body
            except (http.client.HTTPException, OSError):
                if self.c is not None:
                    self.c.close()
                self.c = None
                if attempt:
                    raise


def ticker_name(k):
    s, k = "", k
    for _ in range(3):
        s = chr(65 + k % 26) + s
        k //= 26
    return "Q" + s  # QAAA, QAAB…: valid symbols, one per viewer


def closed_loop(args):
    """(port, conns, seconds, mode, offset) → latencies: each connection asks back to back."""
    port, conns, seconds, mode, offset = args
    codes = list(WEIGHTS)
    out, lock = [], threading.Lock()
    stop = time.time() + seconds

    def worker(k):
        c = Conn(port)
        lat, n = [], 0
        sym = ticker_name(offset + k)
        while time.time() < stop:
            if mode == "build":  # every request a (ticker, interval) never seen
                sym, code = ticker_name(offset * 1000 + k * 997 + n), random.choice(codes)
            else:
                code = codes[n % len(codes)]
            t0 = time.perf_counter()
            st, _, _ = c.get(f"/api/tv/{sym}?i={code}", {"Accept-Encoding": "gzip"})
            lat.append(time.perf_counter() - t0)
            assert st == 200, st
            n += 1
        with lock:
            out.extend(lat)

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(conns)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def viewer_loop(args):
    """(port, symbols, seconds, seed, arrive) → per-request records of viewers using the chart like a person does."""
    port, symbols, seconds, seed, arrive = args
    rnd = random.Random(seed)
    codes, weights = list(WEIGHTS), list(WEIGHTS.values())
    recs, lock = [], threading.Lock()
    stop = time.time() + seconds

    def viewer(sym, k):
        time.sleep(rnd.random() * arrive)  # people don't all arrive in the same second
        c = Conn(port)
        sid = secrets.token_hex(8)
        etags = {}
        mine = []
        code = "1D"
        next_switch = time.time() + rnd.expovariate(1 / SWITCH_EVERY)
        kind = "open"
        while time.time() < stop:
            h = {"Accept-Encoding": "gzip"}
            if kind == "poll" and code in etags:
                h["If-None-Match"] = etags[code]
            t0 = time.perf_counter()
            try:
                st, hd, body = c.get(f"/api/tv/{sym}?i={code}&s={sid}", h)
            except Exception:
                st, hd, body = 0, {}, b""
            dt = time.perf_counter() - t0
            mine.append((kind, st, dt, len(body), time.time()))
            if st == 200 and hd.get("ETag"):
                etags[code] = hd["ETag"]
            if st == 429:
                time.sleep(float(hd.get("Retry-After", 10)))
                continue
            refresh = int(hd.get("X-TV-Refresh", POLL[1])) if st in (200, 304) else 5
            poll_at = time.time() + min(POLL[1], max(POLL[0], refresh))
            if next_switch <= poll_at:
                time.sleep(max(0.0, next_switch - time.time()))
                code = rnd.choices(codes, weights)[0]
                kind = "switch"
                next_switch = time.time() + rnd.expovariate(1 / SWITCH_EVERY)
            else:
                time.sleep(max(0.0, poll_at - time.time()))
                kind = "poll"
        try:
            c.get(f"/api/tv/release?s={sid}")
        except Exception:
            pass
        with lock:
            recs.extend((sym,) + r for r in mine)

    ts = [threading.Thread(target=viewer, args=(s, k), daemon=True) for k, s in enumerate(symbols)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return recs


def run_viewers(port, symbols, seconds, procs=4, arrive=5.0):
    chunks = [symbols[i::procs] for i in range(procs)]
    with mp.get_context("spawn").Pool(procs) as pool:
        parts = pool.map(viewer_loop, [(port, ch, seconds, 1000 + i, arrive) for i, ch in enumerate(chunks) if ch])
    return [r for p in parts for r in p]


def viewer_report(recs, seconds, n):
    by = {}
    for sym, kind, st, dt, size, ts in recs:
        by.setdefault(kind, []).append((st, dt, size))
    out = {"viewers": n, "requests": len(recs), "req_per_s": round(len(recs) / seconds, 1),
           "req_per_viewer_min": round(len(recs) / max(1, n) / (seconds / 60), 2)}
    for kind, rows in by.items():
        ok = [dt for st, dt, _ in rows if st in (200, 304)]
        out[kind] = dict(summary(ok), status={str(s): sum(1 for r in rows if r[0] == s) for s in sorted({r[0] for r in rows})},
                         kb=round(statistics.mean([sz for _, _, sz in rows]) / 1024, 1) if rows else 0)
    return out


def proc_cpu(pid):
    import psutil
    p = psutil.Process(pid)
    total = sum(p.cpu_times()[:2])
    for ch in p.children(recursive=True):
        try:
            total += sum(ch.cpu_times()[:2])
        except psutil.Error:
            pass
    return total


# ── offline ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def offline(a):
    if not os.path.isdir(FIXTURES):
        sys.exit(f"no fixtures in {FIXTURES}: run `python ui/tests/tv_bench.py record` first")
    port = a.port
    env = dict(os.environ, PYTHONWARNINGS="ignore")
    srv = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", "--port", str(port)]
                           + (["--python"] if a.python else []) + ["--fetch-ms", str(a.fetch_ms)],
                           env=env, cwd=REPO)
    results = {"builder": "numpy" if a.python else "native", "fetch_ms": a.fetch_ms}
    try:
        wait_up(f"http://127.0.0.1:{port}")
        procs = 4
        for mode in ("build", "max"):
            if mode == "max":  # warm: every (ticker, interval) of the max run built once
                closed_loop((port, a.conns, 3, "max", 0))
            cpu0, t0 = proc_cpu(srv.pid), time.time()
            per = max(1, a.conns // procs)
            with mp.get_context("spawn").Pool(procs) as pool:
                parts = pool.map(closed_loop, [(port, per, a.seconds, mode, i * per) for i in range(procs)])
            wall = time.time() - t0
            lat = [x for p in parts for x in p]
            cpu = proc_cpu(srv.pid) - cpu0
            results[mode] = dict(summary(lat), req_per_s=round(len(lat) / wall, 1), server_cpu_cores=round(cpu / wall, 2),
                                 server_cpu_ms_per_req=round(cpu / max(1, len(lat)) * 1000, 3), conns=per * procs)
            print(f"{mode:6} {per * procs} conns: {len(lat) / wall:8.1f} req/s  p50 {results[mode]['p50_ms']} ms  "
                  f"p99 {results[mode]['p99_ms']} ms  server CPU {cpu / wall:.2f} cores "
                  f"({cpu / max(1, len(lat)) * 1000:.3f} ms/request)", flush=True)
        for n in a.viewers:
            cpu0, t0 = proc_cpu(srv.pid), time.time()
            recs = run_viewers(port, [ticker_name(5000 + k) for k in range(n)], a.seconds * 2)
            wall = time.time() - t0
            cpu = proc_cpu(srv.pid) - cpu0
            rep = viewer_report(recs, wall, n)
            rep.update(server_cpu_cores=round(cpu / wall, 3), server_cpu_ms_per_viewer_min=round(cpu * 1000 / n / (wall / 60), 1))
            results[f"viewers_{n}"] = rep
            sw = rep.get("switch") or rep.get("open") or {}
            print(f"viewers {n:5}: {rep['req_per_s']:7.1f} req/s, switch p50 {sw.get('p50_ms')} p99 {sw.get('p99_ms')} ms, "
                  f"server CPU {cpu / wall:.2f} cores = {rep['server_cpu_ms_per_viewer_min']} ms per viewer-minute", flush=True)
    finally:
        srv.send_signal(signal.SIGTERM)
        srv.wait(10)
    return results


# ── live ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def cold_lookups(base, tickers, clients):
    """cold_bench.py's loop: (results, wall seconds)."""
    from concurrent.futures import ThreadPoolExecutor

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def cold(sym):
        t0, first = time.time(), True
        while True:
            code, s = get(f"/api/ticker/{sym}" + ("" if first else "?poll=1"))
            if code == 429:
                time.sleep(1)
                continue
            first = False
            if s.get("status") in ("done", "nodata", "error"):
                return sym, s["status"], time.time() - t0, s.get("cached")
            time.sleep(0.2)

    t0 = time.time()
    with ThreadPoolExecutor(clients) as ex:
        res = list(ex.map(cold, tickers))
    return res, time.time() - t0


def live(a):
    nasdaq = [l.strip() for l in open(os.path.join(REPO, a.file)) if l.strip()]
    cold = nasdaq[a.offset:a.offset + a.count]
    others = [l.strip() for l in open(os.path.join(REPO, "database", "sp500.txt")) if l.strip() and l.strip() not in nasdaq]
    rnd = random.Random(7)
    rnd.shuffle(others)
    results = {"workers": a.workers, "clients": a.clients, "count": a.count, "runs": []}
    for n in a.viewers:
        cache = tempfile.mkdtemp(prefix="tvbench-")
        env = dict(os.environ, LYNCH_UI_CACHE_DIR=cache, LYNCH_UI_SOCIALS="0", LYNCH_UI_VALUATION="0", FMP_API_KEY="",
                   LYNCH_UI_TV_FORCE_OPEN="1", PYTHONWARNINGS="ignore")
        if a.unlimited:  # the trading view's raw cost: no viewer limit, no Yahoo budget
            env.update(LYNCH_UI_TV_VIEWERS="0", LYNCH_UI_TV_YAHOO_RPM="0", LYNCH_UI_TV_VIEWER_RPM="0")
        log = open(os.path.join(cache, "server.log"), "w")
        srv = subprocess.Popen([sys.executable, "-m", "ui.server", "--no-ai", "--port", str(a.port), "--enrich", "off",
                                "--workers", str(a.workers), "--precache", "0"], env=env, cwd=REPO, stdout=log, stderr=log)
        base = f"http://127.0.0.1:{a.port}"
        try:
            wait_up(base)
            time.sleep(a.warmup)  # the analysis processes import the engine
            out = {}
            cpu0 = proc_cpu(srv.pid)
            th = None
            if n:
                symbols = (others * (n // len(others) + 1))[:n]
                box = {}
                th = threading.Thread(target=lambda: box.update(recs=run_viewers(a.port, symbols, a.tv_seconds,
                                                                                 arrive=a.lead)))
                th.start()
                time.sleep(a.lead)  # viewers are on the charts before the lookups start
            res, wall = cold_lookups(base, cold, a.clients)
            cpu = proc_cpu(srv.pid) - cpu0
            h = health(base)
            if th is not None:
                th.join()
            statuses = {}
            for _, st, _, _ in res:
                statuses[st] = statuses.get(st, 0) + 1
            lat = [dt for _, _, dt, _ in res]
            out.update(viewers=n, lookups_per_min=round(len(res) / wall * 60, 1), wall_s=round(wall, 1),
                       lookup=summary(lat), statuses=statuses, server_cpu_cores=round(cpu / (wall + (a.lead if n else 0)), 2),
                       yahoo_429=h.get("cache", {}).get("rate_limit_hits"), tv=h.get("tv"))
            if n:
                recs = [r for r in box["recs"]]
                out["chart"] = viewer_report(recs, a.tv_seconds, n)
                tvh = h.get("tv") or {}
                out["chart"]["yahoo_fetches"] = tvh.get("fetches")
                out["chart"]["yahoo_fetches_per_viewer_min"] = round((tvh.get("fetches") or 0) / n / (a.tv_seconds / 60), 2)
            results["runs"].append(out)
            ch = out.get("chart") or {}
            sw = ch.get("switch") or {}
            print(f"viewers {n:4}: {out['lookups_per_min']:6.1f} cold lookups/min (p50 {out['lookup']['p50_ms'] / 1000:.1f} s, "
                  f"p90 {out['lookup']['p90_ms'] / 1000:.1f} s) {statuses}  Yahoo 429s {out['yahoo_429']}"
                  + (f" | chart switch p50 {sw.get('p50_ms')} p90 {sw.get('p90_ms')} ms, {ch.get('req_per_s')} req/s, "
                     f"Yahoo fetches {ch.get('yahoo_fetches')} ({ch.get('yahoo_fetches_per_viewer_min')}/viewer-min)" if n else ""),
                  flush=True)
        finally:
            srv.send_signal(signal.SIGTERM)
            try:
                srv.wait(20)
            except subprocess.TimeoutExpired:
                srv.kill()
            log.close()
        time.sleep(a.pause)
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("record")
    r.add_argument("--sym", default="MU")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--python", action="store_true")
    s.add_argument("--fetch-ms", type=float, default=0)
    o = sub.add_parser("offline")
    o.add_argument("--port", type=int, default=8797)
    o.add_argument("--python", action="store_true", help="numpy builder instead of the native library")
    o.add_argument("--conns", type=int, default=32)
    o.add_argument("--seconds", type=float, default=10)
    o.add_argument("--fetch-ms", type=float, default=0, help="simulated Yahoo latency per fetch")
    o.add_argument("--viewers", type=lambda v: [int(x) for x in v.split(",") if x], default=[250, 1000])
    lv = sub.add_parser("live")
    lv.add_argument("--port", type=int, default=8798)
    lv.add_argument("--viewers", type=lambda v: [int(x) for x in v.split(",") if x], default=[0, 25, 50, 100])
    lv.add_argument("--clients", type=int, default=24)
    lv.add_argument("--count", type=int, default=96)
    lv.add_argument("--offset", type=int, default=0)
    lv.add_argument("--file", default="database/nasdaq_100.txt")
    lv.add_argument("--workers", type=int, default=8)
    lv.add_argument("--warmup", type=float, default=8)
    lv.add_argument("--lead", type=float, default=10,
                    help="s the viewers take to arrive (spread evenly) before the lookups start")
    lv.add_argument("--tv-seconds", type=float, default=80, help="how long the viewers stay")
    lv.add_argument("--pause", type=float, default=20, help="s between runs (Yahoo)")
    lv.add_argument("--unlimited", action="store_true",
                    help="no viewer limit and no Yahoo budget (the shipped defaults otherwise)")
    a = ap.parse_args(argv)
    if a.mode == "record":
        return record(a.sym)
    if a.mode == "serve":
        return serve_offline(a.port, a.python, a.fetch_ms)
    res = offline(a) if a.mode == "offline" else live(a)
    os.makedirs(os.path.join(REPO, "tmp"), exist_ok=True)
    path = os.path.join(REPO, "tmp", f"tv_bench_{a.mode}{'_python' if getattr(a, 'python', False) else ''}.json")
    with open(path, "w") as f:
        json.dump(res, f, indent=1)
    print(f"→ {os.path.relpath(path, REPO)}")


if __name__ == "__main__":
    main()
