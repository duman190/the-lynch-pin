"""Multi-source 5Y EPS growth estimator.

Fast mode (default): Yahoo PEG + fundamental cap only.
Enriched mode (enrich=True): adds FMP analyst estimates for top picks.

Env vars (optional — graceful degradation if missing):
  FMP_API_KEY
"""

import os
import time
import pandas as pd
from curl_cffi.requests import Session

_SESSION = Session(impersonate="chrome", timeout=15, verify=True)

FMP_KEY = os.environ.get("FMP_API_KEY")


def _yahoo_5y_growth(info, fwd_pe):
    """Yahoo PEG-derived 5Y analyst consensus growth."""
    peg = info.get('pegRatio')
    if peg and peg > 0 and fwd_pe and fwd_pe > 0:
        g = fwd_pe / peg
        if 3 < g < 150:
            return g
    return None


def _fmp_5y_growth(symbol):
    """5Y EPS CAGR from Financial Modeling Prep analyst estimates.
    Retries once after 60s on rate limit, gives up on second failure."""
    if not FMP_KEY:
        return None
    for attempt in range(2):
        try:
            url = (f"https://financialmodelingprep.com/stable/analyst-estimates"
                   f"?symbol={symbol}&period=annual&apikey={FMP_KEY}")
            resp = _SESSION.get(url)
            if resp.status_code == 429:
                if attempt == 0:
                    print(f"  ⏳ FMP rate limit — waiting 60s...")
                    time.sleep(60)
                    continue
                return None
            data = resp.json()
            if not data or not isinstance(data, list) or len(data) < 2:
                return None

            eps_estimates = []
            for entry in data:
                eps = entry.get('epsAvg')
                date = entry.get('date', '')
                if eps and eps > 0 and date:
                    eps_estimates.append((date, eps))

            eps_estimates.sort(key=lambda x: x[0])

            if len(eps_estimates) < 2:
                return None

            start = eps_estimates[-6] if len(eps_estimates) >= 6 else eps_estimates[0]
            end = eps_estimates[-1]
            n_years = int(end[0][:4]) - int(start[0][:4])

            if n_years < 2 or start[1] <= 0 or end[1] <= start[1]:
                return None

            cagr = ((end[1] / start[1]) ** (1 / n_years) - 1) * 100
            if 3 < cagr < 150:
                return cagr
        except Exception:
            pass
        return None
    return None


def _fundamental_cap(ticker_obj):
    """Fundamental growth ceiling: Revenue CAGR + margin expansion + buybacks."""
    try:
        inc = ticker_obj.income_stmt
        bs = ticker_obj.balance_sheet
        if inc is None or 'Total Revenue' not in inc.index:
            return None

        rev = inc.loc['Total Revenue'].dropna().sort_index()
        if len(rev) < 3:
            return None

        rev_cagr = (rev.iloc[-1] / rev.iloc[0]) ** (1 / (len(rev) - 1)) - 1

        margin_delta = 0.0
        if 'Net Income' in inc.index:
            ni = inc.loc['Net Income'].dropna().sort_index()
            if len(ni) >= 3 and rev.iloc[0] > 0 and rev.iloc[-1] > 0:
                m_old = ni.iloc[0] / rev.iloc[0]
                m_new = ni.iloc[-1] / rev.iloc[-1]
                margin_delta = (m_new - m_old) / (len(ni) - 1)

        buyback_rate = 0.0
        if bs is not None and 'Ordinary Shares Number' in bs.index:
            shares = bs.loc['Ordinary Shares Number'].dropna().sort_index()
            if len(shares) >= 2 and shares.iloc[0] > 0:
                share_change = (shares.iloc[-1] / shares.iloc[0]) ** (1 / (len(shares) - 1)) - 1
                buyback_rate = max(-share_change, 0)

        cap = (rev_cagr + margin_delta + buyback_rate) * 100
        return cap if cap > 0 else None
    except Exception:
        return None


def _fallback_growth(info, ticker_obj, fwd_pe):
    """Last-resort: short-term metrics when no 5Y source is available."""
    try:
        ee = ticker_obj.earnings_estimate
        if ee is not None and '0y' in ee.index and '+1y' in ee.index:
            g0 = ee.loc['0y', 'growth']
            g1 = ee.loc['+1y', 'growth']
            if pd.notna(g0) and pd.notna(g1):
                compound = (1 + g0) * (1 + g1)
                if compound > 0:
                    cagr = (compound ** 0.5 - 1) * 100
                    if 3 < cagr < 150:
                        return cagr, 'fallback_2y'
    except Exception:
        pass

    eg = info.get('earningsGrowth')
    if eg and 0.03 < eg < 1.5:
        return eg * 100, 'fallback_eg'

    rg = info.get('revenueGrowth')
    if rg and 0.03 < rg < 1.5:
        return rg * 100, 'fallback_rg'

    fwd_eps = info.get('forwardEps')
    eps = info.get('trailingEps')
    if fwd_eps and eps and eps > 0.5:
        g = ((fwd_eps / eps) - 1) * 100
        if 3 < g < 150:
            return g, 'fallback_eps_delta'

    return 0, None


def estimate_growth(symbol, info, ticker_obj, fwd_pe, enrich=False):
    """Blends 5Y growth sources into a single robust estimate.

    Args:
        enrich: If True, calls FMP API (use for top picks only).

    Returns:
        (growth_pct, sources_used) — growth in %, list of source labels.
    """
    five_year_sources = []

    # Source 1: Yahoo PEG-derived 5Y consensus (always available)
    yahoo_g = _yahoo_5y_growth(info, fwd_pe)
    if yahoo_g:
        five_year_sources.append(('yahoo_peg', yahoo_g))

    # Source 2: FMP 5Y forward EPS CAGR (only when enriching)
    if enrich:
        fmp_g = _fmp_5y_growth(symbol)
        if fmp_g:
            five_year_sources.append(('fmp', fmp_g))

    # Simple average across all 5Y sources, then cap-check
    if five_year_sources:
        avg = sum(g for _, g in five_year_sources) / len(five_year_sources)

        # Only haircut if projection > 1.5x historical run rate
        fund_cap = _fundamental_cap(ticker_obj)
        if fund_cap and avg > fund_cap * 1.5:
            avg = avg * 0.6 + fund_cap * 0.4

        sources = [s for s, _ in five_year_sources]
        return (avg if avg > 3 else 0), sources

    # No 5Y sources — use fallbacks with cap validation
    fallback_g, fallback_src = _fallback_growth(info, ticker_obj, fwd_pe)
    if fallback_g > 0:
        fund_cap = _fundamental_cap(ticker_obj)
        if fund_cap and fallback_g > fund_cap * 1.5:
            fallback_g = fallback_g * 0.6 + fund_cap * 0.4
        return fallback_g, [fallback_src]

    return 0, []
