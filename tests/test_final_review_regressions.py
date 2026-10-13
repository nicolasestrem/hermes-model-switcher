"""Final independent-review regressions, isolated data only."""
import inspect
from typing import Any, Dict
import json
import pytest
import yaml

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import plugin_api as api
from model_switcher.presets import ReviewedPreset, RouteTarget, BillingPolicy
from hermes_adapter import discover_profiles
import route_inventory as ri

ROUTE_CONFIG = "model:\n  provider: openai\n  default: gpt-4o-mini\nproviders:\n  lablocal:\n    base_url: http://127.0.0.1:8080/v1\n"


def _client():
    app = FastAPI()
    app.include_router(api.router, prefix="")
    return TestClient(app)


def _profile(cp, name, config=ROUTE_CONFIG, env=False):
    home = cp.path
    pdir = home if name == "default" else home / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(config)
    if env:
        (pdir / ".env").write_text("FAKE_LAB_PROVIDER_KEY=lab-only\n")
    return pdir / "config.yaml"


def test_referenced_providers_reads_route_dict_values():
    """Route-dict slot values carry their provider INSIDE the slot object."""
    assert api._referenced_providers(
        {"auxiliary.compression": {"provider": "somecloud", "model": "x"}}) == ["somecloud"]
    # Provider-suffixed paired paths and chain hops keep working.
    assert api._referenced_providers(
        {"cron.model": "m", "cron.model_provider": "dispatch"}) == ["dispatch"]
    assert api._referenced_providers(
        {"delegation.fallback_providers": [
            {"provider": "nous", "model": "m1"}, "opencode-go/m2"]}) == ["nous", "opencode-go"]


def test_preview_based_apply_runs_the_provider_credential_gate(hermes_home):
    """A cached preview is gated exactly like a direct apply: no credential,
    no write — never a preview that outlives its credential."""
    home = hermes_home.path
    cfg = _profile(hermes_home, "default")
    tc = _client()
    change = {"profileId": "default", "slotId": "auxiliary.compression",
              "action": "set", "to": {"provider": "somecloud", "model": "x"}}
    r = tc.post("/preview", json={"changes": [change]})
    assert r.status_code == 200, r.text
    preview_id = r.json()["previewId"]
    before = cfg.read_text()

    for payload in ({"previewId": preview_id}, {"changes": [change]}):
        r = tc.post("/operations", json=payload)
        assert r.status_code == 409, (payload, r.text)
        detail = r.json()["detail"]
        assert detail["blocked"] is True
        assert "missing provider credentials" in detail["error"]
    assert cfg.read_text() == before


