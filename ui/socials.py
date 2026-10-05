"""Socials: the latest posts on X, read with the posting tokens (next to the profile links on the page).

X reads are metered, so a background thread reads X once a day at the read time (9 AM Pacific by default:
before the 1 PM scan posts, so the widget shows what else is on X instead of repeating the Latest
scans widget; on startup if that day's read was missed), keeps it on disk, and everyone is served
from there: one X request of 5 posts a day. Page views never trigger a read. After a failed fetch the
last good posts stay up and the next try waits an hour. Post images are downloaded and re-encoded
as JPEG here, because the page may only load images from this server (CSP img-src 'self').

Tokens come from venv/bin/activate (where main.py's posting tokens live), else from the environment.
They are never sent to the page.
"""
import io
import json
import os
import re
import shlex
import sys
import tempfile
import threading
import time

import datetime

HANDLE = "lynch_pin_quant"  # the profile links (X, Instagram, Threads) are in static/index.html
TOKEN_NAMES = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")
POSTS = 5
RETRY_S = 3600
READ_AT = "09:00"               # daily X read time...
READ_TZ = "America/Los_Angeles"  # ...in this time zone (PDT / PST), whatever the server's clock says


def parse_hhmm(value):
    """'09:00' / '6:30' / '9' -> (hour, minute); ValueError otherwise."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*$", str(value))
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ValueError(f"expected HH:MM, got {value!r}")
    return int(m.group(1)), int(m.group(2) or 0)


def read_day(read_at=READ_AT, now=None, tz=READ_TZ):
    """The X-read 'day' a moment belongs to: it rolls over at ``read_at`` in time zone ``tz``."""
    from zoneinfo import ZoneInfo
    h, m = parse_hhmm(read_at)
    now = now or datetime.datetime.now(ZoneInfo(tz))
    if (now.hour, now.minute) < (h, m):
        now -= datetime.timedelta(days=1)
    return now.date().isoformat()
IMG_WIDTH = 640
MAX_IMG_BYTES = 8 * 1024 * 1024
IMG_RE = re.compile(r"^x_[0-9]{1,30}\.jpg$")


def read_tokens(env_file):
    """X posting tokens: ``export NAME=value`` lines of ``env_file`` first, then the environment."""
    out = {}
    try:
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^\s*export\s+([A-Z_]+)=(.*)$", line)
                if m and m.group(1) in TOKEN_NAMES:
                    try:
                        val = shlex.split(m.group(2))
                    except ValueError:
                        continue
                    if val and val[0]:
                        out[m.group(1)] = val[0]
    except OSError:
        pass
    for k in TOKEN_NAMES:
        if not out.get(k) and os.environ.get(k):
            out[k] = os.environ[k]
    return out


# ── network (swapped out in tests) ────────────────────────────────────────────
def fetch_x(tok, user_id=None):
    """(user_id, [post]) for the account's latest posts, newest first."""
    import tweepy
    c = tweepy.Client(consumer_key=tok["X_API_KEY"], consumer_secret=tok["X_API_SECRET"],
                      access_token=tok["X_ACCESS_TOKEN"], access_token_secret=tok["X_ACCESS_SECRET"])
    if not user_id:
        user_id = str(c.get_me(user_auth=True).data.id)
    r = c.get_users_tweets(user_id, max_results=POSTS, exclude=["retweets"], user_auth=True,
                           tweet_fields=["created_at", "public_metrics"], expansions=["attachments.media_keys"],
                           media_fields=["url", "preview_image_url", "type"])
    media = {m.media_key: (m.url or m.preview_image_url) for m in (r.includes or {}).get("media", [])}
    posts = []
    for t in r.data or []:
        keys = (t.attachments or {}).get("media_keys") or []
        pm = t.public_metrics or {}
        posts.append({"id": str(t.id), "text": t.text, "time": t.created_at.isoformat() if t.created_at else "",
                      "url": f"https://x.com/{HANDLE}/status/{t.id}",
                      "image_src": next((media[k] for k in keys if media.get(k)), None),
                      "likes": pm.get("like_count"), "replies": pm.get("reply_count"),
                      "reposts": pm.get("retweet_count"), "views": pm.get("impression_count")})
    return user_id, posts


