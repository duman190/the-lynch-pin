"""Local LLM (LM Studio, OpenAI-compatible) client + the Lynch Pin single-ticker narrative.

The prompt, normalisation and validation are exactly the daily scan's
(``LynchPinResearcher.build_prompt / normalize_narrative / narrative_gaps``, all static — the
researcher itself is never instantiated, so no Gemini/OpenRouter key is needed), and the reply is
parsed the same way ``main.py`` parses it for the X thread.

Degrades quietly: when LM Studio is not running or no model is loaded, ``status()`` reports
``available: False`` with a reason and the portal shows "AI offline" instead of an error.
"""
import math
import re
import threading
import time

import requests

CHARS_PER_TOKEN = 3.0     # conservative for emoji-heavy prompts (English prose is ~4)
PROMPT_MARGIN = 512       # tokens that must remain after the prompt for any reply at all
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_NOT_LOADED = ("model not loaded", "no models loaded", "model_not_found", "not found", "no model")


class LLMUnavailable(Exception):
    """LM Studio unreachable or no usable model (not a failure of this request)."""


class LLMError(Exception):
    pass


def strip_thinking(text):
    """Drops <think>…</think> blocks; a dangling unclosed <think> (truncated reasoning) drops the rest."""
    text = _THINK_RE.sub("", text or "")
    low = text.lower()
    if "<think>" in low:
        text = text[:low.index("<think>")]
    return text.replace("</think>", "").strip()


def estimate_tokens(text):
    return int(math.ceil(len(text or "") / CHARS_PER_TOKEN))


def _short(model_id):
    """'lmstudio-community/qwen3-30b-a3b-GGUF' → 'qwen3-30b-a3b'."""
    s = (model_id or "").split("/")[-1]
    s = re.sub(r"(?i)[-_.](gguf|mlx|awq|gptq)$", "", s)
    return s[:32]


