"""End-to-end fidelity against a REAL host payload.

These fixtures call ``hermes_cli.inventory.build_model_options_payload`` — the
engine behind ``GET /api/model/options`` — against a throwaway lab home with
fake credentials, and feed the result straight into ``catalog_response`` /
``models_response``.  That pins the transform to the shape the host actually
emits (pricing display strings, ``pricing_pending``, explicit
``authenticated: False`` skeleton rows), not to a hand-written guess.

No provider I/O: every network / background seam the picker build touches is
replaced with a deterministic stub (see ``_stub_host``).  The curated model
lists and the raw pricing payloads are stubbed; the row assembly, the pricing
FORMATTING (``_apply_pricing``), the auth hints and the ordering are real.
"""
from __future__ import annotations

import json

import pytest

import dashboard.model_catalog as mc

CONFIG = """\
model:
  provider: nous
  default: lab-alpha
"""

NOUS_MODELS = ["lab-alpha", "lab-beta", "lab-free", "lab-unpriced"]

# Raw per-token rates exactly as the Portal/models.dev seam returns them; the
# host formats these into display strings ("$3.00", "free", ...).
RAW_PRICING = {
    "lab-alpha": {"prompt": "0.000003", "completion": "0.000015",
                  "input_cache_read": "0.0000003"},
    "lab-beta": {"prompt": "0.0000005", "completion": "0.0000015",
                 "input_cache_read": ""},
    "lab-free": {"prompt": "0", "completion": "0", "input_cache_read": ""},
}


@pytest.fixture()
def host_payload(hermes_home, monkeypatch):
    """The real ``GET /api/model/options`` body for a lab profile, offline."""
    (hermes_home.path / "config.yaml").write_text(CONFIG)

    import agent.models_dev as models_dev
    from hermes_cli import inventory as inv
    from hermes_cli import model_switch_providers as msp
    from hermes_cli import models as hermes_models
    from hermes_cli import models_pricing as pricing

    def _stub_host(mode: str) -> None:
        monkeypatch.setattr(inv, "_prewarm_pricing_async", lambda *a, **k: None)
        monkeypatch.setattr(inv, "_apply_usage", lambda *a, **k: None)
        monkeypatch.setattr(models_dev, "fetch_models_dev", lambda *a, **k: {})
        monkeypatch.setattr(
            hermes_models, "cached_provider_model_ids",
            lambda slug, *a, **k: (list(NOUS_MODELS) if str(slug) == "nous"
                                   else [f"{str(slug).split(':')[-1]}-model"]))
        # Credential DETECTION is a stubbed seam; the row itself is host-built.
        original_creds = msp._overlay_has_creds
        monkeypatch.setattr(
            msp, "_overlay_has_creds",
            lambda b, pid, hermes_slug, overlay: (
                True if pid == "nous" else original_creds(b, pid, hermes_slug, overlay)))
        monkeypatch.setattr(msp, "_nous_picker_model_ids",
                            lambda curated, force_fresh: list(NOUS_MODELS))
        monkeypatch.setattr(hermes_models, "get_cached_nous_free_tier", lambda: False)
        monkeypatch.setattr(hermes_models, "check_nous_free_tier",
                            lambda force_fresh=False: False)
        raw = dict(RAW_PRICING) if mode == "priced" else {}
        monkeypatch.setattr(pricing, "get_pricing_for_provider",
                            lambda slug, **k: dict(raw) if str(slug).lower() == "nous" else {})

    def build(mode: str = "priced") -> dict:
        _stub_host(mode)
        return inv.build_model_options_payload(
            inv.load_picker_context(), include_unconfigured=True, refresh=False)

    return build


def _row(body: dict, slug: str) -> dict:
    return next(r for r in body["providers"] if r.get("slug") == slug)


def _dto(body: dict, slug: str) -> dict:
    res = mc.catalog_response(body, slug)
    assert res["ok"] is True, res
    return res["providers"][0]


# -- shape fidelity ----------------------------------------------------------


def test_real_payload_row_shape_is_what_the_transform_expects(host_payload):
    body = host_payload()
    nous = _row(body, "nous")
    for key in ("slug", "name", "models", "authenticated", "pricing",
                "free_tier", "source"):
        assert key in nous, sorted(nous)
    assert isinstance(nous["pricing"], dict) and nous["pricing"]
    alpha = nous["pricing"]["lab-alpha"]
    # The host gives display strings + an explicit free flag — no raw rates.
    assert set(alpha) >= {"input", "output", "cache", "free"}
    assert alpha["input"] == "$3.00" and alpha["output"] == "$15.00"
    assert alpha["free"] is False
    assert "prompt" not in alpha and "completion" not in alpha


