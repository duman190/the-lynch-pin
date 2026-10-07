"""Gemini offload for the portal's AI overview: the request budget and a streaming client.

Used above the local model's low watermark (ui/inference_queue.py). The client is ``LocalLLMClient`` pointed at Gemini's
OpenAI-compatible endpoint, so streaming, the TTFT / tok/s meter, cancellation and the section parser are the same
code as for LM Studio.

Free-tier limits, measured on 2026-10-07 with the portal prompt:
- 15 requests a minute per project per model. A burst of 25 got HTTP 429 naming the quota
  ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier``, ``quotaValue 15`` for gemini-3.5-flash-lite, which
  gemini-flash-lite-latest pointed to. Requests Google turned away with 503 ("high demand") counted too: 17 of the
  25 got past the quota check, 6 of those answered 503.
- Requests per day: not published (Google points to AI Studio); they reset at midnight Pacific. The portal keeps to
  ``rpd`` (975) per Pacific day so the daily scans' backup tier (engine/ai_research.py, same model) has room.

So every request sent counts, answered or not, in SQLite (``<cache_dir>/gemini_budget.sqlite3``) so a restart can't
reset the day. A 429 closes Gemini for the delay Google asks for (the whole day when the per-day quota ran out).

Thinking: gemini-3.5-flash-lite does not think, and the API refuses any attempt to turn thinking off (400
"invalid argument" for ``reasoning_effort: "none"`` and for ``thinking_budget: 0``); "minimal" is accepted but
turned a ~1 s first token into ~7 s. So the client sends no reasoning field: the model's default writes no
reasoning tokens. (Sending "none" once to find out cost one of the 15 requests a minute: in the 2026-10-07
benchmark the minute's 15th request then got a 429.)
"""
import collections
import dataclasses
import datetime as _dt
import math
import os
import re
import sqlite3
import threading
import time

import requests

from ui.llm import LLMError, LLMUnavailable, LocalLLMClient

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
PACIFIC = "America/Los_Angeles"  # Google resets requests-per-day at midnight Pacific
MAX_TOKENS = 2048                # a reply is ~300 tokens and Flash-Lite writes no reasoning tokens


def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # no tz database: UTC-8 is close enough to midnight Pacific
        return _dt.timezone(_dt.timedelta(hours=-8))


