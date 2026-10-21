"""Focused integration tests for the dashboard API repair (narrow scope).

Covers, against REAL FastAPI routes on throwaway /tmp Hermes homes:

  * runtime-path roundtrip after the inventory rename — specialist
    stt.openai.model / tts.openai.model and the chain paths fallback_providers /
    delegation.fallback_providers (never the legacy speech.* / fallback.providers);
  * the ``/state`` preset DTO carries the EXACT reviewed slot-scope changes
    (``preset.changes``) the UI previews, with a truthful ``families`` summary;
  * an operation DTO exposes per-result ``revisionBefore`` / ``revisionAfter``;
  * truthful configured vs effective route (``effectiveRoute``) and chain state
    (null -> unset/inherit, [] -> disabled, list -> configured);
  * ``to:{chain:null}`` normalizes to an inherit/unset (None) — never a disabled
    [] and never a removal;
  * provider-scoped credential + cost checks: a configured custom endpoint needs
    no credential, a provider with no explicable credential is "unknown" (never
    free), and a provider slug alone never becomes "subscription_included".
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(0, REPO)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import dashboard.plugin_api as plugin_api  # noqa: E402
from host_protocol_helpers import finish_host_main_calls  # noqa: E402


BASE_CONFIG = """\
model:
  provider: openai
  default: gpt-4o-mini
providers:
  lablocal:
    base_url: http://127.0.0.1:8080/v1
