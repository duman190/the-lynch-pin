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
    s.llm_reasoning = "on"  # most tests exercise the thinking stream; the "off" switch has its own tests
    return s


def test_defaults_are_configurable(monkeypatch):
    assert Settings().llm_ctx == 65536 and Settings().llm_base_url == "http://127.0.0.1:1234"
    assert Settings().llm_reasoning == "off"
    assert Settings().llm_model == ""
    monkeypatch.setenv("LYNCH_LLM_MODEL", "google/gemma-3-27b")
    monkeypatch.setenv("LYNCH_LLM_CTX", "32768")
    s = Settings()
    assert s.llm_model == "google/gemma-3-27b" and s.llm_ctx == 32768


def test_cli_overrides():
    from ui.server import parse_args
    s, _ = parse_args(["--llm-model", "qwen3-8b", "--llm-ctx", "16384", "--llm-url", "http://10.0.0.5:8080/",
                       "--llm-reasoning", "on"])
    assert (s.llm_model, s.llm_ctx, s.llm_base_url, s.llm_reasoning) == ("qwen3-8b", 16384, "http://10.0.0.5:8080", "on")
    assert parse_args([])[0].llm_reasoning == "off"


def test_reasoning_off_sends_switch_and_skips_thinking(settings, lm):
    settings.llm_reasoning = "off"
    c = L.LocalLLMClient(settings)
    text, meta = c.generate("- MSFT: x")
    req = lm[1]["requests"][-1][1]
    assert req["reasoning_effort"] == "none" and meta["reasoning"] == "off"
    assert meta["metrics"]["reasoning_tokens"] == 0 and meta["metrics"]["thinking_s"] is None
    assert text.startswith("🤖:") and c.status()["reasoning"] == "off"


def test_reasoning_on_leaves_server_default(settings, lm):
    text, meta = L.LocalLLMClient(settings).generate("- MSFT: x")
    assert "reasoning_effort" not in lm[1]["requests"][-1][1] and meta["metrics"]["reasoning_tokens"] > 0


def test_server_rejecting_reasoning_switch_falls_back_once(settings, lm):
    settings.llm_reasoning = "off"
    lm[1]["reject_reasoning_effort"] = True
    c = L.LocalLLMClient(settings)
    text, meta = c.generate("- MSFT: x")
    chats = [r for p, r in lm[1]["requests"] if p == "/v1/chat/completions"]
    assert [("reasoning_effort" in r) for r in chats] == [True, False] and text.startswith("🤖:")
    assert meta["reasoning"] == "unsupported" and c.status(force=True)["reasoning"] == "unsupported"
    c.generate("- MSFT: x")
    chats = [r for p, r in lm[1]["requests"] if p == "/v1/chat/completions"]
    assert len(chats) == 3 and "reasoning_effort" not in chats[-1]  # remembered: no second rejection


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


def test_generate_streams_with_metrics_and_separates_reasoning(settings, lm):
    seen = []
    c = L.LocalLLMClient(settings)
    text, meta = c.generate("- MSFT: PE 28", on_delta=lambda r, t, m: seen.append((r, t, dict(m))))
    path, req = lm[1]["requests"][-1]
    assert path == "/v1/chat/completions" and req["stream"] is True
    assert req["stream_options"] == {"include_usage": True}
    assert req["model"] == fake_lmstudio.MODEL and req["max_tokens"] == settings.llm_max_tokens == 8192
    assert text.startswith("🤖:") and fake_lmstudio.REASONING not in text
    assert "".join(r for r, _, _ in seen) == fake_lmstudio.REASONING  # reasoning streamed separately
    assert "".join(t for _, t, _ in seen) == text
    assert seen[0][2]["ttft_s"] is not None and seen[-1][2]["tokens"] == len(seen)
    m = meta["metrics"]
    assert meta["finish_reason"] == "stop" and m["tokens"] == meta["usage"]["completion_tokens"]
    assert m["reasoning_tokens"] > 0 and m["content_tokens"] > 0 and m["prompt_tokens"] == 900


def test_inline_think_tags_are_split_while_streaming(settings, lm):
    lm[1]["inline_think"] = True
    reasoning = []
    text, meta = L.LocalLLMClient(settings).generate("- MSFT: x", on_delta=lambda r, t, m: reasoning.append(r))
    assert "<think>" not in text and "</think>" not in text and text.startswith("🤖:")
    assert "".join(reasoning).strip() == fake_lmstudio.REASONING
    assert meta["metrics"]["reasoning_tokens"] > 0


