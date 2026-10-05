"""Scan archive: each daily scan's X thread (text + charts) in its own folder.

    scans/<kind>/scan.json             the thread (header post, one reply per ticker, footer) and the
                                       run's raw AI overview (sentiment, portfolio thesis, per ticker)
    scans/<kind>/<TICKER>_valuation.png
    scans/<kind>/<idx>_benchmark.png   (portfolio_allocation.png for the portfolio X-ray)

One folder per scan kind (mags, qqq, schd, smh, igv, fintwit, portfolio), so Monday's GOOGL chart
in scans/mags/ is never overwritten by Saturday's GOOGL chart in scans/fintwit/; each run replaces
only its own folder (next Monday's MAGS scan replaces this Monday's). The portal's "Latest scans"
widget (ui/scans.py) reads these folders. It is separate from images/, the Threads upload area that
main.py clears and pushes to GitHub on every run. scans/ is local (git-ignored).

    python -m social.scan_archive --backfill logs/run_20261005.log ...   # rebuild from run logs + git
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime

SCAN_DIR = "scans"
SCAN_FILE = "scan.json"
SCAN_VERSION = 1
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,80}\.png$")

# kind -> (short name on the widget, what was scanned)
SCAN_KINDS = {
    "mags": ("MAGS", "Magnificent 7"),
    "qqq": ("QQQ", "Nasdaq 100"),
    "schd": ("SCHD", "Dow Jones Dividend 100"),
    "smh": ("SMH", "Semiconductor sector"),
    "igv": ("IGV", "Software sector"),
    "fintwit": ("X Favorite 100", "Most discussed stocks on FinTwit this week"),
    "portfolio": ("Portfolio X-Ray", "Weekly portfolio update"),
}


def scan_kind(idx_name, src=None, weekly=False, portfolio=False):
    """Archive folder for a run: the index ETF (mags, qqq...), fintwit, portfolio, or the source file's stem."""
    if portfolio:
        return "portfolio"
    if weekly:
        return "fintwit"
    if idx_name and idx_name.upper() != "SPY":
        return idx_name.lower()
    stem = os.path.splitext(os.path.basename(src or "spy"))[0].lower()
    return re.sub(r"[^a-z0-9_\-]+", "-", stem).strip("-")[:32] or "spy"


def split_ai_overviews(bulk_text, tickers):
    """Each ticker's AI overview from the batch narrative ("$TICKER:" header lines), untruncated."""
    out = {}
    for t in tickers:
        m = re.search(rf"^\${re.escape(t)}\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)", bulk_text or "", re.DOTALL | re.MULTILINE)
        if m and m.group(1).strip():
            out[t] = m.group(1).strip()
    return out


