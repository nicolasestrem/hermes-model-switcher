"""The real host routes, mounted over a throwaway lab home — offline.

What this proves (and how): the lab app in ``tests/real_app.py`` mounts Hermes'
OWN ``/api/model/*`` dashboard router (``hermes_cli/web_routers/models.py``),
unmodified, next to the plugin router. These tests drive that app with a
FastAPI ``TestClient`` — the same object uvicorn serves on :8932 for the
Playwright harness — against a freshly seeded lab home with fake credentials,
and assert:

* ``GET /api/model/options?profile=<named>&include_unconfigured=1`` returns
  provider rows for a NAMED profile (profile scoping works),
* the root profile's ``?profile=`` value is ``"default"`` (Hermes' alias for the
  launch home; see ``hermes_cli.profiles.validate_profile_name``),
* ``POST /api/model/set?profile=<named>`` with ``scope=main`` writes THAT
  profile's ``config.yaml`` and leaves the default profile's bytes alone,
* the expensive-model guard returns Hermes' ``confirm_required`` payload and
  writes nothing,
* nothing in any of it reaches the network (see ``test_lab_makes_no_outbound``).

No route logic is stubbed: the lab supplies only the one process-state symbol
the extracted routers late-bind from ``hermes_cli.web_server`` (see
``real_app.install_host_state``), and nothing else is monkeypatched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
REAL_APP_PATH = Path(__file__).resolve().parent / "real_app.py"

pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_MODEL_SWITCHER_HERMES_REPO"),
    reason="needs the Hermes source checkout (HERMES_MODEL_SWITCHER_HERMES_REPO)")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REAL_APP_PATH), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture(scope="module")
def lab():
    """Seed a lab home, import the lab app against it, yield (client, home)."""
    parent = Path(_run("--print-lab-parent").stdout.strip())
    home = Path(tempfile.mkdtemp(prefix="hms-host-routes-", dir=str(parent)))
    try:
        _run("--seed", str(home))
        previous_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        # The app module mounts routers at import, so it must be imported AFTER
        # HERMES_HOME points at the lab; drop stale copies first.
        for name in ("real_app",):
            sys.modules.pop(name, None)
        for path in (str(REPO), str(REPO / "tests")):
            if path not in sys.path:
                sys.path.insert(0, path)
        import real_app  # noqa: PLC0415 — import order is the point

        from fastapi.testclient import TestClient  # noqa: PLC0415

        client = TestClient(real_app.app)
        yield client, home, real_app
        sys.modules.pop("real_app", None)
        # install_host_state() registered a minimal hermes_cli.web_server; drop
        # it so a later test that imports the REAL module gets the real one.
        import hermes_cli  # noqa: PLC0415

        shim = sys.modules.get("hermes_cli.web_server")
        if shim is not None and getattr(shim, "__doc__", "").startswith("Lab-host state"):
            sys.modules.pop("hermes_cli.web_server", None)
            if getattr(hermes_cli, "web_server", None) is shim:
                delattr(hermes_cli, "web_server")
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _providers(client, profile: str) -> dict:
    response = client.get("/api/model/options",
                          params={"profile": profile, "include_unconfigured": 1})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload.get("providers"), list)
    return payload


def test_options_returns_provider_rows_for_named_profile(lab):
    client, _home, _app = lab
    payload = _providers(client, "research")

    rows = payload["providers"]
    assert rows, "options returned no provider rows"
    slugs = {row["slug"] for row in rows}
    # configured provider + curated ones the picker always lists
    assert {"anthropic", "openai-api", "openrouter"} <= slugs
    authenticated = {row["slug"] for row in rows if row.get("authenticated")}
    assert "openai-api" in authenticated, "research profile's openai key was not seen"
    # include_unconfigured=1 really does list unconfigured providers
    assert any(row.get("authenticated") is False for row in rows)
    for row in rows:
        assert isinstance(row.get("models"), list)


def test_root_profile_query_value_is_default(lab):
    """``?profile=default`` is the alias for the launch (root) home."""
    client, _home, _app = lab
    root = _providers(client, "default")
    named = _providers(client, "research")

    assert root["provider"] == "anthropic"          # PROFILES["default"]
    assert root["model"] == "claude-sonnet-4-6"
    assert named["provider"] == "openai"            # PROFILES["research"]
    assert named["model"] == "gpt-5.1"
    # same mounted router, two different homes
    assert root["model"] != named["model"]


def test_host_routes_mount_next_to_the_plugin_router(lab):
    client, _home, _app = lab
    plugin = client.get("/api/plugins/hermes-model-switcher/models")
    assert plugin.status_code == 200, plugin.text
    assert client.get("/healthz").json() == {"ok": True}
    for path in ("/api/model/options", "/api/model/auxiliary", "/api/model/moa"):
        assert client.get(path, params={"profile": "default"}).status_code == 200, path


def test_auxiliary_and_moa_read_the_named_profile(lab):
    client, _home, _app = lab
    aux = client.get("/api/model/auxiliary", params={"profile": "research"})
    assert aux.status_code == 200, aux.text
    tasks = {task["task"] for task in aux.json()["tasks"]}
    assert {"vision", "compression", "skills_hub"} <= tasks

    moa = client.get("/api/model/moa", params={"profile": "research"})
    assert moa.status_code == 200, moa.text
    assert "presets" in moa.json()


def test_expensive_model_requires_confirmation_and_writes_nothing(lab):
    """Hermes' cost guard answers ``confirm_required`` — offline, deterministically.

    ``openai/gpt-5.5-pro`` is the id Hermes flags from a static table
    (``hermes_cli.model_cost_guard.GPT55_PRO_OPENROUTER_ID``), so the guard fires
    without any pricing lookup reaching the network.
    """
    client, home, app = lab
    research_cfg = home / "profiles" / "research" / "config.yaml"
    before = research_cfg.read_bytes()

    response = client.post("/api/model/set", params={"profile": "research"}, json={
        "scope": "main", "provider": "openrouter", "model": "openai/gpt-5.5-pro"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["confirm_required"] is True
    assert body["scope"] == "main"
    assert body["provider"] == "openrouter"
    assert body["model"] == "openai/gpt-5.5-pro"
    assert "EXPENSIVE MODEL WARNING" in body["confirm_message"]
    assert research_cfg.read_bytes() == before, "a refused confirm must not write"

    # ...and confirming for real goes through the same host write path.
    confirmed = client.post("/api/model/set", params={"profile": "research"}, json={
        "scope": "main", "provider": "openrouter", "model": "openai/gpt-5.5-pro",
        "confirm_expensive_model": True})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["ok"] is True
    written = research_cfg.read_text()
    assert "provider: openrouter" in written
    assert "default: openai/gpt-5.5-pro" in written
    # restore the seeded bytes so test order cannot matter
    research_cfg.write_bytes(before)
    assert app.PROFILES["research"] == before.decode()


def test_set_main_scope_writes_the_named_profile_only(lab):
    client, home, app = lab
    default_cfg = home / "config.yaml"
    research_cfg = home / "profiles" / "research" / "config.yaml"
    assert default_cfg.read_text() == app.PROFILES["default"]
    assert research_cfg.read_text() == app.PROFILES["research"]

    response = client.post("/api/model/set", params={"profile": "research"}, json={
        "scope": "main", "provider": "zhipu", "model": "glm-5.3-flash"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["scope"] == "main"
    assert body["provider"] == "zhipu"
    assert body["model"] == "glm-5.3-flash"

    written = research_cfg.read_text()
    assert "provider: zhipu" in written
    assert "default: glm-5.3-flash" in written
    # the default profile was NOT the target — byte-identical to the seed
    assert default_cfg.read_text() == app.PROFILES["default"]

    # the write is visible through the same host read path
    assert _providers(client, "research")["provider"] == "zhipu"
    assert _providers(client, "default")["provider"] == "anthropic"

    research_cfg.write_text(app.PROFILES["research"])  # leave the lab as seeded


def test_lab_makes_no_outbound_network(lab):
    """The lab is offline structurally, and the cache warmers never even try.

    Three things are asserted: the guard is armed (every non-loopback connect
    raises before a byte leaves), the config/cache switches Hermes offers are in
    place, and no *cache-warming* read path attempted a fetch — the only refusals
    allowed are the account-usage daemon, which has no offline switch (its
    throttle, ``agent.account_usage_cache.REFRESH_AFTER_S``, is in-memory only).
    """
    client, home, app = lab
    offline = client.get("/lab/offline").json()

    assert offline["offline"] is True
    assert offline["outbound_connections"] == 0
    assert "socket.connect" in offline["guard"]
    assert offline["config_switches"] == {"model_catalog.enabled": False}

    browser_warmers = ("models-dev-refresh", "reasoning-caps-warm", "model-cache-swr-",
                       "model-catalog-swr", "openrouter-prewarm")
    tried = [attempt["thread"] for attempt in offline["refused_network_attempts"]]
    assert not [name for name in tried if name.startswith(browser_warmers)], tried

    # the caches the read paths consult are warm in EVERY profile home, not just
    # the launch home (a ?profile= read runs inside that profile's home)
    for profile_home in (home, home / "profiles" / "research"):
        assert (profile_home / "models_dev_cache.json").exists()
        assert json.loads((profile_home / "provider_models_cache.json").read_text())
        assert json.loads((profile_home / "cache" / "reasoning_caps.json").read_text())

    # and the copy generated by the app is the one this home is resolving
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(str(home / "profiles" / "research"))
    try:
        from hermes_cli.models import _credential_fingerprint

        rows = json.loads((home / "profiles" / "research" / "provider_models_cache.json").read_text())
        assert rows["anthropic"]["fp"] == _credential_fingerprint("anthropic")
    finally:
        reset_hermes_home_override(token)

    # the guard is real: a direct outbound connect from this process is refused
    import socket

    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