def test_blocked_preview_cannot_be_partially_applied(hermes_home):
    """All-or-nothing: a preview that contained blocked changes is refused on
    apply — its valid subset is never written silently."""
    home = hermes_home.path
    cfg = _profile(hermes_home, "default")
    tc = _client()
    changes = [
        {"profileId": "default", "slotId": "auxiliary.compression", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}},
        {"profileId": "default", "slotId": "specialist.embedding", "action": "set",
         "to": {"provider": "openai", "model": "text-embedding-3-small"}},
    ]
    r = tc.post("/preview", json={"changes": changes})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preview"]["blocked"], body
    assert body["preview"]["changes"], "the valid subset still previews"
    before = cfg.read_text()

    r = tc.post("/operations", json={"previewId": body["previewId"]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["blocked"] is True
    assert "contained blocked changes" in detail["error"]
    assert any("read-only" in reason for reason in detail["reasons"]), detail
    assert cfg.read_text() == before


def test_route_handlers_are_sync_so_blocking_work_leaves_the_event_loop():
    """Every route handler does blocking work (config I/O, canonical saves,
    live provider probes). They must be plain defs — FastAPI then runs them in
    its threadpool instead of stalling the dashboard event loop."""
    for route in api.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            assert not inspect.iscoroutinefunction(endpoint), \
                f"{getattr(route, 'path', '?')} handler is async"


def test_preview_preparation_race_is_refused_not_adopted(hermes_home, monkeypatch):
    """An external write landing between preparation and bind must abort the
    preview — never be adopted as the mutation's expected revision."""
    cfg = _profile(hermes_home, "default")
    tc = _client()
    real = api._changes_to_targets

    def raced(changes, profiles, strict=True):
        targets, blocked = real(changes, profiles, strict=strict)
        with open(cfg, "a") as fh:
            fh.write("\nexternal:\n  marker: true\n")  # external writer
        return targets, blocked

    monkeypatch.setattr(api, "_changes_to_targets", raced)
    r = tc.post("/preview", json={"changes": [
        {"profileId": "default", "slotId": "auxiliary.compression",
         "action": "set", "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["preview"]["changes"], body
    assert body["preview"]["blocked"], body
    assert "changed while the preview was prepared" in body["preview"]["blocked"][0]["reason"]

    r = tc.post("/operations", json={"previewId": body["previewId"]})
    assert r.status_code == 409, r.text
    assert "contained blocked changes" in r.json()["detail"]["error"]


def test_scalar_removal_validates_reviewed_from_identity(hermes_home):
    """An exact-model removal whose reviewed `from` no longer matches the
    stored scalar route blocks; a matching one applies."""
    cfg = _profile(hermes_home, "default", config=(
        ROUTE_CONFIG + "delegation:\n  provider: dele\n  model: del-model\n"))
    tc = _client()
    stale = {"profileId": "default", "slotId": "delegation.model",
             "action": "remove", "from": {"provider": "someoneelse",
                                          "model": "del-model"}}
    r = tc.post("/preview", json={"changes": [stale]})
    assert r.status_code == 200, r.text
    blocked = r.json()["preview"]["blocked"]
    assert blocked, "a stale reviewed identity must block the removal"
    assert "changed since review" in blocked[0]["reason"]

    before = cfg.read_text()
    fresh = {**stale, "from": {"provider": "dele", "model": "del-model"}}
    r = tc.post("/preview", json={"changes": [fresh]})
    body = r.json()
    assert not body["preview"]["blocked"], body
    r = tc.post("/operations", json={"previewId": body["previewId"]})
    assert r.status_code == 200 and r.json()["operation"]["state"] == "committed", r.text
    assert cfg.read_text() != before
    doc = yaml.safe_load(cfg.read_text())
    assert (doc.get("delegation") or {}).get("model") is None


def test_stale_preset_removal_is_refused_on_apply(hermes_home):
    """Preset applies re-prepare their reviewed intents against the CURRENT
    document: a removal whose route drifted since review is refused, never
    replayed from frozen mutations."""
    cfg = _profile(hermes_home, "default", config=(
        ROUTE_CONFIG + "delegation:\n  provider: dele\n  model: del-model\n"))
    tc = _client()
    removal = {"profileId": "default", "slotId": "delegation.model",
               "action": "remove", "from": {"provider": "dele",
                                            "model": "del-model"}}
    r = tc.post("/presets", json={"name": "strip deleg", "changes": [removal]})
    assert r.status_code == 200, r.text
    pid = r.json()["preset"]["id"]

    # external drift: the route changed after the preset was reviewed
    cfg.write_text(cfg.read_text().replace("del-model", "new-model"))
    before = cfg.read_text()
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "no longer applies cleanly" in detail["error"]
    assert any("changed since review" in reason for reason in detail["reasons"])
    assert cfg.read_text() == before, "no deletion may fire on a drifted route"

    # a matching reviewed removal still applies
    cfg.write_text(before.replace("new-model", "del-model"))
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200 and r.json()["operation"]["state"] == "committed", r.text
    doc = yaml.safe_load(cfg.read_text())
    assert (doc.get("delegation") or {}).get("model") is None


def test_same_provider_model_edit_is_credential_gated(hermes_home):
    """A model-only edit omits the unchanged provider field; the gate must
    still resolve it from the current route — not skip."""
    _profile(hermes_home, "default", config=(
        ROUTE_CONFIG + "delegation:\n  provider: somecloud\n  model: m1\n"))
    tc = _client()
    change = {"profileId": "default", "slotId": "delegation.model",
              "action": "set", "to": {"model": "m2"}}
    r = tc.post("/preview", json={"changes": [change]})
    assert r.status_code == 200 and not r.json()["preview"]["blocked"], r.text
    preview_id = r.json()["previewId"]
    for payload in ({"changes": [change]}, {"previewId": preview_id}):
        r = tc.post("/operations", json=payload)
        assert r.status_code == 409, (payload, r.text)
        assert "missing provider credentials" in r.json()["detail"]["error"]


def test_chain_entries_are_shape_validated():
    """Malformed chain entries (partial objects, scalars, empty ids) are
    rejected before anything is persisted."""
    for bad in ([{"provider": "nous"}], [42], ["just-a-word"], ["nous"],
                [{"provider": "nous", "model": "  "}], [["nested"]]):
        p = ri.preview_changes({}, [ri.RouteEdit("fallback.main_chain",
                                                 {"chain": bad})])
        assert not p.clean, bad
        assert "chain entry" in p.rejected[0], p.rejected
    ok = ri.preview_changes({}, [ri.RouteEdit("fallback.main_chain", {
        "chain": [{"provider": "nous", "model": "hermes-4"},
                  {"provider": "opencode-go", "model": "glm-5.3-flash"}]})])
    assert ok.clean, ok.rejected


def test_direct_apply_preparation_race_is_refused(hermes_home, monkeypatch):
    """Direct (no-preview) applies are revision-pinned too: an external write
    during preparation blocks instead of being written onto."""
    cfg = _profile(hermes_home, "default")
    tc = _client()
    real = api._changes_to_targets

    def raced(changes, profiles, strict=True):
        targets, blocked = real(changes, profiles, strict=strict)
        with open(cfg, "a") as fh:
            fh.write("\nexternal:\n  marker: true\n")
        return targets, blocked

    monkeypatch.setattr(api, "_changes_to_targets", raced)
    r = tc.post("/operations", json={"changes": [
        {"profileId": "default", "slotId": "auxiliary.compression",
         "action": "set", "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    assert r.status_code == 409, r.text
    assert "changed while it was prepared" in r.json()["detail"]["error"]


def test_undo_restore_detects_interleaved_writer(hermes_home, monkeypatch):
    """A writer landing between the restore and its verification turns undo
    into a conflict (recovery_required), never a blind success."""
    home = hermes_home.path
    (home / "config.yaml").write_text("model:\n  provider: openai\n  default: m1\n")
    import hermes_adapter as ha
    from model_switcher.adapters import adapter_for, StoreConflict

    adapter = adapter_for("canonical_config", str(home / "config.yaml"), "default")
    snapshot = adapter.read()
    real_save = ha.HermesAdapter.apply_canonical_save

    def racing(self, profile, data, **kwargs):
        result = real_save(self, profile, data, **kwargs)
        with open(home / "config.yaml", "a") as fh:
            fh.write("interloper:\n  marker: true\n")
        return result

    monkeypatch.setattr(ha.HermesAdapter, "apply_canonical_save", racing)
    with pytest.raises(StoreConflict, match="changed during restore"):
        adapter.compensate(snapshot, adapter.revision())


def test_typed_decision_markers_reach_validation_but_are_never_stored(hermes_home):
    """A judge edit carrying typed-decision markers passes the protocol check;
    the markers are VALIDATION-only and never land in the stored route."""
    _profile(hermes_home, "default", config=(
        ROUTE_CONFIG + "auxiliary:\n  goal_judge:\n    model: old\n    threshold: 0.8\n"))
    tc = _client()
    change = {"profileId": "default", "slotId": "judge.goal_judge.route",
              "action": "set", "to": {"provider": "typed-decision-lab",
                                      "model": "judge-1",
                                      "labels": ["pass", "fail"]}}
    r = tc.post("/preview", json={"changes": [change]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["preview"]["blocked"], body
    preview = ri.preview_changes(
        {"auxiliary": {"goal_judge": {"model": "old", "threshold": 0.8}}},
        [ri.RouteEdit("judge.goal_judge.route", change["to"])])
    after = next(c for c in preview.changes
                 if c.storage_path == "auxiliary.goal_judge").after
    assert after.get("model") == "judge-1"
    assert "labels" not in after and "threshold" not in change["to"]


def test_duplicate_slot_changes_rejected_everywhere(hermes_home):
    """A set and an exact-removal for the SAME slot are refused on every entry
    path — the engine would coalesce them and silently drop one."""
    _profile(hermes_home, "default")
    tc = _client()
    duplicates = [
        {"profileId": "default", "slotId": "auxiliary.compression",
         "action": "set", "to": {"provider": "lablocal", "model": "new"}},
        {"profileId": "default", "slotId": "auxiliary.compression",
         "action": "remove", "to": None,
         "from": {"provider": "lablocal", "model": "old"}},
    ]
    r = tc.post("/preview", json={"changes": duplicates})
    assert r.status_code == 200, r.text
    body = r.json()["preview"]
    assert body["changes"] == [] and body["blocked"], body
    assert "duplicate change" in body["blocked"][0]["reason"]

    r = tc.post("/operations", json={"changes": duplicates})
    assert r.status_code == 422, r.text
    assert any("duplicate change" in str(b.get("reason"))
               for b in r.json()["detail"]["blocked"])

    r = tc.post("/presets", json={"name": "dup", "changes": duplicates})
    assert r.status_code == 422, r.text


def test_delegation_inherited_provider_is_credential_gated(hermes_home):
    """delegation.model with NO delegation.provider inherits model.provider;
    a model-only edit through an unauthenticated parent must be refused."""
    _profile(hermes_home, "default", config=(
        "model:\n  provider: somecloud\n  default: m1\n"
        "delegation:\n  model: d1\n"))
    tc = _client()
    r = tc.post("/operations", json={"changes": [
        {"profileId": "default", "slotId": "delegation.model",
         "action": "set", "to": {"model": "d2"}}]})
    assert r.status_code == 409, r.text
    assert "missing provider credentials" in r.json()["detail"]["error"]


def test_main_slot_without_provider_never_reports_base_url_as_provider(hermes_home):
    """The inventory's configured provider comes from a PROVIDER companion
    only — an endpoint URL (which can carry credentials) never becomes it."""
    secret_url = "http://user:secret-token@127.0.0.1:9000/v1?token=abc"
    _profile(hermes_home, "default", config=(
        f"model:\n  default: m1\n  base_url: {secret_url}\n"))
    slot = next(s for s in api._profile_inventory()["default"]["slots"]
                if s["slotId"] == "main")
    assert slot["provider"] == ""
    assert slot["model"] == "m1"
    assert "secret-token" not in json.dumps(slot)


def test_picker_auth_prefers_an_authenticated_duplicate_row(hermes_home, monkeypatch):
    """Duplicate picker rows for one slug: an earlier unauthenticated row must
    not shadow a later authenticated one."""
    hermes_home.make_profile("alpha")

    class StubCatalog:
        @staticmethod
        def _load_picker_payload(refresh):
            return {"providers": [
                {"slug": "nous", "authenticated": False},
                {"slug": "nous", "authenticated": True},
            ]}

    monkeypatch.setattr(api, "_model_catalog_module", lambda: StubCatalog)
    assert api._runtime_provider_authenticated("alpha", "nous") is True
    monkeypatch.setattr(api, "_model_catalog_module", lambda: type(
        "M", (), {"_load_picker_payload": staticmethod(lambda refresh: {
            "providers": [{"slug": "nous", "authenticated": False}]})}))
    assert api._runtime_provider_authenticated("alpha", "nous") is False


def test_state_chain_entries_are_sanitized(hermes_home):
    """A stored chain can carry route metadata beside provider/model (base_url
    with userinfo, api_key, headers). The public /state DTO is allowlisted and
    URL-sanitized — never the raw configured value."""
    _profile(hermes_home, "default", config=(
        ROUTE_CONFIG
        + "delegation:\n"
        + "  fallback_providers:\n"
        + "    - provider: nous\n"
        + "      model: hermes-4\n"
        + "      base_url: http://user:tok3n@127.0.0.1:9000/v1?t=abc\n"
        + "      api_key: SECRET-KEY\n"
        + "      headers: {Authorization: 'Bearer SECRET-TOK'}\n"))
    slot = next(s for s in api._profile_inventory()["default"]["slots"]
                if s["slotId"] == "delegation.fallback_chain")
    blob = json.dumps(slot)
    assert "SECRET" not in blob and "tok3n" not in blob
    assert "api_key" not in blob and "headers" not in blob
    entry = slot["chain"][0]
    assert entry["provider"] == "nous" and entry["model"] == "hermes-4"
    # the endpoint survives, sanitized: no userinfo, no query
    assert entry["base_url"] == "http://127.0.0.1:9000/v1"


def test_reconciliation_runs_under_the_mutation_lock(hermes_home, monkeypatch):
    """Threadpool handlers made concurrent reconciliation possible; /recover
    must serialize with Apply/Undo through the same mutation lock."""
    _profile(hermes_home, "default")
    tc = _client()
    r = tc.post("/operations", json={"changes": [
        {"profileId": "default", "slotId": "auxiliary.compression",
         "action": "set", "to": {"provider": "lablocal", "model": "lab-7b"}}]})
    assert r.status_code == 200, r.text
    op_id = r.json()["operation"]["id"]

    held: Dict[str, Any] = {}
    real = api.reconcile

    def probe(journal, oid):
        if hasattr(api._LOCK, "_is_owned"):
            held["owned"] = api._LOCK._is_owned()
        return real(journal, oid)

    monkeypatch.setattr(api, "reconcile", probe)
    r = tc.post(f"/operations/{op_id}/recover")
    assert r.status_code == 200, r.text
    assert held.get("owned") is True, "reconcile must run under _LOCK"


def test_speech_observation_uses_selected_runtime_backend():
    cfg = {'stt': {'provider': 'groq', 'groq': {'model': 'whisper-large-v3'}}}
    obs = next(item for item in ri.build_inventory(cfg) if item.descriptor.slot_id == 'specialist.stt')
    assert obs.descriptor.storage_path == 'stt.groq.model'
    assert obs.effective == 'whisper-large-v3'
    assert obs.effective_provider == 'groq'


def test_moa_edit_addresses_runtime_aggregator_not_unused_preset_fields():
    cfg = {'moa': {'default_preset': 'fast', 'presets': {'fast': {
        'reference_models': [{'provider': 'openai', 'model': 'advisor'}],
        'aggregator': {'provider': 'openai', 'model': 'old', 'temperature': 0.4},
        'fanout': 'user_turn'}}}}
    preview = ri.preview_changes(cfg, [ri.RouteEdit('moa.preset.fast', {'provider': 'openai', 'model': 'new'})])
    assert preview.clean, preview.rejected
    assert preview.changes[0].storage_path == 'moa.presets.fast.aggregator'
    assert preview.changes[0].after['temperature'] == 0.4
    from hermes_cli.moa_config import validate_moa_payload
    cfg['moa']['presets']['fast']['aggregator'] = preview.changes[0].after
    assert validate_moa_payload(cfg['moa']) == []


def test_unknown_moa_preset_is_not_a_fake_success():
    preview = ri.preview_changes({}, [ri.RouteEdit('moa.default_preset', {'model': 'nonexistent-preset'})])
    assert not preview.clean


def test_speech_backend_switch_binds_new_model_path_and_preserves_voice():
    cfg = {'tts': {'provider': 'elevenlabs', 'elevenlabs': {'model_id': 'eleven_multilingual_v2', 'voice_id': 'keep-voice'}}}
    preview = ri.preview_changes(cfg, [ri.RouteEdit('specialist.tts', {'provider': 'openai', 'model': 'gpt-4o-mini-tts'})])
    assert preview.clean, preview.rejected
    changes = {item.storage_path: item.after for item in preview.changes}
    assert changes['tts.openai.model'] == 'gpt-4o-mini-tts'
    assert changes['tts.provider'] == 'openai'
    assert cfg['tts']['elevenlabs']['voice_id'] == 'keep-voice'


def test_unknown_provider_is_not_authenticated_by_unrelated_env(hermes_home, monkeypatch):
    hermes_home.make_profile('alpha', env={'UNRELATED_KEY': 'not-authentication'})
    monkeypatch.setattr(api, '_runtime_provider_authenticated', lambda *args: None)
    ident = discover_profiles()['alpha']
    assert not api._provider_credentials_satisfied('alpha', ident, {}, 'unrecognized-provider')


def test_declared_custom_key_env_is_not_bypassed_by_endpoint(hermes_home):
    hermes_home.make_profile('alpha')
    ident = discover_profiles()['alpha']
    cfg = {'providers': {'custom': {'base_url': 'http://127.0.0.1:9090/v1', 'key_env': 'NEEDED_CUSTOM_KEY'}}}
    assert not api._provider_credentials_satisfied('alpha', ident, cfg, 'custom')


def test_request_ack_does_not_override_saved_preset_policy(hermes_home, monkeypatch):
    hermes_home.make_profile('alpha')
    monkeypatch.setattr(api, '_profile_has_credentials', lambda *args: True)
    monkeypatch.setattr(api, '_live_cost_class', lambda *args: 'unknown')
    preset = ReviewedPreset('locked', 'Locked policy', BillingPolicy(['metered'], False),
        [RouteTarget('alpha', 'canonical_config', '', {'delegation.model': 'm'}, 'metered')],
        list(discover_profiles()))
    reasons = api._preset_preflight(preset, allow_unknown=True)
    assert any('unknown cost' in reason for reason in reasons)


def test_quarantine_failure_never_overwrites_corrupt_file(hermes_home, monkeypatch):
    path = api._state_dir() / 'presets.json'
    path.write_text('{broken')
    def fail(*args):
        raise PermissionError('test quarantine refusal')
    monkeypatch.setattr(api.os, 'replace', fail)
    with pytest.raises(Exception):
        api._preset_store()
    assert path.read_text() == '{broken'


def test_quarantine_names_unique_and_warning_persists(hermes_home, monkeypatch):
    path = api._state_dir() / 'presets.json'
    monkeypatch.setattr(api.time, 'time', lambda: 100)
    path.write_text('{first')
    api._preset_store()
    path.write_text('{second')
    api._preset_store()
    backups = list(path.parent.glob('presets.json.corrupt-*'))
    assert len(backups) == 2
    assert {backup.read_text() for backup in backups} == {'{first', '{second'}
    api._preset_store()
    assert api._PRESET_CORRUPT is True
