"""Minimal LM Studio impersonator for tests and offline demos (no model required).

    python -m ui.tests.fake_lmstudio --port 18080 --delay 0.04   # then: python -m ui.server --llm-url http://127.0.0.1:18080

Implements GET /v1/models, GET /api/v0/models, POST /api/v1/models/load and POST /v1/chat/completions
(streaming SSE like LM Studio, or plain JSON when ``stream`` is false). The reply is a Lynch-style
three-paragraph overview for the first ``- TICKER`` line of the prompt, preceded by reasoning tokens
sent as ``delta.reasoning_content`` (or inline ``<think>…</think>`` with ``state["inline_think"]``).
Loopback only.

Knobs (``state`` dict): delay (s per chunk), inline_think, prefill_think (only ``</think>`` streams),
sloppy (markdown-bold labels), finish ("stop" | "length"), limit (max chunks), fail (404 "No models
loaded"), reject_reasoning_effort (400 on that field), state / loaded_ctx (native model listing),
http_errors (list of (status, body) answered to the next chat requests, one each), error_rate (share of chat
requests answered 503, like Gemini under load), streams (generations at once; the rest wait, like a local
server's batch width). The last chat request's Authorization header is kept in ``state["auth"]``.
``"reasoning_effort": "none"`` in a request suppresses the reasoning tokens, like Qwen3.6 Splash.
"""
import argparse
import json
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "lmstudio-community/qwen3-30b-a3b-GGUF"
REASONING = "Let me weigh the PEG against its history, then the income waterfall and the balance sheet."


def canned_reply(prompt, sloppy=False):
    # the daily scan's "- MSFT: …" DATASET line, or the portal's "$MSFT Microsoft…" data block
    m = re.search(r"^(?:- |\$)([A-Z][A-Z0-9.\-]*)\*?(?: \[|:| )", prompt, re.MULTILINE)
    s = m.group(1) if m else "XYZ"
    b = "**" if sloppy else ""
    return (f"{b}🤖:{b} {s} is a sleep-well compounder: PEG near its mean with an A+ waterfall. Conviction beats risk here.\n\n"
            f"{b}📊 Reverse DCF:{b} {s} sells software and cloud with a deep moat. The math: 13.5% base ROI requires EPS "
            "to compound at 13%/yr for 5 years, re-rating from 21.8x FwdPE to 24x implied PE. Realistic.\n\n"
            f"{b}🧪 Stomach Test:{b} AI capex could compress margins for years; a fortress AAA balance sheet cushions it.")


def _tokens(text):
    """Word-ish chunks (keeps whitespace) — one SSE chunk per 'token' like a real server."""
    return re.findall(r"\s*\S+", text) or [text]


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/v1/models":
                return self._json(200, {"object": "list", "data": [{"id": MODEL}, {"id": "text-embedding-nomic"}]})
            if self.path == "/api/v0/models":
                state["native_probes"] = state.get("native_probes", 0) + 1
            if self.path == "/api/v0/models" and not state.get("no_native"):  # no_native: e.g. standalone Splash
                return self._json(200, {"data": [
                    {"id": MODEL, "type": "llm", "state": state.get("state", "loaded"),
                     "max_context_length": 131072, "loaded_context_length": state.get("loaded_ctx", 65536)},
                    {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded"}]})
            return self._json(404, {"error": "not found"})

        def _chunk(self, data):
            """HTTP/1.1 chunked framing — like LM Studio, so clients see every event as it is sent."""
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()

        def _sse(self, obj):
            self._chunk(b"data: " + json.dumps(obj).encode() + b"\n\n")

        def _stream(self, req, reply):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            delay = float(state.get("delay", 0))
            base = {"id": "chatcmpl-x", "object": "chat.completion.chunk", "model": req.get("model")}
            n = 0
            try:
                if req.get("reasoning_effort") == "none":  # thinking switched off
                    pieces = []
                elif state.get("inline_think"):
                    pieces = [("content", t) for t in _tokens(f"<think>{REASONING}</think>\n\n")]
                elif state.get("prefill_think"):  # template prefilled "<think>": only the closing tag streams
                    pieces = [("content", t) for t in _tokens(f"{REASONING}</think>\n\n")]
                else:
                    pieces = [("reasoning_content", t) for t in _tokens(REASONING)]
                pieces = list(pieces) + [("content", t) for t in _tokens(reply)]
                if state.get("finish") == "length":
                    pieces = pieces[:len(pieces) // 3]
                if state.get("limit"):
                    pieces = pieces[:state["limit"]]
                for key, tok in pieces:
                    if delay:
                        time.sleep(delay)
                    self._sse(dict(base, choices=[{"index": 0, "delta": {key: tok}, "finish_reason": None}]))
                    n += 1
                    state["sent_chunks"] = n
                self._sse(dict(base, choices=[{"index": 0, "delta": {},
                                               "finish_reason": state.get("finish", "stop")}]))
                if (req.get("stream_options") or {}).get("include_usage"):
                    self._sse(dict(base, choices=[], usage={"prompt_tokens": 900, "completion_tokens": n,
                                                            "total_tokens": 900 + n}))
                self._chunk(b"data: [DONE]\n\n")
                self._chunk(b"")  # zero-length chunk ends the body
            except (BrokenPipeError, ConnectionResetError):
                state["client_closed_at"] = n  # the portal cancelled the generation

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            state.setdefault("requests", []).append((self.path, req))
            if self.path == "/api/v1/models/load":
                return self._json(200, {"status": "loaded"})
            if self.path != "/v1/chat/completions":
                return self._json(404, {"error": "not found"})
            state["auth"] = self.headers.get("Authorization")
            if state.get("http_errors"):
                code, body = state["http_errors"].pop(0)
                return self._json(code, body)
            if state.get("error_rate") and random.random() < state["error_rate"]:
                return self._json(503, {"error": {"code": 503, "status": "UNAVAILABLE",
                                                  "message": "This model is currently experiencing high demand."}})
            if state.get("fail"):
                return self._json(404, {"error": {"message": "No models loaded. Please load a model."}})
            if state.get("reject_reasoning_effort") and "reasoning_effort" in req:
                return self._json(400, {"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}})
            prompt = req["messages"][-1]["content"]
            reply = canned_reply(prompt, state.get("sloppy"))
            if req.get("stream"):
                if state.get("streams"):
                    with state.setdefault("_slots", threading.BoundedSemaphore(state["streams"])):
                        return self._stream(req, reply)
                return self._stream(req, reply)
            return self._json(200, {"id": "x", "model": req.get("model"), "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": reply, "reasoning_content": REASONING}}],
                "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": 200}})

    return H


def serve(port=0, state=None):
    state = state if state is not None else {}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, state


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--delay", type=float, default=0.04, help="seconds per streamed chunk (typing speed)")
    ap.add_argument("--inline-think", action="store_true")
    ap.add_argument("--streams", type=int, default=0, help="generations at once (0 = no limit)")
    a = ap.parse_args()
    httpd, _ = serve(a.port, {"delay": a.delay, "inline_think": a.inline_think, "streams": a.streams})
    print(f"fake LM Studio on http://127.0.0.1:{a.port}")
    threading.Event().wait()
