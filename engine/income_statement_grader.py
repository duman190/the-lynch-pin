import time

import yfinance as yf
import pandas as pd

# Waterfall items in order: (display label, yfinance field)
WATERFALL = [
    ('Revenue',    'Total Revenue'),
    ('COGS',       'Cost Of Revenue'),
    ('Gross',      'Gross Profit'),
    ('R&D',        'Research And Development'),
    ('S&M',        'Selling And Marketing Expense'),
    ('G&A',        'General And Administrative Expense'),
    ('OpIncome',   'Operating Income'),
    ('NetIncome',  'Net Income'),
    ('EPS',        'Diluted EPS'),
]

# SEC XBRL us-gaap tags per waterfall item (priority order), for a quarter Yahoo lists only
# EPS of. 'Revenues' first: where both are tagged (lenders, fintech) it is the total and the
# contract revenue a part of it. Gross falls back to Revenue - COGS, as Yahoo derives it.
_SEC_TAGS = {
    'Revenue':   ['Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax',
                  'SalesRevenueNet', 'RevenueFromContractWithCustomerIncludingAssessedTax'],
    'COGS':      ['CostOfRevenue', 'CostOfGoodsAndServicesSold'],
    'Gross':     ['GrossProfit'],
    'R&D':       ['ResearchAndDevelopmentExpense',
                  'ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost'],
    'S&M':       ['SellingAndMarketingExpense', 'MarketingExpense'],
    'G&A':       ['GeneralAndAdministrativeExpense'],
    'OpIncome':  ['OperatingIncomeLoss'],
    'NetIncome': ['NetIncomeLoss'],
    'EPS':       ['EarningsPerShareDiluted'],
}


def _yoy_growth(curr, prev):
    if prev and prev != 0:
        return (curr - prev) / abs(prev)
    return None


def _year_ago(dates, d):
    """The date in ``dates`` closest to one year before ``d`` (52/53-week fiscal
    quarters end a few days apart year to year), or None."""
    cands = [x for x in dates if 350 <= (d - x).days <= 380]
    return min(cands, key=lambda x: abs((d - x).days - 365)) if cands else None


def _yahoo_growths(qinc):
    """(quarter, growths) for the newest quarter in ``qinc`` that has a full statement
    and a year-ago quarter to compare with, or None.

    Yahoo lists a just-reported quarter with only EPS and share counts for days to
    weeks before it fills in the statement; those columns are skipped, not graded.
    """
    if qinc is None or qinc.empty or 'Total Revenue' not in qinc.index:
        return None
    full = [c for c in qinc.columns if pd.notna(qinc.loc['Total Revenue', c])]
    for latest in sorted(full, reverse=True):
        yoy_col = _year_ago(full, latest)
        if yoy_col is None:
            continue
        growths = {}
        for label, field in WATERFALL:
            if field not in qinc.index:
                growths[label] = None
                continue
            curr = qinc.loc[field, latest]
            prev = qinc.loc[field, yoy_col]
            if pd.isna(curr) or pd.isna(prev):
                growths[label] = None
            else:
                growths[label] = _yoy_growth(float(curr), float(prev))
        return latest, growths
    return None


def _sec_facts(ticker_obj):
    """us-gaap facts from the ticker's SEC filings ({} for a non-SEC filer or on
    error), fetched once per Ticker object. After the engine's stats they are
    already there: LynchPinEngine._get_companyfacts leaves its download on the Ticker."""
    gaap = getattr(ticker_obj, '_lynch_sec_gaap', None)
    if gaap is None:
        from engine.lynch_pin_core import LynchPinEngine, _SEC_HEADERS, _SEC_SESSION
        gaap = {}
        try:
            cik = LynchPinEngine._get_cik(ticker_obj.ticker)
            if cik:
                time.sleep(0.12)  # SEC fair-access limit
                resp = _SEC_SESSION.get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json',
                                        headers=_SEC_HEADERS)
                gaap = resp.json()['facts']['us-gaap']
        except Exception:
            pass
        ticker_obj._lynch_sec_gaap = gaap
    return gaap


def _sec_quarters(gaap, tag):
    """{quarter end: value} of a tag's single-quarter facts in 10-Q/10-K filings, the
    latest filing winning per quarter (restatements). Keeps the sign, unlike
    LynchPinEngine._parse_sec_quarterly: losses are graded too."""
    units = gaap.get(tag, {}).get('units', {})
    seen = {}
    for e in units.get('USD') or units.get('USD/shares') or []:
        if e.get('form') not in ('10-Q', '10-K') or not e.get('start') or not e.get('filed'):
            continue
        end = pd.Timestamp(e['end'])
        if 80 <= (end - pd.Timestamp(e['start'])).days < 120:
            if end not in seen or e['filed'] > seen[end][1]:
                seen[end] = (e['val'], e['filed'])
    return {d: v for d, (v, _) in seen.items()}


