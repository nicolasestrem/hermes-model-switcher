"""Exercise destructive-path guards using disposable homes, never native data."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GNU/Linux lab shell helpers")
ROOT = Path(__file__).resolve().parents[1]


def invoke(script, target, user_home, *args):
    env = {**os.environ, "HOME": str(user_home)}
    env.pop("HERMES_MODEL_SWITCHER_LIVE_HOME", None)
    env.pop("HERMES_MODEL_SWITCHER_ALLOW_LIVE", None)
    return subprocess.run(["bash", str(ROOT / "scripts" / script), str(target), *map(str, args)], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("script", ["install-local.sh", "uninstall-local.sh"])
@pytest.mark.parametrize("suffix", ["", "/profiles/lab"])
def test_native_live_home_and_descendants_refused(tmp_path, script, suffix):
    user = tmp_path / "user"
    live = user / ".hermes"
    live.mkdir(parents=True)
    result = invoke(script, str(live) + suffix, user)
    assert result.returncode == 3, result.stderr


@pytest.mark.parametrize("script", ["install-local.sh", "uninstall-local.sh"])
def test_symlink_plugins_cannot_escape_lab(tmp_path, script):
    lab = tmp_path / "lab"
    lab.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "hermes-model-switcher"
    sentinel.mkdir()
    (sentinel / "keep").write_text("preserve")
    (lab / "plugins").symlink_to(external, target_is_directory=True)
    result = invoke(script, lab, tmp_path / "user")
    assert result.returncode == 3, result.stderr
    assert (sentinel / "keep").read_text() == "preserve"


@pytest.mark.parametrize("flag_position", ["first", "middle", "last"])
def test_existing_install_requires_replace_and_preserves_backup(tmp_path, flag_position):
    lab = tmp_path / "lab"
    old = lab / "plugins" / "hermes-model-switcher"
    old.mkdir(parents=True)
    (old / "journal.sqlite").write_bytes(b"private recovery data")
    package = tmp_path / "package"
    for rel in ["dashboard/manifest.json", "dashboard/dist/index.js", "dashboard/plugin_api.py", "__init__.py"]:
        path = package / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test package input")
    result = invoke("install-local.sh", lab, tmp_path / "user", package)
    assert result.returncode == 5, result.stderr
    assert (old / "journal.sqlite").read_bytes() == b"private recovery data"
    args = {"first": ["--replace", lab, package], "middle": [lab, "--replace", package], "last": [lab, package, "--replace"]}[flag_position]
    result = invoke("install-local.sh", args[0], tmp_path / "user", *args[1:])
    assert result.returncode == 0, result.stderr
    backups = list(lab.glob(".model-switcher-backup.*/plugin/journal.sqlite"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"private recovery data"


def test_uninstall_preserves_recovery_data(tmp_path):
    lab = tmp_path / "lab"
    old = lab / "plugins" / "hermes-model-switcher"
    old.mkdir(parents=True)
    (old / "journal.sqlite").write_bytes(b"private recovery data")
    result = invoke("uninstall-local.sh", lab, tmp_path / "user")
    assert result.returncode == 0, result.stderr
    assert not old.exists()
    backups = list(lab.glob(".model-switcher-removed.*/plugin/journal.sqlite"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"private recovery data"


def test_replace_with_default_package(tmp_path):
    repo = tmp_path / "repo"
    script = repo / "scripts" / "install-local.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes((ROOT / "scripts" / "install-local.sh").read_bytes())
    package = repo / "dist-package" / "hermes-model-switcher"
    for rel in ["dashboard/manifest.json", "dashboard/dist/index.js", "dashboard/plugin_api.py", "__init__.py"]:
        path = package / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test package input")
    lab = tmp_path / "lab"
    old = lab / "plugins" / "hermes-model-switcher"
    old.mkdir(parents=True)
    (old / "journal.sqlite").write_bytes(b"retained recovery")
    result = invoke(str(script), lab, tmp_path / "user", "--replace")
    assert result.returncode == 0, result.stderr
    assert (old / "__init__.py").is_file()
    backups = list(lab.glob(".model-switcher-backup.*/plugin/journal.sqlite"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"retained recovery"