def test_tokens_are_delivered_one_by_one_as_sent(settings, lm):
    lm[1].update(delay=0.2, limit=6)
    stamps = []
    L.LocalLLMClient(settings).generate("- MSFT: x", on_delta=lambda r, t, m: stamps.append(time.monotonic()))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 6 and all(g > 0.12 for g in gaps), gaps  # no batching of several tokens per read


def test_prefilled_think_is_moved_out_of_the_answer(settings, lm):
    lm[1]["prefill_think"] = True
    seen, rewound = [], []
    text, meta = L.LocalLLMClient(settings).generate(
        "- MSFT: x", on_delta=lambda r, t, m: seen.append((r, t)), on_rewind=rewound.append)
    assert text.startswith("🤖:") and fake_lmstudio.REASONING not in text
    assert len(rewound) == 1 and (rewound[0] + "".join(r for r, _ in seen)).strip() == fake_lmstudio.REASONING
    m = meta["metrics"]
    assert m["reasoning_tokens"] >= len(fake_lmstudio.REASONING.split()) and m["content_tokens"] > 0


def test_prefilled_think_streams_into_the_reasoning_box(settings, lm):
    lm[1].update(prefill_think=True, delay=0.01)
    jm, _, _ = make(settings)
    wait(lambda: jm.request("MSFT"), lambda s: s["status"] == "done")
    jm.request_ai("MSFT")
    snap = wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done")
    assert snap["narrative"]["overview"].startswith("MSFT is") and snap["metrics"]["reasoning_tokens"] > 0
    jm.shutdown()


def test_cancelled_generation_closes_stream(settings, lm):
    lm[1]["delay"] = 0.01
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 5

    with pytest.raises(L.LLMCancelled):
        L.LocalLLMClient(settings).generate("- MSFT: x", cancelled=cancelled)
    time.sleep(0.3)
    assert lm[1]["sent_chunks"] < 40  # the server stopped streaming once we hung up


def test_non_streaming_server_fallback(settings):
    class R:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def json(self):
            return {"model": "m", "choices": [{"finish_reason": "stop", "message": {
                "content": "<think>hmm</think>🤖: ok", "reasoning_content": None}}], "usage": {}}

        def close(self):
            pass

    meter = L.StreamMeter()
    content, finish, usage, served = L.LocalLLMClient._consume(R(), meter, None, None)
    assert content == "🤖: ok" and finish == "stop" and meter.reasoning_tokens == 1


def test_think_splitter_implicit_open_tag():
    sp = L.ThinkSplitter()
    out = [sp.feed(x) for x in ["Okay, weigh", " it.</th", "ink>\n\n🤖: Fine"]]
    assert sp.take_rewind() is True and sp.take_rewind() is False
    # text before "</think>" was emitted as content (nothing marked it as reasoning yet) and the
    # rewind flag tells the caller to move it; only the partial-tag tail was held back
    assert out[0] == ("", "Okay, weigh") and out[1] == ("", " it.") and out[2] == ("", "\n\n🤖: Fine")
    sp2 = L.ThinkSplitter()
    sp2.feed("<think>a</think>b")
    assert sp2.feed(" c </think> d") == ("", " c </think> d") and not sp2.take_rewind()


def test_think_splitter_handles_tags_split_across_chunks():
    sp = L.ThinkSplitter()
    out = [sp.feed(x) for x in ["Hi <th", "ink>reason", "ing</thi", "nk> answer", " <"]]
    out.append(sp.flush())
    assert "".join(r for r, _ in out) == "reasoning"
    assert "".join(c for _, c in out) == "Hi  answer <"


def test_stream_meter_ttft_and_rate():
    t = {"now": 100.0}
    m = L.StreamMeter(clock=lambda: t["now"])
    t["now"] = 102.5
    m.chunk("think", "")        # first token (reasoning) at 2.5 s → TTFT
    t["now"] = 103.5
    for _ in range(40):         # 40 answer tokens over 2 s after 1 s of thinking
        m.chunk("", "tok")
        t["now"] += 0.05
    t["now"] -= 0.05
    live = m.metrics()
    assert live["ttft_s"] == 2.5 and live["tokens"] == 41 and live["reasoning_tokens"] == 1
    assert live["content_tokens"] == 40 and live["thinking_s"] == 1.0
    assert live["tok_s"] == pytest.approx(40 / 2.95, rel=0.01)
    m.finish({"completion_tokens": 82, "prompt_tokens": 800})  # server's exact count wins at the end
    final = m.metrics()
    assert final["tokens"] == 82 and final["tok_s"] == pytest.approx(81 / 2.95, rel=0.01)
    assert final["prompt_tokens"] == 800
    assert (final["reasoning_tokens"], final["content_tokens"]) == (2, 80)  # split in the observed ratio


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


