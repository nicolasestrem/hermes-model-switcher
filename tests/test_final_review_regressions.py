"""Final independent-review regressions, isolated data only."""
import inspect
import json
import pytest

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
