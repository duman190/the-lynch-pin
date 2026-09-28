"""Local LLM (LM Studio, OpenAI-compatible) client + the portal's single-ticker AI overview.

Generation is **streamed** (``stream: true``): every token is forwarded to a *sink* as it arrives so
the UI can type the overview in real time and show time-to-first-token / tokens-per-second. Reasoning
("thinking") tokens are kept apart from the answer whether the server sends them as
``delta.reasoning_content`` / ``delta.reasoning`` or inline ``<think>…</think>`` in the content.

The prompt reuses the daily scan's DATASET block verbatim (``LynchPinResearcher.build_prompt`` —
static, the researcher is never instantiated, so no Gemini/OpenRouter key is needed) but replaces the
Twitter-thread task with a lean per-ticker one: no index SENTIMENT line (meaningless for one ticker
and a local model has no market feed) and no character limits, just "one paragraph" per section,
so the model spends no tokens counting.

For thinking models keep LM Studio's "Reasoning Section Parsing" (Developer settings) on so reasoning
arrives separately; inline and prefilled ``<think>`` blocks are handled too, just less precisely.

Degrades quietly: when LM Studio is not running or no model is loaded, ``status()`` reports
``available: False`` with a reason and the portal shows "AI offline" instead of an error.
"""
import json
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


class LLMCancelled(LLMError):
    """The caller abandoned the generation (watchdog / shutdown); the HTTP stream was closed."""


def strip_thinking(text):
    """Drops <think>…</think> blocks; a dangling unclosed <think> (truncated reasoning) drops the rest."""
    text = _THINK_RE.sub("", text or "")
    low = text.lower()
    if "<think>" in low:
        text = text[:low.index("<think>")]
    return text.replace("</think>", "").strip()


class ThinkSplitter:
    """Incrementally routes streamed content inside ``<think>…</think>`` to reasoning.

    ``feed(text)`` returns ``(reasoning, content)``; tags split across chunks are held back
    until they can be decided. ``flush()`` releases whatever is buffered at end of stream.

    Chat templates that *prefill* ``<think>`` stream the reasoning without the opening tag and
    only ever emit ``</think>``. If ``</think>`` arrives before any ``<think>``, everything seen so
    far was reasoning: the text before the tag is returned as reasoning and ``take_rewind()``
    reports once that the content already emitted must be moved to reasoning too.
    """
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.in_think = False
        self.seen_tag = False
        self.buf = ""
        self._rewind = False

    @staticmethod
    def _partial(buf, tag):
        low = buf.lower()
        for k in range(min(len(tag) - 1, len(buf)), 0, -1):
            if low.endswith(tag[:k]):
                return k
        return 0

    def take_rewind(self):
        r, self._rewind = self._rewind, False
        return r

    def feed(self, text):
        self.buf += text or ""
        reasoning, content = [], []
        while self.buf:
            low = self.buf.lower()
            if self.in_think:
                i = low.find(self.CLOSE)
                if i >= 0:
                    reasoning.append(self.buf[:i])
                    self.buf, self.in_think = self.buf[i + len(self.CLOSE):], False
                    continue
                keep = self._partial(self.buf, self.CLOSE)
                reasoning.append(self.buf[:len(self.buf) - keep])
                self.buf = self.buf[len(self.buf) - keep:]
                break
            i_open = low.find(self.OPEN)
            i_close = low.find(self.CLOSE) if not self.seen_tag else -1
            if i_close >= 0 and (i_open < 0 or i_close < i_open):  # implicit (prefilled) <think>
                reasoning.append(self.buf[:i_close])
                self.buf = self.buf[i_close + len(self.CLOSE):]
                self.seen_tag, self._rewind = True, True
                continue
            if i_open >= 0:
                content.append(self.buf[:i_open])
                self.buf, self.in_think, self.seen_tag = self.buf[i_open + len(self.OPEN):], True, True
                continue
            keep = self._partial(self.buf, self.OPEN)
            if not self.seen_tag:
                keep = max(keep, self._partial(self.buf, self.CLOSE))
            content.append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(reasoning), "".join(content)

    def flush(self):
        rest, self.buf = self.buf, ""
        return (rest, "") if self.in_think else ("", rest)