def test_parse_sections_tolerates_small_model_formatting():
    raw = ("**$MSFT:**\n**🤖:** Great business.\n\n📊 Reverse 5Y DCF: [the math]\n\n"
           "🐻 \"Stomach Test\" (why it can underperform in the next 5 years): capex risk")
    assert L.parse_sections(raw) == {"overview": "Great business.", "reverse_dcf": "the math",
                                     "stomach_test": "capex risk"}
    assert L.parse_sections("$MSFT:\nJust prose, no labels.")["overview"] == "Just prose, no labels."
    assert L.parse_sections("<think>x</think>🤖: a\n\n🧪 Stomach Test: b")["stomach_test"] == "b"


def test_parse_sections_emoji_only_labels_from_real_qwen_output():
    # Qwen3.6 once answered with bare emoji, no "Reverse DCF:" / "Stomach Test:" text and no colons
    raw = ("🤖 Let's look at the books before we buy the shovel. Caterpillar is a B grade.\n\n"
           "📊 Caterpillar makes the heavy equipment that digs the world's infrastructure. The math: 11.9% base ROI.\n\n"
           "🧪 Now let's talk about why your money might sleep poorly for the next five years.")
    sec = L.parse_sections(raw)
    assert sec["overview"].startswith("Let's look") and sec["reverse_dcf"].startswith("Caterpillar makes")
    assert sec["stomach_test"].startswith("Now let's talk") and L.section_score(sec) == 3
    assert L.parse_sections("🤖: one line with 📊 inside the text")["reverse_dcf"] == ""  # mid-line emoji ≠ label


def test_parse_sections_label_words_after_wrong_or_missing_emoji():
    # qwen3.6-35b-a3b wrote "🧧 Stomach Test:" — the words still mark the section
    sec = L.parse_sections("🤖: a\n\n📊 Reverse DCF: b\n\n🧧 Stomach Test: c")
    assert sec == {"overview": "a", "reverse_dcf": "b", "stomach_test": "c"}
    sec = L.parse_sections("🤖: a\nReverse DCF: b\nStomach Test: c")
    assert sec == {"overview": "a", "reverse_dcf": "b", "stomach_test": "c"}
    assert L.parse_sections("🤖: The Stomach Test: stays mid-line")["stomach_test"] == ""


def test_parse_sections_unlabelled_overview_before_labels():
    # qwen2.5-7b skipped "🤖:" but labelled the rest: the overview used to vanish ("partial reply")
    text = "$META:\nMeta owns the social graph.\n\n📊 Reverse 5Y DCF: the math\n\n🐻 Stomach test: the bear case"
    assert L.parse_sections(text) == {"overview": "Meta owns the social graph.", "reverse_dcf": "the math",
                                      "stomach_test": "the bear case"}
    live = L.parse_sections("Meta owns the social graph.\n\n📊 Rever", partial=True)
    assert live["overview"] == "Meta owns the social graph." and live["reverse_dcf"] == ""
    # a labelled overview still wins over stray text before it
    assert L.parse_sections("Sure!\n🤖: real overview\n📊 Reverse DCF: b")["overview"] == "real overview"


def test_parse_sections_three_unlabelled_paragraphs_in_order():
    text = "Walmart runs stores.\n\n-2.7% base ROI requires EPS +9.1%/yr.\n\nThe bear case is the PEG."
    assert L.parse_sections(text) == {"overview": "Walmart runs stores.", "reverse_dcf": "-2.7% base ROI requires EPS +9.1%/yr.",
                                      "stomach_test": "The bear case is the PEG."}
    assert L.parse_sections("One.\n\nTwo.")["overview"] == "One.\n\nTwo."  # final: only exactly three
    assert L.parse_sections("One.\n\nTwo.", partial=True)["reverse_dcf"] == "Two."  # streaming: in order
    assert L.parse_sections("A.\n\nB.\n\nC.\n\nD.")["reverse_dcf"] == ""


