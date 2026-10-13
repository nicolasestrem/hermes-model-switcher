"""T7 — real, isolated dashboard-plugin load (official extension contract).

Builds the full plugin package (scripts/build_package.py), installs it into a
THROWAWAY Hermes home under /tmp with scripts/install-local.sh (exact layout:
<home>/plugins/hermes-model-switcher/{plugin.yaml,python/,dashboard/{manifest.json,
dist/index.js,plugin_api.py}}), then uses Hermes' OWN dashboard discovery seam
(hermes_cli.web_server_dashboard._discover_dashboard_plugins) to prove:

  - the manifest is discovered from the isolated home,
  - the manifest's api field passes the dashboard's safe-relpath validation,
  - the INSTALLED backend module imports and serves /state through FastAPI,
  - install → uninstall round-trips cleanly (never touching the live home).

No inference is ever performed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VENV = Path(sys.executable)


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    """Assemble the releasable package once (requires a prior npm build)."""
    out = tmp_path_factory.mktemp("pkg")
    subprocess.run([str(VENV), str(REPO / "scripts" / "build_package.py"),
                    "--out", str(out)], cwd=REPO, check=True,
                   capture_output=True, text=True)
    pkg = out / "hermes-model-switcher"
    # package completeness: manifest, IIFE bundle, backend, python modules
    for rel in ("plugin.yaml", "__init__.py", "LICENSE", "README.md",
                "dashboard/manifest.json", "dashboard/dist/index.js",
                "dashboard/plugin_api.py",
                "python/hermes_adapter.py", "python/route_inventory.py",
                "python/model_switcher/engine.py"):
        assert (pkg / rel).is_file(), rel
    return pkg


@pytest.fixture()
def installed_plugin(hermes_home, package):
    home = hermes_home.path
    (home / "config.yaml").write_text(
        "model:\n  provider: openai\n  default: gpt-4o-mini\n")
    # LOCAL install script into the throwaway home (never the live home)
    subprocess.run(["bash", str(REPO / "scripts" / "install-local.sh"),
                    str(home), str(package)], check=True,
                   capture_output=True, text=True)
    target = home / "plugins" / "hermes-model-switcher" / "dashboard"
    return home, target


def test_official_layout_discovered_by_real_seam(installed_plugin):
    home, target = installed_plugin
    assert (target / "manifest.json").exists()
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["name"] == "hermes-model-switcher"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["api"] == "plugin_api.py"

    sys.path.insert(0, str(REPO))
    from hermes_cli.web_server_dashboard import (
        _discover_dashboard_plugins,
        _safe_plugin_api_relpath,
    )
    plugins = _discover_dashboard_plugins()
    mine = next((p for p in plugins if p.get("name") == "hermes-model-switcher"), None)
    assert mine is not None, [p.get("name") for p in plugins]
    assert mine["entry"].endswith("index.js")

    # The manifest's api field must pass the dashboard's safe-relpath guard
    # (arbitrary Python import = RCE primitive; traversal must be refused).
    assert _safe_plugin_api_relpath("plugin_api.py", dashboard_dir=target) == "plugin_api.py"
    assert _safe_plugin_api_relpath("../plugin_api.py", dashboard_dir=target) is None


def test_installed_backend_module_serves_state(installed_plugin, monkeypatch):
    home, target = installed_plugin
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "installed_plugin_api", target / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app)
    r = client.get("/state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "default" in {p["id"] for p in body["profiles"]}
    blob = json.dumps(body)
    assert '"providers"' not in blob and '"base_url"' not in blob  # sanitized DTO


def test_checkout_layout_loads_the_backend(tmp_path, monkeypatch):
    """The documented clone/plain-copy install must load its backend too.

    A checkout is a src/ layout — ``model_switcher`` lives under ``src/``, and
    the backend resolves its own modules through its import bootstrap. Copy out
    exactly what ``git clone`` produces (``git ls-files``: no ``python/``, no
    ``node_modules``) and import the backend the way the Dashboard does. This
    gap survived the suite because conftest.py puts ``src/`` on sys.path itself,
    masking a bootstrap that only handled the packaged layout.
    """
    checkout = tmp_path / "checkout"
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
    for rel in (p for p in listing.split("\0") if p):
        dst = checkout / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)
    assert not (checkout / "python").exists(), "a checkout is not the package"
    assert (checkout / "src" / "model_switcher").is_dir()
    assert (checkout / "dashboard" / "dist" / "index.js").is_file()

    # Import in isolation: the bootstrap has to work on its own, not because a
    # previous test (or conftest) already put src/ and the repo root on sys.path.
    monkeypatch.setattr(sys, "path", [p for p in sys.path
                                      if p not in (str(REPO / "src"), str(REPO))])
    for name in [n for n in sys.modules
                 if n.split(".")[0] in ("model_switcher", "hermes_adapter",
                                        "route_inventory")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "lab-home"))

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "checkout_plugin_api", checkout / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert getattr(mod, "router", None) is not None
    assert mod.__file__.startswith(str(checkout))


def test_install_uninstall_roundtrip(hermes_home, package):
    home = hermes_home.path
    subprocess.run(["bash", str(REPO / "scripts" / "install-local.sh"),
                    str(home), str(package)], check=True,
                   capture_output=True, text=True)
    dest = home / "plugins" / "hermes-model-switcher"
    assert dest.is_dir()
    r = subprocess.run(["bash", str(REPO / "scripts" / "uninstall-local.sh"),
                        str(home)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not dest.exists()
    # uninstalling must NOT silently revert applied model choices: the home's
    # config.yaml (applied state) is untouched by removal.
    assert (home / "config.yaml").exists()


def test_install_script_refuses_live_home(package, tmp_path):
    """The installer must refuse the declared live home — asserted hermetically.

    HERMES_MODEL_SWITCHER_LIVE_HOME names the home to refuse, so this test does
    not depend on the operator's real ~/.hermes. Keying it off ``Path.home()``
    meant that on a workstation where the plugin IS installed live the test
    asserted the operator's own install out of existence.
    """
    live = tmp_path / "live-home"
    (live / "plugins").mkdir(parents=True)
    r = subprocess.run(
        ["bash", str(REPO / "scripts" / "install-local.sh"), str(live), str(package)],
        env={"PATH": "/usr/bin:/bin", "HERMES_HOME": str(live),
             "HERMES_MODEL_SWITCHER_LIVE_HOME": str(live),
             "HERMES_MODEL_SWITCHER_ALLOW_LIVE": "0"},
        capture_output=True, text=True)
    # Refusal = exit 3 (live home detected) or 4 (live home undeterminable —
    # fail-closed). Either way nothing is installed.
    assert r.returncode in (3, 4), (r.returncode, r.stdout, r.stderr)
    assert not (live / "plugins" / "hermes-model-switcher").exists()
