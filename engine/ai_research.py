# engine/ai_research.py
from google import genai
import os

class LynchPinResearcher:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        # Using 3.1 for its superior context handling in batch requests
        self.model_id = "gemini-3.1-flash-lite-preview"

    def get_batch_narrative(self, tickers_data):
        """Analyzes all top picks at once for a cohesive market thread."""
        context = "\n".join([
            f"- {d['Ticker']}: PEG {d['PEG']}, Hist Mean {d['Mean']}, Dev(SD) {d['Dev_SD']}" 
            for d in tickers_data
        ])

        prompt = f"""
        Act as Peter Lynch writing a high-signal Twitter thread for value investors.
        I have a list of stocks currently trading significantly below their 5-year PEG means.
        Try to keep it short and conscise, but explain mispricing. Add some classic Peter Lynch humor. 
        
        DATASET:
        {context}
        
        TASK:
        Describe in a single tweet what is the market consensus that explains mispricing of these stocks.
        
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
