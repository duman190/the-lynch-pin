import pandas as pd

# Damodaran's interest coverage ratio -> synthetic rating lookup
# Source: Damodaran Online (NYU Stern) - updated thresholds for large-cap firms
# Format: (min_coverage, rating, numeric_score)
_COVERAGE_TABLE = [
    (8.50, 'AAA', 20),
    (6.50, 'AA+', 19),
    (5.50, 'AA',  18),
    (4.25, 'AA-', 17),
    (3.75, 'A+',  16),
    (3.00, 'A',   15),
    (2.50, 'A-',  14),
    (2.25, 'BBB+', 13),
    (2.00, 'BBB', 12),
    (1.75, 'BBB-', 11),
    (1.50, 'BB+', 10),
    (1.25, 'BB',   9),
    (1.00, 'BB-',  8),
    (0.80, 'B+',   7),
    (0.65, 'B',    6),
    (0.50, 'B-',   5),
    (0.35, 'CCC+', 4),
    (0.20, 'CCC',  3),
    (0.10, 'CCC-', 2),
    (0.00, 'CC',   1),
    (-999, 'D',    0),
]

# Numeric score -> rating string (for final output after adjustments)
_SCORE_TO_RATING = {s: r for (_, r, s) in _COVERAGE_TABLE}


def _coverage_to_score(coverage):
    """Maps interest coverage ratio to numeric score via Damodaran table."""
    for min_cov, _, score in _COVERAGE_TABLE:
        if coverage >= min_cov:
            return score
    return 0


def _notch_adjust(base_score, net_debt_ebitda, cash_debt_ratio, debt_fcf_pct):
    """Adjusts base rating ±1-2 notches based on leverage/liquidity metrics."""
    adj = 0

    # Net Debt / EBITDA adjustment
    if net_debt_ebitda is not None:
        if net_debt_ebitda < 0:       # net cash position
            adj += 2
        elif net_debt_ebitda < 1.0:
            adj += 1
        elif net_debt_ebitda > 4.0:
            adj -= 2
        elif net_debt_ebitda > 3.0:
            adj -= 1

    # Cash / Total Debt adjustment
    if cash_debt_ratio is not None:
        if cash_debt_ratio > 1.0:     # more cash than debt
            adj += 1
        elif cash_debt_ratio < 0.1:
            adj -= 1

    # Debt service as % of FCF adjustment
    if debt_fcf_pct is not None:
        if debt_fcf_pct < 15:
            adj += 1
        elif debt_fcf_pct > 50:
            adj -= 1
        elif debt_fcf_pct > 80:
            adj -= 2

    return max(0, min(20, base_score + adj))


def grade_ticker(ticker_obj):
    """Assigns synthetic credit rating to a ticker based on balance sheet health.

    Uses Damodaran's interest coverage methodology as primary driver,
    adjusted by Net Debt/EBITDA, Cash/Debt ratio, and debt service/FCF.

    Args:
        ticker_obj: yfinance Ticker object (with session)

    Returns:
        dict with 'rating' and 'metrics', or None if insufficient data
    """
    try:
        bs = ticker_obj.quarterly_balance_sheet
        inc = ticker_obj.quarterly_income_stmt
        cf = ticker_obj.quarterly_cashflow

        if bs is None or bs.empty or inc is None or inc.empty:
            return None

        # Use latest quarter balance sheet
        latest_bs = bs.iloc[:, 0]

        # TTM financials (sum last 4 quarters)
        def _ttm_sum(df, field):
            if field not in df.index:
                return None
            vals = df.loc[field].head(4).dropna()
            return float(vals.sum()) if len(vals) >= 2 else None

        # Core inputs
        total_debt = None
        for field in ['Total Debt', 'Long Term Debt', 'Long Term Debt And Capital Lease Obligation']:
            if field in latest_bs.index and pd.notna(latest_bs.get(field)):
                total_debt = float(latest_bs[field])
                break

        cash = None
        for field in ['Cash And Cash Equivalents', 'Cash Cash Equivalents And Short Term Investments']:
            if field in latest_bs.index and pd.notna(latest_bs.get(field)):
                cash = float(latest_bs[field])
                break

        op_income = _ttm_sum(inc, 'Operating Income')
        interest_exp = _ttm_sum(inc, 'Interest Expense')
        ebitda = _ttm_sum(inc, 'EBITDA')
        fcf = None
        if cf is not None and not cf.empty:
            fcf = _ttm_sum(cf, 'Free Cash Flow')

        # Interest coverage ratio (primary metric)
        no_debt_service = False
        if op_income is not None and interest_exp and abs(interest_exp) > 0:
            coverage = op_income / abs(interest_exp)
        elif op_income and op_income > 0:
            # No debt or zero cost to service = AAA, show N/A for coverage metrics
            coverage = 999.0
            no_debt_service = True
        else:
            return None

        # Secondary metrics
        net_debt_ebitda = None
        if total_debt is not None and cash is not None and ebitda and ebitda > 0:
            net_debt_ebitda = (total_debt - cash) / ebitda

        cash_debt_ratio = None
        if cash is not None and total_debt and total_debt > 0:
            cash_debt_ratio = cash / total_debt

        debt_fcf_pct = None
        if interest_exp and abs(interest_exp) > 0 and fcf and fcf > 0:
            debt_fcf_pct = (abs(interest_exp) / fcf) * 100

        # Score and adjust
        base_score = _coverage_to_score(coverage)
        final_score = _notch_adjust(base_score, net_debt_ebitda, cash_debt_ratio, debt_fcf_pct)
        rating = _SCORE_TO_RATING.get(final_score, 'NR')

        return {
            'rating': rating,
            'metrics': [
                ('IntCov', None if no_debt_service else coverage),
                ('ND/EBITDA', net_debt_ebitda),
                ('Cash/Debt', cash_debt_ratio),
                ('Svc/FCF%', None if no_debt_service else debt_fcf_pct),
            ]
        }
    except Exception:
        return None


def print_grader_table(tickers_data, engine_map):
    """Prints a compact balance sheet credit rating table.

    Args:
        tickers_data: list of result dicts from get_ticker_stats()
        engine_map: dict of symbol -> LynchPinEngine instances
    """
    results = {}
    for row in tickers_data:
        sym = row['Ticker'].replace('*', '')
        if sym not in engine_map:
            continue
        grade = grade_ticker(engine_map[sym].ticker)
        if grade:
            results[sym] = grade

    if not results:
        return

    col_w = 10
    lbl_w = 12

    print(f"\n{'':>{lbl_w}}", end='')
    for sym in results:
        print(f" | {sym:^{col_w}}", end='')
    print(f"\n{'':>{lbl_w}}" + "-" * (len(results) * (col_w + 3) + 1))

    # Rating row
    print(f"{'Rating':>{lbl_w}}", end='')
    for sym, g in results.items():
        print(f" | {g['rating']:^{col_w}}", end='')
    print()

    # Metric rows
    for i, (label, _) in enumerate([('IntCov', None), ('ND/EBITDA', None),
                                     ('Cash/Debt', None), ('Svc/FCF%', None)]):
        print(f"{label:>{lbl_w}}", end='')
        for sym, g in results.items():
            _, val = g['metrics'][i]
            if val is not None:
                cell = f"{val:.1f}" if abs(val) < 100 else f"{val:.0f}"
                print(f" | {cell:^{col_w}}", end='')
            else:
                print(f" | {'N/A':^{col_w}}", end='')
        print()

    print()
