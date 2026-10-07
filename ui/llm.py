"""Local LLM (LM Studio, OpenAI-compatible) client + the portal's single-ticker AI overview.

Generation is **streamed** (``stream: true``): every token is forwarded to a *sink* as it arrives so
the UI can type the overview in real time and show time-to-first-token / tokens-per-second. Reasoning
("thinking") tokens are kept apart from the answer whether the server sends them as
``delta.reasoning_content`` / ``delta.reasoning`` or inline ``<think>…</think>`` in the content.

The prompt is a static system message (the three-section task, kept in the server's prefix cache: the
client primes it once, see ``LocalLLMClient._prime``) plus a terse per-ticker data block built from the portal's own analysis: the company profile, the
analysts' price target and the Quick Overview's reverse-DCF math and red flags arrive pre-computed, so
a small model only has to write. Time to first token is the data's prefill; total time is mostly the
reply's length, hence "2-3 sentences" per section (see "Local AI tuning" in ui/README.md).

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
    NAME = "LM Studio"  # in error messages (ui/gemini.py reuses this client for Gemini's OpenAI-compatible API)
    SEND_REASONING_SWITCH = True  # reasoning "off" sends reasoning_effort=none
    STATUS_TTL = 15.0
    PRIME_EVERY = 60.0  # seconds between system-prompt primes (see _prime)

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
        self._reasoning_field_ok = True  # flips to False if the server rejects reasoning_effort
        self._native_ok = True           # flips to False if the server has no LM Studio /api/v0
        self._primed, self._primed_at = None, -1e9  # (model, system prompt) the server's prefix cache holds

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
        """LM Studio's native REST API (state / loaded context). Empty list when unsupported; a 404 (another
        OpenAI-compatible server, e.g. standalone Splash) is remembered so it is not asked again."""
        if not self._native_ok:
            return []
        try:
            r = self.http.get(f"{self.base}/api/v0/models", timeout=(2, 2))
            if r.status_code == 200:
                return [m for m in (r.json().get("data") or []) if isinstance(m, dict) and m.get("id")]
            if r.status_code == 404:
                self._native_ok = False
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
        st["reasoning"] = self.reasoning_mode()
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
    def reasoning_mode(self):
        """"off" (reasoning_effort=none is sent), "on" (server default) or "unsupported" (server refused the field)."""
        if self.settings.llm_reasoning != "off":
            return "on"
        return "off" if self._reasoning_field_ok else "unsupported"

    def autoload_pending(self):
        """True when the next generate() will first ask LM Studio to load the model (may take minutes)."""
        return bool(self.settings.llm_autoload and not self._autoloaded)

    def generate(self, prompt, max_tokens=None, on_delta=None, cancelled=None, on_rewind=None):
        """Streams a chat completion and returns ``(content, meta)``. ``prompt`` is a user message, or a
        list of chat messages (a static system message first lets the server reuse its KV cache).

        ``on_delta(reasoning, content, metrics)`` is called for every chunk; ``on_rewind()`` when
        content already delivered turns out to have been reasoning (prefilled ``<think>``);
        ``cancelled()`` returning True closes the stream (LM Studio stops generating) and raises
        LLMCancelled. Raises LLMUnavailable / LLMError.
        """
        st = self.status()
        if not st.get("available"):
            raise LLMUnavailable(st.get("reason") or "local model unavailable")
        model, ctx = st["model"], int(st.get("ctx") or self.settings.llm_ctx)
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
        est = estimate_tokens("\n".join(m["content"] for m in messages))
        if est + PROMPT_MARGIN > ctx:
            raise LLMError(f"prompt (~{est} tokens) does not fit the {ctx}-token context window")
        budget = max(256, min(max_tokens or self.settings.llm_max_tokens, ctx - est - 256))
        self._autoload(model)
        if messages[0].get("role") == "system":
            self._prime(model, messages[0])
        meter = StreamMeter()
        payload = {"model": model, "messages": messages,
                   "temperature": 0.6, "max_tokens": budget,
                   "stream": True, "stream_options": {"include_usage": True}}
        if self.SEND_REASONING_SWITCH and self.reasoning_mode() == "off":
            payload["reasoning_effort"] = "none"  # "a switch, not a dial": none turns thinking off
        r = self._post_chat(payload)
        if "reasoning_effort" in payload and self._rejects_reasoning(r):
            r.close()  # this server does not know the switch: remember and go without it
            self._reasoning_field_ok = False
            print(f"ℹ️  {self.base} rejected reasoning_effort — generating with the model's default reasoning")
            payload.pop("reasoning_effort")
            r = self._post_chat(payload)
        try:
            if r.status_code != 200:
                self._http_error(r.status_code, r.text[:400], model)
            content, finish, usage, served = self._consume(r, meter, on_delta, cancelled, on_rewind, self.NAME)
        except requests.RequestException as e:  # connection dropped / read timeout mid-stream
            raise LLMError(f"stream from {self.NAME} interrupted ({type(e).__name__})") from e
        finally:
            r.close()
        meter.finish(usage)
        cached = ((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached == 0 and messages[0].get("role") == "system":
            with self._lock:  # the server reports it lost the system prompt (restart / eviction): prime again
                self._primed = None
        self._mark(True, model=model)
        if st.get("ctx_loaded") is None:
            with self._lock:  # JIT-loaded just now: re-probe so the real context window is used next
                self._status_at = -1e9
        meta = {"model": served or model, "finish_reason": finish, "max_tokens": budget, "prompt_tokens_est": est,
                "reasoning": "off" if "reasoning_effort" in payload else self.reasoning_mode(),
                "ctx": ctx, "usage": usage or {}, "metrics": meter.metrics(),
                "elapsed_s": meter.metrics()["elapsed_s"]}
        return content.strip(), meta

    def _rejects_reasoning(self, r):
        """The server refused the request because of ``reasoning_effort``."""
        return r.status_code in (400, 422) and "reasoning" in r.text[:400].lower()

    def _http_error(self, status, body_text, model):
        """Raises for a non-200 chat completion: LLMUnavailable when no model can answer, else LLMError."""
        if status in (400, 404) and any(k in body_text.lower() for k in _NOT_LOADED):
            self._mark(False, f"model '{model}' is not loaded in LM Studio")
            raise LLMUnavailable(f"model '{model}' is not loaded in LM Studio")
        raise LLMError(f"LM Studio HTTP {status}: {body_text}")

    def _prime(self, model, system):
        """Sends the system prompt alone (empty user message, one token) once per prompt, and again after the
        server reports a miss (at most once a minute). Splash keeps a request's reusable state at the end of its
        prompt, so only a request that *ends* with the shared instructions leaves a state every ticker can resume
        from: after it each overview prefills just its data ("cached 224"). Harmless elsewhere (LM Studio caches a
        prefix seen twice anyway). Best effort: errors are ignored."""
        key = (model, system["content"])
        with self._lock:
            if self._primed == key or self._clock() - self._primed_at < self.PRIME_EVERY:
                return
            self._primed, self._primed_at = key, self._clock()
        payload = {"model": model, "messages": [system, {"role": "user", "content": ""}], "max_tokens": 1,
                   "temperature": 0, "stream": False}
        if self.reasoning_mode() == "off":
            payload["reasoning_effort"] = "none"
        try:
            self.http.post(f"{self.base}/v1/chat/completions", json=payload, timeout=(3, 60)).close()
        except requests.RequestException:
            pass

    def _post_chat(self, payload):
        try:
            return self.http.post(f"{self.base}/v1/chat/completions", json=payload, stream=True,
                                  timeout=(3, self.settings.llm_timeout))
        except requests.ConnectionError as e:
            self._mark(False, f"LM Studio not reachable at {self.base}")
            raise LLMUnavailable(f"LM Studio not reachable at {self.base}") from e
        except requests.Timeout as e:
            raise LLMError(f"local model timed out after {self.settings.llm_timeout}s") from e

    @staticmethod
    def _consume(r, meter, on_delta, cancelled, on_rewind=None, name="LM Studio"):
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
                raise LLMError(f"{name} error: {err.get('message', err) if isinstance(err, dict) else err}")
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
# The instructions are a static system message and the ticker's data the user message: LM Studio reuses the KV
# cache of a prefix it has seen twice, so after warm-up only the data is prefilled. Prefill is the TTFT (~100
# tok/s for a 27B on an M3 Pro: every 100 data tokens ≈ 1 s), so the data is pre-digested and terse: the
# reverse-DCF sentence, the analysts' upside and the red flags come computed (ui/quick.py), never left to the
# model's arithmetic. See "Local AI tuning" in ui/README.md.
PORTAL_SYSTEM = """You are Peter Lynch talking to a friend over coffee: wise, plain-spoken, slightly witty. Write a \
research note on the one stock in the data for a long-term value investor.

