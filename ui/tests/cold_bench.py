"""Cold-lookup benchmark against a running portal: real Yahoo / SEC traffic, nothing cached.

    python ui/tests/cold_bench.py                                   # 12 Nasdaq-100 tickers, 4 at a time
    python ui/tests/cold_bench.py --base http://127.0.0.1:8790 --clients 16 --count 32 --offset 12

Start the server with an empty cache (e.g. LYNCH_UI_CACHE_DIR=$(mktemp -d)) or pick tickers it has not
seen today (--offset), otherwise lookups are cache hits. Run it with the FMP key unset and --enrich off
so the benchmark does not spend the FMP daily quota.
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8765", help="portal URL (default %(default)s)")
    ap.add_argument("--clients", type=int, default=4, help="tickers requested at the same time (default 4)")
    ap.add_argument("--count", type=int, default=12, help="tickers to look up (default 12)")
    ap.add_argument("--offset", type=int, default=0, help="skip this many tickers of the list first")
    ap.add_argument("--file", default="database/nasdaq_100.txt", help="ticker list, one per line")
    a = ap.parse_args(argv)
    tickers = [l.strip() for l in open(a.file) if l.strip()][a.offset:a.offset + a.count]

    def get(path):
        try:
            with urllib.request.urlopen(a.base + path, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def cold(sym):
        t0, first, busy = time.time(), True, 0
        while True:
            code, s = get(f"/api/ticker/{sym}" + ("" if first else "?poll=1"))
            if code == 429:  # queue full: retry
                busy += 1
                time.sleep(1)
                continue
            first = False
            if s["status"] in ("done", "nodata", "error"):
                return (sym, s["status"], time.time() - t0, busy, (s.get("data") or {}).get("stage_ms", {}),
                        s.get("cached"))
            time.sleep(0.2)

    t0 = time.time()
    statuses = {}
    with ThreadPoolExecutor(a.clients) as ex:
        for sym, st, dt, busy, stages, cached in ex.map(cold, tickers):
            statuses[st] = statuses.get(st, 0) + 1
            print(f"{sym:<6} {st:<7} {dt:6.1f}s  busy={busy}  {'CACHED ' if cached else ''}{stages}")
    wall = time.time() - t0
    print(f"\n{len(tickers)} tickers ({a.clients} clients) in {wall:.1f}s -> {len(tickers) / wall * 60:.1f} tickers/min"
          f"  {statuses}")


if __name__ == "__main__":
    main()
