"""Phase B acceptance tests (plan T2+T3), public-seams only.

Every test runs against a throwaway isolated Hermes home; the tested Hermes
checkout is used read-only as the reference implementation. No writes outside
the lab home. Nothing here exercises a private Hermes core module: profile
scoping, config reads and the canonical save all go through
``hermes_adapter``'s public seams.
"""
from __future__ import annotations

import copy

import pytest


RAW_MAIN = """\
model:
  provider: openai
  default: gpt-4o-mini
providers:
  lablocal:
    base_url: http://127.0.0.1:8080/v1
unrelated:
  keep_me: yes
"""


def _cfg_from_yaml(path):
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh)


# ------------------------------------------------------------------ T2 tests

def test_explicit_profile_and_secret_scope(hermes_home, adapter):
    """Explicit profile scoping: config reads land in the named profile's home
    only, and the secret scope follows the profile (alpha's .env never visible
    under beta's scope; outside any scope under multiplex semantics it is
    fail-closed, never borrowed from os.environ)."""
    hermes_home.profile("alpha", env={"PROFILE_SECRET_TOKEN": "alpha-token"})
    hermes_home.profile("beta", env={"PROFILE_SECRET_TOKEN": "beta-token"})

    from agent.secret_scope import get_secret, is_multiplex_active, reset_multiplex_context, set_multiplex_context

    with adapter.scoped("alpha"):
        assert get_secret("PROFILE_SECRET_TOKEN") == "alpha-token"
    with adapter.scoped("beta"):
        assert get_secret("PROFILE_SECRET_TOKEN") == "beta-token"

    # Config reads are scoped too: alpha's config only.
    home = hermes_home.path
    (home / "profiles" / "alpha" / "config.yaml").write_text(
        "model:\n  provider: alpha-only\n  default: alpha-model\n")
    cfg = adapter.load_raw_config("alpha")
    assert cfg["model"]["provider"] == "alpha-only"

    # Unknown profile is refused outright — never degraded to the current home.
    from hermes_adapter import UnknownProfile
    with pytest.raises(UnknownProfile):
        with adapter.scoped("does-not-exist"):
            pass

    # Fail-closed outside a scope once multiplex semantics are on: the secret
    # must raise, never fall back to whatever os.environ happens to hold.
    token = set_multiplex_context(True)
    try:
        assert is_multiplex_active()
        with pytest.raises(Exception):
            get_secret("PROFILE_SECRET_TOKEN")
    finally:
        reset_multiplex_context(token)


def test_canonical_save_round_trip(hermes_home, adapter):
    """The canonical save path is Hermes' public ``config.save_config`` inside
    the profile scope: it preserves unrelated sections, writes only the named
    profile's home, and reports the revision domain the guard compares."""
    home = hermes_home.path
    (home / "config.yaml").write_text(RAW_MAIN)
    raw = adapter.load_raw_config("default")
    assert raw["providers"]["lablocal"]["base_url"] == "http://127.0.0.1:8080/v1"
    reviewed = adapter.config_revision("default")

    # Build the updated document the way the engine does: the raw document
    # plus the typed semantic changes.
    model_cfg = dict(raw.get("model") or {})
    model_cfg.update({"provider": "lablocal", "default": "lab-model"})
    updated = copy.deepcopy(raw)
    updated["model"] = model_cfg

    out = adapter.apply_canonical_save(
        "default", updated, expected_revision=reviewed)
    assert out["ok"] is True
    assert out["profile"] == "default"
    assert out["revision_before"] == reviewed
    assert out["revision_before"] != out["revision_after"]
    # The reported post-write revision IS what a fresh strict read computes.
    assert adapter.config_revision("default") == out["revision_after"]

    on_disk = _cfg_from_yaml(str(home / "config.yaml"))
    assert on_disk["model"]["default"] == "lab-model"
    assert on_disk["model"]["provider"] == "lablocal"
    # Unrelated sections survive the canonical round-trip untouched.
    assert on_disk["unrelated"]["keep_me"] is True
    assert on_disk["providers"]["lablocal"]["base_url"] == "http://127.0.0.1:8080/v1"


