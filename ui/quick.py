"""Quick Overview: the AI overview's three sections built from the quant data by fixed rules.

Shown instead of the AI overview when the portal runs with ``--no-ai``. Nothing here reasons: every
sentence is a number from the analysis (Yahoo's profile, the engine's stats, grades and technicals)
or a threshold check, so the same data always gives the same text.

* Overview: who the company is (name, sector, HQ, size, Yahoo's business summary, margins, dividend,
  analyst consensus) and a one-line valuation snapshot.
* Reverse DCF: the base ROI's arithmetic, the same "Base ROI math" the AI prompt and the daily scan's
  research prompt cite (engine._scenario_pegs + _growth_decay): X% base ROI requires EPS to compound
  at Y%/yr for 5 years and the forward PE to move from Z to the implied terminal PE.
* Stomach test: rule-based red flags (``RULES``), most serious first.
"""
import re

# Thresholds for the stomach-test flags. "high" = a reason the stock can lag for years; "watch" = worth
# knowing before buying.
RULES = {
    "ttm_pe": 50.0,        # trailing PE above this: priced for perfection
    "fwd_pe": 40.0,        # forward PE above this
    "growth": 40.0,        # 5Y EPS growth assumption above this (%/yr): hard to sustain for 5 years
    "peg": 2.5,            # the engine's own expensive-for-its-growth flag
    "dev_sd": 1.0,         # PEG this many SDs above its 5Y mean: mean reversion works against it
    "base_roi": 9.0,       # base ROI below this (%/yr): the engine's risk flag, ~a market return
    "beta": 1.5,           # swings more than the market
    "short_float": 0.10,   # short interest above 10% of the float
    "payout": 1.0,         # dividend payout above 100% of earnings
    "small_cap": 2e9,      # market cap below $2B
    "rsi": 70.0,           # overbought
    "edge": 60.0,          # 6M bear-signal accuracy above this
    "int_cov": 3.0,        # interest coverage below this
    "nd_ebitda": 3.0,      # net debt / EBITDA above this
    "credit_min": "A",     # synthetic credit rating below this: balance-sheet risk (junk: high)
    "income_min": "A",     # income grade below this: income statement subpar (C/D: high)
}
# Best first: engine/balance_sheet_grader.py (Damodaran synthetic ratings), engine/income_statement_grader.py
CREDIT_SCALE = ("AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
                "CCC+", "CCC", "CCC-", "CC", "C", "D")
INCOME_SCALE = ("A++", "A+", "A", "B+", "B", "B-", "C", "D")
_JUNK = CREDIT_SCALE[CREDIT_SCALE.index("BB+"):]
_WEAK_GRADES = ("C", "D")


def _below(grade, floor, scale):
    """True when ``grade`` ranks below ``floor`` on ``scale`` (unknown grades, e.g. NR, are never flagged)."""
    return grade in scale and scale.index(grade) > scale.index(floor)


def _num(v):
    return v if isinstance(v, (int, float)) and v == v else None


def _money(v, currency="USD"):
    v = _num(v)
    if v is None:
        return None
    sign = "$" if currency in (None, "USD") else ""
    neg, v = ("-" if v < 0 else ""), abs(v)
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= div:
            return f"{neg}{sign}{v / div:.1f}{unit}" + ("" if sign else f" {currency}")
    return f"{neg}{sign}{v:,.0f}" + ("" if sign else f" {currency}")


def _first_sentences(text, n=2, limit=420):
    """First ``n`` sentences of Yahoo's business summary (it can run to 2,000 characters)."""
    if not text:
        return None
    parts = re.split(r"(?<=[a-z0-9\)])\.\s+(?=[A-Z])", text.strip())
    out = ". ".join(parts[:n]).strip()
    if not out.endswith("."):
        out += "."
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


def _items(d):
    inc = d.get("income") or {}
    return {i["label"]: i for i in inc.get("items") or []}


