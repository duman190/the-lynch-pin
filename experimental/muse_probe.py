"""Muse probe — send the real Lynch Pin prompt to Meta's paid Tier 4
(Muse Spark 1.3 Contributor) exactly as engine/ai_research.py does, then build the
X thread preview exactly as main.py would. Nothing is posted.

⚠️  This tier is PAID (pay-as-you-go on dev.meta.ai). One batch prompt is ~3K tokens in /
~4K out ≈ $0.001. The probe makes at most ATTEMPTS_PER_TIER calls per run (--single: 1).

Run from the project root (needs META_API_KEY or MODEL_API_KEY):

    python tmp/muse_probe.py                        # Meta only, via _call_ai (validation + backoff on 429)
    python tmp/muse_probe.py --single               # one raw call, no validation/retry
    python tmp/muse_probe.py --ping                 # 1-line sanity check of key/endpoint (~10 tokens)
    python tmp/muse_probe.py --raw tmp/muse_raw.txt # re-parse a saved reply, no API call
    python tmp/muse_probe.py --no-normalize         # how main.py fares on the untouched reply

The raw reply is always saved to tmp/muse_raw.txt so it can be replayed.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_common import LynchPinResearcher, build_prompt, tickers, save_raw, render_preview  # noqa: E402
from engine.ai_research import META_MODEL, META_URL  # noqa: E402

RAW_PATH = "tmp/muse_raw.txt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=META_MODEL, help="Meta model id")
    ap.add_argument("--raw", help="Skip the API call; parse a saved raw reply instead")
    ap.add_argument("--idx", default="SMH")
    ap.add_argument("--no-normalize", action="store_true", help="Show how main.py fares on the untouched reply")
    ap.add_argument("--single", action="store_true", help="One raw call, no validation/retry")
    ap.add_argument("--ping", action="store_true", help="Tiny request to verify key + endpoint, then exit")
    args = ap.parse_args()

    syms = tickers()
    prompt = build_prompt(args.idx)

    if args.raw:
        with open(args.raw, encoding="utf-8") as f:
            raw = f.read()
        print(f"📄 Loaded raw reply from {args.raw} ({len(raw)} chars)")
    else:
        if not (os.environ.get("META_API_KEY") or os.environ.get("MODEL_API_KEY")):
            sys.exit("META_API_KEY (or MODEL_API_KEY) not set")
        # Force the Meta-only path: no Gemini, no OpenRouter
        for k in ("GEMINI_API_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)
        researcher = LynchPinResearcher()
        researcher.meta_model = args.model
        print(f"🔗 {META_URL} → {args.model}  (tiers: {[t[0] for t in researcher._tiers()]})")

        if args.ping:
            t0 = time.time()
            try:
                text = researcher._call_meta_model(args.model, "Reply with the single word OK.")
                print(f"[✓] {args.model} {time.time() - t0:4.1f}s -> {text!r}")
            except Exception as e:
                print(f"[✗] {args.model} {time.time() - t0:4.1f}s -> {type(e).__name__}: {str(e)[:300]}")
            return

        print(f"📤 Prompt: {len(prompt)} chars → {args.model} "
              f"({'single call' if args.single else f'via _call_ai: up to {researcher.ATTEMPTS_PER_TIER} attempts'})")
        t0 = time.time()
        if args.single:
            raw = researcher._call_meta_model(args.model, prompt)
        else:
            norm = lambda t: LynchPinResearcher.normalize_narrative(t, syms)
            raw = researcher._call_ai(prompt,
                                      check=lambda t: LynchPinResearcher.narrative_gaps(norm(t), syms),
                                      score=lambda t: LynchPinResearcher.narrative_coverage(norm(t), syms))
        save_raw(raw, RAW_PATH)
        print(f"📥 Reply: {len(raw)} chars in {time.time() - t0:.1f}s (saved to {RAW_PATH})")

    render_preview(raw, args.idx, normalize=not args.no_normalize)


if __name__ == "__main__":
    main()