def test_unsupported_runtime_is_read_only(hermes_home, adapter):
    """A runtime missing a required public seam is read-only: saves are
    refused, reads still work and no private path is attempted."""
    from hermes_adapter import HermesAdapter, ReadOnlyRuntime, RuntimeInfo

    ro = HermesAdapter(runtime=RuntimeInfo(
        importable=True, version_label="", supported=False, read_only=True,
        reasons=("missing public Hermes seams: hermes_cli.config.save_config",)))
    with pytest.raises(ReadOnlyRuntime):
        ro.apply_canonical_save("default", {"model": {"default": "x"}})
    # Reads and discovery still work on a read-only runtime.
    assert ro.load_raw_config("default")["model"]["provider"] == "openai"
    assert ro.profile_home("default")


# ------------------------------------------------------------------ T3 tests

JUDGE_RAW = RAW_MAIN + """\
auxiliary:
  goal_judge:
    provider: typed-decision-lab
    model: verdict-v2
    threshold: 0.8
    enforcement: hard_block
"""

PLAIN_CHAT_EDIT = {"provider": "openai", "model": "gpt-4o-mini"}
TYPED_EDIT = {"provider": "typed-decision-lab", "model": "verdict-v3"}


def test_judge_protocol_compatibility():
    """Judge slots speak the typed decision protocol; a plain chat route is
    rejected before any write, a typed replacement previews cleanly."""
    from route_inventory import RouteEdit, preview_changes
    cfg = _inline_cfg(JUDGE_RAW)

    bad = preview_changes(cfg, [RouteEdit("judge.goal_judge.route", PLAIN_CHAT_EDIT)])
    assert not bad.clean
    assert "judge.goal_judge.route" in bad.rejected[0]
    assert bad.changes == ()  # nothing to write on a rejected edit

    good = preview_changes(cfg, [RouteEdit("judge.goal_judge.route", TYPED_EDIT)])
    assert good.clean
    assert [(c.slot_id, c.after["model"]) for c in good.changes] == [
        ("judge.goal_judge.route", "verdict-v3")]
    # Threshold/enforcement protected neighbors survive the route swap
    # (route-dict edits merge, F2 fix — they are preserved, never wiped).
    assert all(c.after.get("threshold") == 0.8 and c.after.get("enforcement") == "hard_block"
               for c in good.changes if c.storage_path == "auxiliary.goal_judge")

    # And the typed payload carrying a protected field is still refused.
    smuggled = dict(TYPED_EDIT, threshold=0.1)
    smuggle = preview_changes(cfg, [RouteEdit("judge.goal_judge.route", smuggled)])
    assert not smuggle.clean


def test_delegate_chain_inheritance():
    """Delegation fallback chains keep order, and the null-vs-[] distinction:
    null means inherit-unset and stays null in the preview; [] means explicitly
    disabled. Child rows carrying inherit_from keep their own stored values."""
    from route_inventory import RouteEdit, build_inventory, preview_changes
    cfg = _inline_cfg(RAW_MAIN + """\
delegation:
  model: child-pinned
  provider: openai
  fallback_providers:
    - provider: zai
      model: glm-5.3
    - provider: nous
      model: hermes-4
""")

    inv = {o.descriptor.slot_id: o for o in build_inventory(cfg)}
    chain = inv["delegation.fallback_chain"]
    assert chain.configured == [{"provider": "zai", "model": "glm-5.3"},
                                {"provider": "nous", "model": "hermes-4"}]
    assert chain.source == "configured"

    # Reorder + append: the preview shows the full ordered after-state.
    prev = preview_changes(cfg, [RouteEdit(
        "delegation.fallback_chain", {"model": "chain", "chain": [
            {"provider": "nous", "model": "hermes-4"},
            {"provider": "zai", "model": "glm-5.3"},
            {"provider": "local", "model": "lab-7b"}]})])
    assert prev.clean
    change = next(c for c in prev.changes if c.slot_id == "delegation.fallback_chain")
    assert change.before == [{"provider": "zai", "model": "glm-5.3"},
                             {"provider": "nous", "model": "hermes-4"}]
    assert change.after == [{"provider": "nous", "model": "hermes-4"},
                            {"provider": "zai", "model": "glm-5.3"},
                            {"provider": "local", "model": "lab-7b"}]  # order preserved exactly

    # null keeps inherit-unset semantics verbatim (never normalized to []).
    null_prev = preview_changes(cfg, [RouteEdit(
        "delegation.fallback_chain", {"model": "chain", "chain": None})])
    assert null_prev.clean
    null_change = next(c for c in null_prev.changes if c.slot_id == "delegation.fallback_chain")
    assert null_change.after is None

    # [] stays explicitly-disabled ([]), a different state from null.
    empty_prev = preview_changes(cfg, [RouteEdit(
        "delegation.fallback_chain", {"model": "chain", "chain": []})])
    assert empty_prev.clean
    empty_change = next(c for c in empty_prev.changes if c.slot_id == "delegation.fallback_chain")
    assert empty_change.after == []

    # Delegation model slot: editing it never touches the chain path.
    dm = preview_changes(cfg, [RouteEdit("delegation.model", {"provider": "nous", "model": "hermes-4"})])
    assert dm.clean
    # The companion provider write is part of the same slot edit.
    assert {c.storage_path for c in dm.changes} == {"delegation.model", "delegation.provider"}


