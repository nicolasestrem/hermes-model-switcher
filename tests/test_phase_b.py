"""Phase B acceptance tests (PLAN T2+T3). Every test runs against a throwaway
isolated Hermes home created in tmp_path; the live Hermes checkout is used
read-only as the reference implementation. No writes outside tmp_path."""
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

def test_plugin_auth_and_enablement(hermes_home, adapter):
    """Adapter mirrors web_server.py:650-688's gate: disabled plugins and
    never-enabled user plugins are refused; empty/unknown names fail closed."""
    home = hermes_home.path
    (home / "config.yaml").write_text(
        RAW_MAIN + "plugins:\n  enabled:\n    - alpha\n  disabled:\n    - beta\n")
    # Plugin enablement is read from the (default home) config in scope.
    assert adapter.plugin_backend_allowed("alpha") is True      # explicitly enabled
    assert adapter.plugin_backend_allowed("beta") is False      # explicitly disabled
    assert adapter.plugin_backend_allowed("never-seen") is False  # unknown => user => blocked
    assert adapter.plugin_backend_allowed("") is False          # no name, no access


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


def test_canonical_route_protocol(hermes_home, adapter):
    """Main-slot assignment goes through Hermes' canonical preparation seam
    (web_server_config._prepare_main_assignment) and the canonical save path
    (config.save_config), preserving unrelated sections and writing only the
    named profile's home."""
    pdir = hermes_home.path
    (pdir / "config.yaml").write_text(RAW_MAIN)
    raw = adapter.load_raw_config("default")
    assert raw["providers"]["lablocal"]["base_url"] == "http://127.0.0.1:8080/v1"

    prepared = adapter.prepare_main_assignment(
        raw, provider="custom", model="lab-model",
        base_url="http://127.0.0.1:8080/v1", api_key="sk-lab")
    result = prepared["result"]
    assert result.success, getattr(result, "error_message", None)
    # switch_model resolves a bare custom endpoint onto the known provider row
    # (custom:lablocal) — the canonical aliasing, not a new anonymous provider.
    assert result.target_provider.lower().startswith("custom")
    assert result.new_model == "lab-model"
    assert prepared["base_url"] == "http://127.0.0.1:8080/v1"

    # Canonical save: build the updated document the way _apply_main_assignment_sync
    # does, persist via save_config, then read back and confirm isolation.
    from hermes_cli.model_switch import apply_model_selection
    model_cfg = dict(raw.get("model") or {})
    model_cfg = apply_model_selection(model_cfg, result)
    model_cfg["base_url"] = prepared["base_url"]
    model_cfg["api_key"] = "sk-lab"
    updated = copy.deepcopy(raw)
    updated["model"] = model_cfg
    out = adapter.apply_canonical_save("default", updated)
    assert out["ok"] is True
    assert out["revision_before"] != out["revision_after"]

    on_disk = _cfg_from_yaml(str(pdir / "config.yaml"))
    assert on_disk["model"]["default"] == "lab-model"
    assert on_disk["model"]["api_key"] == "sk-lab"
    # Unrelated sections survive the canonical round-trip untouched.
    assert on_disk["unrelated"]["keep_me"] is True
    assert on_disk["providers"]["lablocal"]["base_url"] == "http://127.0.0.1:8080/v1"


def test_unsupported_runtime_is_read_only(hermes_home, adapter):
    """A runtime below the tested baseline (or an unimportable Hermes) is
    read-only: preparation and saves are refused, reads and previews still work."""
    from hermes_adapter import ReadOnlyRuntime, RuntimeInfo, HermesAdapter

    ro = HermesAdapter(runtime=RuntimeInfo(
        importable=False, version_label="v0.19.0", supported=False,
        read_only=True, reasons=("hermes import failed: simulated",)))
    # The default throwaway home already carries a minimal config.yaml.
    with pytest.raises(ReadOnlyRuntime):
        ro.prepare_main_assignment({}, "openai", "gpt-4o-mini")
    with pytest.raises(ReadOnlyRuntime):
        ro.apply_canonical_save("default", {"model": {"default": "x"}})
    with pytest.raises(ReadOnlyRuntime):
        ro.plan_canonical_save("default", {"model": {"default": "x"}})
    # Reads still work.
    assert ro.load_raw_config("default")["model"]["provider"] == "openai"


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
    # Threshold/enforcement are untouched even though they share the slot dict.
    assert all("threshold" not in c.after and "enforcement" not in c.after
               for c in good.changes)

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
    - zai
    - nous
""")

    inv = {o.descriptor.slot_id: o for o in build_inventory(cfg)}
    chain = inv["delegation.fallback_chain"]
    assert chain.configured == ["zai", "nous"]
    assert chain.source == "configured"

    # Reorder + append: the preview shows the full ordered after-state.
    prev = preview_changes(cfg, [RouteEdit(
        "delegation.fallback_chain", {"model": "chain", "chain": ["nous", "zai", "local/x"]})])
    assert prev.clean
    change = next(c for c in prev.changes if c.slot_id == "delegation.fallback_chain")
    assert change.before == ["zai", "nous"]
    assert change.after == ["nous", "zai", "local/x"]  # order preserved exactly

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
      provider: zai
      model: glm-5.3
    silver:
      provider: nous
      model: hermes-4
""")

    bad = preview_changes(cfg, [RouteEdit("moa.preset.gold", {"provider": "zai", "model": ""})])
    assert not bad.clean
    assert "cannot remove preset 'gold'" in bad.rejected[0]
    assert bad.changes == ()  # the whole edit set is refused — nothing partial

    # A preset NOT referenced by the default is removable.
    ok = preview_changes(cfg, [RouteEdit("moa.preset.silver", {"provider": "nous", "model": ""})])
    assert ok.clean
    # And replacing (not removing) the referenced default preset is fine.
    swap = preview_changes(cfg, [RouteEdit("moa.preset.gold", {"provider": "nous", "model": "hermes-4"})])
    assert swap.clean


# -- helpers -------------------------------------------------------------

def _inline_cfg(raw: str):
    import yaml
    return yaml.safe_load(raw)
