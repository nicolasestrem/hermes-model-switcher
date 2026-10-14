"""Focused RED regressions for two PR #9 review findings in route_inventory.py.

Owns: ``route_inventory.py`` (shared Ri writer) only — never the DTO/API layer.

P2 #4271275343 — malformed ``auxiliary.openrouter_model`` scalars were accepted.
    The scalar slot is ONE bare model-id string, but preview validation only
    checked the companion fields (provider/base_url/api_mode); a direct Preview
    or saved-preset client could submit ``model: null`` (stored as the literal
    id ``"None"``), a number, a list, a dict, an empty/whitespace/untrimmed
    string and overwrite a valid fallback route. The fix shares ONE focused
    model-identifier validator with the speech assignment guard so the two
    cannot drift, and checks sibling scalar assignments without changing their
    intentional reset/empty sentinels (MoA preset catalog, delegation's
    inherit-when-empty).

P2 #4271275359 — unpinned plugin task routes mislabelled.
    When a registered plugin task declares ``inherit_from`` and its own route is
    absent or an unpinned placeholder (``{}``/timeout-only/auto-empty), the
    effective route correctly resolves the base, but ``_aux_observation`` kept
    ``source`` at ``default``/``configured``. ``/state`` then hid the real
    provider/model the runtime uses. The fix marks the row ``inherited`` under
    the runtime ``_layer_over_inherited`` pin semantics (own provider/model/
    base_url, including plugin-declared defaults), and never fabricates
    inheritance for an unresolved base or a cycle (which warn instead).
"""
from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

import route_inventory as ri

pytest.importorskip("hermes_cli.config_defaults")


# --------------------------------------------------------------------------- #
# helpers — REAL registry entries, never hand-built route dicts
# --------------------------------------------------------------------------- #


def _entry(key: str, *, base: Any = None, defaults: Dict[str, Any] | None = None,
           registered: Dict[str, Any] | None = None):
    build = pytest.importorskip("hermes_cli.plugins_aux_tasks")\
        .build_auxiliary_task_entry
    return build("review_regress_plug", "review_regress_plug", key,
                 display_name=key, description="", defaults=defaults or {},
                 inherit_from=base, registered=registered or {})


def _plugin_pair(child_defaults: Dict[str, Any] | None = None):
    """A registered base + child that declares ``inherit_from`` on the base."""
    base = _entry("rr_base")
    child = _entry("rr_child", base="rr_base", defaults=child_defaults,
                   registered={"rr_base": base})
    assert child["inherit_from"] == "rr_base", "real builder must keep inheritance"
    return base, child


def _obs(cfg: Dict[str, Any], slot_id: str, entries=None):
    return next(o for o in ri.build_inventory(cfg, plugin_aux_entries=entries)
                if o.descriptor.slot_id == slot_id)


def _obs_with_warnings(cfg: Dict[str, Any], slot_id: str, entries=None):
    res = ri.build_inventory(cfg, plugin_aux_entries=entries)
    return next(o for o in res if o.descriptor.slot_id == slot_id), res.warnings


# =========================================================================== #
# P2 #4271275343 — reject malformed OpenRouter scalar model identifiers
# =========================================================================== #

_BAD_MODELS = [None, 5, 3.2, True, ["a/b"], {"id": "a/b"}, "", "   ", "\t",
               "\n", " a/b", "a/b ", " a/b "]


@pytest.mark.parametrize("model", _BAD_MODELS)
def test_openrouter_malformed_model_is_refused_before_stringify(model):
    """Every malformed shape is refused in preflight: no stringified ``"None"``
    id, no overwrite of a valid stored route, and NOTHING is written."""
    cfg = {"auxiliary": {"openrouter_model": "nvidia/valid:free"}}
    before = copy.deepcopy(cfg)
    prev = ri.preview_changes(cfg, [ri.RouteEdit("auxiliary.openrouter_model",
                                                 {"model": model})])
    assert not prev.clean, (model, prev.changes)
    assert prev.changes == (), (model, prev.changes)
    assert cfg == before, "a refused edit must not mutate the source config"


