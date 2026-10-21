"""Public host protocol exercised through HTTP with real journal/config stores."""
import importlib

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

PREFIX = "/api/plugins/hermes-model-switcher"


@pytest.fixture
def lab(hermes_home, monkeypatch):
    from hermes_adapter import HermesAdapter
    api = importlib.import_module("dashboard.plugin_api")
    monkeypatch.setattr(api, "_ADAPTER", HermesAdapter())
    monkeypatch.setattr(api, "_PREVIEW_CACHE", {})
    monkeypatch.setattr(api, "_STARTUP_RECONCILED", None)
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    options = {"providers": [{"slug": "openai", "name": "OpenAI",
                             "authenticated": True, "models": ["new-model"]}],
               "provider": "openai", "model": "gpt-4o-mini"}
    host = {"profiles": {"default": {"options": options, "auxiliary": None, "moa": None}}}
    with TestClient(app) as client:
        yield client, api, hermes_home.path, host


def start(lab):
    client, api, home, host = lab
    changes = [{"profileId": "default", "slotId": "main", "action": "set",
                "to": {"provider": "openai", "model": "new-model"}}]
    before = (home / "config.yaml").read_bytes()
    response = client.post(PREFIX + "/preview", json={"changes": changes, "host": host})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["preview"]["blocked"] == []
    assert preview["preview"]["changes"][0]["executor"] == "host"
    response = client.post(PREFIX + "/operations", json={
        "previewId": preview["previewId"], "host": host})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["operation"]["state"] == "awaiting_host", data
    assert not data["ok"]
    call = data["hostCall"]
    assert set(call) == {"opId", "seq", "token", "method", "path", "query", "body"}
    assert call["seq"] == 0
    assert call["method"] == "POST" and call["path"] == "/api/model/set"
    assert call["query"] == {"profile": "default"}
    assert call["body"] == {"scope": "main", "provider": "openai", "model": "new-model"}
    assert (home / "config.yaml").read_bytes() == before
    return data


def host_write(home, foreign=False):
    # Simulate model/set's exact ordinary-provider assignment effects. No
    # private route import, catalog discovery or network is needed for this lab.
    path = home / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["model"].update(provider="openai", default="new-model",
                        base_url="https://api.openai.com/v1", api_mode="chat_completions")
    for key in ("context_length", "api_key", "api", "key_env", "api_key_env"):
        cfg["model"].pop(key, None)
    if foreign:
        cfg["unrelated"] = "foreign edit"
    path.write_text(yaml.safe_dump(cfg))


def result(lab, data, outcome, **extra):
    client = lab[0]
    call = data["hostCall"]
    return client.post(PREFIX + "/operations/" + call["opId"] + "/host-result",
                       json={"seq": call["seq"], "token": call["token"],
                             "outcome": outcome, **extra})


def test_main_preview_host_write_result_commits(lab):
    data = start(lab)
    host_write(lab[2])
    response = result(lab, data, "ok", status=200)
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["ok"] and done["operation"]["state"] == "committed"
    assert "hostCall" not in done
    step = done["operation"]["results"][0]
    assert step["status"] == "applied"
    assert step["revisionBefore"] != step["revisionAfter"]
    saved = lab[0].get(PREFIX + "/operations/" + data["operation"]["id"]).json()
    assert saved == done
    undone = lab[0].post(PREFIX + "/operations/" + data["operation"]["id"] + "/undo").json()
    assert undone["ok"] and undone["operation"]["state"] == "undone"
    assert yaml.safe_load((lab[2] / "config.yaml").read_text())["model"]["default"] == "gpt-4o-mini"


@pytest.mark.parametrize("outcome,write,category", [
    ("declined", False, "host_declined"),
    ("error", True, "host_error"),
    ("ok", False, "host_no_write"),
])
def test_host_failures_use_readback_and_compensate(lab, outcome, write, category):
    data = start(lab)
    if write:
        host_write(lab[2])
    response = result(lab, data, outcome, status=503 if outcome == "error" else 200)
    assert response.status_code == 200, response.text
    done = response.json()
    assert not done["ok"]
    assert done["operation"]["failure"] == category
    assert done["operation"]["state"] in ("compensated", "pending")
    assert yaml.safe_load((lab[2] / "config.yaml").read_text())["model"]["default"] == "gpt-4o-mini"
    assert "hostCall" not in done


