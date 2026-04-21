import yfinance as yf
import pandas as pd
import numpy as np
import warnings
import argparse

warnings.filterwarnings("ignore", category=UserWarning, module='urllib3')
warnings.filterwarnings("ignore", category=FutureWarning)


def _get_growth(info, fwd_pe, fwd_eps, eps):
    peg = info.get('pegRatio')
    if peg and peg > 0 and fwd_pe and fwd_pe > 0:
        g = fwd_pe / peg
        if 3 < g < 80:
            return g
    eg = info.get('earningsGrowth')
    if eg and 0.03 < eg < 1.5:
        return eg * 100
    rg = info.get('revenueGrowth')
    if rg and 0.03 < rg < 1.5:
        return rg * 100
    if fwd_eps and eps and eps > 0.5:
        g = ((fwd_eps / eps) - 1) * 100
        if 3 < g < 80:
            return g
    return 0


def _historical_peg(t, curr_peg, growth_pct):
    fallback = (curr_peg, curr_peg * 0.2, 0.0)

    inc = t.income_stmt
    qinc = t.quarterly_income_stmt
    eps_points = {}

    if inc is not None and 'Diluted EPS' in inc.index:
        for dt, val in inc.loc['Diluted EPS'].dropna().items():
            eps_points[dt] = val
    if qinc is not None and 'Diluted EPS' in qinc.index:
        qeps = qinc.loc['Diluted EPS'].sort_index().dropna()
        if len(qeps) >= 4:
            for dt, val in qeps.rolling(4).sum().dropna().items():
                eps_points[dt] = val

    if len(eps_points) < 2:
        return fallback

    ttm_eps = pd.Series(eps_points).sort_index()
    ttm_eps = ttm_eps[~ttm_eps.index.duplicated(keep='last')]

    hist = t.history(period="5y", interval="1mo")
    if hist.empty or len(hist) < 12:
        return fallback

    close = hist['Close'].copy()
    close.index = close.index.tz_localize(None).to_period('M')
    close = close.groupby(level=0).last()

    ttm_eps.index = ttm_eps.index.to_period('M')
    ttm_eps = ttm_eps.groupby(level=0).last()

    ttm_m = ttm_eps.reindex(close.index, method='ffill').dropna()
    common = ttm_m.index.intersection(close.index)
    if len(common) < 12:
        return fallback

    pe = close[common] / ttm_m[common]
    pe = pe[(pe > 3) & (pe < 300)].dropna()
    if len(pe) < 12:
        return fallback

    peg_series = pe / growth_pct
    mean_peg = float(peg_series.mean())
    std_peg = max(float(peg_series.std()), 0.01)
    dev_sd = (curr_peg - mean_peg) / std_peg
    return mean_peg, std_peg, dev_sd


def get_ticker_stats(symbol):
    try:
        t = yf.Ticker(symbol)
        info = t.info

        curr_price = info.get('currentPrice')
        fwd_pe = info.get('forwardPE')
        fwd_eps = info.get('forwardEps')
        eps = info.get('trailingEps')
        curr_pe = info.get('trailingPE')

        if not curr_price or not fwd_pe or fwd_pe <= 0:
            return None

        growth_pct = _get_growth(info, fwd_pe, fwd_eps, eps)
        if growth_pct <= 0:
            return None

        curr_peg = fwd_pe / growth_pct
        mean_peg, std_peg, dev_sd = _historical_peg(t, curr_peg, growth_pct)

        pe_2y = (curr_price / (fwd_eps * (1 + growth_pct / 100))
                 if fwd_eps and fwd_eps > 0 else fwd_pe)

        base_eps = eps if eps and eps > 0 else fwd_eps
        if not base_eps or base_eps <= 0:
            return None
        proj_eps = base_eps * ((1 + growth_pct / 100) ** 5)

        def roi(target_peg):
            pt = target_peg * growth_pct * proj_eps
            return ((pt / curr_price) ** 0.2) - 1 if pt > 0 else -1

        risk = growth_pct > 40 or curr_peg >= 2.5

        return {
            "Ticker": f"{symbol}*" if risk else symbol,
            "PE": round(curr_pe, 1) if curr_pe else 0,
            "FwdPE": round(fwd_pe, 1),
            "2YFwd": round(pe_2y, 1),
            "5YGrowth": f"{round(growth_pct, 1)}%",
            "PEG": round(curr_peg, 2),
            "Mean": round(mean_peg, 2),
            "Dev_SD": round(dev_sd, 2),
            "Bull": f"{round(roi(min(3,mean_peg + std_peg)) * 100, 1)}%",
            "Base": f"{round(roi(min(3,mean_peg)) * 100, 1)}%",
            "Bear": f"{round(roi(min(3,min(curr_peg, mean_peg))) * 100, 1)}%",
        }
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=str, default="nasdaq_100.txt")
    args = parser.parse_args()

    with open(args.src, 'r') as f:
        tickers = [line.strip() for line in f if line.strip()]

    all_data = []
    for s in tickers:
        result = get_ticker_stats(s)
        if result:
            all_data.append(result)

    df = pd.DataFrame(all_data).sort_values(by='Dev_SD')

    header = (f"{'Ticker':<9} | {'PE':<6} | {'FwdPE':<7} | {'2YFwd':<7} | "
              f"{'5YGrowth':<8} | {'PEG':<6} | {'Mean':<6} | {'Dev(SD)':<7} | "
              f"{'Bull':>8} | {'Base':>8} | {'Bear':>8}")
    print(header + "\n" + "-" * len(header))
    for _, r in df.iterrows():
        print(f"{r['Ticker']:<9} | {r['PE']:>6.1f} | {r['FwdPE']:>7.1f} | "
              f"{r['2YFwd']:>7.1f} | {r['5YGrowth']:>8} | {r['PEG']:>6.2f} | "
              f"{r['Mean']:>6.2f} | {r['Dev_SD']:>7.2f} | {r['Bull']:>8} | "
              f"{r['Base']:>8} | {r['Bear']:>8}")


if __name__ == "__main__":
    main()
