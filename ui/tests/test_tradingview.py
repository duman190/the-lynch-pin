"""Trading view: bars built from Yahoo's chart JSON (native and numpy builders), the service's cache / single fetch /
viewer limit / Yahoo pause, and its HTTP route. Offline: Yahoo's responses are made up here."""
import gzip
import http.client
import json
import os
import random
import threading

import pytest

from ui import tvdata
from ui.config import Settings
from ui.tradingview import CLOSED_TTL, OPEN_TTL, TradingView

NATIVE = tvdata.native() is not None
BUILDERS = [False] + ([True] if NATIVE else [])
EDT, EST = -14400, -18000
MON = 1792368000  # 2026-10-19 00:00 UTC, a Monday
H, M = 3600, 60


def yahoo(ts, o=None, h=None, l=None, c=None, v=None, periods=None, meta=None):
    n = len(ts)
    c = c if c is not None else [100.0 + i for i in range(n)]
    o = o if o is not None else [x - 0.5 if x is not None else None for x in c]
    h = h if h is not None else [x + 1 if x is not None else None for x in c]
    l = l if l is not None else [x - 1 if x is not None else None for x in c]
    v = v if v is not None else [1000] * n
    m = {"symbol": "TST", "currency": "USD", "exchangeName": "NMS", "fullExchangeName": "NasdaqGS",
         "exchangeTimezoneName": "America/New_York", "gmtoffset": EDT, "priceHint": 2, "longName": "Test Co",
         "regularMarketPrice": next((x for x in reversed(c) if x is not None), None), "regularMarketTime": ts[-1] if ts else 0,
         "currentTradingPeriod": {"regular": {"start": MON + 13 * H + 30 * M, "end": MON + 20 * H}}}
    if periods is not None:
        m["tradingPeriods"] = [[{"timezone": "X", "start": s, "end": e, "gmtoffset": g}] for s, e, g in periods]
    m.update(meta or {})
    q = {"open": o, "high": h, "low": l, "close": c, "volume": v}
    return json.dumps({"chart": {"result": [{"meta": m, "timestamp": ts, "indicators": {"quote": [q]}}], "error": None}}).encode()


def bars(raw, code, native, **kw):
    b = tvdata.build(raw, code, use_native=native, **kw)
    j = json.loads(b.body)
    assert json.loads(gzip.decompress(b.gz)) == j
    t, acc = [], 0
    for d in j["dt"]:
        acc += d
        t.append(acc)
    j["t"] = t
    return j


def session(day_utc, open_utc=13 * H + 30 * M, close_utc=20 * H, g=EDT):
    return (day_utc + open_utc, day_utc + close_utc, g)


@pytest.mark.parametrize("native", BUILDERS)
def test_4h_bars_start_at_the_open_and_take_in_the_closing_print(native):
    d1, d2 = session(MON), session(MON + 86400, close_utc=17 * H)  # Tuesday closes early (13:00)
    ts = [d1[0] + k * H for k in range(7)] + [d1[1]]          # 9:30 … 15:30 and the 16:00 print
    ts += [d2[0] + k * H for k in range(4)]                    # 9:30 … 12:30
    c = [10, 11, 12, 13, 14, 15, 16, 16.5, 20, 21, 22, 23]
    v = [100] * 7 + [0] + [200] * 4
    j = bars(yahoo(ts, c=c, v=v, periods=[d1, d2]), "240", native)
    assert j["t"] == [d1[0], d1[0] + 4 * H, d2[0]]           # 9:30, 13:30 and the half day's 9:30
    assert j["o"] == [9.5, 13.5, 19.5] and j["c"] == [13, 16.5, 23]
    assert j["h"] == [14, 17.5, 24] and j["l"] == [9, 13, 19]
    assert j["vol"] == [400, 300, 800]
    assert j["intraday"] is True and j["n"] == 3


