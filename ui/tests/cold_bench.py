import json, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
BASE, CLIENTS = "http://127.0.0.1:8765", 4           # CLIENTS = tickers requested at the same time
tickers = [l.strip() for l in open("database/nasdaq_100.txt") if l.strip()][:12]
  
def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)
  
def cold(sym):
    t0, first, busy = time.time(), True, 0
    while True:
        code, s = get(f"/api/ticker/{sym}" + ("" if first else "?poll=1"))
        if code == 429: busy += 1; time.sleep(1); continue           # queue full: retry
        first = False
        if s["status"] in ("done", "nodata", "error"):
            return sym, s["status"], time.time() - t0, busy, (s.get("data") or {}).get("stage_ms", {}), s.get("cached")
        time.sleep(0.2)
  
t0 = time.time()
with ThreadPoolExecutor(CLIENTS) as ex:
    for sym, st, dt, busy, stages, cached in ex.map(cold, tickers):
        print(f"{sym:<6} {st:<7} {dt:6.1f}s  busy={busy}  {'CACHED ' if cached else ''}{stages}")
wall = time.time() - t0
print(f"\n{len(tickers)} tickers in {wall:.1f}s -> {len(tickers) / wall * 60:.1f} tickers/min")
