import math
import json
import re
import time
import os
import requests
from google import genai


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# OpenRouter "Free Models Router" (https://openrouter.ai/openrouter/free):
# "The simplest way to get free inference. openrouter/free is a router that selects free
# models at random from the models available on OpenRouter. The router smartly filters for
# models that support features needed for your request such as image understanding, tool
# calling, structured outputs and more." Zero cost per token, 200K-token context window,
# text + image in / text out.
OPENROUTER_FREE_MODEL = "openrouter/free"

# Meta AI developer API (https://dev.meta.ai) — OpenAI Responses-style endpoint. Paid last resort:
# "Muse Spark 1.3 Contributor" is the same model as Muse Spark 1.3 at up to 95% off ($0.10/M in,
# $0.20/M out) in exchange for inputs/outputs being used to train Meta's models; 1M context,
# rate-limited by tokens. A full batch prompt (~3K in / ~4K out) costs well under a cent.
META_URL = "https://api.meta.ai/v1/responses"
META_MODEL = "muse-spark-1.3-contributor"

# Error signatures that mean "the model is busy / rate limited" → worth retrying the same tier.
_TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "502", "504", "overloaded")


def _is_transient(error_msg):
    return any(marker in error_msg for marker in _TRANSIENT_MARKERS)


class LynchPinResearcher:
    ATTEMPTS_PER_TIER = 3
    MAX_DELAY = 120  # seconds; cap for exponential backoff between busy retries

    def __init__(self):
        gemini_key = os.environ.get("GEMINI_API_KEY")
        # Gemini tiers are skipped when no key is set (the chain then runs on OpenRouter only)
        self.client = genai.Client(api_key=gemini_key) if gemini_key else None
        self.best_model = "gemini-3.7-flash"
        self.backup_model = "gemini-3.6-flash"
        self.openrouter_model = OPENROUTER_FREE_MODEL
        self.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")
        # Paid 4th tier; dev.meta.ai's quick-start names the variable MODEL_API_KEY, accept both
        self.meta_model = META_MODEL
        self.meta_api_key = os.environ.get("META_API_KEY") or os.environ.get("MODEL_API_KEY")
        if not self.client and not self.openrouter_api_key and not self.meta_api_key:
            print("⚠️  None of GEMINI_API_KEY / OPENROUTER_API_KEY / META_API_KEY is set — AI research will be unavailable.")

    # ── provider adapters ────────────────────────────────────────────────────────
    def _call_gemini_model(self, model, prompt):
        response = self.client.models.generate_content(model=model, contents=prompt)
        return response.text

    def _call_meta_model(self, model, prompt):
        """Non-streaming call to Meta's Responses API; returns the concatenated output text.

        Response shape (OpenAI Responses convention): ``output`` is a list of items; the
        assistant text lives in items of ``type == "message"`` as ``content[].text`` parts of
        ``type == "output_text"``. ``reasoning`` items and any other part types are ignored.
        Some servers also expose a convenience ``output_text`` string — used when present.
        """
        resp = requests.post(
            META_URL,
            headers={"Authorization": f"Bearer {self.meta_api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                "stream": False,
            },
            # Non-streaming + reasoning model: ~100s for 8 tickers, ~200s for a 16-position portfolio
            timeout=(30, 600),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Meta HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("error"):
            err = body["error"]
            raise RuntimeError(f"Meta error {err.get('code', '') if isinstance(err, dict) else ''}: "
                               f"{err.get('message', err) if isinstance(err, dict) else err}")
        text = body.get("output_text")
        if not isinstance(text, str) or not text.strip():
            parts = []
            for item in body.get("output") or []:
                if item.get("type") != "message":
                    continue
                for part in item.get("content") or []:
                    if part.get("type") == "output_text" and part.get("text"):
                        parts.append(part["text"])
            text = "".join(parts)
        text = text.strip()
        if not text:
            raise RuntimeError(f"Meta returned empty content (status: {body.get('status')})")
        usage = body.get("usage") or {}
        if usage:
            print(f"ℹ️  Meta {model}: {usage.get('input_tokens', '?')} in / {usage.get('output_tokens', '?')} out tokens")
        return text

    def _call_openrouter_model(self, model, prompt):
        """OpenAI-compatible streaming chat completion against OpenRouter.

        Streams SSE chunks and concatenates ``choices[0].delta.content``. Reasoning
        models served by the router also emit ``delta.reasoning`` — that is ignored,
        only the final answer text is returned.
        """
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "Content-Type": "application/json",
                # Optional attribution headers recommended by OpenRouter
                "HTTP-Referer": "https://github.com/duman190/the-lynch-pin",
                "X-Title": "The Lynch Pin",
            },
            json={"model": model, "stream": True, "messages": [{"role": "user", "content": prompt}]},
            stream=True,
            timeout=(30, 300),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")

        parts = []
        served_by = None
        resp.encoding = "utf-8"  # SSE has no charset header → requests would default to ISO-8859-1 (mojibake on emoji)
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data:"):
                continue  # keep-alive comments / blank separators
            payload = raw_line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            # OpenRouter may send an error envelope mid-stream (e.g. upstream provider 429/503)
            if "error" in chunk:
                err = chunk["error"]
                code = err.get("code", "") if isinstance(err, dict) else ""
                msg = err.get("message", err) if isinstance(err, dict) else err
                raise RuntimeError(f"OpenRouter error {code}: {msg}")
            served_by = served_by or chunk.get("model")
            for choice in chunk.get("choices") or []:
                content = (choice.get("delta") or {}).get("content")
                if content:
                    parts.append(content)

        text = "".join(parts).strip()
        if not text:
            raise RuntimeError(f"OpenRouter returned empty content (model: {served_by})")
        print(f"ℹ️  OpenRouter routed to {served_by}")
        return text

    # ── fallback chain ───────────────────────────────────────────────────────────
    def _tiers(self):
        """Ordered (label, model, caller, reroll) tiers. A tier only joins when its API key is set.

        ``reroll`` marks a tier whose model is a random router: every call may land on a
        different model, so a non-transient error from one draw says nothing about the next
        and the tier is retried instead of abandoned.
        """
        tiers = []
        if self.client:
            tiers += [
                ("BEST", self.best_model, self._call_gemini_model, False),
                ("BACKUP", self.backup_model, self._call_gemini_model, False),
            ]
        if self.openrouter_api_key:
            tiers.append(("OPENROUTER", self.openrouter_model, self._call_openrouter_model, True))
        if self.meta_api_key:
            tiers.append(("META (paid)", self.meta_model, self._call_meta_model, False))
        return tiers

    def _backoff(self, delay, i):
        """Exponential backoff for the i-th (0-based) retry within a tier: delay, 2×, 4×… capped."""
        return min(delay * (2 ** i), self.MAX_DELAY)

    def _call_ai(self, prompt, delay=30, check=None, score=None):
        """4-layer fallback: best Gemini → backup Gemini → OpenRouter free router → Meta Muse (paid).

        Each tier gets ``ATTEMPTS_PER_TIER`` tries. Transient errors (503/429/...) are
        retried on the same tier with exponential backoff (``delay``, 2×, 4×… capped at
        ``MAX_DELAY`` seconds); any other error skips straight to the next tier — except on a
        ``reroll`` tier (the OpenRouter random router), where the next draw is a different
        model, so the tier is retried immediately.

        ``check(text)`` optionally validates a reply: it returns ``None`` when the reply is
        usable, else a short reason. A rejected reply burns the attempt and is retried
        immediately (no sleep — it is not a capacity problem). If every attempt is rejected,
        the rejected reply with the highest ``score(text)`` (default: length) is returned —
        but only if that score is positive, so a reply with nothing usable in it (e.g. a
        safety classifier's ``User Safety: safe``) is never used. Otherwise returns an
        ``AI Research Error: ...`` string.
        """
        tiers = self._tiers()
        total = self.ATTEMPTS_PER_TIER * len(tiers)
        last_error = "no AI tier available"
        best_rejected = None  # (score, text) of the most complete rejected reply
        for tier_idx, (tier_label, model, call, reroll) in enumerate(tiers):
            for i in range(self.ATTEMPTS_PER_TIER):
                attempt = tier_idx * self.ATTEMPTS_PER_TIER + i + 1
                try:
                    text = call(model, prompt)
                except Exception as e:
                    last_error = str(e)
                    if _is_transient(last_error):
                        if attempt < total:
                            wait = self._backoff(delay, i)
                            print(f"⚠️  {tier_label} AI Busy ({model}): {last_error[:160]} "
                                  f"— retrying in {wait}s... (Attempt {attempt}/{total})")
                            time.sleep(wait)
                        continue
                    if reroll:
                        print(f"⚠️  {tier_label} AI Error ({model}): {last_error[:120]} "
                              f"— re-rolling... (Attempt {attempt}/{total})")
                        continue
                    print(f"⚠️  {tier_label} AI Error ({model}): {last_error[:120]} — switching tier.")
                    break
                reason = check(text) if check else None
                if reason is None:
                    return text
                s = score(text) if score else len(text)
                if best_rejected is None or s > best_rejected[0]:
                    best_rejected = (s, text)
                print(f"⚠️  {tier_label} AI reply unusable ({model}): {reason} "
                      f"— retrying... (Attempt {attempt}/{total})")
        if best_rejected is not None and best_rejected[0] > 0:
            print("⚠️  No fully usable AI reply; using the most complete one.")
            return best_rejected[1]
        if best_rejected is not None:
            last_error = "every AI reply was unusable"
        return f"AI Research Error: {last_error}"

    # Backwards-compatible alias
    _call_gemini = _call_ai

    @staticmethod
    def _format_grader(grade_result):
        """Formats income statement grade into a compact string for the prompt."""
        if not grade_result:
            return "Income Statement: N/A"
        lines = [f"Income Grade: {grade_result['grade']}"]
        for label, growth, sig in grade_result['items']:
            if growth is not None:
                emoji = sig.replace('🟢', 'GREEN').replace('🔵', 'BLUE').replace('🔴', 'RED').replace('⚪', 'N/A')
                lines.append(f"  {label}: {growth*100:+.0f}% [{emoji}]")
        return "\n".join(lines)

    @staticmethod
    def _format_balance_sheet(bs_result):
        """Formats balance sheet credit rating into a compact string for the prompt."""
        if not bs_result:
            return "Balance Sheet: N/A"
        lines = [f"Credit Rating: {bs_result['rating']} (Synthetic — Damodaran methodology)"]
        for label, val in bs_result['metrics']:
            if val is not None:
                lines.append(f"  {label}: {val:.1f}")
        return "\n".join(lines)

    @staticmethod
    def _format_technicals(tech_result):
        """Formats technical timing data into a compact string for the prompt."""
        if not tech_result:
            return "Technicals: N/A"
        line = (
            f"Technicals: {tech_result['signal']} | "
            f"Trend: {tech_result['trend']} (Price {tech_result['price_vs_sma200']:+.1f}% from SMA200) | "
            f"RSI: {tech_result['rsi']:.0f} | "
            f"ATR Compression: {tech_result['atr_compression']:.2f}"
        )
        zone = tech_result.get('accumulation_zone')
        if zone and len(zone) >= 2:
            if not (math.isnan(zone[0]) or math.isnan(zone[1])):
                line += f" | Accumulation Zone: ${int(zone[0])}-${int(zone[1])}"
        return line

    @staticmethod
    def _format_edge(edge_result):
        """Formats 6M directional edge data into a compact string for the prompt."""
        if not edge_result:
            return "6M Directional Edge: N/A"
        return (
            f"6M Directional Edge: {edge_result['best_edge']} | "
            f"Bull: {edge_result['bull_acc']:.0f}% acc, {edge_result['bull_pnl']:+.1f}% avg P&L ({edge_result['bull_n']} signals) | "
            f"Bear: {edge_result['bear_acc']:.0f}% acc, {edge_result['bear_pnl']:+.1f}% avg P&L ({edge_result['bear_n']} signals)"
        )

    @staticmethod
    def build_prompt(tickers_data, grader_data=None, idx_name="SPY", bs_data=None, tech_data=None, edge_data=None,
                     portfolio_summary=None):
        """Builds single combined prompt for sentiment + per-ticker narratives.

        When ``portfolio_summary`` (a pre-formatted weighted-metrics block) is
        given, the dataset is treated as a holder's portfolio: the SENTIMENT
        line becomes a one-line verdict on the portfolio as a whole and each
        ticker is analysed as an existing position.
        """
        from engine.lynch_pin_core import _growth_decay, _scenario_pegs
        context_lines = []
        for d in tickers_data:
            ticker = d['Ticker'].replace('*', '')
            try:
                growth_val = float(d['5YGrowth'].replace('%', ''))
                mean_peg_val = float(d['Mean'])
                curr_peg_val = float(d['PEG'])
                dev_val = float(d['Dev_SD'])
                std_val = abs(curr_peg_val - mean_peg_val) / abs(dev_val) if dev_val else 0.0
                decay = _growth_decay(growth_val)
                terminal_growth = growth_val ** decay
                # Same scenario logic as the engine, so the "Base ROI math" matches the Base ROI shown
                _, t_peg, _ = _scenario_pegs(growth_val, mean_peg_val, curr_peg_val, std_val)
                implied_pe = t_peg * terminal_growth
            except (ValueError, TypeError, ZeroDivisionError):
                growth_val, t_peg, terminal_growth, implied_pe = 0, 0, 0, 0
            weight = d.get('Weight')
            w_tag = f" [{float(weight) * 100:.1f}% of portfolio]" if weight is not None else ""
            line = (
                f"- {d['Ticker']}{w_tag}: PE {d['PE']}, FwdPE {d['FwdPE']}, 2YFwd {d['2YFwd']}, "
                f"Growth {d['5YGrowth']}, PEG {d['PEG']} (Hist Mean: {d['Mean']}, Dev: {d['Dev_SD']} SD). "
                f"ROI Projections: Bull {d['Bull']}, Base {d['Base']}, Bear {d['Bear']}. "
                f"Base ROI math: EPS compounds at {d['5YGrowth']}/yr for 5 years, "
                f"terminal growth decays to {terminal_growth:.1f}% (decay {decay}), "
                f"terminal PEG {t_peg:.2f} × {terminal_growth:.1f}% = {implied_pe:.0f}x implied PE. "
                f"Current FwdPE is {d['FwdPE']}x → re-rates to {implied_pe:.0f}x at maturity."
            )
            if grader_data and ticker in grader_data:
                line += "\n" + LynchPinResearcher._format_grader(grader_data[ticker])
            if bs_data and ticker in bs_data:
                line += "\n" + LynchPinResearcher._format_balance_sheet(bs_data[ticker])
            if tech_data and ticker in tech_data:
                line += "\n" + LynchPinResearcher._format_technicals(tech_data[ticker])
            if edge_data and ticker in edge_data:
                line += "\n" + LynchPinResearcher._format_edge(edge_data[ticker])
            context_lines.append(line)

        context = "\n\n".join(context_lines)

        daily_ticker_task = """SECTION 2 — PER-TICKER ANALYSIS:
For EACH ticker provide three labeled paragraphs. Start each block with a header line that is
ONLY the cashtag of that ticker followed by a colon — e.g. "$AAPL:" for AAPL. Never write the
literal word TICKER in the header.

$<cashtag>:
🤖: [Overview: STRICT MAX 250 characters. This is the tweet preview before "show more".
2-3 SHORT sentences. Conviction vs Risk. Use valuation + Income Grade.
If waterfall accelerating (A/A+) = "sleep well" compounder.
If costs bloating (RED) = flag what could go wrong.
If PEG low but grade poor = trap vs opportunity.]

📊 Reverse DCF: [CITE ALL NUMBERS from "Base ROI math" in the dataset. Be concise but include
every important number. Structure: (1) What the company does and its competitive moat.
(2) The math: "X% base ROI requires EPS to compound at Y%/yr for 5 years, re-rating
from current Mx FwdPE to Nx implied PE at maturity." Do NOT mention decay exponents,
terminal PEG formulas, or intermediate calculation steps — just state the final implied PE.
(3) What this means operationally — specific revenue growth,
margin targets, market share gains needed. (4) Your verdict: is this realistic, achievable,
or a stretch given current trajectory? Use numbers freely, don't be vague.]

🧪 Stomach Test: [The specific bear thesis. Be concise but thorough.
Why could this company underperform the market for 5 years? What keeps you up at night?
Be specific — real risks, not generic disclaimers. Include numbers where relevant.
Factor in balance sheet health: if credit rating is high (AA+/AAA), note the fortress balance sheet
as a mitigating factor. If rating is low (BBB or below), flag debt burden as a key risk.
Reference specific metrics like interest coverage, net debt/EBITDA, or debt service/FCF when relevant.
If Technicals show BEARISH or price is below SMA200, warn about catching a falling knife.
If ACCUMULATION signal is present, note the favorable entry timing.
If 6M Directional Edge data is available, incorporate it:
  - If BULL edge (>60% accuracy): note this supports selling cash-secured puts on dips for income.
  - If BEAR edge (>60% accuracy): note this supports selling covered calls on bounces for income.
  - If neither direction has >55% accuracy, flag as low-conviction for options income.]"""

        if portfolio_summary:
            header = (f"PORTFOLIO (positions listed in descending order of market-value weight; "
                      f"weights shown, no dollar amounts):\n{portfolio_summary}")
            sentiment_task = (
                "SECTION 1 — PORTFOLIO VERDICT:\n"
                "First a one-line verdict (100-150 characters) on this PORTFOLIO as a whole: is it priced "
                "for GARP, how concentrated/quality is it, and what is the single biggest thing the holder "
                "should watch? Then a portfolio-level bull and bear thesis, each 3-5 sentences (roughly "
                "500-700 characters), weighing positions by their weight — the top holdings drive the verdict. "
                "Cover concentration, sector overlap, the weighted PEG vs its historical mean, weighted base ROI "
                "vs what an index would give, and the weighted Income Grade / Credit Rating. Name tickers where "
                "it helps, but do NOT prefix them with $ inside these paragraphs.\n\n"
                "Use EXACTLY this layout for Section 1:\n\n"
                "SENTIMENT: [your one-line verdict here]\n\n"
                "PORTFOLIO:\n"
                "🐂 Bull: [portfolio bull thesis]\n\n"
                "🐻 Bear: [portfolio bear thesis]"
            )
            ticker_task = daily_ticker_task  # per-position replies use the same format as the index scan
        else:
            header = f"INDEX: ${idx_name}"
            sentiment_task = (
                "SECTION 1 — SENTIMENT (one line, 100-150 characters):\n"
                f"Describe current market sentiment for ${idx_name} sector this week.\n"
                "What's driving price action? Outperforming or underperforming? Dominant narrative?\n\n"
                "SENTIMENT: [your one-line summary here]"
            )
            ticker_task = daily_ticker_task

        prompt = f"""Act as Peter Lynch writing a high-signal Twitter thread for value investors.

{header}

DATASET:
{context}

TASK:
Produce the following output in EXACT format:

{sentiment_task}

{ticker_task}

Separate each ticker block with a double newline.
Tone: Wise, slightly witty, Peter Lynch talking to a friend over coffee.
Do NOT use markdown formatting. Plain text only."""

        return prompt

    @staticmethod
    def normalize_narrative(text, tickers):
        """Coerces loosely formatted model output into the layout ``main.py`` parses.

        ``main.py`` anchors on a line-start ``$TICKER`` header per block and a
        ``SENTIMENT:`` label. Gemini follows the template; the small free models
        served by ``openrouter/free`` sometimes copy it literally (``$TICKER: ARM``),
        use bare ``ARM:`` headers, wrap the header in markdown bold, or drop the
        ``SENTIMENT:`` label entirely. Each of those is rewritten here so a fallback
        run still produces per-ticker replies instead of the generic placeholder.
        """
        if not text or text.startswith("AI Research Error"):
            return text
        syms = sorted({t.replace('*', '') for t in tickers if t}, key=len, reverse=True)
        if syms:
            alt = "|".join(re.escape(s) for s in syms)
            # "$TICKER: ARM" / "TICKER: ARM" / "Ticker - ARM" / "**$TICKER: ARM**"  →  "$ARM:"
            text = re.sub(rf"^[ \t*#]*\$?TICKER[ \t]*[:\-—]?[ \t]*\$?({alt})\b[ \t*:]*$",
                          r"$\1:", text, flags=re.MULTILINE | re.IGNORECASE)
            # bare "ARM:" / "ARM" / "**$ARM**" header line (case-sensitive: ON must not match prose)
            text = re.sub(rf"^[ \t*#]*\$?({alt})[ \t*:]*$", r"$\1:", text, flags=re.MULTILINE)
        # Template brackets copied literally: "🤖: [Overview: text]" / "📊 Reverse DCF: [text]" → drop the
        # brackets and the placeholder label. Content may wrap lines but never crosses a blank line,
        # a ticker header or another section label (so an unclosed bracket can't swallow the next section).
        text = re.sub(
            r"^(🤖:|📊 Reverse DCF:|🧪 Stomach Test:)[ \t]*\[(?:Overview:[ \t]*)?"
            r"((?:[^\n]|\n(?![\n$🤖📊🧪]))*?)\][ \t]*$",
            r"\1 \2", text, flags=re.MULTILINE,
        )
        if not re.search(r"SENTIMENT:", text):
            # Label the first prose line before the first ticker block as the sentiment.
            # Only when ticker blocks exist — a reply with none is garbage (e.g. a safety
            # classifier answering "User Safety: safe") and must not become the headline.
            first_hdr = re.search(r"^\$[A-Z]", text, re.MULTILINE)
            if first_hdr:
                for line in text[:first_hdr.start()].splitlines():
                    s = line.strip()
                    if s and not re.match(r"^(PORTFOLIO|SECTION|INDEX|🐂|🐻)", s):
                        text = text.replace(line, f"SENTIMENT: {s}", 1)
                        break
        return text

    @staticmethod
    def _covered_tickers(text, tickers):
        """Tickers that have a ``$TICKER`` block containing the 🤖 overview in (normalized) ``text``."""
        covered = []
        for s in (t.replace('*', '') for t in tickers if t):
            m = re.search(rf"^\${re.escape(s)}\b:?[ \t]*\n?(.*?)(?=\n\$[A-Z]|\Z)", text, re.DOTALL | re.MULTILINE)
            if m and "🤖" in m.group(1):
                covered.append(s)
        return covered

    @staticmethod
    def narrative_coverage(text, tickers):
        """Number of tickers with a usable block — the score ``_call_ai`` ranks rejected replies by.

        Zero for a reply with no ticker blocks at all (safety-classifier verdicts, empty
        text), so such a reply is never chosen as the "most complete" fallback.
        """
        if not text or text.startswith("AI Research Error"):
            return 0
        return len(LynchPinResearcher._covered_tickers(text, tickers))

    @staticmethod
    def narrative_gaps(text, tickers, min_ticker_ratio=1.0):
        """Returns ``None`` if ``text`` is a usable batch narrative, else a short reason.

        A usable reply has a ``SENTIMENT:`` line and, for at least ``min_ticker_ratio`` of
        the tickers, a ``$TICKER`` block that contains the 🤖 overview. Anything else —
        a safety-classifier verdict, a truncated reply, a model that skipped half the
        names — is rejected so ``_call_ai`` can spend another attempt instead of letting
        ``main.py`` post placeholders. Expects normalized text.
        """
        if not text or text.startswith("AI Research Error"):
            return "empty reply"
        syms = [t.replace('*', '') for t in tickers if t]
        covered_set = set(LynchPinResearcher._covered_tickers(text, syms))
        missing = [s for s in syms if s not in covered_set]
        reasons = []
        if not re.search(r"^SENTIMENT:[ \t]*\S", text, re.MULTILINE):
            reasons.append("no SENTIMENT line")
        covered = len(syms) - len(missing)
        if syms and covered < math.ceil(min_ticker_ratio * len(syms)):
            reasons.append(f"{covered}/{len(syms)} ticker blocks (missing: {', '.join(missing)})")
        return "; ".join(reasons) or None

    def get_batch_narrative(self, tickers_data, grader_data=None, idx_name="SPY", bs_data=None, tech_data=None,
                            edge_data=None, portfolio_summary=None):
        """Single API call: returns sentiment + all per-ticker narratives.

        Replies that fail ``narrative_gaps`` after normalization are treated as failed
        attempts by ``_call_ai`` and retried.
        """
        prompt = self.build_prompt(tickers_data, grader_data, idx_name, bs_data, tech_data, edge_data,
                                   portfolio_summary=portfolio_summary)
        tickers = [d['Ticker'] for d in tickers_data]
        norm = lambda t: self.normalize_narrative(t, tickers)
        raw = self._call_ai(prompt,
                            check=lambda t: self.narrative_gaps(norm(t), tickers),
                            score=lambda t: self.narrative_coverage(norm(t), tickers))
        return self.normalize_narrative(raw, tickers)

    def get_fintwit_trending(self):
        """Fetches top 100 most discussed stocks on FinTwit this week via Gemini."""
        prompt = (
            'Print EXACTLY a single column (no line numbers, no repetitive tickers) of 100 of the '
            'most frequently discussed, trending, and highly active stocks of companies commonly '
            'discussed on "FinTwit" (Financial X.com) THIS WEEK (no ETF / index funds or other assets)'
        )
        raw = self._call_ai(prompt)
        if not raw or "Error" in raw:
            return []
        # Parse tickers: handle comma/space/tab separated or one-per-line, strip numbering
        import re
        tokens = re.split(r'[,\s]+', raw.strip())
        seen = set()
        tickers = []
        for t in tokens:
            t = re.sub(r'^\d+[.)\-:]?', '', t).strip().upper()
            if t.isalpha() and 1 <= len(t) <= 5 and t not in seen:
                seen.add(t)
                tickers.append(t)
        return tickers[:100]