class StreamMeter:
    """Time-to-first-token and tokens/s for one streamed generation (1 SSE chunk ≈ 1 token)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.t0 = clock()
        self.t_first = None
        self.t_first_content = None
        self.t_end = None
        self.tokens = self.reasoning_tokens = self.content_tokens = 0
        self.usage = {}

    def chunk(self, reasoning, content):
        if not (reasoning or content):
            return
        now = self._clock()
        if self.t_first is None:
            self.t_first = now
        if content and self.t_first_content is None:
            self.t_first_content = now
        self.tokens += 1
        self.reasoning_tokens += 1 if reasoning else 0
        self.content_tokens += 1 if content and not reasoning else 0

    def rewind(self):
        """Content seen so far was reasoning after all (prefilled ``<think>``)."""
        self.reasoning_tokens += self.content_tokens
        self.content_tokens = 0
        self.t_first_content = None

    def finish(self, usage=None):
        self.t_end = self._clock()
        self.usage = usage or {}

    def metrics(self):
        now = self.t_end or self._clock()
        tokens, reasoning, content = self.tokens, self.reasoning_tokens, self.content_tokens
        exact = self.usage.get("completion_tokens")
        if self.t_end and isinstance(exact, int) and exact > 0:
            # the server's exact count once the stream is over; split it in the observed chunk ratio
            reasoning = round(reasoning * exact / self.tokens) if self.tokens else 0
            tokens, content = exact, exact - reasoning
        gen = (now - self.t_first) if self.t_first is not None else 0.0
        tok_s = round(max(tokens - 1, 1) / gen, 1) if gen >= 0.25 and tokens > 1 else None
        out = {"ttft_s": round(self.t_first - self.t0, 2) if self.t_first is not None else None,
               "tok_s": tok_s, "tokens": tokens, "reasoning_tokens": reasoning,
               "content_tokens": content, "elapsed_s": round(now - self.t0, 1),
               "thinking_s": round((self.t_first_content or now) - self.t_first, 1)
               if self.t_first is not None and self.reasoning_tokens else None}
        if self.usage.get("prompt_tokens") is not None:
            out["prompt_tokens"] = self.usage.get("prompt_tokens")
        return out


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
    def autoload_pending(self):
        """True when the next generate() will first ask LM Studio to load the model (may take minutes)."""
        return bool(self.settings.llm_autoload and not self._autoloaded)

    def generate(self, prompt, max_tokens=None, on_delta=None, cancelled=None, on_rewind=None):
        """Streams a chat completion and returns ``(content, meta)``.

        ``on_delta(reasoning, content, metrics)`` is called for every chunk; ``on_rewind()`` when
        content already delivered turns out to have been reasoning (prefilled ``<think>``);
        ``cancelled()`` returning True closes the stream (LM Studio stops generating) and raises
        LLMCancelled. Raises LLMUnavailable / LLMError.
        """
        st = self.status()
        if not st.get("available"):
            raise LLMUnavailable(st.get("reason") or "local model unavailable")
        model, ctx = st["model"], int(st.get("ctx") or self.settings.llm_ctx)
        est = estimate_tokens(prompt)
        if est + PROMPT_MARGIN > ctx:
            raise LLMError(f"prompt (~{est} tokens) does not fit the {ctx}-token context window")
        budget = max(256, min(max_tokens or self.settings.llm_max_tokens, ctx - est - 256))
        self._autoload(model)
        meter = StreamMeter()
        try:
            r = self.http.post(f"{self.base}/v1/chat/completions", json={
                "model": model, "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.6, "max_tokens": budget,
                "stream": True, "stream_options": {"include_usage": True},
            }, stream=True, timeout=(3, self.settings.llm_timeout))
        except requests.ConnectionError as e:
            self._mark(False, f"LM Studio not reachable at {self.base}")
            raise LLMUnavailable(f"LM Studio not reachable at {self.base}") from e
        except requests.Timeout as e:
            raise LLMError(f"local model timed out after {self.settings.llm_timeout}s") from e
        try:
            if r.status_code != 200:
                body_text = r.text[:400]
                if r.status_code in (400, 404) and any(k in body_text.lower() for k in _NOT_LOADED):
                    self._mark(False, f"model '{model}' is not loaded in LM Studio")
                    raise LLMUnavailable(f"model '{model}' is not loaded in LM Studio")
                raise LLMError(f"LM Studio HTTP {r.status_code}: {body_text}")
            content, finish, usage, served = self._consume(r, meter, on_delta, cancelled, on_rewind)
        except requests.RequestException as e:  # connection dropped / read timeout mid-stream
            raise LLMError(f"stream from LM Studio interrupted ({type(e).__name__})") from e
        finally:
            r.close()
        meter.finish(usage)
        self._mark(True, model=model)
        if st.get("ctx_loaded") is None:
            with self._lock:  # JIT-loaded just now: re-probe so the real context window is used next
                self._status_at = -1e9
        meta = {"model": served or model, "finish_reason": finish, "max_tokens": budget, "prompt_tokens_est": est,
                "ctx": ctx, "usage": usage or {}, "metrics": meter.metrics(),
                "elapsed_s": meter.metrics()["elapsed_s"]}
        return content.strip(), meta

    @staticmethod
    def _consume(r, meter, on_delta, cancelled, on_rewind=None):
        """Reads an SSE (or, if the server ignored ``stream``, a plain JSON) chat completion."""
        splitter = ThinkSplitter()
        parts, finish, usage, served = [], None, None, None

        def emit(reasoning, content):
            if splitter.take_rewind():  # "</think>" without "<think>": the content so far was reasoning
                moved = "".join(parts)
                parts.clear()
                meter.rewind()
                if on_rewind:
                    on_rewind(moved)
            emit_now(reasoning, content)

        def emit_now(reasoning, content):
            if not (reasoning or content):
                return
            meter.chunk(reasoning, content)
            if content:
                parts.append(content)
            if on_delta:
                on_delta(reasoning, content, meter.metrics())

        def feed(reasoning, content):
            r_extra, c = splitter.feed(content)
            emit((reasoning or "") + r_extra, c)

        if "text/event-stream" not in (r.headers.get("Content-Type") or ""):
            try:
                body = r.json()
                choice = (body.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
            except (ValueError, AttributeError, IndexError) as e:
                raise LLMError("unexpected chat completion response") from e
            rs = msg.get("reasoning_content") or msg.get("reasoning")
            feed(rs if isinstance(rs, str) else "", msg.get("content") or "")
            emit(*splitter.flush())
            return "".join(parts), choice.get("finish_reason"), body.get("usage"), body.get("model")

        r.encoding = "utf-8"  # SSE has no charset → requests would decode emoji as ISO-8859-1
        for raw in r.iter_lines(decode_unicode=True):
            if cancelled is not None and cancelled():
                raise LLMCancelled("generation cancelled")
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("error"):
                err = chunk["error"]
                raise LLMError(f"LM Studio error: {err.get('message', err) if isinstance(err, dict) else err}")
            served = served or chunk.get("model")
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                rs = delta.get("reasoning_content") or delta.get("reasoning")
                feed(rs if isinstance(rs, str) else "", delta.get("content") or "")
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        emit(*splitter.flush())
        return "".join(parts), finish, usage, served


# ── portal prompt ───────────────────────────────────────────────────────────
_DATASET_RE = re.compile(r"DATASET:\n(.*?)\n\nTASK:", re.DOTALL)

PORTAL_TASK = """TASK: Write about {sym} in exactly three labeled paragraphs, in this order, each starting with its label. \
Plain text, no markdown, no headings, nothing before the first label.

