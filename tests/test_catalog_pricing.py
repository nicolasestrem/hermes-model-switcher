"""``catalog_response`` pricing — display text passes through, numbers never invented.

Every fixture is a HOST OPTIONS PAYLOAD row (``GET /api/model/options``): the
pricing map the host picker builds, whose entries are DISPLAY strings plus an
explicit both-directions ``free`` flag.  There are no raw per-token rates and
no billing-route provenance in that payload, so the DTO's numeric cost fields
are always ``None`` — this suite pins that, and pins the rules that keep an
unknown price from ever rendering as ``free``/``$0.00``.
"""
from __future__ import annotations

import json

import pytest

import dashboard.model_catalog as mc


# -- host-payload fixtures ---------------------------------------------------


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


def entry(inp=None, out=None, cache=None, free=False) -> dict:
    """One host pricing entry (already formatted per 1M tokens)."""
    return {"input": inp, "output": out, "cache": cache, "free": free}


def one_provider(row_obj, provider="alpha"):
    res = mc.catalog_response(payload(row_obj), provider)
    assert res["ok"] is True, res
    assert len(res["providers"]) == 1, res["providers"]
    return res, res["providers"][0]


def model_of(provider_dto, model_id):
    return next(m for m in provider_dto["models"] if m["id"] == model_id)


# -- display text is the only price evidence --------------------------------


def test_priced_model_keeps_the_host_display_verbatim_and_carries_no_numeric_rate():
    r = row("alpha", ["m1"], pricing={"m1": entry("$3.00", "$15.00", "$0.30")})
    _, dto = one_provider(r)
    m = model_of(dto, "m1")
    assert m["pricingStatus"] == "priced"
    assert m["cost"]["display"] == {"input": "$3.00", "output": "$15.00", "cache": "$0.30"}
    assert m["cost"]["input"] is None
    assert m["cost"]["output"] is None
    assert m["cost"]["cacheRead"] is None
    assert m["cost"]["free"] is False
    assert m["cost"]["unit"] == mc.COST_UNIT
    assert m["cost"]["source"] == "picker"
    assert m["cost"]["pricingVersion"] is None


def test_sub_cent_display_is_never_rounded_or_parsed_into_a_number():
    r = row("alpha", ["m1"], pricing={"m1": entry("$0.00009", "$0.30", None)})
    _, dto = one_provider(r)
    m = model_of(dto, "m1")
    assert m["cost"]["display"]["input"] == "$0.00009"
    assert m["cost"]["display"]["cache"] is None
    assert m["cost"]["input"] is None


def test_numeric_rates_are_never_reconstructed_from_display_text():
    r = row("alpha", ["m1"], pricing={"m1": entry("$3.00", "$15.00", "$0.30")})
    _, dto = one_provider(r)
    cost = model_of(dto, "m1")["cost"]
    assert cost["input"] is None
    assert cost["output"] is None
    assert cost["cacheRead"] is None
    assert cost["display"]["input"] == "$3.00"


def test_explicit_free_flag_is_the_only_free_evidence():
    r = row("alpha", ["freebie"], pricing={"freebie": entry("free", "free", None, free=True)})
    _, dto = one_provider(r)
    m = model_of(dto, "freebie")
    assert m["pricingStatus"] == "free"
    assert m["cost"]["free"] is True
    assert m["cost"]["display"] == {"input": "free", "output": "free", "cache": None}


def test_per_direction_free_is_a_priced_model_not_a_free_one():
    r = row("alpha", ["cheap"], pricing={"cheap": entry("free", "$5.00", None, free=False)})
    _, dto = one_provider(r)
    m = model_of(dto, "cheap")
    assert m["pricingStatus"] == "priced"
    assert m["cost"]["free"] is False
    assert m["cost"]["display"]["input"] == "free"       # verbatim, that direction only
    assert m["cost"]["display"]["output"] == "$5.00"


def test_all_free_display_without_the_host_flag_stays_unknown():
    r = row("alpha", ["m1"], pricing={"m1": {"input": "free", "output": "free", "cache": None}})
    _, dto = one_provider(r)
    m = model_of(dto, "m1")
    assert m["pricingStatus"] == "unknown"
    assert m["cost"] is None


def test_included_status_is_unreachable_from_a_host_payload():
    r = row("alpha", ["m1"], pricing={"m1": entry("$1.00", "$2.00", None)})
    _, dto = one_provider(r)
    assert model_of(dto, "m1")["pricingStatus"] != "included"


# -- unknown stays unknown ---------------------------------------------------


