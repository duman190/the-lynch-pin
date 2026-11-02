"""FMP request budget for the portal's growth enrichment (ui/fmp_budget.py): no network."""
import multiprocessing as mp
import os

import pytest

from ui.config import Settings
from ui.fmp_budget import FmpBudget


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_cap_and_rolling_window(tmp_path):
    clock = Clock()
    b = FmpBudget(str(tmp_path / "b.sqlite3"), limit=3, clock=clock)
    assert [bool(b.acquire("A")) for _ in range(4)] == [True, True, True, False]
    assert b.status() == {"used": 3, "limit": 3, "remaining": 0, "next_free_s": 86400, "unenriched": 0}
    clock.t += 3600
    assert b.acquire("B") is False and b.status()["next_free_s"] == 82800  # still 24 h back
    clock.t += 82801  # the first three are now more than 24 h old
    assert b.acquire("C") and b.status()["used"] == 1


def test_failed_request_hands_its_slot_back(tmp_path):
    b = FmpBudget(str(tmp_path / "b.sqlite3"), limit=2, clock=Clock())
    ok, failed = b.acquire("A"), b.acquire("B")
    assert b.acquire("C") is False  # both slots reserved while the requests are in flight
    ok(True)
    failed(False)
    assert b.status()["used"] == 1 and b.acquire("D")


def test_survives_a_restart(tmp_path):
    path, clock = str(tmp_path / "b.sqlite3"), Clock()
    FmpBudget(path, limit=2, clock=clock).acquire("A")(True)
    FmpBudget(path, limit=2, clock=clock).acquire("B")(True)
    assert FmpBudget(path, limit=2, clock=clock).acquire("C") is False


def _grab(path, n, out):
    b = FmpBudget(path, limit=25)
    out.put(sum(bool(b.acquire(f"P{os.getpid()}")) for _ in range(n)))


def test_processes_share_one_count(tmp_path):
    path = str(tmp_path / "b.sqlite3")
    FmpBudget(path, limit=25)
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_grab, args=(path, 10, out)) for _ in range(5)]  # 50 asks for 25 slots
    for p in procs:
        p.start()
    granted = sum(out.get(timeout=60) for _ in procs)
    for p in procs:
        p.join(timeout=60)
    assert granted == 25


class FakeResp:
    def __init__(self, status=200, data=None, text=None):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        if self.text is not None:  # a body that isn't JSON
            raise ValueError("Expecting value")
        return self._data


@pytest.fixture
def fmp(monkeypatch):
    """The engine's FMP fetch with a key, a fake HTTP session (recording requests) and no 60 s sleeps."""
    from engine import growth_estimator as ge
    calls, replies = [], []
    monkeypatch.setattr(ge, "FMP_KEY", "test-key")
    monkeypatch.setattr(ge._SESSION, "get", lambda url: (calls.append(url), replies.pop(0))[1])
    monkeypatch.setattr(ge.time, "sleep", lambda s: None)
    monkeypatch.setattr(ge, "FMP_GATE", None)
    return ge, calls, replies


ESTIMATES = [{"date": f"{y}-12-31", "epsAvg": 2.0 * 1.15 ** (y - 2025)} for y in range(2025, 2031)]


def test_every_answered_request_counts(tmp_path, fmp):
    """FMP counts what it answers, errors included (dashboard, 2026-10-07: 1 data + 6 HTTP 402 answers = +7)."""
    ge, calls, replies = fmp
    b = FmpBudget(str(tmp_path / "b.sqlite3"), limit=8, clock=Clock())
    ge.FMP_GATE = b.acquire
    replies[:] = [FakeResp(data=ESTIMATES)]
    assert round(ge._fmp_5y_growth("NVDA")) == 15 and b.status()["used"] == 1   # data
    for i, reply in enumerate((FakeResp(402, {"Error Message": "Premium Query Parameter"}), FakeResp(data=[]),
                               FakeResp(200, {"Error Message": "Limit Reach"}),
                               FakeResp(403, {"Error Message": "Invalid API KEY"})), start=2):
        replies[:] = [reply]
        assert ge._fmp_5y_growth("XYZ") is None and b.status()["used"] == i     # "Not enriched", still counted
    for reply in (FakeResp(500, None), FakeResp(502, None), FakeResp(503, None)):
        replies[:] = [reply]
        assert ge._fmp_5y_growth("XYZ") is None and b.status()["used"] == 5     # FMP unavailable: handed back
    replies[:] = [FakeResp(429), FakeResp(data=ESTIMATES)]
    assert round(ge._fmp_5y_growth("AMD")) == 15 and b.status()["used"] == 7    # the 429 and its retry: two
    replies[:] = [FakeResp(data=ESTIMATES)]
    assert ge._fmp_5y_growth("MSFT") and b.status()["used"] == 8
    n = len(calls)
    assert ge._fmp_5y_growth("TSM") is None and len(calls) == n                 # spent: no request at all