🤖: One short paragraph: conviction vs risk, from the valuation and the Income Grade. An accelerating \
waterfall (A/A+) is a "sleep well" compounder; bloating costs (RED) mean flagging what could go wrong; a low \
PEG with a poor grade means judging trap vs opportunity.

📊 Reverse DCF: One paragraph. What the company does and its moat. Then the math, citing the "Base ROI math" \
numbers: "X% base ROI requires EPS to compound at Y%/yr for 5 years, re-rating from Mx FwdPE to Nx implied PE \
at maturity" (no decay exponents or terminal PEG formulas). What that requires operationally (revenue growth, \
margins, share gains), and your verdict: realistic, achievable or a stretch.

🧪 Stomach Test: One paragraph with the specific bear case: why it could underperform the market for 5 years, \
with numbers. Weigh the balance sheet: AA+/AAA is a fortress that mitigates risk; BBB or below makes debt a key \
risk (cite interest coverage, net debt/EBITDA or debt service/FCF). If Technicals are BEARISH or price is below \
SMA200, warn about catching a falling knife; if ACCUMULATION, note the favorable entry. If a 6M Directional Edge \
is given: BULL above 60% supports selling cash-secured puts on dips, BEAR above 60% supports covered calls on \
bounces, neither above 55% means low conviction for options income.

