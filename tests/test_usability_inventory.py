"""Strict-TDD suite for the usability polish inventory work.

These tests define the contract BEFORE implementation (RED), against the
INSTALLED Hermes runtime — never against a frozen key list:

1.  Complete aux inventory: every built-in ``DEFAULT_CONFIG['auxiliary']`` key
    whose stored shape is a route dict (``provider``/``model``) is a selectable
    slot. Policies/scalars (``transient_retries``, ``free_only``,
    ``stream_only_base_urls``, ``openrouter_model``) are NOT aux selectors;
    ``openrouter_model`` is covered separately as a cross-cutting override.
2.  ``auxiliary.goal_judge`` stays judge-owned: one row, judge protocol, no
    duplicate ``auxiliary.goal_judge`` slot.
3.  Obsolete aux leftovers (``auxiliary.session_search`` /
    ``auxiliary.web_extract`` — the runtime ignores them, see
    config_defaults.py) produce no slot and cannot be edited.
4.  Plugin-registered auxiliary tasks (real ``hermes_cli.plugins_aux_tasks.
    build_auxiliary_task_entry`` API) become slots; a supported
    ``inherit_from`` resolves the base's effective route until the task is
    pinned; a base's base_url/api_key never leak into a pinned route.
5.  Speech: unset STT resolves to local (``tools.transcription_common.
    DEFAULT_PROVIDER``), unset TTS to edge (``tools.tts_tool.DEFAULT_PROVIDER``
    == ``config_defaults['tts']['provider']``); model-bearing backends take
    their real ``stt.<provider>.model`` / ``model_id`` keys; no-model backends
    (edge, xai, piper) are read-only WITHOUT an invented
    ``tts.<backend>.model`` key.
6.  Edits are real previews: neighbors (e.g. vision's ``download_timeout``)
    survive the route swap, typed review protocol unchanged, scalar
    ``auxiliary.openrouter_model`` edits store the bare model id.
"""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

import route_inventory as ri

pytest.importorskip("hermes_cli.config_defaults")  # installed Hermes defaults

from hermes_cli.config_defaults import DEFAULT_CONFIG  # noqa: E402


# ---------------------------------------------------------------------------
# helpers


def _aux_defaults() -> Dict[str, Any]:
    aux = DEFAULT_CONFIG["auxiliary"]
    return dict(aux) if isinstance(aux, dict) else {}


def _expected_builtin_aux_slot_ids() -> set:
    """Independent re-derivation from the INSTALLED defaults module."""
    aux = _aux_defaults()
    return {
        f"auxiliary.{key}" for key, block in aux.items()
        if isinstance(block, dict) and "provider" in block
    } - {"auxiliary.goal_judge"}  # judge-owned: no duplicate


def _inventory_slot_ids(cfg: Dict[str, Any], plugin_entries=None) -> set:
    return {o.descriptor.slot_id
            for o in ri.build_inventory(cfg, plugin_aux_entries=plugin_entries)}


def _aux_obs(cfg: Dict[str, Any], key: str, plugin_entries=None):
    return next(o for o in ri.build_inventory(cfg, plugin_aux_entries=plugin_entries)
                if o.descriptor.slot_id == f"auxiliary.{key}")


def _slot_obs(cfg: Dict[str, Any], slot_id: str):
    return next(o for o in ri.build_inventory(cfg)
                if o.descriptor.slot_id == slot_id)


def _entries_by_key(entries) -> Dict[str, Any]:
    return {e["key"]: e for e in entries}


def _plugin_entry(key: str, *, base: Any = None, label: str = "My task",
                  description: str = "demo", defaults: Dict[str, Any] | None = None,
                  owner: str = "usability_plug", registered: Dict[str, Any] | None = None):
    """A REAL registry entry via the installed register_auxiliary_task
    validator — not a hand-built dict."""
    build = pytest.importorskip("hermes_cli.plugins_aux_tasks").build_auxiliary_task_entry
    return build(owner, owner, key, display_name=label, description=description,
                 defaults=defaults or {}, inherit_from=base,
                 registered=registered or {})


