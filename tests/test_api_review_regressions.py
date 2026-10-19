"""Review-round regression tests for dashboard/plugin_api.py.

One test per finding from the deeper API audit; written RED, then made GREEN.
All state lives in throwaway Hermes homes minted under /tmp by the conftest
``hermes_home`` fixture — no live profile, no inference, no real credentials.

Covered findings:
  A1 typed chain payload: preview forwards the FULL typed ``to`` (incl. the
     ordered ``chain``) to route_inventory — never narrowed to provider/model.
  A2 allowlisted public diff: a route-dict value never leaks protected/
     secret neighbours (threshold, api_key, ...) into a preview diff.
  A3 preview/apply parity: the preview diff is derived from the SAME prepared
     mutation an apply runs (one code path), and applying a previewId executes
     exactly that cached mutation.
  A4 revision- and identity-bound preview with expiry: an apply whose store
     revision or profile identity moved on (or an expired preview) is refused
     with an explicit stale/expired 409 — never replayed.
  A5 exact preset scope/preview match: presetId + previewId must describe the
     same scope; a mismatch is rejected instead of silently ignoring the
     preview.
  A6 inventory correctness: scalar slots carry their provider/model, the state
     vocabulary is configured|inherit|unset, and lastObserved is genuinely
     unobserved (never a copy of the configured route).
  A7 removal correctness: a route-dict slot removal unsets ONLY the route
     fields (neighbours survive) and is bound to the reviewed server snapshot.
  A8 subset preset scope: an existing-but-unselected profile never blocks an
     alpha-only preset, while a truly-new profile still does.
  A9 resource cleanup: the apply path closes its journal connection.
  A10 corrupt preset store is quarantined and surfaced, never silently empty.
  A11 GET /models: truthful catalog passthrough, no-store, unknown profile is
      a visible error.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import route_inventory as ri  # noqa: E402
from model_switcher.journal import OperationJournal  # noqa: E402

import dashboard.plugin_api as plugin_api  # noqa: E402


BASE_CONFIG = """\
model:
  provider: openai
  default: gpt-4o-mini
providers:
  lablocal:
    base_url: http://127.0.0.1:8080/v1
auxiliary:
  goal_judge:
    provider: judgeprov
    model: j/old
    threshold: 0.8
    api_key: sk-should-never-leak
unrelated:
  keep_me: yes
