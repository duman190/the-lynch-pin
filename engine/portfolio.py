"""Portfolio aggregation for The Lynch Pin.

Parses a ``ticker, shares`` holdings file, derives market-value weights and
rolls the per-position GARP metrics up into portfolio-level figures.

Weighting conventions (w_i = position market value / total portfolio value):

* PE, Fwd PE, 2Y Fwd PE — **harmonic** weighted mean ``1 / Σ(w_i / PE_i)``.
  This equals ``total value / total earnings`` and is how index providers
  compute a fund's P/E; an arithmetic mean over-weights high-multiple names.
  Non-positive multiples (unprofitable companies) are excluded and the
  remaining weights are renormalised.
* PEG, historical mean PEG, PEG std, Dev(SD), 5Y growth, Bull/Base/Bear ROI —
  arithmetic weighted mean ``Σ(w_i × x_i)``.
* Median PEG — weight-aware median of the current position PEGs.
* Income grade / credit rating — letter → ordinal score, weighted mean,
  rounded back to the nearest letter.

No dollar values are ever emitted — only weights.
"""
import math
import os
import re
from collections import OrderedDict

from engine.balance_sheet_grader import _SCORE_TO_RATING

# Income-statement letter grade → ordinal score (N/A is excluded).
INCOME_GRADE_SCORES = OrderedDict([
    ('A++', 8), ('A+', 7), ('A', 6), ('B+', 5),
    ('B', 4), ('B-', 3), ('C', 2), ('D', 1),
])
_SCORE_TO_INCOME_GRADE = {v: k for k, v in INCOME_GRADE_SCORES.items()}

# Credit rating letter → ordinal score (inverse of Damodaran table, 0..20).
_RATING_TO_SCORE = {r: s for s, r in _SCORE_TO_RATING.items()}

_LINE_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9.\-]{0,9})\s*[,;\t ]\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*$')


def parse_portfolio_file(path):
    """Reads a holdings file and returns ``OrderedDict[ticker -> shares]``.

    Accepted line format: ``TICKER, SHARES`` (comma, semicolon, tab or
    whitespace separated). Blank lines and ``#`` comments are ignored.
    Tickers are upper-cased; repeated tickers are de-duplicated by summing
    their share counts, preserving first-seen order.

    Raises ``ValueError`` on a malformed line so bad input fails loudly
    instead of silently dropping a position.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    positions = OrderedDict()
    with open(path, 'r') as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            m = _LINE_RE.match(line)
            if not m:
                raise ValueError(f"{path}:{lineno}: expected 'TICKER, SHARES', got {raw.rstrip()!r}")
            ticker = m.group(1).upper()
            shares = float(m.group(2).replace(',', ''))
            if shares <= 0:
                raise ValueError(f"{path}:{lineno}: share count must be positive, got {shares}")
            positions[ticker] = positions.get(ticker, 0.0) + shares
    if not positions:
        raise ValueError(f"{path}: no positions found")
    return positions


def compute_weights(positions, prices):
    """Market-value weights ``shares × price / Σ(shares × price)``.

    ``prices`` maps ticker → current price; tickers with a missing or
    non-positive price are skipped (they cannot be valued). Returns an
    ``OrderedDict[ticker -> weight]`` sorted by weight descending.
    """
    values = {}
    for t, shares in positions.items():
        px = prices.get(t)
        if px is None or not isinstance(px, (int, float)) or px <= 0 or math.isnan(px):
            continue
        values[t] = shares * float(px)
    total = sum(values.values())
    if total <= 0:
        return OrderedDict()
    weights = OrderedDict(sorted(((t, v / total) for t, v in values.items()),
                                 key=lambda kv: kv[1], reverse=True))
    return weights


def _pct(s):
    """'12.3%' → 12.3 ; passes numbers through; None on failure."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(str(s).replace('%', '').strip())
    except ValueError:
        return None


def _weighted_mean(pairs):
    """Arithmetic weighted mean of ``[(weight, value), ...]`` ignoring None values."""
    pairs = [(w, v) for w, v in pairs if v is not None and not (isinstance(v, float) and math.isnan(v))]
    tw = sum(w for w, _ in pairs)
    if tw <= 0:
        return None
    return sum(w * v for w, v in pairs) / tw


def _harmonic_weighted_mean(pairs):
    """Harmonic weighted mean of positive values; None values / non-positive excluded."""
    pairs = [(w, v) for w, v in pairs if v is not None and v > 0]
    tw = sum(w for w, _ in pairs)
    if tw <= 0:
        return None
    return tw / sum(w / v for w, v in pairs)


def _weighted_median(pairs):
    """Weight-aware median: smallest value whose cumulative weight ≥ 50%."""
    pairs = sorted(((w, v) for w, v in pairs if v is not None), key=lambda kv: kv[1])
    tw = sum(w for w, _ in pairs)
    if tw <= 0:
        return None
    acc = 0.0
    for w, v in pairs:
        acc += w
        if acc >= tw / 2.0:
            return v
    return pairs[-1][1]


def position_peg_std(row):
    """Recovers a position's historical PEG std from ``|PEG − Mean| / |Dev_SD|``."""
    try:
        dev = float(row['Dev_SD'])
        if dev == 0:
            return None
        return abs(float(row['PEG']) - float(row['Mean'])) / abs(dev)
    except (KeyError, TypeError, ValueError):
        return None