def test_openrouter_missing_model_is_refused():
    prev = ri.preview_changes({}, [ri.RouteEdit("auxiliary.openrouter_model", {})])
    assert not prev.clean
    assert prev.changes == ()


def test_openrouter_valid_bare_model_stays_byte_exact():
    cfg: Dict[str, Any] = {}
    before = copy.deepcopy(cfg)
    model = "nvidia/nemotron-3-ultra-550b-a55b:free"
    prev = ri.preview_changes(cfg, [ri.RouteEdit("auxiliary.openrouter_model",
                                                 {"model": model})])
    assert prev.clean, prev.rejected
    chg = next(c for c in prev.changes
               if c.storage_path == "auxiliary.openrouter_model")
    assert chg.after == model  # bare string, byte-exact, never trimmed/rewritten
    assert cfg == before       # unit derivation performs no canonical write


@pytest.mark.parametrize("key", ["provider", "base_url", "api_mode"])
def test_openrouter_companion_route_fields_still_refused(key):
    """The scalar is a shared auto-chain setting, not a task route block."""
    prev = ri.preview_changes({}, [ri.RouteEdit("auxiliary.openrouter_model",
                                                {"model": "a/b", key: "x"})])
    assert not prev.clean
    assert prev.changes == ()


def test_model_identifier_validator_is_shared_with_the_speech_guard():
    """One focused validator backs BOTH the speech assignment guard and the new
    OpenRouter scalar, so the model-string class cannot drift between them."""
    helper = getattr(ri, "_model_identifier_refusal", None)
    assert callable(helper), "expected a shared model-identifier validator"
    for model in _BAD_MODELS:
        assert helper("s", model, "x") is not None, model
    assert helper("s", "a/b", "x") is None
    # ...and the speech assignment guard is demonstrably wired to it
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "specialist.tts", {"provider": "openai", "model": None})])
    assert not prev.clean and prev.changes == ()


def test_sibling_scalar_sentinels_are_untouched():
    """Guard the sibling scalar assignments against scope creep: MoA preset
    validation still rejects a non-catalog model, and delegation.model keeps its
    documented inherit-when-empty sentinel."""
    moa_cfg = {"moa": {"presets": {"p": {"model": "m"}}, "default_preset": "p"}}
    bad = ri.preview_changes(moa_cfg, [ri.RouteEdit("moa.default_preset",
                                                    {"model": None})])
    assert not bad.clean, bad.rejected  # catalog name required, never stringify
    # delegation.model '' means inherit-parent (descriptor inherit_when_empty)
    d_obs = _obs({}, "delegation.model")
    assert d_obs.descriptor.inherit_when_empty is True


# =========================================================================== #
# P2 #4271275359 — unpinned plugin task routes must read as INHERITED
# =========================================================================== #


def test_unpinned_plugin_task_absent_block_reports_inherited():
    base, child = _plugin_pair()
    cfg = {"auxiliary": {"rr_base": {"provider": "somecloud", "model": "cloud-y"}}}
    obs = _obs(cfg, "auxiliary.rr_child", [base, child])
    assert obs.effective["provider"] == "somecloud" and obs.effective["model"] == "cloud-y"
    assert obs.source == "inherited"
    # configured stays the RAW stored value (absent here) — never conflated with
    # the resolved effective route.
    assert obs.configured_present is False


def test_unpinned_plugin_task_empty_block_reports_inherited():
    base, child = _plugin_pair()
    cfg = {"auxiliary": {"rr_base": {"provider": "somecloud", "model": "cloud-y"},
                         "rr_child": {}}}
    obs = _obs(cfg, "auxiliary.rr_child", [base, child])
    assert obs.effective["provider"] == "somecloud"
    assert obs.source == "inherited"


