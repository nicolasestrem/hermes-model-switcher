"""GET /catalog route contract + the unchanged GET /models contract.

The route is exercised through a real FastAPI app mounted exactly like the
Dashboard mounts the plugin router (``include_router(plugin_api.router)``), with
``plugin_api._ADAPTER`` replaced by an offline fake. No live config, network,
inference, preset store or journal is touched.
"""
from __future__ import annotations

import contextlib
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

import dashboard.plugin_api as api  # noqa: E402

PROVIDER_KEYS = {"id", "label", "authenticated", "configured", "isCurrent",
                 "modelCount", "fetchedAt", "stale", "warning", "models"}
MODEL_KEYS = {"id", "label", "pricingStatus", "cost"}
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


class FakeProbe:
    def __init__(self, *, picker=None, routes=None, entries=None,
                 models_dev=None, available=True):
        self.picker = picker or {}
        self.routes = routes or {}
        self.entries = entries or {}
        self.models_dev = models_dev or {}
        self.available = available
        self.unavailable = () if available else ("pricing",)
        self.calls = []

    def picker_pricing(self, provider):
        self.calls.append(("picker", provider))
        return dict(self.picker.get(provider) or {})

    def billing_mode(self, model, provider):
        self.calls.append(("route", model, provider))
        return self.routes.get((provider, model))

    def pricing_entry(self, model, provider):
        self.calls.append(("entry", model, provider))
        return self.entries.get((provider, model))

    def models_dev_cost(self, provider, model):
        self.calls.append(("models_dev", provider, model))
        return self.models_dev.get((provider, model))


class FakeAdapter:
    def __init__(self, payload, probe=None, *, raise_on_payload=None):
        self.payload = payload
        self.probe = probe or FakeProbe()
        self.scopes = []
        self.refresh_calls = []
        self.raise_on_payload = raise_on_payload

    @contextlib.contextmanager
    def scoped(self, profile):
        self.scopes.append(profile)
        yield type("Ident", (), {"name": profile or "default"})()

    def catalog_payload(self, refresh):
        self.refresh_calls.append(refresh)
        if self.raise_on_payload is not None:
            raise self.raise_on_payload
        return self.payload

    def pricing_probe(self):
        return self.probe


PRICED_PAYLOAD = make_payload(
    make_row("nous", "Nous", models=["hermes-4-405b"],
             pricing={"hermes-4-405b": {
                 "input": "$10.00", "output": "$50.00", "cache": "$0.25",
                 "free": False}}),
    make_row("openrouter", "OpenRouter", models=["vendor/model-x"]),
)


@pytest.fixture()
def fake(monkeypatch):
    adapter = FakeAdapter(
        PRICED_PAYLOAD,
        FakeProbe(picker={"nous": {"hermes-4-405b": {
            "prompt": "0.00001", "completion": "0.00005",
            "input_cache_read": "0.00000025"}}}),
    )
    monkeypatch.setattr(api, "_ADAPTER", adapter)
    monkeypatch.setattr(api, "discover_profiles",
                        lambda: {"default": object(), "work": object()})
    return adapter


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
    r = _client().get("/catalog")
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
    assert set(model) == MODEL_KEYS
    assert model["pricingStatus"] == "priced"
    assert set(model["cost"]) == COST_KEYS
    assert model["cost"]["display"] == {
        "input": "$10.00", "output": "$50.00", "cache": "$0.25"}


def test_catalog_forwards_profile_provider_and_refresh(fake):
    r = _client().get("/catalog?profileId=work&provider=NOUS&refresh=1")
    assert r.status_code == 200
    body = r.json()
    assert body["profileId"] == "work"
    assert [p["id"] for p in body["providers"]] == ["nous"]
    assert fake.scopes == ["work"]
    assert fake.refresh_calls == [True]


def test_catalog_refresh_defaults_to_false(fake):
    _client().get("/catalog")
    assert fake.refresh_calls == [False]


# -- truthful failures ------------------------------------------------------

def test_unknown_profile_is_refused_before_any_lookup(fake):
    r = _client().get("/catalog?profileId=ghost")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "unknown profile 'ghost'"
    assert body["providers"] == []
    assert body["profileId"] == "ghost"
    assert fake.scopes == []  # refused before the adapter looked anything up


def test_zero_provider_rows_is_a_visible_failure(monkeypatch):
    monkeypatch.setattr(api, "_ADAPTER", FakeAdapter(make_payload()))
    monkeypatch.setattr(api, "discover_profiles", lambda: {"default": object()})
    r = _client().get("/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["providers"] == []
    assert body["error"] == "no providers in this profile's catalog"


def test_adapter_exception_is_sanitized(monkeypatch):
    monkeypatch.setattr(api, "_ADAPTER", FakeAdapter(
        {}, raise_on_payload=RuntimeError(
            "Authorization: Bearer secret-test-token /home/lab/config.yaml")))
    monkeypatch.setattr(api, "discover_profiles", lambda: {"default": object()})
    r = _client().get("/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "RuntimeError" in body["error"]
    serialized = json.dumps(body)
    assert "secret-test-token" not in serialized
    assert "Bearer" not in serialized
    assert "/home/" not in serialized


def test_module_load_failure_is_sanitized(monkeypatch):
    def boom():
        raise RuntimeError("Authorization: Bearer secret-test-token")
    monkeypatch.setattr(api, "_model_catalog_module", boom)
    r = _client().get("/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "model catalog module unavailable"
    assert "secret-test-token" not in json.dumps(body)


def test_non_mapping_module_result_is_a_visible_failure(monkeypatch, fake):
    monkeypatch.setattr(api, "_model_catalog_module", lambda: SimpleNamespace(
        get_model_catalog_all=lambda *a, **k: "not-a-mapping",
        get_model_catalog=lambda *a, **k: "not-a-mapping"))
    r = _client().get("/catalog")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"] == "catalog returned an unexpected payload"


# -- read-only discipline ---------------------------------------------------

def test_catalog_does_not_gate_on_runtime_writability(monkeypatch, fake):
    """Catalog reads are safe on a read-only runtime and must not check writes."""
    def refuse():
        raise AssertionError("catalog must not consult _runtime_writable")
    monkeypatch.setattr(api, "_runtime_writable", refuse)
    r = _client().get("/catalog")
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
    r = _client().get("/catalog")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert fake.scopes == [None]


# -- GET /models contract unchanged ----------------------------------------

def test_models_endpoint_contract_unchanged(fake):
    r = _client().get("/models?provider=nous")
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
    body = _client().get("/models").json()
    assert body["ok"] is False
    assert body["models"] == []
    assert body["error"] == "no provider specified"


def test_models_endpoint_unknown_provider_is_still_truthful(fake):
    body = _client().get("/models?provider=ghost").json()
    assert body["ok"] is False
    assert body["models"] == []
    assert "ghost" in body["error"]
    assert fake.scopes == [None]