def test_foreign_edit_is_retained_and_undo_refused(lab):
    data = start(lab)
    host_write(lab[2], foreign=True)
    before = (lab[2] / "config.yaml").read_bytes()
    response = result(lab, data, "ok")
    assert response.status_code == 200, response.text
    done = response.json()
    assert not done["ok"] and done["operation"]["state"] == "recovery_required"
    assert done["operation"]["failure"] == "external_edit"
    assert done["operation"]["results"][0]["status"] == "conflict"
    assert not done["operation"]["undoable"]
    response = lab[0].post(PREFIX + "/operations/" + data["operation"]["id"] + "/undo")
    assert response.status_code == 409
    assert (lab[2] / "config.yaml").read_bytes() == before


def test_wrong_token_and_replay_are_409_without_mutation(lab):
    data = start(lab)
    client, api, home, host = lab
    call = data["hostCall"]
    wrong = {**data, "hostCall": {**call, "token": "not-the-issued-token"}}
    response = result(lab, wrong, "ok")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["failure"] == "host_token_mismatch"
    pending = client.get(PREFIX + "/operations/" + call["opId"]).json()
    assert pending["operation"] == data["operation"]
    assert "hostCall" not in pending  # plaintext token is never persisted
    host_write(home)
    accepted = result(lab, data, "ok").json()
    assert accepted["ok"]
    response = result(lab, data, "ok")
    assert response.status_code == 409
    assert client.get(PREFIX + "/operations/" + call["opId"]).json() == accepted


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("write", [False, True, "foreign"])
def test_recovery_and_restart_reconcile_awaiting_host(lab, monkeypatch, restart, write):
    data = start(lab)
    if write:
        host_write(lab[2], foreign=write == "foreign")
    if restart:
        monkeypatch.setattr(lab[1], "_STARTUP_RECONCILED", None)
        response = lab[0].get(PREFIX + "/operations/" + data["operation"]["id"])
    else:
        response = lab[0].post(PREFIX + "/operations/" + data["operation"]["id"] + "/recover")
    assert response.status_code == 200, response.text
    done = response.json()
    assert not done["ok"]
    assert done["operation"]["state"] != "awaiting_host"
    assert "hostCall" not in done
    if write == "foreign":
        assert done["operation"]["state"] == "recovery_required"
        assert done["operation"]["failure"] == "external_edit"
        assert yaml.safe_load((lab[2] / "config.yaml").read_text())["unrelated"] == "foreign edit"
    else:
        assert done["operation"]["failure"] == "host_error"
        assert yaml.safe_load((lab[2] / "config.yaml").read_text())["model"]["default"] == "gpt-4o-mini"


@pytest.mark.parametrize("route", ["catalog", "models"])
def test_retired_get_requires_host_catalog(lab, route):
    response = lab[0].get(PREFIX + "/" + route)
    assert response.status_code == 410
    assert response.json() == {"ok": False, "reason": "hostCatalogRequired"}


def test_post_catalog_options_and_host_profile_isolation(lab):
    options = lab[3]["profiles"]["default"]["options"]
    response = lab[0].post(PREFIX + "/catalog", json={"profile": "default", "options": options})
    assert response.status_code == 200, response.text
    assert response.json()["ok"]
    assert response.json()["providers"][0]["models"][0]["id"] == "new-model"
    response = lab[0].post(PREFIX + "/state", json={"host": lab[3]})
    assert response.status_code == 200
    assert response.json()["profiles"][0]["credentials"]["openai"]


def test_state_dir_inside_plugin_tree_is_read_only(lab, monkeypatch):
    client, api, home, host = lab
    monkeypatch.setattr(api, "_PKG_ROOT", home)
    response = client.get(PREFIX + "/state")
    assert response.status_code == 200, response.text
    assert not response.json()["runtime"]["writable"]
    assert response.json()["runtime"]["reasons"]
    response = client.post(PREFIX + "/operations", json={"changes": [{
        "profileId": "default", "slotId": "main", "action": "set",
        "to": {"provider": "openai", "model": "new-model"}}], "host": host})
    assert response.status_code == 409
    assert not (home / "state").exists()


