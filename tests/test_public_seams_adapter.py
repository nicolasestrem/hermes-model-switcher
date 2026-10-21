"""Public-seam contract tests for ``hermes_adapter.py`` (0.3.0).

What is proven here:

1. ``probe_runtime()`` is CAPABILITY-based: a checkout with every required
   public seam is writable even when the version metadata is absent/placeholder,
   and a checkout missing one seam is read-only. No install stamp, no version
   comparison (the old stamp bootstrap is gone with the version gate).
2. ``discover_profiles()`` mirrors ``hermes_cli.profiles.list_profile_names()``
   at the tested revision.
3. ``HermesAdapter.scoped()`` installs the public home override + profile secret
   scope and resets BOTH: a scoped read of one profile never sees another
   profile's config or credentials.
4. The removed private seams stay removed, and the plugin's own modules raise no
   private-Hermes-import findings.
5. ``CanonicalConfigStoreAdapter.intended_write_matches`` recognises only the
   write we intended (no ``plan_canonical_save`` private pipeline).

Lab homes only (conftest mints them under /tmp — never under ~/.hermes, whose
paths resolve back to the live installation as the "default" root).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

CFG = """\
model:
  provider: openai
  default: gpt-4o-mini
unrelated:
  keep: yes
"""


# --------------------------------------------------------------- capability probe

def test_probe_runtime_accepts_the_tested_checkout():
    from hermes_adapter import probe_runtime

    runtime = probe_runtime()
    assert runtime.importable is True
    assert runtime.supported is True
    assert runtime.read_only is False
    assert runtime.reasons == ()
    # Informational only: "" when the distribution metadata is unavailable
    # (a bare checkout), a string otherwise. Never a gate.
    assert isinstance(runtime.version_label, str)


def test_probe_is_capability_based_not_version_based(monkeypatch):
    """A placeholder version must NOT make a capable runtime read-only."""
    import hermes_adapter as ha

    monkeypatch.setattr(ha, "_version_label", lambda: "0.0.0")
    assert ha.probe_runtime().supported is True

    monkeypatch.setattr(ha, "_version_label", lambda: "")
    runtime = ha.probe_runtime()
    assert runtime.supported is True and runtime.read_only is False
    assert runtime.version_label == ""


def test_probe_reports_a_missing_public_seam_as_read_only(monkeypatch):
    """One absent seam degrades the whole runtime to read-only (fail closed)."""
    import hermes_adapter as ha
    from hermes_cli import config as config_mod

    monkeypatch.delattr(config_mod, "save_config")
    runtime = ha.probe_runtime()
    assert runtime.importable is True
    assert runtime.supported is False and runtime.read_only is True
    assert any("hermes_cli.config.save_config" in reason for reason in runtime.reasons)


def test_probe_reports_an_unimportable_seam_module(monkeypatch):
    import hermes_adapter as ha

    # A poisoned sys.modules entry plus no package attribute makes
    # ``from agent import secret_scope`` raise, exactly like a Hermes release
    # that moved the module.
    agent_pkg = sys.modules.setdefault("agent", type(sys)("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.delattr(agent_pkg, "secret_scope", raising=False)
    runtime = ha.probe_runtime()
    assert runtime.importable is False
    assert runtime.supported is False and runtime.read_only is True
    assert any("agent.secret_scope" in reason for reason in runtime.reasons)


def test_read_only_runtime_refuses_writes_but_still_reads(hermes_home):
    from hermes_adapter import HermesAdapter, ReadOnlyRuntime, RuntimeInfo

    ro = HermesAdapter(runtime=RuntimeInfo(
        importable=False, version_label="", supported=False, read_only=True,
        reasons=("hermes_constants import failed: ImportError",)))
    with pytest.raises(ReadOnlyRuntime):
        ro.apply_canonical_save("default", {"model": {"default": "x"}})
    assert ro.load_raw_config("default")["model"]["provider"] == "openai"


# --------------------------------------------------------------- discovery

def test_discover_profiles_mirrors_hermes_list_profile_names(hermes_home):
    """Identity-less shells and tombstoned dirs are not profiles — exactly what
    Hermes' own cheap listing decides."""
    from hermes_cli.profiles import list_profile_names

    from hermes_adapter import discover_profiles

    hermes_home.profile("alpha")
    hermes_home.profile("beta", env={"FAKE_TOKEN": "beta-only"})
    # A marker-less shell (cron/logging side-effect) is NOT a profile.
    (hermes_home.path / "profiles" / "ghost").mkdir(parents=True)
    # Neither is a tombstoned profile dir.
    deleted = hermes_home.profile("gone")
    (hermes_home.path / "profiles" / ".deleted").mkdir(exist_ok=True)
    (hermes_home.path / "profiles" / ".deleted" / "gone").write_text("deleted\n")

    expected = list_profile_names()
    discovered = discover_profiles()

    assert list(discovered) == expected
    assert set(discovered) == {"default", "alpha", "beta"}
    assert discovered["alpha"].path == str(hermes_home.path / "profiles" / "alpha")
    assert discovered["beta"].path == str(deleted.parent / "beta")
    for identity in discovered.values():
        assert len(identity.fingerprint) == 16