# ---------------------------------------------------------------------------
# 1: complete built-in aux inventory from the installed defaults


def test_builtin_aux_inventory_matches_installed_defaults():
    got = {s for s in _inventory_slot_ids({}) if s.startswith("auxiliary.")}
    # auxiliary.openrouter_model is the cross-cutting scalar override, covered
    # by its own contract tests below.
    got_selectors = got - {"auxiliary.openrouter_model"}
    expected = _expected_builtin_aux_slot_ids()
    assert got_selectors == expected, (
        f"aux selector set diverges from installed DEFAULT_CONFIG: "
        f"missing={sorted(expected - got_selectors)} "
        f"extra={sorted(got_selectors - expected)}")
    # known tasks are inside the derived set (order-independent sanity)
    for key in ("vision", "compression", "review", "curator", "monitor",
                "background_review", "moa_reference", "moa_aggregator",
                "memory_query_rewrite", "tts_audio_tags"):
        assert f"auxiliary.{key}" in got


def test_aux_policy_scalars_are_never_selectors():
    aux = _aux_defaults()
    policies = {k for k, v in aux.items() if not isinstance(v, dict)}
    assert {"transient_retries", "free_only", "stream_only_base_urls",
            "openrouter_model"} <= policies, "precondition: installed defaults"
    ids = _inventory_slot_ids({})
    # openrouter_model is its own cross-cutting slot (separate tests); the
    # remaining policy scalars never become slots.
    assert not {f"auxiliary.{k}" for k in policies - {"openrouter_model"}} & ids


def test_openrouter_model_scalar_is_a_cross_cutting_slot():
    """The OpenRouter fallback override lives at a SCALAR config key and is read
    by the runtime for ALL aux auto-chain tasks (agent/auxiliary_client).
    It must stay outside the per-task selector set yet remain editable as one
    bare-string slot."""
    ids = _inventory_slot_ids({})
    assert "auxiliary.openrouter_model" in ids
    obs = _slot_obs({}, "auxiliary.openrouter_model")
    d = obs.descriptor
    assert d.storage_path == "auxiliary.openrouter_model"
    assert d.writable is True and d.protected is False
    # unset: no invented route — the runtime keeps its built-in fallback model
    assert obs.configured_present is False
    assert obs.effective in ("", None)
    note = d.notes.lower()
    assert "cross-cutting" in note or "all auxiliary" in note


def test_openrouter_model_edit_stores_bare_model_string():
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "auxiliary.openrouter_model", {"model": "nvidia/nemotron-3-ultra-550b-a55b:free"})])
    assert prev.clean, prev.rejected
    chg = next(c for c in prev.changes if c.storage_path == "auxiliary.openrouter_model")
    assert chg.after == "nvidia/nemotron-3-ultra-550b-a55b:free"  # bare string, never dict


def test_openrouter_model_edit_with_provider_is_rejected():
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "auxiliary.openrouter_model", {"provider": "openrouter", "model": "x/y"})])
    assert not prev.clean
    assert prev.changes == ()


def test_openrouter_model_runtime_reader_parities_the_scalar(hermes_home):
    """End-to-end: the runtime reader (_aux_openrouter_settings) resolves the
    SAME scalar key from an isolated home config — the slot's storage contract
    and the runtime consumer agree."""
    aux_client = pytest.importorskip("agent.auxiliary_client")
    hermes_home.path.joinpath("config.yaml").write_text(
        "model:\n  provider: openai\n  default: m\n"
        "auxiliary:\n  openrouter_model: nvidia/nemotron-3-ultra-550b-a55b:free\n")
    free_only, model = aux_client._aux_openrouter_settings()
    assert free_only is False
    assert model == "nvidia/nemotron-3-ultra-550b-a55b:free"

    # ...and a planned edit for the slot lands on that exact scalar key
    prev = ri.preview_changes({"auxiliary": {"openrouter_model": "nvidia/x:free"}},
                              [ri.RouteEdit("auxiliary.openrouter_model",
                                            {"model": "nvidia/y:free"})])
    assert prev.clean, prev.rejected
    assert next(c for c in prev.changes).after == "nvidia/y:free"


