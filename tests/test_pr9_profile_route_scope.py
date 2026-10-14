"""Profile task discovery must agree across state, preview, apply and removal."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip('hermes_cli.config_defaults')


def setup_tasks(hermes_home, monkeypatch):
    from dashboard import plugin_api as pa
    from hermes_constants import get_hermes_home
    import hermes_cli.plugins as plugins
    from hermes_cli.plugins_aux_tasks import build_auxiliary_task_entry
    cfg = ('model:\n  provider: lablocal\n  default: chat\n'
           'providers:\n  lablocal:\n    base_url: http://127.0.0.1:8087/v1\n'
           'auxiliary:\n  profile_task:\n    provider: lablocal\n    model: old-model\n')
    hermes_home.make_profile('alpha', {'_raw': cfg})
    hermes_home.make_profile('beta', {'_raw': cfg})
    entry = build_auxiliary_task_entry('profile-plugin', 'profile-plugin', 'profile_task',
                                      display_name='Profile task', description='',
                                      defaults={}, inherit_from=None, registered={})

    def entries():
        home = Path(get_hermes_home())
        return [entry] if home.name == 'alpha' and home.parent.name == 'profiles' else []

    monkeypatch.setattr(plugins, 'get_plugin_auxiliary_tasks', entries)
    app = FastAPI()
    app.include_router(pa.router)
    return TestClient(app)


def test_profile_plugin_route_preview_apply_undo_share_state_scope(hermes_home, monkeypatch):
    tc = setup_tasks(hermes_home, monkeypatch)
    state = tc.get('/state').json()
    assert any(s['profileId'] == 'alpha' and s['slotId'] == 'auxiliary.profile_task'
               for s in state['slots'])
    original = (hermes_home.path / 'profiles' / 'alpha' / 'config.yaml').read_bytes()
    changes = [{'profileId': 'alpha', 'slotId': 'auxiliary.profile_task', 'action': 'set',
                'to': {'provider': 'lablocal', 'model': 'new-model'}}]
    review = tc.post('/preview', json={'changes': changes})
    assert review.status_code == 200, review.text
    assert not review.json()['preview'].get('blocked'), review.text
    assert len(review.json()['preview']['changes']) == 1, review.text
    applied = tc.post('/operations', json={'previewId': review.json()['preview']['id']})
    assert applied.status_code == 200, applied.text
    op = applied.json()['operation']
    assert op['state'] == 'committed', applied.text
    assert b'new-model' in (hermes_home.path / 'profiles' / 'alpha' / 'config.yaml').read_bytes()
    undone = tc.post('/operations/' + op['id'] + '/undo')
    assert undone.status_code == 200, undone.text
    assert (hermes_home.path / 'profiles' / 'alpha' / 'config.yaml').read_bytes() == original
    # The same selector may not leak onto a profile where it is not registered.
    changes[0]['profileId'] = 'beta'
    refused = tc.post('/preview', json={'changes': changes})
    assert refused.status_code == 200, refused.text
    assert refused.json()['preview'].get('blocked'), refused.text
    assert not refused.json()['preview']['changes'], refused.text


def test_profile_plugin_exact_removal_uses_target_registry(hermes_home, monkeypatch):
    tc = setup_tasks(hermes_home, monkeypatch)
    review = tc.post('/preview', json={'changes': [{
        'profileId': 'alpha', 'slotId': 'auxiliary.profile_task', 'action': 'remove',
        'from': {'provider': 'lablocal', 'model': 'old-model'}, 'to': None,
    }]})
    assert review.status_code == 200, review.text
    assert not review.json()['preview'].get('blocked'), review.text
    assert len(review.json()['preview']['changes']) == 1, review.text


def test_legacy_readonly_descriptor_does_not_gain_assignment_capability(hermes_home, monkeypatch):
    from dashboard import plugin_api as pa
    from route_inventory import SlotDescriptor, SlotObservation, SlotFamily, InventoryResult, PROTOCOL_CHAT
    hermes_home.make_profile('default')
    desc = SlotDescriptor('plugin.legacy', SlotFamily.PLUGIN, PROTOCOL_CHAT,
                          'plugin.legacy.model', writable=False)
    obs = SlotObservation(desc, None, False, None, 'default')
    monkeypatch.setattr(pa, 'build_inventory', lambda cfg: InventoryResult([obs], []))
    row = pa._profile_inventory()['default']['slots'][0]
    assert row['writable'] is False
    assert row['selectable'] is False
    assert row['requiresModelBackend'] is False