class LocalLLMClient:
    STATUS_TTL = 15.0

    def __init__(self, settings, session=None, clock=time.monotonic):
        self.settings = settings
        self.base = settings.llm_base_url.rstrip("/")
        self.http = session or requests.Session()
        self._clock = clock
        self._lock = threading.Lock()
        self._status = None
        self._status_at = -1e9
        self._autoloaded = False
        self._refreshing = False

    # ── discovery ────────────────────────────────────────────────────────────
    def _probe(self):
        s = self.settings
        try:
            r = self.http.get(f"{self.base}/v1/models", timeout=(2, 2))
        except requests.RequestException as e:
            return {"available": False, "reason": f"LM Studio not reachable at {self.base} ({type(e).__name__})"}
        if r.status_code != 200:
            return {"available": False, "reason": f"{self.base}/v1/models → HTTP {r.status_code}"}
        try:
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except ValueError:
            return {"available": False, "reason": "unexpected /v1/models response"}
        native = self._native_models()
        loaded = [m for m in native if m.get("state") == "loaded" and m.get("type", "llm") in ("llm", "vlm")]
        model, ctx_loaded = None, None
        if s.llm_model:
            model = s.llm_model
            info = next((m for m in native if m.get("id") == model), None)
            if info is None and model not in ids:  # /v1/models alone is authoritative for "downloaded"
                return {"available": False, "reason": f"model '{model}' is not available in LM Studio",
                        "models": ids[:20]}
            if info and info.get("state") == "loaded":
                ctx_loaded = info.get("loaded_context_length")
        elif loaded:
            model, ctx_loaded = loaded[0]["id"], loaded[0].get("loaded_context_length")
        else:
            llms = [m["id"] for m in native if m.get("type", "llm") in ("llm", "vlm")] or \
                   [i for i in ids if "embed" not in i.lower()]
            if not llms:
                return {"available": False, "reason": "no model downloaded/loaded in LM Studio", "models": ids[:20]}
            model = llms[0]  # LM Studio JIT-loads it on the first request
        ctx = s.llm_ctx
        if isinstance(ctx_loaded, int) and ctx_loaded > 0:
            ctx = min(ctx, ctx_loaded)  # the real window of the loaded instance wins
        out = {"available": True, "model": model, "model_short": _short(model), "ctx": ctx,
               "ctx_configured": s.llm_ctx, "ctx_loaded": ctx_loaded, "base_url": self.base}
        if ctx_loaded is None and not s.llm_autoload:
            # LM Studio will JIT-load with the model's *default* context and may silently truncate
            out["warning"] = ("model not loaded yet — the first request JIT-loads it with LM Studio's default "
                              "context; load it with 64K context in LM Studio or start the portal with --llm-autoload")
        return out

    def _native_models(self):
        """LM Studio's native REST API (state / loaded context). Empty list when unsupported."""
        try:
            r = self.http.get(f"{self.base}/api/v0/models", timeout=(2, 2))
            if r.status_code == 200:
                return [m for m in (r.json().get("data") or []) if isinstance(m, dict) and m.get("id")]
        except (requests.RequestException, ValueError):
            pass
        return []

    def status(self, force=False, block=True):
        """Cached LM Studio status. ``block=False`` (used by /api/health) never waits on the network:
        it returns the last known status and refreshes it in the background when stale."""
        with self._lock:
            fresh = self._status is not None and self._clock() - self._status_at < self.STATUS_TTL
            if not force and fresh:
                return dict(self._status)
            if not block:
                if not self._refreshing:
                    self._refreshing = True
                    threading.Thread(target=self._refresh_bg, name="llm-probe", daemon=True).start()
                return dict(self._status) if self._status is not None else {
                    "available": False, "checking": True, "reason": "checking local model…",
                    "ctx": self.settings.llm_ctx, "model": self.settings.llm_model or None}
        return self._refresh()

    def _refresh_bg(self):
        try:
            self._refresh()
        finally:
            with self._lock:
                self._refreshing = False

    def _refresh(self):
        st = self._probe()
        st.setdefault("base_url", self.base)
        st.setdefault("ctx", self.settings.llm_ctx)
        st.setdefault("model", self.settings.llm_model or None)
        with self._lock:
            self._status, self._status_at = st, self._clock()
        return dict(st)

    def _mark(self, available, reason=None, model=None):
        with self._lock:
            st = dict(self._status or {"ctx": self.settings.llm_ctx})
            st["available"] = available
            if reason:
                st["reason"] = reason
            if model:
                st["model"], st["model_short"] = model, _short(model)
            self._status, self._status_at = st, self._clock()

    def _autoload(self, model):
        """Best effort: ask LM Studio to load ``model`` with our context length (once)."""
        if self._autoloaded or not self.settings.llm_autoload:
            return
        self._autoloaded = True
        try:
            self.http.post(f"{self.base}/api/v1/models/load",
                           json={"model": model, "context_length": self.settings.llm_ctx}, timeout=(3, 300))
        except requests.RequestException:
            pass

    # ── generation ───────────────────────────────────────────────────────────
    def generate(self, prompt, max_tokens=None):
        """Returns ``(text, meta)``. Raises LLMUnavailable / LLMError."""
        st = self.status()
        if not st.get("available"):
            raise LLMUnavailable(st.get("reason") or "local model unavailable")
        model, ctx = st["model"], int(st.get("ctx") or self.settings.llm_ctx)
        est = estimate_tokens(prompt)
        if est + PROMPT_MARGIN > ctx:
            raise LLMError(f"prompt (~{est} tokens) does not fit the {ctx}-token context window")
        budget = max(256, min(max_tokens or self.settings.llm_max_tokens, ctx - est - 256))
        self._autoload(model)
        t0 = time.monotonic()
        try:
            r = self.http.post(f"{self.base}/v1/chat/completions", json={
                "model": model, "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.6, "max_tokens": budget, "stream": False,
            }, timeout=(3, self.settings.llm_timeout))
        except requests.ConnectionError as e:
            self._mark(False, f"LM Studio not reachable at {self.base}")
            raise LLMUnavailable(f"LM Studio not reachable at {self.base}") from e
        except requests.Timeout as e:
            raise LLMError(f"local model timed out after {self.settings.llm_timeout}s") from e
        body_text = r.text[:400]
        if r.status_code != 200:
            if r.status_code in (400, 404) and any(k in body_text.lower() for k in _NOT_LOADED):
                self._mark(False, f"model '{model}' is not loaded in LM Studio")
                raise LLMUnavailable(f"model '{model}' is not loaded in LM Studio")
            raise LLMError(f"LM Studio HTTP {r.status_code}: {body_text}")
        try:
            body = r.json()
            choice = (body.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
        except (ValueError, AttributeError, IndexError) as e:
            raise LLMError("unexpected chat completion response") from e
        self._mark(True, model=model)
        if st.get("ctx_loaded") is None:
            with self._lock:  # JIT-loaded just now: re-probe so the real context window is used next
                self._status_at = -1e9
        meta = {"model": body.get("model") or model, "finish_reason": choice.get("finish_reason"),
                "max_tokens": budget, "prompt_tokens_est": est, "ctx": ctx,
                "usage": body.get("usage") or {}, "elapsed_s": round(time.monotonic() - t0, 1)}
        return strip_thinking(content), meta


# ── narrative parsing (mirrors main.py) ─────────────────────────────────────
_SECTION_LABELS = (("overview", "🤖:"), ("reverse_dcf", "📊 Reverse DCF:"), ("stomach_test", "🧪 Stomach Test:"))


def parse_narrative(raw_ai, sym):
    """Splits a normalised batch reply the way main.py does, then into the three labelled parts."""
    sentiment, bulk = "", raw_ai or ""
    m = re.search(r"SENTIMENT:\s*(.+)", bulk)
    if m:
        sentiment = re.sub(r"^SENTIMENT:\s*", "", m.group(1).strip())
        sentiment = re.sub(r"\$([A-Z]+)", r"\1", sentiment)
        bulk = bulk[m.end():].strip()
    bulk = re.sub(r"SECTION \d+[^\n]*\n*", "", bulk)
    pattern = rf"^\${re.escape(sym)}\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
    bm = re.search(pattern, bulk, re.DOTALL | re.MULTILINE)
    block = bm.group(1).strip() if bm else ""
    out = {"sentiment": sentiment, "block": block, "overview": "", "reverse_dcf": "", "stomach_test": ""}
    if block:
        idx = sorted((block.find(lbl), key, lbl) for key, lbl in _SECTION_LABELS if block.find(lbl) >= 0)
        for i, (pos, key, lbl) in enumerate(idx):
            end = idx[i + 1][0] if i + 1 < len(idx) else len(block)
            out[key] = block[pos + len(lbl):end].strip()
        if not idx:
            out["overview"] = block
    return out


def ticker_narrative(client, data, benchmark="SPY"):
    """AI overview for one analysed ticker. Returns a result dict (status done | error)."""
    try:
        from engine.ai_research import LynchPinResearcher as R
    except ImportError as e:  # google-genai missing
        return {"status": "error", "error": f"engine.ai_research unavailable ({e})"}
    inp = data.get("_ai_inputs") or {}
    row = inp.get("row")
    if not row:
        return {"status": "error", "error": "AI overview needs GARP data (no valuation row)"}
    sym = str(row["Ticker"]).replace("*", "")
    wrap = lambda x: {sym: x} if x else None  # noqa: E731  (formatters are not None-safe)
    prompt = R.build_prompt([row], wrap(inp.get("g")), benchmark, wrap(inp.get("b")), wrap(inp.get("t")),
                            wrap(inp.get("e")))
    tickers = [row["Ticker"]]
    best, best_score, meta, attempts = None, -1, {}, 0
    max_tokens = None
    for attempt in range(2):  # one retry for small local models / token exhaustion
        attempts += 1
        try:
            text, meta = client.generate(prompt, max_tokens=max_tokens)
        except Exception:
            if best is None:  # nothing usable yet → surface the error
                raise
            break  # keep the partial narrative from attempt 1
        norm = R.normalize_narrative(text, tickers)
        score = R.narrative_coverage(norm, tickers)
        if score > best_score:
            best, best_score = norm, score
        gaps = R.narrative_gaps(norm, tickers)
        if gaps is None:
            break
        if meta.get("finish_reason") == "length":
            max_tokens = meta["max_tokens"] * 2  # reasoning ate the budget — give it room once
        print(f"⚠️  local AI reply for {sym} unusable ({gaps}) — attempt {attempt + 1}/2")
    if not best or not best.strip():
        reason = "model ran out of tokens — raise --llm-max-tokens or disable thinking" \
            if meta.get("finish_reason") == "length" else "model returned an empty reply"
        return {"status": "error", "error": reason, "model": meta.get("model")}
    parsed = parse_narrative(best, sym)
    if not (parsed["overview"] or parsed["reverse_dcf"] or parsed["stomach_test"]):
        parsed["raw"] = best.strip()[:6000]
    return {"status": "done", "narrative": parsed, "model": meta.get("model"),
            "model_short": _short(meta.get("model")), "elapsed_s": meta.get("elapsed_s"),
            "usage": meta.get("usage"), "attempts": attempts, "generated_at": time.time(),
            "complete": best_score >= 1}
