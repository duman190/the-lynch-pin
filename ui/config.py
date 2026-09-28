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
    enrich: bool = field(default_factory=lambda: _env_bool("LYNCH_UI_ENRICH"))  # FMP multi-source growth
    cache_capacity: int = field(default_factory=lambda: _env_int("LYNCH_UI_CACHE_SIZE", 100))

    # Paths
    static_dir: str = os.path.join(UI_DIR, "static")
    cache_dir: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_CACHE_DIR", os.path.join(UI_DIR, ".cache")))

    @property
    def bind_host(self):
        return "0.0.0.0" if self.lan else self.host
