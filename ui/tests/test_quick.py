"""Quick Overview (ui/quick.py): the rule-based stand-in for the AI overview with --no-ai, and ↻ Refresh off."""
import copy
import re
import time

import pytest

from ui import quick
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.tests import fakes

KO_INFO = {"longName": "The Coca-Cola Company", "fullTimeEmployees": 65900, "city": "Atlanta", "state": "GA",
           "country": "United States", "website": "https://www.coca-colacompany.com", "beta": 0.342,
           "dividendYield": 2.46, "payoutRatio": 0.6246, "shortPercentOfFloat": 0.0095,
           "freeCashflow": 5218749952, "operatingMargins": 0.34873, "profitMargins": 0.28558,
           "recommendationKey": "buy", "targetMeanPrice": 94.65, "numberOfAnalystOpinions": 23,
           "longBusinessSummary": "The Coca-Cola Company, a beverage company, manufactures and sells various "
                                  "nonalcoholic beverages worldwide. It offers sparkling soft drinks. The company "
                                  "was founded in 1886 and is headquartered in Atlanta, Georgia."}


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    return s


@pytest.fixture
def msft(settings):
    info = dict(fakes.MSFT_INFO, **{k: v for k, v in KO_INFO.items() if k != "longName"})  # MSFT + a full profile
    info["targetMeanPrice"] = 500.0
    eng = type("E", (fakes.FakeEngine,), {"infos": {"MSFT": info}})
    return TickerAnalyzer(settings, backends=fakes.backends(engine=eng)).run("MSFT")


def texts(d):
    return [f["text"] for f in quick.quick_overview(d)["stomach_test"]]


# ── analysis output ─────────────────────────────────────────────────────────
def test_analysis_carries_profile_and_quick_overview(msft):
    assert msft["profile"]["employees"] == 65900 and msft["profile"]["dividend_yield"] == 2.46
    q = msft["quick"]
    assert set(q) >= {"overview", "reverse_dcf", "dcf_verdict", "stomach_test", "rules"}
    ov = q["overview"]
    assert ov.startswith("Microsoft Corporation (MSFT) — Technology · Software—Infrastructure")
    assert "Atlanta, GA, United States" in ov and "~65,900 employees" in ov and "$3.2T market cap" in ov
    assert "It offers sparkling soft drinks." in ov and "founded in 1886" not in ov  # first two sentences
    assert "operating margin 34.9%" in ov.lower() and "dividend yield 2.46% (payout 62% of earnings)" in ov
    assert "Analysts (23): buy, mean target 500.00 (+16% vs today)" in ov
    assert "Snapshot: PEG 1.68 vs a 5Y mean of 1.72 (-0.19 SD), income grade A+, credit AAA" in ov


def test_reverse_dcf_matches_the_research_prompts_base_roi_math(msft):
    from engine.ai_research import LynchPinResearcher
    prompt = LynchPinResearcher.build_prompt([fakes.MSFT_ROW])
    implied = re.search(r"re-rates to (\d+)x at maturity", prompt).group(1)
    dcf = msft["quick"]["reverse_dcf"]
    m = re.match(r"13\.5% base ROI requires EPS to compound at 13\.0%/yr for the next 5 years and the stock to "
                 r"re-rate from a forward PE of 21\.8x today to a terminal forward PE of ([\d.]+)x\.", dcf)
    assert m and round(float(m.group(1))) == int(implied)  # 1.72 terminal PEG × 13% = 22.4x
    assert "Latest quarter vs a year ago: revenue +18%." in dcf and msft["quick"]["dcf_verdict"] == "realistic"


@pytest.mark.parametrize("growth,peg,mean,verdict,phrase", [
    ("13.0%", 1.68, 1.72, "realistic", "roughly holds"),
    ("20.0%", 1.5, 1.6, "achievable", "expansion"),
    ("45.0%", 0.9, 1.2, "stretch", "expansion"),
    ("6.2%", 3.91, 4.42, "realistic", "earnings growth has to outrun the de-rating"),  # KO, 2 Oct 2026
])
def test_dcf_verdicts(msft, growth, peg, mean, verdict, phrase):
    d = copy.deepcopy(msft)
    d["stats"].update(growth_pct=float(growth[:-1]), PEG=peg, Mean=mean, Dev_SD=-1.0)
    q = quick.quick_overview(d)
    assert q["dcf_verdict"] == verdict and phrase in q["reverse_dcf"]
    assert f"Assumptions: {'a ' if verdict == 'stretch' else ''}{verdict}" in q["reverse_dcf"]
    trails = "trails the ~9%/yr hurdle" in q["reverse_dcf"]
    assert trails == (d["stats"]["Base"] < 9)


