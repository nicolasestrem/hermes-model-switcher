"""Installer/packager regression tests (lab homes under pytest tmp_path only).

D1  install-local.sh preflighted only four files, so a package missing python/
    (or model_catalog.py / style.css) "installed" and then broke at import.
D3  install-local.sh created <plugin>/state with a comment claiming the
    journal lives there; state lives only at <home>/state/hermes-model-switcher
    and the backend refuses any plugin-tree path.
D4  uninstall-local.sh claimed recovery data was preserved in the plugin
    backup, which never held the journal.
D10 build_package.py must assemble every file the installer demands and must
    refuse an output inside the checkout.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GNU/Linux lab shell helpers")
ROOT = Path(__file__).resolve().parents[1]

# Every file the installer preflight requires (kept identical to REQUIRED in
# scripts/install-local.sh; test_package_files_match_installer enforces it).
PACKAGE_FILES = [
    "plugin.yaml",
    "__init__.py",
    "dashboard/manifest.json",
    "dashboard/dist/index.js",
    "dashboard/dist/style.css",
    "dashboard/plugin_api.py",
    "dashboard/model_catalog.py",
    "python/hermes_adapter.py",
    "python/route_inventory.py",
    "python/model_switcher/__init__.py",
    "python/model_switcher/engine.py",
    "python/model_switcher/journal.py",
    "python/model_switcher/adapters.py",
    "python/model_switcher/presets.py",
    "python/model_switcher/registry/__init__.py",
    "python/model_switcher/registry/scanner.py",
    "python/model_switcher/registry/descriptors.py",
]


def _fake_package(root: Path, skip: str | None = None) -> Path:
    for rel in PACKAGE_FILES:
        if rel == skip:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test package input")
    return root


def _run(script, *args, user_home):
    env = {**os.environ, "HOME": str(user_home)}
    env.pop("HERMES_MODEL_SWITCHER_LIVE_HOME", None)
    env.pop("HERMES_MODEL_SWITCHER_ALLOW_LIVE", None)
    return subprocess.run(["bash", str(ROOT / "scripts" / script), *map(str, args)],
                          env=env, capture_output=True, text=True)


def test_package_files_match_installer():
    text = (ROOT / "scripts" / "install-local.sh").read_text()
    block = text.split("REQUIRED=(", 1)[1].split(")", 1)[0]
    required = [line.strip().strip('"') for line in block.splitlines() if line.strip()]
    assert required == PACKAGE_FILES


@pytest.mark.parametrize("missing", ["python/model_switcher/engine.py",
                                     "dashboard/model_catalog.py",
                                     "dashboard/dist/style.css"])
def test_incomplete_package_is_rejected_before_install(tmp_path, missing):
    lab = tmp_path / "lab"
    lab.mkdir()
    pkg = _fake_package(tmp_path / "pkg", skip=missing)
    result = _run("install-local.sh", lab, pkg, user_home=tmp_path / "user")
    assert result.returncode == 1, result.stdout + result.stderr
    assert missing in result.stderr
    assert not (lab / "plugins" / "hermes-model-switcher").exists()


def test_install_creates_no_state_dir_in_plugin_tree_and_prints_enable(tmp_path):
    lab = tmp_path / "lab"
    lab.mkdir()
    pkg = _fake_package(tmp_path / "pkg")
    result = _run("install-local.sh", lab, pkg, user_home=tmp_path / "user")
    assert result.returncode == 0, result.stderr
    dest = lab / "plugins" / "hermes-model-switcher"
    assert dest.is_dir()
    assert not (dest / "state").exists()
    assert "hermes plugins enable hermes-model-switcher" in result.stdout


def test_uninstall_reports_the_true_state_location(tmp_path):
    lab = tmp_path / "lab"
    lab.mkdir()
    pkg = _fake_package(tmp_path / "pkg")
    assert _run("install-local.sh", lab, pkg, user_home=tmp_path / "user").returncode == 0
    result = _run("uninstall-local.sh", lab, user_home=tmp_path / "user")
    assert result.returncode == 0, result.stderr
    assert not (lab / "plugins" / "hermes-model-switcher").exists()
    assert "recovery data preserved" not in result.stdout
    assert f"{lab}/state/hermes-model-switcher" in result.stdout


def test_built_package_satisfies_installer_preflight(tmp_path):
    if not (ROOT / "dashboard" / "dist" / "index.js").is_file():
        pytest.skip("UI bundle not built")
    out = tmp_path / "out"
    built = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_package.py"),
                            "--out", str(out)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    pkg = out / "hermes-model-switcher"
    missing = [rel for rel in PACKAGE_FILES if not (pkg / rel).is_file()]
    assert missing == []


def test_build_package_refuses_output_inside_checkout(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_package.py"),
                             "--out", str(ROOT / "ui" / "pkg-out")],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not (ROOT / "ui" / "pkg-out").exists()
