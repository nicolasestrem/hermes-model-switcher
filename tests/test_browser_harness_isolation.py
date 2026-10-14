"""The browser harness must prove profile discovery isolation before serving."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent


def boot(home: Path):
    env = dict(os.environ, HERMES_HOME=str(home))
    env.pop("PYTHONPATH", None)
    code = "import sys; sys.path.insert(0, 'tests'); import real_app; print('isolated app booted')"
    return subprocess.run([sys.executable, "-c", code], cwd=REPO,
                          env=env, text=True, capture_output=True)


def test_real_harness_refuses_discovery_outside_declared_lab(tmp_path):
    # TMPDIR is inside the real profile tree. Hermes resolves its DEFAULT root
    # back to the machine home, so merely setting HERMES_HOME is insufficient.
    (tmp_path / "config.yaml").write_text("{}\n")
    result = boot(tmp_path)
    assert result.returncode != 0
    assert "lab profile discovery escaped declared home" in result.stderr


def test_real_harness_boots_with_actual_isolated_discovery(hermes_home):
    hermes_home.make_profile("research")
    result = boot(hermes_home.path)
    assert result.returncode == 0, result.stderr
    assert "isolated app booted" in result.stdout