def download_jpeg(src, dest):
    """Downloads an image and re-encodes it as a small JPEG (never serves remote bytes as-is)."""
    import requests
    from PIL import Image
    r = requests.get(src, timeout=20, stream=True)
    r.raise_for_status()
    if not r.headers.get("Content-Type", "").startswith("image/"):
        raise ValueError("not an image")
    data = r.raw.read(MAX_IMG_BYTES + 1, decode_content=True)
    if len(data) > MAX_IMG_BYTES:
        raise ValueError("image too large")
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((IMG_WIDTH, IMG_WIDTH * 2), Image.LANCZOS)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".part")
        with os.fdopen(fd, "wb") as f:
            im.save(f, "JPEG", quality=82, optimize=True, progressive=True)
    os.replace(tmp, dest)


class SocialFeed:
    def __init__(self, cache_dir, env_file, read_at=READ_AT, tz=READ_TZ, fetch=fetch_x, download=download_jpeg):
        from zoneinfo import ZoneInfo
        parse_hhmm(read_at)
        ZoneInfo(tz)  # unknown zone: fail at startup, not at 9 AM
        self.read_at, self.tz = read_at, tz
        self.dir = os.path.join(cache_dir, "socials")
        self.path = os.path.join(self.dir, "feed.json")
        self.env_file = env_file
        self._fetch, self._download = fetch, download
        self._lock = threading.Lock()
        self._running = False
        self._failed_at = 0.0
        self._feed = self._load()
        self._stop = threading.Event()

    def start(self, every_s=60):
        """Checks every ``every_s`` seconds (cheap: no network) whether the daily read is due."""
        def loop():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(every_s)
        threading.Thread(target=loop, name="socials", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def tick(self):
        """Reads X if the daily read time has passed since the last read (retried hourly on failure)."""
        with self._lock:
            if not self._stale() or self._running:
                return False
            self._running = True
        self._refresh_bg()
        return True

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                feed = json.load(f)
            return feed if isinstance(feed, dict) else {}
        except (OSError, ValueError):
            return {}

    def _stale(self):
        return self._feed.get("day") != read_day(self.read_at, tz=self.tz) and time.time() - self._failed_at > RETRY_S

    def snapshot(self):
        """What the page shows (never calls X)."""
        with self._lock:
            feed, running = self._feed, self._running
        posts = []
        for p in feed.get("x") or []:
            q = {k: p[k] for k in ("text", "time", "url", "likes", "replies", "reposts", "views") if p.get(k) is not None}
            if p.get("image"):
                q["image"] = f"/social/{p['image']}"
            posts.append(q)
        return {"handle": HANDLE, "x": posts, "refreshing": running, "fetched": feed.get("fetched")}

    def _refresh_bg(self):
        try:
            self.refresh()
        except Exception as e:  # keep the last good posts; try again in an hour
            sys.stderr.write(f"⚠️  socials: X refresh failed: {type(e).__name__}: {str(e)[:160]}\n")
            self._failed_at = time.time()
        finally:
            with self._lock:
                self._running = False

    def refresh(self):
        tok = read_tokens(self.env_file)
        feed = {"day": read_day(self.read_at, tz=self.tz), "fetched": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "x_user": self._feed.get("x_user"), "x": self._feed.get("x") or []}
        if all(tok.get(k) for k in TOKEN_NAMES):  # no tokens: profile links only
            feed["x_user"], feed["x"] = self._fetch(tok, feed["x_user"])
        os.makedirs(self.dir, exist_ok=True)
        keep = set()
        for p in feed["x"]:
            name = f"x_{p.get('id', '')}.jpg"
            if not (p.get("image_src") and IMG_RE.match(name)):
                continue
            dest = os.path.join(self.dir, name)
            if not os.path.exists(dest):
                try:
                    self._download(p["image_src"], dest)
                except Exception as e:  # the post still shows, without its picture
                    sys.stderr.write(f"⚠️  socials: image for post {p.get('id')}: {type(e).__name__}\n")
            if os.path.exists(dest):
                p["image"] = name
                keep.add(name)
        for old in os.listdir(self.dir):
            if old.endswith((".jpg", ".part")) and old not in keep:
                try:
                    os.remove(os.path.join(self.dir, old))
                except OSError:
                    pass
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".part")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(feed, f, ensure_ascii=False)
        os.replace(tmp, self.path)
        with self._lock:
            self._feed = feed
        return feed

    def image_path(self, name):
        """A downloaded post image, only if the current posts use it."""
        if not IMG_RE.match(name or ""):
            return None
        path = os.path.join(self.dir, name)
        return path if name in {p.get("image") for p in self._feed.get("x") or []} and os.path.isfile(path) else None