Tone: wise, slightly witty, Peter Lynch talking to a friend over coffee."""


def build_portal_prompt(row, g=None, b=None, t=None, e=None, benchmark="SPY"):
    """Per-ticker prompt: the daily scan's DATASET block + the lean three-paragraph task."""
    from engine.ai_research import LynchPinResearcher as R
    sym = str(row["Ticker"]).replace("*", "")
    row = dict(row, Ticker=sym)  # the "*" risk flag means nothing to the model; keep DATASET and TASK aligned
    wrap = lambda x: {sym: x} if x else None  # noqa: E731  (the formatters are not None-safe)
    full = R.build_prompt([row], wrap(g), benchmark, wrap(b), wrap(t), wrap(e))
    m = _DATASET_RE.search(full)
    if not m:  # upstream prompt layout changed — fail loudly rather than send the thread task
        raise LLMError("could not extract the DATASET block from LynchPinResearcher.build_prompt")
    return ("Act as Peter Lynch analysing one stock for a value investor.\n\n"
            f"DATASET:\n{m.group(1).strip()}\n\n" + PORTAL_TASK.format(sym=f"${sym}"))


# ── reply parsing ───────────────────────────────────────────────────────────
SECTION_KEYS = ("overview", "reverse_dcf", "stomach_test")
_LABELS = (
    ("overview", re.compile(r"🤖\s*(?:Overview\s*)?:")),
    ("reverse_dcf", re.compile(r"📊\s*(?:Reverse\s*(?:5Y\s*)?DCF)?\s*:", re.IGNORECASE)),
    ("stomach_test", re.compile(r"(?:🧪|🐻)\s*(?:\"?Stomach\s*Test\"?[^:\n]{0,60})?:", re.IGNORECASE)),
)
_LABEL_STARTS = ("🤖", "📊", "🧪", "🐻")


def _clean(text):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):  # template brackets copied literally
        text = text[1:-1].strip()
    text = re.sub(r"^(?:Overview|Bull|Bear)\s*:\s*", "", text)
    return text.strip()


def _trim_partial_label(text):
    """While streaming, drop a label that has started to arrive but has no colon yet ("…\n\n📊 Rev")."""
    tail = text[-40:]
    cut = max(tail.rfind(e) for e in _LABEL_STARTS)
    if cut >= 0 and ":" not in tail[cut:]:
        return text[:len(text) - len(tail) + cut]
    return text


