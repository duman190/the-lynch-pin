"""Analysis worker processes: run ``TickerAnalyzer`` in a child process, one ticker at a time.

Half of a cold lookup is CPU (the 6M edge backtest, the 300-dpi chart, pandas), so threads in one
process would queue on the GIL. A child process per worker uses the other cores, gives every worker
its own pyplot state and engine globals, and can be killed when an analysis hangs (a thread cannot).

Each ``ProcessRunner`` owns one spawned child that imports the engine once and then serves tickers
sent over a pipe. Stage updates stream back as they happen, so the UI still renders block by block.

Children run their BLAS / OpenMP math single-threaded. The edge backtest's KDE (scipy) otherwise
starts a thread per core in every process: 8 workers × 8 BLAS threads made the stage ~20× slower.
"""
import importlib
import multiprocessing as mp
import os
import threading

# Read by numpy's BLAS (OpenBLAS / Accelerate / MKL) when the child imports it. A value already in the
# environment wins, so the parallelism can still be tuned by hand.
CHILD_ENV = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
             "VECLIB_MAXIMUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
_spawn_lock = threading.Lock()  # os.environ is process-wide: one start at a time


class WorkerCancelled(Exception):
    pass


def _resolve(spec):
    """'pkg.module:function' → the function (lets tests run the child on offline fakes)."""
    mod, _, name = spec.partition(":")
    return getattr(importlib.import_module(mod), name)


def _child_main(conn, settings, backends_spec):
    import matplotlib
    matplotlib.use("Agg")
    from ui.analysis import TickerAnalyzer
    analyzer = TickerAnalyzer(settings, backends=_resolve(backends_spec)() if backends_spec else None)
    try:
        analyzer.backends  # import yfinance / pandas / pyplot now, not on the first lookup
    except Exception as e:  # reported per job by run() below
        print(f"⚠️  analysis worker could not load the engine: {type(e).__name__}: {e}", flush=True)
    while True:
        try:
            sym = conn.recv()
        except (EOFError, OSError):
            return
        if sym is None:
            return

        def on_stage(name, state, data):
            conn.send(("stage", name, state, {k: v for k, v in data.items() if k != "_ai_inputs"}))

        try:
            msg = ("result", analyzer.run(sym, on_stage=on_stage))
        except Exception as e:
            msg = ("error", f"{type(e).__name__}: {e}")
        try:
            conn.send(msg)
        except Exception as e:  # e.g. an unpicklable value in the result
            conn.send(("error", f"{type(e).__name__}: {e}"))


class ProcessRunner:
    """One analysis child process. ``run`` has the same contract as ``TickerAnalyzer.run``."""

    _ctx = mp.get_context("spawn")  # fork is unsafe with threads (and with macOS frameworks)

    def __init__(self, settings, backends_spec=None):
        parent, child = self._ctx.Pipe()
        self.proc = self._ctx.Process(target=_child_main, args=(child, settings, backends_spec),
                                      name="lynch-analysis", daemon=True)
        with _spawn_lock:  # the spawned interpreter inherits os.environ as it is at start()
            added = [k for k in CHILD_ENV if k not in os.environ]
            os.environ.update({k: CHILD_ENV[k] for k in added})
            try:
                self.proc.start()
            finally:
                for k in added:
                    os.environ.pop(k, None)
        child.close()
        self.conn = parent
        self._lock = threading.Lock()

    def alive(self):
        return self.proc.is_alive()

    def run(self, sym, on_stage=None, cancelled=None, poll=0.25):
        emit = on_stage or (lambda *a: None)
        stop = cancelled or (lambda: False)
        with self._lock:
            self.conn.send(sym)
            while True:
                if not self.conn.poll(poll):
                    if stop():
                        self.close()
                        raise WorkerCancelled("cancelled")
                    if not self.proc.is_alive():
                        raise RuntimeError(f"analysis worker exited (code {self.proc.exitcode})")
                    continue
                try:
                    msg = self.conn.recv()
                except (EOFError, OSError):
                    raise RuntimeError(f"analysis worker exited (code {self.proc.exitcode})")
                if msg[0] == "stage":
                    emit(*msg[1:])
                elif msg[0] == "result":
                    return msg[1]
                else:
                    raise RuntimeError(msg[1])

    def close(self):
        """Stop the child now (a hung analysis cannot be interrupted any other way)."""
        try:
            self.conn.close()
        except OSError:
            pass
        if self.proc.is_alive():
            self.proc.kill()
        self.proc.join(5)
