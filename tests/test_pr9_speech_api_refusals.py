"""Speech shape refusals must hold at both HTTP entry points, not just in UI."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip('hermes_cli.config_defaults')

BASE = ('model:\n  provider: lablocal\n  default: chat\n'
        'providers:\n  lablocal:\n    base_url: http://127.0.0.1:8087/v1\n'
        '  openai:\n    base_url: http://127.0.0.1:8088/v1\n')


def client(hermes_home):
    from dashboard import plugin_api as pa
    (hermes_home.path / 'config.yaml').write_text(BASE)
    app = FastAPI()
    app.include_router(pa.router)
    return TestClient(app)


@pytest.mark.parametrize('action,to', [
    ('remove', None),
    ('set', {'chain': None}),
    ('set', {'chain': []}),
    ('set', {'provider': 'edge', 'model': 'imaginary'}),
    ('set', {'provider': 'openai'}),
    ('set', {'model': 'tts-1'}),
])
def test_model_less_tts_refuses_current_field_mutations_at_http_endpoints(hermes_home, action, to):
    tc = client(hermes_home)
    original = (hermes_home.path / 'config.yaml').read_bytes()
    change = {'profileId': 'default', 'slotId': 'specialist.tts', 'action': action,
              'from': {'provider': 'edge', 'model': ''}, 'to': to}
    reviewed = tc.post('/preview', json={'changes': [change]})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()['preview']['blocked'], reviewed.text
    assert not reviewed.json()['preview']['changes'], reviewed.text
    attempted = tc.post('/operations', json={'changes': [change]})
    assert attempted.status_code in (409, 422), attempted.text
    assert (hermes_home.path / 'config.yaml').read_bytes() == original


@pytest.mark.parametrize('to', [
    {'provider': 'openai', 'model': ''},
    {'provider': 'openai', 'model': '  '},
    {'provider': 'openai', 'model': None},
    {'provider': 'openai', 'model': 123},
    {'provider': ' openai ', 'model': 'tts-1'},
    {'provider': 'openai', 'model': ' tts-1 '},
])
def test_invalid_speech_routes_cannot_be_saved_directly_or_via_review(hermes_home, to):
    tc = client(hermes_home)
    original = (hermes_home.path / 'config.yaml').read_bytes()
    change = {'profileId': 'default', 'slotId': 'specialist.tts', 'action': 'set', 'to': to}
    reviewed = tc.post('/preview', json={'changes': [change]})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()['preview']['blocked'], reviewed.text
    assert not reviewed.json()['preview']['changes'], reviewed.text
    attempted = tc.post('/operations', json={'changes': [change]})
    assert attempted.status_code in (409, 422), attempted.text
    assert (hermes_home.path / 'config.yaml').read_bytes() == original
