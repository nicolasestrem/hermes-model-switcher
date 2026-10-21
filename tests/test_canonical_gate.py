"""Bounded canonical-apply + privacy-API-gate tests.

Evidence required (plan §Mutation/§Privacy, this phase):
  1. Main apply uses the host handshake and public config save; undo invokes
     HermesAdapter.apply_canonical_save — no raw YAML write path.
  2. Non-main cron change pairs cron.model with cron.model_provider and the
     scheduler key cron.provider is preserved verbatim.
  3. Public operation responses carry only the to_public_operation allowlist:
     no snapshots, no storage paths, no changes, no secrets/auth material, no
     exception strings; undoable is a safe status (committed + applied steps).
  4. Unsupported stores/slots block visibly (ApplyBlocked / 422), never
     falling back to a raw YAML write.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

from fastapi import FastAPI
from fastapi.testclient import TestClient

from model_switcher.adapters import adapter_for
from model_switcher.engine import ApplyBlocked, plan_operation

NEW_PROVIDER, NEW_MODEL = "lablocal", "lab-7b-instruct"


from host_protocol_helpers import finish_host_main_calls


CRON_CONFIG = """\
model:
  provider: openai
  default: gpt-4o-mini
providers:
  lablocal:
    base_url: http://127.0.0.1:8080/v1
cron:
  provider: /usr/bin/systemd-run   # the SCHEDULER — never a model slot
  model: j/old
  model_provider: judge-old
unrelated:
  keep: yes