def test_parse_sections_partial_hides_half_arrived_label():
    live = L.parse_sections("🤖: Solid compounder.\n\n📊 Rever", partial=True)
    assert live == {"overview": "Solid compounder.", "reverse_dcf": "", "stomach_test": ""}
    for tail in ("\n\n📊", "\n\n🧪 Stomach Test (why it can underperform"):
        assert L.parse_sections("🤖: a" + tail, partial=True)["overview"] == "a"
        assert not L.parse_sections("🤖: a" + tail, partial=True)["reverse_dcf"]
    assert L.parse_sections("🤖: a\n\n📊 Caterpillar", partial=True)["reverse_dcf"] == "Caterpillar"
    assert L.parse_sections("🤖: a\n\n📊 Reverse DCF: b", partial=True)["reverse_dcf"] == "b"


def test_portal_messages_static_system_then_terse_data(settings):
    data = _analysed(settings)
    sys_msg, user = L.build_portal_messages(data)
    assert sys_msg == {"role": "system", "content": L.PORTAL_SYSTEM}  # identical for every ticker → KV-cached
    assert user["role"] == "user" and "MSFT" not in sys_msg["content"]
    low = sys_msg["content"].lower()
    for banned in ("sentiment", "character", "twitter", "tweet", "markdown formatting"):
        assert banned not in low, banned
    for label in ("🤖:", "📊 Reverse DCF:", "🧪 Stomach Test:"):
        assert label in sys_msg["content"]
    text = user["content"]
    assert text.startswith("$MSFT Microsoft Corporation")
    # pre-computed: the reverse-DCF sentence, fortress tag, RED cost lines, the red-flag verdict
    assert ("Reverse DCF: 13.5% base ROI requires EPS +13.0%/yr for 5 years, re-rating 21.8x to 22.4x forward PE; "
            "assumptions: realistic") in text  # Quick Overview's rule-based verdict
    assert "Income grade A+: revenue +18%, op income +18%, G&A +40% RED" in text
    assert "Credit AAA (fortress)" in text and text.endswith("Red flags: none")
    assert len(text) < 750


def test_portal_data_profile_analyst_target_and_flags():
    d = {"ticker": "INTC", "name": "Intel Corporation", "industry": "Semiconductors", "price": 119.33,
         "market_cap": 6.3e11, "currency": "USD",
         "profile": {"summary": "Intel Corporation designs and makes chips. It operates through three segments. "
                                "Founded in 1968.", "target_mean": 116.37, "recommendation": "buy",
                     "operating_margin": 0.12, "profit_margin": -0.2},
         "stats": {"PE": 0.0, "FwdPE": 57.9, "growth_pct": 42.6, "PEG": 1.36, "Mean": 0.77, "Dev_SD": 0.71,
                   "Bull": 30.9, "Base": 21.2, "Bear": 6.7, "history": "ok"},
         "quick": {"stomach_test": [
             {"level": "high", "text": "Forward PE 57.9x (above 40x): even next year's earnings look expensive."},
             {"level": "watch", "text": "Analysts' mean target (116.37) is below today's price (119.33)."},
             {"level": "watch", "text": "RSI 72: overbought in the short term."}]}}
    text = L.portal_data(d)
    assert "Intel Corporation designs and makes chips. It operates through three segments." in text
    assert "Founded" not in text  # the Quick Overview's first two sentences
    # a whole sentence for the model to copy (sign spelled out: small models flipped or doubled it)
    assert "Analysts' target $116.37 (2% downside). Consensus: buy." in text  # 🤖 copies it
    assert "no trailing earnings, FwdPE 57.9" in text and "op margin 12%, net -20%" in text
    assert text.endswith("Red flags: Forward PE 57.9x (above 40x); RSI 72: overbought in the short term")


def test_portal_data_accumulation_signal_shows_buy_zone():
    """The engine labels it ACCUMUL; the brief spells it out and adds the buy zone."""
    d = {"ticker": "AMZN", "stats": {}, "quick": {"stomach_test": []},
         "technicals": {"signal": "ACCUMUL", "price_vs_sma200": 4.2, "rsi": 54.0, "accumulation_zone": [236.4, 247.9]}}
    assert "Technicals ACCUMULATION, +4% vs SMA200, RSI 54, buy zone $236-248" in L.portal_data(d)
    d["technicals"]["signal"] = "BULLISH"
    assert "Technicals BULLISH, +4% vs SMA200, RSI 54\n" in L.portal_data(d)  # the zone only matters when buying