def _sec_growths(gaap, after, yahoo_revenue):
    """(quarter, growths) from SEC filings for the newest quarter with revenue, when it
    is newer than ``after``, or None.

    ``yahoo_revenue`` (quarter end -> Yahoo's Total Revenue) vets the revenue tag: the
    year-ago SEC revenue must match Yahoo's within 5% where Yahoo has that quarter.
    """
    rev_quarters = set().union(*(_sec_quarters(gaap, t) for t in _SEC_TAGS['Revenue']))
    if not rev_quarters:
        return None
    quarter = max(rev_quarters)
    if after is not None and quarter <= after:
        return None

    pairs = {}  # label -> (current, year-ago, year-ago quarter end)
    for label, tags in _SEC_TAGS.items():
        for tag in tags:
            q = _sec_quarters(gaap, tag)
            if quarter in q:
                prev = _year_ago(q, quarter)
                if prev is not None:
                    pairs[label] = (q[quarter], q[prev], prev)
                break
    if 'Gross' not in pairs and 'Revenue' in pairs and 'COGS' in pairs:
        (rc, rp, d), (cc, cp, _) = pairs['Revenue'], pairs['COGS']
        pairs['Gross'] = (rc - cc, rp - cp, d)

    if 'Revenue' not in pairs:
        return None
    _, rev_prev, prev_q = pairs['Revenue']
    y = yahoo_revenue.get(prev_q)
    if y is not None and pd.notna(y) and y and abs(rev_prev / float(y) - 1) > 0.05:
        return None

    growths = {label: _yoy_growth(pairs[label][0], pairs[label][1]) if label in pairs else None
               for label, _ in WATERFALL}
    return quarter, growths


def _grade_item(label, item_g, rev_g):
    """Grade each line item relative to revenue growth.

    Rules from the framework:
    - Revenue: GREEN if growing, RED if flat/declining. Never blue.
    - Costs (COGS, S&M, G&A): GREEN if growing slower than revenue,
      BLUE if growing but not a problem, RED if running ahead of revenue.
    - R&D: Lenient — GREEN if below revenue growth, BLUE if above but
      reasonable (investment), RED only if egregiously ahead.
    - Profit items (Gross, OpIncome, NetIncome, EPS): GREEN if growing
      faster than revenue (leverage), BLUE if growing but slower,
      RED if declining or negative growth while revenue grows.
    """
    if item_g is None:
        return '⚪'

    if label == 'Revenue':
        return '🟢' if item_g > 0.02 else '🔴'

    if label in ('COGS', 'S&M', 'G&A'):
        if item_g <= 0 and rev_g > 0:
            return '🟢'
        if item_g < rev_g:
            return '🟢'
        if item_g < rev_g * 1.3:
            return '🔵'
        return '🔴'

    if label == 'R&D':
        # Lenient on R&D — investment in future
        if item_g <= 0 and rev_g > 0:
            return '🟢'
        if item_g < rev_g:
            return '🟢'
        if item_g < rev_g * 2.0:
            return '🔵'
        return '🔴'

    # Profit items: Gross, OpIncome, NetIncome, EPS
    if item_g > rev_g and item_g > 0:
        return '🟢'
    if item_g > 0:
        return '🔵'
    return '🔴'


