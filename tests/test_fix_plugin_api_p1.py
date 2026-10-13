"""P1 regression tests for dashboard/plugin_api.py (fresh Codex P1 round).

Six findings, all in dashboard/plugin_api.py, all handled RED → GREEN here:

  F1  presetId apply must run ReviewedPreset.preflight FIRST and block on any
      reason (missing credentials / cost change / new profiles) before any
      write — the preset's stored policy must actually gate one-click apply.
  F2  preset targets persist their real slot identity (cron/delegation/aux/
      specialist/plugin) and restore the ORIGINAL typed changes; they are
      never reconstructed as a main route read out of model.provider/default.
  F3  UI removal edits (action:"remove", to:null) bind as removal/unset
      operations per slot semantics — never normalized to a null/"None"
      route assignment.
  F4  main-route changes route through HermesAdapter.prepare_main_assignment
      (the canonical assignment seam used by the engine's canonical store
      adapter), so alias resolution / custom endpoints / persistence side
      effects match what /preview promises.
  F5  the journal DB lives in a private Hermes state dir OUTSIDE the plugin
      source tree, so the documented clone-into-plugins-dir install works
      (OperationJournal's worktree guard cannot fire in supported setups).
  F6  GET /operations/{id} (and a suitable restart path) runs
      engine.reconcile for pending/applying/partial operations and surfaces
      the crash-recovery contract's recovery_required state.

Everything here runs against throwaway /tmp Hermes homes (conftest).
No inference, no real provider credentials, no paths under ~/.hermes.
"""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from model_switcher.engine import plan_operation  # noqa: E402
from model_switcher.journal import (  # noqa: E402
    OperationJournal, PublicOperationView,
)
from model_switcher.presets import RouteTarget  # noqa: E402

import dashboard.plugin_api as plugin_api  # noqa: E402

API_PREFIX = ""


BASE_CONFIG = """\
model:
  provider: openai
  default: gpt-4o-mini
providers:
  lablocal:
    base_url: http://127.0.0.1:8080/v1
auxiliary:
  goal_judge:
    model: j/old
    threshold: 0.8
unrelated:
  keep_me: yes
"""


def _client():
    app = FastAPI()
    app.include_router(plugin_api.router, prefix=API_PREFIX)
    return TestClient(app)


def _m(cp, name, *, env=True, config=BASE_CONFIG):
    """Mint a profile (special-cased 'default' = home-root document)."""
    home = cp.path
    if name == "default":
        pdir = home
    else:
        pdir = home / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(config)
    if env:
        # lab-only presence marker for preflight; never a real provider key
        (pdir / ".env").write_text("FAKE_LAB_PROVIDER_KEY=lab-only\n")
    return pdir / "config.yaml"


def _load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def _read(path):
    with open(path) as fh:
        return fh.read()


# -- F1: preset preflight runs before any write ---------------------------


