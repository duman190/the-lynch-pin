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


def _yoy_growth(curr, prev):
    if prev and prev != 0:
        return (curr - prev) / abs(prev)
    return None


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

    Compares latest quarter vs same quarter last year (YoY).
    Every line item is graded relative to revenue growth.

    Args:
        ticker_obj: yfinance Ticker object (with session)

    Returns:
        dict with 'grade', 'items' list, or None if insufficient data
    """
    try:
        qinc = ticker_obj.quarterly_income_stmt
        if qinc is None or qinc.empty or len(qinc.columns) < 5:
            return None

        latest = qinc.columns[0]
        yoy_col = qinc.columns[4]

        # Compute all YoY growths
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
        return {'grade': grade, 'items': items}

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
