"""Test-only browser/host handshake over isolated /tmp profile configs.

These helpers simulate host writes through public config load/save seams, not
through the plugin's backend writer. Real host routes have their own tests.
"""
from pathlib import Path

import yaml


def finish_host_main_call(client, response, home):
    """Write and acknowledge exactly one pending main assignment."""
    from hermes_adapter import HermesAdapter
    from hermes_cli.config import load_config, save_config

    home = Path(home).resolve()
    assert home.parent == Path("/tmp")
    assert home.name.startswith("hermes-model-switcher-lab-")
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is False, envelope
    assert envelope["operation"]["state"] == "awaiting_host", envelope
    call = envelope["hostCall"]
    assert call["method"] == "POST" and call["path"] == "/api/model/set"
    assert call["opId"] == envelope["operation"]["id"]
    assert isinstance(call["seq"], int) and call["seq"] >= 0
    assert call["token"]
    body = call["body"]
    assert set(body) == {"scope", "provider", "model"}
    assert body["scope"] == "main"
    profile = call["query"]["profile"]
    adapter = HermesAdapter()
    profile_home = Path(adapter.profile_home(profile)).resolve()
    expected = home if profile == "default" else home / "profiles" / profile
    assert profile_home == expected
    assert profile_home.is_relative_to(home)
    with adapter.scoped(profile):
        cfg = load_config()
        cfg["model"].update(provider=body["provider"], default=body["model"])
        endpoint = cfg.get("providers", {}).get(body["provider"], {}).get("base_url")
        if endpoint:
            cfg["model"].update(base_url=endpoint, api_mode="chat_completions")
        save_config(cfg)
    stored = yaml.safe_load((profile_home / "config.yaml").read_text())
    assert stored["model"]["provider"] == body["provider"]
    assert stored["model"]["default"] == body["model"]
    response = client.post(f"/operations/{call['opId']}/host-result", json={
        "seq": call["seq"], "token": call["token"], "outcome": "ok", "status": 200,
    })
    assert response.status_code == 200, response.text
    return response


def finish_host_main_calls(client, response, home):
    """Complete all host main steps, requiring a persisted committed result."""
    assert response.status_code == 200, response.text
    assert response.json()["operation"]["state"] == "awaiting_host", response.text
    seen = set()
    while response.json()["operation"]["state"] == "awaiting_host":
        call = response.json()["hostCall"]
        key = (call["opId"], call["seq"])
        assert key not in seen
        seen.add(key)
        response = finish_host_main_call(client, response, home)
    assert response.json()["ok"] is True, response.text
    assert response.json()["operation"]["state"] == "committed", response.text
    persisted = client.get(f"/operations/{response.json()['operation']['id']}")
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["operation"]["state"] == "committed", persisted.text
    return response