def test_models_response_returns_the_hosts_exact_model_ids(host_payload):
    body = host_payload()
    res = mc.models_response(body, "nous")
    assert res["ok"] is True
    assert [m["id"] for m in res["models"]] == NOUS_MODELS
    assert res["source"] == _row(body, "nous")["source"]
    assert res["fetchedAt"] is None and res["stale"] is True


def test_priced_rows_keep_host_display_and_numeric_rates(host_payload):
    body = host_payload()
    dto = _dto(body, "nous")
    by_id = {m["id"]: m for m in dto["models"]}
    alpha = by_id["lab-alpha"]
    assert alpha["pricingStatus"] == "priced"
    assert alpha["cost"]["display"] == {"input": "$3.00", "output": "$15.00",
                                        "cache": "$0.30"}
    assert alpha["cost"]["input"] == 3.0 and alpha["cost"]["output"] == 15.0
    assert alpha["cost"]["cacheRead"] == 0.3
    assert alpha["cost"]["source"] == "picker"
    beta = by_id["lab-beta"]
    assert beta["pricingStatus"] == "priced"
    assert beta["cost"]["display"] == {"input": "$0.50", "output": "$1.50",
                                       "cache": None}


def test_free_and_unpriced_models_are_classified_truthfully(host_payload):
    body = host_payload()
    by_id = {m["id"]: m for m in _dto(body, "nous")["models"]}
    assert by_id["lab-free"]["pricingStatus"] == "free"
    assert by_id["lab-free"]["cost"]["free"] is True
    assert by_id["lab-unpriced"]["pricingStatus"] == "unknown"
    assert by_id["lab-unpriced"]["cost"] is None
    # A priced provider is not reported as pricing-unavailable.
    assert "nous" not in mc.catalog_response(body)["cost"]["unavailableProviders"]


def test_pricing_pending_payload_keeps_every_model_unknown(host_payload):
    body = host_payload("pending")
    nous_row = _row(body, "nous")
    assert nous_row.get("pricing_pending") is True
    res = mc.catalog_response(body, "nous")
    assert res["cost"]["pending"] is True
    assert {m["pricingStatus"] for m in res["providers"][0]["models"]} == {"unknown"}
    assert all(m["cost"] is None for m in res["providers"][0]["models"])


def test_explicit_unauthenticated_skeleton_rows_stay_unauthenticated(host_payload):
    body = host_payload()
    skeletons = [r for r in body["providers"] if r.get("authenticated") is False]
    assert skeletons, "expected include_unconfigured skeleton rows"
    for raw in skeletons:
        dto = _dto(body, raw["slug"])
        assert dto["authenticated"] is False
        assert dto["configured"] is False
        assert dto["modelCount"] == 0
        assert mc.provider_authenticated(body, raw["slug"]) is False
    assert mc.provider_authenticated(body, "nous") is True


def test_authenticated_rows_are_the_ones_with_credentials(host_payload):
    body = host_payload()
    authenticated = {r["slug"] for r in body["providers"] if r.get("authenticated") is True}
    assert "nous" in authenticated
    assert mc.provider_authenticated(body, "nous") is True
    assert mc.provider_authenticated(body, "not-a-provider") is None


# -- duplicate slug, malformed and absent payloads ---------------------------


def test_duplicate_slug_rows_from_the_host_resolve_to_the_authenticated_row(host_payload):
    body = host_payload()
    nous = dict(_row(body, "nous"))
    duplicate = dict(nous, authenticated=False, models=[], pricing={},
                     warning="paste NOUS_API_KEY to activate")
    body = dict(body, providers=list(body["providers"]) + [duplicate])
    assert mc.provider_authenticated(body, "nous") is True
    assert [p["id"] for p in mc.models_response(body, "nous")["providers"]].count("nous") == 2
    assert [m["id"] for m in mc.models_response(body, "nous")["models"]] == NOUS_MODELS
    dto = _dto(body, "nous")
    assert dto["authenticated"] is True
    assert {m["id"]: m["pricingStatus"] for m in dto["models"]}["lab-alpha"] == "priced"


def test_absent_host_payload_is_the_fixed_reason():
    for body in (None, {"providers": None}, "options"):
        assert mc.catalog_response(body)["error"] == mc.HOST_UNAVAILABLE
        assert mc.models_response(body, "nous")["error"] == mc.HOST_UNAVAILABLE


def test_whole_payload_never_leaks_host_row_chrome(host_payload):
    body = host_payload()
    dumped = json.dumps(mc.catalog_response(body))
    for forbidden in ("capabilities", "api_url", "usage", "limit",
                      "featured", "key_env", "auth_type", "http://",
                      "is_user_defined", "total_models"):
        assert forbidden not in dumped
