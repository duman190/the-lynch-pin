"""Step 4: local LM Studio client, narrative parsing, AI jobs and endpoint."""
import http.client
import json
import threading
import time

import pytest

from ui import llm as L
from ui.analysis import TickerAnalyzer
from ui.config import Settings
from ui.jobs import DayStore, JobManager
from ui.server import PortalApp, PortalServer, make_handler
from ui.tests import fake_lmstudio, fakes


@pytest.fixture
def lm():
    httpd, state = fake_lmstudio.serve()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def settings(tmp_path, lm):
    s = Settings()
    s.cache_dir = str(tmp_path / "cache")
    s.llm_base_url = lm[0]
    return s


def test_defaults_are_configurable(monkeypatch):
    assert Settings().llm_ctx == 65536 and Settings().llm_base_url == "http://127.0.0.1:1234"
    assert Settings().llm_model == ""
    monkeypatch.setenv("LYNCH_LLM_MODEL", "google/gemma-3-27b")
    monkeypatch.setenv("LYNCH_LLM_CTX", "32768")
    s = Settings()
    assert s.llm_model == "google/gemma-3-27b" and s.llm_ctx == 32768


def test_cli_overrides():
    from ui.server import parse_args
    s, _ = parse_args(["--llm-model", "qwen3-8b", "--llm-ctx", "16384", "--llm-url", "http://10.0.0.5:8080/"])
    assert (s.llm_model, s.llm_ctx, s.llm_base_url) == ("qwen3-8b", 16384, "http://10.0.0.5:8080")


def test_strip_thinking():
    assert L.strip_thinking("<think>a\nb</think>\nHello") == "Hello"
    assert L.strip_thinking("Answer <THINK>cut off reasoning") == "Answer"
    assert L.strip_thinking("plain") == "plain"


def test_status_offline_is_quiet():
    s = Settings()
    s.llm_base_url = "http://127.0.0.1:9"  # nothing listens on discard port
    st = L.LocalLLMClient(s).status()
    assert st["available"] is False and "not reachable" in st["reason"] and st["ctx"] == 65536


def test_status_prefers_loaded_model_and_real_ctx(settings, lm):
    lm[1]["loaded_ctx"] = 8192
    st = L.LocalLLMClient(settings).status()
    assert st["available"] and st["model"] == fake_lmstudio.MODEL
    assert st["model_short"] == "qwen3-30b-a3b" and st["ctx"] == 8192 and st["ctx_configured"] == 65536


def test_configured_model_must_exist(settings):
    settings.llm_model = "does-not-exist"
    st = L.LocalLLMClient(settings).status()
    assert st["available"] is False and "not available" in st["reason"]


def test_generate_budgets_tokens_and_strips_think(settings, lm):
    c = L.LocalLLMClient(settings)
    text, meta = c.generate("- MSFT: PE 28")
    assert not text.startswith("<think>") and "$MSFT:" in text
    path, req = lm[1]["requests"][-1]
    assert path == "/v1/chat/completions" and req["stream"] is False
    assert req["model"] == fake_lmstudio.MODEL and req["max_tokens"] == settings.llm_max_tokens
    assert meta["finish_reason"] == "stop"


def test_prompt_larger_than_ctx_rejected(settings, lm):
    lm[1]["loaded_ctx"] = 1024
    with pytest.raises(L.LLMError, match="does not fit"):
        L.LocalLLMClient(settings).generate("x" * 6000)


def test_model_not_loaded_maps_to_unavailable(settings, lm):
    lm[1]["fail"] = True
    c = L.LocalLLMClient(settings)
    with pytest.raises(L.LLMUnavailable, match="not loaded"):
        c.generate("- MSFT: x")
    assert c.status()["available"] is False  # cached negative status


def test_autoload_sends_ctx(settings, lm):
    settings.llm_autoload = True
    L.LocalLLMClient(settings).generate("- MSFT: x")
    loads = [r for p, r in lm[1]["requests"] if p == "/api/v1/models/load"]
    assert loads == [{"model": fake_lmstudio.MODEL, "context_length": 65536}]


