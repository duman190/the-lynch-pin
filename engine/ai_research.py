import time
from google import genai
import os


class LynchPinResearcher:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self.model_id = "gemini-2.5-flash"

    def _call_gemini(self, prompt, retries=5, delay=30):
        """Calls Gemini with retry logic for 503s."""
        for attempt in range(retries):
            try:
                response = self.client.models.generate_content(
                    model=self.model_id,
                    contents=prompt
                )
                return response.text
            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    if attempt < retries - 1:
                        print(f"⚠️  AI Busy (503). Retrying in {delay}s... (Attempt {attempt + 1}/{retries})")
                        time.sleep(delay)
                        continue
                return f"AI Research Error: {str(e)}"

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
    def build_prompt(tickers_data, grader_data=None, idx_name="SPY"):
        """Builds single combined prompt for sentiment + per-ticker narratives."""
        context_lines = []
        for d in tickers_data:
            ticker = d['Ticker'].replace('*', '')
            # Implied target PE = Mean PEG × growth
            try:
                growth_val = float(d['5YGrowth'].replace('%', ''))
                implied_pe = float(d['Mean']) * growth_val
            except (ValueError, TypeError):
                growth_val, implied_pe = 0, 0
            line = (
                f"- {d['Ticker']}: PE {d['PE']}, FwdPE {d['FwdPE']}, 2YFwd {d['2YFwd']}, "
                f"Growth {d['5YGrowth']}, PEG {d['PEG']} (Hist Mean: {d['Mean']}, Dev: {d['Dev_SD']} SD). "
                f"ROI Projections: Bull {d['Bull']}, Base {d['Base']}, Bear {d['Bear']}. "
                f"Base ROI math: EPS must compound at {d['5YGrowth']}/yr for 5 years, "
                f"then stock re-rates to {implied_pe:.0f}x PE (Mean PEG {d['Mean']} × {d['5YGrowth']} growth). "
                f"Current PE is {d['FwdPE']}x → needs to expand to {implied_pe:.0f}x while earnings grow."
            )
            if grader_data and ticker in grader_data:
                line += "\n" + LynchPinResearcher._format_grader(grader_data[ticker])
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
(2) The math: "X% base ROI requires EPS to compound at Y%/yr for 5 years while PE re-rates
from current Zx to implied Wx." (3) What this means operationally — specific revenue growth,
margin targets, market share gains needed. (4) Your verdict: is this realistic, achievable,
or a stretch given current trajectory? Use numbers freely, don't be vague.]

🧪 Stomach Test: [The specific bear thesis. Be concise but thorough.
Why could this company underperform the market for 5 years? What keeps you up at night?
Be specific — real risks, not generic disclaimers. Include numbers where relevant.]

Separate each ticker block with a double newline.
Tone: Wise, slightly witty, Peter Lynch talking to a friend over coffee.
Do NOT use markdown formatting. Plain text only."""

        return prompt

    def get_batch_narrative(self, tickers_data, grader_data=None, idx_name="SPY"):
        """Single API call: returns sentiment + all per-ticker narratives."""
        prompt = self.build_prompt(tickers_data, grader_data, idx_name)
        return self._call_gemini(prompt)
