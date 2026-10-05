"""Latest scans: the daily run_lynch.sh threads archived by main.py in scans/<kind>/scan.json.

The index is cached in memory. run_lynch.sh starts at 1 PM and its thread is archived by ~3 PM, so a
cached index is re-read once a day after 3 PM, and earlier whenever a scan.json changes on disk
(checked at most every ``check_s`` seconds). Only charts listed in a scan are served.
"""
import datetime
import json
import os
import re
import threading
import time

from social.scan_archive import IMAGE_RE, KIND_RE, SCAN_FILE, SCAN_KINDS

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROLLOVER_HOUR = 15  # the daily scan is done by 3 PM local time
MAX_POSTS = 40
MAX_TEXT = 6000
PREVIEW_WIDTH = 1100


def scan_day(now=None):
    """The scan 'day' a moment belongs to: it rolls over at 3 PM, when the day's scan is in."""
    now = now or datetime.datetime.now()
    if now.hour < ROLLOVER_HOUR:
        now -= datetime.timedelta(days=1)
    return now.date().isoformat()


def _load(path, kind):
    """A validated scan dict, or None if the file is missing or malformed."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not DATE_RE.match(str(doc.get("date", ""))):
        return None
    posts = []
    for p in doc.get("posts") or []:
        if not isinstance(p, dict) or not isinstance(p.get("text", ""), str):
            continue
        post = {"text": p.get("text", "")[:MAX_TEXT]}
        if isinstance(p.get("ticker"), str) and re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", p["ticker"]):
            post["ticker"] = p["ticker"]
        if isinstance(p.get("image"), str) and IMAGE_RE.match(p["image"]):
            post["image"] = p["image"]
        posts.append(post)
        if len(posts) >= MAX_POSTS:
            break
    if not posts:
        return None
    title, subtitle = SCAN_KINDS.get(kind, (kind.upper(), ""))
    ai, raw = {}, doc.get("ai") if isinstance(doc.get("ai"), dict) else {}
    for k in ("sentiment", "portfolio"):
        if isinstance(raw.get(k), str) and raw[k].strip():
            ai[k] = raw[k][:MAX_TEXT]
    if isinstance(raw.get("tickers"), dict):
        ai["tickers"] = {t: v[:MAX_TEXT] for t, v in list(raw["tickers"].items())[:MAX_POSTS]
                         if isinstance(t, str) and re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", t) and isinstance(v, str)}
    return {"ai": ai, "kind": kind, "title": str(doc.get("title") or title)[:40],
            "subtitle": str(doc.get("subtitle") if doc.get("subtitle") is not None else subtitle)[:80],
            "date": doc["date"], "created": str(doc.get("created") or "")[:40], "posts": posts}


class ScanArchive:
    def __init__(self, root, cache_dir, limit=7, check_s=60):
        self.root = root
        self.cache_dir = cache_dir
        self.limit = limit
        self.check_s = check_s
        self._lock = threading.Lock()
        self._sig = None        # (kind, mtime_ns, size) of every scan.json at the last load
        self._scans = {}        # kind -> scan dict (+ "_v": cache-busting version per image)
        self._checked = 0.0
        self._day = None

    # ── index ─────────────────────────────────────────────────────────────────
    def _signature(self):
        sig = []
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return ()
        for kind in names:
            if not KIND_RE.match(kind):
                continue
            try:
                st = os.stat(os.path.join(self.root, kind, SCAN_FILE))
            except OSError:
                continue
            sig.append((kind, st.st_mtime_ns, st.st_size))
        return tuple(sig)

    def _fresh(self, force=False):
        now = time.monotonic()
        with self._lock:
            day = scan_day()
            if not force and day == self._day and now - self._checked < self.check_s:
                return self._scans
            self._checked, self._day = now, day
            sig = self._signature()
            if sig != self._sig:
                scans = {}
                for kind, mtime_ns, _ in sig:
                    doc = _load(os.path.join(self.root, kind, SCAN_FILE), kind)
                    if doc:
                        doc["_v"] = format(mtime_ns // 1_000_000, "x")
                        scans[kind] = doc
                self._scans, self._sig = scans, sig
            return self._scans

    def refresh(self):
        self._fresh(force=True)

    def _ordered(self):
        scans = sorted(self._fresh().values(), key=lambda d: (d["date"], d["created"]), reverse=True)
        return scans[:self.limit]

    # ── API payloads ──────────────────────────────────────────────────────────
    def _img(self, doc, name):
        stem = name[:-4]
        return {"src": f"/scans/{doc['kind']}/{stem}.jpg?v={doc['_v']}",
                "full": f"/scans/{doc['kind']}/{name}?v={doc['_v']}"}

    def summaries(self):
        out = []
        for d in self._ordered():
            head = d["posts"][0]
            out.append({"kind": d["kind"], "title": d["title"], "subtitle": d["subtitle"], "date": d["date"],
                        "text": head["text"], "image": self._img(d, head["image"]) if head.get("image") else None,
                        "tickers": [p["ticker"] for p in d["posts"] if p.get("ticker")],
                        "posts": len(d["posts"])})
        return {"scans": out, "day": scan_day(), "rollover_hour": ROLLOVER_HOUR}

    def get(self, kind):
        if not KIND_RE.match(kind or ""):
            return None
        d = self._fresh().get(kind)
        if d is None:
            return None
        posts = []
        for p in d["posts"]:
            q = {k: v for k, v in p.items() if k != "image"}
            if p.get("image"):
                q["image"] = self._img(d, p["image"])
            posts.append(q)
        return dict({k: d[k] for k in ("kind", "title", "subtitle", "date", "created", "ai")}, posts=posts)

    # ── files ─────────────────────────────────────────────────────────────────
    def image_path(self, kind, name):
        """Path of a chart a scan lists: NAME.png as archived, or NAME.jpg = a smaller JPEG copy."""
        if not KIND_RE.match(kind or "") or not name.endswith((".png", ".jpg")):
            return None
        png = name[:-4] + ".png"
        d = self._fresh().get(kind)
        if d is None or not IMAGE_RE.match(png) or png not in {p.get("image") for p in d["posts"]}:
            return None
        path = os.path.join(self.root, kind, png)
        if name.endswith(".png"):
            return path if os.path.isfile(path) else None
        from ui.imaging import jpeg_preview
        return jpeg_preview(path, os.path.join(self.cache_dir, "scans", kind), PREVIEW_WIDTH)