def test_profile_home_resolves_only_discovered_profiles(hermes_home, adapter):
    from hermes_adapter import UnknownProfile

    hermes_home.profile("alpha")
    assert adapter.profile_home("alpha") == str(
        hermes_home.path / "profiles" / "alpha")
    assert adapter.profile_home("default") == str(hermes_home.path)
    with pytest.raises(UnknownProfile):
        adapter.profile_home("nope")


# --------------------------------------------------------------- scoping

def test_scoped_read_of_one_profile_never_sees_another(hermes_home, adapter):
    """Two lab profile homes, distinct configs and distinct fake credentials:
    a read inside profile B must never see profile A's config or secret."""
    alpha_dir = hermes_home.profile(
        "alpha", config={"_raw": "model:\n  provider: alpha-only\n  default: alpha-model\n"},
        env={"ALPHA_ONLY_TOKEN": "alpha-token"})
    beta_dir = hermes_home.profile(
        "beta", config={"_raw": "model:\n  provider: beta-only\n  default: beta-model\n"},
        env={"BETA_ONLY_TOKEN": "beta-token"})
    assert alpha_dir != beta_dir

    from agent.secret_scope import (
        current_secret_scope,
        current_secret_scope_home,
        get_secret,
    )
    from hermes_constants import get_hermes_home, get_hermes_home_override

    # Nothing scoped yet.
    assert get_hermes_home_override() is None
    assert current_secret_scope() is None

    with adapter.scoped("alpha"):
        assert Path(get_hermes_home()) == alpha_dir
        assert get_hermes_home_override() == str(alpha_dir)
        assert current_secret_scope_home() == str(alpha_dir)
        alpha_cfg = adapter.load_raw_config()
        assert alpha_cfg["model"]["provider"] == "alpha-only"
        assert get_secret("ALPHA_ONLY_TOKEN") == "alpha-token"
        # B's credential is invisible inside A's scope.
        assert get_secret("BETA_ONLY_TOKEN") is None

    with adapter.scoped("beta"):
        assert Path(get_hermes_home()) == beta_dir
        assert current_secret_scope_home() == str(beta_dir)
        beta_cfg = adapter.load_raw_config()
        assert beta_cfg["model"]["provider"] == "beta-only"
        assert beta_cfg["model"]["default"] == "beta-model"
        # Never A's document, never A's credential.
        assert beta_cfg["model"] != alpha_cfg["model"]
        assert get_secret("BETA_ONLY_TOKEN") == "beta-token"
        assert get_secret("ALPHA_ONLY_TOKEN") is None

    # Both scopes are fully unwound on exit.
    assert get_hermes_home_override() is None
    assert current_secret_scope() is None


