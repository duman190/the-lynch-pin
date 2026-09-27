"""Minimal LM Studio impersonator for tests and offline demos (no model required).

    python -m ui.tests.fake_lmstudio --port 18080     # then: python -m ui.server --llm-url http://127.0.0.1:18080

Implements GET /v1/models, GET /api/v0/models, POST /v1/chat/completions (non-streaming) and
POST /api/v1/models/load. The completion echoes a Lynch-style narrative for every ``- TICKER``
line found in the prompt, wrapped in a <think> block like a reasoning model would emit.
Loopback only.
"""
import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "lmstudio-community/qwen3-30b-a3b-GGUF"


def canned_reply(prompt, sloppy=False):
    syms = re.findall(r"^- ([A-Z][A-Z0-9.\-]*)\*?(?: \[|:)", prompt, re.MULTILINE)
    out = ["<think>Let me weigh PEG against growth...</think>",
           "SENTIMENT: Mega-cap tech keeps leading; investors pay up for durable AI earnings.", ""]
    for s in syms:
        header = f"TICKER: {s}" if sloppy else f"${s}:"
        out += [header,
                f"🤖: {s} is a sleep-well compounder: PEG near its mean with an A+ waterfall. Conviction beats risk here.",
                "",
                f"📊 Reverse DCF: {s} sells software and cloud with a deep moat. The math: 13.5% base ROI requires EPS "
                "to compound at 13%/yr for 5 years, re-rating from 21.8x FwdPE to 24x implied PE. Realistic.",
                "",
                "🧪 Stomach Test: AI capex could compress margins for years; a fortress AAA balance sheet cushions it.",
                ""]
    return "\n".join(out)


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
                return self._json(200, {"data": [
                    {"id": MODEL, "type": "llm", "state": state.get("state", "loaded"),
                     "max_context_length": 131072, "loaded_context_length": state.get("loaded_ctx", 65536)},
                    {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded"}]})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            state.setdefault("requests", []).append((self.path, req))
            if self.path == "/api/v1/models/load":
                return self._json(200, {"status": "loaded"})
            if self.path != "/v1/chat/completions":
                return self._json(404, {"error": "not found"})
            if state.get("fail"):
                return self._json(404, {"error": {"message": "No models loaded. Please load a model."}})
            prompt = req["messages"][-1]["content"]
            return self._json(200, {"id": "x", "model": req.get("model"), "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": canned_reply(prompt, state.get("sloppy")),
                            "reasoning_content": "hidden"}}],
                "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": 200}})

    return H


def serve(port=0, state=None):
    state = state if state is not None else {}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, state


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    a = ap.parse_args()
    httpd, _ = serve(a.port)
    print(f"fake LM Studio on http://127.0.0.1:{a.port}")
    threading.Event().wait()