def test_network_error_does_not_count(tmp_path, fmp, monkeypatch):
    ge, calls, replies = fmp
    b = FmpBudget(str(tmp_path / "b.sqlite3"), limit=2, clock=Clock())
    ge.FMP_GATE = b.acquire
    def boom(url):
        raise ConnectionError("down")
    monkeypatch.setattr(ge._SESSION, "get", boom)
    assert ge._fmp_5y_growth("NVDA") is None and b.status()["used"] == 0


def test_no_key_or_no_gate(tmp_path, fmp, monkeypatch):
    ge, calls, replies = fmp
    b = FmpBudget(str(tmp_path / "b.sqlite3"), limit=3, clock=Clock())
    ge.FMP_GATE = b.acquire
    monkeypatch.setattr(ge, "FMP_KEY", None)
    assert ge._fmp_5y_growth("NVDA") is None and not calls and b.status()["used"] == 0  # no key: nothing spent
    monkeypatch.setattr(ge, "FMP_KEY", "test-key")
    ge.FMP_GATE = None  # main.py: no gate, unchanged behaviour
    replies[:] = [FakeResp(data=ESTIMATES)]
    assert round(ge._fmp_5y_growth("NVDA")) == 15


def test_settings_and_health(tmp_path, monkeypatch):
    from ui.server import PortalApp, parse_args
    assert Settings().fmp_limit == 240 and parse_args(["--fmp-limit", "100"])[0].fmp_limit == 100
    s = Settings()
    s.cache_dir, s.enrich = str(tmp_path / "cache"), "on"

    class Jobs:
        allow_refresh = True

        def cache_stats(self):
            return {"size": 0}

    app = PortalApp(s, jobs=Jobs())
    assert app.health()["enrich"] == {"used": 0, "limit": 240, "remaining": 240, "next_free_s": 0,
                                        "unenriched": 0}
    s.public = True
    assert "enrich" not in app.health()  # visitors don't see this machine's quota


# ── tickers FMP has nothing for: skipped for 30 days (the portal only) ───────
@pytest.fixture
def installed(tmp_path, fmp, monkeypatch):
    """The portal's install(): the gate plus the engine's FMP request / growth lookup wrapped (restored after)."""
    from ui import fmp_budget
    ge, calls, replies = fmp
    monkeypatch.setattr(ge._SESSION, "get", ge._SESSION.get)  # the fmp fixture's fake
    monkeypatch.setattr(ge, "_fmp_growth", ge._fmp_growth)
    clock = Clock()
    b = FmpBudget(str(tmp_path / "b.sqlite3"), limit=50, clock=clock)
    fmp_budget.install(b)
    fmp_budget.install(b)  # twice (a new analyzer in the same process): wrapped once
    assert not hasattr(ge._SESSION.get.__wrapped__, "__wrapped__") and not hasattr(ge._fmp_growth.__wrapped__, "__wrapped__")
    return ge, calls, replies, b, clock


THIN = [{"date": "2026-12-31", "epsAvg": 1.0}]  # one estimate: no 5Y growth
# FMP's real answer for a symbol outside the free plan (SHOP.TO, 2026-11-01): HTTP 402, a plain-text body
PREMIUM = FakeResp(402, text="Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not "
                             "available under your current subscription ...")


@pytest.mark.parametrize("reply", [PREMIUM, FakeResp(402, {"Error Message": "Premium Query Parameter"}),
                                   FakeResp(data=[]), FakeResp(data=THIN),
                                   FakeResp(data=[dict(e, epsAvg=2.0) for e in ESTIMATES])])  # flat: no growth