def test_scoped_resets_home_and_secret_scope_after_an_error(hermes_home, adapter):
    from agent.secret_scope import current_secret_scope
    from hermes_constants import get_hermes_home_override

    hermes_home.profile("alpha")
    with pytest.raises(RuntimeError):
        with adapter.scoped("alpha"):
            raise RuntimeError("boom")
    assert get_hermes_home_override() is None
    assert current_secret_scope() is None


def test_scoped_none_reads_the_process_home(hermes_home, adapter):
    from agent.secret_scope import current_secret_scope
    from hermes_constants import get_hermes_home, get_hermes_home_override

    with adapter.scoped(None) as identity:
        assert identity.fingerprint == "current"
        assert Path(get_hermes_home()) == hermes_home.path
        assert current_secret_scope() is not None
        assert adapter.load_raw_config()["model"]["provider"] == "openai"
    assert get_hermes_home_override() is None
    assert current_secret_scope() is None


# --------------------------------------------------------------- removals

def test_removed_private_seams_stay_removed():
    import hermes_adapter as ha

    for name in ("catalog_payload", "pricing_probe", "prepare_main_assignment",
                 "plan_canonical_save", "plugin_backend_allowed",
                 "_load_inventory", "_load_pricing_seams",
                 "_raw_models_dev_cost"):
        assert not hasattr(ha.HermesAdapter, name), name
        assert not hasattr(ha, name), name
    for name in ("PricingProbe", "CatalogSeamUnavailable",
                 "SUPPORTED_BASELINE_VERSION", "SUPPORTED_BASELINE_LABEL"):
        assert not hasattr(ha, name), name


def test_owned_modules_have_no_private_hermes_imports():
    """The acceptance gate, scoped to the files this adapter worker owns."""
    from hermes_cli.plugin_validate_private_imports import private_import_findings

    findings = [
        hit for hit in private_import_findings(REPO)
        if not hit.startswith((".venv", "node_modules", "dist-package"))
    ]
    owned = [hit for hit in findings
             if hit.startswith("hermes_adapter.py")
             or hit.startswith("src/")]
    assert owned == [], owned


# --------------------------------------------------------------- canonical predicate

class _FakeHermesAdapter:
    """Doubles the one adapter method ``intended_write_matches`` may read."""

    def __init__(self, documents):
        self._documents = list(documents)
        self.calls = 0

    def load_raw_config(self, profile=None):
        self.calls += 1
        return self._documents.pop(0)

    def plan_canonical_save(self, profile, updated):  # must never be called
        raise AssertionError("plan_canonical_save is a private pipeline and is gone")


def test_intended_write_matches_accepts_only_our_own_change(tmp_path):
    from model_switcher.adapters import CanonicalConfigStoreAdapter

    path = tmp_path / "config.yaml"
    written = {"model": {"provider": "openai", "default": "lab-model"},
               "unrelated": {"keep": True}}
    path.write_text(yaml.safe_dump(written))
    snapshot = {"model": {"provider": "openai", "default": "gpt-4o-mini"},
                "unrelated": {"keep": True}}
    changes = {"model.default": "lab-model"}

    ours = _FakeHermesAdapter([written])
    adapter = CanonicalConfigStoreAdapter(str(path), "default", hermes_adapter=ours)
    assert adapter.intended_write_matches(snapshot, changes, "rev-after") is True

    # A foreign key that we never planned to touch makes it undecidable -> False.
    foreign = dict(written, external={"edit": True})
    adapter2 = CanonicalConfigStoreAdapter(
        str(path), "default", hermes_adapter=_FakeHermesAdapter([foreign]))
    assert adapter2.intended_write_matches(snapshot, changes, "rev-after") is False

    # Our change missing on disk -> readback already fails -> False.
    adapter3 = CanonicalConfigStoreAdapter(
        str(path), "default", hermes_adapter=_FakeHermesAdapter([written]))
    assert adapter3.intended_write_matches(
        snapshot, {"model.default": "something-else"}, "rev-after") is False
    assert ours.calls == 1  # the failed readback short-circuits the re-read
