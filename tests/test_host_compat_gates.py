"""Host-compatibility gates for route execution.

H1 — auxiliary host-assignability: the rows derived from the runtime's
DEFAULT_CONFIG include tasks the host's POST /api/model/set refuses with 400
"unknown auxiliary task" (background_review, memory_query_rewrite,
moa_aggregator, moa_reference, monitor, tts_audio_tags — the host accepts
only its built-in ``_AUX_TASK_SLOTS`` plus plugin-registered tasks). Those
rows stay visible, but their assignments must route through the backend
public writer, and the preview must reflect that executor. Host-assignable
built-ins and plugin-registered tasks keep their host step.

H2 — main provider canonicalization: the host persists its OWN canonical
provider id (aliases resolved, lowercased) for a main assignment, so the
journal must record the canonical id derived from the host options
evidence — never the typed string — or readback misreads the host's write
as a foreign edit. A provider the supplied catalog cannot resolve is
unprovable and blocks with a fixed reason instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("hermes_cli.config_defaults")

import dashboard.plugin_api as plugin_api

AUX_CFG = (
    "model:\n"
    "  provider: lablocal\n"
    "  default: chat\n"
    "providers:\n"
    "  lablocal:\n"
    "    base_url: http://127.0.0.1:8087/v1\n"
    "auxiliary:\n"
    "  tts_audio_tags:\n"
    "    provider: lablocal\n"
    "    model: old-model\n"
    "  review:\n"
    "    provider: lablocal\n"
    "    model: old-model\n"
    "  title_generation:\n"
    "    provider: lablocal\n"
    "    model: old-model\n"
    "  profile_task:\n"
    "    provider: lablocal\n"
    "    model: old-model\n"
)

MAIN_CFG = (
    "model:\n"
    "  provider: openai\n"
    "  default: old-model\n"
    "providers:\n"
    "  lablocal:\n"
    "    base_url: http://127.0.0.1:8087/v1\n"
)

PROFILE_TASK_HOST = {
    "profiles": {
        "alpha": {"auxiliary": {"tasks": [{
            "task": "profile_task", "label": "Profile task", "hint": "",
            "plugin": "profile-plugin", "inherit_from": None,
            "provider": "lablocal", "model": "old-model", "base_url": "",
            "reasoning_effort": None, "local_endpoint": False,
        }]}},
    },
}

ANTHROPIC_OPTIONS = {"providers": [
    {"slug": "anthropic", "name": "Anthropic", "authenticated": True,
     "models": ["claude-x"]},
]}


def _client():
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def _preview(tc, changes, host=None):
    payload = {"changes": changes}
    if host is not None:
        payload["host"] = host
    r = tc.post("/preview", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["preview"]


def _simulate_host_main_write_canonical(tc, response, home, adapter,
                                        canonical_provider, persisted_model=None):
    """Browser/host stand-in: the host persists its OWN canonical provider id
    (what hermes_cli.providers.normalize_provider resolves at write time),
    which can differ in case/alias from the typed value the call carried, and
    may normalize the model id (hermes_cli.model_normalize)."""
    from hermes_cli.config import load_config, save_config

    envelope = response.json()
    call = envelope["hostCall"]
    assert call["method"] == "POST" and call["path"] == "/api/model/set"
    assert call["body"]["scope"] == "main"
    profile = call["query"]["profile"]
    with adapter.scoped(profile):
        cfg = load_config()
        cfg["model"].update(provider=canonical_provider,
                            default=persisted_model or call["body"]["model"])
        save_config(cfg)
    return tc.post(f"/operations/{call['opId']}/host-result", json={
        "seq": call["seq"], "token": call["token"],
        "outcome": "ok", "status": 200})


# ------------------------------------------------------------------------ H1

def test_host_refused_aux_task_row_visible_but_routes_backend(hermes_home, adapter):
    hermes_home.make_profile("alpha", {"_raw": AUX_CFG})
    tc = _client()
    cfg_path = hermes_home.path / "profiles" / "alpha" / "config.yaml"

    # The row stays visible (derived from the runtime DEFAULT_CONFIG).
    r = tc.get("/state")
    assert r.status_code == 200, r.text
    slot_ids = [s["slotId"] for s in r.json()["slots"]
                if s["profileId"] == "alpha"]
    assert "auxiliary.tts_audio_tags" in slot_ids, slot_ids

    changes = [{"profileId": "alpha", "slotId": "auxiliary.tts_audio_tags",
                "action": "set",
                "to": {"provider": "lablocal", "model": "new-model"}}]
    preview = _preview(tc, changes)
    assert not preview.get("blocked"), preview
    entry = preview["changes"][0]
    # The host route refuses this task with 400; the backend writer applies it.
    assert entry["executor"] == "backend", entry

    applied = tc.post("/operations", json={"changes": changes})
    assert applied.status_code == 200, applied.text
    op = applied.json()["operation"]
    assert op["state"] == "committed", applied.text
    after = yaml.safe_load(cfg_path.read_text())
    assert after["auxiliary"]["tts_audio_tags"]["model"] == "new-model"
    assert after["auxiliary"]["tts_audio_tags"]["provider"] == "lablocal"


def test_host_assignable_builtin_aux_task_keeps_host_step(hermes_home, adapter):
    hermes_home.make_profile("alpha", {"_raw": AUX_CFG})
    tc = _client()
    changes = [{"profileId": "alpha", "slotId": "auxiliary.title_generation",
                "action": "set",
                "to": {"provider": "lablocal", "model": "new-model"}}]
    preview = _preview(tc, changes)
    assert not preview.get("blocked"), preview
    entry = preview["changes"][0]
    # "title_generation" is a host built-in slot: the exact host step still applies.
    assert entry["executor"] == "host", entry

    applied = tc.post("/operations", json={"changes": changes})
    assert applied.status_code == 200, applied.text
    assert applied.json()["operation"]["state"] == "awaiting_host", applied.text
    call = applied.json()["hostCall"]
    assert call["body"]["scope"] == "auxiliary"
    assert call["body"]["task"] == "title_generation"


def test_plugin_registered_aux_task_keeps_host_step(hermes_home, adapter):
    hermes_home.make_profile("alpha", {"_raw": AUX_CFG})
    tc = _client()
    changes = [{"profileId": "alpha", "slotId": "auxiliary.profile_task",
                "action": "set",
                "to": {"provider": "lablocal", "model": "new-model"}}]
    preview = _preview(tc, changes, host=PROFILE_TASK_HOST)
    assert not preview.get("blocked"), preview
    entry = preview["changes"][0]
    # Plugin-registered tasks are host-assignable (host _aux_task_slots()).
    assert entry["executor"] == "host", entry


# ------------------------------------------------------------------------ H2

def test_main_journals_the_host_canonical_provider(hermes_home, adapter):
    hermes_home.make_profile("alpha", {"_raw": MAIN_CFG})
    tc = _client()
    cfg_path = hermes_home.path / "profiles" / "alpha" / "config.yaml"
    host = {"profiles": {"alpha": {"options": ANTHROPIC_OPTIONS}}}
    changes = [{"profileId": "alpha", "slotId": "main", "action": "set",
                "to": {"provider": "Anthropic", "model": "claude-x"}}]

    preview = _preview(tc, changes, host=host)
    assert not preview.get("blocked"), preview

    applied = tc.post("/operations", json={"changes": changes, "host": host})
    assert applied.status_code == 200, applied.text
    assert applied.json()["operation"]["state"] == "awaiting_host", applied.text

    # The host canonicalizes "Anthropic" -> its catalog id "anthropic" when
    # persisting; the journal must agree or readback flags a foreign edit.
    completed = _simulate_host_main_write_canonical(
        tc, applied, hermes_home.path, adapter, "anthropic")
    assert completed.status_code == 200, completed.text
    op = completed.json()["operation"]
    assert op["state"] == "committed", completed.text
    stored = yaml.safe_load(cfg_path.read_text())
    assert stored["model"]["provider"] == "anthropic"
    assert stored["model"]["default"] == "claude-x"


def test_main_blocks_provider_unresolvable_from_the_supplied_catalog(hermes_home):
    hermes_home.make_profile("alpha", {"_raw": MAIN_CFG})
    tc = _client()
    cfg_path = hermes_home.path / "profiles" / "alpha" / "config.yaml"
    before = cfg_path.read_bytes()
    host = {"profiles": {"alpha": {"options": ANTHROPIC_OPTIONS}}}
    changes = [{"profileId": "alpha", "slotId": "main", "action": "set",
                "to": {"provider": "google", "model": "gemini-2.5-pro"}}]

    preview = _preview(tc, changes, host=host)
    assert preview.get("blocked"), preview
    assert any("host catalog" in str(b.get("reason")) for b in preview["blocked"]), \
        preview["blocked"]

    applied = tc.post("/operations", json={"changes": changes, "host": host})
    assert applied.status_code == 409, applied.text
    assert applied.json()["detail"].get("blocked") is True, applied.text
    assert cfg_path.read_bytes() == before


def test_main_noop_uses_the_canonical_provider_identity(hermes_home):
    hermes_home.make_profile("alpha", {"_raw": MAIN_CFG})
    tc = _client()
    host = {"profiles": {"alpha": {"options": ANTHROPIC_OPTIONS}}}
    # Stored route is already the canonical assignment; the typed alias
    # spelling of the same assignment is a no-op, never a new host call.
    cfg_path = hermes_home.path / "profiles" / "alpha" / "config.yaml"
    doc = yaml.safe_load(cfg_path.read_text())
    doc["model"] = {"provider": "anthropic", "default": "claude-x"}
    cfg_path.write_text(yaml.safe_dump(doc))

    changes = [{"profileId": "alpha", "slotId": "main", "action": "set",
                "to": {"provider": "Anthropic", "model": "claude-x"}}]
    preview = _preview(tc, changes, host=host)
    assert preview.get("blocked"), preview
    assert any("no-op" in str(b.get("reason")) for b in preview["blocked"]), \
        preview["blocked"]


def test_main_typed_uncatalogued_model_respelled_by_host_stops_for_review(
        hermes_home, adapter):
    """A typed model id the host catalog does not list may be respelled by the
    host on save (dot/hyphen, vendor prefix, case). The plugin cannot prove
    that respelling is its own write, so it must stop for review -- never
    commit a value it did not plan, and never compensate blindly over it. The
    preview warns about this before anything is written."""
    hermes_home.make_profile("alpha", {"_raw": MAIN_CFG})
    tc = _client()
    cfg_path = hermes_home.path / "profiles" / "alpha" / "config.yaml"
    host = {"profiles": {"alpha": {"options": ANTHROPIC_OPTIONS}}}
    changes = [{"profileId": "alpha", "slotId": "main", "action": "set",
                "to": {"provider": "anthropic", "model": "claude-y.1"}}]
    preview = _preview(tc, changes, host=host)
    assert not preview.get("blocked"), preview
    assert "different spelling" in json.dumps(preview), preview
    applied = tc.post("/operations", json={"changes": changes, "host": host})
    assert applied.status_code == 200, applied.text
    completed = _simulate_host_main_write_canonical(
        tc, applied, hermes_home.path, adapter, "anthropic",
        persisted_model="claude-y-1")
    assert completed.status_code == 200, completed.text
    op = completed.json()["operation"]
    assert op["state"] == "recovery_required", completed.text
    assert op["undoable"] is False
    # The host's write is left in place, never blindly reverted.
    assert yaml.safe_load(cfg_path.read_text())["model"]["default"] == "claude-y-1"


def test_main_catalogued_model_still_requires_exact_readback(hermes_home, adapter):
    """H2b control: a catalogued id IS the host's canonical spelling, so a
    different persisted id is not our write and must never commit."""
    hermes_home.make_profile("alpha", {"_raw": MAIN_CFG})
    tc = _client()
    host = {"profiles": {"alpha": {"options": ANTHROPIC_OPTIONS}}}
    changes = [{"profileId": "alpha", "slotId": "main", "action": "set",
                "to": {"provider": "anthropic", "model": "claude-x"}}]
    applied = tc.post("/operations", json={"changes": changes, "host": host})
    assert applied.status_code == 200, applied.text
    completed = _simulate_host_main_write_canonical(
        tc, applied, hermes_home.path, adapter, "anthropic",
        persisted_model="something-else")
    assert completed.status_code == 200, completed.text
    assert completed.json()["operation"]["state"] != "committed", completed.text
