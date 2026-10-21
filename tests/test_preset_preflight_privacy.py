"""Preset preflight privacy (P1) and presets.json file privacy (P2).

P1: preflight blocking reasons travel verbatim inside the public 409 apply
DTO, so a route's identity must be profile + store kind + slot paths ONLY.
Change VALUES can carry inline credentials (an ``api_key`` leaf, a
credential-bearing ``base_url``) and must never be stringified into the
reason text.

P2: presets.json persists reviewed intents that can embed credential-bearing
values. The file must be private (0600) from the first byte — never a
world-readable tempfile — and a pre-existing wider file is tightened on load.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard.plugin_api as plugin_api
from model_switcher.presets import (
    COST_SUBSCRIPTION,
    COST_UNKNOWN,
    BillingPolicy,
    ReviewedPreset,
    RouteTarget,
    _route_key,
)

# Distinctive fake markers, never shaped like a real key: the assertions only
# need a substring that must not reach any response.
LEAK_MARKER_CREDENTIAL = "lab-fake-credential-must-not-echo"
LEAK_MARKER_ENDPOINT = "https://user:lab-fake-pass@vault.internal/v1"

LEAKY_CHANGES = {
    "model.provider": "custom",
    "model.default": "lab-7b",
    "model.api_key": LEAK_MARKER_CREDENTIAL,
    "model.base_url": LEAK_MARKER_ENDPOINT,
}


def _client():
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def _leaky_target(profile: str = "alpha", path: str = "/tmp/nonexistent/config.yaml",
                  cost_class: str = COST_SUBSCRIPTION) -> RouteTarget:
    return RouteTarget(
        profile=profile, store_kind="canonical_config", path=path,
        changes=dict(LEAKY_CHANGES), cost_class=cost_class)


def _leaky_preset(cost_class: str = COST_SUBSCRIPTION) -> ReviewedPreset:
    return ReviewedPreset(
        preset_id="p-leak", name="leak",
        billing_policy=BillingPolicy(approved_classes=[COST_SUBSCRIPTION]),
        targets=[_leaky_target(cost_class=cost_class)],
        reviewed_profile_ids=["alpha"])


# ------------------------------------------------------------------ P1 (unit)

def test_route_key_is_identity_only_never_change_values():
    key = _route_key(_leaky_target())
    assert LEAK_MARKER_CREDENTIAL not in key, key
    assert LEAK_MARKER_ENDPOINT not in key, key
    assert "model.api_key" in key and "model.base_url" in key, key
    assert key.startswith("alpha:canonical_config:"), key


def test_preflight_cost_drift_reasons_never_embed_change_values():
    preset = _leaky_preset()
    reasons = preset.preflight(
        credentials={"alpha": True},
        live_cost_classes={_route_key(preset.targets[0]): "metered"},
        discovered_profiles=["alpha"])
    assert any("cost changed after review" in r for r in reasons), reasons
    blob = "\n".join(reasons)
    assert LEAK_MARKER_CREDENTIAL not in blob, blob
    assert LEAK_MARKER_ENDPOINT not in blob, blob
    # the identity survives so the reviewer can still act on the block
    assert "alpha" in blob and "model.api_key" in blob, blob


def test_preflight_unknown_cost_reason_never_embeds_change_values():
    preset = _leaky_preset(cost_class=COST_UNKNOWN)
    reasons = preset.preflight(
        credentials={"alpha": True},
        live_cost_classes={},
        discovered_profiles=["alpha"])
    assert any("unknown cost" in r for r in reasons), reasons
    blob = "\n".join(reasons)
    assert LEAK_MARKER_CREDENTIAL not in blob, blob
    assert LEAK_MARKER_ENDPOINT not in blob, blob


# ------------------------------------------------------------ P1 (409 surface)

def test_apply_preflight_409_never_embeds_change_values(hermes_home):
    hermes_home.make_profile("alpha")
    state = hermes_home.path / "state" / "hermes-model-switcher"
    state.mkdir(parents=True)
    cfg_path = str(hermes_home.path / "profiles" / "alpha" / "config.yaml")
    record = {
        "preset_id": "p-leak", "name": "leak",
        "approved_classes": [COST_SUBSCRIPTION],
        "allow_unknown": False,
        "reviewed_profile_ids": ["alpha"],
        "targets": [{
            "profile": "alpha", "store_kind": "canonical_config",
            "path": cfg_path, "changes": dict(LEAKY_CHANGES),
            "cost_class": COST_UNKNOWN,
        }],
    }
    (state / "presets.json").write_text(json.dumps([record]))

    r = _client().post("/operations", json={"presetId": "p-leak"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail.get("blocked") is True, detail
    reasons = detail.get("reasons") or []
    assert reasons, detail
    blob = json.dumps(detail)
    assert LEAK_MARKER_CREDENTIAL not in blob, blob
    assert LEAK_MARKER_ENDPOINT not in blob, blob
    # the block still names the route identity so it is actionable
    assert any("model.api_key" in reason for reason in reasons), reasons


# -------------------------------------------------------------------- P2 (API)

def test_persist_creates_presets_json_0600(hermes_home):
    hermes_home.make_profile("alpha")
    tc = _client()
    r = tc.post("/presets", json={"name": "perms", "changes": [
        {"profileId": "alpha", "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    assert r.status_code == 200, r.text
    path = hermes_home.path / "state" / "hermes-model-switcher" / "presets.json"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)
    assert not path.with_suffix(".json.tmp").exists()


def test_load_tightens_an_existing_wider_presets_json(hermes_home):
    state = hermes_home.path / "state" / "hermes-model-switcher"
    state.mkdir(parents=True)
    path = state / "presets.json"
    path.write_text("[]")
    os.chmod(path, 0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    r = _client().get("/state")
    assert r.status_code == 200, r.text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)