def test_missing_pricing_entry_is_unknown_not_free():
    r = row("alpha", ["m1", "m2"], pricing={"m1": entry("$1.00", "$2.00", None)})
    _, dto = one_provider(r)
    m2 = model_of(dto, "m2")
    assert m2["pricingStatus"] == "unknown"
    assert m2["cost"] is None


def test_empty_pricing_entry_is_unknown():
    r = row("alpha", ["m1"], pricing={"m1": {}})
    _, dto = one_provider(r)
    assert model_of(dto, "m1")["pricingStatus"] == "unknown"
    assert model_of(dto, "m1")["cost"] is None


@pytest.mark.parametrize("bad_pricing", ["$3.00", ["m1"], 7, {"m1": "$3.00"}, {"m1": None}])
def test_malformed_pricing_shapes_are_unknown(bad_pricing):
    r = row("alpha", ["m1"], pricing=bad_pricing)
    res = mc.catalog_response(payload(r), "alpha")
    assert res["ok"] is True
    assert model_of(res["providers"][0], "m1")["cost"] is None


def test_pricing_join_is_exact_id_never_fuzzy():
    r = row("alpha", ["m1", "M2"], pricing={
        "m1:free": entry("$1.00", "$2.00", None),
        "m2": entry("$3.00", "$4.00", None),
    })
    _, dto = one_provider(r)
    assert model_of(dto, "m1")["pricingStatus"] == "unknown"
    assert model_of(dto, "M2")["pricingStatus"] == "unknown"


def test_pricing_for_models_the_row_does_not_offer_is_not_emitted():
    r = row("alpha", ["m1"], pricing={
        "m1": entry("$1.00", "$2.00", None),
        "ghost": entry("$9.00", "$9.00", None),
    })
    _, dto = one_provider(r)
    assert [m["id"] for m in dto["models"]] == ["m1"]
    assert "ghost" not in json.dumps(dto)


# -- pending vs unavailable --------------------------------------------------


def test_pricing_pending_makes_every_model_unknown_and_the_catalog_pending():
    r = row("nous", ["m1"], pricing_pending=True,
            pricing={"m1": entry("$3.00", "$15.00", None)})  # stale map must not be trusted
    res, dto = one_provider(r, "nous")
    assert model_of(dto, "m1")["pricingStatus"] == "unknown"
    assert model_of(dto, "m1")["cost"] is None
    assert res["cost"]["pending"] is True
    assert res["cost"]["unavailableProviders"] == []


def test_free_tier_pending_is_pending_and_locks_every_model():
    r = row("nous", ["m1", "m2"], free_tier_pending=True,
            unavailable_models=["m1", "m2"], warning="Checking Nous plan entitlement…")
    res, dto = one_provider(r, "nous")
    assert {m["pricingStatus"] for m in dto["models"]} == {"unknown"}
    assert res["cost"]["pending"] is True
    assert res["cost"]["unavailableProviders"] == []


def test_provider_with_no_pricing_at_all_is_listed_as_unavailable():
    res = mc.catalog_response(payload(row("alpha", ["m1"]), row("beta", ["m9"])), "alpha")
    assert res["cost"]["unavailableProviders"] == ["alpha"]   # this response's provider
    both = mc.catalog_response(payload(row("alpha", ["m1"]), row("beta", ["m9"])))
    assert both["cost"]["unavailableProviders"] == ["alpha", "beta"]
    assert all(p["models"][0]["cost"] is None for p in both["providers"])


def test_partially_priced_provider_is_not_unavailable():
    r = row("alpha", ["m1", "m2"], pricing={"m1": entry("$1.00", "$2.00", None)})
    res, _ = one_provider(r)
    assert res["cost"]["unavailableProviders"] == []


def test_virtual_free_tier_row_without_pricing_is_not_unavailable():
    r = row("nous-free", ["m1"], free_tier_row=True)
    res, _ = one_provider(r, "nous-free")
    assert res["cost"]["unavailableProviders"] == []


def test_pending_on_another_provider_does_not_leak_into_a_filtered_response():
    body = payload(row("nous", ["m1"], pricing_pending=True), row("alpha", ["m2"]))
    res = mc.catalog_response(body, "alpha")
    assert res["cost"] == {"pending": False, "unavailableProviders": ["alpha"]}


def test_payload_level_pricing_pending_flag_applies_only_to_the_unfiltered_catalog():
    body = payload(row("alpha", ["m1"], pricing={"m1": entry("$1.00", "$2.00")}),
                   pricing_pending=True)
    assert mc.catalog_response(body)["cost"]["pending"] is True
    assert mc.catalog_response(body, "alpha")["cost"]["pending"] is False


# -- isolation and shape -----------------------------------------------------