def _assign_grade(signals, growths):
    """Assigns letter grade based on the waterfall pattern.

    Grading rules:
    - A++: Everything accelerating, zero reds, EPS >> Revenue
    - A+:  OpIncome & EPS beat revenue, at most 1 blue cost line
    - A:   Waterfall accelerating, minor imperfections
    - B+:  Mostly green/blue, 1-2 reds in cost lines
    - B:   Decent but some leakage
    - B-:  Mixed signals, bottom half saving top half or vice versa
    - C:   Sluggish, multiple reds, waterfall decelerating
    - D:   Broken — flat/declining revenue with red cost lines
    """
    scored = [s for s in signals if s != '⚪']
    if len(scored) == 0:
        return 'N/A'

    greens = scored.count('🟢')
    blues = scored.count('🔵')
    reds = scored.count('🔴')
    total = len(scored)

    rev_g = growths.get('Revenue')
    op_g = growths.get('OpIncome')
    eps_g = growths.get('EPS')
    ni_g = growths.get('NetIncome')

    rev_sig = signals[0]  # Revenue signal
    op_sig = signals[6] if len(signals) > 6 else '⚪'
    ni_sig = signals[7] if len(signals) > 7 else '⚪'
    eps_sig = signals[8] if len(signals) > 8 else '⚪'

    # Revenue declining = ceiling of C
    if rev_sig == '🔴':
        if reds >= total * 0.5:
            return 'D'
        return 'C'

    # Check if waterfall accelerates: OpIncome & EPS beat revenue
    waterfall_accelerates = op_sig == '🟢' and eps_sig == '🟢'

    # A++ : near-perfect, zero reds, waterfall accelerating strongly
    if waterfall_accelerates and reds == 0 and blues <= 1:
        if eps_g and rev_g and eps_g > rev_g * 1.5:
            return 'A++'
        return 'A+'

    # A+ : waterfall accelerating, at most 1 red (in a cost line)
    if waterfall_accelerates and reds <= 1:
        return 'A+'

    # A : waterfall accelerating but some imperfections
    if waterfall_accelerates:
        return 'A'

    # B+ : mostly green, OpIncome or EPS growing, limited reds
    if greens >= total * 0.5 and reds <= 2:
        if op_sig in ('🟢', '🔵') and eps_sig in ('🟢', '🔵'):
            return 'B+'

    # B : decent, some leakage
    if greens >= total * 0.4 and reds <= 3:
        return 'B'

    # B- : mixed, bottom saving top or vice versa
    if greens >= total * 0.3:
        return 'B-'

    # C : sluggish
    if reds < total * 0.5:
        return 'C'

    return 'D'


def grade_ticker(ticker_obj):
    """Grades a single ticker's income statement efficiency.

    Compares the latest reported quarter vs the same quarter last year (YoY).
    Every line item is graded relative to revenue growth. While Yahoo lists the
    newest quarter with EPS only, that quarter comes from the company's SEC 10-Q
    if it has been filed; otherwise the newest quarter Yahoo has in full is graded.

    Args:
        ticker_obj: yfinance Ticker object (with session)

    Returns:
        dict with 'grade', 'items' list, 'quarter' (end date, YYYY-MM-DD) and
        'source' ('Yahoo' or 'SEC'), or None if insufficient data
    """
    try:
        qinc = ticker_obj.quarterly_income_stmt
        if qinc is None or qinc.empty:
            return None

        found, source = _yahoo_growths(qinc), 'Yahoo'
        graded = found[0] if found else None
        if graded is None or max(qinc.columns) > graded:
            yahoo_revenue = (qinc.loc['Total Revenue'].to_dict()
                             if 'Total Revenue' in qinc.index else {})
            sec = _sec_growths(_sec_facts(ticker_obj), graded, yahoo_revenue)
            if sec:
                found, source = sec, 'SEC'
        if not found:
            return None
        quarter, growths = found

        rev_g = growths.get('Revenue')

        # Grade each item relative to revenue
        items = []
        signals = []
        for label, field in WATERFALL:
            g = growths.get(label)
            if g is None or (label != 'Revenue' and rev_g is None):
                items.append((label, g, '⚪'))
                signals.append('⚪')
            else:
                sig = _grade_item(label, g, rev_g)
                items.append((label, g, sig))
                signals.append(sig)

        grade = _assign_grade(signals, growths)
        return {'grade': grade, 'items': items,
                'quarter': pd.Timestamp(quarter).strftime('%Y-%m-%d'), 'source': source}

    except Exception:
        return None


def print_grader_table(tickers_data, engine_map):
    """Prints a compact income statement grade table.

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

    # Grade row
    print(f"{'Grade':>{lbl_w}}", end='')
    for sym, g in results.items():
        print(f" | {g['grade']:^{col_w}}", end='')
    print()

    # Quarter graded (e.g. Sep'26), marked when it came from the SEC 10-Q
    print(f"{'Quarter':>{lbl_w}}", end='')
    for sym, g in results.items():
        q = pd.Timestamp(g['quarter']).strftime("%b'%y")
        if g['source'] == 'SEC':
            q += ' SEC'
        print(f" | {q:^{col_w}}", end='')
    print()

    # Each waterfall item
    for i, (label, _) in enumerate(WATERFALL):
        print(f"{label:>{lbl_w}}", end='')
        for sym, g in results.items():
            _, growth, sig = g['items'][i]
            if growth is not None:
                cell = f"{sig}{growth*100:>+.0f}%"
                # Emoji takes 2 terminal columns but len() counts 1
                pad = col_w - len(cell) - 1
                print(f" | {cell}{' ' * pad}", end='')
            else:
                print(f" | {'N/A':^{col_w}}", end='')
        print()

    print()