Reply with exactly three paragraphs of 2-3 sentences, plain text, no markdown, each starting with its label:
🤖: Start with what the company does and its moat, then conviction vs risk from the valuation and income grade; \
end with the analysts' target sentence ("Analysts' target $X (Y% upside)") copied word for word.
📊 Reverse DCF: Only the math: "X% base ROI requires EPS to compound at Y%/yr for 5 years, re-rating from Mx to Nx \
forward PE", what that demands operationally (revenue growth, margins), then one plain sentence on whether \
those assumptions are realistic, achievable or a stretch (as given) and why, never a label like "Assumptions: achievable".
🧪 Stomach Test: The specific bear case: why it could lag the market for 5 years, built on the red flags, with numbers.
Quote numbers exactly as given, never add or combine them, and never mention "the data" or these instructions."""

_INCOME_KEEP = ("Revenue", "OpIncome", "EPS")  # plus any RED line
_INCOME_NAMES = {"Revenue": "revenue", "OpIncome": "op income", "NetIncome": "net income"}
_VERDICTS = {"realistic": "realistic", "achievable": "achievable", "stretch": "a stretch"}
_DUP_FLAGS = ("Analysts' mean target", "Costs running ahead")  # already in the PT / income lines


def _flag_headline(text):
    """'Forward PE 57.9x (above 40x): even next year's…' → 'Forward PE 57.9x (above 40x)'."""
    head, _, rest = text.partition(": ")
    return text.rstrip(".") if len(rest) < 40 else head