def test_auth_free_custom_endpoint_does_not_require_unrelated_env(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    # A declared local endpoint without an auth requirement needs no .env.
    _m(hermes_home, "delta", env=False)

    tc = _client()
    changes = [
        {"profileId": p, "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}
        for p in ("default", "alpha", "delta")
    ]
    r = tc.post("/presets", json={"name": "fleet", "changes": changes})
    assert r.status_code == 200, r.text
    pid = r.json()["preset"]["id"]

    before = {p: _read(_cfg_of(home, p)) for p in ("default", "alpha", "delta")}
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200, r.text
    assert r.json()["operation"]["state"] == "committed"
    after = {p: _read(_cfg_of(home, p)) for p in ("default", "alpha", "delta")}
    assert after != before
    assert _load(_cfg_of(home, "delta"))["model"]["default"] == "lab-7b-instruct"
    journal = OperationJournal(_journal_path(plugin_api))
    try:
        assert len(PublicOperationView(journal).list_operations()) == 1
    finally:
        journal.close()


def test_preset_apply_preflight_blocks_new_profile_and_cost_classes(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")


    tc = _client()
    changes = [
        {"profileId": p, "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}
        for p in ("default", "alpha")
    ]
    r = tc.post("/presets", json={"name": "fleet", "changes": changes})
    assert r.status_code == 200, r.text
    pid = r.json()["preset"]["id"]

    _m(hermes_home, "gamma")  # created after review, not just unselected
    before = {p: _read(_cfg_of(home, p)) for p in ("default", "alpha", "gamma")}
    # 1) a NEW profile never silently joins the apply's fleet
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 409, r.text
    reasons = r.json()["detail"]["reasons"]
    assert any("gamma" in reason and "new profile" in reason for reason in reasons), reasons
    assert {p: _read(_cfg_of(home, p)) for p in ("default", "alpha", "gamma")} == before

    # 2) a cost class unknown after review blocks (unknown is never free)
    records = _records(plugin_api)
    target = next(t for t in records[0]["targets"]
                  if t["profile"] == "default")
    target["cost_class"] = "unknown"
    _write_records(plugin_api, records)
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 409, r.text
    reasons = r.json()["detail"]["reasons"]
    assert any("unknown cost" in reason for reason in reasons), reasons
    assert {p: _read(_cfg_of(home, p)) for p in ("default", "alpha", "gamma")} == before


def test_preset_apply_allowed_when_preflight_clean_then_targets_written(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    changes = [
        {"profileId": p, "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}
        for p in ("default", "alpha")
    ]
    r = tc.post("/presets", json={"name": "fleet", "changes": changes})
    assert r.status_code == 200, r.text
    pid = r.json()["preset"]["id"]

    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200, r.text
    op = r.json()["operation"]
    assert op["state"] == "committed", op
    assert all(res["status"] == "applied" for res in op["results"]), op
    for p in ("default", "alpha"):
        doc = _load(_cfg_of(home, p))
        assert doc["model"]["provider"] == "lablocal"
        assert doc["model"]["default"] == "lab-7b-instruct"


# -- F2: slot identity is persisted and restored --------------------------


def test_preset_persists_slot_id_and_reapplies_non_main_target(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    cron_cfg = BASE_CONFIG.replace("  lablocal:\n", "  dispatchlab:\n    base_url: http://127.0.0.1:8081/v1\n  lablocal:\n") + "\ncron:\n  provider: cronctl\n  model: old-cron-model\n"
    _m(hermes_home, "alpha", config=cron_cfg)
    tc = _client()

    changes = [
        {"profileId": "alpha", "slotId": "cron.model", "action": "set",
         "to": {"provider": "dispatchlab", "model": "cron-9"}},
        {"profileId": "default", "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}},
    ]
    r = tc.post("/presets", json={"name": "mixed fleet", "changes": changes})
    assert r.status_code == 200, r.text
    pid = r.json()["preset"]["id"]

    # the persisted record carries the real slot identity
    rec = next(entry for entry in _records(plugin_api) if entry["preset_id"] == pid)
    assert rec["targets"][0]["slot_id"] == "cron.model", rec
    assert rec["ui_changes"][0]["slotId"] == "cron.model", rec

    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200, r.text
    op = r.json()["operation"]
    assert op["state"] == "committed", op

    alpha = _load(home / "profiles" / "alpha" / "config.yaml")
    # the CRON target was applied to the CRON slot with its own typed pair…
    assert alpha["cron"]["model"] == "cron-9"
    assert alpha["cron"]["model_provider"] == "dispatchlab"
    # …and the main route of that profile was NOT clobbered with a
    # reconstructed None/"None" route from model.provider/default
    assert alpha["model"]["provider"] == "openai"
    assert alpha["model"]["default"] == "gpt-4o-mini"
    assert alpha["cron"]["provider"] == "cronctl"  # scheduler untouched
    # meanwhile the main target went to the main slot
    home_doc = _load(home / "config.yaml")
    assert home_doc["model"]["provider"] == "lablocal"
    assert home_doc["model"]["default"] == "lab-7b-instruct"


def test_legacy_preset_record_restored_through_slot_identity(hermes_home):
    """A preset saved WITHOUT ui_changes still restores per-slot targets from
    the persisted slot identity — never rebuilt as a main route."""
    preset = plugin_api._preset_from_record({
        "preset_id": "p-legacy",
        "name": "legacy",
        "approved_classes": ["metered"],
        "allow_unknown": False,
        "reviewed_profile_ids": ["alpha"],
        "targets": [{
            "profile": "alpha", "store_kind": "canonical_config",
            "path": "/tmp/placeholder/config.yaml",
            "changes": {"cron.model": "cron-9", "cron.model_provider": "dispatchlab"},
            "cost_class": "metered",
            "slot_id": "cron",
        }],
    })
    ui = plugin_api._ui_changes_from_targets(preset)
    assert ui == [{"profileId": "alpha", "slotId": "cron.model", "action": "set",
                   "to": {"provider": "dispatchlab", "model": "cron-9"}}], ui


# -- F3: removal edits are unsets, never null/"None" routes ---------------


def test_removal_edit_previews_as_unset_not_route_assignment(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    cron_cfg = BASE_CONFIG + "\ncron:\n  provider: cronctl\n  model: old-model\n"
    _m(hermes_home, "alpha", config=cron_cfg)
    tc = _client()

    r = tc.post("/preview", json={"changes": [
        {"profileId": "alpha", "slotId": "main", "action": "remove",
         "from": {"provider": "openai", "model": "gpt-4o-mini"}, "to": None},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preview"]["blocked"] == [], body
    assert '"None"' not in json.dumps(body), body
    main_diff = body["preview"]["changes"][0]["diff"]
    assert {d["path"] for d in main_diff} == {"model.default", "model.provider"}
    assert all(d["to"] is None for d in main_diff)

    # cron removal unsets the inference pair, never the scheduler key
    r = tc.post("/preview", json={"changes": [
        {"profileId": "alpha", "slotId": "cron", "action": "remove",
         "from": {"provider": "judge-old", "model": "old-cron-model"}, "to": None},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preview"]["blocked"] == [], body
    assert '"None"' not in json.dumps(body), body
    cron_diff = body["preview"]["changes"][0]["diff"]
    assert {d["path"] for d in cron_diff} == {"cron.model", "cron.model_provider"}
    assert all(d["to"] is None for d in cron_diff)

    # chain slots keep their slot semantics: removal == inherit-unset (null),
    # never an explicitly-empty chain
    chain_cfg = cron_cfg + "\ndelegation:\n  fallback_providers:\n    - a\n    - b\n"
    _m(hermes_home, "delta", config=chain_cfg)
    r = tc.post("/preview", json={"changes": [
        {"profileId": "delta", "slotId": "delegation.fallback_chain", "action": "remove",
         "from": {"provider": "", "model": "a"}, "to": None},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preview"]["blocked"] == [], body
    chain_diff = body["preview"]["changes"][0]["diff"]
    assert {d["path"] for d in chain_diff} == {"delegation.fallback_providers"}
    assert chain_diff[0]["to"] is None and chain_diff[0]["from"] == ["a", "b"]


def test_removal_edit_binds_typed_unset_targets_not_none_strings(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    cron_cfg = BASE_CONFIG + "\ncron:\n  provider: cronctl\n  model: old-model\n"
    _m(hermes_home, "alpha", config=cron_cfg)
    profiles = plugin_api._profile_inventory()

    targets, blocked = plugin_api._changes_to_targets(
        [{"profileId": "alpha", "slotId": "cron", "action": "remove", "to": None}],
        profiles, strict=False)
    assert blocked == []
    assert len(targets) == 1
    assert targets[0].changes == {"cron.model": None, "cron.model_provider": None}
    assert targets[0].changes == {"cron.model": None, "cron.model_provider": None}
    assert all(isinstance(v, type(None)) for v in targets[0].changes.values())

    targets, blocked = plugin_api._changes_to_targets(
        [{"profileId": "alpha", "slotId": "main", "action": "remove", "to": None}],
        profiles, strict=False)
    assert blocked == []
    assert targets[0].changes == {"model.default": None, "model.provider": None}
    assert all(isinstance(v, type(None)) for v in targets[0].changes.values())

    # scalar slots that share the profile config: no "None" string further down
    # the pipe (real unsets are None-valued, never str "None")
    for target in targets:
        assert not any(isinstance(v, str) and v == "None"
                       for v in target.changes.values())


# -- F4: main-route changes go through canonical preparation --------------


def test_main_apply_routes_through_canonical_preparation(hermes_home, monkeypatch):
    home = hermes_home.path
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()

    import hermes_adapter as ha
    real_prepare = ha.HermesAdapter.prepare_main_assignment
    calls = []

    def spy(self, cfg, provider, model, base_url="", api_key=""):
        calls.append((provider, model))
        return real_prepare(self, cfg, provider, model, base_url=base_url, api_key=api_key)

    monkeypatch.setattr(ha.HermesAdapter, "prepare_main_assignment", spy)

    r = tc.post("/operations", json={"changes": [
        {"profileId": "alpha", "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    assert r.status_code == 200, r.text
    assert r.json()["operation"]["state"] == "committed", r.text
    assert ("lablocal", "lab-7b-instruct") in calls, calls

    # canonical assignment semantics on disk: aliases/custom endpoints are
    # honoured — the providers-row base_url side effect the /preview promises
    doc = _load(home / "profiles" / "alpha" / "config.yaml")
    assert doc["model"]["provider"] == "lablocal"
    assert doc["model"]["default"] == "lab-7b-instruct"
    assert doc["model"]["base_url"] == "http://127.0.0.1:8080/v1"
    assert doc["providers"]["lablocal"]["base_url"] == "http://127.0.0.1:8080/v1"
    assert doc["auxiliary"]["goal_judge"]["threshold"] == 0.8  # protected intact


def test_main_apply_invalid_canonical_route_blocks_before_writes(hermes_home):
    """An alias/credential-validated main switch that the canonical seam
    rejects must block the apply (no write, no journal row) — never fall back
    to a blind model.default/model.provider overwrite."""
    home = hermes_home.path
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()

    before = _read(home / "profiles" / "alpha" / "config.yaml")
    r = tc.post("/operations", json={"changes": [
        {"profileId": "alpha", "slotId": "main", "action": "set",
         "to": {"provider": "openai", "model": "lab-7b-instruct"}}]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["blocked"] is True, detail
    assert _read(home / "profiles" / "alpha" / "config.yaml") == before
    journal = OperationJournal(_journal_path(plugin_api))
    try:
        assert PublicOperationView(journal).list_operations() == []
    finally:
        journal.close()


# -- F5: journal DB lives outside the plugin source tree ------------------


def test_journal_state_dir_resolves_outside_plugin_install_worktree(hermes_home, monkeypatch):
    home = hermes_home.path
    # the documented install: the repository cloned INTO <home>/plugins/
    install = home / "plugins" / "hermes-model-switcher"
    (install / "dashboard").mkdir(parents=True)
    (install / ".git").write_text("gitdir: /nonexistent\n")
    monkeypatch.setattr(plugin_api, "_PKG_ROOT", install)

    d = plugin_api._state_dir()
    d_resolved = str(d.resolve())
    assert str(install.resolve()) not in d_resolved, d_resolved
    assert not d_resolved.startswith(str(install.resolve()))
    # ... and the journal must construct cleanly (worktree guard keeps silent
    # only because the DB lives outside the checkout)
    journal = plugin_api.OperationJournal(str(d / "operations.db"))
    journal.close()
    mode = stat.S_IMODE(d.stat().st_mode)
    assert mode & 0o077 == 0, oct(mode)  # private state dir


def test_state_dir_override_is_honoured(hermes_home, monkeypatch, tmp_path):
    monkeypatch.setenv(plugin_api._PLUGIN_STATE_ENV, str(tmp_path / "custom-state"))
    d = plugin_api._state_dir()
    assert d.resolve() == (tmp_path / "custom-state").resolve()
    assert stat.S_IMODE(d.stat().st_mode) & 0o077 == 0


# -- F6: crash reconciliation surfaces through the API --------------------


def _journal_path(plugin_module) -> str:
    return str(plugin_module._state_dir() / "operations.db")


def _crash_written_op(hermes_home, profile="alpha"):
    """Journal an op, then land the config write WITHOUT updating the
    journal — the crash window between write and journalled step update."""
    _m(hermes_home, "default")
    _m(hermes_home, profile)
    journal = OperationJournal(_journal_path(plugin_api))
    targets = [{
        "profile": profile, "store_kind": "canonical_config",
        "path": str(_cfg_of(hermes_home.path, profile)),
        "changes": {"model.provider": "lablocal", "model.default": "lab-7b-instruct"},
    }]
    op = plan_operation(journal, targets)
    from model_switcher.adapters import adapter_for
    adapter = adapter_for("canonical_config", targets[0]["path"], profile)
    adapter.apply_changes(targets[0]["changes"])
    return journal, op


def _cfg_of(home, profile):
    if profile in (None, "default"):
        return home / "config.yaml"
    return home / "profiles" / profile / "config.yaml"


def _preset_obj(plugin_module):
    from model_switcher.presets import PresetStore
    return PresetStore()


def _records(plugin_module):
    path = plugin_module._state_dir() / "presets.json"
    return json.loads(path.read_text())


def _write_records(plugin_module, records):
    path = plugin_module._state_dir() / "presets.json"
    path.write_text(json.dumps(records, indent=2))


def test_get_operation_reconciles_crash_window_to_committed(hermes_home):
    tc = _client()
    sidebar, op = _crash_written_op(hermes_home)
    try:
        assert op["state"] in ("pending", "applying")  # the ambiguated record
        r = tc.get(f"/operations/{op['op_id']}")
        assert r.status_code == 200, r.text
        body = r.json()["operation"]
        # reconcile detected the crash-write (bytes match the intended write)
        # and completed the operation — not an opaque read of a stale row
        assert body["state"] == "committed", body
        assert all(res["status"] == "applied" for res in body["results"]), body
        assert body["undoable"] is True, body
    finally:
        sidebar.close()


def test_get_operation_surfaces_recovery_required_after_external_edit(hermes_home):
    tc = _client()
    sidebar, op = _crash_written_op(hermes_home)
    try:
        # an external writer intervenes: the crash-write is no longer the
        # exact intended document -> recovery_required per the contract
        from model_switcher.adapters import adapter_for
        adapter = adapter_for("canonical_config",
                              str(_cfg_of(hermes_home.path, "alpha")), "alpha")
        adapter.apply_changes({"unrelated.keep_me": "externally-edited"})
        r = tc.get(f"/operations/{op['op_id']}")
        assert r.status_code == 200, r.text
        body = r.json()["operation"]
        assert body["state"] == "recovery_required", body
        assert body["undoable"] is False, body
    finally:
        sidebar.close()


def test_recover_endpoint_reconciles_unfinished_operation(hermes_home):
    tc = _client()
    sidebar, op = _crash_written_op(hermes_home)
    try:
        r = tc.post(f"/operations/{op['op_id']}/recover")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["operation"]["state"] == "committed", body
    finally:
        sidebar.close()


def test_restart_path_reconciles_unfinished_operations(hermes_home, monkeypatch):
    """The lazy restart path (first journal use after a restart) reconciles
    every unfinished operation in the state dir."""
    tc = _client()
    sidebar, op = _crash_written_op(hermes_home)
    try:
        monkeypatch.setattr(plugin_api, "_STARTUP_RECONCILED", False)
        plugin_api._journal()
        r = tc.get(f"/operations/{op['op_id']}")
        assert r.status_code == 200, r.text
        assert r.json()["operation"]["state"] == "committed", r.text
    finally:
        sidebar.close()


