"""Company-name search: "micro" or "coca cola" → the ticker, for the search box's suggestions.

The names are the SEC's ticker list (``company_tickers.json``, the file the engine already reads for CIKs):
~10,000 US-listed companies, each with its ticker(s), ordered by market value, which ranks the matches. It
is read once a week into ``<cache_dir>/sec_tickers.json``, in the background: until it has loaded, suggestions
are empty and the box takes tickers as before, and a failed download keeps the last copy (retried in an hour).

A name always maps to a ticker, and the page looks the ticker up the usual way (``?t=``), so a ticker already
analysed today comes straight from the day's cache: a suggestion is marked ``cached`` and shows Yahoo's name
for it. Ranking (``search``):

1. the exact ticker (META);
2. the whole name, or its leading words ("apple", "coca cola"); over an exact ticker too when that company is
   far larger ("ford" → F, not FORD, Forward Industries);
3. prefixes (ticker, name, each word of the name; spaces ignored: "jp morgan"), by market value;
4. the query anywhere in the name, by market value;
5. a ticker-shaped query that is no listed ticker, as itself: last, or first when nothing else matches or it
   carries an exchange suffix (SHOP.TO).

One suggestion per company (GOOGL for Alphabet, unless GOOG is typed). Enter takes the first suggestion.
"""
import json
import os
import re
import threading
import time

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_HEADERS = {"User-Agent": "LynchPin/1.0 (contact@example.com)"}  # the SEC asks for one (engine's too)
MAX_AGE = 7 * 86400     # the list changes with listings and delistings: weekly is plenty
RETRY_S = 3600
LIMIT = 8
MUCH_LARGER = 10        # rank ratio over which a full-name match beats an exact ticker ("ford")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Words that say what kind of company it is, not which: left out of matching ("apple" = "Apple Inc.")
_FILLER = {"inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd", "limited", "plc",
           "llc", "lp", "the", "holdings", "holding", "group", "sa", "nv", "ag", "se", "class", "com", "de", "del"}
_KEEP_UPPER = {"LLC", "LP", "PLC", "NV", "SA", "AG", "SE", "ADR", "REIT", "ETF", "II", "III", "IV", "USA", "US",
               "AI", "IBM", "HP", "ASML", "AMD", "SPDR", "CVS", "UPS", "DBA", "NXP", "RTX", "AES", "PPG", "CSX",
               "JPMORGAN", "TJX", "PG&E", "AT&T", "BJ'S", "MSCI", "KLA", "SBA", "CBRE", "HCA", "IQVIA", "CME"}
_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text):
    return _WORD_RE.findall((text or "").lower().replace("&", " and ").replace("'", ""))


def _key(text):
    """The words that identify a company, without the filler (``_FILLER``)."""
    return [w for w in _words(text) if w not in _FILLER]


_STATE_TAG_RE = re.compile(r"\s*/[A-Za-z]{2,4}/?\s*$")  # "Applied Materials Inc /de", "Bank Of Montreal /can/"
_SPECIAL = {"JPMORGAN": "JPMorgan"}


def pretty(title):
    """SEC titles are often in capitals ("MICROSOFT CORP"): title-case those, keep acronyms; drop state tags."""
    title = _STATE_TAG_RE.sub("", title or "").strip()
    letters = [c for c in title if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) < 0.8 * len(letters):  # mostly capitals: "... PARTNERS plc"
        return title
    out = []
    for w in title.split():
        if not w.isupper():  # a word already in mixed or lower case ("plc", "nVent") stays as written
            out.append(w)
            continue
        out.append(_SPECIAL[w] if w in _SPECIAL else w if w in _KEEP_UPPER or (len(w) <= 3 and not re.search(r"[AEIOUY]", w)) else "-".join(part.capitalize() for part in w.split("-")))
    return " ".join(out)


