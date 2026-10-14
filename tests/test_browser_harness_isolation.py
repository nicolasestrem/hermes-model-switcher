"""The browser harness must prove profile discovery isolation before serving.

Two boundaries are exercised here, both against the REAL Hermes runtime
resolver (never a discovery shim):

* the import guard that runs before the FastAPI/plugin_api mount, and
* the ``--seed`` fail-closed write boundary that must run before the first
  write, evaluated against the *requested* target rather than the inherited
  ``HERMES_HOME`` (on a live host that points at the live profile, so trusting
  it would let a seed nested under ``~/.hermes`` reach the live home).

Every subprocess redirects ``HOME`` so Hermes' platform-default root
(``~/.hermes``) is a throwaway sandbox: a defective write can never reach the
real live profile from these tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

REPO = Path(__file__).resolve().parent.parent

# Read-only reference Hermes checkout used by real_app.py (same override the
# conftest and README document). Redirecting HOME in a subprocess moves the
# default `~/.hermes` into the sandbox, so the source path is passed explicitly.
HERMES_REPO = str(Path(os.environ.get(
    "HERMES_MODEL_SWITCHER_HERMES_REPO", Path.home() / ".hermes" / "hermes-agent")))

# Emitted by real_app.assert_seed_target_isolated on refusal; kept stable.
SEED_ISOLATION_MESSAGE = "seed target is not an isolated Hermes home"


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


# -- CLI seeding boundary ----------------------------------------------------

def _sandbox_home(tmp_path: Path) -> Path:
    """A throwaway *native* HOME: Hermes' default root becomes <home>/.hermes.

    Redirecting HOME (not HERMES_HOME) is deliberate — it moves the resolver's
    platform default into the sandbox, so the "nested under the live root" case
    is reproduced without a test ever naming the real live profile.
    """
    native_home = tmp_path / "sandbox-home"
    native_home.mkdir()
    return native_home


def run_main(*args: str, home: Path, home_file: Path | None = None,
             extra_env: dict | None = None, guard_app: bool = False):
    """Run tests/real_app.py as ``__main__`` in a subprocess.

    ``guard_app`` makes a bypassed FastAPI/dashboard import observable by
    turning any attempt to import them into a hard error.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("HERMES_HOME", None)  # never inherit a live profile home
    env["HOME"] = str(home)
    env["HERMES_MODEL_SWITCHER_HERMES_REPO"] = HERMES_REPO
    if extra_env:
        env.update(extra_env)
    argv = list(args)
    if home_file is not None:
        argv += ["--home-file", str(home_file)]
    if guard_app:
        code = textwrap.dedent(
            """
            import builtins, runpy, sys
            _real_import = builtins.__import__
            def _guard(name, *a, **k):
                if name.split(".")[0] in {"fastapi", "dashboard"}:
                    raise RuntimeError("app import attempted: " + name)
                return _real_import(name, *a, **k)
            builtins.__import__ = _guard
            runpy.run_path("tests/real_app.py", run_name="__main__")
            """
        )
        cmd = [sys.executable, "-c", code, *argv]
    else:
        cmd = [sys.executable, "tests/real_app.py", *argv]
    return subprocess.run(cmd, cwd=REPO, env=env, text=True, capture_output=True)


def test_cli_seed_refuses_target_nested_under_native_home_before_writing(tmp_path):
    # RED first: the requested TARGET is unsafe (nested under ~/.hermes), while
    # the inherited HERMES_HOME points at a genuinely isolated lab. A target
    # check must ignore the inherited home and refuse — and must refuse BEFORE
    # creating anything.
    home = _sandbox_home(tmp_path)
    native = home / ".hermes"                 # the sandbox's live-root surrogate
    target = native / "scratch-lab"
    decoy = tmp_path / "decoy-lab"
    decoy.mkdir()

    result = run_main("--seed", str(target), home=home,
                      extra_env={"HERMES_HOME": str(decoy)})

    assert result.returncode != 0, result.stdout
    assert SEED_ISOLATION_MESSAGE in result.stderr
    assert not target.exists()
    assert not (native / "config.yaml").exists()


def test_cli_seed_refuses_the_native_home_itself_before_writing(tmp_path):
    # Seeding the native root IS seeding the live profile store.
    home = _sandbox_home(tmp_path)
    native = home / ".hermes"

    result = run_main("--seed", str(native), home=home)

    assert result.returncode != 0, result.stdout
    assert SEED_ISOLATION_MESSAGE in result.stderr
    assert not native.exists()


def test_cli_seed_writes_only_a_provably_isolated_target(tmp_path):
    home = _sandbox_home(tmp_path)
    target = tmp_path / "isolated-lab"       # outside the native root
    home_file = tmp_path / "home.json"

    result = run_main("--seed", str(target), home=home, home_file=home_file)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target)
    assert (target / "config.yaml").is_file()
    assert (target / ".env").is_file()
    assert (target / "profiles" / "research" / "config.yaml").is_file()
    # nothing escaped to the native root
    assert not (home / ".hermes").exists()
    assert json.loads(home_file.read_text())["home"] == str(target)


def test_cli_print_profiles_bypasses_app_imports_and_writes_nothing(tmp_path):
    # --print-profiles is a pure data dump: it must not construct the router
    # (no FastAPI / plugin_api import), must not need a lab home, and must not
    # write anything even when the inherited HERMES_HOME is unsafe.
    home = _sandbox_home(tmp_path)
    unsafe = home / ".hermes" / "nested"

    result = run_main("--print-profiles", home=home, guard_app=True,
                      extra_env={"HERMES_HOME": str(unsafe)})

    assert result.returncode == 0, result.stderr
    assert "app import attempted" not in result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"default", "research"}
    assert "claude-sonnet-4-6" in payload["default"]
    # non-mutating: the sandbox home gained nothing
    assert not unsafe.exists()
    assert not list(home.rglob("config.yaml"))


@pytest.mark.parametrize('filename', ['config.yaml', '.env'])
def test_cli_seed_preserves_existing_custom_home_data(tmp_path, filename):
    home = _sandbox_home(tmp_path)
    target = tmp_path / 'existing-custom-home'
    target.mkdir()
    existing = target / filename
    existing.write_bytes(b'user-owned-content\n')
    pointer = tmp_path / 'home.json'
    # A custom runtime home outside the native tree is not necessarily a lab.
    result = run_main('--seed', str(target), home=home, home_file=pointer,
                      extra_env={'HERMES_HOME': str(target)})
    assert result.returncode != 0, result.stdout
    assert SEED_ISOLATION_MESSAGE in result.stderr
    assert existing.read_bytes() == b'user-owned-content\n'
    assert sorted(p.name for p in target.iterdir()) == [filename]
    assert not pointer.exists()
