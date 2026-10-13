"""T0 completeness gate: every configurable model-selection slot has a descriptor."""
import os
import sys
from pathlib import Path

import pytest

import model_switcher.registry as reg
from model_switcher.registry import (
    build_registry,
    coverage_gaps,
    load_default_config,
    normalize_candidate,
    scan_core,
    scan_plugins,
    is_excluded,
)

REPO = Path(__file__).resolve().parent.parent
HERMES_REPO = Path(os.environ.get(
    "HERMES_MODEL_SWITCHER_HERMES_REPO", Path.home() / ".hermes" / "hermes-agent"))
# Read-only scan fixture; resolved from the environment, never a host literal.
# The registry baseline was reviewed against a specific installed-plugin set
# (jev-judge, jev-skill-router — see PLUGIN_ROLES). Scan the dir that actually
# carries that baseline (env override wins), so a host with extra plugins
# doesn't silently change the reviewed baseline; genuinely unknown plugin
# selectors still surface as coverage gaps whenever HERMES_MODEL_SWITCHER_PLUGINS_DIR
# points at them.
def _default_plugins_dir() -> Path:
    root = Path.home() / ".hermes"
    candidates = [root / "plugins", *sorted((root / "profiles").glob("*/plugins"))]
    baseline = [c for c in candidates
                if c.is_dir() and (c / "jev-judge").is_dir()]
    if baseline:
        return baseline[0]
    for cand in candidates:
        if cand.is_dir() and any(cand.iterdir()):
            return cand
    return root / "plugins"


PLUGINS_DIR = Path(os.environ.get("HERMES_MODEL_SWITCHER_PLUGINS_DIR",
                                  _default_plugins_dir()))


@pytest.fixture(scope="module")
def registry():
    return build_registry()


@pytest.fixture(scope="module")
def core_candidates():
    return scan_core(load_default_config(HERMES_REPO))


@pytest.fixture(scope="module")
def plugin_candidates():
    return scan_plugins(PLUGINS_DIR)


def test_every_configurable_selector_has_descriptor(registry, core_candidates, plugin_candidates):
    """Every scanned candidate must map to a slot descriptor or a registered exclusion."""
    gaps = coverage_gaps(core_candidates, registry)
    assert gaps == [], f"core model-selection candidates without descriptors: {gaps}"
    for plugin_name, cands in plugin_candidates.items():
        plugin_gaps = coverage_gaps(cands, registry)
        assert plugin_gaps == [], f"{plugin_name} candidates without descriptors: {plugin_gaps}"
    assert len(core_candidates) >= 40, "scanner stopped finding core candidates"
    assert "jev-judge" in plugin_candidates, "installed plugin jev-judge not scanned"


def test_unset_inactive_and_plugin_slots_discovered(registry):
    """Unset (default ''), inactive, and plugin-owned slots are in the registry."""
    ids = {s["slot_id"] for s in registry["slots"]}
    # Unset by default (empty string) but still first-class slots:
    for slot in ("auxiliary.review.model", "cron.model", "delegation.model"):
        assert slot in ids, f"unset-by-default slot missing: {slot}"
    # Per-backend speech slots normalize into their family descriptor:
    from model_switcher.registry import normalize_candidate
    assert normalize_candidate("stt.xai.model") in ids
    # Inactive-by-default / gated slots:
    assert "auxiliary.background_review.model" in ids
    assert "moa.presets.<name>.reference_models[].provider/model" in ids
    # Plugin-owned slots (discovered from installed plugin manifests):
    assert "plugin:jev-judge:config_schema.jev_model" in ids
    assert "plugin:jev-judge:config_schema.typesafe_model" in ids
    assert "plugin:jev-judge:config_schema.openrouter_model" in ids
    assert "omh.routing.model-chains.categories.<name>.chain[]" in ids


def test_model_metadata_not_misclassified(registry):
    """Metadata and non-model 'model*' keys must be exclusions, never slots."""
    ids = {s["slot_id"] for s in registry["slots"]}
    for path in ("model_overrides", "model_catalog", "model_thresholds",
                 "models_dev", "models_max", "model_dir",
                 "wake_word.openwakeword.model"):
        assert is_excluded(path), f"metadata key not excluded: {path}"
        assert path not in ids
    # cron.provider (scheduler) is not a model slot; cron.model_provider is the inference pair
    assert "cron.provider" not in ids
    assert "cron.model_provider" in ids
    cron_model = next(s for s in registry["slots"] if s["slot_id"] == "cron.model")
    assert "cron.model_provider" in cron_model["companions"]
    assert "scheduler" in cron_model["notes"]
    # Delegation fallback null-vs-[] semantics preserved, not normalized
    deleg_fb = next(s for s in registry["slots"]
                    if s["slot_id"] == "delegation.fallback_providers[].provider/model")
    assert "null = inherit" in deleg_fb["inheritance"] and "[] = disable" in deleg_fb["inheritance"]


def test_judge_protocols_and_jev_thresholds(registry, plugin_candidates):
    """Typed judge slots are distinct roles; jev thresholds are not model slots."""
    ids = {s["slot_id"] for s in registry["slots"]}
    roles = {s["slot_id"]: s["role"] for s in registry["slots"]}
    assert roles["auxiliary.goal_judge.model"] == "judge"
    assert roles["auxiliary.approval.model"] == "judge"
    assert roles["auxiliary.review.model"] == "reviewer"
    assert roles["plugin:jev-judge:config_schema.jev_model"] == "plugin_judge"
    # jev-judge scanned candidates are exactly the three model fields
    scanned = {c["path"].rsplit(".", 1)[-1] for c in plugin_candidates["jev-judge"]}
    assert scanned == {"jev_model", "typesafe_model", "openrouter_model"}
    # Threshold keys are protected: no descriptor carries them as slot ids
    assert not any("threshold" in i for i in ids)


def test_omh_code_constants_immutable(registry):
    """OMH shipped model chains are code constants: labeled immutable, never patched."""
    omh = next(s for s in registry["slots"] if s["slot_id"].startswith("omh."))
    assert omh["adapter"] == "omh_chains_store"
    assert "immutable" in omh["notes"]


def test_registry_is_versioned_and_serializable(registry):
    import json
    payload = json.dumps(registry)
    assert registry["registry_version"] >= 1
    assert registry["hermes_baseline"].startswith("590af66a58")
    for slot in registry["slots"]:
        for key in ("slot_id", "owner", "role", "storage", "inheritance",
                    "validator", "adapter", "activation", "support"):
            assert slot[key], f"slot {slot['slot_id']} missing {key}"
    assert len(payload) > 0
    # slot ids unique
    ids = [s["slot_id"] for s in registry["slots"]]
    assert len(ids) == len(set(ids))


def test_speech_slots_use_model_id_shapes(core_candidates):
    """Speech fields (tts/stt model_id vs model) normalize into family slots."""
    normalized = {normalize_candidate(c["path"]) for c in core_candidates}
    assert "tts.<backend>.model|model_id" in normalized
    assert "stt.<backend>.model|model_id" in normalized
    assert any(p.startswith("tts.elevenlabs.model_id") for p in
               (c["path"] for c in core_candidates))
