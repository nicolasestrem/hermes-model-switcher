"""Profile host-task evidence must agree across state, preview, apply and removal."""
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip('hermes_cli.config_defaults')


def _client():
    from dashboard import plugin_api as pa
    app = FastAPI()
    app.include_router(pa.router)
    return TestClient(app)


def setup_tasks(hermes_home):
    cfg = ('model:\n  provider: lablocal\n  default: chat\n'
           'providers:\n  lablocal:\n    base_url: http://127.0.0.1:8087/v1\n'
           'auxiliary:\n  profile_task:\n    provider: lablocal\n    model: old-model\n')
    hermes_home.make_profile('alpha', {'_raw': cfg})
    hermes_home.make_profile('beta', {'_raw': cfg})
    host = {'profiles': {
        'default': {'auxiliary': {'tasks': []}},
        'alpha': {'auxiliary': {
            'tasks': [{
                'task': 'profile_task', 'label': 'Profile task', 'hint': '',
                'plugin': 'profile-plugin', 'inherit_from': None,
                'provider': 'lablocal', 'model': 'old-model', 'base_url': '',
                'reasoning_effort': None, 'local_endpoint': False,
            }], 'main': {'provider': 'lablocal', 'model': 'chat'},
        }},
        'beta': {'auxiliary': {'tasks': [], 'main': {'provider': 'lablocal', 'model': 'chat'}}},
    }}
    return _client(), host


def _simulate_host_aux_write(call, hermes_home, adapter):
    """Browser/host stand-in: perform ONLY this fixture's same-provider assignment.

    Discovery is supplied as public host JSON, not patched into Hermes. The
    actual lab write uses public config APIs, independently of the plugin engine.
    """
    from hermes_cli.config import load_config, save_config
    assert call['method'] == 'POST' and call['path'] == '/api/model/set'
    assert call['query'] == {'profile': 'alpha'}
    assert call['body'] == {
        'scope': 'auxiliary', 'task': 'profile_task',
        'provider': 'lablocal', 'model': 'new-model',
    }
    lab = hermes_home.path.resolve()
    assert str(lab).startswith('/tmp/hermes-model-switcher-lab-')
    with adapter.scoped(call['query']['profile']):
        cfg = load_config()
        task = cfg['auxiliary'][call['body']['task']]
        assert task['provider'] == call['body']['provider']
        task.update(provider=call['body']['provider'], model=call['body']['model'])
        save_config(cfg)
    stored = yaml.safe_load((lab / 'profiles' / 'alpha' / 'config.yaml').read_text())
    assert stored['auxiliary']['profile_task']['model'] == 'new-model'


def test_profile_plugin_route_preview_apply_undo_share_state_scope(hermes_home, adapter):
    tc, host = setup_tasks(hermes_home)
    response = tc.post('/state', json={'host': host})
    assert response.status_code == 200, response.text
    state = response.json()
    assert any(s['profileId'] == 'alpha' and s['slotId'] == 'auxiliary.profile_task'
               for s in state['slots'])
    assert not any(s['profileId'] == 'beta' and s['slotId'] == 'auxiliary.profile_task'
                   for s in state['slots'])
    alpha_config = hermes_home.path / 'profiles' / 'alpha' / 'config.yaml'
    beta_config = hermes_home.path / 'profiles' / 'beta' / 'config.yaml'
    original, beta_original = alpha_config.read_bytes(), beta_config.read_bytes()
    default_original = (hermes_home.path / 'config.yaml').read_bytes()
    changes = [{'profileId': 'alpha', 'slotId': 'auxiliary.profile_task', 'action': 'set',
                'to': {'provider': 'lablocal', 'model': 'new-model'}}]
    review = tc.post('/preview', json={'changes': changes, 'host': host})
    assert review.status_code == 200, review.text
    assert not review.json()['preview'].get('blocked'), review.text
    assert len(review.json()['preview']['changes']) == 1, review.text
    assert review.json()['preview']['changes'][0]['executor'] == 'host', review.text
    applied = tc.post('/operations', json={'previewId': review.json()['preview']['id'], 'host': host})
    assert applied.status_code == 200, applied.text
    envelope = applied.json()
    op = envelope['operation']
    assert op['state'] == 'awaiting_host', applied.text
    assert alpha_config.read_bytes() == original  # planning must not perform the host write
    call = envelope['hostCall']
    assert call['opId'] == op['id'] and call['token']
    _simulate_host_aux_write(call, hermes_home, adapter)
    completed = tc.post('/operations/' + op['id'] + '/host-result', json={
        'seq': call['seq'], 'token': call['token'], 'outcome': 'ok', 'status': 200,
    })
    assert completed.status_code == 200, completed.text
    assert completed.json()['operation']['state'] == 'committed', completed.text
    assert completed.json()['ok'] is True, completed.text
    persisted = tc.get('/operations/' + op['id'])
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()['operation']['state'] == 'committed', persisted.text
    assert b'new-model' in alpha_config.read_bytes()
    undone = tc.post('/operations/' + op['id'] + '/undo')
    assert undone.status_code == 200, undone.text
    assert undone.json()['operation']['state'] == 'undone', undone.text
    assert alpha_config.read_bytes() == original
    assert beta_config.read_bytes() == beta_original
    assert (hermes_home.path / 'config.yaml').read_bytes() == default_original
    # The same selector may not leak onto a profile whose host has no registration.
    changes[0]['profileId'] = 'beta'
    refused = tc.post('/preview', json={'changes': changes, 'host': host})
    assert refused.status_code == 200, refused.text
    assert refused.json()['preview'].get('blocked'), refused.text
    assert not refused.json()['preview']['changes'], refused.text


