import time
from google import genai
import os


class LynchPinResearcher:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self.model_id = "gemini-2.5-flash"

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
    def build_prompt(tickers_data, grader_data=None):
        """Builds the full prompt. Can be called without API key for preview."""
        context_lines = []
        for d in tickers_data:
            ticker = d['Ticker'].replace('*', '')
            line = (
                f"- {d['Ticker']}: PE {d['PE']}, FwdPE {d['FwdPE']}, 2YFwd {d['2YFwd']}, "
                f"Growth {d['5YGrowth']}, PEG {d['PEG']} (Hist Mean: {d['Mean']}, Dev: {d['Dev_SD']} SD). "
                f"ROI Projections: Bull {d['Bull']}, Base {d['Base']}, Bear {d['Bear']}"
            )
            if grader_data and ticker in grader_data:
                line += "\n" + LynchPinResearcher._format_grader(grader_data[ticker])
            context_lines.append(line)

        context = "\n\n".join(context_lines)

        prompt = f"""Act as Peter Lynch writing a high-signal Twitter thread for value investors.

DATASET:
{context}

TASK:
Provide an overview for EACH ticker. Use BOTH the valuation metrics AND the Income Statement Grade.
Focus on CONVICTION vs RISK — would this keep you up at night or let you sleep well?
- If the waterfall is accelerating (Grade A/A+), emphasize why this is a "sleep well" compounder.
- If costs are bloating (RED items), explain what could go wrong and whether the bear case is real.
- If PEG is low but income grade is poor, flag whether the discount is a trap or an opportunity.
- Avoid repeating raw numbers. Instead, translate them into plain-English conviction signals.
Tone: Wise, slightly witty, Peter Lynch talking to a friend over coffee.
Start each with "$TICKER: " and separate blocks with a double newline.
STRICT LIMIT: Analysis text under 250 characters."""

        return prompt

    def get_batch_narrative(self, tickers_data, grader_data=None, retries=5, delay=30):
        """Analyzes pick metrics with built-in 503 retry logic."""
        prompt = self.build_prompt(tickers_data, grader_data)

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
