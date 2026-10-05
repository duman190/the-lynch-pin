"""Live benchmark of the AI overview against a real LM Studio server (not part of the offline test suite).

    python ui/tests/llm_bench.py --llm-url http://192.168.86.186:1234 --conc 1,2,4,6,8
    python ui/tests/llm_bench.py --llm-url http://192.168.86.186:1234 --conc 1 --show 3   # print 3 replies

Analyses real tickers once (live Yahoo, cached in ui/.cache/llm_bench.pkl for the day), then, for each
concurrency level C, streams overviews through ``LocalLLMClient.generate`` with C requests in flight and
max(4, 2·C) requests in total, every one a different ticker (the portal generates one overview per ticker
per day). A short nonce starts each data block so a prompt seen in an earlier run is never an exact-repeat
cache hit; the shared system prompt is still cached, as in production. Reports per level: overviews/min,
total tokens/s, mean/max time to first token, per-stream tokens/s, latency and complete (3/3) replies.
"""
import argparse
import datetime as dt
import json
import os
import pickle
import random
import statistics as S
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ui import llm  # noqa: E402
from ui.config import Settings  # noqa: E402

TICKERS = ("MSFT NVDA INTC KO PLTR CAT AAPL GOOGL AMZN META AVGO COST JPM V UNH XOM PEP AMD NFLX ADBE CRM ORCL HD "
           "LLY MRK WMT").split()


def analyses(settings, tickers, path):
    """{sym: analysis} for today, from ``path`` when fresh, else analysed live (≈5 s per ticker)."""
    today = dt.date.today().isoformat()
    cached = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            day, cached = pickle.load(f)
        cached = cached if day == today else {}
    missing = [t for t in tickers if t not in cached]
    if missing:
        from ui.analysis import TickerAnalyzer
        an = TickerAnalyzer(settings)
        for sym in missing:
            d = an.run(sym)
            ok = d["status"] == "done" and (d.get("_ai_inputs") or {}).get("row")
            print(f"  analysed {sym}: {'ok' if ok else d.get('reason') or d['status']}", flush=True)
            if ok:
                cached[sym] = {k: v for k, v in d.items() if k != "_ai_inputs"} | {"_ai_inputs": {"row": True}}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump((today, cached), f)
    return {t: cached[t] for t in tickers if t in cached}


def run_level(settings, data, conc, n, max_tokens=None):
    syms = list(data)
    random.shuffle(syms)
    jobs = iter(syms[:n] if n <= len(syms) else [syms[k % len(syms)] for k in range(n)])
    nonce = f"[run {random.randint(1000, 9999)}]"
    lock, res = threading.Lock(), []
    gate = threading.Barrier(conc)

    def worker():
        client = llm.LocalLLMClient(settings)
        client.status()
        gate.wait()
        while True:
            with lock:
                sym = next(jobs, None)
            if sym is None:
                return
            msgs = llm.build_portal_messages(data[sym])
            msgs[-1] = dict(msgs[-1], content=f"{nonce}\n{msgs[-1]['content']}")
            t0 = time.time()
            try:
                text, meta = client.generate(msgs, max_tokens=max_tokens)
            except llm.LLMError as e:
                print(f"  {sym}: {e}", flush=True)
                continue
            m = meta["metrics"]
            with lock:
                res.append({"sym": sym, "ttft": m["ttft_s"] or 0, "tok_s": m["tok_s"] or 0, "out": m["tokens"],
                            "prompt": (meta.get("usage") or {}).get("prompt_tokens") or 0, "latency": time.time() - t0,
                            "complete": llm.section_score(llm.parse_sections(text)) == 3, "text": text})

    threads = [threading.Thread(target=worker) for _ in range(conc)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    if not res:
        return None, res
    return {"conc": conc, "n": len(res), "per_min": len(res) / wall * 60, "agg_tok_s": sum(r["out"] for r in res) / wall,
            "ttft": S.mean(r["ttft"] for r in res), "ttft_max": max(r["ttft"] for r in res),
            "tok_s": S.mean(r["tok_s"] for r in res), "latency": S.mean(r["latency"] for r in res),
            "prompt": S.mean(r["prompt"] for r in res), "out": S.mean(r["out"] for r in res),
            "complete": sum(r["complete"] for r in res)}, res


def main(argv=None):
    s = Settings()
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--llm-url", default=s.llm_base_url)
    p.add_argument("--llm-model", default=s.llm_model)
    p.add_argument("--conc", default="1,2,4", help="comma-separated concurrency levels")
    p.add_argument("--tickers", default=",".join(TICKERS))
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument("--show", type=int, default=0, help="print this many replies per level")
    p.add_argument("--json", help="append each level's summary and replies to this JSON-lines file")
    a = p.parse_args(argv)
    s.llm_base_url, s.llm_model = a.llm_url.rstrip("/"), a.llm_model
    st = llm.LocalLLMClient(s).status()
    if not st.get("available"):
        sys.exit(f"LM Studio unavailable: {st.get('reason')}")
    data = analyses(s, a.tickers.split(","), os.path.join(s.cache_dir, "llm_bench.pkl"))
    print(f"model {st['model']} · ctx {st.get('ctx')} · {len(data)} tickers")
    print(f"{'conc':>4} {'n':>3} {'ovw/min':>7} {'total t/s':>9} {'TTFT':>6} {'TTFT max':>8} {'t/s each':>8} "
          f"{'latency':>7} {'prompt':>6} {'reply':>5} {'3/3':>4}")
    for conc in (int(c) for c in a.conc.split(",")):
        summ, res = run_level(s, data, conc, max(4, 2 * conc), a.max_tokens)
        if summ is None:
            print(f"{conc:>4}  all requests failed")
            continue
        print(f"{conc:>4} {summ['n']:>3} {summ['per_min']:>7.2f} {summ['agg_tok_s']:>9.1f} {summ['ttft']:>5.1f}s "
              f"{summ['ttft_max']:>7.1f}s {summ['tok_s']:>8.1f} {summ['latency']:>6.1f}s {summ['prompt']:>6.0f} "
              f"{summ['out']:>5.0f} {summ['complete']:>2}/{summ['n']}", flush=True)
        for r in res[:a.show]:
            print(f"\n── {r['sym']} ──\n{r['text']}\n")
        if a.json:
            with open(a.json, "a") as f:
                f.write(json.dumps({"model": st["model"], **summ, "runs": res}) + "\n")


if __name__ == "__main__":
    main()