def test_scan_brief_matches_the_portal_data_block(settings):
    """main.py's AI prompt describes a ticker with exactly the portal's AI-overview data block."""
    from ui.tests import fakes
    portal = L.portal_data(_analysed(settings))
    scan = L.scan_brief(dict(fakes.MSFT_ROW), fakes.MSFT_INFO, fakes.INCOME, fakes.CREDIT, fakes.TECH, fakes.EDGE)
    assert scan == portal
    assert L.scan_brief(dict(fakes.MSFT_ROW, Ticker="MSFT*"), fakes.MSFT_INFO).startswith("$MSFT Microsoft")


def test_autoload_note_while_model_loads(settings, lm):
    settings.llm_autoload = True
    notes = []

    class Sink(L.NullSink):
        def begin(self, attempt, note=None):
            notes.append(note)

    c = L.LocalLLMClient(settings)
    assert c.autoload_pending() is True
    L.ticker_narrative(c, _analysed(settings), sink=Sink())
    assert notes == ["loading the model in LM Studio"] and c.autoload_pending() is False


def _analysed(settings):
    return TickerAnalyzer(settings, backends=fakes.backends()).run("MSFT")


def test_ticker_narrative_end_to_end(settings, lm):
    data = _analysed(settings)
    out = L.ticker_narrative(L.LocalLLMClient(settings), data)
    assert out["status"] == "done" and out["complete"] and out["attempts"] == 1
    n = out["narrative"]
    assert "sleep-well compounder" in n["overview"] and "Reverse" not in n["overview"]
    assert n["reverse_dcf"].startswith("MSFT sells") and n["stomach_test"].startswith("AI capex")
    assert set(n) == {"overview", "reverse_dcf", "stomach_test"}  # no sentiment any more
    assert out["metrics"]["ttft_s"] is not None and out["metrics"]["tokens"] > 0
    system, user = lm[1]["requests"][-1][1]["messages"]
    assert system == {"role": "system", "content": L.PORTAL_SYSTEM} and user["role"] == "user"
    assert "SENTIMENT" not in user["content"] and "Income grade A+" in user["content"] and "6M edge" in user["content"]


def test_sloppy_small_model_is_normalised(settings, lm):
    lm[1]["sloppy"] = True  # **🤖:** markdown-bold labels
    out = L.ticker_narrative(L.LocalLLMClient(settings), _analysed(settings))
    assert out["status"] == "done" and out["complete"] and "compounder" in out["narrative"]["overview"]
    assert "*" not in "".join(out["narrative"].values())


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

    def generate(self, prompt, max_tokens=None, on_delta=None, cancelled=None, on_rewind=None):
        self.calls.append(max_tokens)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {"model": "m", "finish_reason": self.finish, "max_tokens": max_tokens or 4096, "elapsed_s": 1,
                      "metrics": {"tokens": 10}}


def test_length_finish_retries_with_double_budget(settings):
    c = ScriptedClient(["", fake_lmstudio.canned_reply("- MSFT: x")], finish="length")
    out = L.ticker_narrative(c, _analysed(settings))
    assert c.calls == [None, 8192] and out["status"] == "done" and out["attempts"] == 2


def test_partial_kept_when_retry_errors(settings):
    partial = "📊 Reverse DCF: only the math"  # truncated after one section
    c = ScriptedClient([partial, L.LLMError("timed out")], finish="length")
    out = L.ticker_narrative(c, _analysed(settings))
    assert out["status"] == "done" and out["complete"] is False
    assert out["narrative"]["reverse_dcf"] == "only the math"


def test_usable_partial_is_not_retried(settings):
    c = ScriptedClient(["🤖: short but fine"])
    out = L.ticker_narrative(c, _analysed(settings))
    assert out["status"] == "done" and out["attempts"] == 1 and out["complete"] is False


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


def test_server_without_lmstudio_api_is_probed_once(settings, lm):
    lm[1]["no_native"] = True  # an OpenAI-compatible server only (standalone Splash 404s /api/v0/models)
    c = L.LocalLLMClient(settings)
    for _ in range(3):
        st = c.status(force=True)
        assert st["available"] and st["ctx_loaded"] is None and st["model"]
    assert lm[1]["native_probes"] == 1