def test_state_dir_legacy_environment_override_is_ignored(lab, monkeypatch):
    # Split the retired name so the repository-wide removal gate also proves
    # there are no remaining runtime consumers of this old environment seam.
    alternate = lab[2] / "alternate-state"
    monkeypatch.setenv("HERMES_MODEL_SWITCHER_" + "STATE_DIR", str(alternate))
    data = start(lab)
    assert (lab[2] / "state" / "hermes-model-switcher" / "operations.db").is_file()
    assert not alternate.exists()
    assert result(lab, data, "declined").status_code == 200


def test_saved_preset_retains_host_metadata_through_preview_apply(lab):
    client, api, home, host = lab
    changes = [{"profileId": "default", "slotId": "main", "action": "set",
                "to": {"provider": "openai", "model": "new-model"}}]
    saved = client.post(PREFIX + "/presets", json={
        "name": "host review", "changes": changes, "host": host})
    assert saved.status_code == 200, saved.text
    preset_id = saved.json()["preset"]["id"]
    import json
    record = json.loads((home / "state" / "hermes-model-switcher" / "presets.json").read_text())[0]
    target = record["targets"][0]
    assert target["executor"] == "host"
    assert target["readback_keys"] == ["model.provider", "model.default"]
    assert target["host_call"]["path"] == "/api/model/set"
    assert target["allowed_prefixes"] == ["model", "_config_version"]
    preview = client.post(PREFIX + "/preview", json={"changes": changes, "host": host}).json()
    response = client.post(PREFIX + "/operations", json={
        "presetId": preset_id, "previewId": preview["previewId"], "host": host})
    assert response.status_code == 200, response.text
    assert response.json()["operation"]["state"] == "awaiting_host"
    host_write(home)
    assert result(lab, response.json(), "ok").json()["ok"]


def test_profile_host_auth_does_not_transfer_and_is_rechecked(lab):
    client, api, home, host = lab
    (home / "profiles" / "alpha").mkdir(parents=True)
    path = home / "profiles" / "alpha" / "config.yaml"
    path.write_bytes((home / "config.yaml").read_bytes())
    changes = [{"profileId": "alpha", "slotId": "main", "action": "set",
                "to": {"provider": "openai", "model": "new-model"}}]
    response = client.post(PREFIX + "/operations", json={"changes": changes, "host": host})
    assert response.status_code == 409, response.text
    assert "missing provider credentials" in response.text
    changes[0]["profileId"] = "default"
    preview = client.post(PREFIX + "/preview", json={"changes": changes, "host": host}).json()
    # A reviewed intent isn't cached credential authority. Current false or
    # missing per-profile host evidence must refuse before opening an op.
    import copy
    declined = copy.deepcopy(host)
    declined["profiles"]["default"]["options"]["providers"][0]["authenticated"] = False
    response = client.post(PREFIX + "/operations", json={
        "previewId": preview["previewId"], "host": declined})
    assert response.status_code == 409, response.text
    assert "missing provider credentials" in response.text
    assert yaml.safe_load(path.read_text())["model"]["default"] == "gpt-4o-mini"


def test_non_main_multi_slot_batch_uses_backend_not_multiple_host_tokens(lab):
    client, api, home, host = lab
    changes = [
        {"profileId": "default", "slotId": "auxiliary.compression", "action": "set",
         "to": {"provider": "openai", "model": "new-model"}},
        {"profileId": "default", "slotId": "specialist.tts", "action": "set",
         "to": {"provider": "openai", "model": "tts-1"}},
    ]
    response = client.post(PREFIX + "/preview", json={"changes": changes, "host": host})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert not preview["preview"]["blocked"]
    assert [c["executor"] for c in preview["preview"]["changes"]] == ["backend", "backend"]
    assert all("host assignment" not in effect for c in preview["preview"]["changes"]
               for effect in c["sideEffects"])
    response = client.post(PREFIX + "/operations", json={"previewId": preview["previewId"], "host": host})
    assert response.status_code == 200, response.text
    assert response.json()["operation"]["state"] == "committed"
    assert "hostCall" not in response.json()