@pytest.mark.parametrize("native", BUILDERS)
def test_sessions_keep_their_open_across_dst(native):
    winter = session(MON - 30 * 86400, open_utc=14 * H + 30 * M, close_utc=21 * H, g=EST)  # 9:30 EST = 14:30 UTC
    ts = [winter[0] + k * H for k in range(7)]
    j = bars(yahoo(ts, periods=[winter]), "120", native)  # 2H; meta says EDT (today's offset)
    assert j["t"] == [winter[0], winter[0] + 2 * H, winter[0] + 4 * H, winter[0] + 6 * H]


@pytest.mark.parametrize("native", BUILDERS)
def test_3m_from_1m_counts_from_the_open(native):
    d = session(MON)
    ts = [d[0] + k * M for k in range(11)]   # 9:30 … 9:40
    j = bars(yahoo(ts, periods=[d]), "3", native)
    assert j["t"] == [d[0] + k * 180 for k in range(4)]
    assert j["vol"] == [3000, 3000, 3000, 2000]


@pytest.mark.parametrize("native", BUILDERS)
def test_extended_hours_sessions_and_flags(native):
    pre, reg, post = (MON + 8 * H, MON + 13 * H + 30 * M), (MON + 13 * H + 30 * M, MON + 20 * H), (MON + 20 * H, MON + 24 * H)
    ts = list(range(pre[0], post[1], H // 2))  # 30-min bars, 04:00-20:00 New York
    meta = {"tradingPeriods": {"pre": [[{"start": pre[0], "end": pre[1], "gmtoffset": EDT}]],
                               "regular": [[{"start": reg[0], "end": reg[1], "gmtoffset": EDT}]],
                               "post": [[{"start": post[0], "end": post[1], "gmtoffset": EDT}]]}}
    raw = yahoo(ts, meta=meta)
    j = bars(raw, "240", native, ext=True)                      # 4H from the pre-market's open: 04:00, 08:00, 12:00, 16:00
    assert j["t"] == [pre[0] + k * 4 * H for k in range(4)]
    assert j["sess"] == "eere"  # a bar's session is its start's: 04:00 and 08:00 pre-market, 16:00 after the close
    j30 = bars(raw, "30", native, ext=True)
    assert j30["n"] == 32 and j30["sess"].count("r") == 13 and j30["sess"][0] == "e"
    plain = bars(raw, "240", native)                            # without ext: no session flags
    assert "sess" not in plain


@pytest.mark.parametrize("native", BUILDERS)
def test_weekly_live_row_joins_its_week(native):
    mondays = [MON - 14 * 86400 + 4 * H, MON - 7 * 86400 + 4 * H, MON + 4 * H]  # Yahoo: Monday 00:00 New York
    live = MON + 86400 + 20 * H + 1                                               # Tuesday 16:00:01
    j = bars(yahoo(mondays + [live], c=[1, 2, 3, 4], v=[10, 10, 10, 5]), "1W", native)
    assert j["n"] == 3 and j["c"] == [1, 2, 4] and j["vol"] == [10, 10, 15]


@pytest.mark.parametrize("native", BUILDERS)
def test_months_group_into_quarters_halves_and_years(native):
    import datetime as dt
    ts = [int(dt.datetime(2025, m, 1, 4).replace(tzinfo=dt.timezone.utc).timestamp()) for m in range(1, 13)]
    ts.append(int(dt.datetime(2026, 1, 1, 5).replace(tzinfo=dt.timezone.utc).timestamp()))  # EST midnight
    raw = yahoo(ts, c=list(range(1, 14)))
    assert bars(raw, "3M", native)["c"] == [3, 6, 9, 12, 13]
    assert bars(raw, "6M", native)["c"] == [6, 12, 13]
    assert bars(raw, "12M", native)["c"] == [12, 13]
    assert bars(raw, "1M", native)["n"] == 13


@pytest.mark.parametrize("native", BUILDERS)
def test_missing_close_of_the_live_bar_comes_from_the_quote(native):
    ts = [MON - 2 * 86400 + 13 * H + 30 * M, MON - 86400 + 13 * H + 30 * M, MON + 13 * H + 30 * M]
    raw = yahoo(ts, o=[1, None, 5], h=[2, None, 6], l=[0.5, None, 4], c=[1.5, None, None], v=[1, None, None],
                meta={"regularMarketPrice": 5.5, "regularMarketTime": MON + 20 * H, "regularMarketVolume": 77,
                      "regularMarketDayHigh": 6.2, "regularMarketDayLow": 3.9})
    j = bars(raw, "1D", native)
    assert j["n"] == 2                       # the empty row in the middle is dropped
    assert j["c"] == [1.5, 5.5] and j["h"] == [2, 6] and j["l"] == [0.5, 4] and j["vol"] == [1, 77]


def ref_sma(x, n):
    return [None if i < n - 1 else sum(x[i - n + 1:i + 1]) / n for i in range(len(x))]


def ref_ema(x, n):
    out, a = [None] * len(x), 2 / (n + 1)
    first = next((i for i, v in enumerate(x) if v is not None), None)
    if first is None or len(x) < first + n:
        return out
    e = sum(x[first:first + n]) / n
    out[first + n - 1] = e
    for i in range(first + n, len(x)):
        e = a * x[i] + (1 - a) * e
        out[i] = e
    return out


def ref_rsi(c, n=14):
    out = [None] * len(c)
    d = [c[i] - c[i - 1] for i in range(1, len(c))]
    up = sum(max(x, 0) for x in d[:n]) / n
    dn = sum(max(-x, 0) for x in d[:n]) / n
    out[n] = 100 - 100 / (1 + up / dn)
    for i in range(n + 1, len(c)):
        x = d[i - 1]
        up, dn = (up * (n - 1) + max(x, 0)) / n, (dn * (n - 1) + max(-x, 0)) / n
        out[i] = 100 - 100 / (1 + up / dn)
    return out


@pytest.mark.parametrize("native", BUILDERS)
def test_indicators_match_their_definitions(native):
    rnd = random.Random(3)
    c = [100.0]
    for _ in range(399):
        c.append(round(c[-1] * (1 + rnd.gauss(0, 0.02)), 2))
    ts = [MON - (400 - i) * 86400 + 13 * H + 30 * M for i in range(400)]
    j = bars(yahoo(ts, c=c), "1D", native)
    fast, slow = ref_ema(c, 12), ref_ema(c, 26)
    line = [None if a is None or b is None else a - b for a, b in zip(fast, slow)]
    sig = ref_ema(line, 9)
    for key, ref, tol in (("sma50", ref_sma(c, 50), 0.006), ("sma100", ref_sma(c, 100), 0.006),
                          ("sma200", ref_sma(c, 200), 0.006), ("ema8", ref_ema(c, 8), 0.006),
                          ("ema21", ref_ema(c, 21), 0.006), ("rsi", ref_rsi(c), 0.06),
                          ("macd", line, 0.0006), ("signal", sig, 0.0006)):
        got = j[key]
        assert len(got) == len(ref)
        for g, r in zip(got, ref):
            assert (g is None) == (r is None), key
            if r is not None:
                assert abs(g - r) <= tol, (key, g, r)


@pytest.mark.parametrize("native", BUILDERS)
def test_only_the_latest_bars_are_sent(native):
    ts = [MON - (300 - i) * 86400 for i in range(300)]
    j = bars(yahoo(ts), "1D", native, max_bars=50)
    assert j["n"] == 50 and j["total"] == 300 and j["c"][-1] == 399
    assert j["sma200"][-1] is not None  # computed over the whole series first


@pytest.mark.parametrize("native", BUILDERS)
def test_unknown_symbol_is_no_data(native):
    raw = b'{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found, symbol may be delisted"}}}'
    with pytest.raises(tvdata.NoData, match="Not Found"):
        tvdata.build(raw, "1D", use_native=native)
    with pytest.raises(tvdata.BuildError):
        tvdata.build(b"<html>", "1D", use_native=native)


@pytest.mark.skipif(not NATIVE, reason="native library not built (cd ui/native && cargo build --release)")
def test_native_and_numpy_build_the_same_json():
    rnd = random.Random(11)
    days = [session(MON - k * 86400, g=EDT if k < 20 else EST) for k in range(30, -1, -1)]
    ts, c = [], []
    for s, e, g in days:
        for k in range((e - s) // 300):
            if rnd.random() < 0.03:
                continue  # a minute without trades
            ts.append(s + k * 300)
            c.append(round(100 + rnd.gauss(0, 3), 2) if rnd.random() > 0.01 else None)
    raw = yahoo(ts, c=c, v=[rnd.randint(0, 10 ** 7) for _ in ts], periods=days)
    fixtures = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache", "tv_fixtures")
    cases = [(raw, code) for code in ("5", "10", "15", "30", "45", "60", "240")]
    if os.path.isdir(fixtures):  # Yahoo's real series, when tv_bench.py has recorded them
        for code, (_, base, _, _) in tvdata.INTERVALS.items():
            p = os.path.join(fixtures, f"{base}.json")
            if os.path.exists(p):
                cases.append((open(p, "rb").read(), code))
    for r, code in cases:
        a, b = bars(r, code, True), bars(r, code, False)
        assert a.keys() == b.keys()
        d = a["decimals"]
        # indicators are sums in a different order (a running sum vs numpy's cumsum): a value on a rounding boundary
        # may land one unit of its last decimal apart; prices, times and volumes are the same numbers
        unit = {"sma50": d, "sma100": d, "sma200": d, "ema8": d, "ema21": d, "rsi": 1, "macd": d + 1, "signal": d + 1,
                "bbu": d, "bbm": d, "bbl": d}
        for k in a:
            if isinstance(a[k], list):
                assert len(a[k]) == len(b[k]), (code, k)
                tol = 1.0001 * 10 ** -unit[k] if k in unit else 0
                for x, y in zip(a[k], b[k]):
                    assert (x is None) == (y is None) and (x is None or abs(x - y) <= tol), (code, k, x, y)
            else:
                assert a[k] == b[k], (code, k)


# ── the service ─────────────────────────────────────────────────────────────────────────────────────────────────────
DAILY = yahoo([MON - (260 - i) * 86400 + 13 * H + 30 * M for i in range(260)])


class FakeYahoo:
    def __init__(self, status=200, body=DAILY, gate=None):
        self.status, self.body, self.gate, self.calls = status, body, gate, []
        self.lock = threading.Lock()

    def __call__(self, sym, base):
        with self.lock:
            self.calls.append((sym, base))
        if self.gate is not None:
            self.gate.wait(5)
        if isinstance(self.status, Exception):
            raise self.status
        return self.status, self.body


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeJobs:
    def __init__(self):
        self.throttled, self.cooldown = [], 0

    def yahoo_throttled(self, what):
        self.throttled.append(what)
        self.cooldown = 30

    def yahoo_cooldown(self):
        return self.cooldown


def make_tv(fetch=None, viewers=0, public=False, force_open=True, wall=None, jobs=None, clock=None, yahoo_rpm=0,
            viewer_rpm=0):
    s = Settings()
    s.tv_viewers, s.public, s.tv_force_open = viewers, public, force_open
    s.tv_yahoo_rpm, s.tv_viewer_rpm = yahoo_rpm, viewer_rpm
    return TradingView(s, fetch=fetch or FakeYahoo(), jobs=jobs, clock=clock or Clock(),
                       wall=wall or (lambda: MON + 15 * H))


def test_concurrent_viewers_share_one_yahoo_request():
    gate = threading.Event()
    y = FakeYahoo(gate=gate)
    tv = make_tv(y)
    out = []
    ts = [threading.Thread(target=lambda: out.append(tv.chart("AAA", "1D").status)) for _ in range(8)]
    [t.start() for t in ts]
    gate.set()
    [t.join(10) for t in ts]
    assert out == [200] * 8 and y.calls == [("AAA", "1d")]
    tv.chart("AAA", "1W")
    assert y.calls[-1] == ("AAA", "1wk")
    tv.shutdown()


def test_cached_chart_is_bytes_and_revalidates_with_304():
    y = FakeYahoo()
    tv = make_tv(y)
    r1 = tv.chart("AAA", "1D")
    r2 = tv.chart("AAA", "1D")
    assert r1.status == r2.status == 200 and r1.gz is r2.gz and len(y.calls) == 1
    assert tv.chart("AAA", "1D", if_none_match=r1.etag).status == 304
    assert json.loads(r1.body(False))["n"] == 260
    assert tv.counters["builds"] == 1
    other = tv.chart("AAA", "1W")  # same series? no: weekly is its own Yahoo series
    assert other.status == 200 and len(y.calls) == 2
    tv.shutdown()


def test_series_is_refreshed_after_its_ttl():
    clock, y = Clock(), FakeYahoo()
    tv = make_tv(y, clock=clock)
    e1 = tv.chart("AAA", "1D").etag
    clock.t += OPEN_TTL["1d"] - 1
    assert tv.chart("AAA", "1D").etag == e1 and len(y.calls) == 1
    clock.t += 2
    r = tv.chart("AAA", "1D")
    assert r.etag != e1 and len(y.calls) == 2 and r.headers["X-TV-Live"] == "1"
    tv.shutdown()


def test_closed_market_keeps_series_longer():
    after_close = lambda: MON + 23 * H  # currentTradingPeriod ends at MON + 20 h
    before_open = lambda: MON + 13 * H + 20 * M  # 10 min before the open
    tv = make_tv(force_open=False, wall=after_close)
    r = tv.chart("AAA", "1D")
    assert r.headers["X-TV-Live"] == "0" and int(r.headers["X-TV-Refresh"]) == CLOSED_TTL + 1
    tv2 = make_tv(force_open=False, wall=before_open)
    r2 = tv2.chart("AAA", "1D")
    assert int(r2.headers["X-TV-Refresh"]) == 10 * M + 5 + 1  # fresh bars right after the open
    tv.shutdown(), tv2.shutdown()


def test_viewer_limit_release_and_idle_expiry():
    clock = Clock()
    tv = make_tv(viewers=2, clock=clock)
    assert tv.chart("AAA", "1D", ip="1.1.1.1", sid="aaaaaaaa").status == 200
    assert tv.chart("BBB", "1D", ip="1.1.1.2", sid="bbbbbbbb").status == 200
    full = tv.chart("CCC", "1D", ip="1.1.1.3", sid="cccccccc")
    assert full.status == 429 and full.reason == "tv_full" and 1 <= full.retry_after <= tv.idle + 1
    assert tv.chart("AAA", "240", ip="1.1.1.1", sid="aaaaaaaa").status == 200  # a viewer keeps its place
    tv.release("1.1.1.2", "bbbbbbbb")
    assert tv.chart("CCC", "1D", ip="1.1.1.3", sid="cccccccc").status == 200
    assert tv.viewers() == 2
    clock.t += tv.idle + 1  # both stop polling
    assert tv.viewers() == 0
    assert tv.chart("DDD", "1D", ip="1.1.1.4", sid="dddddddd").status == 200
    tv.shutdown()


def test_public_visitor_gets_two_trading_views():
    tv = make_tv(viewers=10, public=True)
    assert tv.chart("AAA", "1D", ip="9.9.9.9", sid="tab00001").status == 200
    assert tv.chart("AAA", "1D", ip="9.9.9.9", sid="tab00002").status == 200
    r = tv.chart("AAA", "1D", ip="9.9.9.9", sid="tab00003")
    assert r.status == 429 and r.reason == "tv_visitor"
    tv.shutdown()


def test_unknown_symbol_is_404_and_not_asked_again():
    y = FakeYahoo(status=404, body=b'{"chart":{"result":null,"error":{"code":"Not Found","description":"x"}}}')
    tv = make_tv(y)
    assert tv.chart("ZZZZ", "1D").status == 404
    assert tv.chart("ZZZZ", "1D").status == 404 and len(y.calls) == 1
    assert tv.chart("ZZZZ", "nope").status == 400
    tv.shutdown()


def test_yahoo_429_pauses_charts_with_the_lookups():
    clock, jobs = Clock(), FakeJobs()
    y = FakeYahoo()
    tv = make_tv(y, jobs=jobs, clock=clock)
    assert tv.chart("AAA", "1D").status == 200
    y.status = 429
    r = tv.chart("BBB", "1D")
    assert r.status == 503 and jobs.throttled and r.retry_after
    calls = len(y.calls)
    assert tv.chart("CCC", "1D").status == 503 and len(y.calls) == calls  # paused: Yahoo isn't asked
    clock.t += OPEN_TTL["1d"] + 1  # AAA's series is stale: shown as it is rather than nothing
    r = tv.chart("AAA", "1D")
    assert r.status == 200 and r.headers.get("X-TV-Stale") == "1" and len(y.calls) == calls
    tv.shutdown()


def test_network_failure_is_502_then_stale():
    clock, y = Clock(), FakeYahoo()
    tv = make_tv(y, clock=clock)
    tv.chart("AAA", "1D")
    y.status = ConnectionError("down")
    assert tv.chart("BBB", "1D").status == 502
    clock.t += OPEN_TTL["1d"] + 1
    r = tv.chart("AAA", "1D")
    assert r.status == 200 and r.headers.get("X-TV-Stale") == "1"
    tv.shutdown()


def test_yahoo_budget_keeps_the_lookups_share():
    clock, y, slept = Clock(), FakeYahoo(), []
    tv = make_tv(y, clock=clock, yahoo_rpm=6, viewer_rpm=3)   # 6 a minute (one per 10 s), a burst of 4
    tv._sleep = slept.append
    assert [tv.chart(s, "1D", ip="1.1.1.1", sid="viewer01").status for s in ("A", "B", "C")] == [200] * 3
    r = tv.chart("D", "1D", ip="1.1.1.1", sid="viewer01")      # a 4th new series in a minute: this viewer waits
    assert r.status == 429 and r.reason == "tv_rate" and len(y.calls) == 3
    assert tv.chart("A", "1D", ip="1.1.1.1", sid="viewer01").status == 200   # what is in memory is free
    assert tv.chart("E", "1D", ip="1.1.1.2", sid="viewer02").status == 200   # the burst's last token
    r = tv.chart("F", "1D", ip="1.1.1.3", sid="viewer03")       # out of tokens: the next comes in 10 s > 8 s
    assert r.status == 503 and r.reason == "tv_budget" and r.retry_after == 11 and len(y.calls) == 4
    clock.t += 3                                                # 7 s to wait: the fetch waits its turn
    assert tv.chart("F", "1D", ip="1.1.1.3", sid="viewer03").status == 200 and slept and 6.9 < slept[-1] < 7.1
    clock.t += OPEN_TTL["1d"] + 1                               # bucket full again; E's series is stale
    tv._tokens, tv._refilled = -0.5, clock.t                    # (spent by others): the refresh must wait 15 s
    r = tv.chart("E", "1D", ip="1.1.1.2", sid="viewer02")     # too long a wait: the stale series, no fetch
    assert r.status == 200 and r.headers.get("X-TV-Stale") == "1" and len(y.calls) == 5
    st = tv.status()
    assert st["yahoo_rpm"] == 6 and st["over_budget"] >= 1 and st["budget_waits"] == 1
    tv.shutdown()


def test_over_budget_refresh_shows_the_stale_series_meanwhile():
    clock, gate = Clock(), threading.Event()
    y = FakeYahoo()
    tv = make_tv(y, clock=clock, yahoo_rpm=6)
    tv._sleep = lambda s: gate.wait(5)
    assert tv.chart("A", "1D").status == 200
    clock.t += OPEN_TTL["1d"] + 1
    tv._tokens, tv._refilled = 0.5, clock.t                     # 5 s to wait for the next token
    r = tv.chart("A", "1D")                                     # stale now, fresh once the refresh lands
    assert r.status == 200 and r.headers.get("X-TV-Stale") == "1"
    gate.set()
    for _ in range(100):
        if len(y.calls) == 2 and not tv._inflight:
            break
        threading.Event().wait(0.02)
    assert len(y.calls) == 2 and tv.chart("A", "1D").headers.get("X-TV-Stale") is None
    tv.shutdown()


def test_budget_wait_counts_from_the_reservation_not_from_the_pool():
    """Fetches queued behind others in the fetcher pool must not wait their budget delay again on top: each sleeps only
    until its token is due."""
    clock, slept = Clock(), []
    tv = make_tv(FakeYahoo(), clock=clock, yahoo_rpm=60)       # one a second, a burst of 15
    tv._sleep = lambda d: (slept.append(d), setattr(clock, "t", clock.t + d))
    tv._tokens, tv._refilled = 0.0, clock.t
    assert tv.chart("AAA", "1D").status == 200 and slept == [1.0]
    clock.t += 5                                               # the next token was due 4 s ago: no more sleeping
    tv._tokens, tv._refilled = -3.0, clock.t - 6               # (refills to +3 at reservation time)
    assert tv.chart("BBB", "1D").status == 200 and slept == [1.0]
    assert tv._fetch(("CCC", "1d"), start=clock.t - 2) is None and slept == [1.0]
    tv.shutdown()


def test_jobs_breaker_shared_with_charts(tmp_path):
    from ui.jobs import JobManager
    s = Settings()
    s.cache_dir = str(tmp_path)
    jm = JobManager(s, start=False)
    assert jm.yahoo_cooldown() == 0
    jm.yahoo_throttled("trading view AAA 1d")
    st = jm.cache_stats()
    assert st["rate_limited"] and st["rate_limit_hits"] == 1 and 0 < jm.yahoo_cooldown() <= 30


# ── HTTP ────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def portal(tmp_path):
    from ui.server import PortalApp, PortalServer, make_handler
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    s.tv_viewers, s.tv_force_open = 1, True
    tv = TradingView(s, fetch=FakeYahoo())
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(s, tv=tv)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    tv.shutdown()


def req(httpd, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def test_http_chart_gzip_etag_and_304(portal):
    r, body = req(portal, "/api/tv/aaa?i=1D&s=viewer001", {"Accept-Encoding": "gzip"})
    assert r.status == 200 and r.getheader("Content-Encoding") == "gzip" and r.getheader("Vary") == "Accept-Encoding"
    assert json.loads(gzip.decompress(body))["n"] == 260
    assert r.getheader("Cache-Control") == "no-cache" and r.getheader("X-TV-Refresh")
    etag = r.getheader("ETag")
    r2, body2 = req(portal, "/api/tv/AAA?i=1D&s=viewer001", {"If-None-Match": etag})
    assert r2.status == 304 and body2 == b""
    r3, body3 = req(portal, "/api/tv/AAA?i=1D&s=viewer001")  # no gzip asked: plain JSON
    assert r3.status == 200 and r3.getheader("Content-Encoding") is None and json.loads(body3)["n"] == 260


def test_http_limit_release_and_bad_input(portal):
    assert req(portal, "/api/tv/AAA?i=1D&s=viewer001")[0].status == 200
    r, body = req(portal, "/api/tv/AAA?i=1D&s=viewer002")
    assert r.status == 429 and r.getheader("Retry-After") and "full" in json.loads(body)["error"]
    assert req(portal, "/api/tv/release?s=viewer001")[0].status == 200
    assert req(portal, "/api/tv/AAA?i=1D&s=viewer002")[0].status == 200
    assert req(portal, "/api/tv/AAA?i=7m&s=viewer002")[0].status == 400
    assert req(portal, "/api/tv/AAA?i=1D&s=bad!")[0].status == 400
    assert req(portal, "/api/tv/1AAA?i=1D&s=viewer002")[0].status == 400
    h = json.loads(req(portal, "/api/health")[1])
    assert h["features"]["tv"] is True and h["tv"]["limit"] == 1 and h["tv"]["viewers"] == 1


def test_public_health_shows_only_the_count():
    from ui.server import PortalApp
    out = PortalApp._public_health({"tv": {"viewers": 3, "limit": 50, "fetches": 9, "series_mb": 1.0}})
    assert out["tv"] == {"viewers": 3, "limit": 50}