def _overview(d):
    p = d.get("profile") or {}
    sym, name = d.get("ticker"), d.get("name") or d.get("ticker")
    where = ", ".join(x for x in (p.get("city"), p.get("state"), p.get("country")) if x)
    what = " · ".join(x for x in (d.get("sector"), d.get("industry")) if x)
    first = f"{name} ({sym})"
    if what:
        first += f" — {what}"
    facts = []
    if where:
        facts.append(f"headquartered in {where}")
    if _num(p.get("employees")):
        facts.append(f"~{p['employees']:,} employees")
    mc = _money(d.get("market_cap"), d.get("currency"))
    if mc:
        facts.append(f"{mc} market cap")
    out = [first + (f": {', '.join(facts)}." if facts else ".")]
    summary = _first_sentences(p.get("summary"))
    if summary:
        out.append(summary)
    money_bits = []
    if _num(p.get("operating_margin")) is not None:
        money_bits.append(f"operating margin {p['operating_margin'] * 100:.1f}%")
    if _num(p.get("profit_margin")) is not None:
        money_bits.append(f"net margin {p['profit_margin'] * 100:.1f}%")
    if _num(p.get("dividend_yield")):
        div = f"dividend yield {p['dividend_yield']:.2f}%"
        if _num(p.get("payout_ratio")):
            div += f" (payout {p['payout_ratio'] * 100:.0f}% of earnings)"
        money_bits.append(div)
    if money_bits:
        out.append(money_bits[0][0].upper() + "; ".join(money_bits)[1:] + ".")
    rec, target, n = p.get("recommendation"), _num(p.get("target_mean")), _num(p.get("analysts"))
    price = _num(d.get("price"))
    if rec and n:
        line = f"Analysts ({int(n)}): {rec.replace('_', ' ')}"
        if target and price:
            line += f", mean target {target:,.2f} ({(target / price - 1) * 100:+.0f}% vs today)"
        out.append(line + ".")
    st = d.get("stats") or {}
    snap = []
    if _num(st.get("PEG")) is not None and _num(st.get("Mean")) is not None:
        peg = f"PEG {st['PEG']:.2f} vs a 5Y mean of {st['Mean']:.2f}"
        if st.get("history") == "ok" and _num(st.get("Dev_SD")) is not None:
            peg += f" ({st['Dev_SD']:+.2f} SD)"
        snap.append(peg)
    if (d.get("income") or {}).get("grade"):
        snap.append(f"income grade {d['income']['grade']}")
    if (d.get("credit") or {}).get("rating"):
        snap.append(f"credit {d['credit']['rating']}")
    if (d.get("technicals") or {}).get("signal"):
        snap.append(f"technicals {d['technicals']['signal']}")
    if snap:
        out.append("Snapshot: " + ", ".join(snap) + ".")
    return " ".join(out)


def implied_terminal_pe(st):
    """(growth %, terminal forward PE) behind the base ROI — the engine's scenario math, or None."""
    from engine.lynch_pin_core import _growth_decay, _scenario_pegs
    g, peg, mean, dev = (_num(st.get(k)) for k in ("growth_pct", "PEG", "Mean", "Dev_SD"))
    if not g or g <= 0 or peg is None or mean is None:
        return None
    std = abs(peg - mean) / abs(dev) if dev else 0.0
    _, base_peg, _ = _scenario_pegs(g, mean, peg, std)
    return g, base_peg * g ** _growth_decay(g)


def _reverse_dcf(d):
    st = d.get("stats") or {}
    base, fwd = _num(st.get("Base")), _num(st.get("FwdPE"))
    math = implied_terminal_pe(st) if base is not None and fwd else None
    if math is None:
        return None, None
    g, term = math
    change = (term / fwd - 1) * 100
    per_year = ((term / fwd) ** 0.2 - 1) * 100  # the multiple's contribution, %/yr over the 5 years
    out = [f"{base:.1f}% base ROI requires EPS to compound at {g:.1f}%/yr for the next 5 years and the stock to "
           f"re-rate from a forward PE of {fwd:.1f}x today to a terminal forward PE of {term:.1f}x."]
    if change > 5:
        out.append(f"That is {change:.0f}% multiple expansion ({per_year:+.1f}%/yr) on top of the earnings growth: "
                   f"the market has to pay more for each dollar of earnings in 5 years than it does today.")
    elif change < -5:
        out.append(f"The multiple compresses {-change:.0f}% ({per_year:+.1f}%/yr), so earnings growth has to outrun "
                   f"the de-rating: roughly {g:+.1f}%/yr from EPS and {per_year:+.1f}%/yr from the multiple.")
    else:
        out.append("The multiple roughly holds, so the return is the earnings growth itself.")
    items = _items(d)
    rev, eps = (_num((items.get(k) or {}).get("growth")) for k in ("Revenue", "EPS"))
    if rev is not None or eps is not None:
        bits = [f"{lab} {v * 100:+.0f}%" for lab, v in (("revenue", rev), ("EPS", eps)) if v is not None]
        out.append(f"Latest quarter vs a year ago: {' and '.join(bits)}.")
    if g > 30 or change > 30:
        verdict = "stretch"
    elif g <= 15 and change <= 5:
        verdict = "realistic"
    else:
        verdict = "achievable"
    out.append({"realistic": "Assumptions: realistic — moderate growth without needing a richer multiple.",
                "achievable": "Assumptions: achievable — the growth has to hold up, but nothing heroic.",
                "stretch": "Assumptions: a stretch — sustained high growth and/or a richer multiple."}[verdict])
    if base < RULES["base_roi"]:
        out.append(f"Even if they hold, the base return trails the ~{RULES['base_roi']:.0f}%/yr hurdle.")
    return " ".join(out), verdict


