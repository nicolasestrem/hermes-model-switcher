"""Tests for the CI runtime-identity bootstrap (scripts/bootstrap_ci_runtime.py).

The gap this covers: a bare CI clone of the pinned Hermes revision carries no
install-stamp.json (it is installer-generated and git-ignored), so
``hermes_cli.__version__`` falls back to ``0.0.0`` and
``hermes_adapter.probe_runtime()`` refuses writes -- failing the integration
suites. The bootstrap writes the stamp the supported way (Hermes' own
``scripts/write_install_stamp.py``) with a base version derived from the
verified release tag.

Unit tests are hermetic. The end-to-end test writes a stamp into the resolved
Hermes checkout and is enabled only when ``HMS_CI_BOOTSTRAP_E2E=1`` -- the CI
checkout is a throwaway clone; the live runtime source must never be mutated.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_BOOTSTRAP_PATH = REPO / "scripts" / "bootstrap_ci_runtime.py"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("bootstrap_ci_runtime", _BOOTSTRAP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load_bootstrap()

# The runtime's own tag grammar (the single authority the bootstrap imports).
from hermes_cli import update_channel  # noqa: E402  (after conftest puts the checkout on sys.path)

CANARY_TAG = "v0.22.0+canary.20261014T070054Z"


# --- derive_base_version ----------------------------------------------------

@pytest.mark.parametrize(
    "tag, expected",
    [
        (CANARY_TAG, "0.22.0"),
        ("v0.22.0", "0.22.0"),
        ("v1.2.3", "1.2.3"),
    ],
)
def test_derive_base_version_accepts_release_tags(tag, expected):
    assert B.derive_base_version(tag, update_channel=update_channel) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "0.22.0",          # no v prefix
        "v9.9.9+foo",      # build metadata that is not a canary identity
        "v2026.7.20",      # CalVer label (4-digit major) must not pass as SemVer
        "v0.22.0+canary.20261014",  # truncated canary timestamp
        "",
    ],
)
def test_derive_base_version_rejects_non_release_tags(tag):
    with pytest.raises(ValueError):
        B.derive_base_version(tag, update_channel=update_channel)


# --- verify_identity --------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture()
def tagged_repo(tmp_path):
    """A throwaway git repo with an annotated canary tag on HEAD."""
    repo = tmp_path / "hermes-clean"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "pin")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "tag", "-a", CANARY_TAG, "-m", "canary")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_verify_identity_admits_matching_annotated_tag(tagged_repo):
    repo, sha = tagged_repo
    assert B.verify_identity(repo, sha, CANARY_TAG) == sha


def test_verify_identity_rejects_wrong_sha(tagged_repo):
    repo, _ = tagged_repo
    with pytest.raises(SystemExit):
        B.verify_identity(repo, "0" * 40, CANARY_TAG)


def test_verify_identity_rejects_tag_not_peeling_to_sha(tmp_path):
    repo = tmp_path / "hermes-clean"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "tag", "-a", CANARY_TAG, "-m", "canary")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "two")
    # HEAD advanced past the tag: the tag no longer names the pinned commit.
    with pytest.raises(SystemExit):
        B.verify_identity(repo, first, CANARY_TAG)


# --- verify_stamp -----------------------------------------------------------

def test_verify_stamp_rejects_commit_or_base_mismatch():
    good = {"commit": "a" * 40, "baseVersion": "0.22.0", "updateMechanism": "self"}
    B.verify_stamp(good, "a" * 40, "0.22.0")  # no raise
    with pytest.raises(SystemExit):
        B.verify_stamp({**good, "commit": "b" * 40}, "a" * 40, "0.22.0")
    with pytest.raises(SystemExit):
        B.verify_stamp({**good, "baseVersion": "0.0.0"}, "a" * 40, "0.22.0")
    with pytest.raises(SystemExit):
        B.verify_stamp({**good, "updateMechanism": None}, "a" * 40, "0.22.0")


# --- end to end (CI checkout only) -----------------------------------------

_E2E = os.environ.get("HMS_CI_BOOTSTRAP_E2E") == "1"
_E2E_REASON = "set HMS_CI_BOOTSTRAP_E2E=1 with a throwaway pinned checkout to run"


@pytest.mark.skipif(not _E2E, reason=_E2E_REASON)
def test_clean_checkout_becomes_supported_after_bootstrap():
    """Reproduce the 0.0.0 read-only gap, then prove the bootstrap closes it."""
    repo = Path(os.environ.get("HERMES_MODEL_SWITCHER_HERMES_REPO", ""))
    sha = os.environ.get("HERMES_TESTED_SHA", "")
    tag = os.environ.get("HERMES_TESTED_TAG", "")
    if not repo.is_dir() or not sha or not tag:
        pytest.skip("HERMES_MODEL_SWITCHER_HERMES_REPO / HERMES_TESTED_SHA / HERMES_TESTED_TAG unset")

    import hermes_adapter

    stamp = repo / "install-stamp.json"
    stamp.unlink(missing_ok=True)
    # Before: a bare checkout reports the pre-stamp placeholder and is read-only.
    before = hermes_adapter.probe_runtime()
    assert before.version_label == "0.0.0", before
    assert not before.supported and before.read_only, before

    written = B.initialize(repo, sha, tag)
    assert written["commit"] == sha
    assert written["baseVersion"] == "0.22.0"

    # After: the same checkout is supported and writable, on genuine metadata.
    after = hermes_adapter.probe_runtime()
    assert after.importable and after.supported and not after.read_only, after
    assert after.version_label == "0.22.0", after
    # The stamp on disk names the pinned commit and a real updateMechanism.
    on_disk = json.loads(stamp.read_text(encoding="utf-8-sig"))
    assert on_disk["commit"] == sha and on_disk["updateMechanism"]
