import yfinance as yf
import pandas as pd
import numpy as np
import warnings
import time
import json
from datetime import datetime
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

# SEC EDGAR config
_SEC_HEADERS = {'User-Agent': 'LynchPin/1.0 (contact@example.com)'}
_SEC_SESSION = Session(impersonate="chrome", timeout=15, verify=True)
_CIK_MAP = None  # Lazy-loaded ticker->CIK map
_CIK_MAP_LOADED = False

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
        """Derives forward growth rate from multiple data points.
        Prefers 5Y analyst consensus (via pegRatio), falls back to
        2Y analyst CAGR, then backward-looking metrics."""
        # 1. Yahoo PEG-derived 5Y growth (reverse-engineered analyst consensus)
        peg = self.info.get('pegRatio')
        if peg and peg > 0 and fwd_pe and fwd_pe > 0:
            g = fwd_pe / peg
            if 3 < g < 80: return g

        # 2. Analyst consensus 2Y EPS CAGR (proxy for sustained growth)
        try:
            ee = self.ticker.earnings_estimate
            if ee is not None and '0y' in ee.index and '+1y' in ee.index:
                g0 = ee.loc['0y', 'growth']
                g1 = ee.loc['+1y', 'growth']
                if pd.notna(g0) and pd.notna(g1):
                    cagr = ((1 + g0) * (1 + g1)) ** 0.5 - 1
                    if 0.03 < cagr < 0.8:
                        return cagr * 100
        except Exception:
            pass
        
        # 3. Backward-looking earnings/revenue growth
        eg = self.info.get('earningsGrowth')
        if eg and 0.03 < eg < 1.5: return eg * 100
            
        rg = self.info.get('revenueGrowth')
        if rg and 0.03 < rg < 1.5: return rg * 100
            
        # 4. EPS delta
        if fwd_eps and eps and eps > 0.5:
            g = ((fwd_eps / eps) - 1) * 100
            if 3 < g < 80: return g
        return 0

    @staticmethod
    def _get_cik(ticker):
        """Resolves ticker to SEC CIK via official SEC ticker map."""
        global _CIK_MAP, _CIK_MAP_LOADED
        if not _CIK_MAP_LOADED:
            try:
                resp = _SEC_SESSION.get('https://www.sec.gov/files/company_tickers.json',
                                        headers=_SEC_HEADERS)
                data = resp.json()
                _CIK_MAP = {v['ticker'].upper(): str(v['cik_str']).zfill(10) for v in data.values()}
                _CIK_MAP_LOADED = True
            except Exception:
                _CIK_MAP = {}
        return _CIK_MAP.get(ticker.upper())

    @staticmethod
    def _get_sec_quarterly_eps(cik):
        """Fetches single-quarter diluted EPS from SEC EDGAR XBRL.
        For each period, keeps the most recently filed value."""
        resp = _SEC_SESSION.get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json',
                                headers=_SEC_HEADERS)
        data = resp.json()
        entries = data['facts']['us-gaap']['EarningsPerShareDiluted']['units']
        for unit_key in ['USD/shares', 'EUR/shares', 'GBP/shares', 'JPY/shares']:
            if unit_key in entries:
                entries = entries[unit_key]
                break
        else:
            entries = list(entries.values())[0] if entries else []

        seen = {}  # date -> (val, filed_date)
        for e in entries:
            if e.get('form') in ('10-Q', '10-K') and e.get('start') and e.get('filed'):
                days = (datetime.strptime(e['end'], '%Y-%m-%d') -
                        datetime.strptime(e['start'], '%Y-%m-%d')).days
                if days < 120:
                    d = e['end']
                    filed = e['filed']
                    if d not in seen or filed > seen[d][1]:
                        seen[d] = (e['val'], filed)

        result = {d: v for d, (v, _) in seen.items()}
        s = pd.Series(result).sort_index()
        return s[s > 0]

    def _build_ttm_eps_from_sec(self):
        """Builds dense TTM EPS series from SEC EDGAR quarterly data.
        Uses most recently filed value per period, then detects and fixes
        unrestated split discontinuities."""
        time.sleep(0.12)
        cik = self._get_cik(self.symbol)
        if not cik:
            return None

        time.sleep(0.12)
        qeps = self._get_sec_quarterly_eps(cik)
        if len(qeps) < 4:
            return None

        qeps.index = pd.to_datetime(qeps.index)

        # Fix split discontinuities: if consecutive quarters jump by ~split ratio,
        # divide the larger side to normalize
        try:
            splits = self.ticker.splits
            splits = splits[splits > 0]
            if not splits.empty:
                splits.index = splits.index.tz_localize(None)
                for split_date, ratio in splits.items():
                    if ratio < 1.5:
                        continue
                    pre = qeps[qeps.index < split_date]
                    post = qeps[qeps.index >= split_date]
                    if len(pre) == 0 or len(post) == 0:
                        continue
                    # Check if pre-split values are ~ratio times larger than post
                    pre_med = pre.tail(4).median()
                    post_med = post.head(4).median()
                    if post_med > 0 and pre_med / post_med > ratio * 0.4:
                        qeps.loc[qeps.index < split_date] /= ratio
        except Exception:
            pass

        ttm = qeps.rolling(4).sum().dropna()
        ttm = ttm[ttm > 0]
        return ttm if len(ttm) >= 4 else None

    def _build_ttm_eps_from_yfinance(self):
        """Builds TTM EPS from yfinance annual + quarterly income statements."""
        eps_points = {}
        inc = self.ticker.income_stmt
        qinc = self.ticker.quarterly_income_stmt

        # Annual EPS = direct TTM values (always split-adjusted)
        if inc is not None and 'Diluted EPS' in inc.index:
            for dt, val in inc.loc['Diluted EPS'].dropna().items():
                eps_points[dt] = val

        # Quarterly rolling 4Q sum = TTM between annual dates
        if qinc is not None and 'Diluted EPS' in qinc.index:
            qeps = qinc.loc['Diluted EPS'].sort_index().dropna()
            if len(qeps) >= 4:
                for dt, val in qeps.rolling(4).sum().dropna().items():
                    if dt not in eps_points:  # don't overwrite annual
                        eps_points[dt] = val

        if len(eps_points) < 2:
            return None

        ttm = pd.Series(eps_points).sort_index()
        ttm = ttm[~ttm.index.duplicated(keep='first')]
        return ttm

    def calculate_peg_statistics(self, curr_peg, growth_pct):
        """Calculates Mean PEG and SD based on 5Y history.
        Uses SEC EDGAR for dense quarterly EPS, falls back to yfinance."""
        fallback = (curr_peg, curr_peg * 0.2, 0.0)

        try:
            hist = self.ticker.history(period="5y", interval="1mo")
            if hist.empty or len(hist) < 12:
                return fallback

            # Build TTM EPS from SEC EDGAR + yfinance combined
            ttm_sec = None
            try:
                ttm_sec = self._build_ttm_eps_from_sec()
            except Exception:
                pass
            ttm_yf = self._build_ttm_eps_from_yfinance()

            # Merge: yfinance is always split-adjusted (anchor), SEC adds density
            # On overlap, prefer yfinance to avoid split issues
            price_start = hist.index[0].tz_localize(None)
            if ttm_sec is not None and ttm_yf is not None:
                ttm_sec = ttm_sec[ttm_sec.index >= price_start - pd.DateOffset(years=1)]
                combined = pd.concat([ttm_yf, ttm_sec])  # yfinance first = preferred on dupes
                combined = combined[~combined.index.duplicated(keep='first')]
                ttm_eps = combined.sort_index()
            elif ttm_sec is not None:
                ttm_eps = ttm_sec[ttm_sec.index >= price_start - pd.DateOffset(years=1)]
            elif ttm_yf is not None:
                ttm_eps = ttm_yf
            else:
                return fallback

            if len(ttm_eps) < 2: return fallback

            close = hist['Close'].copy()
            close.index = close.index.tz_localize(None).to_period('M')
            close = close.groupby(level=0).last()

            ttm_eps.index = pd.to_datetime(ttm_eps.index).to_period('M')
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

            # High-growth, high-PEG, or insufficient historical data
            risk = growth_pct > 40 or curr_peg >= 2.5 or dev_sd == 0.0

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
