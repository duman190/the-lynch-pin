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


# Analysis workers when the AI overview is off: each one is a process analysing one ticker at a time.
# A cold lookup is about half CPU, half waiting on Yahoo / SEC, so 1.5 workers per core keep the cores
# busy (M1, 8 cores: 12 workers ≈ 117 cold tickers/min, 16 ≈ 125, 8 ≈ 82)...
CPU_WORKERS = min(16, max(2, (os.cpu_count() or 4) * 3 // 2))
# ...as long as they fit in RAM: a worker is ~155 MB idle, ~200 MB mid-analysis. Once they swap, every
# stage slows down several times (a model loaded in LM Studio can hold most of an 8 GB Mac).
WORKER_MB = 200


def available_mb():
    """RAM available without swapping, in MB (None when psutil is missing)."""
    try:
        import psutil
    except ImportError:
        return None
    return psutil.virtual_memory().available // (1024 * 1024)


def auto_workers(free_mb=None):
    """Workers for --no-ai: CPU_WORKERS, fewer when free RAM can't hold them (never below 2)."""
    free_mb = available_mb() if free_mb is None else free_mb
    if free_mb is None:
        return CPU_WORKERS
    return max(2, min(CPU_WORKERS, int(free_mb // WORKER_MB)))


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
    # Served to the internet through a tunnel on this machine (e.g. cloudflared → 127.0.0.1): any Host
    # header, the visitor's IP from CF-Connecting-IP, one analysis at a time per visitor, less metadata.
    public: bool = field(default_factory=lambda: _env_bool("LYNCH_UI_PUBLIC"))
    # -v: the page shows the local model's name, its token counts and thinking setting, and the cache
    # chip. Off by default: the header shows only "AI" with a green/red dot.
    verbose: bool = field(default_factory=lambda: _env_bool("LYNCH_UI_VERBOSE"))
    # Stats page (ui/stats.py): a second server with --lan or --public, on its own port, for LAN / Tailscale
    # clients only. It keeps a rolling window of stats_days days in cache_dir/stats.sqlite3, and
    # stats_visitor_days of DAU / MAU.
    stats: bool = field(default_factory=lambda: _env_bool("LYNCH_UI_STATS", True))
    stats_port: int = field(default_factory=lambda: _env_int("LYNCH_UI_STATS_PORT", 190))
    stats_days: int = field(default_factory=lambda: _env_int("LYNCH_UI_STATS_DAYS", 30))
    stats_visitor_days: int = field(default_factory=lambda: _env_int("LYNCH_UI_STATS_VISITOR_DAYS", 365))

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
    # AI overviews streamed at once (one AI worker each). LM Studio batches parallel requests: more streams
    # raise total tokens/s but slow each stream down (see "Local AI tuning" in ui/README.md).
    llm_parallel: int = field(default_factory=lambda: _env_int("LYNCH_LLM_PARALLEL", 1))
    # Ask LM Studio to (JIT-)load the model with llm_ctx context before the first request.
    llm_autoload: bool = field(default_factory=lambda: _env_bool("LYNCH_LLM_AUTOLOAD"))

    # Analysis
    benchmark: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_BENCHMARK", "SPY"))
    # FMP multi-source growth: auto = on when FMP_API_KEY is set, or on / off
    enrich: str = field(default_factory=lambda: _enrich_mode(os.environ.get("LYNCH_UI_ENRICH", "auto")))
    # FMP requests the portal may make in any rolling 24 h (free plan: 250 a day; the daily scans need up to 25)
    fmp_limit: int = field(default_factory=lambda: _env_int("LYNCH_UI_FMP_LIMIT", 225))
    # Tickers cached per day. An entry is ~21 KB of RAM plus ~560 KB of chart files on disk (swept at
    # midnight), so 500 costs ~10 MB RAM / ~280 MB disk. FMP is only called on a miss (free plan: 250
    # calls/day), so a bigger cache never adds FMP calls; it saves Yahoo calls (~12 per cold lookup).
    cache_capacity: int = field(default_factory=lambda: _env_int("LYNCH_UI_CACHE_SIZE", 500))
    # Tickers analysed at the same time. 0 = auto: 1 with the AI overview on (the local model is the
    # bottleneck and shares the machine), auto_workers() with it off. Above 1, each worker is a process.
    workers: int = field(default_factory=lambda: _env_int("LYNCH_UI_WORKERS", 0))

    # Paths
    static_dir: str = os.path.join(UI_DIR, "static")
    cache_dir: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_CACHE_DIR", os.path.join(UI_DIR, ".cache")))
    # "Latest scans": the threads main.py archives per scan kind (scans/<kind>/scan.json), newest first
    scans_dir: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_SCANS_DIR", os.path.join(REPO_ROOT, "scans")))
    scans_limit: int = field(default_factory=lambda: _env_int("LYNCH_UI_SCANS", 7))
    # Socials: profile links + the latest X posts, read with the posting tokens (from this file, else the environment)
    socials: bool = field(default_factory=lambda: _env_bool("LYNCH_UI_SOCIALS", True))
    # Daily "Latest on X" read: 9 AM Pacific, before the 1 PM scan posts
    social_read_at: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_SOCIAL_READ_AT", "09:00"))
    social_tz: str = field(default_factory=lambda: os.environ.get("LYNCH_UI_SOCIAL_TZ", "America/Los_Angeles"))
    social_env_file: str = field(default_factory=lambda: os.environ.get(
        "LYNCH_UI_SOCIAL_ENV", os.path.join(REPO_ROOT, "venv", "bin", "activate")))

    @property
    def enrich_enabled(self):
        return self.enrich == "on" or (self.enrich == "auto" and bool(os.environ.get("FMP_API_KEY")))

    def analysis_workers(self, with_ai):
        if self.workers > 0:
            return self.workers
        return 1 if with_ai else auto_workers()

    @property
    def bind_host(self):
        return "0.0.0.0" if self.lan else self.host

    @property
    def stats_enabled(self):
        return (self.lan or self.public) and self.stats

    @property
    def stats_bind_host(self):
        """All interfaces with --lan or --public: with --public the portal stays on loopback for the tunnel,
        while the stats page answers LAN / Tailscale clients only (ui/server.py: make_stats_handler)."""
        return "0.0.0.0" if self.lan or self.public else self.host

    @property
    def stats_path(self):
        return os.path.join(self.cache_dir, "stats.sqlite3")

    @property
    def fmp_budget_path(self):
        """FMP requests made for enrichment in the last 24 h, shared by the analysis processes (ui/fmp_budget.py)."""
        return os.path.join(self.cache_dir, "fmp_budget.sqlite3")
