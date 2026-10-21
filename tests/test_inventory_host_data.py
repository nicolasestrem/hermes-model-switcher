"""HOST-data contract for ``route_inventory`` (public-seams refactor, section B).

The plugin no longer reaches into private Hermes modules to discover
plugin-registered auxiliary tasks or to read the MoA preset catalog. Both come
from HOST payloads the dashboard UI fetches through the SDK:

* ``host_aux``  — the JSON body of ``GET /api/model/auxiliary?profile=<p>``:
  ``{"tasks": [{task, provider, model, base_url, reasoning_effort,
  local_endpoint, label?, hint?, plugin?, inherit_from?, effective?}],
  "main": {provider, model}}``. Rows carrying a non-empty ``plugin`` are
  plugin-registered tasks for THAT profile (the host resolves discovery and
  profile scoping inside the request's profile scope).
* ``host_moa`` — the JSON body of ``GET /api/model/moa?profile=<p>``, whose
  ``presets`` mapping is the authoritative catalog for ``moa.default_preset``.

Coverage locked here:
1. plugin aux rows come from the host payload, with label/hint/plugin/
   inherit_from mapped onto the descriptor metadata;
2. per-profile scoping: one profile's payload never yields another's rows;
3. ``host_aux is None`` hides plugin rows AND records the "not supplied"
   warning — never a false, truthful-looking "no plugins registered";
4. malformed payloads hide plugin rows with a "malformed" warning, and junk
   rows / unknown fields are ignored rather than invented into slots;
5. ``inherit_from`` chains (and cycles) still resolve/warn from host rows;
6. the host's ``effective`` route only ENRICHES display — stored values stay
   from the raw config;
7. MoA ``default_preset`` validation: host catalog first, raw-config fallback
   second, "runtime preset catalog unavailable" when neither exists;
8. the local inheritance mirror still matches the checked-out runtime helper
   (drift detection), and ``route_inventory.py`` imports no private Hermes
   module (the acceptance gate, asserted locally).
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict

import pytest

import route_inventory as ri

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _host_row(task: str, *, plugin: str | None = None, label: str | None = None,
              hint: str = "", inherit_from: Any = None,
              effective: Dict[str, str] | None = None,
              provider: str = "auto", model: str = "") -> Dict[str, Any]:
    """One row of a real ``GET /api/model/auxiliary`` body."""
    row: Dict[str, Any] = {
        "task": task, "provider": provider, "model": model, "base_url": "",
        "reasoning_effort": None, "local_endpoint": False,
    }
    if plugin is not None:
        row["plugin"] = plugin
        row["label"] = label if label is not None else task
        row["hint"] = hint
        row["inherit_from"] = inherit_from
    if effective is not None:
        row["effective"] = dict(effective)
    return row


def _host_aux(*rows: Dict[str, Any], builtins: bool = True) -> Dict[str, Any]:
    tasks = list(rows)
    if builtins:
        # A real payload always carries the built-in slots first; they are NOT
        # plugin tasks and must never become plugin rows.
        tasks.insert(0, {"task": "compression", "provider": "auto", "model": "",
                         "base_url": "", "reasoning_effort": None,
                         "local_endpoint": False})
    return {"tasks": tasks, "main": {"provider": "openai", "model": "gpt-4o-mini"}}


def _slot_ids(cfg: Dict[str, Any], *, host_aux: Any = None) -> set:
    return {o.descriptor.slot_id for o in ri.build_inventory(cfg, host_aux=host_aux)}


def _obs(cfg: Dict[str, Any], slot_id: str, *, host_aux: Any = None):
    return next(o for o in ri.build_inventory(cfg, host_aux=host_aux)
                if o.descriptor.slot_id == slot_id)


def _warnings(cfg: Dict[str, Any], *, host_aux: Any = None):
    return ri.build_inventory(cfg, host_aux=host_aux).warnings


# --------------------------------------------------------------------------- #
# 1: plugin rows come from the host payload
# --------------------------------------------------------------------------- #


def test_host_plugin_row_becomes_a_slot_with_metadata():
    host = _host_aux(_host_row("host_demo", plugin="host_plug",
                               label="Host task", hint="does a thing"))
    cfg: Dict[str, Any] = {}
    assert "auxiliary.host_demo" in _slot_ids(cfg, host_aux=host)
    obs = _obs(cfg, "auxiliary.host_demo", host_aux=host)
    desc = obs.descriptor
    assert desc.family is ri.SlotFamily.AUXILIARY
    assert desc.storage_path == "auxiliary.host_demo"
    assert desc.protocol == ri.PROTOCOL_CHAT
    assert desc.writable is True and desc.protected is False
    meta = dict(desc.metadata)
    assert meta["label"] == "Host task"
    assert meta["description"] == "does a thing"
    assert meta["plugin"] == "host_plug"


def test_builtin_rows_in_the_host_payload_never_become_plugin_rows():
    host = _host_aux(_host_row("host_demo", plugin="host_plug"))
    rows = [o for o in ri.build_inventory({}, host_aux=host)
            if o.descriptor.storage_path == "auxiliary.compression"]
    # exactly the built-in row derived from DEFAULT_CONFIG, one row only
    assert len(rows) == 1
    assert rows[0].descriptor.metadata == {}
    assert rows[0].descriptor.slot_id == "auxiliary.compression"


def test_host_plugin_row_is_editable_and_stores_a_route_dict():
    host = _host_aux(_host_row("host_demo", plugin="host_plug"))
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "auxiliary.host_demo", {"provider": "nous", "model": "own"})], host_aux=host)
    assert prev.clean, prev.rejected
    chg = next(c for c in prev.changes
               if c.storage_path == "auxiliary.host_demo")
    assert chg.after["provider"] == "nous" and chg.after["model"] == "own"


def test_unknown_host_fields_are_ignored():
    row = _host_row("host_demo", plugin="host_plug")
    row.update({"something_new": {"nested": True}, "bogus": [1, 2, 3]})
    host = _host_aux(row)
    obs = _obs({}, "auxiliary.host_demo", host_aux=host)
    assert obs.descriptor.metadata["plugin"] == "host_plug"


# --------------------------------------------------------------------------- #
# 2: per-profile scoping
# --------------------------------------------------------------------------- #


def test_plugin_rows_are_scoped_to_the_profiles_host_payload():
    profile_a = _host_aux(_host_row("alpha_task", plugin="plug_a"))
    profile_b = _host_aux(_host_row("beta_task", plugin="plug_b"))
    assert "auxiliary.alpha_task" in _slot_ids({}, host_aux=profile_a)
    assert "auxiliary.beta_task" not in _slot_ids({}, host_aux=profile_a)
    assert "auxiliary.beta_task" in _slot_ids({}, host_aux=profile_b)
    assert "auxiliary.alpha_task" not in _slot_ids({}, host_aux=profile_b)


def test_profile_payloads_do_not_leak_effective_routes_between_builds():
    cfg_a = {"auxiliary": {"shared_child": {}}}
    cfg_b = {"auxiliary": {"shared_child": {}}}
    host_a = _host_aux(
        _host_row("shared_base", plugin="plug_a", provider="alpha", model="a-1"),
        _host_row("shared_child", plugin="plug_b"))
    host_b = _host_aux(
        _host_row("shared_base", plugin="plug_a", provider="alpha", model="a-1"),
        _host_row("shared_child", plugin="plug_b", inherit_from="shared_base",
                  effective={"provider": "beta", "model": "b-1", "base_url": ""}))
    assert _obs(cfg_a, "auxiliary.shared_child", host_aux=host_a) \
        .effective.get("model") != "b-1"
    rhs = _obs(cfg_b, "auxiliary.shared_child", host_aux=host_b).effective
    assert rhs["provider"] == "beta" and rhs["model"] == "b-1"


# --------------------------------------------------------------------------- #
# 3: host_aux None -> hidden + warning (never a false "no plugins")
# --------------------------------------------------------------------------- #


def test_host_aux_none_hides_plugin_rows_and_warns():
    ids = _slot_ids({"auxiliary": {"ghost_task": {}}})
    assert "auxiliary.ghost_task" not in ids
    warnings = _warnings({})
    assert any("host auxiliary data was not supplied" in w for w in warnings), warnings
    assert any("plugin-registered auxiliary rows are hidden" in w for w in warnings)


def test_host_aux_none_is_not_reported_as_no_plugins_registered():
    warnings = _warnings({})
    blob = " ".join(warnings).lower()
    assert "no plugin" not in blob and "plugins registered" not in blob


def test_explicit_empty_task_list_is_truthful_absence_without_a_warning():
    """A host that really has no plugin tasks sends ``{"tasks": [...]}`` with
    no plugin row: that is absence, not a missing payload — no warning."""
    assert _warnings({}, host_aux=_host_aux()) == []
    assert "auxiliary.host_demo" not in _slot_ids({}, host_aux=_host_aux())


def test_warnings_are_per_build_not_a_process_global_sink():
    first = _warnings({})                       # no host data -> warns
    assert any("not supplied" in w for w in first)
    second = _warnings({}, host_aux=_host_aux())  # host data -> clean
    assert second == []


# --------------------------------------------------------------------------- #
# 4: malformed payloads
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", [
    "not-a-dict",
    ["tasks"],
    {},
    {"tasks": {}},
    {"tasks": None},
    {"tasks": [1, 2]},
    {"tasks": ["nope"]},
    {"tasks": [{"plugin": "plug", "label": "x"}]},        # no task key
    {"tasks": [{"task": "", "plugin": "plug"}]},          # empty task key
    {"tasks": [{"task": 7, "plugin": "plug"}]},           # non-string task
    {"tasks": [{"task": "t", "plugin": 42}]},             # non-string plugin owner
    {"main": {"provider": "openai"}},
])
def test_malformed_host_payload_hides_plugin_rows_with_a_warning(payload):
    result = ri.build_inventory({}, host_aux=payload)
    ids = {o.descriptor.slot_id for o in result}
    assert not any(sid.startswith("auxiliary.") and "demo" in sid for sid in ids)
    joined = " ".join(result.warnings)
    assert "malformed" in joined, result.warnings
    assert "plugin-registered auxiliary row" in joined, result.warnings
    assert "hidden" in joined, result.warnings
    assert "no plugin" not in joined.lower()


@pytest.mark.parametrize("plugin", [None, ""])
def test_rows_without_a_plugin_owner_are_not_plugin_slots(plugin):
    """Built-in rows carry no ``plugin`` owner (or an empty one): they are not
    plugin tasks, and that is ordinary payload shape — not a warning."""
    host = _host_aux({"task": "builtinish", "provider": "auto", "model": "",
                      "base_url": "", "plugin": plugin})
    result = ri.build_inventory({}, host_aux=host)
    assert "auxiliary.builtinish" not in {o.descriptor.slot_id for o in result}
    assert result.warnings == [], result.warnings


def test_malformed_host_payload_still_keeps_every_config_only_row():
    """Degrading to "no plugin rows" must never cost the config-only slots."""
    clean = _slot_ids({})
    malformed = _slot_ids({}, host_aux={"tasks": "boom"})
    assert clean == malformed


# --------------------------------------------------------------------------- #
# 5: inheritance from host rows (chain + cycle)
# --------------------------------------------------------------------------- #


def test_host_row_inherit_from_resolves_the_base_route():
    host = _host_aux(
        _host_row("host_base", plugin="plug", provider="auto", model=""),
        _host_row("host_child", plugin="plug", inherit_from="host_base"))
    cfg = {"auxiliary": {"host_base": {"provider": "somecloud", "model": "cloud-y"}}}
    obs = _obs(cfg, "auxiliary.host_child", host_aux=host)
    assert obs.effective["provider"] == "somecloud"
    assert obs.effective["model"] == "cloud-y"
    assert obs.source == "inherited"
    assert obs.configured_present is False   # stored value stays absent


def test_pinned_host_task_never_claims_inheritance_nor_leaks_base_url():
    host = _host_aux(
        _host_row("host_base", plugin="plug"),
        _host_row("host_child", plugin="plug", inherit_from="host_base"))
    cfg = {"auxiliary": {
        "host_base": {"provider": "somecloud", "model": "cloud-y",
                      "base_url": "https://somecloud.internal/v1",
                      "api_key": "SECRET-CLOUD"},
        "host_child": {"provider": "nous", "model": "own-model"}}}
    obs = _obs(cfg, "auxiliary.host_child", host_aux=host)
    assert obs.source == "configured"
    assert obs.effective["provider"] == "nous"
    assert not obs.effective.get("base_url")
    assert obs.effective.get("api_key") != "SECRET-CLOUD"


def test_host_inherit_cycle_warns_and_claims_no_inheritance():
    host = _host_aux(
        _host_row("host_a", plugin="plug", inherit_from="host_b"),
        _host_row("host_b", plugin="plug", inherit_from="host_a"))
    result = ri.build_inventory({}, host_aux=host)
    a = next(o for o in result if o.descriptor.slot_id == "auxiliary.host_a")
    assert a.source != "inherited"
    joined = " ".join(result.warnings).lower()
    assert "circular" in joined or "cycle" in joined, result.warnings


def test_unresolved_host_inherit_base_warns_without_inventing_a_route():
    host = _host_aux(_host_row("host_orphan", plugin="plug",
                               inherit_from="no_such_task"))
    result = ri.build_inventory({}, host_aux=host)
    obs = next(o for o in result if o.descriptor.slot_id == "auxiliary.host_orphan")
    assert obs.source != "inherited"
    assert any("no_such_task" in w for w in result.warnings), result.warnings


# --------------------------------------------------------------------------- #
# 6: host `effective` enriches display only
# --------------------------------------------------------------------------- #


def test_host_effective_route_enriches_the_display_of_an_inheriting_row():
    host = _host_aux(
        _host_row("host_base", plugin="plug"),
        _host_row("host_child", plugin="plug", inherit_from="host_base",
                  effective={"provider": "resolved-cloud", "model": "resolved-1",
                             "base_url": ""}))
    cfg: Dict[str, Any] = {}
    obs = _obs(cfg, "auxiliary.host_child", host_aux=host)
    assert obs.effective["provider"] == "resolved-cloud"
    assert obs.effective["model"] == "resolved-1"
    # stored values are untouched: nothing was configured, nothing invented
    assert obs.configured_present is False
    assert obs.configured is None


def test_host_effective_route_never_overrides_stored_configuration():
    host = _host_aux(_host_row(
        "host_demo", plugin="plug", inherit_from="host_base",
        effective={"provider": "resolved-cloud", "model": "resolved-1"}))
    cfg = {"auxiliary": {"host_demo": {"provider": "stored", "model": "stored-m"}}}
    obs = _obs(cfg, "auxiliary.host_demo", host_aux=host)
    assert obs.configured == {"provider": "stored", "model": "stored-m"}
    assert obs.source == "configured"


def test_host_effective_empty_fields_never_erase_a_resolved_value():
    host = _host_aux(
        _host_row("host_base", plugin="plug"),
        _host_row("host_child", plugin="plug", inherit_from="host_base",
                  effective={"provider": "", "model": "", "base_url": ""}))
    cfg = {"auxiliary": {"host_base": {"provider": "somecloud", "model": "cloud-y"}}}
    obs = _obs(cfg, "auxiliary.host_child", host_aux=host)
    assert obs.effective["provider"] == "somecloud"
    assert obs.effective["model"] == "cloud-y"


# --------------------------------------------------------------------------- #
# 7: MoA default_preset validation from host_moa / raw config / neither
# --------------------------------------------------------------------------- #


def _moa_edit(model: str):
    return ri.RouteEdit("moa.default_preset", {"model": model})


def test_moa_preset_validated_against_the_host_catalog():
    host_moa = {"default_preset": "host_pick",
                "presets": {"host_pick": {"aggregator": {"model": "m"}}}}
    cfg = {"moa": {"presets": {"raw_only": {}}, "default_preset": "raw_only"}}
    ok = ri.preview_changes(cfg, [_moa_edit("host_pick")], host_moa=host_moa)
    assert ok.clean, ok.rejected
    # the host catalog is authoritative: a raw-only name is unknown to it
    bad = ri.preview_changes(cfg, [_moa_edit("raw_only")], host_moa=host_moa)
    assert not bad.clean
    assert any("unknown preset" in r for r in bad.rejected), bad.rejected


def test_moa_preset_validated_against_the_raw_config_when_no_host_data():
    cfg = {"moa": {"presets": {"raw_pick": {"aggregator": {"model": "m"}}},
                   "default_preset": "other"}}
    ok = ri.preview_changes(cfg, [_moa_edit("raw_pick")])
    assert ok.clean, ok.rejected
    bad = ri.preview_changes(cfg, [_moa_edit("host_pick")])
    assert not bad.clean
    assert any("unknown preset" in r for r in bad.rejected), bad.rejected


def test_moa_preset_catalog_unavailable_without_host_or_raw_presets():
    for cfg in ({}, {"moa": {}}, {"moa": {"presets": []}},
                {"moa": {"presets": "nope"}}, {"moa": "nope"}):
        prev = ri.preview_changes(cfg, [_moa_edit("any")])
        assert not prev.clean, cfg
        assert any("runtime preset catalog unavailable" in r
                   for r in prev.rejected), (cfg, prev.rejected)


def test_malformed_host_moa_falls_back_to_the_raw_config_catalog():
    cfg = {"moa": {"presets": {"raw_pick": {"aggregator": {"model": "m"}}},
                   "default_preset": "raw_pick"}}
    for host_moa in ({"presets": []}, {"presets": "nope"}, {}, "nope", None, []):
        ok = ri.preview_changes(cfg, [_moa_edit("raw_pick")], host_moa=host_moa)
        assert ok.clean, (host_moa, ok.rejected)
    assert ri._moa_preset_names(cfg, {"presets": {"x": {}}}) == frozenset({"x"})


def test_moa_unknown_preset_edit_writes_nothing():
    cfg = {"moa": {"presets": {"p": {"aggregator": {"model": "m"}}},
                   "default_preset": "p"}}
    prev = ri.preview_changes(cfg, [_moa_edit("missing")])
    assert not prev.clean and prev.changes == ()


# --------------------------------------------------------------------------- #
# 8: mirror parity (drift detection) + no private Hermes imports
# --------------------------------------------------------------------------- #


_LAYERING_CASES = [
    # (inherited, user)
    ({}, {}),
    ({"provider": "base", "model": "base-m", "api_key": "K"}, {}),
    ({"provider": "base", "model": "base-m", "base_url": "https://b/v1", "api_key": "K"},
     {"provider": "auto", "model": "", "base_url": "", "api_key": ""}),
    ({"provider": "base", "model": "base-m", "base_url": "https://b/v1", "api_key": "K"},
     {"provider": "own", "model": "own-m"}),
    ({"provider": "base", "model": "base-m", "timeout": 60},
     {"timeout": 120, "reasoning_effort": False}),
    ({"provider": "base", "model": "base-m", "timeout": 60},
     {"provider": "AUTO", "model": "  ", "base_url": " "}),
    ({"provider": "base", "model": "base-m", "extra_body": {"a": 1}},
     {"extra_body": {}, "model": "pinned"}),
]


def test_local_mirror_matches_the_runtime_helper():
    """Drift detector: the local mirror replaced a private import, so it must
    stay byte-equivalent in behaviour to the checked-out runtime helper."""
    runtime = pytest.importorskip("agent.auxiliary_task_config")
    helper = getattr(runtime, "_layer_over_inherited", None)
    assert callable(helper), "runtime helper moved — re-check the mirror"
    for inherited, user in _LAYERING_CASES:
        assert ri._mirror_layer_route_over_inherited(dict(inherited), dict(user)) \
            == helper(dict(inherited), dict(user)), (inherited, user)


def test_layer_route_over_inherited_uses_the_mirror_without_a_runtime_import():
    """The production path must not depend on the private runtime helper."""
    source = (REPO_ROOT / "route_inventory.py").read_text(encoding="utf-8")
    assert "from agent.auxiliary_task_config import" not in source
    assert "import agent.auxiliary_task_config" not in source
    for inherited, user in _LAYERING_CASES:
        assert ri._layer_route_over_inherited(dict(inherited), dict(user)) \
            == ri._mirror_layer_route_over_inherited(dict(inherited), dict(user))


def test_route_inventory_imports_no_private_hermes_module():
    """Local run of the acceptance gate for the module this worker owns."""
    source = (REPO_ROOT / "route_inventory.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    private = ("hermes_cli.config_defaults", "agent.auxiliary_task_config",
               "hermes_cli.moa_config", "tools.transcription_common",
               "hermes_cli.plugins", "hermes_cli.inventory", "agent.auxiliary_client")
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported
                if name.startswith(private)}, sorted(imported)

    gate = pytest.importorskip("hermes_cli.plugin_validate_private_imports")
    findings = [h for h in gate.private_import_findings(REPO_ROOT)
                if h.startswith("route_inventory.py")]
    assert findings == []