# ---------------------------------------------------------------------------
# 2: judge ownership preserved


def test_goal_judge_remains_judge_owned_single_row():
    ids = _inventory_slot_ids({})
    assert "auxiliary.goal_judge" not in ids
    obs = _slot_obs({}, "judge.goal_judge.route")
    assert obs.descriptor.protocol == ri.PROTOCOL_TYPED_DECISION
    rows = [o for o in ri.build_inventory({})
            if o.descriptor.storage_path == "auxiliary.goal_judge"]
    assert [o.descriptor.slot_id for o in rows] == ["judge.goal_judge.route"]


def test_aux_review_typed_protocol_unchanged():
    obs = _aux_obs({}, "review")
    assert obs.descriptor.protocol == ri.PROTOCOL_TYPED_DECISION
    plain = ri.preview_changes({}, [ri.RouteEdit(
        "auxiliary.review", {"provider": "openai", "model": "gpt-4o-mini"})])
    assert not plain.clean
    # and chat-model aux tasks keep speaking chat
    for key in ("compression", "vision", "mcp"):
        assert _aux_obs({}, key).descriptor.protocol == ri.PROTOCOL_CHAT


# ---------------------------------------------------------------------------
# 3: obsolete aux blocks are ignored by the runtime -> not editable


def test_obsolete_aux_blocks_yield_no_slot_and_reject_edits():
    cfg = {"auxiliary": {
        "session_search": {"provider": "auto", "model": "ghost"},
        "web_extract": {"provider": "auto", "model": "ghost"},
        "memory_query_rewrite": {"provider": "auto", "model": ""},
    }}
    ids = _inventory_slot_ids(cfg)
    assert "auxiliary.session_search" not in ids
    assert "auxiliary.web_extract" not in ids
    assert "auxiliary.memory_query_rewrite" in ids  # real task still present
    for ghost in ("auxiliary.session_search", "auxiliary.web_extract"):
        prev = ri.preview_changes(cfg, [ri.RouteEdit(
            ghost, {"provider": "auto", "model": "anything"})])
        assert not prev.clean
        assert "unknown slot" in " ".join(prev.rejected)


# ---------------------------------------------------------------------------
# 4: plugin-registered aux tasks through the REAL registry API


def test_plugin_aux_task_from_real_registry_api_becomes_a_slot():
    entry = _plugin_entry("usability_demo")
    ids = _inventory_slot_ids({}, [entry])
    assert "auxiliary.usability_demo" in ids
    obs = _aux_obs({}, "usability_demo", [entry])
    d = obs.descriptor
    assert d.writable is True and d.protected is False
    meta = dict(d.metadata or {})
    assert meta.get("label") == "My task"
    assert meta.get("plugin") == "usability_plug"
    assert "inherit" in json.dumps(meta).lower()


def test_plugin_aux_task_without_discovery_is_absent():
    """Without a plugin discovery payload there is no plugin aux slot — never a
    silently invented row."""
    ids = _inventory_slot_ids({})
    assert "auxiliary.usability_demo" not in ids


def test_plugin_aux_task_supported_inheritance_follows_base():
    cfg = {"auxiliary": {
        "compression": {"provider": "somecloud", "model": "cloud-y"},
        "usability_demo": {},
    }}
    entry = _plugin_entry("usability_demo", base="compression")
    assert entry["inherit_from"] == "compression", "real builder must keep the inheritance"
    obs = _aux_obs(cfg, "usability_demo", [entry])
    # unpinned: the task speaks the base task's route
    assert obs.effective["provider"] == "somecloud"
    assert obs.effective["model"] == "cloud-y"


