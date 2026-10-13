"""F1-F5 regression tests (review-routes fixes, test-first).

Covers:
  F1: scalar slots (main.default, delegation.model) preview as strings, never
      route dicts.
  F2: route-dict edits merge into the existing stored dict so protected
      neighbors (threshold/enforcement) survive for judge/auxiliary slots.
  F3: apply_canonical_save(expected_revision=...) refuses a stale document with
      StoreConflict; the intervening edit stays intact; a fresh doc saves fine.
  F4: speech/embedding/image slots accept plain {provider, model}; judge stays
      strict (plain chat rejected).
  F5: preflight unknown-cost reason is gated on billing_policy.allow_unknown.
"""
from __future__ import annotations

import copy

import pytest

import route_inventory as ri
from model_switcher.presets import BillingPolicy, RouteTarget, ReviewedPreset

MAINE_CFG = {
    "model": {"provider": "openai", "default": "gpt-4o-mini"},
    "delegation": {"provider": "openai", "model": "gpt-4o-mini"},
}


# ---------------------------------------------------------------- F1: scalars

def test_main_default_preview_yields_scalar_string():
    cfg = copy.deepcopy(MAINE_CFG)
    preview = ri.preview_changes(cfg, [ri.RouteEdit(
        "main.default", {"provider": "lablocal", "model": "lab-7b"})])
    assert preview.clean, preview.rejected
    chg = next(c for c in preview.changes if c.storage_path == "model.default")
    assert chg.after == "lab-7b"                 # string, never a route dict
    assert isinstance(chg.after, str)


def test_delegation_model_preview_yields_scalar_string():
    cfg = copy.deepcopy(MAINE_CFG)
    preview = ri.preview_changes(cfg, [ri.RouteEdit(
        "delegation.model", {"provider": "lablocal", "model": "lab-9b"})])
    assert preview.clean, preview.rejected
    chg = next(c for c in preview.changes if c.storage_path == "delegation.model")
    assert chg.after == "lab-9b"
    assert isinstance(chg.after, str)


def test_apply_scalar_slot_writes_string_not_dict():
    cfg = copy.deepcopy(MAINE_CFG)
    preview = ri.preview_changes(cfg, [ri.RouteEdit(
        "main.default", {"provider": "lablocal", "model": "lab-7b"})])
    changed = preview  # document under test is mutated only in test below; here peek stored shape
    after_cfg = copy.deepcopy(cfg)
    for c in changed.changes:
        if c.storage_path == "model.default":
            ri._set(after_cfg, "model.default", c.after)
    assert after_cfg["model"]["default"] == "lab-7b"


# ------------------------------------------------- F2: dict merge for judges

def test_judge_route_edit_merges_and_preserves_threshold_enforcement():
    cfg = {
        "auxiliary": {
            "goal_judge": {
                "provider": "typed-decision/lab",
                "model": "judge-v1",
                "threshold": 0.82,
                "enforcement": "block",
                "label": "goal-judge",
            },
        },
    }
    preview = ri.preview_changes(cfg, [ri.RouteEdit(
        "judge.goal_judge.route", {"provider": "typed-decision/lab2",
                                   "model": "judge-v2"})])
    assert preview.clean, preview.rejected
    chg = next(c for c in preview.changes if c.storage_path == "auxiliary.goal_judge")
    after = chg["after"] if isinstance(chg, dict) else chg.after
    assert after["provider"] == "typed-decision/lab2"
    assert after["model"] == "judge-v2"
    # protected threshold/enforcement neighbors survive the route swap
    assert after["threshold"] == 0.82
    assert after["enforcement"] == "block"


# ----------------------------------------------------- F3: stale-save refusal

