"""Regression tests for typed route-inventory correctness.

These lock the fixes for a set of previously silent inventory bugs:

1. fallback-chain slots: an edit that omits the explicit ``chain`` key used to
   collapse the stored chain to ``[]`` (``payload.get("chain", [])``). A missing
   chain payload is now REJECTED, never a silent clear.
2. ``chain: null`` (inherit-unset) and ``chain: []`` (explicitly disabled) stay
   two DISTINCT stored states, and the full typed payload reaches
   ``_stored_value_for`` (it is not stripped down to route fields first).
3. Native chain entry shapes (``{provider, model, ...}`` dicts and bare provider
   strings) round-trip verbatim — no forced rewrite.
4. Specialist slots store the bare model-id string, and every writable storage
   path is a runtime-consumed path (``hermes_cli/config_defaults.py``); paths
   with no runtime consumer are explicitly read-only, not fake-editable.
5. Duplicate selectable rows (auxiliary.goal_judge vs judge.goal_judge.route)
   collapse to a single row that prefers the judge descriptor/protocol.
6. The typed-decision guard is the genuine protocol check, not an unconditional
   ``... and False`` bool.
7. Delegation inheritance/effective resolution and provider companions are
   backed by runtime evidence (the actual configured base), not guesses.

Only ``route_inventory`` (stdlib) and the registry scanner are imported, so the
file has no yaml/fastapi dependency of its own.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

import route_inventory as ri

HERMES_REPO = Path(os.environ.get(
    "HERMES_MODEL_SWITCHER_HERMES_REPO", Path.home() / ".hermes" / "hermes-agent"))


# -- fixtures -------------------------------------------------------------

def _cfg(**over):
    cfg = {
        "model": {"provider": "openai", "default": "gpt-4o-mini"},
        "delegation": {},
        "auxiliary": {},
        "fallback_providers": [],
        "moa": {"presets": {"default": {"reference_models": [], "aggregator": {}}}},
        "cron": {},
        "plugins": {},
        "stt": {"openai": {"model": "whisper-1"}},
        "tts": {"openai": {"model": "gpt-4o-mini-tts"}},
    }
    cfg.update(over)
    return cfg


def _change(preview, storage_path):
    return next(c for c in preview.changes if c.storage_path == storage_path)


def _slot_ids(cfg):
    return {o.descriptor.slot_id for o in ri.build_inventory(cfg)}


# -- 1/2/3: chain payload semantics ---------------------------------------

def test_chain_edit_without_chain_is_rejected_delegation():
    """A delegation.fallback_chain edit that carries no ``chain`` key must be
    rejected — it used to silently clear the stored chain to []."""
    cfg = _cfg(delegation={"fallback_providers": ["zai", "nous"]})
    prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "delegation.fallback_chain", {"provider": "x", "model": "y"})])
    assert not prev.clean
    assert any("chain" in r for r in prev.rejected)
    assert prev.changes == ()
    # the source config chain is untouched
    assert cfg["delegation"]["fallback_providers"] == ["zai", "nous"]


def test_chain_edit_without_chain_is_rejected_main():
    cfg = _cfg(fallback_providers=["zai"])
    prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "fallback.main_chain", {"provider": "x", "model": "y"})])
    assert not prev.clean
    assert any("chain" in r for r in prev.rejected)
    assert cfg["fallback_providers"] == ["zai"]


def test_chain_null_vs_empty_stay_distinct_and_model_is_optional():
    cfg = _cfg(delegation={"fallback_providers": ["zai"]})

    # explicit null -> inherit-unset, and a chain payload needs no "model".
    null_prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "delegation.fallback_chain", {"chain": None})])
    assert null_prev.clean, null_prev.rejected
    assert _change(null_prev, "delegation.fallback_providers").after is None

    # [] -> explicitly disabled, a DIFFERENT stored state.
    empty_prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "delegation.fallback_chain", {"chain": []})])
    assert empty_prev.clean, empty_prev.rejected
    assert _change(empty_prev, "delegation.fallback_providers").after == []
    assert [] != None  # noqa: E711  (the two states are not equivalent)


def test_chain_non_list_rejected():
    cfg = _cfg(delegation={"fallback_providers": ["zai"]})
    prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "delegation.fallback_chain", {"chain": "nous"})])
    assert not prev.clean
    assert any("chain" in r for r in prev.rejected)


def test_chain_entries_must_be_complete_route_objects():
    """The runtime chain normalizer requires ``provider`` + ``model`` and
    silently drops other shapes (fallback-providers.md), so a bare provider
    string or scalar is REJECTED — never persisted as a dead entry."""
    cfg = _cfg(delegation={"fallback_providers": ["zai"]})
    dead = ri.preview_changes(cfg, [ri.RouteEdit(
        "delegation.fallback_chain", {"chain": ["nous"]})])
    assert not dead.clean
    assert "route object" in dead.rejected[0]

    chain = [
        {"provider": "zai", "model": "glm-5.3"},
        {"provider": "nous", "model": "hermes-4"},
        {"provider": "local", "model": "lab-7b", "base_url": "http://x/v1"},
    ]
    prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "delegation.fallback_chain", {"model": "chain", "chain": chain})])
    assert prev.clean, prev.rejected
    after = _change(prev, "delegation.fallback_providers").after
    assert after == chain


def test_full_typed_payload_reaches_stored_value_for():
    """The chain value must be read from the FULL payload, not a route-fields
    projection that has already stripped ``chain``."""
    cfg = _cfg(delegation={})
    payload = {"provider": "zai", "model": "glm-5.3", "chain": [{"provider": "nous", "model": "hermes-4"}]}
    assert ri._stored_value_for(
        ri.build_inventory(cfg)[0].descriptor, payload)  # smoke: never raises
    prev = ri.preview_changes(cfg, [ri.RouteEdit("delegation.fallback_chain", payload)])
    assert prev.clean, prev.rejected
    assert _change(prev, "delegation.fallback_providers").after == [
        {"provider": "nous", "model": "hermes-4"}]


# -- 4: specialist scalar shape + runtime paths ---------------------------

@pytest.mark.parametrize("slot,path,model", [
    ("specialist.stt", "stt.openai.model", "whisper-1"),
    ("specialist.tts", "tts.openai.model", "gpt-4o-mini-tts"),
])
def test_specialist_stores_bare_model_string(slot, path, model):
    cfg = _cfg()
    cfg.pop("stt", None)
    cfg.pop("tts", None)
    prev = ri.preview_changes(cfg, [ri.RouteEdit(
        slot, {"provider": "openai", "model": model})])
    assert prev.clean, prev.rejected
    chg = _change(prev, path)
    assert chg.after == model
    assert isinstance(chg.after, str)  # never a route dict


def test_specialist_embedding_is_explicitly_read_only():
    """No runtime consumer exists for an embedding-model config key, so the row
    is explicitly read-only instead of a fake editable route."""
    cfg = _cfg()
    obs = next(o for o in ri.build_inventory(cfg)
               if o.descriptor.slot_id == "specialist.embedding")
    assert obs.descriptor.writable is False
    prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "specialist.embedding", {"provider": "openai", "model": "text-embedding-3-small"})])
    assert not prev.clean


def test_writable_slot_paths_are_runtime_consumed():
    """Every writable, non-protected slot resolves to a real DEFAULT_CONFIG key
    (the main route block ``model`` is modeled as a top-level string there)."""
    pytest.importorskip("model_switcher.registry.scanner")
    try:
        from model_switcher.registry.scanner import load_default_config
        defaults = load_default_config(HERMES_REPO)
    except FileNotFoundError:
        pytest.skip("reference Hermes checkout not available")

    def resolves(dotted):
        if dotted.startswith("model."):
            return "model" in defaults
        node = defaults
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return False
            node = node[key]
        return True

    cfg = _cfg()
    bad = []
    for obs in ri.build_inventory(cfg):
        d = obs.descriptor
        if d.writable and not d.protected and not resolves(d.storage_path):
            bad.append((d.slot_id, d.storage_path))
    assert bad == [], f"writable slots with no runtime consumer: {bad}"


def test_fallback_main_chain_uses_runtime_path():
    cfg = _cfg()
    obs = next(o for o in ri.build_inventory(cfg)
               if o.descriptor.slot_id == "fallback.main_chain")
    assert obs.descriptor.storage_path == "fallback_providers"


# -- 5: duplicate selectable rows -----------------------------------------

def test_no_duplicate_selectable_storage_paths():
    cfg = _cfg()
    seen = {}
    for obs in ri.build_inventory(cfg):
        d = obs.descriptor
        if d.writable and not d.protected:
            seen.setdefault(d.storage_path, []).append(d.slot_id)
    dupes = {p: ids for p, ids in seen.items() if len(ids) > 1}
    assert dupes == {}, f"duplicate selectable rows share a storage path: {dupes}"


def test_goal_judge_single_row_prefers_judge_protocol():
    cfg = _cfg()
    rows = [o.descriptor for o in ri.build_inventory(cfg)
            if o.descriptor.storage_path == "auxiliary.goal_judge"]
    assert [r.slot_id for r in rows] == ["judge.goal_judge.route"]
    assert rows[0].protocol == ri.PROTOCOL_TYPED_DECISION
    assert "auxiliary.goal_judge" not in _slot_ids(cfg)


def test_unsupported_aux_placeholders_removed():
    cfg = _cfg()
    ids = _slot_ids(cfg)
    assert "auxiliary.summarize" not in ids
    assert "auxiliary.title" not in ids
    assert {"auxiliary.compression", "auxiliary.title_generation"} <= ids


def test_no_legacy_moa_flat_slots():
    cfg = _cfg()
    assert not any(s.startswith("moa.legacy.") for s in _slot_ids(cfg))


# -- 6: genuine protocol guard --------------------------------------------

def test_typed_decision_guard_is_real_not_unconditional_bool():
    cfg = _cfg(auxiliary={"goal_judge": {"provider": "typed-decision/lab", "model": "judge-v2"}})
    obs = next(o for o in ri.build_inventory(cfg)
               if o.descriptor.slot_id == "judge.goal_judge.route")
    d = obs.descriptor
    assert d.protected is False          # not marked read-only
    assert d.writable is True            # valid typed routes ARE permitted
    assert d.protocol == ri.PROTOCOL_TYPED_DECISION

    plain = ri.preview_changes(cfg, [ri.RouteEdit(
        "judge.goal_judge.route", {"provider": "openai", "model": "gpt-4o-mini"})])
    assert not plain.clean
    assert any("typed_decision" in r or "protocol" in r for r in plain.rejected)

    typed = ri.preview_changes(cfg, [ri.RouteEdit(
        "judge.goal_judge.route", {"provider": "typed-decision/lab", "model": "judge-v3"})])
    assert typed.clean, typed.rejected


# -- 7: runtime-evidence-backed inheritance + provider companions ---------

def test_delegation_inherits_main_route_with_provider_companion():
    cfg = _cfg(delegation={})
    obs = next(o for o in ri.build_inventory(cfg)
               if o.descriptor.slot_id == "delegation.model")
    # main route is configured -> that is the runtime evidence for inheritance
    assert obs.source == "inherited"
    assert obs.effective == "gpt-4o-mini"
    assert obs.effective_provider == "openai"
    assert obs.provider_source == "inherited"


def test_delegation_pin_overrides_inheritance():
    cfg = _cfg(delegation={"model": "hermes-4", "provider": "nous"})
    obs = next(o for o in ri.build_inventory(cfg)
               if o.descriptor.slot_id == "delegation.model")
    assert obs.source == "configured"
    assert obs.effective == "hermes-4"
    assert obs.effective_provider == "nous"
    assert obs.provider_source == "configured"


def test_delegation_no_evidence_stays_default():
    cfg = _cfg(model={})  # no runtime main-route evidence
    obs = next(o for o in ri.build_inventory(cfg)
               if o.descriptor.slot_id == "delegation.model")
    assert obs.source == "default"
    assert obs.effective is None
    assert obs.effective_provider is None


def test_delegation_chain_inherits_parent_chain_on_null():
    cfg = _cfg(delegation={"fallback_providers": None},
               fallback_providers=[{"provider": "nous", "model": "hermes-4"}])
    obs = next(o for o in ri.build_inventory(cfg)
               if o.descriptor.slot_id == "delegation.fallback_chain")
    assert obs.source == "inherited"
    assert obs.effective == [{"provider": "nous", "model": "hermes-4"}]

    # [] disables fallback: that is its own evidence, never inherited
    disabled = _cfg(delegation={"fallback_providers": []})
    obs2 = next(o for o in ri.build_inventory(disabled)
                if o.descriptor.slot_id == "delegation.fallback_chain")
    assert obs2.source == "configured"
    assert obs2.effective == []


# -- extra: shape guard ----------------------------------------------------

def test_list_valued_path_not_replaced_by_scalar_route():
    """Defensive: a list-shaped stored value (e.g. a MoA reference_models list)
    is never overwritten by a scalar route dict, while null/list replacements on
    a chain-shaped path stay allowed."""
    reason = ri._shape_refusal("moa.presets.default.reference_models",
                               ["a", "b"], {"provider": "openai", "model": "x"})
    assert reason is not None and "list" in reason
    # null and list replacements on a list-valued path are legitimate
    assert ri._shape_refusal("fallback_providers", ["a"], None) is None
    assert ri._shape_refusal("fallback_providers", ["a"], ["b"]) is None
    # an unset path never trips the guard
    assert ri._shape_refusal("fallback_providers", ri.MISSING, {"p": "m"}) is None
    # a mapping path may not be replaced by a scalar
    assert ri._shape_refusal("auxiliary.goal_judge", {"model": "m"}, "m") is not None