def portal_data(d):
    """The ticker's analysis (``ui.analysis`` result) as a terse, pre-digested data block."""
    from ui.quick import _first_sentences, _money, _num, implied_terminal_pe, quick_overview
    sym, p, st, cur = d["ticker"], d.get("profile") or {}, d.get("stats") or {}, d.get("currency")
    quick = d.get("quick") or quick_overview(d)
    out = [f"${sym} {d.get('name') or sym}" + (f", {d['industry']}" if d.get("industry") else "")
           + (f", {_money(d['market_cap'], cur)} cap" if _num(d.get("market_cap")) else "")]
    biz = _first_sentences(p.get("summary"))  # what the company does: small models may not know it
    if biz:
        out.append(biz)
    money = []
    if _num(p.get("operating_margin")) is not None:
        money.append(f"op margin {p['operating_margin'] * 100:.0f}%")
    if _num(p.get("profit_margin")) is not None:
        money.append(f"net {p['profit_margin'] * 100:.0f}%")
    if _num(p.get("free_cashflow")) is not None:
        money.append(f"FCF {_money(p['free_cashflow'], cur)}")
    if _num(p.get("dividend_yield")):
        money.append(f"dividend {p['dividend_yield']:.1f}%")
    if money:
        out.append(", ".join(money))
    target, price = _num(p.get("target_mean")), _num(d.get("price"))
    if target and price:
        up, rec = (target / price - 1) * 100, (p.get("recommendation") or "").replace("_", " ")
        # the whole sentence, for 🤖 to copy: small models skipped it or wrote "(-2% downside)" from a template
        out.append(f"Analysts' target ${target:,.2f} ({abs(up):.0f}% {'upside' if up >= 0 else 'downside'})."
                   + (f" Consensus: {rec}." if rec else ""))
    pe, fwd, g, peg = (_num(st.get(k)) for k in ("PE", "FwdPE", "growth_pct", "PEG"))
    if fwd and g and peg is not None:
        v = (f"PE {pe:.1f}" if pe else "no trailing earnings") + f", FwdPE {fwd:.1f}, growth {g:.1f}%/yr, PEG {peg:.2f}"
        if st.get("history") == "ok" and _num(st.get("Mean")) is not None:
            v += f" (5Y mean {st['Mean']:.2f})"
        out.append(v)
        bull, base, bear = (_num(st.get(k)) for k in ("Bull", "Base", "Bear"))
        if None not in (bull, base, bear):
            out.append(f"5Y ROI/yr: bull {bull:.1f}%, base {base:.1f}%, bear {bear:.1f}%")
            math = implied_terminal_pe(st)
            if math:
                verdict = _VERDICTS.get(quick.get("dcf_verdict"))  # rule-based: small models muddle it
                from engine.lynch_pin_core import _avg_eps_growth  # growth fades from 20% (engine ROI math)
                div = _num(st.get("div_yield")) or 0.0
                out.append(f"Reverse DCF: {base:.1f}% base ROI"
                           + (f" ({div:.1f}% of it dividend yield)" if div >= 0.05 else "")
                           + f" requires EPS +{_avg_eps_growth(math[0]):.1f}%/yr for 5 years, "
                           f"re-rating {fwd:.1f}x to {math[1]:.1f}x forward PE" + (f"; assumptions: {verdict}" if verdict else ""))
    inc = d.get("income") or {}
    if inc.get("grade"):
        items = [f"{_INCOME_NAMES.get(i['label'], i['label'])} {i['growth'] * 100:+.0f}%"
                 + (" RED" if i.get("signal") == "bad" else "")
                 for i in inc.get("items") or [] if _num(i.get("growth")) is not None
                 and (i["label"] in _INCOME_KEEP or i.get("signal") == "bad")]
        out.append(f"Income grade {inc['grade']}" + (": " + ", ".join(items) if items else ""))
    cr = d.get("credit") or {}
    if cr.get("rating"):
        ms = {m["label"]: m["value"] for m in cr.get("metrics") or [] if _num(m.get("value")) is not None}
        bits = [f"{name} {ms[k]:.1f}x" for k, name in (("IntCov", "interest cover"), ("ND/EBITDA", "net debt/EBITDA"))
                if k in ms]
        out.append(f"Credit {cr['rating']}" + (" (fortress)" if cr["rating"] in ("AAA", "AA+") else "")
                   + (": " + ", ".join(bits) if bits else ""))
    t = d.get("technicals") or {}
    if t.get("signal") and _num(t.get("price_vs_sma200")) is not None and _num(t.get("rsi")) is not None:
        signal = "ACCUMULATION" if t["signal"].startswith("ACCUMUL") else t["signal"]  # the engine says ACCUMUL
        line = f"Technicals {signal}, {t['price_vs_sma200']:+.0f}% vs SMA200, RSI {t['rsi']:.0f}"
        zone = t.get("accumulation_zone")
        if signal == "ACCUMULATION" and zone and _num(zone[0]) and _num(zone[1]):
            line += f", buy zone ${zone[0]:.0f}-{zone[1]:.0f}"
        out.append(line)
    e = d.get("edge") or {}
    bull_acc, bear_acc = _num(e.get("bull_acc")), _num(e.get("bear_acc"))
    if bull_acc is not None and bear_acc is not None:
        use = ("fits cash-secured puts" if bull_acc > 60 and bull_acc >= bear_acc else
               "fits covered calls" if bear_acc > 60 else
               "low conviction for options" if max(bull_acc, bear_acc) <= 55 else "no clear options edge")
        out.append(f"6M edge: bull {bull_acc:.0f}% / bear {bear_acc:.0f}% right, {use}")
    flags = [_flag_headline(f["text"]) for f in quick.get("stomach_test") or [] if not f["text"].startswith(_DUP_FLAGS)]
    out.append("Red flags: " + ("; ".join(flags) if flags else "none"))
    return "\n".join(out)