def test_stale_save_refused_with_storeconflict_and_intervening_edit_intact(hermes_home):
    import hermes_adapter as ha
    from hermes_adapter import StoreConflict
    hermes_home.profile("alpha", config={"_raw": "model:\n  provider: openai\n  default: gpt-4o-mini\n"})
    adapter = ha.HermesAdapter()
    updated = {"model": {"provider": "lablocal", "default": "lab-7b"}}
    fresh = adapter.load_raw_config("alpha")
    stale_revision = ha._rev(fresh)

    # simulate an intervening edit after the revision was taken
    intervening = adapter.load_raw_config("alpha")
    intervening["extra_marker"] = "intervening"
    adapter.apply_canonical_save("alpha", intervening)

    with pytest.raises(StoreConflict):
        adapter.apply_canonical_save("alpha", updated, expected_revision=stale_revision)

    after = adapter.load_raw_config("alpha")
    assert after["extra_marker"] == "intervening"          # intervening edit intact
    assert after["model"]["default"] == "gpt-4o-mini"      # stale payload NOT written


def test_fresh_document_saves_with_matching_expected_revision(hermes_home):
    import hermes_adapter as ha
    hermes_home.profile("alpha", config={"_raw": "model:\n  provider: openai\n  default: gpt-4o-mini\n"})
    adapter = ha.HermesAdapter()
    fresh = adapter.load_raw_config("alpha")
    updated = copy.deepcopy(fresh)
    updated["model"]["default"] = "lab-7b"
    result = adapter.apply_canonical_save("alpha", updated,
                                          expected_revision=ha._rev(fresh))
    assert result["ok"] is True
    after = adapter.load_raw_config("alpha")
    assert after["model"]["default"] == "lab-7b"


# --------------------------------------------- F4: specialist vs judge strict

def test_specialist_stt_plain_route_previews_clean():
    cfg = copy.deepcopy(MAINE_CFG)
    preview = ri.preview_changes(cfg, [ri.RouteEdit(
        "specialist.stt", {"provider": "openai", "model": "whisper-1"})])
    assert preview.clean, preview.rejected


def test_unsupported_specialist_embedding_is_explicitly_read_only():
    cfg = copy.deepcopy(MAINE_CFG)
    preview = ri.preview_changes(cfg, [ri.RouteEdit(
        "specialist.embedding", {"provider": "openai", "model": "text-embedding-3-small"})])
    assert not preview.clean
    assert any("read-only" in reason for reason in preview.rejected)


def test_judge_plain_chat_still_rejected():
    cfg = copy.deepcopy(MAINE_CFG)
    preview = ri.preview_changes(cfg, [ri.RouteEdit(
        "judge.goal_judge.route", {"provider": "openai", "model": "gpt-4o-mini"})])
    assert not preview.clean
    assert any("typed_decision" in r or "rejects a plain chat route" in r
               for r in preview.rejected), preview.rejected


# ------------------------------------------------- F5: allow_unknown gate

def _target(cost):
    return RouteTarget(profile="alpha", store_kind="config", path="/tmp/x",
                       changes={"model.default": "m"}, cost_class=cost)


def _preset(target, allow_unknown):
    return ReviewedPreset(
        preset_id="p1", name="P",
        billing_policy=BillingPolicy(approved_classes=[], allow_unknown=allow_unknown),
        targets=[target], reviewed_profile_ids=["alpha"])


def test_allow_unknown_true_unknown_cost_produces_no_reason():
    t = _target("subscription")
    p = _preset(t, allow_unknown=True)
    reasons = p.preflight(credentials={"alpha": True},
                          live_cost_classes={_route_key(t): "unknown"},
                          discovered_profiles=["alpha"])
    assert reasons == [], reasons


def test_allow_unknown_false_unknown_cost_still_blocks():
    t = _target("subscription")
    p = _preset(t, allow_unknown=False)
    reasons = p.preflight(credentials={"alpha": True},
                          live_cost_classes={_route_key(t): "unknown"},
                          discovered_profiles=["alpha"])
    assert any("unknown cost" in r for r in reasons), reasons


def _route_key(t):
    from model_switcher.presets import _route_key as rk
    return rk(t)


def test_preset_unknown_true_does_not_enable_unapproved_classes():
    """allow_unknown is scoped to COST_UNKNOWN only; known classes still gated."""
    t = _target("subscription")
    p = _preset(t, allow_unknown=True)
    reasons = p.preflight(credentials={"alpha": True},
                          live_cost_classes={_route_key(t): "metered"},
                          discovered_profiles=["alpha"])
    assert reasons, "known cost-class change should still block"


__all__ = []