def test_parse_narrative_mirrors_main():
    raw = ("SENTIMENT: SENTIMENT: $QQQ rallies on AI\n\nSECTION 2 — PER-TICKER ANALYSIS:\n"
           "$ON:\n🤖: ON is cheap.\n\n📊 Reverse DCF: math here.\n\n🧪 Stomach Test: risks.\n\n"
           "$ONTO:\n🤖: other ticker")
    p = L.parse_narrative(raw, "ON")
    assert p["sentiment"] == "QQQ rallies on AI"
    assert p["overview"] == "ON is cheap." and p["reverse_dcf"] == "math here." and p["stomach_test"] == "risks."
    assert "other ticker" not in p["block"]


def _analysed(settings):
    return TickerAnalyzer(settings, backends=fakes.backends()).run("MSFT")


def test_ticker_narrative_end_to_end(settings, lm):
    data = _analysed(settings)
    out = L.ticker_narrative(L.LocalLLMClient(settings), data)
    assert out["status"] == "done" and out["complete"] and out["attempts"] == 1
    n = out["narrative"]
    assert "sleep-well compounder" in n["overview"] and "Reverse" not in n["overview"]
    assert n["reverse_dcf"].startswith("MSFT sells") and n["stomach_test"].startswith("AI capex")
    assert n["sentiment"].startswith("Mega-cap")
    prompt = lm[1]["requests"][-1][1]["messages"][0]["content"]
    assert "Income Grade: A+" in prompt and "Credit Rating: AAA" in prompt and "6M Directional Edge: BULL" in prompt


def test_sloppy_small_model_is_normalised(settings, lm):
    lm[1]["sloppy"] = True  # "TICKER: MSFT" headers
    out = L.ticker_narrative(L.LocalLLMClient(settings), _analysed(settings))
    assert out["status"] == "done" and "compounder" in out["narrative"]["overview"]


def test_prompt_inputs_none_safe(settings, lm):
    data = _analysed(settings)
    data["_ai_inputs"].update(g=None, b=None, t=None, e=None)
    assert L.ticker_narrative(L.LocalLLMClient(settings), data)["status"] == "done"


def test_nodata_has_no_ai(settings):
    assert L.ticker_narrative(None, {"_ai_inputs": {"row": None}})["status"] == "error"


# ── jobs + HTTP ─────────────────────────────────────────────────────────────
def wait(fn, pred, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if pred(v):
            return v
        time.sleep(0.02)
    raise AssertionError(v)


def make(settings):
    client = L.LocalLLMClient(settings)
    store = DayStore()
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), store=store, llm=client)
    return jm, store, client


def test_ai_job_flow_and_cache(settings, lm):
    jm, store, _ = make(settings)
    assert jm.request_ai("MSFT")["need_quant"] is True
    wait(lambda: jm.request("MSFT"), lambda s: s["status"] == "done")
    snap = wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done")
    assert snap["narrative"]["overview"] and snap["model_short"] == "qwen3-30b-a3b"
    assert store.peek("MSFT")["ai"]["status"] == "done"
    n = len(lm[1]["requests"])
    jm._recent_ai.clear()
    again = jm.request_ai("MSFT")
    assert again["cached"] is True and len(lm[1]["requests"]) == n  # no second LLM call
    jm.shutdown()


def test_ai_offline_returns_unavailable_without_queueing(settings, lm):
    settings.llm_base_url = "http://127.0.0.1:9"
    jm, _, _ = make(settings)
    wait(lambda: jm.request("MSFT"), lambda s: s["status"] == "done")
    snap = jm.request_ai("MSFT")
    assert snap["status"] == "unavailable" and "not reachable" in snap["error"]
    assert jm.cache_stats()["ai_queue"] == 0
    jm.shutdown()


def test_ai_for_nodata_ticker_unavailable(settings, lm):
    eng = type("E", (fakes.FakeEngine,), {"infos": {"QQQ": {"quoteType": "ETF", "regularMarketPrice": 5.0}}})
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends(engine=eng)),
                    store=DayStore(), llm=L.LocalLLMClient(settings))
    wait(lambda: jm.request("QQQ"), lambda s: s["status"] == "nodata")
    assert jm.request_ai("QQQ")["status"] == "unavailable"
    jm.shutdown()


