"""Public host-options payload -> catalog/model POST route contracts.

The host owns profile scope, refresh and prices; the plugin transforms supplied
/api/model/options JSON. No private adapter picker/pricing seam, live config,
network, inference, preset store or journal is touched.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Runtime-built host-path shape: no baked-in ``/home/<user>`` literal in the
# repo (the skeleton scan forbids host-specific tracked strings), while the
# sanitizer test still proves an absolute host path never reaches the client.
LAB_HOST_PATH = str(Path("/home") / "lab" / "config.yaml")

import dashboard.plugin_api as api  # noqa: E402
import dashboard.model_catalog as mc  # noqa: E402

PROVIDER_KEYS = {"id", "label", "authenticated", "configured", "isCurrent",
                 "modelCount", "fetchedAt", "stale", "warning", "models"}
# ``capabilities`` is optional on a model entry, so it is asserted separately.
MODEL_REQUIRED_KEYS = {"id", "label", "pricingStatus", "cost"}
CAPABILITY_KEYS = {"fast", "reasoning", "canDisableReasoning"}
COST_KEYS = {"unit", "input", "output", "cacheRead", "display", "free",
             "source", "pricingVersion"}


@pytest.fixture(autouse=True)
def isolated_home(hermes_home):
    return hermes_home


def _client():
    app = FastAPI()
    app.include_router(api.router, prefix="")
    return TestClient(app)


def make_row(slug, name=None, models=(), source="built-in", pricing=None, **extra):
    row = {
        "slug": slug,
        "name": name if name is not None else slug.replace("-", " ").title(),
        "models": list(models),
        "total_models": len(models),
        "source": source,
        "is_current": False,
        "is_user_defined": False,
        "authenticated": True,
    }
    if pricing is not None:
        row["pricing"] = pricing
    row.update(extra)
    return row


def make_payload(*rows):
    return {"providers": list(rows), "model": "", "provider": ""}


PRICED_PAYLOAD = make_payload(
    make_row("nous", "Nous", models=["hermes-4-405b"],
             pricing={"hermes-4-405b": {
                 "input": "$10.00", "output": "$50.00", "cache": "$0.25",
                 "free": False}}),
    make_row("openrouter", "OpenRouter", models=["vendor/model-x"]),
)


@pytest.fixture()
def fake(monkeypatch):
    class NoCoreCatalog:
        def __getattr__(self, name):
            raise AssertionError(f"transform must not use adapter.{name}")

    monkeypatch.setattr(api, "_ADAPTER", NoCoreCatalog())
    monkeypatch.setattr(api, "discover_profiles",
                        lambda: {"default": object(), "work": object()})
    return copy.deepcopy(PRICED_PAYLOAD)


# -- route shape / mounting -------------------------------------------------

def test_catalog_route_is_mounted_like_models_and_is_sync():
    paths = [getattr(route, "path", "") for route in api.router.routes]
    assert "/catalog" in paths
    assert "/models" in paths
    import inspect
    for route in api.router.routes:
        if getattr(route, "path", "") == "/catalog":
            assert not inspect.iscoroutinefunction(getattr(route, "endpoint", None))


def test_catalog_returns_aggregate_dto_with_no_store(fake):
    r = _client().post("/catalog", json={"options": fake})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] is True
    assert body["profileId"] is None
    assert body["error"] is None
    assert set(body["cost"]) == {"pending", "unavailableProviders"}
    assert [p["id"] for p in body["providers"]] == ["nous", "openrouter"]
    provider = body["providers"][0]
    assert set(provider) == PROVIDER_KEYS
    assert provider["label"] == "Nous"
    assert provider["modelCount"] == 1
    model = provider["models"][0]
    assert set(model) == MODEL_REQUIRED_KEYS
    assert set(model) <= set(mc.MODEL_KEYS)
    assert model["pricingStatus"] == "priced"
    assert set(model["cost"]) == COST_KEYS
    assert model["cost"]["display"] == {
        "input": "$10.00", "output": "$50.00", "cache": "$0.25"}
    assert model["cost"]["input"] == 10.0
    assert model["cost"]["output"] == 50.0
    assert model["cost"]["cacheRead"] == 0.25
    assert model["cost"]["pricingVersion"] is None
    assert model["cost"]["source"] == "picker"
    assert provider["fetchedAt"] is None and provider["stale"] is True


def test_catalog_forwards_profile_provider_and_refresh(fake):
    r = _client().post("/catalog", json={"profile": "work", "provider": "NOUS",
                                         "refresh": True, "options": fake})
    assert r.status_code == 200
    body = r.json()
    assert body["profileId"] == "work"
    assert [p["id"] for p in body["providers"]] == ["nous"]
    assert body["cost"] == {"pending": False, "unavailableProviders": []}


@pytest.mark.parametrize("refresh", [False, True])
def test_refresh_is_transform_metadata_not_core_fetch(fake, monkeypatch, refresh):
    from dashboard.model_catalog import catalog_response
    calls = []

    def transform(options, provider, **filters):
        calls.append((options, provider, filters))
        return catalog_response(options, provider, **filters)

    monkeypatch.setattr(api, "_model_catalog_module", lambda: SimpleNamespace(
        catalog_response=transform))
    body = {"profile": "work", "provider": "nous", "options": fake}
    if refresh:
        body["refresh"] = True
    r = _client().post("/catalog", json=body)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls == [(fake, "nous", {
        "profile_id": "work", "refresh": refresh, "options_failure": None})]
    assert catalog_response(fake, refresh=True) == catalog_response(fake, refresh=False)


# -- truthful failures ------------------------------------------------------

def test_unknown_profile_is_refused_before_transform(fake, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("unknown profile must not reach the transform")

    monkeypatch.setattr(api, "_model_catalog_module", lambda: SimpleNamespace(
        catalog_response=refuse))
    r = _client().post("/catalog", json={"profile": "ghost", "options": fake})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "unknown profile 'ghost'"
    assert body["providers"] == []
    assert body["profileId"] == "ghost"


def test_zero_provider_rows_is_a_visible_failure(fake):
    r = _client().post("/catalog", json={"options": make_payload()})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["providers"] == []
    assert body["error"] == "no providers in this profile's catalog"


@pytest.mark.parametrize("path", ["/catalog", "/models"])
def test_transform_exception_is_sanitized(monkeypatch, fake, path):
    def boom(*args, **kwargs):
        raise RuntimeError(f"Authorization: Bearer secret-test-token at {LAB_HOST_PATH}")

    monkeypatch.setattr(api, "_model_catalog_module", lambda: SimpleNamespace(
        catalog_response=boom, models_response=boom))
    r = _client().post(path, json={"options": fake, "provider": "nous"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "catalog lookup failed"
    serialized = json.dumps(body)
    for forbidden in ("secret-test-token", "Bearer", "/home/", LAB_HOST_PATH):
        assert forbidden not in serialized


@pytest.mark.parametrize("path", ["/catalog", "/models"])
def test_module_load_failure_is_sanitized(monkeypatch, path, fake):
    def boom():
        raise RuntimeError("Authorization: Bearer ***")
    monkeypatch.setattr(api, "_model_catalog_module", boom)
    r = _client().post(path, json={"options": fake, "provider": "nous"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "model catalog module unavailable"
    assert "secret-test-token" not in json.dumps(body)


def test_non_mapping_module_result_is_a_visible_failure(monkeypatch, fake):
    monkeypatch.setattr(api, "_model_catalog_module", lambda: SimpleNamespace(
        catalog_response=lambda *a, **k: "not-a-mapping",
        models_response=lambda *a, **k: "not-a-mapping"))
    r = _client().post("/catalog", json={"options": fake})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"] == "catalog returned an unexpected payload"


# -- read-only discipline ---------------------------------------------------

def test_catalog_does_not_gate_on_runtime_writability(monkeypatch, fake):
    """Catalog reads are safe on a read-only runtime and must not check writes."""
    def refuse():
        raise AssertionError("catalog must not consult _runtime_writable")
    monkeypatch.setattr(api, "_runtime_writable", refuse)
    r = _client().post("/catalog", json={"options": fake})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_catalog_touches_no_preset_or_state_store(monkeypatch, fake):
    def refuse_state():
        raise AssertionError("catalog must not touch the plugin state dir")

    def refuse_store():
        raise AssertionError("catalog must not touch the preset store")

    def refuse_journal():
        raise AssertionError("catalog must not open the operation journal")

    monkeypatch.setattr(api, "_state_dir", refuse_state)
    monkeypatch.setattr(api, "_preset_store", refuse_store)
    monkeypatch.setattr(api, "_journal", refuse_journal)
    r = _client().post("/catalog", json={"options": fake})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# -- POST /models preserves DTO; GET requires host payload ------------------

def test_models_endpoint_contract_unchanged(fake):
    r = _client().post("/models", json={"options": fake, "provider": "nous"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "nous"
    # Exact frozen shape: model rows are {id, name} only.
    assert body["models"] == [{"id": "hermes-4-405b", "name": "hermes-4-405b"}]
    assert body["providers"] == [
        {"id": "nous", "name": "Nous"},
        {"id": "openrouter", "name": "OpenRouter"},
    ]
    assert body["fetchedAt"] is None
    assert body["stale"] is True
    assert body["source"] == "built-in"
    assert body["error"] is None


def test_models_endpoint_empty_provider_is_still_truthful(fake):
    body = _client().post("/models", json={"options": fake}).json()
    assert body["ok"] is False
    assert body["models"] == []
    assert body["error"] == "no provider specified"


def test_models_endpoint_unknown_provider_is_still_truthful(fake):
    body = _client().post("/models", json={"options": fake, "provider": "ghost"}).json()
    assert body["ok"] is False
    assert body["models"] == []
    assert "ghost" in body["error"]


@pytest.mark.parametrize("path", ["/catalog", "/models"])
def test_legacy_get_requires_host_payload_without_lookup(path, monkeypatch, fake):
    def refuse(*args, **kwargs):
        raise AssertionError("legacy GET must not look up a catalog or profile")

    monkeypatch.setattr(api, "_model_catalog_module", refuse)
    monkeypatch.setattr(api, "discover_profiles", refuse)
    r = _client().get(path + "?profileId=work&provider=nous&refresh=1")
    assert r.status_code == 410
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {"ok": False, "reason": "hostCatalogRequired"}


@pytest.mark.parametrize("path", ["/catalog", "/models"])
@pytest.mark.parametrize("options", [None, "not-a-mapping", {}, {"providers": "bad"}])
def test_missing_or_malformed_host_options_are_visible_failures(path, options, fake):
    r = _client().post(path, json={"provider": "nous", "options": options})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "hostCatalogUnavailable"
    assert body["providers"] == []
    if path == "/models":
        assert body["models"] == []


@pytest.mark.parametrize("path", ["/catalog", "/models"])
def test_bundled_options_belong_to_requested_profile(path, fake):
    r = _client().post(path, json={"profile": "work", "provider": "nous",
        "host": {"profiles": {
            "default": {"options": make_payload(make_row("wrong", models=["not-work"]))},
            "work": {"options": fake}}}})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "not-work" not in r.text
    assert "hermes-4-405b" in r.text


@pytest.mark.parametrize("path", ["/catalog", "/models"])
def test_direct_options_take_precedence_over_bundle(path, fake):
    r = _client().post(path, json={"profile": "work", "provider": "nous", "options": fake,
        "host": {"profiles": {"work": {"options": make_payload()}}}})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_pending_host_prices_are_unknown_not_invented_free(fake):
    fake["providers"][0]["pricing_pending"] = True
    r = _client().post("/catalog", json={"provider": "nous", "options": fake})
    assert r.status_code == 200
    body = r.json()
    assert body["cost"] == {"pending": True, "unavailableProviders": []}
    model = body["providers"][0]["models"][0]
    assert model["pricingStatus"] == "unknown" and model["cost"] is None


# -- per-model capability flags ---------------------------------------------
# The host's shape (`hermes_cli/inventory.py::_apply_capabilities`): one
# row-level `capabilities` dict keyed by the exact model id, each entry
# `{fast, reasoning[, can_disable_reasoning]}` plus non-forwarded extras such
# as `ultrafast`. The DTO allowlists it to camelCased booleans.


def _catalog_models(options, provider="nous"):
    r = _client().post("/catalog", json={"provider": provider, "options": options})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, body
    return body["providers"][0]["models"]


def _catalog_model(options, provider="nous", model_id="hermes-4-405b"):
    r = _client().post("/catalog", json={"provider": provider, "options": options})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, body
    return next(m for m in body["providers"][0]["models"] if m["id"] == model_id)


def test_model_capabilities_pass_through_with_camelcase_names(fake):
    fake["providers"][0]["capabilities"] = {
        "hermes-4-405b": {"fast": True, "reasoning": False,
                          "can_disable_reasoning": True}}
    model = _catalog_model(fake)
    assert model["capabilities"] == {
        "fast": True, "reasoning": False, "canDisableReasoning": True}
    assert set(model["capabilities"]) == CAPABILITY_KEYS
    assert set(model) <= set(mc.MODEL_KEYS)


def test_missing_capability_evidence_omits_the_object_instead_of_defaulting(fake):
    # No map at all, an empty map, and an empty entry for this model are all
    # "the host said nothing" — never a synthesized False.
    for capabilities in (None, {}, {"hermes-4-405b": {}}):
        provider = fake["providers"][0]
        if capabilities is None:
            provider.pop("capabilities", None)
        else:
            provider["capabilities"] = capabilities
        model = _catalog_model(fake)
        assert "capabilities" not in model
        assert set(model) == MODEL_REQUIRED_KEYS


def test_non_boolean_capability_values_are_dropped_not_coerced(fake):
    fake["providers"][0]["capabilities"] = {
        "hermes-4-405b": {"fast": 1, "reasoning": "yes",
                          "can_disable_reasoning": None}}
    assert "capabilities" not in _catalog_model(fake)

    fake["providers"][0]["capabilities"] = {
        "hermes-4-405b": {"fast": True, "reasoning": "yes",
                          "can_disable_reasoning": 0}}
    assert _catalog_model(fake)["capabilities"] == {"fast": True}


def test_only_allowlisted_capability_keys_reach_the_dto(fake):
    fake["providers"][0]["capabilities"] = {
        "hermes-4-405b": {"fast": True, "reasoning": True, "ultrafast": True,
                          "supported_efforts": ["low", "high"], "tools": True,
                          "vision": {"supported": True}, "context": 131072}}
    model = _catalog_model(fake)
    assert model["capabilities"] == {"fast": True, "reasoning": True}
    dumped = json.dumps(model)
    for forbidden in ("ultrafast", "supported_efforts", "can_disable_reasoning",
                      "tools", "vision", "context", "131072"):
        assert forbidden not in dumped


def test_capabilities_attach_to_the_exact_host_model_id_only(fake):
    # The host keys the map by the id it listed; a near-miss (" hermes-4-405b ")
    # must not be guessed onto this model.
    fake["providers"][0]["capabilities"] = {
        " hermes-4-405b ": {"fast": True},
        "hermes-4-405b": {"reasoning": True},
    }
    model = _catalog_model(fake)
    assert model["capabilities"] == {"reasoning": True}
    fake["providers"][0]["capabilities"] = {" hermes-4-405b ": {"fast": True}}
    assert "capabilities" not in _catalog_model(fake)


def test_capability_maps_are_not_shared_across_models(fake):
    fake["providers"][0]["models"] = ["hermes-4-405b", "hermes-4-mini"]
    fake["providers"][0]["capabilities"] = {
        "hermes-4-405b": {"fast": True, "reasoning": True},
        "hermes-4-mini": {"fast": False, "reasoning": True,
                          "can_disable_reasoning": False},
        "unlisted-model": {"fast": True},
    }
    models = {m["id"]: m for m in _catalog_models(fake)}
    assert models["hermes-4-405b"]["capabilities"] == {"fast": True, "reasoning": True}
    assert models["hermes-4-mini"]["capabilities"] == {
        "fast": False, "reasoning": True, "canDisableReasoning": False}
    assert "unlisted-model" not in models


def test_models_route_carries_the_same_capability_object(fake):
    fake["providers"][0]["capabilities"] = {
        "hermes-4-405b": {"fast": True, "reasoning": True,
                          "can_disable_reasoning": False}}
    r = _client().post("/models", json={"provider": "nous", "options": fake})
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == [{"id": "hermes-4-405b", "name": "hermes-4-405b",
                               "capabilities": {"fast": True, "reasoning": True,
                                                "canDisableReasoning": False}}]