def weighted_income_grade(weights, grader_data):
    """Weighted ordinal average of income grades → nearest letter (or 'N/A')."""
    pairs = []
    for t, w in weights.items():
        g = grader_data.get(t) if grader_data else None
        if g and g.get('grade') in INCOME_GRADE_SCORES:
            pairs.append((w, INCOME_GRADE_SCORES[g['grade']]))
    score = _weighted_mean(pairs)
    if score is None:
        return 'N/A', None
    return _SCORE_TO_INCOME_GRADE[int(round(score))], score


def weighted_credit_rating(weights, bs_data):
    """Weighted ordinal average of synthetic credit ratings → nearest letter (or 'NR')."""
    pairs = []
    for t, w in weights.items():
        b = bs_data.get(t) if bs_data else None
        if b and b.get('rating') in _RATING_TO_SCORE:
            pairs.append((w, _RATING_TO_SCORE[b['rating']]))
    score = _weighted_mean(pairs)
    if score is None:
        return 'NR', None
    return _SCORE_TO_RATING[int(round(score))], score


def weighted_metrics(rows, weights, grader_data=None, bs_data=None):
    """Rolls per-position ``get_ticker_stats()`` rows up to portfolio level.

    Args:
        rows: iterable of engine result dicts (``Ticker`` may carry a ``*``).
        weights: ticker → market-value weight (should sum to ~1 across the
            whole portfolio; rows without a weight are ignored).
        grader_data / bs_data: ticker → grade / rating dicts (optional).

    Returns a dict:
        ``PE, FwdPE, 2YFwd`` (harmonic), ``PEG, Mean, Std, Dev_SD, Growth,
        Bull, Base, Bear`` (arithmetic, growth/ROI in %), ``MedianPEG``,
        ``IncomeGrade, IncomeScore, CreditRating, CreditScore``,
        ``Coverage`` (share of portfolio weight with valuation data),
        ``Positions`` (count of rows used).
    """
    used = []
    for r in rows:
        t = str(r['Ticker']).replace('*', '')
        w = weights.get(t)
        if w is None or w <= 0:
            continue
        used.append((w, r))

    coverage = sum(w for w, _ in used)
    out = {
        'Coverage': coverage,
        'Positions': len(used),
        'TotalPositions': len(weights),
        'PE': _harmonic_weighted_mean([(w, _pct(r.get('PE'))) for w, r in used]),
        'FwdPE': _harmonic_weighted_mean([(w, _pct(r.get('FwdPE'))) for w, r in used]),
        '2YFwd': _harmonic_weighted_mean([(w, _pct(r.get('2YFwd'))) for w, r in used]),
        'PEG': _weighted_mean([(w, _pct(r.get('PEG'))) for w, r in used]),
        'MedianPEG': _weighted_median([(w, _pct(r.get('PEG'))) for w, r in used]),
        'Mean': _weighted_mean([(w, _pct(r.get('Mean'))) for w, r in used]),
        'Std': _weighted_mean([(w, position_peg_std(r)) for w, r in used]),
        'Dev_SD': _weighted_mean([(w, _pct(r.get('Dev_SD'))) for w, r in used]),
        'Growth': _weighted_mean([(w, _pct(r.get('5YGrowth'))) for w, r in used]),
        'Bull': _weighted_mean([(w, _pct(r.get('Bull'))) for w, r in used]),
        'Base': _weighted_mean([(w, _pct(r.get('Base'))) for w, r in used]),
        'Bear': _weighted_mean([(w, _pct(r.get('Bear'))) for w, r in used]),
    }
    grade, gscore = weighted_income_grade(weights, grader_data)
    rating, rscore = weighted_credit_rating(weights, bs_data)
    out.update({'IncomeGrade': grade, 'IncomeScore': gscore,
                'CreditRating': rating, 'CreditScore': rscore})
    return out


def format_weighted_summary(wm):
    """Human-readable multi-line summary of ``weighted_metrics()`` output."""
    def f(v, spec):
        return format(v, spec) if v is not None else 'N/A'
    return (
        f"  Weighted PE:        {f(wm['PE'], '.1f')}\n"
        f"  Weighted Fwd PE:    {f(wm['FwdPE'], '.1f')}\n"
        f"  Weighted 2Y Fwd PE: {f(wm['2YFwd'], '.1f')}\n"
        f"  Weighted 5Y Growth: {f(wm['Growth'], '.1f')}%\n"
        f"  Weighted PEG:       {f(wm['PEG'], '.2f')}  (median {f(wm['MedianPEG'], '.2f')})\n"
        f"  Weighted Mean PEG:  {f(wm['Mean'], '.2f')}  ± {f(wm['Std'], '.2f')} SD  → Dev {f(wm['Dev_SD'], '+.2f')} SD\n"
        f"  Weighted ROI:       Bull {f(wm['Bull'], '.1f')}% | Base {f(wm['Base'], '.1f')}% | Bear {f(wm['Bear'], '.1f')}%\n"
        f"  Weighted Grade:     Income {wm['IncomeGrade']} | Credit {wm['CreditRating']}\n"
        f"  Coverage:           {wm['Coverage'] * 100:.0f}% of portfolio weight "
        f"({wm['Positions']} of {wm.get('TotalPositions', wm['Positions'])} positions have GARP data)"
    )