class GeminiBudget:
    """Requests sent to Gemini: at most ``rpm`` in any rolling minute and ``rpd`` per Pacific day, none during a
    429 cooldown. ``try_acquire`` reserves a request before it is sent (the inference queue admits on it)."""
    NO_HINT_COOLDOWN = 30.0  # seconds Gemini stays closed after a 429 that names no delay
    WINDOW = 60.0

    def __init__(self, path, rpm=15, rpd=975, clock=time.time, tz=PACIFIC):
        self.path, self.rpm, self.rpd, self._clock, self._tz = path, max(0, int(rpm)), max(0, int(rpd)), clock, _zone(tz)
        self._lock = threading.Lock()
        self._minute = collections.deque()  # send times in the last minute
        self._cooldown_until = 0.0
        self._day, self._used = None, 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        now = self._clock()
        try:
            with self._connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, day TEXT NOT NULL, symbol TEXT)")
                db.execute("DELETE FROM calls WHERE ts < ?", (now - 3 * 86400,))
                self._day = self._date(now)
                (self._used,) = db.execute("SELECT COUNT(*) FROM calls WHERE day = ?", (self._day,)).fetchone()
                self._minute.extend(ts for (ts,) in db.execute(
                    "SELECT ts FROM calls WHERE ts > ? ORDER BY ts", (now - self.WINDOW,)))
        except sqlite3.Error as e:
            print(f"⚠️  Gemini budget: {e} — counting in memory only", flush=True)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def _date(self, ts):
        return _dt.datetime.fromtimestamp(ts, self._tz).date().isoformat()

    def _roll(self, now):
        """Lock held: a new Pacific day starts from zero; the minute window drops what is older than a minute."""
        day = self._date(now)
        if day != self._day:
            self._day, self._used = day, 0
        while self._minute and self._minute[0] <= now - self.WINDOW:
            self._minute.popleft()

    def try_acquire(self, symbol=None):
        """Reserve one request: False when the minute or the day is full or Google asked us to wait."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            if now < self._cooldown_until or self._used >= self.rpd or len(self._minute) >= self.rpm:
                return False
            self._record(now, symbol)
            return True

    def _record(self, now, symbol):
        self._minute.append(now)
        self._used += 1
        try:
            with self._connect() as db:
                db.execute("INSERT INTO calls VALUES (?, ?, ?)", (now, self._day, symbol))
        except sqlite3.Error:
            pass  # still counted in memory

    def cooldown(self, seconds, whole_day=False):
        """Google answered 429: no request until ``seconds`` from now (or the next Pacific midnight)."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            if whole_day:
                self._used = max(self._used, self.rpd)
            self._cooldown_until = max(self._cooldown_until, now + max(1.0, seconds))

    def _until_midnight(self, now):
        local = _dt.datetime.fromtimestamp(now, self._tz)
        nxt = _dt.datetime.combine(local.date() + _dt.timedelta(days=1), _dt.time(), tzinfo=self._tz)
        return max(1.0, nxt.timestamp() - now)

    def retry_after(self):
        """Seconds until a request may be sent again."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            waits = [self._cooldown_until - now]
            if self._used >= self.rpd:
                waits.append(self._until_midnight(now))
            if len(self._minute) >= self.rpm and self._minute:
                waits.append(self._minute[0] + self.WINDOW - now)
            return max(0, int(math.ceil(max(waits))))

    def status(self):
        with self._lock:
            now = self._clock()
            self._roll(now)
            return {"rpm": self.rpm, "last_min": len(self._minute), "rpd": self.rpd, "today": self._used,
                    "day": self._day, "cooldown_s": max(0, int(math.ceil(self._cooldown_until - now)))}


def _retry_delay(body):
    """Seconds Google asks us to wait, from a 429 body ("retryDelay": "25s" / "Please retry in 25.7s")."""
    m = re.search(r'retryDelay"?\s*[:=]\s*"?([\d.]+)s', body) or re.search(r"retry in ([\d.]+)\s*s", body)
    return float(m.group(1)) if m else None


class GeminiClient(LocalLLMClient):
    """``LocalLLMClient`` against Gemini's OpenAI-compatible API: same generate() / status() contract, no model
    probe (nothing to load), no system-prompt priming and no reasoning switch (every request counts against the
    quota, a refused one too)."""
    NAME = "Gemini"
    SEND_REASONING_SWITCH = False

    def __init__(self, settings, api_key, budget=None, base_url=GEMINI_URL, session=None, clock=time.monotonic):
        s = dataclasses.replace(settings, llm_base_url=base_url, llm_model=settings.gemini_model, llm_autoload=False,
                                llm_reasoning="off", llm_timeout=settings.gemini_timeout,
                                llm_max_tokens=min(settings.llm_max_tokens, MAX_TOKENS))
        super().__init__(s, session=session, clock=clock)
        self.http.headers["Authorization"] = f"Bearer {api_key}"
        self.budget = budget
        self._symbol = threading.local()

    @classmethod
    def from_settings(cls, settings, api_key=None):
        """The portal's Gemini offload, or None when it is off or GEMINI_API_KEY is not set."""
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not settings.gemini or not key or settings.gemini_rpm <= 0 or settings.gemini_rpd <= 0:
            return None
        budget = GeminiBudget(settings.gemini_budget_path, settings.gemini_rpm, settings.gemini_rpd)
        return cls(settings, key, budget)

    @property
    def model_id(self):
        return self.settings.llm_model

    def _probe(self):
        ctx = self.settings.llm_ctx
        return {"available": True, "model": self.model_id, "model_short": self.model_id, "ctx": ctx,
                "ctx_configured": ctx, "ctx_loaded": ctx, "base_url": self.base}

    def _prime(self, model, system):
        pass  # a priming request would cost one of the 15 a minute

    def _post_chat(self, payload):
        try:
            return self.http.post(f"{self.base}/chat/completions", json=payload, stream=True,
                                  timeout=(5, self.settings.llm_timeout))
        except requests.ConnectionError as e:
            raise LLMUnavailable(f"Gemini not reachable ({type(e).__name__})") from e
        except requests.Timeout as e:
            raise LLMError(f"Gemini timed out after {self.settings.llm_timeout}s") from e

    def _http_error(self, status, body_text, model):
        if status == 429:
            delay = _retry_delay(body_text) or GeminiBudget.NO_HINT_COOLDOWN
            whole_day = "PerDay" in body_text
            if self.budget is not None:
                self.budget.cooldown(delay, whole_day=whole_day)
            raise LLMUnavailable("Gemini's daily quota is used up" if whole_day
                                 else f"Gemini rate limit — retry in {int(math.ceil(delay))}s")
        if status >= 500:
            raise LLMUnavailable(f"Gemini is overloaded (HTTP {status}) — try again shortly")
        raise LLMError(f"Gemini HTTP {status}: {body_text[:300]}")