def test_one_broken_row_never_hides_the_other_providers():
    class Boom:
        def __str__(self):
            raise RuntimeError("Authorization: Bearer secret-test-token")

    body = payload(row("broken", [Boom()]), row("alpha", ["m1"]))
    res = mc.catalog_response(body)
    assert res["ok"] is True
    assert res["error"] is None
    broken = res["providers"][0]
    assert broken["id"] == "broken"
    assert broken["models"] == []
    assert broken["warning"] == "provider catalog unavailable: RuntimeError"
    assert res["providers"][1]["models"][0]["id"] == "m1"
    assert res["cost"]["unavailableProviders"] == ["alpha", "broken"]
    dumped = json.dumps(res)
    assert "secret-test-token" not in dumped and "Bearer" not in dumped


def test_cost_and_model_and_provider_shapes_are_the_frozen_allowlist():
    r = row("alpha", ["m1"], pricing={"m1": entry("$1.00", "$2.00", "$0.10")})
    res, dto = one_provider(r)
    assert set(dto) == set(mc.PROVIDER_KEYS)
    m = dto["models"][0]
    assert set(m) == set(mc.MODEL_KEYS)
    assert set(m["cost"]) == set(mc.COST_KEYS)
    assert set(m["cost"]["display"]) == set(mc.COST_DISPLAY_KEYS)
    assert m["pricingStatus"] in mc.PRICING_STATUSES
    assert res["cost"]["pending"] is False
    assert set(res) == set(mc.CATALOG_RESULT_KEYS)


def test_sale_chrome_from_the_host_is_not_turned_into_rates():
    r = row("nous", ["m1"], pricing={"m1": {
        "input": "$1.50", "output": "$7.50", "cache": None, "free": False,
        "discount_percent": 50, "was_input": "$3.00", "was_output": "$15.00",
    }})
    _, dto = one_provider(r, "nous")
    cost = model_of(dto, "m1")["cost"]
    assert cost["display"] == {"input": "$1.50", "output": "$7.50", "cache": None}
    assert cost["input"] is None
    assert "discount_percent" not in cost and "was_input" not in cost


def test_provider_dto_reports_authentication_and_catalog_identity():
    r = row("alpha", ["m1", "m2"], is_current=True, source="canonical")
    _, dto = one_provider(r)
    assert dto["id"] == "alpha"
    assert dto["label"] == "ALPHA"
    assert dto["authenticated"] is True
    assert dto["configured"] is True
    assert dto["isCurrent"] is True
    assert dto["modelCount"] == 2
    assert dto["warning"] is None
    assert dto["fetchedAt"] is None and dto["stale"] is True


def test_explicitly_unauthenticated_skeleton_row_is_not_configured():
    r = row("openai", [], authenticated=False, warning="paste OPENAI_API_KEY to activate")
    _, dto = one_provider(r, "openai")
    assert dto["authenticated"] is False
    assert dto["configured"] is False
    assert dto["modelCount"] == 0
    assert dto["warning"] == "paste OPENAI_API_KEY to activate"


def test_catalog_label_falls_back_to_the_slug_when_the_host_omits_a_name():
    r = {"slug": "lab", "models": ["m1"], "authenticated": True}
    _, dto = one_provider(r, "lab")
    assert dto["label"] == "lab"


def test_duplicate_slug_rows_are_merged_keeping_the_authenticated_pricing():
    body = payload(
        row("nous", [], authenticated=False, warning="paste NOUS_API_KEY to activate"),
        row("nous", ["m1"], pricing={"m1": entry("$3.00", "$15.00", None)}),
    )
    res = mc.catalog_response(body)
    assert len(res["providers"]) == 1
    dto = res["providers"][0]
    assert dto["authenticated"] is True
    assert dto["warning"] is None
    assert model_of(dto, "m1")["pricingStatus"] == "priced"


def test_provider_filter_picks_the_authenticated_duplicate_row():
    body = payload(
        row("nous", [], authenticated=False),
        row("nous", ["m1"], pricing={"m1": entry("$3.00", "$15.00", None)}),
    )
    res = mc.catalog_response(body, "nous")
    assert res["providers"][0]["modelCount"] == 1
    assert model_of(res["providers"][0], "m1")["pricingStatus"] == "priced"


def test_refresh_never_manufactures_freshness():
    body = payload(row("alpha", ["m1"], pricing={"m1": entry("$1.00", "$2.00")}))
    for refresh in (False, True):
        res = mc.catalog_response(body, "alpha", refresh=refresh)
        assert res["providers"][0]["fetchedAt"] is None
        assert res["providers"][0]["stale"] is True