def test_reverse_dcf_quotes_the_faded_growth_from_20_percent(msft):
    from engine.lynch_pin_core import _avg_eps_growth
    d = copy.deepcopy(msft)
    d["stats"].update(growth_pct=45.0, PEG=0.9, Mean=1.2, Dev_SD=-1.0)
    dcf = quick.quick_overview(d)["reverse_dcf"]
    assert (f"requires EPS to compound at {_avg_eps_growth(45.0):.1f}%/yr for the next 5 years (growth fading from "
            f"45.0% to {45.0 ** 0.9:.1f}%) and the stock to re-rate") in dcf


def test_reverse_dcf_splits_out_the_dividend(msft):
    d = copy.deepcopy(msft)
    d["stats"]["div_yield"] = 2.5  # included in the base ROI by the engine
    base = d["stats"]["Base"]
    dcf = quick.quick_overview(d)["reverse_dcf"]
    assert dcf.startswith(f"{base:.1f}% base ROI = 2.5% dividend yield + {base - 2.5:.1f}%/yr from the share price, "
                          f"which requires EPS to compound at 13.0%/yr for the next 5 years")


def test_format_stats_carries_the_dividend_yield():
    from ui.formats import format_stats
    assert format_stats({"DivYield": "2.5%", "Base": "12.0%"})["div_yield"] == 2.5
    assert format_stats({"Base": "12.0%"})["div_yield"] is None  # rows from before the dividend was added


def test_nodata_has_an_overview_but_no_dcf(settings):
    eng = type("E", (fakes.FakeEngine,), {"infos": {"SPY": {"quoteType": "ETF", "regularMarketPrice": 500.0,
                                                            "longName": "SPDR S&P 500 ETF Trust"}}, "rows": {}})
    r = TickerAnalyzer(settings, backends=fakes.backends(engine=eng)).run("SPY")
    assert r["status"] == "nodata" and r["quick"]["overview"].startswith("SPDR S&P 500 ETF Trust (SPY)")
    assert r["quick"]["reverse_dcf"] is None


# ── stomach-test rules ──────────────────────────────────────────────────────
def test_clean_compounder_has_only_minor_flags(msft):
    assert texts(msft) == ["Costs running ahead of revenue: G&A."]


def test_valuation_rules_and_their_thresholds(msft):
    d = copy.deepcopy(msft)
    d["stats"].update(PE=51.0, FwdPE=41.0, growth_pct=41.0, PEG=2.6, Dev_SD=1.2, Base=8.5, Bear=-2.0)
    t = texts(d)
    for needle in ("Trailing PE 51.0x (above 50x)", "Forward PE 41.0x (above 40x)", "Growth assumption 41.0%/yr",
                   "PEG 2.60 (≥ 2.5)", "PEG +1.20 SD above its own 5Y mean", "Base ROI 8.5%/yr",
                   "Bear case loses money: -2.0%/yr"):
        assert any(needle in x for x in t), needle
    d["stats"].update(PE=50.0, FwdPE=40.0, growth_pct=40.0, PEG=2.4, Dev_SD=1.0, Base=9.0, Bear=0.0)
    assert texts(d) == ["Costs running ahead of revenue: G&A."]  # thresholds are exclusive