def scan_brief(row, info, grade=None, credit=None, technicals=None, edge=None):
    """The daily scan's ticker as the same data block the portal's AI overview reads (main.py passes it
    to the scan's thread prompt): the engine row, the Yahoo quote the engine already holds, the grades."""
    from ui.formats import _extract_price, _num, format_credit, format_income, format_stats, format_technicals
    from ui.quick import profile_from_info
    info = info or {}
    sym = str(row.get("Ticker", "")).replace("*", "")
    return portal_data({
        "ticker": sym, "status": "done", "name": info.get("longName") or info.get("shortName") or sym,
        "sector": info.get("sector"), "industry": info.get("industry"), "currency": info.get("currency") or "USD",
        "market_cap": _num(info.get("marketCap")), "price": _extract_price(info),
        "profile": profile_from_info(info), "stats": format_stats(row), "flagged": str(row.get("Ticker", "")).endswith("*"),
        "income": format_income(grade), "credit": format_credit(credit),
        "technicals": format_technicals(technicals), "edge": dict(edge) if edge else None,
    })


def build_portal_messages(data):
    """Chat messages for one ticker's AI overview: the static instructions, then its data."""
    return [{"role": "system", "content": PORTAL_SYSTEM}, {"role": "user", "content": portal_data(data)}]