def test_plugin_aux_task_pin_overrides_base_and_never_leaks_base_url():
    cfg = {"auxiliary": {
        "compression": {"provider": "somecloud", "model": "cloud-y",
                        "base_url": "https://somecloud.internal/v1",
                        "api_key": "SECRET-CLOUD"},
        "usability_demo": {"provider": "nous", "model": "own-model"},
    }}
    entry = _plugin_entry("usability_demo", base="compression")
    obs = _aux_obs(cfg, "usability_demo", [entry])
    eff = obs.effective
    assert eff["provider"] == "nous" and eff["model"] == "own-model"
    # a pinned route must travel as ONE unit: the base's base_url/api_key never
    # rides along (agent.auxiliary_task_config._layer_over_inherited semantics)
    assert not eff.get("base_url")
    assert eff.get("api_key") != "SECRET-CLOUD"


def test_plugin_aux_task_inheriting_registered_plugin_base():
    build = pytest.importorskip("hermes_cli.plugins_aux_tasks").build_auxiliary_task_entry
    base = build("usability_plug", "usability_plug", "usability_base",
                 display_name="Base", description="base task", defaults={},
                 inherit_from=None, registered={})
    entry = build("usability_plug", "usability_plug", "usability_child",
                  display_name="Child", description="child task", defaults={},
                  inherit_from="usability_base", registered={"usability_base": base})
    assert entry["inherit_from"] == "usability_base"
    cfg = {"auxiliary": {"usability_base": {"provider": "nous", "model": "base-y"}}}
    obs = _aux_obs(cfg, "usability_child", [base, entry])
    assert obs.effective["provider"] == "nous"
    assert obs.effective["model"] == "base-y"


def test_plugin_aux_task_unsupported_inherit_from_is_dropped():
    """A base that is neither a built-in nor a registered task never invents
    inheritance: the REAL builder logs it away (inherit_from=None) and the
    slot keeps its own declared defaults."""
    entry = _plugin_entry("usability_demo", base="nonexistent_base")
    assert entry["inherit_from"] is None
    obs = _aux_obs({}, "usability_demo", [entry])
    assert obs.effective.get("provider") == "auto"
    assert obs.effective.get("model") == ""


def test_plugin_aux_task_cannot_shadow_goal_judge():
    """The judge path stays judge-owned even defensively, in case a future
    registry lets a plugin register the reserved key."""
    build = pytest.importorskip("hermes_cli.plugins_aux_tasks").build_auxiliary_task_entry
    reserved_ok = True
    try:
        build("usability_plug", "usability_plug", "goal_judge",
              display_name="x", description="d", defaults={},
              inherit_from=None, registered={})
    except ValueError:
        reserved_ok = False
    entry = {"key": "goal_judge", "display_name": "x", "description": "d",
             "defaults": {}, "inherit_from": None,
             "plugin": "usability_plug", "plugin_key": "usability_plug"}
    ids = _inventory_slot_ids({}, [entry])
    assert "auxiliary.goal_judge" not in ids or not reserved_ok


def test_plugin_aux_task_edit_previews_and_stores_route_dict():
    entry = _plugin_entry("usability_demo")
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "auxiliary.usability_demo", {"provider": "nous", "model": "own"})],
        plugin_aux_entries=[entry])
    assert prev.clean, prev.rejected
    chg = next(c for c in prev.changes if c.storage_path == "auxiliary.usability_demo")
    assert chg.after["provider"] == "nous" and chg.after["model"] == "own"


# ---------------------------------------------------------------------------
# 5: real defaults parity for aux effective routes