class NameIndex:
    """The SEC's companies, searchable by ticker or name. Thread-safe; loads in the background."""

    def __init__(self, cache_dir, fetch=None, clock=time.time):
        self.path = os.path.join(cache_dir, "sec_tickers.json")
        self._fetch = fetch or _fetch_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = []      # (rank, ticker, name, cik, key words, joined key)
        self._tickers = {}      # ticker -> entry
        self._loaded_at = 0.0
        self._failed_at = 0.0
        self._loading = False
        self._read_file = False

    # ── loading ──────────────────────────────────────────────────────────────
    def load(self, rows, loaded_at=None):
        """``rows``: the SEC file's values ({cik_str, ticker, title}), best-ranked first."""
        entries, tickers = [], {}
        for rank, r in enumerate(rows):
            t = str(r.get("ticker") or "").upper().strip()
            title = str(r.get("title") or "").strip()
            if not TICKER_RE.match(t) or not title or t in tickers:
                continue
            words = _key(title) or _words(title)
            e = (rank, t, pretty(title), r.get("cik_str"), words, "".join(words))
            entries.append(e)
            tickers[t] = e
        with self._lock:
            self._entries, self._tickers = entries, tickers
            self._loaded_at = loaded_at if loaded_at is not None else self._clock()
        return len(entries)

    def _refresh(self):
        try:
            rows = self._fetch()
            if rows:
                tmp = self.path + ".tmp"  # the file first: once the index answers, a restart finds it on disk
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(rows, f)
                os.replace(tmp, self.path)
                self.load(rows)
                print(f"🔎 Company names: {len(self._entries)} from the SEC", flush=True)
        except Exception as e:  # a failed download keeps the last list; retried in an hour
            self._failed_at = self._clock()
            print(f"⚠️  Company names: SEC download failed ({type(e).__name__}: {e})", flush=True)
        finally:
            self._loading = False

    def ensure(self):
        """Loads the cached list (once), and refreshes a missing or week-old one in the background."""
        if not self._entries and not self._read_file:
            self._read_file = True
            try:
                with open(self.path, encoding="utf-8") as f:
                    rows = json.load(f)
                if rows:
                    self.load(rows, os.path.getmtime(self.path))
            except (OSError, ValueError):
                pass  # none yet, or unreadable: downloaded below
        with self._lock:
            now = self._clock()
            if now - self._loaded_at <= MAX_AGE or self._loading or now - self._failed_at < RETRY_S:
                return self
            self._loading = True
        threading.Thread(target=self._refresh, name="lynch-names", daemon=True).start()
        return self

    start = ensure

    @property
    def size(self):
        return len(self._entries)

    # ── search ───────────────────────────────────────────────────────────────
    def search(self, query, limit=LIMIT, cached=None):
        """Up to ``limit`` suggestions: [{ticker, name, match, cached?}], best first (see the module doc).
        ``cached(ticker)`` → today's analysis, or None: its name replaces the SEC's and it is marked cached."""
        self.ensure()
        q = (query or "").strip()
        if not q or len(q) > 60:
            return []
        qt = q.upper().lstrip("$")
        qwords = _key(q) or _words(q)
        qjoined = "".join(qwords)  # spaces dropped: "jp morgan" finds "JPMORGAN", "coca cola" "COCA-COLA"
        with self._lock:
            entries, tickers = self._entries, self._tickers
        exact = tickers.get(qt) or (tickers.get(qt.replace(".", "-")) if "." in qt else None)

        full, partial, inner = [], [], []
        if qwords:
            for e in entries:
                rank, t, _, _, words, joined = e
                if e is exact:
                    continue
                if joined == qjoined or words[:len(qwords)] == qwords:
                    full.append(e)
                elif t.startswith(qt) or joined.startswith(qjoined) or all(
                        any(w.startswith(qw) for w in words) for qw in qwords):
                    partial.append(e)
                elif len(qjoined) >= 3 and qjoined in joined:
                    inner.append(e)

        ordered = []
        if exact is not None:
            if full and full[0][0] * MUCH_LARGER < exact[0]:  # a far larger company by that name ("ford")
                ordered.append(full.pop(0))
            ordered.append(exact)
        ordered += full
        ordered += partial + inner
        # A ticker-shaped query that is no listed ticker: offered as itself, last (a fund, a foreign listing),
        # or first when it is no word of any name ("ZZZZ") or carries an exchange suffix ("SHOP.TO")
        raw = None
        if exact is None and not full and TICKER_RE.match(qt):
            raw = {"ticker": qt, "name": None, "match": "ticker"}

        if raw and cached and cached(qt):
            raw = dict(raw, cached=True, name=cached(qt).get("name"))
        raw_first = raw is not None and ("." in qt or not ordered)
        out, seen = ([raw] if raw_first else []), ({qt} if raw else set())
        ciks = set()
        for e in ordered:
            rank, t, name, cik, _, _ = e
            if t in seen or (cik in ciks and e is not exact):
                continue
            seen.add(t)
            ciks.add(cik)
            item = {"ticker": t, "name": name,
                    "match": "ticker" if e is exact else "name"}
            hit = cached(t) if cached else None
            if hit:
                item["cached"] = True
                item["name"] = hit.get("name") or name
            out.append(item)
            if len(out) >= limit:
                break
        if raw is not None and not raw_first:
            out = out[:limit - 1] + [raw]
        return out


def _fetch_sec():
    from engine.http_session import browser_session
    s = browser_session(timeout=15, verify=True)
    r = s.get(SEC_URL, headers=SEC_HEADERS)
    if r.status_code != 200:
        raise OSError(f"HTTP {r.status_code}")
    data = r.json()
    rows = list(data.values()) if isinstance(data, dict) else data
    return [{"cik_str": d.get("cik_str"), "ticker": d.get("ticker"), "title": d.get("title")} for d in rows]