# ── reply parsing ───────────────────────────────────────────────────────────
SECTION_KEYS = ("overview", "reverse_dcf", "stomach_test")
_BOL = r"(?:^|\n)[ \t>*#_-]*"  # start of a line, ignoring markdown bullets / quotes / bold remnants
# The label words at the start of a line after a wrong emoji or none ("🧧 Stomach Test:", seen from a 3B-active MoE)
_ANY_EMOJI = r"[^\w\s:]{0,3}[ \t]*"
_LABELS = (
    # Either the emoji at the start of a line (label text and colon optional — small models often write
    # "🤖 Let's look…" or "📊 Caterpillar makes…"), or the full label with its colon anywhere.
    ("overview", re.compile(_BOL + r"🤖[ \t]*(?:Overview\b)?[ \t]*:?|🤖[ \t]*(?:Overview[ \t]*)?:", re.IGNORECASE)),
    ("reverse_dcf", re.compile(_BOL + r"📊[ \t]*(?:Reverse[ \t]*(?:5Y[ \t]*)?DCF\b)?[ \t]*:?"
                               r"|📊[ \t]*Reverse[ \t]*(?:5Y[ \t]*)?DCF[ \t]*:"
                               r"|" + _BOL + _ANY_EMOJI + r"Reverse[ \t]*(?:5Y[ \t]*)?DCF[ \t]*:", re.IGNORECASE)),
    ("stomach_test", re.compile(_BOL + r"(?:🧪|🐻)[ \t]*(?:\"?Stomach[ \t]*Test\"?[^:\n]{0,60}:)?[ \t]*:?"
                                r"|(?:🧪|🐻)[ \t]*\"?Stomach[ \t]*Test\"?[^:\n]{0,60}:"
                                r"|" + _BOL + _ANY_EMOJI + r"\"?Stomach[ \t]*Test\"?[^:\n]{0,60}:", re.IGNORECASE)),
)
_LABEL_WORDS = {"🤖": ("overview",), "📊": ("reverse dcf", "reverse 5y dcf"),
                "🧪": ("stomach test",), "🐻": ("stomach test",)}
_LABEL_STARTS = tuple(_LABEL_WORDS)


