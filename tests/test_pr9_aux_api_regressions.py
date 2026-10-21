"""Real HTTP regressions for auxiliary scalar validation and inherited task DTOs."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip('hermes_cli.config_defaults')

BASE = ('model:\n  provider: lablocal\n  default: chat\n'
        'providers:\n  lablocal:\n    base_url: http://127.0.0.1:8087/v1\n'
        '  openrouter:\n    base_url: http://127.0.0.1:8088/v1\n'
        'auxiliary:\n  compression:\n    provider: lablocal\n    model: base-model\n'
        '  openrouter_model: org/original-model:free\n')


def client(hermes_home, suffix=''):
    from dashboard import plugin_api as pa
    (hermes_home.path / 'config.yaml').write_text(BASE + suffix)
    app = FastAPI()
    app.include_router(pa.router)
    return TestClient(app)


@pytest.mark.parametrize('payload', [
    {'model': None}, {'model': 123}, {'model': True}, {'model': []},
    {'model': {}}, {'model': ''}, {'model': '  '}, {'model': ' org/new-model:free '}, {},
])
def test_invalid_openrouter_scalar_cannot_overwrite_existing_route(hermes_home, payload):
    tc = client(hermes_home)
    original = (hermes_home.path / 'config.yaml').read_bytes()
    change = {'profileId': 'default', 'slotId': 'auxiliary.openrouter_model', 'action': 'set', 'to': payload}
    reviewed = tc.post('/preview', json={'changes': [change]})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()['preview']['blocked'], reviewed.text
    assert not reviewed.json()['preview']['changes'], reviewed.text
    attempted = tc.post('/operations', json={'changes': [change]})
    assert attempted.status_code in (409, 422), attempted.text
    assert (hermes_home.path / 'config.yaml').read_bytes() == original


def test_valid_openrouter_scalar_save_and_removal_keep_exact_intents(hermes_home):
    tc = client(hermes_home)
    original = (hermes_home.path / 'config.yaml').read_bytes()
    row = next(s for s in tc.get('/state').json()['slots'] if s['slotId'] == 'auxiliary.openrouter_model')
    assert row['provider'] == 'openrouter' and row['model'] == 'org/original-model:free'
    change = {'profileId': 'default', 'slotId': 'auxiliary.openrouter_model', 'action': 'set',
              'to': {'model': 'org/new-model:free'}}
    reviewed = tc.post('/preview', json={'changes': [change]})
    assert reviewed.status_code == 200 and not reviewed.json()['preview']['blocked'], reviewed.text
    applied = tc.post('/operations', json={'previewId': reviewed.json()['preview']['id']})
    assert applied.status_code == 200, applied.text
    assert applied.json()['operation']['state'] == 'committed', applied.text
    assert b'org/new-model:free' in (hermes_home.path / 'config.yaml').read_bytes()
    undone = tc.post('/operations/' + applied.json()['operation']['id'] + '/undo')
    assert undone.status_code == 200, undone.text
    assert (hermes_home.path / 'config.yaml').read_bytes() == original
    change.update(action='remove', to=None, **{'from': {'provider': 'openrouter', 'model': 'org/original-model:free'}})
    removed = tc.post('/preview', json={'changes': [change]})
    assert removed.status_code == 200 and not removed.json()['preview']['blocked'], removed.text
    assert len(removed.json()['preview']['changes']) == 1
    applied = tc.post('/operations', json={'previewId': removed.json()['preview']['id']})
    assert applied.status_code == 200, applied.text
    assert applied.json()['operation']['state'] == 'committed', applied.text
    import yaml
    assert not yaml.safe_load((hermes_home.path / 'config.yaml').read_text())['auxiliary'].get('openrouter_model')
    undone = tc.post('/operations/' + applied.json()['operation']['id'] + '/undo')
    assert undone.status_code == 200, undone.text
    assert (hermes_home.path / 'config.yaml').read_bytes() == original


@pytest.mark.parametrize('block', ['', '  follow_base: {}\n',
    '  follow_base:\n    timeout: 30\n',
    '  follow_base:\n    provider: auto\n    model: ""\n    base_url: ""\n'])
def test_unpinned_registered_task_dto_reports_inheritance_and_effective_route(hermes_home, block):
    tc = client(hermes_home, block)
    host = {'profiles': {'default': {'auxiliary': {
        'tasks': [{
            'task': 'follow_base', 'label': 'Follow base', 'hint': '', 'plugin': 'example-plugin',
            'provider': 'auto', 'model': '', 'base_url': '', 'reasoning_effort': None,
            'local_endpoint': False, 'inherit_from': 'compression',
            'effective': {'provider': 'lablocal', 'model': 'base-model', 'base_url': ''},
        }], 'main': {'provider': 'lablocal', 'model': 'chat'},
    }}}}
    original = (hermes_home.path / 'config.yaml').read_bytes()
    response = tc.post('/state', json={'host': host})
    assert response.status_code == 200, response.text
    row = next(s for s in response.json()['slots'] if s['slotId'] == 'auxiliary.follow_base')
    assert row['state'] == 'inherit', row
    assert row['effectiveRoute']['provider'] == 'lablocal', row
    assert row['effectiveRoute']['model'] == 'base-model', row
    assert row['lastObserved'] is None
    assert row['provider'] != 'lablocal' and row['model'] != 'base-model'
    assert row['metadata']['label'] == 'Follow base', row
    assert row['metadata']['plugin'] == 'example-plugin', row
    assert row['metadata']['inheritFrom'] == 'compression', row
    assert (hermes_home.path / 'config.yaml').read_bytes() == original
    # Host evidence is request-scoped, not a replacement process-global registry.
    missing = tc.get('/state')
    assert missing.status_code == 200, missing.text
    assert not any(s['slotId'] == 'auxiliary.follow_base' for s in missing.json()['slots'])