@pytest.mark.parametrize("block", [
    {"timeout": 120},                                   # timeout-only
    {"provider": "auto", "model": "", "base_url": ""},  # picker auto/empty placeholder
    {"provider": "AUTO", "model": "  ", "base_url": " "},
])
def test_unpinned_plugin_task_placeholder_blocks_report_inherited(block):
    base, child = _plugin_pair()
    cfg = {"auxiliary": {"rr_base": {"provider": "somecloud", "model": "cloud-y"},
                         "rr_child": block}}
    obs = _obs(cfg, "auxiliary.rr_child", [base, child])
    assert obs.source == "inherited", (block, obs.source, obs.effective)
    assert obs.effective["provider"] == "somecloud"


def test_pinned_plugin_task_stays_configured_and_never_leaks_base_url():
    base, child = _plugin_pair()
    cfg = {"auxiliary": {
        "rr_base": {"provider": "somecloud", "model": "cloud-y",
                    "base_url": "https://somecloud.internal/v1",
                    "api_key": "SECRET-CLOUD"},
        "rr_child": {"provider": "nous", "model": "own-model"}}}
    obs = _obs(cfg, "auxiliary.rr_child", [base, child])
    assert obs.source == "configured"          # own route pinned, not inherited
    assert obs.effective["provider"] == "nous" and obs.effective["model"] == "own-model"
    assert not obs.effective.get("base_url")   # pinned route travels as one unit
    assert obs.effective.get("api_key") != "SECRET-CLOUD"


def test_plugin_declared_defaults_that_pin_do_not_claim_inheritance():
    """A task whose OWN declared defaults pin the route is not following the
    base: its source stays default/configured, never 'inherited'."""
    base, child = _plugin_pair(child_defaults={"provider": "nous", "model": "pinned"})
    cfg = {"auxiliary": {"rr_base": {"provider": "somecloud", "model": "cloud-y"}}}
    obs = _obs(cfg, "auxiliary.rr_child", [base, child])
    assert obs.effective["provider"] == "nous"   # own default wins over base
    assert obs.source != "inherited"


def test_valid_base_auto_routing_is_inherited_without_inventing_a_model():
    """A real base's automatic route is still inherited, not an unknown base.
    Report auto honestly instead of guessing a concrete runtime model."""
    base, child = _plugin_pair()
    cfg = {"auxiliary": {"rr_child": {}}}
    obs = _obs(cfg, "auxiliary.rr_child", [base, child])
    assert obs.source == "inherited", (obs.source, obs.effective)
    assert obs.effective["provider"] == "auto"


def test_unresolved_base_warns_and_never_fabricates_inheritance():
    ghost = {"key": "rr_ghost", "display_name": "g", "description": "",
             "plugin": "review_regress_plug", "defaults": {},
             "inherit_from": "no_such_base"}
    obs, warnings = _obs_with_warnings({"auxiliary": {"rr_ghost": {}}},
                                       "auxiliary.rr_ghost", [ghost])
    assert obs.source != "inherited", (obs.source, obs.effective)
    assert any("rr_ghost" in w and "inherit_from" in w for w in warnings), warnings


def test_inherit_cycle_keeps_no_inheritance_and_still_warns():
    a = {"key": "rr_a", "display_name": "a", "description": "", "plugin": "p",
         "defaults": {}, "inherit_from": "rr_b"}
    b = {"key": "rr_b", "display_name": "b", "description": "", "plugin": "p",
         "defaults": {}, "inherit_from": "rr_a"}
    obs, warnings = _obs_with_warnings({}, "auxiliary.rr_a", [a, b])
    assert obs.source != "inherited", (obs.source, obs.effective)
    assert any("cycle" in w.lower() or "circular" in w.lower() for w in warnings), warnings


def test_builtin_unset_selector_keeps_its_default_source():
    """Regression guard: the change only concerns plugin-declared inheritance —
    a built-in unset task still reports 'default' with the runtime auto route."""
    obs = _obs({}, "auxiliary.compression")
    assert obs.source == "default"
    assert obs.effective["provider"] == "auto" and obs.effective["model"] == ""
