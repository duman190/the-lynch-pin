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
        if k.startswith(("LYNCH_UI_", "LYNCH_LLM_")) or k == "FMP_API_KEY":
            monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _clean_cli_allowlists(monkeypatch):
    from ui import netguard
    monkeypatch.setattr(netguard, "_CLI_NETS", [])
    monkeypatch.setattr(netguard, "_CLI_HOSTS", set())