def test_aux_effective_route_parities_installed_resolver(hermes_home):
    aux_cfg = pytest.importorskip("agent.auxiliary_task_config")
    hermes_home.path.joinpath("config.yaml").write_text(
        "model:\n  provider: openai\n  default: m\n"
        "auxiliary:\n"
        "  compression:\n    provider: somecloud\n    model: cloud-y\n    timeout: 300\n"
        "  vision:\n    model: vision-x\n"
        "  openrouter_model: nvidia/x:free\n")
    from hermes_cli.config import load_config_readonly
    live = load_config_readonly()
    for key in ("compression", "vision"):
        runtime = aux_cfg._get_auxiliary_task_config(key)
        ours = ri._aux_effective_route(live, key, _entries_by_key([]))
        for field in ("provider", "model", "timeout"):
            assert ours[field] == runtime[field], (key, field, ours, runtime)


def test_aux_unset_row_reports_the_runtime_default_route():
    """Parity with `hermes model`/web UI: an unset aux row speaks provider
    'auto' with no model — never a bare empty route."""
    obs = _aux_obs({}, "compression")
    assert obs.effective["provider"] == "auto"
    assert obs.effective["model"] == ""
    assert obs.source == "default"


# ---------------------------------------------------------------------------
# aux edits keep their dict neighbors (preview/edit correctness)


def test_aux_edit_preserves_neighbor_fields_and_full_defaults():
    cfg = {"auxiliary": {"vision": {"provider": "auto", "model": "",
                                    "download_timeout": 45, "timeout": 90}}}
    prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "auxiliary.vision", {"provider": "nous", "model": "vision-y"})])
    assert prev.clean, prev.rejected
    after = next(c for c in prev.changes if c.storage_path == "auxiliary.vision").after
    assert after["provider"] == "nous" and after["model"] == "vision-y"
    assert after["download_timeout"] == 45   # neighbor preserved
    assert after["timeout"] == 90            # neighbor preserved


def test_vision_slot_note_is_truthful_about_side_effect_key():
    notes = _aux_obs({}, "vision").descriptor.notes
    assert "download_timeout" in notes  # vision honors auxiliary.vision.download_timeout


def test_editing_unset_aux_slot_lands_on_the_runtime_path():
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "auxiliary.curator", {"provider": "nous", "model": "cheap-c"})])
    assert prev.clean, prev.rejected
    after = next(c for c in prev.changes).after
    # the stored block stays minimal (operator intent only — never a copy of
    # runtime defaults); the default timeout keeps resolving at READ time
    assert after == {"provider": "nous", "model": "cheap-c"}
    import copy as _copy
    merged_doc = _copy.deepcopy({"auxiliary": {"curator": after}})
    assert ri._aux_effective_route(merged_doc, "curator", {})["timeout"] == 600


# ---------------------------------------------------------------------------
# 6: speech defaults + supported backend paths


def test_speech_unset_defaults_resolve_local_stt_and_edge_tts():
    transcription_common = pytest.importorskip("tools.transcription_common")
    assert transcription_common.DEFAULT_PROVIDER == "local"
    tts_tool = pytest.importorskip("tools.tts_tool")
    assert tts_tool.DEFAULT_PROVIDER == "edge"
    assert ri._speech_default_backend({}, "stt") == "local"
    assert ri._speech_default_backend({}, "tts") == "edge"
    # a stored pick wins over the default
    assert ri._speech_default_backend(
        {"stt": {"groq": {"model": "x"}, "provider": "groq"}}, "stt") == "groq"


def test_speech_unset_resolves_to_supported_backend_paths():
    obs_stt = _slot_obs({}, "specialist.stt")
    # provider unset -> local backend with its real runtime model key
    assert obs_stt.descriptor.storage_path == "stt.local.model"
    assert obs_stt.descriptor.writable is True
    assert obs_stt.descriptor.runtime_consumed is True
    from tools.transcription_tools import _BUILTIN_MODEL_KEYS
    for provider, (section, key, _default, _empty) in _BUILTIN_MODEL_KEYS.items():
        if provider in ("local", "local_command"):
            continue  # local is the unset default row; local_command shares stt.local
        obs = _slot_obs({"stt": {"provider": provider}}, "specialist.stt")
        d = obs.descriptor
        assert d.storage_path == f"stt.{section}.{key}", (provider, d.storage_path)
        assert d.writable is True, provider


