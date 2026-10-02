"""Runtime settings for the Lynch Pin web UI (env vars, overridable from the CLI)."""
import os
from dataclasses import dataclass, field

UI_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(UI_DIR)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Analysis workers when the AI overview is off: each one is a process (~155 MB) analysing one ticker at
# a time. A cold lookup is about half CPU, half waiting on Yahoo / SEC, so 1.5 workers per core keep
# the cores busy (M1, 8 cores: 12 workers ≈ 117 cold tickers/min, 16 ≈ 125, 8 ≈ 82).
AUTO_WORKERS = min(16, max(2, (os.cpu_count() or 4) * 3 // 2))


def _enrich_mode(value):
    v = (value or "auto").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return "on"
    if v in ("0", "false", "no", "off"):
        return "off"
    return "auto"


@dataclass
class Settings:
    # HTTP listener. Loopback by default; --lan opts into 0.0.0.0 (private-network clients only).
    host: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("LYNCH_UI_PORT", 8765))
    lan: bool = field(default_factory=lambda: _env_bool("LYNCH_UI_LAN"))

    # Local LLM (LM Studio, OpenAI-compatible). Empty model = use the first model the server lists.
    llm_base_url: str = field(default_factory=lambda: os.environ.get("LYNCH_LLM_BASE_URL", "http://127.0.0.1:1234"))
    llm_model: str = field(default_factory=lambda: os.environ.get("LYNCH_LLM_MODEL", ""))
    llm_ctx: int = field(default_factory=lambda: _env_int("LYNCH_LLM_CTX", 65536))
    # Output budget incl. reasoning tokens of thinking models (the model stops as soon as it is done)
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LYNCH_LLM_MAX_TOKENS", 8192))
    llm_timeout: int = field(default_factory=lambda: _env_int("LYNCH_LLM_TIMEOUT", 600))
    # Thinking models: "off" sends reasoning_effort="none" (documented switch for Qwen3.6 Splash and
    # OpenAI-style servers; retried without it if the server rejects the field), "on" leaves the
    # server default.
    llm_reasoning: str = field(default_factory=lambda: os.environ.get("LYNCH_LLM_REASONING", "off").strip().lower())
    # Ask LM Studio to (JIT-)load the model with llm_ctx context before the first request.
    llm_autoload: bool = field(default_factory=lambda: _env_bool("LYNCH_LLM_AUTOLOAD"))

    # Analysis
    benchmark: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_BENCHMARK", "SPY"))
    # FMP multi-source growth: auto = on when FMP_API_KEY is set, or on / off
    enrich: str = field(default_factory=lambda: _enrich_mode(os.environ.get("LYNCH_UI_ENRICH", "auto")))
    # Tickers cached per day. An entry is ~21 KB of RAM plus ~560 KB of chart files on disk (swept at
    # midnight), so 500 costs ~10 MB RAM / ~280 MB disk. FMP is only called on a miss (free plan: 250
    # calls/day), so a bigger cache never adds FMP calls; it saves Yahoo calls (~12 per cold lookup).
    cache_capacity: int = field(default_factory=lambda: _env_int("LYNCH_UI_CACHE_SIZE", 500))
    # Tickers analysed at the same time. 0 = auto: 1 with the AI overview on (the local model is the
    # bottleneck and shares the machine), AUTO_WORKERS with it off. Above 1, each worker is a process.
    workers: int = field(default_factory=lambda: _env_int("LYNCH_UI_WORKERS", 0))

    # Paths
    static_dir: str = os.path.join(UI_DIR, "static")
    cache_dir: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_CACHE_DIR", os.path.join(UI_DIR, ".cache")))

    @property
    def enrich_enabled(self):
        return self.enrich == "on" or (self.enrich == "auto" and bool(os.environ.get("FMP_API_KEY")))

    def analysis_workers(self, with_ai):
        if self.workers > 0:
            return self.workers
        return 1 if with_ai else AUTO_WORKERS

    @property
    def bind_host(self):
        return "0.0.0.0" if self.lan else self.host