def _flags(d):
    r = RULES
    st, p = d.get("stats") or {}, d.get("profile") or {}
    flags = []

    def add(level, text):
        flags.append({"level": level, "text": text})

    pe, fwd, g = _num(st.get("PE")), _num(st.get("FwdPE")), _num(st.get("growth_pct"))
    peg, dev, base, bear = (_num(st.get(k)) for k in ("PEG", "Dev_SD", "Base", "Bear"))
    if st:
        if not pe or pe <= 0:
            add("high", "No trailing earnings: the last 12 months were loss-making, so the valuation rests on forecasts.")
        elif pe > r["ttm_pe"]:
            add("high", f"Trailing PE {pe:.1f}x (above {r['ttm_pe']:.0f}x): priced for perfection; a miss hurts.")
        if fwd and fwd > r["fwd_pe"]:
            add("high", f"Forward PE {fwd:.1f}x (above {r['fwd_pe']:.0f}x): even next year's earnings look expensive.")
        if g and g > r["growth"]:
            add("high", f"Growth assumption {g:.1f}%/yr for 5 years (above {r['growth']:.0f}%): few companies sustain "
                        f"it; the ROI collapses if growth halves.")
        if peg and peg >= r["peg"]:
            add("high", f"PEG {peg:.2f} (≥ {r['peg']}): expensive even after paying for the growth.")
        if st.get("history") != "ok":
            add("watch", "5Y PEG history unavailable: the SD and mean-reversion maths are placeholders today.")
        elif dev is not None and dev > r["dev_sd"]:
            add("high", f"PEG {dev:+.2f} SD above its own 5Y mean: mean reversion works against the multiple.")
        if base is not None and base < r["base_roi"]:
            add("high", f"Base ROI {base:.1f}%/yr is below the ~{r['base_roi']:.0f}% hurdle: an index fund may do as well.")
        if bear is not None and bear < 0:
            add("high", f"Bear case loses money: {bear:.1f}%/yr over 5 years.")
    inc, items = d.get("income") or {}, _items(d)
    rev = _num((items.get("Revenue") or {}).get("growth"))
    if rev is not None and rev < 0:
        add("high", f"Revenue is shrinking: {rev * 100:+.0f}% vs the same quarter last year.")
    if inc.get("grade") in _WEAK_GRADES:
        add("high", f"Income grade {inc['grade']}: the income statement is not converting growth into profit.")
    elif _below(inc.get("grade"), r["income_min"], INCOME_SCALE):
        add("watch", f"Income grade {inc['grade']} (below {r['income_min']}): the income statement is subpar; "
                     f"operating income and EPS are not outgrowing revenue cleanly.")
    reds = [i["label"] for i in items.values() if i.get("signal") == "bad" and i["label"] != "Revenue"]
    if reds:
        add("watch", f"Costs running ahead of revenue: {', '.join(reds)}.")
    cr = d.get("credit") or {}
    metrics = {m["label"]: _num(m.get("value")) for m in cr.get("metrics") or []}
    if cr.get("rating") in _JUNK:
        add("high", f"Credit rating {cr['rating']} (junk): debt can squeeze equity holders in a downturn.")
    elif _below(cr.get("rating"), r["credit_min"], CREDIT_SCALE):
        add("watch", f"Credit rating {cr['rating']} (below {r['credit_min']}): balance-sheet risk; debt service "
                     f"leaves less room in a downturn.")
    if metrics.get("IntCov") is not None and metrics["IntCov"] < r["int_cov"]:
        add("watch", f"Interest coverage {metrics['IntCov']:.1f}x (below {r['int_cov']:.0f}x).")
    if metrics.get("ND/EBITDA") is not None and metrics["ND/EBITDA"] > r["nd_ebitda"]:
        add("watch", f"Net debt {metrics['ND/EBITDA']:.1f}x EBITDA (above {r['nd_ebitda']:.0f}x).")
    if _num(p.get("free_cashflow")) is not None and p["free_cashflow"] < 0:
        add("high", f"Negative free cash flow ({_money(p['free_cashflow'], d.get('currency'))}): growth is funded, "
                    f"not earned.")
    if _num(p.get("payout_ratio")) and p["payout_ratio"] > r["payout"]:
        add("watch", f"Dividend payout {p['payout_ratio'] * 100:.0f}% of earnings: the dividend is not covered.")
    t = d.get("technicals") or {}
    vs200 = _num(t.get("price_vs_sma200"))
    if vs200 is not None and vs200 < 0:
        add("watch", f"Price {vs200:+.1f}% vs its 200-day average (trend {t.get('trend') or '—'}): "
                     f"catching a falling knife.")
    elif t.get("trend") == "BEARISH":
        add("watch", "Trend BEARISH: catching a falling knife.")
    if _num(t.get("rsi")) and t["rsi"] >= r["rsi"]:
        add("watch", f"RSI {t['rsi']:.0f}: overbought in the short term.")
    e = d.get("edge") or {}
    if e.get("best_edge") == "BEAR" and _num(e.get("bear_acc")) and e["bear_acc"] >= r["edge"]:
        add("watch", f"6M edge favours bears: {e['bear_acc']:.0f}% of bearish signals were right.")
    if _num(p.get("beta")) and p["beta"] > r["beta"]:
        add("watch", f"Beta {p['beta']:.2f}: swings {p['beta']:.1f}× as much as the market.")
    if _num(p.get("short_float")) and p["short_float"] > r["short_float"]:
        add("watch", f"Short interest {p['short_float'] * 100:.1f}% of the float: many investors are betting against it.")
    mc = _num(d.get("market_cap"))
    if mc and mc < r["small_cap"]:
        add("watch", f"Small cap ({_money(mc, d.get('currency'))}): thinner coverage, wider swings.")
    target, price = _num(p.get("target_mean")), _num(d.get("price"))
    if target and price and target < price:
        add("watch", f"Analysts' mean target ({target:,.2f}) is below today's price ({price:,.2f}).")
    flags.sort(key=lambda f: f["level"] != "high")  # stable: rule order within each level
    return flags


