"""Focused RED regressions for the backend review-batch defects:

1. The vision aux note states the ``auto`` image_input_mode -> text effect,
   not only download_timeout.
2. Plugin-registered aux task keys cannot shadow cross-cutting policy scalars
   (openrouter_model / free_only / ...) — one row per setting, truthful.
3. The scalar ``auxiliary.openrouter_model`` override ROUTES THROUGH
   OpenRouter: a non-empty value references the openrouter provider, so the
   provider-scoped credential gate blocks it without profile credentials.
4. Runtime aux discovery failures surface as truthful warnings (fail closed),
   never as silently-vanished rows.
"""
from __future__ import annotations

import pytest

import route_inventory as ri

pytest.importorskip("hermes_cli.config_defaults")


from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from dashboard import plugin_api as pa
    app = FastAPI()
    app.include_router(pa.router)
    return TestClient(app)


def _host_aux(tasks):
    return {"host": {"profiles": {"default": {"auxiliary": {"tasks": tasks}}}}}


def _plugin_entry(key: str, *, label: str = "Demo task",
                  description: str = "demo description",
                  owner: str = "review_plug"):
    """A row from the public GET /api/model/auxiliary response."""
    return {"task": key, "label": label, "hint": description,
            "plugin": owner, "provider": "", "model": ""}


# 1 ---------------------------------------------------------- vision note

def test_vision_note_states_auto_image_input_mode_text_effect():
    note = next(o.descriptor.notes
                for o in ri.build_inventory({})
                if o.descriptor.slot_id == "auxiliary.vision")
    low = note.lower()
    assert "download_timeout" in low
    # An explicit auxiliary.vision override wins even when the main model
    # supports vision. This is not merely non-vision-model degradation.
    assert "image_input_mode" in low
    assert "auto" in low and "text" in low
    assert "explicit" in low and "even" in low


def test_vision_override_note_reaches_public_state(hermes_home):
    from agent import image_routing
    r = _client().post("/state", json=_host_aux([]))
    assert r.status_code == 200, r.text
    row = next(s for s in r.json()["slots"]
               if s["profileId"] == "default" and s["slotId"] == "auxiliary.vision")
    assert "explicit" in row["note"].lower()
    assert "even" in row["note"].lower()
    assert image_routing.decide_image_input_mode(
        "openai", "main-vision-model",
        {"agent": {"image_input_mode": "auto"},
         "auxiliary": {"vision": {"provider": "openai", "model": "vision-model"}}}) == "text"


# 2 ------------------------------------------- no scalar shadowing by plugins

def test_plugin_aux_entries_cannot_shadow_cross_cutting_scalars():
    entries = [_plugin_entry("openrouter_model"),
               _plugin_entry("transient_retries"),
               _plugin_entry("goal_judge"),
               _plugin_entry("session_search"),
               _plugin_entry("mytask")]
    rows = ri.build_inventory({}, host_aux={"tasks": entries})
    ids = [o.descriptor.slot_id for o in rows]
    # exactly ONE openrouter override row (the built-in scalar slot)
    assert ids.count("auxiliary.openrouter_model") == 1
    assert "auxiliary.mytask" in ids
    for shadowed in ("auxiliary.transient_retries", "auxiliary.goal_judge",
                     "auxiliary.session_search"):
        assert shadowed not in ids


# 3 ------------------------------- scalar OpenRouter override = real routing

def test_openrouter_scalar_references_openrouter_provider():
    from dashboard import plugin_api as pa
    assert pa._referenced_providers(
        {"auxiliary.openrouter_model": "nvidia/x:free"}) == ["openrouter"]
    assert pa._referenced_providers(
        {"auxiliary.openrouter_model": "anthropic/claude-3-haiku"}) == \
        ["openrouter"]
    # unset/empty stays vacuous (no route is written)
    assert pa._referenced_providers({"auxiliary.openrouter_model": ""}) == []


def test_openrouter_scalar_edit_blocked_without_profile_credentials(
        hermes_home):
    pdir = hermes_home.make_profile("alpha")
    before = (pdir / "config.yaml").read_text()
    r = _client().post("/operations", json={"changes": [
        {"profileId": "alpha", "slotId": "auxiliary.openrouter_model",
         "action": "set", "to": {"model": "a/b"}}],
        "host": {"profiles": {"alpha": {"options": {"providers": [
            {"slug": "openrouter", "authenticated": False}]}}}},
    })
    assert r.status_code == 409, r.text
    assert "missing provider credentials" in r.json()["detail"]["error"]
    assert (pdir / "config.yaml").read_text() == before


def test_openrouter_scalar_edit_allowed_with_declared_profile_key(hermes_home):
    from dashboard import plugin_api as pa
    pdir = hermes_home.make_profile("alpha", {
        "_raw": "model:\n  provider: openai\n  default: m\n"
                "providers:\n  openrouter:\n    key_env: OPENROUTER_API_KEY\n"})
    (pdir / ".env").write_text("OPENROUTER_API_KEY=lab-only\n")
    r = _client().post("/operations", json={"changes": [
        {"profileId": "alpha", "slotId": "auxiliary.openrouter_model",
         "action": "set", "to": {"model": "nvidia/x:free"}}]})
    assert r.status_code == 200, r.text
    assert r.json()["operation"]["state"] == "committed", r.text
    import yaml
    assert yaml.safe_load((pdir / "config.yaml").read_text())["auxiliary"][
        "openrouter_model"] == "nvidia/x:free"
    assert pa._route_cost_class("alpha", {
        "auxiliary.openrouter_model": "nvidia/x:free"}) == "metered"


# 4 ------------------------------------- DTO projection + discovery warnings

@pytest.mark.parametrize("auxiliary, expected", [
    (None, "not supplied"),
    ({"tasks": "unavailable"}, "malformed"),
])
def test_runtime_aux_discovery_failure_surfaces_as_warning(hermes_home,
                                                           auxiliary, expected):
    """Missing or failed host discovery hides plugin rows with a warning;
    built-in inventory must still render through the public state route."""
    body = {"host": {"profiles": {"default": {"auxiliary": auxiliary}}}}
    r = _client().post("/state", json=body)
    assert r.status_code == 200, r.text
    profile = next(p for p in r.json()["profiles"] if p["id"] == "default")
    assert profile["slots"]
    warnings = profile.get("inventoryWarnings") or []
    assert any("host auxiliary" in w and expected in w and "hidden" in w
               for w in warnings), profile


def test_config_read_success_keeps_inventory_warning_free(hermes_home):
    """A successful host discovery (including no plugins) is not a failure,
    and warnings must not leak between requests."""
    tc = _client()
    missing = tc.get("/state")
    assert missing.status_code == 200, missing.text
    assert missing.json()["profiles"][0]["inventoryWarnings"]
    for tasks in ([], [_plugin_entry("mytask")]):
        r = tc.post("/state", json=_host_aux(tasks))
        assert r.status_code == 200, r.text
        profile = next(p for p in r.json()["profiles"] if p["id"] == "default")
        assert profile.get("inventoryWarnings") in (None, []), profile
        ids = [s["slotId"] for s in profile["slots"]]
        assert ("auxiliary.mytask" in ids) == bool(tasks)
