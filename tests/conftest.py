"""Test harness: every test runs against a THROWAWAY isolated Hermes home.

The adapter module is imported against the installed Hermes source tree
(read-only reference checkout); HERMES_HOME is monkeypatched per test so no
live profile/config is ever touched. No test writes outside tmp_path.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))  # hermes_adapter.py / route_inventory.py live at repo root

HERMES_REPO = "/home/hermes/.hermes/hermes-agent"
if HERMES_REPO not in sys.path:
    sys.path.insert(0, HERMES_REPO)


import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """A throwaway Hermes home: <tmp>/hermes-lab as the default home, with a
    helper to mint named profiles (config.yaml marker = profile identity)."""
    home = tmp_path / "hermes-lab"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def make_profile(name: str, config: dict | None = None, env: dict | None = None):
        pdir = home / "profiles" / name
        pdir.mkdir(parents=True)
        raw = (config or {}).get("_raw") or "model:\n  provider: openai\n  default: gpt-4o-mini\n"
        (pdir / "config.yaml").write_text(raw)
        lines = [f"{k}={v}" for k, v in (env or {}).items()]
        if lines:
            (pdir / ".env").write_text("\n".join(lines) + "\n")
        return pdir

    # default home needs at least a config for strict reads
    (home / "config.yaml").write_text("model:\n  provider: openai\n  default: gpt-4o-mini\n")
    holder = type("Home", (), {"path": home, "make_profile": staticmethod(make_profile)})
    return holder


@pytest.fixture()
def adapter():
    from hermes_adapter import HermesAdapter
    return HermesAdapter()
