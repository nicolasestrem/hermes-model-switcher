"""Catalog truthfulness: a missing host payload is a failure, never an empty success.

The transform only knows what the host's ``GET /api/model/options`` body says.
When that body is absent, malformed, or carries a row it cannot shape, the DTO
must say so — with a fixed reason, no exception text, no invented freshness and
no fabricated price.  A refresh request is not evidence that anything was
discovered.
"""
from __future__ import annotations

import json

import pytest

from dashboard import model_catalog


@pytest.fixture(autouse=True)
def isolated_catalog_home(hermes_home):
    return hermes_home


def row(slug: str, models=(), **extra) -> dict:
    base = {"slug": slug, "name": slug.upper(), "models": list(models),
            "total_models": len(list(models)), "source": "hermes",
            "is_current": False, "is_user_defined": False, "authenticated": True}
    base.update(extra)
    return base


def payload(*rows, **extra) -> dict:
    body = {"providers": list(rows), "model": "", "provider": ""}
    body.update(extra)
    return body


@pytest.mark.parametrize("bad_payload", [None, [], "", 0, {}, {"providers": 3}])
def test_a_missing_or_malformed_payload_is_a_fixed_reason_not_an_empty_success(bad_payload):
    catalog = model_catalog.catalog_response(bad_payload)
    models = model_catalog.models_response(bad_payload, "nous")
    assert catalog["ok"] is False and catalog["error"] == "hostCatalogUnavailable"
    assert catalog["providers"] == []
    assert models["ok"] is False and models["error"] == "hostCatalogUnavailable"
    assert models["fetchedAt"] is None and models["stale"] is True


def test_host_unavailable_is_a_constant_not_a_message():
    assert model_catalog.HOST_UNAVAILABLE == "hostCatalogUnavailable"
    res = model_catalog.catalog_response(None)
    assert res["error"] == model_catalog.HOST_UNAVAILABLE
    assert res["providers"] == []
    assert res["cost"] == {"pending": False, "unavailableProviders": []}


def test_failure_message_does_not_publish_exception_contents():
    class Boom:
        def __str__(self):
            raise RuntimeError("Authorization: Bearer secret-test-token")

    res = model_catalog.catalog_response(payload(row("nous", [Boom()])))
    dumped = json.dumps(res)
    assert "secret-test-token" not in dumped
    assert "Bearer" not in dumped
    assert "/home/" not in dumped
    assert res["providers"][0]["warning"] == "provider catalog unavailable: RuntimeError"
    assert res["error"] is None


def test_refresh_without_discovery_evidence_is_never_reported_as_fresh():
    body = payload(row("nous", ["hermes-4-405b"]))
    for refresh in (False, True):
        res = model_catalog.catalog_response(body, "nous", refresh=refresh)
        provider = res["providers"][0]
        assert provider["stale"] is True
        assert provider["fetchedAt"] is None
        models = model_catalog.models_response(body, "nous", refresh=refresh)
        assert models["stale"] is True
        assert models["fetchedAt"] is None


def test_warning_survives_a_nonempty_curated_catalog():
    body = payload(row("nous", ["deepseek/deepseek-v4.1-flash"],
                       warning="Live lookup failed; curated fallback", source="hermes"))
    result = model_catalog.models_response(body, "nous")
    assert result["ok"] is True
    assert result["error"] == "Live lookup failed; curated fallback"
    catalog = model_catalog.catalog_response(body, "nous")
    assert catalog["providers"][0]["warning"] == "Live lookup failed; curated fallback"


def test_unknown_price_is_absent_not_zero():
    """A model the host did not price has ``cost: None`` — the UI cannot render
    $0.00 or a free badge from absent evidence."""
    res = model_catalog.catalog_response(payload(row("alpha", ["unpriced"])))
    model = res["providers"][0]["models"][0]
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None
    assert res["cost"]["pending"] is False


def test_no_raw_row_fields_or_paths_reach_the_catalog_dto():
    body = payload(row("custom:lab", ["lab-7b"], api_url="http://127.0.0.1:9000/v1",
                       key_env="LAB_KEY", headers={"Authorization": "Bearer secret-test-token"},
                       usage={"windows": []}, capabilities={"lab-7b": {}},
                       snapshot="/".join(("", "home", "lab", "state", "snapshots", "1")), raw_config="model: {}"))
    dumped = json.dumps(model_catalog.catalog_response(body))
    for forbidden in ("api_url", "http://", "key_env", "LAB_KEY", "Authorization",
                      "secret-test-token", "snapshot", "raw_config", "/home/"):
        assert forbidden not in dumped


def test_provider_authentication_evidence_is_never_assumed_from_absence():
    body = payload(row("nous", ["m1"], authenticated=True),
                   row("openai", [], authenticated=False),
                   {"slug": "mystery", "models": ["m2"]})
    assert model_catalog.provider_authenticated(body, "nous") is True
    assert model_catalog.provider_authenticated(body, "openai") is False
    assert model_catalog.provider_authenticated(body, "mystery") is None
    assert model_catalog.provider_authenticated(body, "absent") is None
    assert model_catalog.provider_authenticated(None, "nous") is None
    assert model_catalog.provider_authenticated({"providers": {}}, "nous") is None
    assert model_catalog.provider_authenticated(body, "") is None


def test_authentication_prefers_an_authenticated_duplicate_row():
    duplicate = payload(row("nous", [], authenticated=False),
                        row("nous", ["m1"], authenticated=True))
    assert model_catalog.provider_authenticated(duplicate, "nous") is True
    only_false = payload(row("nous", [], authenticated=False))
    assert model_catalog.provider_authenticated(only_false, "nous") is False


def test_authentication_matches_slug_or_provider_id_case_insensitively():
    body = payload(row("custom:lab", ["m1"], provider_id="Lab-Endpoint", authenticated=True))
    assert model_catalog.provider_authenticated(body, "LAB-ENDPOINT") is True
    assert model_catalog.provider_authenticated(body, "custom:lab") is True