def test_system_prompt_is_primed_once_then_again_after_a_reported_miss(settings, lm):
    c = L.LocalLLMClient(settings)
    msgs = L.build_portal_messages(_analysed(settings))
    c.generate(msgs)
    c.generate(msgs)
    chats = [r for path, r in lm[1]["requests"] if path == "/v1/chat/completions"]
    primes = [r for r in chats if r["messages"][-1]["content"] == ""]
    assert len(chats) == 3 and len(primes) == 1 and chats[0] is primes[0]  # primed before the first only
    assert primes[0]["messages"][0] == msgs[0] and primes[0]["max_tokens"] == 1 and not primes[0]["stream"]
    c._primed_at -= c.PRIME_EVERY  # a minute later the server reports it lost the prefix (Splash: cached_tokens 0)
    c._primed = None
    c.generate(msgs)
    chats = [r for path, r in lm[1]["requests"] if path == "/v1/chat/completions"]
    assert sum(r["messages"][-1]["content"] == "" for r in chats) == 2
    lm[1]["requests"].clear()
    c.generate("- MSFT: x")  # a bare user prompt has nothing to prime
    assert all(r["messages"][-1]["content"] for _, r in lm[1]["requests"])


def test_jit_warning_when_not_loaded(settings, lm):
    lm[1]["state"] = "not-loaded"
    st = L.LocalLLMClient(settings).status()
    assert st["available"] and st["ctx_loaded"] is None and "JIT" in st["warning"]


def test_refresh_during_ai_does_not_attach_stale_narrative(settings, lm):
    gate = threading.Event()
    entered = threading.Event()

    class SlowClient(L.LocalLLMClient):
        def generate(self, prompt, max_tokens=None, **kw):
            entered.set()
            gate.wait(10)
            return super().generate(prompt, max_tokens, **kw)

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


def test_llm_parallel_streams_overviews_at_once(settings, monkeypatch):
    monkeypatch.setitem(fakes.FakeEngine.infos, "AAPL", dict(fakes.MSFT_INFO))
    monkeypatch.setitem(fakes.FakeEngine.rows, "AAPL", dict(fakes.MSFT_ROW, Ticker="AAPL"))
    both = threading.Barrier(2, timeout=5)  # only passes when two generations run at the same time

    class PairClient(ScriptedClient):
        def generate(self, prompt, **kw):
            both.wait()
            return super().generate(prompt, **kw)

    settings.llm_parallel = 2
    c = PairClient([fake_lmstudio.canned_reply("- MSFT: x")] * 2)
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), store=DayStore(), llm=c)
    try:
        for sym in ("MSFT", "AAPL"):
            wait(lambda: jm.request(sym, poll=bool(jm.lookup(sym))), lambda s: s["status"] == "done")
        assert jm.ai_workers == 2
        for sym in ("MSFT", "AAPL"):
            jm.request_ai(sym)
        for sym in ("MSFT", "AAPL"):
            assert wait(lambda: jm.request_ai(sym), lambda s: s["status"] in ("done", "error"))["status"] == "done"
        assert jm.cache_stats()["ai_workers"] == 2 and jm.cache_stats()["ai_running"] is None
    finally:
        jm.shutdown()


