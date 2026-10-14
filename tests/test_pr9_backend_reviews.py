"""Focused RED regressions for the PR #9 backend review findings.

Owns: route_inventory.py, dashboard/plugin_api.py and backend tests.

1.  Codex P1 #4270199726 — the DEFAULT (unset -> edge) TTS row was
    non-selectable, so a user whose TTS resolves to edge could not switch it to
    a model-bearing backend (OpenAI/ElevenLabs) even though a requested-provider
    preview supports the switch. The fix keeps the CURRENT no-model path
    read-only (``writable=False``, real ``tts.edge`` storage path, no invented
    ``tts.edge.model`` key) and adds an additive capability contract:
    ``selectable`` (an explicit assignment/backend switch is permitted) and
    ``requiresModelBackend`` (only a set with an explicit, supported
    provider+model — never an unset/disable/chain/remove of the model-less
    section).
2.  Codex P2 #4270199734 / Claude 1 — the process-global
    ``_inventory_warnings`` accumulation is replaced by per-build warnings (no
    cross-request/next-state contamination; true concurrent builds isolated).
    A failed discovery warning never leaks raw exception strings.
3.  Claude 2 — plugin auxiliary-task registry retrieval must be scoped to the
    profile being inventoried (installed ``hermes_cli.plugins.get_plugin_manager``
    keys its manager on the context-local Hermes home), never the process-active
    home.
4.  Claude smaller — a plugin ``inherit_from`` cycle (A->B->A) fails closed with
    a truthful warning instead of recursing forever.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import route_inventory as ri

pytest.importorskip("hermes_cli.config_defaults")


def _slot_obs(cfg, slot_id):
    return next(o for o in ri.build_inventory(cfg)
                if o.descriptor.slot_id == slot_id)


def _stored_paths(cfg):
    return {o.descriptor.storage_path for o in ri.build_inventory(cfg)}


# ---------------------------------------------------------------------------
# 1 — default edge TTS row stays SELECTABLE (explicit backend switch)


def test_default_edge_tts_row_is_selectable_but_not_writable():
    d = _slot_obs({}, "specialist.tts").descriptor
    assert d.storage_path == "tts.edge"          # real read-only current path
    assert d.writable is False                    # current no-model path stays readonly
    assert d.selectable is True                   # ...but a switch may be issued
    assert d.requires_model_backend is True       # only an explicit model-bearing backend
    assert "tts.edge.model" not in _stored_paths({})  # no invented key


def test_edge_tts_switch_to_supported_backend_previews_real_paths():
    prev = ri.preview_changes({}, [ri.RouteEdit(
        "specialist.tts", {"provider": "openai", "model": "tts-1"})])
    assert prev.clean, prev.rejected
    paths = {c.storage_path: c.after for c in prev.changes
             if c.storage_path in ("tts.provider", "tts.openai.model")}
    assert paths == {"tts.provider": "openai", "tts.openai.model": "tts-1"}


def test_requires_model_backend_row_refuses_non_route_shapes():
    """Unset / disable / chain / contiguous reset of the model-less section is
    refused: only an explicit supported provider+model may be set."""
    for payload in ({"model": "anything"}, {"provider": "edge", "model": "m"},
                    {"provider": "", "model": "m"}, {}, {"provider": "openai"},
                    {"chain": []}, {"chain": None}, {"model": ""}):
        prev = ri.preview_changes({}, [ri.RouteEdit("specialist.tts", payload)])
        assert not prev.clean, payload
        assert prev.changes == (), payload


def test_no_model_backends_are_selectable_but_not_writable():
    for backend in ("piper", "xai"):
        d = _slot_obs({"tts": {"provider": backend}}, "specialist.tts").descriptor
        assert d.writable is False, backend
        assert d.selectable is True, backend
        assert d.requires_model_backend is True, backend


def test_model_bearing_speech_row_is_writable_and_not_requires_backend():
    d = _slot_obs({"tts": {"provider": "openai"}}, "specialist.tts").descriptor
    assert d.writable is True and d.storage_path == "tts.openai.model"
    assert d.selectable is True
    assert d.requires_model_backend is False


def test_embedding_row_is_neither_selectable_nor_writable():
    d = _slot_obs({}, "specialist.embedding").descriptor
    assert d.writable is False and d.selectable is False


# ---------------------------------------------------------------------------
# 1b — additive DTO contract on the REAL /state inventory (isolated profile)


def test_dto_default_tts_row_exposes_selectable_and_requires_model_backend(
        hermes_home):
    from dashboard import plugin_api as pa
    hermes_home.make_profile("default")
    inv = pa._profile_inventory()
    row = next(s for s in inv["default"]["slots"] if s["slotId"] == "specialist.tts")
    assert row["writable"] is False
    assert row["selectable"] is True
    assert row["requiresModelBackend"] is True
    assert row["storagePath"] == "tts.edge"
    # an ordinary writable row is selectable too, and not model-backend-gated
    main = next(s for s in inv["default"]["slots"] if s["slotId"] == "main")
    assert main["selectable"] is True and main["writable"] is True
    assert main["requiresModelBackend"] is False
    # the read-only embedding row is neither
    emb = next(s for s in inv["default"]["slots"] if s["slotId"] == "specialist.embedding")
    assert emb["selectable"] is False and emb["writable"] is False


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dashboard import plugin_api as pa
    app = FastAPI()
    app.include_router(pa.router, prefix="")
    return TestClient(app)


_BASE = ("model:\n  provider: openai\n  default: gpt-4o-mini\n"
         # a declared custom-endpoint row so the switch needs no hosted credential
         "providers:\n  openai:\n    base_url: http://127.0.0.1:8087/v1\n")


def test_default_tts_switch_saves_and_undoes_on_isolated_profile(hermes_home):
    """Real DTO -> preview -> apply -> undo: an explicit supported backend is
    persisted on the real backend paths and cleanly undone."""
    import yaml
    home = hermes_home.path
    (home / "config.yaml").write_text(_BASE)
    from dashboard import plugin_api as pa

    row = next(s for s in pa._profile_inventory()["default"]["slots"]
               if s["slotId"] == "specialist.tts")
    assert row["writable"] is False and row["selectable"] is True
    assert row["requiresModelBackend"] is True and row["storagePath"] == "tts.edge"

    tc = _client()
    changes = [{"profileId": "default", "slotId": "specialist.tts", "action": "set",
                "to": {"provider": "openai", "model": "tts-1"}}]

    r = tc.post("/preview", json={"changes": changes})
    assert r.status_code == 200, r.text
    change_dto = r.json()["preview"]["changes"][0]
    diff = {d["path"]: d["to"] for d in change_dto["diff"]}
    assert diff.get("tts.provider") == "openai", diff
    assert diff.get("tts.openai.model") == "tts-1", diff

    r = tc.post("/operations", json={"changes": changes})
    assert r.status_code == 200, r.text
    op = r.json()["operation"]
    assert op["state"] == "committed", r.text
    after = yaml.safe_load((home / "config.yaml").read_text())
    assert after["tts"]["provider"] == "openai", after
    assert after["tts"]["openai"]["model"] == "tts-1", after

    r = tc.post(f"/operations/{op['id']}/undo")
    assert r.status_code == 200, r.text
    undone = yaml.safe_load((home / "config.yaml").read_text()) or {}
    assert not undone.get("tts"), undone


# ---------------------------------------------------------------------------
# 2 — per-build warnings, no process-global accumulation


def test_build_inventory_result_is_list_compatible_and_carries_warnings(monkeypatch):
    import hermes_cli.plugins as plugins

    def boom():
        raise RuntimeError("registry scan exploded")

    monkeypatch.setattr(plugins, "get_plugin_auxiliary_tasks", boom)
    first = ri.build_inventory({})
    assert first.warnings, first
    assert any("plugin" in w and "failed" in w for w in first.warnings), first.warnings
    # still a list: existing list-compatible callers keep working
    assert isinstance(first, list)
    assert any(o.descriptor.slot_id == "main.default" for o in first)

    monkeypatch.setattr(plugins, "get_plugin_auxiliary_tasks", lambda: [])
    second = ri.build_inventory({})
    # NO process-global carryover: a prior failed build cannot contaminate the next
    assert second.warnings == [], second.warnings


def test_concurrent_inventory_builds_isolate_warnings(monkeypatch):
    import hermes_cli.plugins as plugins

    local = threading.local()
    barrier = threading.Barrier(2)

    def fake():
        barrier.wait(timeout=10)
        if getattr(local, "fail", False):
            raise RuntimeError("thread discovery boom")
        return []

    monkeypatch.setattr(plugins, "get_plugin_auxiliary_tasks", fake)
    results = {}

    def worker(name, fail):
        local.fail = fail
        results[name] = ri.build_inventory({})

    t1 = threading.Thread(target=worker, args=("fail", True))
    t2 = threading.Thread(target=worker, args=("ok", False))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results["fail"].warnings, results["fail"].warnings
    assert results["ok"].warnings == [], results["ok"].warnings


def test_discovery_failure_warning_is_sanitized(monkeypatch, tmp_path):
    import hermes_cli.plugins as plugins
    secret = "sk-" + "synthetic-canary-" * 3
    private_path = str(tmp_path / "credentials.yaml")

    def boom():
        raise RuntimeError(f"cred {secret} in {private_path}")

    monkeypatch.setattr(plugins, "get_plugin_auxiliary_tasks", boom)
    res = ri.build_inventory({})
    blob = " ".join(res.warnings)
    assert res.warnings and "plugin" in blob.lower()
    assert secret not in blob, blob
    assert private_path not in blob, blob


def test_profile_inventory_warnings_are_scoped_per_profile(hermes_home, monkeypatch):
    from dashboard import plugin_api as pa
    import hermes_cli.plugins as plugins
    from hermes_constants import get_hermes_home

    hermes_home.make_profile("alpha")
    hermes_home.make_profile("beta")

    def fake():
        if Path(get_hermes_home()).name == "alpha":
            raise RuntimeError("alpha discovery boom")
        return []

    monkeypatch.setattr(plugins, "get_plugin_auxiliary_tasks", fake)
    inv = pa._profile_inventory()
    assert inv["alpha"].get("inventoryWarnings")            # alpha failed
    assert inv["beta"].get("inventoryWarnings") in (None, [])  # beta clean


# ---------------------------------------------------------------------------
# 3 — plugin registry retrieval scoped to the profile home being inventoried


def test_plugin_aux_entries_are_scoped_per_profile_home(hermes_home, monkeypatch):
    from dashboard import plugin_api as pa
    import hermes_cli.plugins as plugins
    from hermes_constants import get_hermes_home

    hermes_home.make_profile("alpha")
    hermes_home.make_profile("beta")

    def fake():
        home = Path(get_hermes_home())
        if home.name == "alpha" and home.parent.name == "profiles":
            return [{"key": "alpha_task", "display_name": "Alpha task",
                     "description": "demo", "plugin": "alpha", "defaults": {},
                     "inherit_from": None}]
        return []

    monkeypatch.setattr(plugins, "get_plugin_auxiliary_tasks", fake)
    inv = pa._profile_inventory()
    ids_a = {s["slotId"] for s in inv["alpha"]["slots"]}
    ids_b = {s["slotId"] for s in inv["beta"]["slots"]}
    assert "auxiliary.alpha_task" in ids_a, ids_a
    assert "auxiliary.alpha_task" not in ids_b, ids_b


# ---------------------------------------------------------------------------
# 4 — plugin inherit_from cycle fails closed with a warning


def _entry(key, base):
    return {"key": key, "display_name": key, "description": "", "plugin": "cyc",
            "defaults": {}, "inherit_from": base}


def test_plugin_aux_inherit_cycle_fails_closed_with_warning():
    res = ri.build_inventory({}, plugin_aux_entries=[_entry("a", "b"),
                                                     _entry("b", "a")])
    ids = {o.descriptor.slot_id for o in res}
    assert {"auxiliary.a", "auxiliary.b"} <= ids, ids  # survived, no RecursionError
    assert any("cycle" in w.lower() or "circular" in w.lower() for w in res.warnings), \
        res.warnings


def test_plugin_aux_self_referential_inherit_is_harmless():
    res = ri.build_inventory({}, plugin_aux_entries=[_entry("solo", "solo")])
    assert "auxiliary.solo" in {o.descriptor.slot_id for o in res}


# ---------------------------------------------------------------------------
# notes-empty UI truthiness is harmless (verify only — no fix required)


def test_empty_notes_is_falsy_and_safe_for_ui_truthiness(hermes_home):
    from dashboard import plugin_api as pa
    hermes_home.make_profile("default")
    main = next(s for s in pa._profile_inventory()["default"]["slots"]
                if s["slotId"] == "main")
    assert main["note"] in ("", None)
    assert not main["note"]  # UI `{note && ...}` renders nothing, never a crash
