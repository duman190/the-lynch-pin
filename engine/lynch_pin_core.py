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


def _growth_decay(growth_pct):
    """Step-function decay: higher growth → more aggressive deceleration assumption."""
    if growth_pct < 20:
        return 1.0
    if growth_pct < 30:
        return 0.95
    if growth_pct < 50:
        return 0.90
    return 0.85


def _terminal_peg(growth_pct, mean_peg):
    """Terminal PEG for ROI projection.
    Mature companies (<20% growth): use min(2.5, mean_peg).
    High-growth companies: reversed formula that penalizes extreme growth."""
    if growth_pct < 20:
        return min(2.5, mean_peg)
    return min(mean_peg, max(0.8, 1.5 - 0.5 * (growth_pct / 30 - 1)))

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

    def _get_growth(self, fwd_pe, fwd_eps, eps, enrich=False):
        """Multi-source 5Y EPS growth estimate via growth_estimator module."""
        from engine.growth_estimator import estimate_growth
        growth_pct, sources = estimate_growth(self.symbol, self.info, self.ticker, fwd_pe, enrich=enrich)
        self._growth_sources = sources
        return growth_pct

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

    def _pe_volatility_fallback(self, curr_peg, growth_pct):
        """Fallback: uses trailing 12M PE volatility as proxy for PEG std."""
        try:
            hist = self.ticker.history(period="1y", interval="1d")
            if hist.empty or len(hist) < 60:
                return (curr_peg, curr_peg * 0.2, 0.0)
            eps = self.info.get('trailingEps')
            if not eps or eps <= 0:
                return (curr_peg, curr_peg * 0.2, 0.0)
            close = hist['Close'].dropna()
            pe_series = close / eps
            pe_series = pe_series[(pe_series > 3) & (pe_series < 300)].dropna()
            if len(pe_series) < 60:
                return (curr_peg, curr_peg * 0.2, 0.0)
            peg_series = pe_series / growth_pct
            mean_peg = float(peg_series.mean())
            std_peg = max(float(peg_series.std()), 0.01)
            dev_sd = (curr_peg - mean_peg) / std_peg
            return mean_peg, std_peg, dev_sd
        except Exception:
            return (curr_peg, curr_peg * 0.2, 0.0)

    def calculate_peg_statistics(self, curr_peg, growth_pct):
        """Calculates Mean PEG and SD based on 5Y history.
        Uses SEC EDGAR for dense quarterly EPS, falls back to yfinance."""

        try:
            hist = self.ticker.history(period="5y", interval="1mo")
            if hist.empty or len(hist) < 12:
                return self._pe_volatility_fallback(curr_peg, growth_pct)

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
                return self._pe_volatility_fallback(curr_peg, growth_pct)

            if len(ttm_eps) < 2: return self._pe_volatility_fallback(curr_peg, growth_pct)

            close = hist['Close'].copy()
            close.index = close.index.tz_localize(None).to_period('M')
            close = close.groupby(level=0).last()

            ttm_eps.index = pd.to_datetime(ttm_eps.index).to_period('M')
            ttm_eps = ttm_eps.groupby(level=0).last()

            ttm_m = ttm_eps.reindex(close.index, method='ffill').dropna()
            common = ttm_m.index.intersection(close.index)

            if len(common) < 12: return self._pe_volatility_fallback(curr_peg, growth_pct)

            pe = close[common] / ttm_m[common]
            pe = pe[(pe > 3) & (pe < 300)].dropna()

            if len(pe) < 12: return self._pe_volatility_fallback(curr_peg, growth_pct)

            peg_series = pe / growth_pct
            mean_peg = float(peg_series.mean())
            std_peg = max(float(peg_series.std()), 0.01)
            dev_sd = (curr_peg - mean_peg) / std_peg
            return mean_peg, std_peg, dev_sd

        except Exception:
            return self._pe_volatility_fallback(curr_peg, growth_pct)

    def get_ticker_stats(self, enrich=False):
        """Main orchestration method for a single symbol.
        Args:
            enrich: If True, uses FMP + Alpha Vantage for growth (top picks only).
        """
        try:
            if not self.info: return None 
            
            curr_price = self.info.get('currentPrice')
            fwd_pe = self.info.get('forwardPE')
            fwd_eps = self.info.get('forwardEps')
            eps = self.info.get('trailingEps')
            curr_pe = self.info.get('trailingPE')

            if not curr_price or not fwd_pe or fwd_pe <= 0:
                return None

            growth_pct = self._get_growth(fwd_pe, fwd_eps, eps, enrich=enrich)
            if growth_pct <= 0: return None

            curr_peg = fwd_pe / growth_pct
            mean_peg, std_peg, dev_sd = self.calculate_peg_statistics(curr_peg, growth_pct)

            pe_2y = (curr_price / (fwd_eps * (1 + growth_pct / 100))
                     if fwd_eps and fwd_eps > 0 else fwd_pe)

            # Use max of forward and trailing EPS as projection base — reflects
            # the higher earnings power the market is pricing in (e.g., AMD's AI
            # shift). Falls back to whichever is available if one is missing/negative.
            if fwd_eps and fwd_eps > 0 and eps and eps > 0:
                base_eps = max(fwd_eps, eps)
            else:
                base_eps = fwd_eps if fwd_eps and fwd_eps > 0 else eps
            if not base_eps or base_eps <= 0: return None
                
            proj_eps = base_eps * ((1 + growth_pct / 100) ** 5)

            # Terminal growth decay: higher growth → more skepticism
            decay = _growth_decay(growth_pct)
            terminal_growth = growth_pct ** decay

            # Terminal PEG: mature companies keep mean, high-growth gets penalized
            terminal_peg = _terminal_peg(growth_pct, mean_peg)

            def roi(target_peg):
                pt = target_peg * terminal_growth * proj_eps
                return ((pt / curr_price) ** 0.2) - 1 if pt > 0 else -1

            # ROI scenarios
            bull_peg = terminal_peg + 0.5 * std_peg
            bear_peg = max(0.5, min(curr_peg, terminal_peg - 0.5 * std_peg))
            base_roi = roi(terminal_peg) * 100
            bull_roi = roi(bull_peg) * 100
            bear_roi = roi(bear_peg) * 100
            risk = growth_pct > 80 or curr_peg >= 2.5 or dev_sd == 0.0 or not curr_pe or curr_pe <= 0 or base_roi < 9.0

            return {
                "Ticker": f"{self.symbol}*" if risk else self.symbol,
                "PE": round(curr_pe, 1) if curr_pe else 0,
                "FwdPE": round(fwd_pe, 1),
                "2YFwd": round(pe_2y, 1),
                "5YGrowth": f"{round(growth_pct, 1)}%",
                "PEG": round(curr_peg, 2),
                "Mean": round(mean_peg, 2),
                "Dev_SD": round(dev_sd, 2),
                "Bull": f"{round(bull_roi, 1)}%",
                "Base": f"{round(base_roi, 1)}%",
                "Bear": f"{round(bear_roi, 1)}%",
            }
        except Exception:
            return None
