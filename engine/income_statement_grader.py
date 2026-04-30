import yfinance as yf
import pandas as pd

# Waterfall chain: (label, yfinance field, parent_label, is_cost)
# is_cost=True means growth BELOW parent is good (efficiency)
# is_cost=False means growth ABOVE parent is good (leverage)
WATERFALL = [
    ('Revenue',    'Total Revenue',                       None,        False),
    ('COGS',       'Cost Of Revenue',                     'Revenue',   True),
    ('Gross',      'Gross Profit',                        'Revenue',   False),
    ('R&D',        'Research And Development',             'Gross',     True),
    ('S&M',        'Selling And Marketing Expense',        'Gross',     True),
    ('G&A',        'General And Administrative Expense',   'Gross',     True),
    ('OpIncome',   'Operating Income',                     'Gross',     False),
    ('NetIncome',  'Net Income',                           'OpIncome',  False),
    ('EPS',        'Diluted EPS',                          'NetIncome', False),
]


def _yoy_growth(curr, prev):
    if prev and prev != 0:
        return (curr - prev) / abs(prev)
    return None


def _grade_item(is_cost, item_g, parent_g):
    """Grade relative to parent in the waterfall chain."""
    if item_g is None or parent_g is None:
        return '⚪'
    if is_cost:
        # Costs: growing slower than parent = efficient
        if item_g <= 0 and parent_g > 0:
            return '🟢'  # costs shrinking while parent grows
        if item_g < parent_g * 0.5:
            return '🟢'
        elif item_g <= parent_g:
            return '🔵'
        return '🔴'
    else:
        # Profit: growing faster than parent = leveraging
        if item_g > parent_g and item_g > 0:
            return '🟢'
        elif item_g > 0:
            return '🔵'
        return '🔴'


def _assign_grade(signals):
    """Assigns letter grade based on waterfall signal pattern."""
    scored = [s for s in signals if s != '⚪']
    if len(scored) == 0:
        return 'N/A'

    greens = scored.count('🟢')
    reds = scored.count('🔴')
    total = len(scored)
    ratio = greens / total

    # Check if full waterfall accelerates: OpIncome > Gross AND EPS > NetIncome
    op_i = next((i for i, (l, *_) in enumerate(WATERFALL) if l == 'OpIncome'), None)
    eps_i = next((i for i, (l, *_) in enumerate(WATERFALL) if l == 'EPS'), None)
    op_sig = signals[op_i] if op_i is not None and op_i < len(signals) else '⚪'
    eps_sig = signals[eps_i] if eps_i is not None and eps_i < len(signals) else '⚪'
    accelerating = op_sig == '🟢' and eps_sig == '🟢'

    if accelerating and reds == 0:
        return 'A+'
    elif accelerating:
        return 'A'
    elif ratio >= 0.7 and reds <= 1:
        return 'B+'
    elif ratio >= 0.5:
        return 'B'
    elif ratio >= 0.3:
        return 'B-'
    elif reds >= total * 0.6:
        return 'D'
    return 'C'


def grade_ticker(ticker_obj):
    """Grades a single ticker's income statement efficiency.

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

        # First pass: compute all YoY growths
        growths = {}
        for label, field, _, _ in WATERFALL:
            if field not in qinc.index:
                growths[label] = None
                continue
            curr = qinc.loc[field, latest]
            prev = qinc.loc[field, yoy_col]
            if pd.isna(curr) or pd.isna(prev):
                growths[label] = None
            else:
                growths[label] = _yoy_growth(float(curr), float(prev))

        # Second pass: grade each item against its parent
        items = []
        signals = []
        for label, field, parent_label, is_cost in WATERFALL:
            g = growths.get(label)
            parent_g = growths.get(parent_label) if parent_label else None

            if label == 'Revenue':
                # Revenue is the base — always blue (reference point)
                sig = '🔵' if g is not None else '⚪'
            else:
                sig = _grade_item(is_cost, g, parent_g)

            items.append((label, g, sig))
            signals.append(sig)

        grade = _assign_grade(signals)
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
    for i, (label, _, _, _) in enumerate(WATERFALL):
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
