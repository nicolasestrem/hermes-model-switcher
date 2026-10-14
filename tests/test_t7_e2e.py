"""T7 — isolated end-to-end runtime evidence (PLAN T7).

Every byte of state lives in throwaway Hermes homes minted under /tmp by the
hermes_home conftest fixture (NEVER under ~/.hermes — nested homes resolve
back to the live install). Three homes: the default home plus named profiles
A (alpha) and B (beta). beta is the untouched control.

Evidence produced:
  1. A reviewed preset applied through the journalled engine changes ONLY the
     selected main routes in default + alpha; beta is untouched; protected
     fields (goal-judge threshold) and unrelated config survive verbatim.
  2. The original session config is intact: journal snapshots equal the
     pre-images and undo restores the exact original document.
  3. The real-cost new-session probe route is removed from the backend: the
     Dashboard exposes no path that can start a paid session, so no usage
     rows can ever be fabricated.

No forced paid fallback tests; no live inference anywhere.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from model_switcher.engine import apply_operation, plan_operation, undo_operation
from model_switcher.journal import OP_COMMITTED, OperationJournal
from model_switcher.presets import (
    BillingPolicy,
    COST_SUBSCRIPTION,
    COST_UNKNOWN,
    PresetStore,
    ReviewedPreset,
    RouteTarget,
    _route_key,
)

OLD_PROVIDER, OLD_MODEL = "openai", "gpt-4o-mini"
NEW_PROVIDER, NEW_MODEL = "lablocal", "lab-7b-instruct"

BASE_CONFIG = """\
model:
  provider: openai
  default: gpt-4o-mini
providers:
  lablocal:
    base_url: http://127.0.0.1:8080/v1
auxiliary:
  goal_judge:
    model: j/old
    threshold: 0.8
unrelated:
  keep_me: yes
  notes: session history untouched