def test_profile_plugin_exact_removal_uses_target_registry(hermes_home):
    tc, host = setup_tasks(hermes_home)
    alpha_config = hermes_home.path / 'profiles' / 'alpha' / 'config.yaml'
    beta_config = hermes_home.path / 'profiles' / 'beta' / 'config.yaml'
    original, beta_original = alpha_config.read_bytes(), beta_config.read_bytes()
    change = {
        'profileId': 'alpha', 'slotId': 'auxiliary.profile_task', 'action': 'remove',
        'from': {'provider': 'lablocal', 'model': 'old-model'}, 'to': None,
    }
    review = tc.post('/preview', json={'changes': [change], 'host': host})
    assert review.status_code == 200, review.text
    assert not review.json()['preview'].get('blocked'), review.text
    assert len(review.json()['preview']['changes']) == 1, review.text
    # Removal is not expressible by model/set; it stays a reviewed backend write.
    applied = tc.post('/operations', json={'previewId': review.json()['preview']['id'], 'host': host})
    assert applied.status_code == 200, applied.text
    assert applied.json()['operation']['state'] == 'committed', applied.text
    assert not applied.json().get('hostCall'), applied.text
    assert not yaml.safe_load(alpha_config.read_text()).get('auxiliary', {}).get('profile_task')
    undone = tc.post('/operations/' + applied.json()['operation']['id'] + '/undo')
    assert undone.status_code == 200, undone.text
    assert undone.json()['operation']['state'] == 'undone', undone.text
    # Canonical save may reorder a removed mapping when restoring its leaves.
    assert yaml.safe_load(alpha_config.read_text()) == yaml.safe_load(original)
    assert beta_config.read_bytes() == beta_original
    change['profileId'] = 'beta'
    refused = tc.post('/preview', json={'changes': [change], 'host': host})
    assert refused.status_code == 200, refused.text
    assert refused.json()['preview']['blocked'], refused.text
    assert not refused.json()['preview']['changes'], refused.text


def test_legacy_readonly_descriptor_does_not_gain_assignment_capability(hermes_home, monkeypatch):
    from dashboard import plugin_api as pa
    from route_inventory import SlotDescriptor, SlotObservation, SlotFamily, InventoryResult, PROTOCOL_CHAT
    hermes_home.make_profile('default')
    desc = SlotDescriptor('plugin.legacy', SlotFamily.PLUGIN, PROTOCOL_CHAT,
                          'plugin.legacy.model', writable=False)
    assert desc.selectable is None  # legacy descriptor leaves the additive capability unspecified
    obs = SlotObservation(desc, None, False, None, 'default')
    # A legacy descriptor is not expressible as a host auxiliary row. Inject only
    # this inventory result; exercise its capability projection over public HTTP.
    monkeypatch.setattr(pa, 'build_inventory', lambda cfg, *, host_aux=None: InventoryResult([obs], []))
    response = _client().post('/state', json={'host': {'profiles': {
        'default': {'auxiliary': {'tasks': []}},
    }}})
    assert response.status_code == 200, response.text
    row = next(s for s in response.json()['slots']
               if s['profileId'] == 'default' and s['slotId'] == 'plugin.legacy')
    assert row['writable'] is False
    assert row['selectable'] is False
    assert row['requiresModelBackend'] is False