def test_mixed_main_batch_is_blocked_consistently_in_preview_and_apply(lab):
    client, api, home, host = lab
    before = (home / "config.yaml").read_bytes()
    changes = [
        {"profileId": "default", "slotId": "main", "action": "set",
         "to": {"provider": "openai", "model": "new-model"}},
        {"profileId": "default", "slotId": "specialist.tts", "action": "set",
         "to": {"provider": "openai", "model": "tts-1"}},
    ]
    response = client.post(PREFIX + "/preview", json={"changes": changes, "host": host})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["preview"]["changes"] == []
    assert preview["preview"]["blocked"][0]["kind"] == "host_scope"
    response = client.post(PREFIX + "/operations", json={"previewId": preview["previewId"], "host": host})
    assert response.status_code == 409
    response = client.post(PREFIX + "/operations", json={"changes": changes, "host": host})
    assert response.status_code == 422
    assert (home / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("legacy", [
    "model:\n  provider: openai\n  default: gpt-4o-mini\nbase_url: http://127.0.0.1:8080/v1\n",
    "model:\n  provider: openai\n  name: gpt-4o-mini\n",
    "model:\n  provider: openai\n  default: gpt-4o-mini\n  api_base: http://127.0.0.1:8080/v1\n",
])
def test_auxiliary_host_save_accepts_only_documented_model_normalization(lab, legacy):
    client, api, home, host = lab
    path = home / "config.yaml"
    legacy += "auxiliary:\n  compression:\n    provider: openai\n    model: old-compression\n"
    path.write_text(legacy)
    change = {"profileId": "default", "slotId": "auxiliary.compression", "action": "set",
              "to": {"provider": "openai", "model": "new-model"}}
    response = client.post(PREFIX + "/preview", json={"changes": [change], "host": host})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert not preview["preview"]["blocked"]
    assert preview["preview"]["changes"][0]["executor"] == "host"
    response = client.post(PREFIX + "/operations", json={"previewId": preview["previewId"], "host": host})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["hostCall"]["body"] == {
        "scope": "auxiliary", "task": "compression", "provider": "openai", "model": "new-model"}
    from hermes_cli.config import load_config, save_config
    with api._ADAPTER.scoped("default"):
        cfg = load_config()
        cfg.setdefault("auxiliary", {}).setdefault("compression", {}).update(
            provider="openai", model="new-model")
        save_config(cfg)
    response = result(lab, data, "ok")
    assert response.status_code == 200, response.text
    assert response.json()["operation"]["state"] == "committed", response.text
    assert response.json()["ok"]
    undone = client.post(PREFIX + "/operations/" + data["operation"]["id"] + "/undo")
    # The public save seam canonicalizes a legacy snapshot again on restore;
    # it cannot reproduce raw alias keys. The adapter correctly refuses to
    # claim exact restoration rather than using a forbidden raw-YAML fallback.
    assert undone.status_code == 200, undone.text
    assert undone.json()["operation"]["state"] == "recovery_required", undone.text
    assert not undone.json()["ok"]
    assert yaml.safe_load(path.read_text())["auxiliary"]["compression"]["model"] == "old-compression"


def test_operation_dto_keeps_each_step_even_for_repeated_profiles(lab):
    dto = lab[1]._op_envelope({"op_id": "op-repeated-profile", "state": "committed", "steps": [
        {"seq": 0, "profile": "default", "store_kind": "canonical_config", "state": "applied",
         "pre_revision": "a", "post_revision": "b", "snapshot": {"private": "never-public"}},
        {"seq": 1, "profile": "default", "store_kind": "cron_jobs", "state": "applied",
         "pre_revision": "c", "post_revision": "d", "path": "private-file"},
    ]})
    assert dto["ok"]
    assert [r["seq"] for r in dto["operation"]["results"]] == [0, 1]
    assert [r["profileId"] for r in dto["operation"]["results"]] == ["default", "default"]
    assert [r["store"] for r in dto["operation"]["results"]] == ["canonical_config", "cron_jobs"]
    assert "never-public" not in str(dto) and "private-file" not in str(dto)