"""


def _client():
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="")
    return TestClient(app)


def _m(cp, name, *, env=True, config=BASE_CONFIG):
    home = cp.path
    pdir = home if name == "default" else home / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(config)
    if env:
        (pdir / ".env").write_text("FAKE_LAB_PROVIDER_KEY=lab-only\n")
    return pdir / "config.yaml"


def _cfg_of(home, profile):
    if profile in (None, "default"):
        return home / "config.yaml"
    return home / "profiles" / profile / "config.yaml"


@pytest.fixture(autouse=True)
def _clear_preview_cache():
    plugin_api._PREVIEW_CACHE.clear()
    yield
    plugin_api._PREVIEW_CACHE.clear()


# --------------------------------------------------------- A1 typed chain

def test_preview_forwards_typed_chain_payload_verbatim(hermes_home, monkeypatch):
    _m(hermes_home, "default")
    captured = {}
    real = plugin_api.preview_changes

    def spy(cfg, edits):
        captured["value"] = dict(edits[0].value)
        return real(cfg, edits)

    monkeypatch.setattr(plugin_api, "preview_changes", spy)
    tc = _client()
    chain = [{"provider": "lablocal", "model": "lab-7b-instruct"},
             {"provider": "openai", "model": "gpt-4o-mini"}]
    r = tc.post("/preview", json={"changes": [{
        "profileId": "default", "slotId": "delegation.fallback_chain",
        "action": "set", "to": {"chain": chain}}]})
    assert r.status_code == 200, r.text
    # the typed payload (with its chain) reached route_inventory unchanged
    assert captured["value"].get("chain") == chain


def test_typed_payload_drops_foreign_keys_but_keeps_chain():
    payload = plugin_api._typed_payload(
        {"provider": "p", "model": "m", "chain": [{"provider": "a", "model": "b"}],
         "threshold": 0.8, "__secret": "x"})
    assert payload == {"provider": "p", "model": "m",
                       "chain": [{"provider": "a", "model": "b"}]}


# ------------------------------------------------------ A2 allowlisted diff

def test_public_diff_value_strips_protected_and_secret_neighbours():
    value = {"provider": "p", "model": "m", "threshold": 0.8,
             "api_key": "sk-secret", "enforcement": "strict"}
    assert plugin_api._public_diff_value(value, "auxiliary.goal_judge") == \
        {"provider": "p", "model": "m"}


def test_preview_never_leaks_secret_neighbour(hermes_home):
    _m(hermes_home, "default")
    tc = _client()
    r = tc.post("/preview", json={"changes": [{
        "profileId": "default", "slotId": "main", "action": "set",
        "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    assert r.status_code == 200, r.text
    blob = json.dumps(r.json())
    assert "sk-should-never-leak" not in blob
    assert "threshold" not in blob


# --------------------------------------------------- A3 preview/apply parity

def test_preview_then_apply_executes_the_same_prepared_mutation(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    tc = _client()
    change = {"profileId": "default", "slotId": "main", "action": "set",
              "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}
    r = tc.post("/preview", json={"changes": [change]})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    pid = r.json()["previewId"]
    # the cached entry is the prepared mutation, not the raw request
    entry = plugin_api._PREVIEW_CACHE[pid]
    assert entry["targets"], entry
    assert isinstance(entry["targets"][0]["changes"], dict)

    # apply with ONLY the previewId (the UI's non-preset path)
    r = tc.post("/operations", json={"previewId": pid})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert r.json()["operation"]["state"] == "committed", r.json()
    doc = yaml.safe_load(_cfg_of(home, "default").read_text())
    assert doc["model"]["provider"] == "lablocal"
    assert doc["model"]["default"] == "lab-7b-instruct"
    # protected neighbour survives the apply
    assert doc["auxiliary"]["goal_judge"]["threshold"] == 0.8


# ------------------------------------- A4 revision/identity bound preview

def test_apply_refuses_preview_when_store_revision_moved(hermes_home):
    home = hermes_home.path
    cfg_path = _m(hermes_home, "default")
    tc = _client()
    r = tc.post("/preview", json={"changes": [{
        "profileId": "default", "slotId": "main", "action": "set",
        "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    pid = r.json()["previewId"]

    # an intervening write moves the store revision on
    text = cfg_path.read_text()
    cfg_path.write_text(text + "\nextra: 1\n")

    r = tc.post("/operations", json={"previewId": pid})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["stale"] is True and detail["blocked"] is True, detail
    # nothing was written
    assert "extra: 1" in cfg_path.read_text()
    assert "lablocal" not in yaml.safe_load(cfg_path.read_text())["model"].get(
        "provider", "")


def test_apply_refuses_expired_preview(hermes_home):
    _m(hermes_home, "default")
    tc = _client()
    r = tc.post("/preview", json={"changes": [{
        "profileId": "default", "slotId": "main", "action": "set",
        "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    pid = r.json()["previewId"]
    plugin_api._PREVIEW_CACHE[pid]["expires_at"] = time.time() - 1

    r = tc.post("/operations", json={"previewId": pid})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["stale"] is True


def test_apply_unknown_preview_is_404(hermes_home):
    _m(hermes_home, "default")
    tc = _client()
    r = tc.post("/operations", json={"previewId": "pv-does-not-exist"})
    assert r.status_code == 404, r.text


# --------------------------------------- A5 exact preset scope/preview match

def test_apply_accepts_matching_preset_and_preview(hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    changes = [{"profileId": p, "slotId": "main", "action": "set",
                "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}
               for p in ("default", "alpha")]
    r = tc.post("/presets", json={"name": "fleet", "changes": changes})
    assert r.status_code == 200, r.text
    pid = r.json()["preset"]["id"]
    r = tc.post("/preview", json={"changes": changes})
    preview_id = r.json()["previewId"]

    r = tc.post("/operations", json={"presetId": pid, "previewId": preview_id})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert r.json()["operation"]["state"] == "committed"


def test_apply_rejects_preview_that_does_not_match_preset_scope(hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    preset_changes = [{"profileId": p, "slotId": "main", "action": "set",
                       "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}
                      for p in ("default", "alpha")]
    r = tc.post("/presets", json={"name": "fleet", "changes": preset_changes})
    pid = r.json()["preset"]["id"]

    # preview a DIFFERENT scope (different profile + slot) that is itself clean,
    # so the refusal under test is the SCOPE mismatch — not a blocked preview.
    other = [{"profileId": "alpha", "slotId": "auxiliary.compression",
              "action": "set",
              "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]
    r = tc.post("/preview", json={"changes": other})
    assert r.status_code == 200, r.text
    assert not r.json()["preview"]["blocked"], r.text
    preview_id = r.json()["previewId"]

    r = tc.post("/operations", json={"presetId": pid, "previewId": preview_id})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["blocked"] is True
    assert "scope" in r.json()["detail"]["error"]


# ----------------------------------------------------- A6 inventory fields

def _inv_slots(profile="default"):
    return plugin_api._profile_inventory()[profile]["slots"]


def test_scalar_slot_reports_provider_and_model(hermes_home):
    _m(hermes_home, "default", config=(
        BASE_CONFIG + "delegation:\n  provider: dele\n  model: del-model\n"))
    slot = next(s for s in _inv_slots() if s["slotId"] == "delegation.model")
    assert slot["model"] == "del-model"
    assert slot["provider"] == "dele"
    assert slot["state"] == "configured"


def test_inventory_state_names_and_unobserved(hermes_home):
    _m(hermes_home, "default")  # no delegation block -> absent/deferred slot
    slots = _inv_slots()
    assert {s["state"] for s in slots} <= {"configured", "inherit", "unset"}
    # lastObserved must be genuinely unobserved, never a copy of configured
    for s in slots:
        assert s["lastObserved"] is None, s
    # an absent scalar slot is never reported as "configured"; its resolved
    # state is one of the non-configured names (inherit shows the effective
    # route it inherits — still not a fabricated configured route).
    deleg = next(s for s in slots if s["slotId"] == "delegation.model")
    assert deleg["state"] in ("unset", "inherit")
    assert deleg["state"] != "configured"


# ------------------------------------------------------ A7 removal identity

def _goal_judge_desc(cfg):
    return next(o.descriptor for o in ri.build_inventory(cfg)
                if o.descriptor.storage_path == "auxiliary.goal_judge")


def test_route_dict_removal_unets_only_route_fields_preserving_neighbours():
    cfg = yaml.safe_load(BASE_CONFIG)
    desc = _goal_judge_desc(cfg)
    changes = plugin_api._removal_changes_for(
        desc, cfg, {"provider": "judgeprov", "model": "j/old"})
    assert set(changes) == {"auxiliary.goal_judge.provider",
                            "auxiliary.goal_judge.model"}
    assert all(v is None for v in changes.values())
    # the whole object is never dropped
    assert "auxiliary.goal_judge" not in changes


def test_route_dict_removal_blocks_on_stale_reviewed_route():
    cfg = yaml.safe_load(BASE_CONFIG)
    desc = _goal_judge_desc(cfg)
    with pytest.raises(ValueError, match="changed since review"):
        plugin_api._removal_changes_for(
            desc, cfg, {"provider": "judgeprov", "model": "SOMETHING-ELSE"})


# --------------------------------------------------- A8 subset preset scope

def test_existing_unselected_profile_does_not_block_preset(hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    _m(hermes_home, "beta")  # exists at review, NOT selected
    tc = _client()
    r = tc.post("/presets", json={"name": "alpha-only", "changes": [
        {"profileId": "alpha", "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    assert r.status_code == 200, r.text
    assert r.json()["preset"]["profileIds"] == ["alpha"]
    pid = r.json()["preset"]["id"]

    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert r.json()["operation"]["state"] == "committed"


def test_truly_new_profile_still_blocks_preset(hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    r = tc.post("/presets", json={"name": "fleet", "changes": [
        {"profileId": p, "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}
        for p in ("default", "alpha")]})
    pid = r.json()["preset"]["id"]

    _m(hermes_home, "gamma")  # discovered AFTER review
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 409, r.text
    reasons = r.json()["detail"]["reasons"]
    assert any("gamma" in reason and "new profile" in reason for reason in reasons), \
        reasons


# ---------------------------------------------------- A9 resource cleanup

def test_apply_closes_its_journal(hermes_home, monkeypatch):
    _m(hermes_home, "default")
    closed = {"n": 0}

    class TrackingJournal(OperationJournal):
        def close(self):
            closed["n"] += 1
            super().close()

    monkeypatch.setattr(plugin_api, "OperationJournal", TrackingJournal)
    tc = _client()
    r = tc.post("/preview", json={"changes": [{
        "profileId": "default", "slotId": "main", "action": "set",
        "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    pid = r.json()["previewId"]
    r = tc.post("/operations", json={"previewId": pid})
    assert r.status_code == 200, r.text
    assert closed["n"] >= 1


# ---------------------------------------------- A10 corrupt preset store

def test_corrupt_preset_store_is_quarantined_and_surfaced(hermes_home):
    _m(hermes_home, "default")
    state_dir = plugin_api._state_dir()
    path = state_dir / "presets.json"
    path.write_text("{ not: valid json ")
    tc = _client()
    r = tc.get("/state")
    assert r.status_code == 200, r.text
    assert r.json()["presetsCorrupt"] is True
    assert r.json()["presets"] == []
    # the corrupt file is moved aside, never silently deleted
    quarantined = list(state_dir.glob("presets.json.corrupt-*"))
    assert quarantined, sorted(p.name for p in state_dir.iterdir())


# ---------------------------------------------------------- A11 /models

def test_get_models_passthrough_no_store_and_unknown_profile(hermes_home, monkeypatch):
    _m(hermes_home, "default")
    mod = plugin_api._model_catalog_module()
    monkeypatch.setattr(mod, "_load_picker_payload", lambda refresh: {
        "providers": [{"slug": "lablocal", "name": "Lab Local",
                       "models": ["lab-7b-instruct", "lab-9b"],
                       "authenticated": True}]})
    tc = _client()
    r = tc.get("/models", params={"provider": "lablocal", "profileId": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert [m["id"] for m in body["models"]] == ["lab-7b-instruct", "lab-9b"]
    assert r.headers.get("cache-control") == "no-store"

    # an unknown provider is a truthful visible failure, not an empty success
    r = tc.get("/models", params={"provider": "nope", "profileId": "default"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["error"]

    # an unknown profile is refused before any catalog lookup
    r = tc.get("/models", params={"provider": "lablocal", "profileId": "ghost"})
    assert r.json()["ok"] is False
    assert "unknown profile" in r.json()["error"]


# ----------------------- A12 exception text never reaches a preview response

def test_preview_never_echoes_exception_text(hermes_home, monkeypatch):
    """A failing seam must not surface str(exc) to the caller (py/stack-trace-exposure).

    RED against the previous revision: the blocked reason carried the full
    RuntimeError text (including the sentinel values below) back to the client.
    """
    _m(hermes_home, "default")

    def boom(cfg, to):
        raise RuntimeError("leaked-secret /etc/passwd token=sk-live-123")

    monkeypatch.setattr(plugin_api, "_canonical_main_changes", boom)
    tc = _client()
    r = tc.post("/preview", json={"changes": [{
        "profileId": "default", "slotId": "main.default", "action": "set",
        "to": {"provider": "nous", "model": "hermes-4"}}]})

    assert r.status_code == 200, r.text
    assert "leaked-secret" not in r.text
    assert "/etc/passwd" not in r.text
    assert "sk-live-123" not in r.text
    assert "route rejected (RuntimeError)" in r.text


def test_preview_removal_refusal_uses_a_fixed_reason(hermes_home, monkeypatch):
    """Unknown removal ValueErrors are reported generically, never verbatim."""
    _m(hermes_home, "default")

    def boom(desc, cfg, from_payload=None):
        raise ValueError("leaked-secret internal path /srv/private")

    monkeypatch.setattr(plugin_api, "_removal_changes_for", boom)
    tc = _client()
    r = tc.post("/preview", json={"changes": [{
        "profileId": "default", "slotId": "delegation.fallback_chain", "action": "remove",
        "to": None}]})

    assert r.status_code == 200, r.text
    assert "leaked-secret" not in r.text
    assert "/srv/private" not in r.text
    assert "removal rejected for this slot" in r.text
