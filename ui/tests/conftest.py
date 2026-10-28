import os
import sys

import matplotlib

matplotlib.use("Agg")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """A developer's shell (LYNCH_UI_LAN=1, a custom LLM URL...) must not change test results."""
    for k in list(os.environ):
        if k.startswith(("LYNCH_UI_", "LYNCH_LLM_", "LYNCH_CACHE_LLM_", "LYNCH_GEMINI")) or k in ("FMP_API_KEY",
                                                                                                "GEMINI_API_KEY"):
            monkeypatch.delenv(k, raising=False)  # never call the real Gemini from a test
    # nor the Mac mini's switchboard: set but empty, so ui.config doesn't read it from the venvs' activate scripts
    # (the LLM defaults are then LM Studio on 127.0.0.1 and no pre-cache model; test_ai.py covers the switchboard's)
    monkeypatch.setenv("LYNCH_SWITCHBOARD", "")
    # never read the real posting tokens or call X / Threads from a test (test_socials.py uses fakes)
    monkeypatch.setenv("LYNCH_UI_SOCIALS", "0")
    # nor fetch the Shiller PE or sweep the S&P 500 on Yahoo (test_valuation.py uses fakes)
    monkeypatch.setenv("LYNCH_UI_VALUATION", "0")
    # the 6M edge is off by default; the tests exercise the full pipeline (test_analysis_edge_off covers the default)
    monkeypatch.setenv("LYNCH_UI_EDGE", "1")


@pytest.fixture(autouse=True)
def _clean_cli_allowlists(monkeypatch):
    from ui import netguard
    monkeypatch.setattr(netguard, "_CLI_NETS", [])
    monkeypatch.setattr(netguard, "_CLI_HOSTS", set())