def _clean(text):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):  # template brackets copied literally
        text = text[1:-1].strip()
    text = re.sub(r"^(?:Overview|Bull|Bear)\s*:\s*", "", text)
    return text.strip()


def _trim_partial_label(text):
    """While streaming, hide a label that is still arriving ("…\n\n📊", "…\n\n📊 Reverse D") so its
    letters never flash as answer text. Once the text after the emoji is clearly prose, it stays."""
    tail = text[-90:]
    cut = max(tail.rfind(e) for e in _LABEL_STARTS)
    if cut < 0:
        return text
    emoji = next(e for e in _LABEL_STARTS if tail.startswith(e, cut))
    after = tail[cut + len(emoji):]
    if "\n" in after or ":" in after:
        return text  # the label line is complete
    words = after.strip().lstrip('"').lower()
    label_prefix = any(w.startswith(words) for w in _LABEL_WORDS[emoji])
    long_stomach = words.startswith("stomach test") and len(after) < 75  # "(why it can underperform…):" follows
    if not words or label_prefix or long_stomach:
        return text[:len(text) - len(tail) + cut]
    return text


def parse_sections(text, partial=False):
    """Splits a reply into overview / reverse_dcf / stomach_test (tolerates **bold**, a leading
    ``$SYM:`` header, 🐻 instead of 🧪 and template brackets). Unlabelled text before the first label (or the
    whole reply, without labels) becomes the overview."""
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
    lead = text[:hits[0][0]] if hits else text
    if not out["overview"]:  # no 🤖 label: unlabelled text before the first label is the overview (qwen2.5-7b)
        lead = re.sub(r"^\s*\$?[A-Z][A-Z0-9.\-]{0,9}\s*:\s*\n", "", lead)  # bare "$MSFT:" header
        out["overview"] = _clean(lead)
    if not hits:  # no labels at all, but the three paragraphs in order (qwen3.6-35b-a3b, ~1 in 30)
        paras = [p for p in re.split(r"\n[ \t]*\n", out["overview"]) if p.strip()]
        if len(paras) == 3 or (partial and len(paras) == 2):
            for key, para in zip(SECTION_KEYS, paras):
                out[key] = _clean(para)
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


def ticker_narrative(client, data, benchmark="SPY", sink=None, cancelled=None, attempts=2):
    """Streams the AI overview for one analysed ticker. Returns a result dict (status done | error).
    ``attempts``: 2 retries an unusable or token-starved reply once; 1 for a rate-limited backend (Gemini), where
    every request was admitted against its quota. ``benchmark`` is unused (the 6M edge in ``data`` already names
    it); kept for callers."""
    row = (data.get("_ai_inputs") or {}).get("row")
    if not row:
        return {"status": "error", "error": "AI overview needs GARP data (no valuation row)"}
    prompt = build_portal_messages(data)
    sym = data.get("ticker") or str(row["Ticker"]).replace("*", "")
    sink = sink or NullSink()
    max_attempts, attempts = attempts, 0
    best, best_score, best_meta, meta, max_tokens = None, -1, {}, {}, None
    for attempt in range(max_attempts):  # by default one retry, only for an unusable or token-starved reply
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
        print(f"⚠️  AI reply for {sym}: {score}/3 sections — attempt {attempt + 1}/{max_attempts}")
    if best is None or best_score <= 0:
        reason = "model ran out of tokens — raise --llm-max-tokens or disable thinking" \
            if meta.get("finish_reason") == "length" else "model returned an empty reply"
        return {"status": "error", "error": reason, "model": meta.get("model"), "metrics": meta.get("metrics")}
    return {"status": "done", "narrative": best, "model": best_meta.get("model"),
            "model_short": _short(best_meta.get("model")), "metrics": best_meta.get("metrics") or {},
            "elapsed_s": best_meta.get("elapsed_s"), "usage": best_meta.get("usage"), "attempts": attempts,
            "reasoning": best_meta.get("reasoning"),
            "generated_at": time.time(), "complete": best_score == len(SECTION_KEYS)}
