# engine/ai_research.py
from google import genai
import os

class LynchPinResearcher:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        # Using 3.1 for its superior context handling in batch requests
        #self.model_id = "gemini-3.1-flash-lite-preview"
        self.model_id = "gemini-2.5-flash"

    def get_batch_narrative(self, tickers_data):
        """Analyzes all top picks at once for a cohesive market thread."""
        # Build a robust context block with all the metrics
        context_lines = []
        for d in tickers_data:
            line = (
                f"- {d['Ticker']}: PE {d['PE']}, FwdPE {d['FwdPE']}, "
                f"Growth {d['5YGrowth']}, PEG {d['PEG']} (Hist Mean: {d['Mean']}), "
                f"Dev: {d['Dev_SD']} SD. Projected Base ROI: {d['Base']}"
            )
            context_lines.append(line)
        
        context = "\n".join(context_lines)        

        prompt = f"""
        Act as Peter Lynch writing a high-signal Twitter thread for value investors.
        I have a list of stocks currently trading significantly below their 5-year PEG means.
        Try to keep it short and conscise, but explain mispricing. Add some classic Peter Lynch humor. 
        
        DATASET:
        {context}
        
        TASK:
        Describe in a thread (1 tweet per each ticker symbol per tweet why the market consensus for ticker explains mispricing of these stocks.
        Constraints: Free Users: 280 characters per tweet. 
        Tone: Wise, slightly witty, focus on conveying Wall Street narrative.
        """

        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt
            )
            return response.text
        except Exception as e:
            return f"AI Research Error: {str(e)}"