def test_balance_sheet_income_technical_and_market_rules(msft):
    d = copy.deepcopy(msft)
    d["stats"]["PE"] = 0
    d["stats"]["history"] = "unavailable"
    d["income"] = {"grade": "D", "items": [{"label": "Revenue", "growth": -0.05, "signal": "bad"},
                                           {"label": "COGS", "growth": 0.02, "signal": "bad"}]}
    d["credit"] = {"rating": "BB", "metrics": [{"label": "IntCov", "value": 1.2}, {"label": "ND/EBITDA", "value": 4.1}]}
    d["technicals"] = dict(d["technicals"], trend="BEARISH", price_vs_sma200=-12.3, rsi=72)
    d["edge"] = dict(d["edge"], best_edge="BEAR", bear_acc=64.0)
    d["profile"].update(free_cashflow=-2e9, payout_ratio=1.3, beta=1.8, short_float=0.15, target_mean=400.0)
    d["market_cap"] = 1.5e9
    flags = quick.quick_overview(d)["stomach_test"]
    t = [f["text"] for f in flags]
    for needle in ("No trailing earnings", "PEG history unavailable", "Revenue is shrinking: -5%", "Income grade D",
                   "Costs running ahead of revenue: COGS", "Credit rating BB (junk)", "Interest coverage 1.2x",
                   "Net debt 4.1x EBITDA", "Negative free cash flow (-$2.0B)", "payout 130%",
                   "Price -12.3% vs its 200-day average", "RSI 72", "6M edge favours bears: 64%", "Beta 1.80",
                   "Short interest 15.0%", "Small cap ($1.5B)", "mean target (400.00) is below today's price"):
        assert any(needle.lower() in x.lower() for x in t), needle
    levels = [f["level"] for f in flags]
    assert levels == sorted(levels, key=lambda lv: lv != "high")  # serious ones first


@pytest.mark.parametrize("rating,level", [("AAA", None), ("A", None), ("A-", "watch"), ("BBB", "watch"),
                                          ("BB+", "high"), ("D", "high"), ("NR", None)])
def test_credit_below_A_is_balance_sheet_risk(msft, rating, level):
    d = copy.deepcopy(msft)
    d["credit"]["rating"] = rating
    hits = [f for f in quick.quick_overview(d)["stomach_test"] if f["text"].startswith(f"Credit rating {rating} ")]
    assert [f["level"] for f in hits] == ([level] if level else [])
    if level == "watch":
        assert "(below A): balance-sheet risk" in hits[0]["text"]


@pytest.mark.parametrize("grade,level", [("A++", None), ("A", None), ("B+", "watch"), ("B-", "watch"),
                                         ("C", "high"), ("D", "high")])
def test_income_grade_below_A_is_subpar(msft, grade, level):
    d = copy.deepcopy(msft)
    d["income"]["grade"] = grade
    hits = [f for f in quick.quick_overview(d)["stomach_test"] if f["text"].startswith(f"Income grade {grade}")]
    assert [f["level"] for f in hits] == ([level] if level else [])
    if level == "watch":
        assert "(below A): the income statement is subpar" in hits[0]["text"]


def test_profile_keeps_only_usable_values():
    p = quick.profile_from_info(dict(KO_INFO, beta=float("nan"), website="  ", state=None, numberOfAnalystOpinions=True))
    assert "beta" not in p and "website" not in p and "state" not in p and "analysts" not in p
    assert p["summary"].startswith("The Coca-Cola Company") and p["payout_ratio"] == 0.6246


# ── ↻ Refresh off ───────────────────────────────────────────────────────────
def test_refresh_off_keeps_serving_the_cached_analysis(settings):
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), store=DayStore(),
                    allow_refresh=False)
    try:
        jm.request("MSFT")
        for _ in range(500):
            if jm.request("MSFT", poll=True)["status"] == "done":
                break
            time.sleep(0.01)
        runs = fakes.FakeEngine.calls.count("MSFT")
        jm._recent.clear()
        snap = jm.request("MSFT", refresh=True)
        assert snap["cached"] is True and snap["status"] == "done"
        assert fakes.FakeEngine.calls.count("MSFT") == runs  # no re-analysis
    finally:
        jm.shutdown()


@pytest.mark.parametrize("no_ai", [True, False])
def test_health_reports_refresh_only_with_ai(tmp_path, no_ai):
    from ui.server import build_app, parse_args
    s, a = parse_args((["--no-ai"] if no_ai else []) + ["--workers", "1", "--llm-url", "http://127.0.0.1:1"])
    s.cache_dir = str(tmp_path / "cache")
    app = build_app(s, with_ai=not a.no_ai)
    try:
        f = app.health()["features"]
        assert f["ai"] is (not no_ai) and f["refresh"] is (not no_ai) and app.jobs.allow_refresh is (not no_ai)
    finally:
        app.jobs.shutdown()