def save_scan(root, kind, posts, day=None, title=None, subtitle=None, ai=None):
    """Writes ``posts`` (dicts with ``text``, optional ``ticker``, ``image`` = source PNG path and
    ``name`` = archived file name) and the run's AI overview ``ai`` (a JSON-able dict, e.g.
    sentiment / portfolio narrative / per-ticker text) to ``root/kind/``, replacing that folder only.

    The new folder is built next to the old one and swapped in, so a reader never sees half a scan.
    Returns the folder path.
    """
    if not KIND_RE.match(kind):
        raise ValueError(f"invalid scan kind: {kind!r}")
    day = day or datetime.now().date().isoformat()
    default_title, default_sub = SCAN_KINDS.get(kind, (kind.upper(), ""))
    os.makedirs(root, exist_ok=True)
    final = os.path.join(root, kind)
    stage = os.path.join(root, f".{kind}.staging")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    out_posts = []
    for p in posts:
        entry = {"text": str(p.get("text") or "").strip()}
        if p.get("ticker"):
            entry["ticker"] = str(p["ticker"])
        src = p.get("image")
        if src and os.path.exists(src):
            name = p.get("name") or os.path.basename(src)
            if not IMAGE_RE.match(name):
                raise ValueError(f"invalid image name: {name!r}")
            shutil.copy2(src, os.path.join(stage, name))
            entry["image"] = name
        out_posts.append(entry)
    doc = {"version": SCAN_VERSION, "kind": kind, "title": title or default_title,
           "subtitle": subtitle if subtitle is not None else default_sub, "date": day,
           "created": datetime.now().astimezone().isoformat(timespec="seconds"), "posts": out_posts}
    if ai:
        doc["ai"] = ai
    with open(os.path.join(stage, SCAN_FILE), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    old = os.path.join(root, f".{kind}.old")
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(final):
        os.replace(final, old)
    os.replace(stage, final)
    shutil.rmtree(old, ignore_errors=True)
    return final


# ── backfill from run logs ─────────────────────────────────────────────────────
_PREVIEW_RE = re.compile(r"--- 📝 PREVIEW OF POST ---\n(.*?)\n-{20,}\n", re.DOTALL)
_ATTACH_RE = re.compile(r"\n?\[Attach: ([^\]\n]*)\]\s*$")


def parse_log(text):
    """The X thread a run printed before posting: (main.py args, [posts]) or None.

    Each post is {"text", "ticker"?, "image"?} with ``image`` the tmp/ path it attached."""
    m = _PREVIEW_RE.search(text)
    if not m:
        return None
    args = re.search(r"Executing: \S+ main\.py (.*)", text)
    body = m.group(1)
    blocks = re.split(r"\n(?=REPLY TWEET \([A-Z0-9.\-]+\):\n|FOOTER:\n)", body)
    posts = []
    for b in blocks:
        head, _, rest = b.partition("\n")
        ticker = re.match(r"REPLY TWEET \(([A-Z0-9.\-]+)\):", head)
        if not (ticker or head in ("MAIN TWEET:", "FOOTER:")):
            continue
        post = {}
        a = _ATTACH_RE.search(rest)
        if a:
            rest = rest[:a.start()]
            post["image"] = a.group(1)
        post["text"] = rest.strip()
        if ticker:
            post["ticker"] = ticker.group(1)
        posts.append(post)
    return (args.group(1).split() if args else []), posts


def _kind_from_args(argv):
    weekly, portfolio = "--weekly" in argv, "--portfolio" in argv
    src = argv[argv.index("--src") + 1] if "--src" in argv else "database/mag7.txt"
    stem = os.path.basename(src).lower()
    idx = next((v for k, v in (("mag7", "MAGS"), ("mags", "MAGS"), ("nasdaq", "QQQ"), ("qqq", "QQQ"),
                               ("schd", "SCHD"), ("smh", "SMH"), ("igv", "IGV")) if k in stem), "SPY")
    if weekly or portfolio:
        idx = "SPY"
    return scan_kind(idx, src, weekly, portfolio), idx


def backfill(log_path, root=SCAN_DIR, repo="."):
    """Rebuilds one scan folder from a run log and the charts its run pushed to git."""
    with open(log_path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    parsed = parse_log(text)
    if not parsed:
        raise ValueError(f"{log_path}: no X thread preview in this log")
    argv, posts = parsed
    kind, idx = _kind_from_args(argv)
    d = re.search(r"(\d{4})(\d{2})(\d{2})", os.path.basename(log_path))
    day = f"{d.group(1)}-{d.group(2)}-{d.group(3)}"
    sha = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%H", f"--grep=update chart images on {day} scan"],
                         capture_output=True, text=True, check=True).stdout.strip()
    stage = os.path.join(root, f".{kind}.backfill")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    try:
        for p in posts:
            src = p.pop("image", None)
            if not src:
                continue
            if "ticker" in p:
                name = os.path.basename(src)
            else:
                name = "portfolio_allocation.png" if kind == "portfolio" else f"{idx.lower()}_benchmark.png"
            blob = subprocess.run(["git", "-C", repo, "show", f"{sha}:images/{name}"], capture_output=True) if sha else None
            if blob is not None and blob.returncode == 0 and blob.stdout:
                p["image"] = os.path.join(stage, name)
                p["name"] = name
                with open(p["image"], "wb") as out:
                    out.write(blob.stdout)
        # AI overview: the logged sentiment, and each reply minus its "$TICKER ..." header line
        sent = re.search(r"^📰 Sentiment: (.*)$", text, re.MULTILINE)
        ai = {"sentiment": sent.group(1).strip() if sent else "",
              "tickers": {p["ticker"]: p["text"].split("\n\n", 1)[-1].strip() for p in posts if "ticker" in p}}
        return save_scan(root, kind, posts, day=day, ai=ai)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Rebuild scan archive folders from run_lynch.sh logs")
    ap.add_argument("--backfill", nargs="+", required=True, metavar="LOG")
    ap.add_argument("--root", default=SCAN_DIR)
    a = ap.parse_args()
    for log in a.backfill:
        print(f"{log} → {backfill(log, a.root)}")
