import time
from google import genai
import os


class LynchPinResearcher:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self.best_model = "gemini-3.5-flash"
        self.backup_model = "gemini-2.5-flash"

    def _call_gemini(self, prompt, retries=5, delay=30):
        """Calls Gemini with retry logic for 503s/429s, cascading through models."""
        for attempt in range(retries):
            if attempt < 2:
                current_model = self.best_model
                tier_label = "BEST"
            else:
                current_model = self.backup_model
                tier_label = "BACKUP"

            try:
                response = self.client.models.generate_content(
                    model=current_model,
                    contents=prompt
                )
                return response.text
            except Exception as e:
                error_msg = str(e)
                if "503" in error_msg or "UNAVAILABLE" in error_msg or "429" in error_msg:
                    if attempt < retries - 1:
                        print(f"⚠️  {tier_label} AI Busy ({current_model}). Retrying in {delay}s... (Attempt {attempt + 1}/{retries})")
                        time.sleep(delay)
                        continue
                return f"AI Research Error: {error_msg}"

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
        if zone:
            line += f" | Accumulation Zone: ${int(zone[0])}-${int(zone[1])}"
        return line

    @staticmethod
    def build_prompt(tickers_data, grader_data=None, idx_name="SPY", bs_data=None, tech_data=None):
        """Builds single combined prompt for sentiment + per-ticker narratives."""
        from engine.lynch_pin_core import _growth_decay, _terminal_peg
        context_lines = []
        for d in tickers_data:
            ticker = d['Ticker'].replace('*', '')
            try:
                growth_val = float(d['5YGrowth'].replace('%', ''))
                mean_peg_val = float(d['Mean'])
                decay = _growth_decay(growth_val)
                terminal_growth = growth_val ** decay
                t_peg = _terminal_peg(growth_val, mean_peg_val)
                implied_pe = t_peg * terminal_growth
            except (ValueError, TypeError):
                growth_val, t_peg, terminal_growth, implied_pe = 0, 0, 0, 0
            line = (
                f"- {d['Ticker']}: PE {d['PE']}, FwdPE {d['FwdPE']}, 2YFwd {d['2YFwd']}, "
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
            context_lines.append(line)

        context = "\n\n".join(context_lines)

        prompt = f"""Act as Peter Lynch writing a high-signal Twitter thread for value investors.

INDEX: ${idx_name}

DATASET:
{context}

TASK:
Produce the following output in EXACT format:

SECTION 1 — SENTIMENT (one line, 100-150 characters):
Describe current market sentiment for ${idx_name} sector this week.
What's driving price action? Outperforming or underperforming? Dominant narrative?

SENTIMENT: [your one-line summary here]

SECTION 2 — PER-TICKER ANALYSIS:
For EACH ticker provide three labeled paragraphs:

$TICKER:
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
If ACCUMULATION signal is present, note the favorable entry timing.]

Separate each ticker block with a double newline.
Tone: Wise, slightly witty, Peter Lynch talking to a friend over coffee.
Do NOT use markdown formatting. Plain text only."""

        return prompt

    def get_batch_narrative(self, tickers_data, grader_data=None, idx_name="SPY", bs_data=None, tech_data=None):
        """Single API call: returns sentiment + all per-ticker narratives."""
        prompt = self.build_prompt(tickers_data, grader_data, idx_name, bs_data, tech_data)
        return self._call_gemini(prompt)

    def get_fintwit_trending(self):
        """Fetches top 100 most discussed stocks on FinTwit this week via Gemini."""
        prompt = (
            'Print EXACTLY a single column (no line numbers, no repetitive tickers) of 100 of the '
            'most frequently discussed, trending, and highly active stocks of companies commonly '
            'discussed on "FinTwit" (Financial X.com) THIS WEEK (no ETF / index funds or other assets)'
        )
        raw = self._call_gemini(prompt)
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
