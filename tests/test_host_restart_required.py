"""The host's 503 "Restart required" code-skew guard, end to end (no host text).

Upstream Hermes refuses ``GET /api/model/options`` and ``POST /api/model/set``
with HTTP 503 while the running dashboard process is older than the checkout on
disk (``hermes_cli/web_routers/models.py`` ``_dashboard_code_skew_guard``;
introduced by upstream commit f293e7206b, 2026-08-14, #86207 — the guard
predates this plugin's Hermes pin). The UI classifies the host's fixed
``detail`` prefix and forwards ONLY the closed-vocabulary token
``hostRestartRequired``; the host's own sentence never enters this repo.

These tests pin the backend half: the transform honours the token only when the
payload is absent, unknown/junk tokens are ignored (fail-soft back to
``hostCatalogUnavailable``), and a POST /catalog or /models route never echoes
an arbitrary ``optionsFailure`` value back into the DTO.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import dashboard.model_catalog as mc  # noqa: E402
import dashboard.plugin_api as api  # noqa: E402

TOKEN = mc.HOST_RESTART_REQUIRED

# The host's real detail shape (upstream _dashboard_code_skew_guard). Used here
# ONLY as the junk an attacker could try to smuggle through ``optionsFailure``;
# the repo must never echo it back.
HOST_DETAIL = (
    "Restart required: This process is running code from git:main:aaaaaaaaaa "
    "but the checkout on disk is now git:main:bbbbbbbbbb. The model picker "
    "would risk a stale-module crash — restart this Hermes process to load "
    "the new code (hermes dashboard --port <port>, or the equivalent service "
    "restart for this install)")


def row(slug: str, models=(), **extra) -> dict:
    base = {
        "slug": slug,
        "name": slug.upper(),
        "models": list(models),
        "total_models": len(list(models)),
        "source": "hermes",
        "is_current": False,
        "is_user_defined": False,
        "authenticated": True,
    }
    base.update(extra)
    return base


def payload(*rows, **extra) -> dict:
    body = {"providers": list(rows), "model": "", "provider": ""}
    body.update(extra)
    return body


@pytest.fixture()
def nous_payload() -> dict:
    return payload(
        row("nous", ["hermes-4-405b"], is_current=True),
        row("openrouter", ["vendor/model-x"]),
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_home(hermes_home):
    return hermes_home


# -- transform: the token is honoured only for an ABSENT payload --------------

def test_catalog_response_declares_restart_required_without_payload():
    res = mc.catalog_response(None, options_failure=TOKEN)
    assert res["ok"] is False
    assert res["error"] == TOKEN == "hostRestartRequired"
    assert res["providers"] == []


def test_models_response_declares_restart_required_without_payload():
    res = mc.models_response(None, "nous", options_failure=TOKEN)
    assert res["ok"] is False
    assert res["error"] == TOKEN


def test_unknown_or_junk_options_failure_stays_host_unavailable():
    for junk in (None, "", "restart required", "hostRestartRequired ",
                 HOST_DETAIL, {"detail": HOST_DETAIL}, 503):
        assert mc.catalog_response(None, options_failure=junk)["error"] \
            == mc.HOST_UNAVAILABLE
        assert mc.models_response(None, "nous", options_failure=junk)["error"] \
            == mc.HOST_UNAVAILABLE


def test_valid_payload_wins_over_a_declared_failure(nous_payload):
    res = mc.catalog_response(nous_payload, options_failure=TOKEN)
    assert res["ok"] is True
    assert res["error"] is None
    assert [p["id"] for p in res["providers"]] == ["nous", "openrouter"]


def test_unusable_payload_with_declared_failure_names_restart(nous_payload):
    # A present-but-unusable payload is indistinguishable from an absent one at
    # the transform level, so the declared category still wins. The UI only
    # ever declares the token when it received NO payload at all
    # (catalogBody attaches optionsFailure only when options is null).
    res = mc.catalog_response({}, options_failure=TOKEN)
    assert res["error"] == TOKEN
    res = mc.models_response({}, "nous", options_failure=TOKEN)
    assert res["error"] == TOKEN


def test_dto_keys_are_unchanged():
    res = mc.catalog_response(None, options_failure=TOKEN)
    assert set(res) == set(mc.CATALOG_RESULT_KEYS)
    res = mc.models_response(None, "nous", options_failure=TOKEN)
    assert set(res) == set(mc.RESULT_KEYS)


# -- routes: sanitize + pass-through, never echo ------------------------------

def test_models_route_names_restart_required():
    r = _client().post("/models", json={
        "profile": "", "provider": "nous", "options": None,
        "optionsFailure": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == TOKEN


def test_catalog_route_names_restart_required():
    r = _client().post("/catalog", json={
        "profile": "", "options": None, "optionsFailure": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == TOKEN
    assert body["providers"] == []


def test_routes_ignore_arbitrary_options_failure_values():
    # An unknown token (including host-shaped text) must never reach the DTO.
    for junk in (HOST_DETAIL, "anything-goes", {"detail": HOST_DETAIL}):
        r = _client().post("/catalog", json={
            "profile": "", "options": None, "optionsFailure": junk})
        body = r.json()
        assert body["ok"] is False
        assert body["error"] == mc.HOST_UNAVAILABLE
        assert HOST_DETAIL not in json.dumps(body)
        r = _client().post("/models", json={
            "profile": "", "provider": "nous", "options": None,
            "optionsFailure": junk})
        body = r.json()
        assert body["error"] == mc.HOST_UNAVAILABLE
        assert HOST_DETAIL not in json.dumps(body)


def test_options_payload_beats_a_declared_failure(nous_payload):
    # With real evidence in hand, a declared failure is inert and never echoed.
    r = _client().post("/catalog", json={
        "profile": "", "options": nous_payload, "optionsFailure": TOKEN})
    body = r.json()
    assert body["ok"] is True
    assert body["error"] is None


def test_declared_failure_without_a_provider_is_not_a_success():
    r = _client().post("/models", json={"options": None, "optionsFailure": TOKEN})
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "no provider specified"