def read_sse(port, path, timeout=15):
    """Minimal SSE client: returns [(event, data)] until the server closes the stream."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request("GET", path, headers={"Accept": "text/event-stream"})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    events, ev, data = [], None, []
    for raw in r:
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "" and ev:
            events.append((ev, json.loads("".join(data))))
            ev, data = None, []
    c.close()
    return events


def test_sse_streams_typing_with_live_metrics(settings, lm):
    lm[1]["delay"] = 0.01
    jm, store, client = make(settings)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm, llm=client)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        wait(lambda: jm.request("MSFT", poll=True) if jm.lookup("MSFT") else jm.request("MSFT"),
             lambda s: s["status"] == "done")
        assert jm.request_ai("MSFT")["status"] in ("queued", "running")
        events = read_sse(httpd.server_address[1], "/api/ticker/MSFT/ai/stream")
        kinds = [e for e, _ in events]
        assert kinds[0] == "snapshot" and kinds[-1] == "done" and kinds.count("delta") >= 5
        deltas = [d for e, d in events if e == "delta"]
        reasoning = events[0][1]["reasoning"] + "".join(d["reasoning"] for d in deltas)
        assert reasoning == fake_lmstudio.REASONING
        overviews = [d["sections"]["overview"] for d in deltas if d.get("sections")]
        assert len(set(overviews)) >= 3 and all(b.startswith(a) for a, b in zip(overviews, overviews[1:]))  # typing
        assert any(d["phase"] == "thinking" for d in deltas) and deltas[-1]["phase"] == "writing"
        live = [d["metrics"] for d in deltas if d["metrics"].get("tok_s")]
        assert live and live[-1]["ttft_s"] is not None and live[-1]["tokens"] > live[0]["tokens"]
        done = events[-1][1]
        assert done["status"] == "done" and done["narrative"]["stomach_test"].startswith("AI capex")
        assert done["metrics"]["tok_s"] > 0 and done["metrics"]["ttft_s"] >= 0
        assert store.peek("MSFT")["ai"]["metrics"]["tokens"] == done["metrics"]["tokens"]
        # a late (re)connect replays the finished result immediately
        again = read_sse(httpd.server_address[1], "/api/ticker/MSFT/ai/stream")
        assert [e for e, _ in again] == ["snapshot", "done"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


def test_sse_first_attempt_is_not_a_reset_but_a_retry_is(settings):
    from ui.jobs import AIJob
    jm = JobManager(settings, analyzer=TickerAnalyzer(settings, backends=fakes.backends()), store=DayStore(),
                    llm=None, start=False)
    job = AIJob("MSFT", {}, jm._clock())
    jm._ai_inflight["MSFT"] = job
    gen = jm.ai_events("MSFT", coalesce=0.0)
    assert next(gen)[0] == "snapshot"  # connected while queued (attempt 0)
    job.status = "running"
    job.stream.begin(1, note="loading the model in LM Studio")
    ev, data = next(gen)
    assert ev == "delta" and data["phase"] == "connecting" and data["note"].startswith("loading")
    job.stream.delta("", "🤖: first try", {"tokens": 1})
    assert next(gen)[0] == "delta"
    job.stream.begin(2, note="first reply was unusable — retrying")
    ev, data = next(gen)
    assert ev == "reset" and data["attempt"] == 2 and "retrying" in data["note"]
    job.stream.finish({"status": "done", "ticker": "MSFT"})
    assert [e for e, _ in gen][-1] == "done"


def test_sse_404_without_job_and_bad_paths(settings, lm):
    jm, _, client = make(settings)
    httpd = PortalServer(("127.0.0.1", 0), make_handler(PortalApp(settings, jobs=jm, llm=client)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for path, code in [("/api/ticker/MSFT/ai/stream", 404), ("/api/ticker/MSFT/ai/x", 400),
                           ("/api/ticker/MSFT/stream", 400), ("/api/ticker/MSFT/ai/stream/x", 400)]:
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            c.request("GET", path)
            assert c.getresponse().status == code, path
            c.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        jm.shutdown()


def test_json_poll_exposes_partial_text_while_running(settings, lm):
    lm[1]["delay"] = 0.03
    jm, _, _ = make(settings)
    wait(lambda: jm.request("MSFT"), lambda s: s["status"] == "done")
    jm.request_ai("MSFT")
    snap = wait(lambda: jm.request_ai("MSFT"),
                lambda s: s["status"] == "running" and ((s.get("live") or {}).get("sections") or {}).get("overview"))
    assert snap["live"]["phase"] == "writing" and snap["live"]["metrics"]["ttft_s"] is not None
    assert wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "done")["complete"] is True
    jm.shutdown()


def test_ai_watchdog_cancels_the_llm_stream(settings, lm):
    lm[1]["delay"] = 0.05
    jm, _, _ = make(settings)
    jm.ai_deadline = 0.6
    wait(lambda: jm.request("MSFT"), lambda s: s["status"] == "done")
    jm.request_ai("MSFT")
    snap = wait(lambda: jm.request_ai("MSFT"), lambda s: s["status"] == "error")
    assert "timed out" in snap["error"]
    wait(lambda: lm[1].get("client_closed_at"), lambda v: v is not None, timeout=5)
    assert lm[1]["client_closed_at"] < 60  # generation stopped early instead of running to the end
    jm.shutdown()


@pytest.fixture(autouse=True)
def _reset_fakes():
    infos, rows = dict(fakes.FakeEngine.infos), dict(fakes.FakeEngine.rows)
    yield
    fakes.FakeEngine.infos, fakes.FakeEngine.rows = infos, rows
