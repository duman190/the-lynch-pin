import yfinance as yf
import pandas as pd
import numpy as np
import warnings

# Silence the urllib3/LibreSSL warning on macOS
warnings.filterwarnings("ignore", category=UserWarning, module='urllib3')

def get_lynch_projections(ticker_symbol):
    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info
        
        current_price = info.get('currentPrice')
        # Use earningsGrowth (5yr fwd) if available, or default to 0
        fwd_eps_growth = info.get('earningsGrowth', 0) * 100 
        current_pe = info.get('trailingPE')
        
        # Guard clause for missing or negative data that breaks PEG logic
        if not fwd_eps_growth or not current_pe or fwd_eps_growth <= 0:
            return f"Invalid Data for {ticker_symbol}"

        current_peg = current_pe / fwd_eps_growth
        avg_peg_5yr = 1.6  # Peter Lynch's benchmark (Adjusted for Tech)
        std_dev_peg = 0.3
        
        targets = {
            "Bull": avg_peg_5yr + (std_dev_peg * 0.5), # Mean + 0.5 SD
            "Base": avg_peg_5yr,                       # Mean Reversion
            "Bear": current_peg                        # Current valuation stays
        }
        
        projections = {}
        for scenario, target_peg in targets.items():
            # Target Price = Current Price * (Where PEG should be / Where it is)
            price_target = current_price * (target_peg / current_peg)
            
            # 5-Year Annualized ROI: ((Target / Current)^(1/5)) - 1
            # We check price_target > 0 to avoid complex number errors
            if price_target > 0:
                roi_ratio = price_target / current_price
                annualized_roi = (roi_ratio ** (1/5)) - 1
                
                # Double check we didn't end up with an imaginary number
                if isinstance(annualized_roi, (int, float)):
                    projections[scenario] = f"{round(annualized_roi * 100, 1)}%"
                else:
                    projections[scenario] = "N/A"
            else:
                projections[scenario] = "N/A"
                
        return {
            "Ticker": ticker_symbol,
            "Current_PEG": round(current_peg, 2),
            "ROI": projections
        }

    except Exception as e:
        return f"Error processing {ticker_symbol}: {e}"

if __name__ == "__main__":
    # Your core list plus a few for variety
    indices = ["AMZN", "GOOGL", "MELI", "PYPL", "ADBE", "UBER", "MSFT", "TSLA"]
    
    print(f"\n{'📍 The Lynch Pin':<10} | Analysis Run")
    print(f"{'Ticker':<8} | {'PEG':<5} | {'Bull %':<8} | {'Base %':<8} | {'Bear %':<8}")
    print("-" * 58)

    for t in indices:
        res = get_lynch_projections(t)
        if isinstance(res, dict):
            p = res['ROI']
            print(f"{res['Ticker']:<8} | {res['Current_PEG']:<5} | {p['Bull']:>7} | {p['Base']:>7} | {p['Bear']:>7}")
        else:
            # Prints the error message if data was missing
            print(f"{t:<8} | {'--':<5} | {res}")
