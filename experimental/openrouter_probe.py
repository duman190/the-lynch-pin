"""OpenRouter probe — send the real Lynch Pin prompt to the free router the way
engine/ai_research.py does (validation + re-roll retry), then build the X thread
preview exactly as main.py would. Nothing is posted.

Run from the project root (needs OPENROUTER_API_KEY):

    python tmp/openrouter_probe.py                              # openrouter/free via _call_ai (retries garbage)
    python tmp/openrouter_probe.py --model qwen/qwen3-8b:free   # pin a specific free model
    python tmp/openrouter_probe.py --single                     # one raw draw, no validation/retry
    python tmp/openrouter_probe.py --raw tmp/openrouter_raw.txt # re-parse a saved reply, no API call
    python tmp/openrouter_probe.py --no-normalize               # how main.py fares on the untouched reply

The raw reply is always saved to tmp/openrouter_raw.txt so a bad one can be replayed.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# probe_common.py ships in tmp/; also look there when this probe is run from another folder
sys.path[:0] = [_HERE, os.path.join(os.path.dirname(_HERE), "tmp")]
from probe_common import LynchPinResearcher, build_prompt, tickers, save_raw, render_preview  # noqa: E402
from engine.ai_research import OPENROUTER_FREE_MODEL  # noqa: E402

RAW_PATH = "tmp/openrouter_raw.txt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=OPENROUTER_FREE_MODEL, help="OpenRouter model id")
    ap.add_argument("--raw", help="Skip the API call; parse a saved raw reply instead")
    ap.add_argument("--idx", default="SMH")
    ap.add_argument("--no-normalize", action="store_true", help="Show how main.py fares on the untouched reply")
    ap.add_argument("--single", action="store_true", help="One raw draw from the router, no validation/retry")
    args = ap.parse_args()

    syms = tickers()
    prompt = build_prompt(args.idx)

    if args.raw:
        with open(args.raw, encoding="utf-8") as f:
            raw = f.read()
        print(f"📄 Loaded raw reply from {args.raw} ({len(raw)} chars)")
    else:
        if not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("OPENROUTER_API_KEY not set")
        # Force the OpenRouter-only path: no Gemini, no paid Meta tier
        for k in ("GEMINI_API_KEY", "META_API_KEY", "MODEL_API_KEY"):
            os.environ.pop(k, None)
        researcher = LynchPinResearcher()
        researcher.openrouter_model = args.model
        print(f"📤 Prompt: {len(prompt)} chars → {args.model} "
              f"({'single draw' if args.single else f'via _call_ai: up to {researcher.ATTEMPTS_PER_TIER} attempts, garbage re-rolled'})")
        if args.single:
            raw = researcher._call_openrouter_model(args.model, prompt)
        else:
            norm = lambda t: LynchPinResearcher.normalize_narrative(t, syms)
            raw = researcher._call_ai(prompt,
                                      check=lambda t: LynchPinResearcher.narrative_gaps(norm(t), syms),
                                      score=lambda t: LynchPinResearcher.narrative_coverage(norm(t), syms))
        save_raw(raw, RAW_PATH)
        print(f"📥 Reply: {len(raw)} chars (saved to {RAW_PATH})")

    render_preview(raw, args.idx, normalize=not args.no_normalize)


if __name__ == "__main__":
    main()