"""

# A hermetic, no-network synthetic credential for a hosted provider. Declared
# explicitly in the lab config so per-provider cost classification exercises the
# REAL credential-resolution path — never ambient host auth (absent on clean CI,
# where 'anthropic' would otherwise correctly classify as "unknown") and never a
# monkeypatched auth success. The value is an obvious fake that can never reach
# a network.
SYNTHETIC_HOSTED_CREDENTIAL = "synthetic-lab-no-network"
LAB_CFG_WITH_HOSTED_CREDENTIAL = BASE_CONFIG + (
    f"  anthropic:\n    api_key: {SYNTHETIC_HOSTED_CREDENTIAL}\n")


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
    return home / "config.yaml" if profile in (None, "default") \
        else home / "profiles" / profile / "config.yaml"


def _inv_slots(profile="default"):
    return plugin_api._profile_inventory()[profile]["slots"]


# ------------------------------------------------ runtime paths / roundtrip


def test_specialist_roundtrip_uses_backend_runtime_paths(hermes_home):
    """A preset reviewed for specialist.stt/tts restores on the REAL runtime
    paths (stt.openai.model / tts.openai.model), not the legacy speech.* keys."""
    assert plugin_api._infer_slot_id({"stt.openai.model": "whisper-1"}) == "specialist.stt"
    assert plugin_api._infer_slot_id({"tts.openai.model": "tts-1"}) == "specialist.tts"
    # legacy records still recognised, so nothing silently becomes main
    assert plugin_api._infer_slot_id({"speech.model_id": "x"}) == "specialist.stt"
    assert plugin_api._infer_slot_id({"speech.streaming_model": "x"}) == "specialist.tts"

    assert plugin_api._typed_to_from_changes(
        "specialist.stt", {"stt.openai.model": "whisper-1"}) == \
        {"provider": "openai", "model": "whisper-1"}
    assert plugin_api._typed_to_from_changes(
        "specialist.tts", {"tts.openai.model": "tts-1"}) == \
        {"provider": "openai", "model": "tts-1"}
    assert plugin_api._typed_to_from_changes("specialist.stt", {
        "stt.groq.model": "whisper-large-v3", "stt.provider": "groq"}) == {
        "provider": "groq", "model": "whisper-large-v3"}
    assert plugin_api._infer_slot_id({"tts.elevenlabs.model_id": "scribe"}) == "specialist.tts"


def test_chain_roundtrip_uses_fallback_providers_paths(hermes_home):
    """Both chain slots read/write their real storage paths — fallback_providers
    (main) and delegation.fallback_providers (delegation) — never the old
    fallback.providers key."""
    assert plugin_api._infer_slot_id({"fallback_providers": ["a"]}) == "fallback.main_chain"
    assert plugin_api._infer_slot_id(
        {"delegation.fallback_providers": ["a"]}) == "delegation.fallback_chain"
    assert plugin_api._typed_to_from_changes(
        "fallback.main_chain", {"fallback_providers": [{"provider": "p", "model": "m"}]}) == \
        {"chain": [{"provider": "p", "model": "m"}]}
    assert plugin_api._typed_to_from_changes(
        "delegation.fallback_chain", {"delegation.fallback_providers": None}) == \
        {"chain": None}


def test_preset_apply_roundtrips_chain_and_specialist_slots(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    # stt.provider pinned: unset providers resolve to LOCAL (autodetect,
    # local-first), so an openai model edit must have the backend stored
    # explicitly. ONE providers table (a duplicate key makes the config
    # unreadable). openai is declared as an OPENAI-COMPATIBLE LOCAL endpoint
    # (base_url row): the profile needs NO hosted credential, per the
    # user-declared custom-endpoint rule — never a fake key.
    chain_cfg = ("model:\n  provider: openai\n  default: gpt-4o-mini\n"
                 "providers:\n"
                 "  lablocal:\n    base_url: http://127.0.0.1:8080/v1\n"
                 "  openai:\n    base_url: http://127.0.0.1:8087/v1\n"
                 "fallback_providers:\n  - lablocal/lab-7b\n"
                 "stt:\n  provider: openai\n  openai:\n    model: old-stt\n")
    _m(hermes_home, "alpha", config=chain_cfg)
    tc = _client()
    changes = [
        {"profileId": "alpha", "slotId": "fallback.main_chain", "action": "set",
         "to": {"chain": [{"provider": "lablocal", "model": "lab-7b"}]}},
        {"profileId": "alpha", "slotId": "specialist.stt", "action": "set",
         "to": {"model": "whisper-1"}},
    ]
    r = tc.post("/presets", json={"name": "mixed", "changes": changes})
    assert r.status_code == 200, r.text
    pid = r.json()["preset"]["id"]

    rec = next(e for e in _records() if e["preset_id"] == pid)
    # the DTO's exact reviewed intent round-trips through the state endpoint too
    state = tc.get("/state").json()
    dto = next(p for p in state["presets"] if p["id"] == pid)
    assert dto["changes"] == changes, dto["changes"]

    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200, r.text
    assert r.json()["operation"]["state"] == "committed", r.text
    import yaml
    after = yaml.safe_load((home / "profiles" / "alpha" / "config.yaml").read_text())
    assert after["fallback_providers"] == [{"provider": "lablocal", "model": "lab-7b"}]
    assert after["stt"]["openai"]["model"] == "whisper-1"


# ---------------------------------------------------- preset DTO fields


def test_state_preset_dto_carries_exact_changes_and_true_families(hermes_home):
    home = hermes_home.path
    _m(hermes_home, "default")
    cron_cfg = BASE_CONFIG + "cron:\n  provider: cronctl\n  model: old\n"
    _m(hermes_home, "alpha", config=cron_cfg)
    tc = _client()
    changes = [
        {"profileId": "alpha", "slotId": "cron.model", "action": "set",
         "to": {"provider": "dispatchlab", "model": "cron-9"}},
    ]
    r = tc.post("/presets", json={"name": "cron fleet", "changes": changes})
    assert r.status_code == 200, r.text

    state = tc.get("/state").json()
    dto = next(p for p in state["presets"])
    # exact slot-scope intents the UI's buildPresetChanges consumes
    assert dto["changes"] == changes, dto
    # truthful families — a cron preset is "automation", never labelled "Main"
    assert dto["families"] == ["automation"], dto
    # no main assignment masquerades for a non-main preset
    assert dto["assignments"] == {}, dto
    assert dto["profileIds"] == ["alpha"]


# ---------------------------------------------------------- operation revisions


def test_operation_dto_exposes_revision_before_and_after(hermes_home):
    _m(hermes_home, "default")
    tc = _client()
    r = tc.post("/operations", json={"changes": [
        {"profileId": "default", "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]})
    assert r.status_code == 200, r.text
    pending = r.json()["operation"]["results"][0]
    assert pending["status"] == "awaiting_host", pending
    assert pending["revisionBefore"] and pending["revisionAfter"] is None, pending
    r = finish_host_main_calls(tc, r, hermes_home.path)
    res = r.json()["operation"]["results"][0]
    assert res["revisionBefore"] == pending["revisionBefore"], res
    assert res["revisionAfter"], res
    # a real write moves the store revision
    assert res["revisionBefore"] != res["revisionAfter"], res


# ------------------------------------------- configured vs effective / chain


def test_state_exposes_configured_and_effective_route_separately(hermes_home):
    _m(hermes_home, "default")  # no delegation block -> inherits main
    slots = _inv_slots()
    deleg = next(s for s in slots if s["slotId"] == "delegation.model")
    # CONFIGURED is explicit-only: nothing is stored for delegation
    assert deleg["provider"] == "" and deleg["model"] == "", deleg
    # EFFECTIVE is the resolved inherited route (main's openai/gpt-4o-mini)
    assert deleg["effectiveRoute"]["provider"] == "openai", deleg
    assert deleg["effectiveRoute"]["model"] == "gpt-4o-mini", deleg
    # main: configured and effective agree (no inheritance involved)
    main = next(s for s in slots if s["slotId"] == "main")
    assert main["provider"] == "openai" and main["model"] == "gpt-4o-mini", main
    assert main["effectiveRoute"]["provider"] == "openai", main


def test_state_chain_slot_reports_three_states(hermes_home):
    _m(hermes_home, "default")  # no fallback_providers -> unset
    _m(hermes_home, "empty", config=BASE_CONFIG + "fallback_providers: []\n")
    _m(hermes_home, "full",
       config=BASE_CONFIG + "fallback_providers:\n  - lablocal/lab-7b\n")
    inv = plugin_api._profile_inventory()

    def chain_slot(prof):
        return next(s for s in inv[prof]["slots"] if s["slotId"] == "fallback.main_chain")

    unset = chain_slot("default")
    assert unset["state"] == "unset" and unset["state"] != "disabled", unset
    disabled = chain_slot("empty")
    assert disabled["state"] == "disabled" and disabled["chain"] == [], disabled
    configured = chain_slot("full")
    assert configured["state"] == "configured", configured
    assert configured["chain"] == ["lablocal/lab-7b"], configured


def test_chain_null_normalizes_to_inherit_unset_not_disabled_or_removed(hermes_home):
    """The critical ambiguity: to:{chain:null} must bind as an inherit/unset
    (a stored None), never a disabled [] and never a key removal."""
    _m(hermes_home, "default")
    chain_cfg = BASE_CONFIG + "fallback_providers:\n  - lablocal/lab-7b\n" \
        "delegation:\n  fallback_providers:\n    - a\n    - b\n"
    _m(hermes_home, "alpha", config=chain_cfg)
    profiles = plugin_api._profile_inventory()

    targets, blocked = plugin_api._changes_to_targets(
        [{"profileId": "alpha", "slotId": "delegation.fallback_chain",
          "action": "set", "to": {"chain": None}}], profiles, strict=False)
    assert blocked == [], blocked
    assert targets[0].changes == {"delegation.fallback_providers": None}, targets[0].changes

    # [] stays an explicit disable (distinct from the null above)
    targets, _ = plugin_api._changes_to_targets(
        [{"profileId": "alpha", "slotId": "delegation.fallback_chain",
          "action": "set", "to": {"chain": []}}], profiles, strict=False)
    assert targets[0].changes == {"delegation.fallback_providers": []}, targets[0].changes


# --------------------------------------------------- credentials + cost


def test_custom_endpoint_needs_no_credential_but_scopes_per_provider(hermes_home):
    """A configured custom endpoint (a providers: row with a base_url) is
    credentialed without a .env; a hosted provider with no explicable
    credential is NOT."""
    _m(hermes_home, "local", env=False)  # lablocal declared in providers:
    _m(hermes_home, "bare", env=False, config="model:\n  provider: openai\n"
                                            "  default: gpt-4o-mini\n")
    ident_local = plugin_api.discover_profiles()["local"]
    ident_bare = plugin_api.discover_profiles()["bare"]
    assert plugin_api._profile_has_credentials("local", ident_local, ["lablocal"]) is True
    assert plugin_api._profile_has_credentials("bare", ident_bare, ["somecloud"]) is False
    # no providers referenced -> nothing to authenticate
    assert plugin_api._profile_has_credentials("bare", ident_bare, []) is True


def test_route_cost_never_infers_subscription_from_slug(hermes_home):
    _m(hermes_home, "default")            # has .env (lab-only marker)
    _m(hermes_home, "bare", env=False, config="model:\n  provider: openai\n"
                                            "  default: gpt-4o-mini\n")
    ident_env = plugin_api.discover_profiles()["default"]
    ident_bare = plugin_api.discover_profiles()["bare"]
    # 'anthropic' is NOT special-cased into a free subscription tier anymore
    assert plugin_api._route_cost_class(
        "default", {"model.provider": "anthropic", "model.default": "x"}, ident_env) \
        != "subscription_included"
    # an inexplicable provider (no endpoint, no runtime auth, no credential
    # material) routes to "unknown" (unknown is never free)
    assert plugin_api._route_cost_class(
        "bare", {"model.provider": "somecloud", "model.default": "x"}, ident_bare) \
        == "unknown"
    # a configured custom endpoint classifies as metered (a real, approved class)
    assert plugin_api._route_cost_class(
        "default", {"model.provider": "lablocal", "model.default": "x"}, ident_env) \
        == "metered"


def test_chain_cost_classified_per_hop_not_all_fallback_providers(hermes_home):
    # "default" declares a synthetic hosted credential for anthropic so the
    # explicable-hop chain classifies as metered WITHOUT ambient host auth.
    _m(hermes_home, "default", config=LAB_CFG_WITH_HOSTED_CREDENTIAL)
    _m(hermes_home, "bare", env=False, config="model:\n  provider: openai\n"
                                            "  default: gpt-4o-mini\n")
    ident_env = plugin_api.discover_profiles()["default"]
    ident_bare = plugin_api.discover_profiles()["bare"]
    stored = {"fallback_providers": [
        {"provider": "lablocal", "model": "m"},
        {"provider": "somecloud", "model": "n"},  # inexplicable hop -> unknown wins
    ]}
    assert plugin_api._route_cost_class("bare", stored, ident_bare) == "unknown"
    # a chain of only explicable hops (custom endpoint + credentialed provider)
    # is metered — never a slug-derived free tier
    ok = {"fallback_providers": [
        {"provider": "lablocal", "model": "m"},
        {"provider": "anthropic", "model": "n"},
    ]}
    assert plugin_api._route_cost_class("default", ok, ident_env) == "metered"


def test_direct_non_main_set_requires_provider_credentials(hermes_home):
    """A direct (non-preset) set for a hosted provider with no explicable
    credential blocks BEFORE any write."""
    home = hermes_home.path
    _m(hermes_home, "default")
    _m(hermes_home, "bare", env=False,
       config=BASE_CONFIG + "cron:\n  provider: cronctl\n  model: old\n")
    tc = _client()
    before = (home / "profiles" / "bare" / "config.yaml").read_text()
    r = tc.post("/operations", json={"changes": [
        {"profileId": "bare", "slotId": "cron.model", "action": "set",
         "to": {"provider": "somecloud", "model": "cron-9"}}]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["blocked"] is True, r.text
    assert (home / "profiles" / "bare" / "config.yaml").read_text() == before

    # a custom endpoint is not gated and applies cleanly
    r = tc.post("/operations", json={"changes": [
        {"profileId": "bare", "slotId": "cron.model", "action": "set",
         "to": {"provider": "lablocal", "model": "cron-9"}}]})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------ URL sanitization


def test_public_diff_value_sanitizes_base_url_credentials():
    dirty = "https://user:s3cret@host.example/v1?api_key=abc#frag"
    assert plugin_api._sanitize_public_url(dirty) == "https://host.example/v1"
    # route dicts and chain entries both get sanitized
    assert plugin_api._public_diff_value(
        {"provider": "p", "model": "m", "base_url": dirty}, "model") == \
        {"provider": "p", "model": "m", "base_url": "https://host.example/v1"}
    out = plugin_api._public_diff_value([{"provider": "p", "model": "m",
                                          "base_url": dirty}], "chain")
    assert out[0]["base_url"] == "https://host.example/v1"


def test_preview_diff_never_leaks_base_url_credentials(hermes_home):
    _m(hermes_home, "default")
    tc = _client()
    dirty = "https://user:s3cret@host.example/v1?api_key=abc"
    r = tc.post("/preview", json={"changes": [
        {"profileId": "default", "slotId": "cron.model", "action": "set",
         "to": {"provider": "lablocal", "model": "m", "base_url": dirty}}]})
    assert r.status_code == 200, r.text
    blob = json.dumps(r.json())
    assert "s3cret" not in blob and "api_key=abc" not in blob, blob


# ---------------------------------------------------------------- helpers


def _records():
    path = plugin_api._state_dir() / "presets.json"
    return json.loads(path.read_text())


# ------------------------------------------------------ scope signature


def test_scope_signature_ignores_guard_metadata_and_side_effects():
    """A cached preview target carries identity/expected_revision AND canonical
    main side effects; a rebuilt preset target carries only the reviewed intent.
    The scope guard must compare INTENT (profile/store/path + route changes),
    never that metadata."""
    a = [{"profile": "default", "store_kind": "canonical_config",
          "path": "/x/config.yaml",
          "changes": {"model.provider": "zhipu", "model.default": "m",
                      "model.base_url": "http://127.0.0.1:8080/v1",
                      "model.api_mode": "chat_completions"},
          "identity": {"name": "default", "path": "/x", "fingerprint": "fp"},
          "expected_revision": "rev-1"}]
    b = [{"profile": "default", "store_kind": "canonical_config",
          "path": "/x/config.yaml",
          "changes": {"model.provider": "zhipu", "model.default": "m"},
          "identity": {"name": "default", "path": "/x", "fingerprint": "fp"}}]
    assert plugin_api._same_prepared_scope(a, b) is True
    # a genuine route-intent difference still blocks
    c = [dict(b[0], changes={"model.provider": "other", "model.default": "m"})]
    assert plugin_api._same_prepared_scope(a, c) is False


def test_preset_one_click_with_preview_id_matches_scope(hermes_home):
    """The UI one-click flow: preview the preset's own changes, then apply with
    BOTH presetId and previewId. The preview binds semantic main intent; host
    writes add model.base_url / api_mode only at readback, without breaking
    the scope guard against the frozen preset targets."""
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    changes = [
        {"profileId": p, "slotId": "main", "action": "set",
         "to": {"provider": "lablocal", "model": "lab-7b"}}
        for p in ("default", "alpha")
    ]
    r = tc.post("/presets", json={"name": "fleet", "changes": changes})
    assert r.status_code == 200, r.text
    pid = r.json()["preset"]["id"]

    r = tc.post("/preview", json={"changes": [
        {"profileId": c["profileId"], "slotId": c["slotId"],
         "action": c["action"], "to": c["to"]} for c in changes]})
    assert r.status_code == 200, r.text
    preview_id = r.json()["previewId"]

    r = tc.post("/operations", json={"presetId": pid, "previewId": preview_id})
    assert r.status_code == 200, r.text
    r = finish_host_main_calls(tc, r, hermes_home.path)
    assert r.json()["operation"]["state"] == "committed", r.text
