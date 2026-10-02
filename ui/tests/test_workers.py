"""Concurrent analysis: several JobManager workers, in threads or in worker processes (ui/workers.py)."""
import multiprocessing as mp
import threading
import time

import pytest

from ui.analysis import TickerAnalyzer
from ui import config
from ui.config import CPU_WORKERS, Settings, auto_workers
from ui.jobs import DayStore, JobManager
from ui.server import parse_args
from ui.tests import fakes

SPEC = "ui.tests.fakes:process_backends"


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    return s


def wait_final(jm, sym, timeout=30):
    """Looks ``sym`` up (once), then polls it like the browser does until it finishes."""
    t0, poll = time.time(), False
    while time.time() - t0 < timeout:
        snap, poll = jm.request(sym, poll=poll), True
        if snap["status"] in ("done", "nodata", "error"):
            return snap
        time.sleep(0.05)
    raise AssertionError(f"{sym} never finished: {snap}")


def wait_no_children(timeout=10):
    t0 = time.time()
    while mp.active_children() and time.time() - t0 < timeout:
        time.sleep(0.1)
    return mp.active_children()


def overlap(a, b):
    return a.started < b.finished and b.started < a.finished


# ── settings ────────────────────────────────────────────────────────────────
def test_worker_count_defaults(monkeypatch):
    monkeypatch.setattr(config, "available_mb", lambda: 64 * 1024)
    s = Settings()
    assert s.workers == 0
    assert s.analysis_workers(with_ai=True) == 1  # the local model is the bottleneck: one at a time
    assert s.analysis_workers(with_ai=False) == CPU_WORKERS
    s.workers = 3
    assert s.analysis_workers(with_ai=True) == s.analysis_workers(with_ai=False) == 3


def test_auto_workers_fit_in_free_memory(monkeypatch):
    assert auto_workers(free_mb=64 * 1024) == CPU_WORKERS
    assert auto_workers(free_mb=5 * config.WORKER_MB + 50) == min(5, CPU_WORKERS)
    assert auto_workers(free_mb=100) == 2  # never below two
    monkeypatch.setattr(config, "available_mb", lambda: None)  # psutil missing: CPU-based
    assert auto_workers() == CPU_WORKERS


def test_workers_flag_and_env(monkeypatch):
    assert parse_args(["--workers", "6"])[0].workers == 6
    assert parse_args(["--workers", "-2"])[0].workers == 0
    monkeypatch.setenv("LYNCH_UI_WORKERS", "5")
    assert parse_args([])[0].workers == 5


# ── worker threads ──────────────────────────────────────────────────────────
def test_thread_workers_analyse_at_the_same_time(settings):
    entered, release = threading.Semaphore(0), threading.Event()

    def edge(*a):
        entered.release()
        release.wait(10)
        return fakes.EDGE

    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends(engine=fakes.AnyEngine,
                                                                                          edge=edge)),
                    store=DayStore(), workers=2, deadline=30)
    try:
        jm.request("MSFT")
        jm.request("AAPL")
        assert entered.acquire(timeout=5) and entered.acquire(timeout=5)  # both inside the edge stage
        st = jm.cache_stats()
        assert st["running"] == "AAPL, MSFT" and st["workers"] == 2 and st["queue"] == 0
        third = jm.request("NVDA")
        assert third["status"] == "queued" and third["queue_position"] == 2
        release.set()
        assert {wait_final(jm, s)["status"] for s in ("MSFT", "AAPL", "NVDA")} == {"done"}
        assert jm.cache_stats()["running"] is None
    finally:
        release.set()
        jm.shutdown()


# ── worker processes ────────────────────────────────────────────────────────
def test_process_workers_analyse_in_parallel(settings):
    store = DayStore()
    jm = JobManager(settings, store=store, workers=2, processes=True, backends_spec=SPEC, deadline=60)
    try:
        assert jm.request("SLOWA")["status"] in ("queued", "running")
        jm.request("SLOWB")
        a, b = wait_final(jm, "SLOWA"), wait_final(jm, "SLOWB")
        assert a["status"] == b["status"] == "done"
        assert overlap(jm._recent["SLOWA"], jm._recent["SLOWB"])  # 1 s edge stages ran side by side
        entry = store.peek("SLOWA")
        assert entry["name"] == "SLOWA Inc." and entry["stages"]["plot"] == "done"
        assert entry["plot_url"].startswith("/plots/SLOWA.png") and jm.plot_path("SLOWA").endswith(".png")
        assert entry["_ai_inputs"]["row"]["Ticker"] == "SLOWA"  # the AI overview still gets its inputs
        assert "_ai_inputs" not in a["data"] and "plot_file" not in a["data"]
    finally:
        jm.shutdown()
    assert not wait_no_children(), "worker processes outlived shutdown"


def test_process_worker_streams_stage_progress(settings):
    jm = JobManager(settings, store=DayStore(), workers=2, processes=True, backends_spec=SPEC, deadline=60)
    try:
        jm.request("SLOWP")
        seen = set()
        t0 = time.time()
        while time.time() - t0 < 30:
            snap = jm.request("SLOWP", poll=True)
            if snap["status"] == "running" and snap["stage"]:
                seen.add(snap["stage"])
                assert snap["data"]["ticker"] == "SLOWP"
            if snap["status"] == "done":
                break
            time.sleep(0.02)
        assert "edge" in seen and snap["status"] == "done"  # partial results arrived before the end
    finally:
        jm.shutdown()


def test_process_watchdog_kills_hung_worker_and_replaces_it(settings):
    store = DayStore()
    jm = JobManager(settings, store=store, workers=1, processes=True, backends_spec=SPEC, deadline=6)
    try:
        jm.request("HANG")
        snap = wait_final(jm, "HANG", timeout=20)
        assert snap["status"] == "error" and "timed out" in snap["error"]
        assert wait_final(jm, "MSFT")["status"] == "done"  # a fresh worker process took over
        time.sleep(0.5)
        assert len(mp.active_children()) == 1  # the hung process was killed, not orphaned
        assert store.peek("HANG") is None
    finally:
        jm.shutdown()
    assert not wait_no_children()


def test_process_worker_crash_fails_the_job_only(settings):
    jm = JobManager(settings, store=DayStore(), workers=1, processes=True, backends_spec=SPEC, deadline=60)
    try:
        snap = wait_final(jm, "CRASH")
        assert snap["status"] == "error" and "worker exited" in snap["error"]
        assert wait_final(jm, "AMD")["status"] == "done"  # the worker started a new process
    finally:
        jm.shutdown()
    assert not wait_no_children()


def test_worker_processes_run_blas_single_threaded(settings, monkeypatch):
    import os
    from ui.workers import ProcessRunner
    for k in ("OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OMP_NUM_THREADS", "2")  # set by hand: left alone
    r = ProcessRunner(settings, SPEC)
    try:
        env = r.run("ENVX")["edge"]["env"]
    finally:
        r.close()
    assert env == {"OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"}
    assert "OPENBLAS_NUM_THREADS" not in os.environ  # the parent's own environment is restored
