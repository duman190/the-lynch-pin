import time
from google import genai
import os

class LynchPinResearcher:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self.model_id = "gemini-2.5-flash"

    def get_batch_narrative(self, tickers_data, retries=3, delay=10):
        """Analyzes pick metrics with built-in 503 retry logic."""
        context_lines = []
        for d in tickers_data:
            # Expanded context including 2Y Fwd and ROI bands
            line = (
                f"- {d['Ticker']}: PE {d['PE']}, FwdPE {d['FwdPE']}, 2YFwd {d['2YFwd']}, "
                f"Growth {d['5YGrowth']}, PEG {d['PEG']} (Hist Mean: {d['Mean']}, Dev: {d['Dev_SD']} SD). "
                f"ROI Projections: Bull {d['Bull']}, Base {d['Base']}, Bear {d['Bear']}"
            )
            context_lines.append(line)
        
        context = "\n".join(context_lines)

        prompt = f"""
        Act as Peter Lynch writing a high-signal Twitter thread for value investors.
        
        DATASET:
        {context}
        
        TASK:
        Provide an overview for EACH ticker, focus less on number and MORE on market sentiment.
        Tone: Wise, slightly witty, focus on justifying whether mispricing is justified!
        Start each with "$TICKER: " and separate blocks with a double newline.
        STRICT LIMIT: Analysis text under 220 characters.
        """

        for attempt in range(retries):
            try:
                response = self.client.models.generate_content(
                    model=self.model_id,
                    contents=prompt
                )
                return response.text
            except Exception as e:
                # Check for 503 (High Demand)
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    if attempt < retries - 1:
                        print(f"⚠️  AI Busy (503). Retrying in {delay}s... (Attempt {attempt + 1}/{retries})")
                        time.sleep(delay)
                        continue
                return f"AI Research Error: {str(e)}"
