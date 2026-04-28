import yfinance as yf
import pandas as pd
import numpy as np
import warnings
import time
from curl_cffi.requests import Session

# Silence internal warnings for cleaner logs
warnings.filterwarnings("ignore", category=UserWarning, module='urllib3')
warnings.filterwarnings("ignore", category=FutureWarning)

def get_robust_session():
    """
    Creates a curl_cffi session required by yfinance for browser impersonation.
    Includes built-in retry strategy and timeout for Dark Wake resilience.
    """
    # yfinance strictly requires curl_cffi to bypass Yahoo's anti-bot measures.
    # 'impersonate="chrome"' makes the request look like a standard browser.
    session = Session(
        impersonate="chrome",
        timeout=30,      # Give Yahoo 30s before timing out
        verify=True
    )
    return session

# Global session reused for all tickers to keep the TCP/SSL connection alive
GLOBAL_SESSION = get_robust_session()

class LynchPinEngine:
    def __init__(self, symbol):
        self.symbol = symbol
        # Pass the robust curl_cffi session directly to yfinance
        self.ticker = yf.Ticker(symbol, session=GLOBAL_SESSION)
        
        # Primary data fetch - wrapped in try/except for network stability
        try:
            self.info = self.ticker.info
        except Exception:
            self.info = {}

    def _get_growth(self, fwd_pe, fwd_eps, eps):
        """Derives 5Y growth rate from multiple data points."""
        peg = self.info.get('pegRatio')
        if peg and peg > 0 and fwd_pe and fwd_pe > 0:
            g = fwd_pe / peg
            if 3 < g < 80: return g
        
        eg = self.info.get('earningsGrowth')
        if eg and 0.03 < eg < 1.5: return eg * 100
            
        rg = self.info.get('revenueGrowth')
        if rg and 0.03 < rg < 1.5: return rg * 100
            
        if fwd_eps and eps and eps > 0.5:
            g = ((fwd_eps / eps) - 1) * 100
            if 3 < g < 80: return g
        return 0

    def calculate_peg_statistics(self, curr_peg, growth_pct):
        """Calculates Mean PEG and SD based on 5Y history with error handling."""
        # Standard fallback if historical data fetch fails
        fallback = (curr_peg, curr_peg * 0.2, 0.0)

        try:
            # Multi-call section: if these fail, we fall back to a current-day estimate
            inc = self.ticker.income_stmt
            qinc = self.ticker.quarterly_income_stmt
            hist = self.ticker.history(period="5y", interval="1mo")
            
            if hist.empty or len(hist) < 12:
                return fallback

            eps_points = {}
            if inc is not None and 'Diluted EPS' in inc.index:
                for dt, val in inc.loc['Diluted EPS'].dropna().items():
                    eps_points[dt] = val
            
            if qinc is not None and 'Diluted EPS' in qinc.index:
                qeps = qinc.loc['Diluted EPS'].sort_index().dropna()
                if len(qeps) >= 4:
                    for dt, val in qeps.rolling(4).sum().dropna().items():
                        eps_points[dt] = val

            if len(eps_points) < 2: return fallback

            ttm_eps = pd.Series(eps_points).sort_index()
            ttm_eps = ttm_eps[~ttm_eps.index.duplicated(keep='last')]

            close = hist['Close'].copy()
            close.index = close.index.tz_localize(None).to_period('M')
            close = close.groupby(level=0).last()

            ttm_eps.index = ttm_eps.index.to_period('M')
            ttm_eps = ttm_eps.groupby(level=0).last()

            ttm_m = ttm_eps.reindex(close.index, method='ffill').dropna()
            common = ttm_m.index.intersection(close.index)
            
            if len(common) < 12: return fallback

            pe = close[common] / ttm_m[common]
            pe = pe[(pe > 3) & (pe < 300)].dropna()
            
            if len(pe) < 12: return fallback

            peg_series = pe / growth_pct
            mean_peg = float(peg_series.mean())
            std_peg = max(float(peg_series.std()), 0.01)
            dev_sd = (curr_peg - mean_peg) / std_peg
            return mean_peg, std_peg, dev_sd

        except Exception:
            # Silence network errors during historical fetch to allow script to finish
            return fallback

    def get_ticker_stats(self):
        """Main orchestration method for a single symbol."""
        try:
            if not self.info: return None 
            
            curr_price = self.info.get('currentPrice')
            fwd_pe = self.info.get('forwardPE')
            fwd_eps = self.info.get('forwardEps')
            eps = self.info.get('trailingEps')
            curr_pe = self.info.get('trailingPE')

            if not curr_price or not fwd_pe or fwd_pe <= 0:
                return None

            growth_pct = self._get_growth(fwd_pe, fwd_eps, eps)
            if growth_pct <= 0: return None

            curr_peg = fwd_pe / growth_pct
            mean_peg, std_peg, dev_sd = self.calculate_peg_statistics(curr_peg, growth_pct)

            pe_2y = (curr_price / (fwd_eps * (1 + growth_pct / 100))
                     if fwd_eps and fwd_eps > 0 else fwd_pe)

            base_eps = eps if eps and eps > 0 else fwd_eps
            if not base_eps or base_eps <= 0: return None
                
            proj_eps = base_eps * ((1 + growth_pct / 100) ** 5)

            def roi(target_peg):
                pt = target_peg * growth_pct * proj_eps
                return ((pt / curr_price) ** 0.2) - 1 if pt > 0 else -1

            # High-growth or high-PEG risk tagging
            risk = growth_pct > 40 or curr_peg >= 2.5

            return {
                "Ticker": f"{self.symbol}*" if risk else self.symbol,
                "PE": round(curr_pe, 1) if curr_pe else 0,
                "FwdPE": round(fwd_pe, 1),
                "2YFwd": round(pe_2y, 1),
                "5YGrowth": f"{round(growth_pct, 1)}%",
                "PEG": round(curr_peg, 2),
                "Mean": round(mean_peg, 2),
                "Dev_SD": round(dev_sd, 2),
                "Bull": f"{round(roi(min(3, mean_peg + std_peg)) * 100, 1)}%",
                "Base": f"{round(roi(min(2.5, mean_peg)) * 100, 1)}%",
                "Bear": f"{round(roi(min(2, min(curr_peg, mean_peg))) * 100, 1)}%",
            }
        except Exception:
            return None