"""


@pytest.fixture()
def client(hermes_home):
    home = hermes_home.path
    hermes_home.profile("alpha", config={"_raw": CRON_CONFIG})
    (home / "config.yaml").write_text(CRON_CONFIG)
    import dashboard.plugin_api as plugin_api
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app), home


def _canonical_spy(monkeypatch):
    """Record every canonical-seam invocation (profile, path-independent)."""
    import hermes_adapter as ha
    calls = []
    orig = ha.HermesAdapter.apply_canonical_save

    def spy(self, profile, updated, identity=None):
        calls.append({"profile": profile,
                      "keys": sorted(k for k in updated if not isinstance(updated[k], dict))})
        return orig(self, profile, updated, identity=identity)

    monkeypatch.setattr(ha.HermesAdapter, "apply_canonical_save", spy)
    return calls


def test_main_apply_and_undo_invoke_canonical_seam(client, monkeypatch):
    tc, home = client
    calls = _canonical_spy(monkeypatch)
    from unittest.mock import Mock
    from hermes_cli import config
    host_save = Mock(wraps=config.save_config)
    monkeypatch.setattr(config, "save_config", host_save)
    cfg_path = home / "config.yaml"
    pre_doc = yaml.safe_load(cfg_path.read_text())

    r = tc.post("/preview", json={"changes": [
        {"profileId": "default", "slotId": "main",
         "to": {"provider": NEW_PROVIDER, "model": NEW_MODEL}}]})
    assert r.status_code == 200, r.text
    preview_id = r.json()["previewId"]

    r = tc.post("/operations", json={"previewId": preview_id})
    assert r.status_code == 200, r.text
    assert r.json()["operation"]["state"] == "awaiting_host"
    assert calls == []  # Main apply belongs to the host, never the backend writer.
    r = finish_host_main_calls(tc, r, home)
    op = r.json()["operation"]
    assert op["state"] == "committed", op
    assert op["undoable"] is True
    assert calls == []
    host_save.assert_called_once()

    after = yaml.safe_load(cfg_path.read_text())
    assert after["model"]["provider"] == NEW_PROVIDER
    assert after["model"]["default"] == NEW_MODEL  # string, never a route dict

    # undo must ALSO go through the canonical seam (restore, not raw write)
    n_before_undo = len(calls)
    r = tc.post(f"/operations/{op['id']}/undo")
    assert r.status_code == 200, r.text
    assert len(calls) > n_before_undo
    assert all(c["profile"] == "default" for c in calls)
    assert yaml.safe_load(cfg_path.read_text()) == pre_doc
    undo_op = r.json()["operation"]
    assert undo_op["state"] == "undone"
    assert undo_op["undoable"] is False  # no longer committed -> not undoable


def test_cron_pairing_preserves_scheduler(client):
    tc, home = client
    pre_doc = yaml.safe_load((home / "config.yaml").read_text())

    r = tc.post("/preview", json={"changes": [
        {"profileId": "default", "slotId": "cron.model",
         "to": {"provider": NEW_PROVIDER, "model": NEW_MODEL}}]})
    assert r.status_code == 200, r.text
    preview_id = r.json()["previewId"]

    r = tc.post("/operations", json={"previewId": preview_id})
    assert r.status_code == 200, r.text
    op = r.json()["operation"]
    assert op["state"] == "committed", op

    after = yaml.safe_load((home / "config.yaml").read_text())
    # inference pair updated together...
    assert after["cron"]["model"] == NEW_MODEL
    assert after["cron"]["model_provider"] == NEW_PROVIDER
    # ...scheduler untouched, and everything else verbatim
    assert after["cron"]["provider"] == pre_doc["cron"]["provider"]
    assert after["unrelated"] == pre_doc["unrelated"]


def test_public_dto_exposes_only_allowlisted_fields(client):
    tc, home = client
    r = tc.post("/preview", json={"changes": [
        {"profileId": "default", "slotId": "main",
         "to": {"provider": NEW_PROVIDER, "model": NEW_MODEL}}]})
    assert r.status_code == 200, r.text
    r = tc.post("/operations", json={"previewId": r.json()["previewId"]})
    r = finish_host_main_calls(tc, r, home)
    op = r.json()["operation"]
    # The public DTO allowlist (catalog-promotion plan): the operation surface
    # plus the outcome-derived final_state and a fixed failure category — never
    # a raw exception message, snapshot, path or config document.
    assert set(op) <= {"id", "state", "results", "undoable",
                       "final_state", "failure"}
    for res in op["results"]:
        assert set(res) == {"seq", "profileId", "status", "store", "revisionBefore", "revisionAfter"}
    blob = json.dumps(r.json())
    for forbidden in ("snapshot", "providers", "path", "changes",
                      "base_url", "api_key", "OPENAI_API_KEY",
                      "Traceback", "exception"):
        assert forbidden not in blob, forbidden

    # GET round-trip is equally sanitized
    r = tc.get(f"/operations/{op['id']}")
    assert r.status_code == 200
    blob = json.dumps(r.json())
    assert "snapshot" not in blob and '"path"' not in blob


def test_unsupported_store_blocks_never_raw_writes(client, monkeypatch, tmp_path):
    tc, home = client
    # 1) unknown store kind -> ApplyBlocked before any write
    with pytest.raises(ApplyBlocked):
        plan_operation(_journal(home), [{
            "profile": "default", "store_kind": "mystery",
            "path": str(home / "config.yaml"),
            "changes": {"model.default": NEW_MODEL}}])
    assert yaml.safe_load((home / "config.yaml").read_text())["model"][
        "default"] == "gpt-4o-mini"

    # 2) canonical store without an explicit profile -> refused, not raw-YAML
    with pytest.raises(RuntimeError, match="explicit profile"):
        adapter_for("canonical_config", str(home / "config.yaml"))

    # 3) unknown slot / protected slot through the API -> visible 422 block
    r = tc.post("/operations", json={"changes": [
        {"profileId": "default", "slotId": "does.not.exist",
         "to": {"provider": NEW_PROVIDER, "model": NEW_MODEL}}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["blocked"], r.text

    r = tc.post("/operations", json={"changes": [
        {"profileId": "default", "slotId": "judge.threshold",
         "to": {"provider": NEW_PROVIDER, "model": NEW_MODEL}}]})
    assert r.status_code == 422, r.text
    assert any("protected" in b["reason"] for b in r.json()["detail"]["blocked"])
    # protected slot untouched on disk
    assert yaml.safe_load((home / "config.yaml").read_text()) == \
        yaml.safe_load((home / "profiles" / "alpha" / "config.yaml").read_text())


def _journal(home):
    from model_switcher.journal import OperationJournal
    import tempfile
    return OperationJournal(tempfile.mkdtemp(prefix="msw-journal-", dir="/tmp")
                            + "/ops.db")
