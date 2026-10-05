"""Cold-lookup benchmark against a running portal: real Yahoo / SEC traffic, nothing cached.

    python ui/tests/cold_bench.py                                   # 12 Nasdaq-100 tickers, 4 at a time
    python ui/tests/cold_bench.py --base http://127.0.0.1:8790 --clients 16 --count 32 --offset 12
    python ui/tests/cold_bench.py --base http://127.0.0.1:8790 --clients 24 --count 48 --visitors --home

Start the server with an empty cache (e.g. LYNCH_UI_CACHE_DIR=$(mktemp -d)) or pick tickers it has not
seen today (--offset), otherwise lookups are cache hits. Run it with the FMP key unset and --enrich off
so the benchmark does not spend the FMP daily quota.

--visitors: each lookup comes from its own visitor IP, sent as CF-Connecting-IP the way Cloudflare's
tunnel does. Needed against --public, which allows one analysis at a time per visitor: without it every
lookup comes from 127.0.0.1 and waits for the previous one.
--home: each visitor first loads the home page (HTML, CSS, scripts, artwork, Latest scans with its
chart previews, Socials with its post images), as a browser does before the search.
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HOME = ["/", "/static/app.css", "/static/app.js", "/static/search.js", "/static/scans.js", "/static/socials.js",
        "/static/img/logo.png", "/static/img/hero_wide.webp", "/api/health"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8765", help="portal URL (default %(default)s)")
    ap.add_argument("--clients", type=int, default=4, help="tickers requested at the same time (default 4)")
    ap.add_argument("--count", type=int, default=12, help="tickers to look up (default 12)")
    ap.add_argument("--offset", type=int, default=0, help="skip this many tickers of the list first")
    ap.add_argument("--file", default="database/nasdaq_100.txt", help="ticker list, one per line")
    ap.add_argument("--visitors", action="store_true", help="one visitor IP per lookup (CF-Connecting-IP)")
    ap.add_argument("--home", action="store_true", help="each visitor loads the home page before searching")
    a = ap.parse_args(argv)
    tickers = [l.strip() for l in open(a.file) if l.strip()][a.offset:a.offset + a.count]

    def fetch(path, ip):
        req = urllib.request.Request(a.base + path, headers={"CF-Connecting-IP": ip} if ip else {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(path, ip):
        code, body = fetch(path, ip)
        return code, json.loads(body)

    def home(ip):
        """The home page as a browser loads it: (requests, bytes, seconds); raises on any non-200."""
        t0, n, size = time.time(), 0, 0
        paths = list(HOME)
        for api, key in (("/api/scans", "scans"), ("/api/socials", "x")):
            code, body = fetch(api, ip)
            n, size = n + 1, size + len(body)
            assert code == 200, (api, code)
            for item in json.loads(body).get(key) or []:
                img = item.get("image")  # scans: {"src", "full"} (the page shows src); socials: a URL
                if img:
                    paths.append(img["src"] if isinstance(img, dict) else img)
        for p in paths:
            code, body = fetch(p, ip)
            n, size = n + 1, size + len(body)
            assert code == 200, (p, code)
        return n, size, time.time() - t0

    def cold(job):
        k, sym = job
        ip = f"198.51.{100 + k // 250}.{k % 250 + 1}" if a.visitors else None  # TEST-NET-2 addresses
        h = home(ip) if a.home else (0, 0, 0.0)
        t0, first, busy = time.time(), True, 0
        while True:
            code, s = get(f"/api/ticker/{sym}" + ("" if first else "?poll=1"), ip)
            if code == 429:  # queue full, or this visitor already has an analysis running: retry
                busy += 1
                time.sleep(1)
                continue
            first = False
            if s["status"] in ("done", "nodata", "error"):
                return (sym, s["status"], time.time() - t0, busy, (s.get("data") or {}).get("stage_ms", {}),
                        s.get("cached"), h)
            time.sleep(0.2)

    t0 = time.time()
    statuses, homes = {}, []
    with ThreadPoolExecutor(a.clients) as ex:
        for sym, st, dt, busy, stages, cached, h in ex.map(cold, enumerate(tickers)):
            statuses[st] = statuses.get(st, 0) + 1
            homes.append(h)
            print(f"{sym:<6} {st:<7} {dt:6.1f}s  busy={busy}  {'CACHED ' if cached else ''}{stages}")
    wall = time.time() - t0
    print(f"\n{len(tickers)} tickers ({a.clients} clients) in {wall:.1f}s -> {len(tickers) / wall * 60:.1f} tickers/min"
          f"  {statuses}")
    if a.home:
        reqs, size = sum(h[0] for h in homes), sum(h[1] for h in homes)
        print(f"home page: {reqs // len(homes)} requests, {size / len(homes) / 1024:.0f} KB, "
              f"avg {sum(h[2] for h in homes) / len(homes) * 1000:.0f} ms, max {max(h[2] for h in homes) * 1000:.0f} ms "
              f"(alongside the analyses)")


if __name__ == "__main__":
    main()