def test_cron_inference_provider_not_scheduler():
    """A cron model change pairs cron.model with cron.model_provider (the
    inference provider) and never touches cron.provider — that's the scheduler."""
    from route_inventory import RouteEdit, preview_changes
    cfg = _inline_cfg(RAW_MAIN + """\
cron:
  provider: system
  model: old-cron-model
  model_provider: openai
""")

    prev = preview_changes(cfg, [RouteEdit(
        "cron.model", {"provider": "zai", "model": "glm-5.3"})])
    assert prev.clean
    paths = {c.storage_path: c.after for c in prev.changes}
    assert paths.get("cron.model") == "glm-5.3"
    assert paths.get("cron.model_provider") == "zai"
    # The scheduler field is invisible to the preview and would remain untouched.
    assert "cron.provider" not in paths
    assert cfg["cron"]["provider"] == "system"


def test_protected_fields_unchanged():
    """Only explicitly selected slots appear in the preview; protected fields
    (thresholds, enforcement, timeouts) can never be written, even smuggled
    inside a payload, and their values are byte-identical after preview."""
    from route_inventory import RouteEdit, preview_changes
    cfg = _inline_cfg(JUDGE_RAW + """\
moa:
  reference_temperature: 0.7
  aggregator_temperature: 0.3
  reference_timeout: 45
""")

    prev = preview_changes(cfg, [RouteEdit("main.default", {"provider": "nous", "model": "hermes-4"})])
    assert prev.clean
    # The main slot's companion (model.provider) rides along by design.
    assert [c.storage_path for c in prev.changes] == ["model.default", "model.provider"]

    # Smuggled protected fields are rejected, not silently dropped.
    smuggled = preview_changes(cfg, [RouteEdit("judge.goal_judge.route", dict(TYPED_EDIT, threshold=0.5))])
    assert not smuggled.clean
    assert any("protected" in r for r in smuggled.rejected)

    # Preview is pure: the source config is untouched.
    assert cfg["moa"]["reference_temperature"] == 0.7
    assert cfg["auxiliary"]["goal_judge"]["threshold"] == 0.8

    # Determinism: identical inputs, identical preview, twice.
    again = preview_changes(cfg, [RouteEdit("main.default", {"provider": "nous", "model": "hermes-4"})])
    assert prev == again


def test_invalid_moa_removal_rejected():
    """Removing the MoA preset that moa.default_preset still references is
    rejected wholesale — reject, don't repair (models.py:323-338 pattern)."""
    from route_inventory import RouteEdit, preview_changes
    cfg = _inline_cfg(RAW_MAIN + """\
moa:
  default_preset: gold
  presets:
    gold:
      reference_models: [{provider: nous, model: hermes-4}]
      aggregator:
        provider: zai
        model: glm-5.3
    silver:
      reference_models: [{provider: zai, model: glm-5.3}]
      aggregator:
        provider: nous
        model: hermes-4
""")

    bad = preview_changes(cfg, [RouteEdit("moa.preset.gold", {"provider": "zai", "model": ""})])
    assert not bad.clean
    assert "cannot remove preset 'gold'" in bad.rejected[0]
    assert bad.changes == ()  # the whole edit set is refused — nothing partial

    # Clearing a required aggregator is invalid even in a non-default preset.
    ok = preview_changes(cfg, [RouteEdit("moa.preset.silver", {"provider": "nous", "model": ""})])
    assert not ok.clean
    assert "required aggregator" in ok.rejected[0]
    # And replacing (not removing) the referenced default preset is fine.
    swap = preview_changes(cfg, [RouteEdit("moa.preset.gold", {"provider": "nous", "model": "hermes-4"})])
    assert swap.clean


# -- helpers -------------------------------------------------------------

def _inline_cfg(raw: str):
    import yaml
    return yaml.safe_load(raw)