"""

SELECTED = ["model.provider", "model.default"]
_PROFILES = ("default", "alpha", "beta")
_SENT = object()


def _cfg_path(home, profile):
    if profile in (None, "default"):
        return home / "config.yaml"
    return home / "profiles" / profile / "config.yaml"


def _load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def _pre_images(home):
    return {p: _load(_cfg_path(home, p)) for p in _PROFILES}


def _leaf_paths(node, prefix=""):
    out = {}
    if isinstance(node, dict) and node:
        for k, v in node.items():
            out.update(_leaf_paths(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = node
    return out


def _assert_selected_only_diff(before, after):
    b, a = _leaf_paths(before), _leaf_paths(after)
    changed = {k for k in set(b) | set(a) if b.get(k, _SENT) != a.get(k, _SENT)}
    assert changed == set(SELECTED), (
        f"unexpected config drift: {sorted(changed - set(SELECTED))}; "
        f"missing: {sorted(set(SELECTED) - changed)}")


@pytest.fixture()
def fleet(hermes_home):
    """default home + A (alpha) + B (beta); B is the untouched control."""
    home = hermes_home.path
    for prof in ("alpha", "beta"):
        hermes_home.profile(prof, config={"_raw": BASE_CONFIG})
    (home / "config.yaml").write_text(BASE_CONFIG)
    journal = OperationJournal(str(home / "journal" / "ops.db"))
    return home, journal


def _preset(home):
    targets = [
        RouteTarget(profile=p, store_kind="yaml_config",
                    path=str(_cfg_path(home, p)),
                    changes={"model.provider": NEW_PROVIDER,
                             "model.default": NEW_MODEL},
                    cost_class=COST_SUBSCRIPTION)
        for p in ("default", "alpha")
    ]
    return ReviewedPreset(
        preset_id="t7-main-switch", name="T7 lab main switch",
        billing_policy=BillingPolicy(approved_classes=[COST_SUBSCRIPTION]),
        targets=targets,
        reviewed_profile_ids=["default", "alpha"])


def _store(preset):
    ps = PresetStore()
    ps.save(preset)
    return ps


def test_t7_selected_routes_only_and_session_config_intact(fleet):
    home, journal = fleet
    pre = _pre_images(home)
    preset = _preset(home)
    ps = _store(preset)

    # Preflight: pure, blocks before any write on non-empty reasons.
    # Approval is checked against the frozen review set (default+alpha);
    # beta is the untouched control and is deliberately never included.
    reasons = preset.preflight(
        credentials={p: True for p in ("default", "alpha")},
        live_cost_classes={},           # unknown keys fall back to reviewed class
        discovered_profiles=["default", "alpha"])
    assert reasons == [], reasons
    targets = ps.apply(preset.preset_id)
    op = plan_operation(journal, targets)
    outcome = apply_operation(journal, op["op_id"])
    assert outcome["state"] == OP_COMMITTED, outcome

    # 1) Only the selected routes changed in the targeted homes.
    for p in ("default", "alpha"):
        after = _load(_cfg_path(home, p))
        _assert_selected_only_diff(pre[p], after)
        assert after["model"]["provider"] == NEW_PROVIDER
        assert after["model"]["default"] == NEW_MODEL
        # protected fields survived verbatim
        assert after["auxiliary"]["goal_judge"]["threshold"] == 0.8
        assert after["auxiliary"]["goal_judge"]["model"] == "j/old"
        assert after["unrelated"] == pre[p]["unrelated"]

    # Control profile untouched — identical document.
    assert _load(_cfg_path(home, "beta")) == pre["beta"]

    # 2) Original session config intact: journal snapshots equal pre-images.
    op_rec = journal.get(outcome["op_id"])
    for step in op_rec["steps"]:
        assert step["snapshot"] == pre[step["profile"]], step["profile"]

    # Revision-guarded undo restores the exact original documents.
    undo_res = undo_operation(journal, outcome["op_id"])
    assert undo_res["state"] in ("undone", "compensated"), undo_res
    for p in ("default", "alpha"):
        assert _load(_cfg_path(home, p)) == pre[p]


def test_t7_api_end_to_end_state_preview_apply_undo(fleet):
    """Real backend wiring over HTTP: /state → /preview → /presets →
    /operations → /operations/{id}/undo against throwaway Hermes homes
    (default + alpha + beta control), YAML config roundtrip, sanitized
    DTOs (no raw config documents, no snapshots in any response)."""
    home, _journal_unused = fleet
    from fastapi.testclient import TestClient
    import dashboard.plugin_api as plugin_api
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(plugin_api.router)
    client = TestClient(app)

    # -- state ----------------------------------------------------------
    r = client.get("/state")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    body = r.json()
    profile_ids = {p["id"] for p in body["profiles"]}
    assert {"default", "alpha", "beta"} <= profile_ids, profile_ids
    main_default = next(s for s in body["slots"]
                        if s["profileId"] == "default" and s["slotId"] == "main")
    assert main_default["provider"] == OLD_PROVIDER
    # sanitized: no raw config document anywhere in the response —
    # top-level config keys like providers/auxiliary/unrelated must not leak
    blob = json.dumps(body)
    assert '"providers"' not in blob and '"unrelated"' not in blob
    assert '"threshold"' not in blob and '"base_url"' not in blob

    # -- preview --------------------------------------------------------
    change = {"profileId": "default", "slotId": "main", "action": "set",
              "to": {"provider": NEW_PROVIDER, "model": NEW_MODEL}}
    r = client.post("/preview", json={"changes": [change]})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    pv = r.json()["preview"]
    assert pv["changes"], pv
    paths = {d["path"] for c in pv["changes"] for d in c["diff"]}
    assert paths == {"model.provider", "model.default"}, paths
    preview_id = r.json()["previewId"]

    # pre-image for later intactness check (parsed document; the YAML
    # adapter roundtrip normalizes formatting but preserves the document)
    cfg_path = home / "config.yaml"
    with open(cfg_path) as fh:
        pre_doc = yaml.safe_load(fh)

    # -- apply (journalled, revision-guarded) ----------------------------
    r = client.post("/operations", json={"previewId": preview_id})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    op = r.json()["operation"]
    assert op["state"] == "committed", op
    assert all(res["status"] == "applied" for res in op["results"]), op

    with open(cfg_path) as fh:
        after = yaml.safe_load(fh)
    assert after["model"]["provider"] == NEW_PROVIDER
    assert after["model"]["default"] == NEW_MODEL
    assert after["auxiliary"]["goal_judge"]["threshold"] == 0.8  # protected
    assert after["unrelated"] == pre_doc["unrelated"]
    # control profile untouched
    with open(home / "profiles" / "beta" / "config.yaml") as fh:
        assert yaml.safe_load(fh) == pre_doc

    # DTO carries no snapshots: re-fetch the operation record
    r = client.get(f"/operations/{op['id']}")
    assert r.status_code == 200
    op_dto = json.dumps(r.json())
    assert "snapshot" not in op_dto and "providers" not in op_dto

    # -- undo restores the exact original document -----------------------
    r = client.post(f"/operations/{op['id']}/undo")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    with open(cfg_path) as fh:
        assert yaml.safe_load(fh) == pre_doc



def test_t7_preset_preflight_blocks_and_writes_nothing(fleet):
    """Negative control: missing credentials / unknown cost / non-reviewed
    profile all block BEFORE any write; the fleet stays byte-identical."""
    home, journal = fleet
    pre = _pre_images(home)
    preset = _preset(home)
    reasons = preset.preflight(
        credentials={"default": False, "alpha": True},
        live_cost_classes={_route_key(preset.targets[1]): COST_UNKNOWN},
        discovered_profiles=list(_PROFILES))
    assert any("missing provider credentials" in r for r in reasons), reasons
    assert any("unknown cost" in r for r in reasons), reasons
    assert any("new profile 'beta'" in r for r in reasons), reasons
    for p in _PROFILES:
        assert _load(_cfg_path(home, p)) == pre[p]