def test_speech_edit_uses_backend_specific_path():
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "specialist.stt", {"provider": "elevenlabs", "model": "scribe_v2"})])
    assert prev.clean, prev.rejected
    chg = next(c for c in prev.changes if c.storage_path == "stt.elevenlabs.model_id")
    assert chg.after == "scribe_v2"  # bare string, never a route dict
    companion = next(c for c in prev.changes if c.storage_path == "stt.provider")
    assert companion.after == "elevenlabs"


def test_speech_edit_with_new_backend_writes_provider_companion():
    cfg = {"stt": {"provider": "local", "local": {"model": "base"}}}
    prev = ri.preview_changes(cfg, [ri.RouteEdit(
        "specialist.stt", {"provider": "openai", "model": "whisper-1"})])
    assert prev.clean, prev.rejected
    paths = {
        c.storage_path: c.after
        for c in prev.changes
        if c.storage_path in ("stt.provider", "stt.openai.model")}
    assert paths == {"stt.provider": "openai", "stt.openai.model": "whisper-1"}


def test_speech_empty_provider_edit_does_not_write_explicit_empty_string():
    """provider "" means unset/unspecified — never a stored empty pick (the
    runtime treats an explicit empty provider key as a broken explicit pick)."""
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "specialist.stt", {"provider": "", "model": "turbo"})])
    assert prev.clean, prev.rejected
    chg = next(c for c in prev.changes if c.storage_path == "stt.local.model")
    assert chg.after == "turbo"
    assert not any(c.storage_path == "stt.provider" and c.after == "" for c in prev.changes)


def test_no_invented_edge_model_key_when_tts_unset():
    """tts.provider unset resolves to edge (free) — edge has NO model key, so
    the row is read-only and must NOT carry an invented tts.edge.model path."""
    obs = _slot_obs({}, "specialist.tts")
    d = obs.descriptor
    assert d.storage_path == "tts.edge"
    assert d.writable is False and d.runtime_consumed is False
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "specialist.tts", {"model": "anything"})])  # unset -> edge, no model key
    assert not prev.clean
    assert prev.changes == ()
    stg = {o.descriptor.storage_path for o in ri.build_inventory({})}
    assert "tts.edge.model" not in stg


def test_no_model_backends_are_read_only_without_invented_keys():
    """Every supported backend list entry that has no model config key in the
    installed defaults produces a read-only row — and none of them produces an
    invented <section>.<backend>.model path."""
    tts_tool = pytest.importorskip("tools.tts_tool")
    tts_mod = tts_tool
    assert tts_mod.DEFAULT_PROVIDER == "edge"
    stg = {o.descriptor.storage_path for o in ri.build_inventory({})}
    for backend in ("edge", "xai", "piper"):
        assert f"tts.{backend}.model" not in stg, backend
    obs = _slot_obs({"tts": {"provider": "piper"}}, "specialist.tts")
    assert obs.descriptor.writable is False
    assert obs.descriptor.storage_path == "tts.piper"


def test_stored_xai_tts_provider_is_voice_only_read_only():
    obs = _slot_obs({"tts": {"provider": "xai"}}, "specialist.tts")
    assert obs.descriptor.writable is False
    assert obs.descriptor.storage_path == "tts.xai"  # no invented tts.xai.model
    assert "xai" in obs.descriptor.notes
    prev = ri.preview_changes({"tts": {"provider": "xai"}}, [ri.RouteEdit(
        "specialist.tts", {"provider": "xai", "model": "nope"})])
    assert not prev.clean