def test_ai_http_endpoint_and_health(settings, lm):
    jm, _, client = make(settings)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm, llm=client)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        b = json.loads(r.read())
        c.close()
        return r.status, b
    try:
        st, h = get("/api/health")
        assert h["features"]["ai"] is True
        h = wait(lambda: get("/api/health")[1], lambda h: h["ai"].get("available"))
        assert h["ai"]["ctx"] == 65536
        wait(lambda: get("/api/ticker/MSFT")[1], lambda s: s["status"] == "done")
        snap = wait(lambda: get("/api/ticker/MSFT/ai")[1], lambda s: s["status"] == "done")
        assert snap["narrative"]["stomach_test"]
        st, _ = get("/api/ticker/MSFT/ai?refresh=1")
        assert st == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


class ScriptedClient:
    """generate() replays scripted (text | Exception) results; records max_tokens per call."""

    def __init__(self, script, finish="stop"):
        self.script, self.calls, self.finish = list(script), [], finish

    def status(self, **k):
        return {"available": True, "model": "m", "ctx": 65536}

    def generate(self, prompt, max_tokens=None):
        self.calls.append(max_tokens)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {"model": "m", "finish_reason": self.finish, "max_tokens": max_tokens or 4096, "elapsed_s": 1}


def test_length_finish_retries_with_double_budget(settings):
    c = ScriptedClient(["<truncated>", fake_lmstudio.canned_reply("- MSFT: x")], finish="length")
    out = L.ticker_narrative(c, _analysed(settings))
    assert c.calls == [None, 8192] and out["status"] == "done" and out["attempts"] == 2


def test_partial_kept_when_retry_errors(settings):
    partial = "SENTIMENT: ok\n\n$MSFT:\n📊 Reverse DCF: only the math"  # no 🤖 → unusable, coverage 0
    c = ScriptedClient([partial, L.LLMError("timed out")])
    out = L.ticker_narrative(c, _analysed(settings))
    assert out["status"] == "done" and out["complete"] is False
    assert out["narrative"]["reverse_dcf"] == "only the math"


def test_first_attempt_error_propagates(settings):
    with pytest.raises(L.LLMError):
        L.ticker_narrative(ScriptedClient([L.LLMError("boom")]), _analysed(settings))


def test_health_probe_never_blocks(settings):
    import socket
    sink = socket.socket()
    sink.bind(("127.0.0.1", 0))
    sink.listen(5)  # accepts TCP, never answers
    settings.llm_base_url = f"http://127.0.0.1:{sink.getsockname()[1]}"
    c = L.LocalLLMClient(settings)
    t0 = time.time()
    st = c.status(block=False)
    assert time.time() - t0 < 0.5 and st.get("checking") is True and st["available"] is False
    sink.close()


def test_configured_model_missing_without_native_api(settings, lm, monkeypatch):
    settings.llm_model = "ghost-model"
    c = L.LocalLLMClient(settings)
    monkeypatch.setattr(c, "_native_models", lambda: [])
    assert c.status()["available"] is False


def test_jit_warning_when_not_loaded(settings, lm):
    lm[1]["state"] = "not-loaded"
    st = L.LocalLLMClient(settings).status()
    assert st["available"] and st["ctx_loaded"] is None and "JIT" in st["warning"]


def test_refresh_during_ai_does_not_attach_stale_narrative(settings, lm):
    gate = threading.Event()
    entered = threading.Event()

    class SlowClient(L.LocalLLMClient):
        def generate(self, prompt, max_tokens=None):
            entered.set()
            gate.wait(10)
            return super().generate(prompt, max_tokens)

    store = DayStore()
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), store=store,
                    llm=SlowClient(settings))
    wait(lambda: jm.request("MSFT"), lambda s: s["status"] == "done")
    old_entry = store.peek("MSFT")
    jm.request_ai("MSFT")
    assert entered.wait(5)
    jm._recent.clear()
    jm.request("MSFT", refresh=True)
    wait(lambda: store.peek("MSFT"), lambda e: e is not old_entry)
    gate.set()
    wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done")
    assert "ai" not in store.peek("MSFT")  # fresh analysis → fresh AI on next request
    assert old_entry["ai"]["status"] == "done"
    jm.shutdown()


@pytest.fixture(autouse=True)
def _reset_fakes():
    infos, rows = dict(fakes.FakeEngine.infos), dict(fakes.FakeEngine.rows)
    yield
    fakes.FakeEngine.infos, fakes.FakeEngine.rows = infos, rows