def parse_sections(text, partial=False):
    """Splits a reply into overview / reverse_dcf / stomach_test (tolerates **bold**, a leading
    ``$SYM:`` header, 🐻 instead of 🧪 and template brackets). Unlabelled text becomes the overview."""
    text = strip_thinking(text or "").replace("**", "").replace("__", "")
    if partial:
        text = _trim_partial_label(text)
    hits = []
    for key, rx in _LABELS:
        m = rx.search(text)
        if m:
            hits.append((m.start(), m.end(), key))
    hits.sort()
    out = {k: "" for k in SECTION_KEYS}
    for i, (start, end, key) in enumerate(hits):
        stop = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        out[key] = _clean(text[end:stop])
    if not hits:
        body = re.sub(r"^\s*\$?[A-Z][A-Z0-9.\-]{0,9}\s*:\s*\n", "", text)  # bare "$MSFT:" header
        out["overview"] = _clean(body)
    return out


def section_score(sections):
    return sum(1 for k in SECTION_KEYS if sections.get(k))


class NullSink:
    def begin(self, attempt, note=None):
        pass

    def delta(self, reasoning, content, metrics):
        pass

    def rewind(self, moved):
        pass


def ticker_narrative(client, data, benchmark="SPY", sink=None, cancelled=None):
    """Streams the AI overview for one analysed ticker. Returns a result dict (status done | error)."""
    inp = data.get("_ai_inputs") or {}
    row = inp.get("row")
    if not row:
        return {"status": "error", "error": "AI overview needs GARP data (no valuation row)"}
    try:
        prompt = build_portal_prompt(row, inp.get("g"), inp.get("b"), inp.get("t"), inp.get("e"), benchmark)
    except ImportError as e:  # engine.ai_research imports google-genai at module top
        return {"status": "error", "error": f"engine.ai_research unavailable ({e})"}
    sym = str(row["Ticker"]).replace("*", "")
    sink = sink or NullSink()
    best, best_score, best_meta, meta, attempts, max_tokens = None, -1, {}, {}, 0, None
    for attempt in range(2):  # one retry, only for an unusable or token-starved reply
        attempts += 1
        loading = getattr(client, "autoload_pending", lambda: False)()
        sink.begin(attempts, note="first reply was unusable — retrying" if attempt else
                   ("loading the model in LM Studio" if loading else None))
        try:
            text, meta = client.generate(prompt, max_tokens=max_tokens, on_delta=sink.delta, cancelled=cancelled,
                                         on_rewind=getattr(sink, "rewind", None))
        except LLMCancelled:
            raise
        except Exception:
            if best is None:  # nothing usable yet → surface the error
                raise
            break  # keep the partial narrative from attempt 1
        sections = parse_sections(text)
        score = section_score(sections)
        if score > best_score:
            best, best_score, best_meta = sections, score, meta
        if score == len(SECTION_KEYS):
            break
        if meta.get("finish_reason") == "length":
            max_tokens = meta["max_tokens"] * 2  # reasoning ate the budget — give it room once
        elif score > 0:
            break  # usable, just not all three sections: don't make the user watch it type again
        print(f"⚠️  local AI reply for {sym}: {score}/3 sections — attempt {attempt + 1}/2")
    if best is None or best_score <= 0:
        reason = "model ran out of tokens — raise --llm-max-tokens or disable thinking" \
            if meta.get("finish_reason") == "length" else "model returned an empty reply"
        return {"status": "error", "error": reason, "model": meta.get("model"), "metrics": meta.get("metrics")}
    return {"status": "done", "narrative": best, "model": best_meta.get("model"),
            "model_short": _short(best_meta.get("model")), "metrics": best_meta.get("metrics") or {},
            "elapsed_s": best_meta.get("elapsed_s"), "usage": best_meta.get("usage"), "attempts": attempts,
            "generated_at": time.time(), "complete": best_score == len(SECTION_KEYS)}