def test_a_ticker_fmp_has_nothing_for_is_skipped_for_30_days(installed, reply):
    ge, calls, replies, b, clock = installed
    replies[:] = [reply]
    assert ge._fmp_5y_growth("tsm") is None and len(calls) == 1 and b.status()["used"] == 1
    assert b.status()["unenriched"] == 1
    for _ in range(3):  # looked up again: no request, nothing spent
        assert ge._fmp_5y_growth("TSM") is None
    assert len(calls) == 1 and b.status()["used"] == 1
    replies[:] = [FakeResp(data=ESTIMATES)]
    assert round(ge._fmp_5y_growth("NVDA")) == 15 and len(calls) == 2  # other tickers are asked as usual
    clock.t += 30 * 86400 - 1
    assert ge._fmp_5y_growth("TSM") is None and len(calls) == 2  # still within the 30 days
    clock.t += 1
    replies[:] = [FakeResp(data=ESTIMATES)]
    assert round(ge._fmp_5y_growth("TSM")) == 15 and len(calls) == 3  # 30 days on: asked again
    assert b.status()["unenriched"] == 0


@pytest.mark.parametrize("replies_", [
    [FakeResp(200, {"Error Message": "Limit Reach"})],         # FMP's own daily cap
    [FakeResp(403, {"Error Message": "Invalid API KEY"})],     # a rejected key
    [FakeResp(401, None)],
    [FakeResp(429), FakeResp(429)],                            # rate-limited twice
    [FakeResp(500, None)], [FakeResp(503, None)],              # FMP unavailable
    [FakeResp(200, text="<html>maintenance</html>")],          # not JSON
])
def test_answers_that_say_nothing_about_the_ticker_never_skip_it(installed, replies_):
    ge, calls, replies, b, clock = installed
    replies[:] = list(replies_)
    assert ge._fmp_5y_growth("TSM") is None
    assert b.status()["unenriched"] == 0
    replies[:] = [FakeResp(data=ESTIMATES)]
    assert round(ge._fmp_5y_growth("TSM")) == 15  # asked again next time


def test_a_network_error_or_a_spent_budget_never_skips_a_ticker(installed, monkeypatch, tmp_path):
    ge, calls, replies, b, clock = installed
    get = ge._SESSION.get
    def boom(url):
        raise ConnectionError("down")
    monkeypatch.setattr(ge._SESSION, "get", boom)
    assert ge._fmp_5y_growth("TSM") is None and b.status()["unenriched"] == 0
    monkeypatch.setattr(ge._SESSION, "get", get)
    b.limit = 0  # spent: nothing is sent
    assert ge._fmp_5y_growth("TSM") is None and b.status()["unenriched"] == 0 and not calls


def test_enriched_or_capped_by_fmp_never_skips(installed):
    """A ticker FMP did enrich is asked again on its next analysis, as before."""
    ge, calls, replies, b, clock = installed
    replies[:] = [FakeResp(data=ESTIMATES), FakeResp(data=ESTIMATES)]
    assert ge._fmp_5y_growth("NVDA") and ge._fmp_5y_growth("NVDA") and len(calls) == 2
    assert b.status()["unenriched"] == 0


def test_estimate_growth_skips_fmp_for_a_ticker_it_had_nothing_for(installed):
    """Through the engine's entry point, as an analysis calls it: the second lookup sends nothing."""
    ge, calls, replies, b, clock = installed
    info = {"pegRatio": 2.0}
    replies[:] = [PREMIUM]
    g1, src1 = ge.estimate_growth("TSM", info, None, 30.0, enrich=True)
    g2, src2 = ge.estimate_growth("TSM", info, None, 30.0, enrich=True)
    assert len(calls) == 1 and "fmp" not in src1 and (g1, src1) == (g2, src2)


def test_skip_list_is_shared_and_survives_restarts(tmp_path):
    path = str(tmp_path / "b.sqlite3")
    clock = Clock()
    FmpBudget(path, clock=clock).mark_unenriched("tsm")
    other = FmpBudget(path, clock=clock)  # another worker process, or after a restart
    assert other.acquire("TSM") is False and other.status() == dict(other.status(), used=0, unenriched=1)
    assert FmpBudget(path, clock=clock, skip_days=0).acquire("NEW")  # 0 = never skip
