"""Test harness: every test runs against a THROWAWAY isolated Hermes home.

The adapter module is imported against the installed Hermes source tree
(read-only reference checkout); HERMES_HOME is monkeypatched per test so no
live profile/config is ever touched. No test writes outside the throwaway dir.

IMPORTANT (a real finding from Phase B): the throwaway home MUST live OUTSIDE
~/.hermes. pytest's default tmp_path is under the live Hermes tree
(~/.hermes/.../cache/scratch), and for any home nested under ~/.hermes,
``hermes_constants.get_default_hermes_root`` resolves the root back to the REAL
~/.hermes — so "default" and the profiles root silently point at the live
installation. The fixture therefore mints homes under /tmp and cleans them up.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))  # hermes_adapter.py / route_inventory.py live at repo root

HERMES_REPO = "/home/hermes/.hermes/hermes-agent"
if HERMES_REPO not in sys.path:
    sys.path.insert(0, HERMES_REPO)

import pytest

_LAB_HOMES: list[str] = []


def _cleanup_labs() -> None:
    for p in _LAB_HOMES:
        shutil.rmtree(p, ignore_errors=True)


atexit.register(_cleanup_labs)


@pytest.fixture()
def hermes_home(monkeypatch):
    """A throwaway Hermes home under /tmp (NEVER under ~/.hermes), with a
    helper to mint named profiles (config.yaml marker = profile identity)."""
    home = Path(tempfile.mkdtemp(prefix="hermes-model-switcher-lab-", dir="/tmp"))
    _LAB_HOMES.append(str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def make_profile(name: str, config: dict | None = None, env: dict | None = None):
        assert name != "default" or True
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
    holder = type("Home", (), {"path": home, "make_profile": staticmethod(make_profile),
                               "profile": staticmethod(make_profile)})
    return holder


@pytest.fixture(autouse=True)
def _reset_hermes_process_globals():
    """Multiplex/secret-scope state is process-global; deactivate it between
    tests. Scope tokens themselves are always paired set/reset inside
    adapter.scoped and the tests, so nothing else is restored here — planting
    an empty scope would break fail-closed semantics (get_secret must RAISE
    under multiplex with no scope, not return None)."""
    from agent import secret_scope as _ss
    yield
    _ss.set_multiplex_active(False)  # releases its own launch-home pin


@pytest.fixture()
def adapter():
    from hermes_adapter import HermesAdapter
    return HermesAdapter()