def quick_overview(d):
    """The Quick Overview for an analysis result ``d`` (status done or nodata)."""
    dcf, verdict = _reverse_dcf(d)
    return {"overview": _overview(d), "reverse_dcf": dcf, "dcf_verdict": verdict, "stomach_test": _flags(d),
            "rules": dict(RULES)}


def profile_from_info(info):
    """The company facts the Quick Overview uses, from the quote Yahoo already returned (no extra call)."""
    keys = {"summary": "longBusinessSummary", "employees": "fullTimeEmployees", "city": "city", "state": "state",
            "country": "country", "website": "website", "beta": "beta",
            "dividend_yield": "dividendYield",  # already in percent (2.46 = 2.46%); the margins are fractions
            "payout_ratio": "payoutRatio", "short_float": "shortPercentOfFloat", "free_cashflow": "freeCashflow",
            "operating_margin": "operatingMargins", "profit_margin": "profitMargins",
            "recommendation": "recommendationKey", "target_mean": "targetMeanPrice",
            "analysts": "numberOfAnalystOpinions"}
    out = {}
    for k, src in keys.items():
        v = info.get(src)
        if isinstance(v, str):
            v = v.strip() or None
        elif not (isinstance(v, (int, float)) and v == v and not isinstance(v, bool)):
            v = None
        if v is not None:
            out[k] = v
    return out
