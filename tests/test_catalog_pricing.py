"""Aggregate provider -> models -> cost catalog: pricing truthfulness.

Owns ``dashboard/model_catalog.py``'s ``get_model_catalog_all`` surface. Every
test runs against a throwaway /tmp Hermes home and a fake adapter/pricing probe:
no live config, no network, no inference, no writes.

Pricing rules pinned here:
  * display strings are the picker's verbatim strings; numerics come ONLY from
    raw cached per-token rates (never parsed out of ``$`` strings),
  * ``free`` only on an explicit free flag (both-zero rule); "free input +
    paid output" is priced, not free,
  * a missing rate is ``cost: null`` + ``pricingStatus: "unknown"`` — NEVER
    ``free`` and never ``$0.00``,
  * model ids join by exact id, so the same id under two providers keeps each
    provider's price.
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Built at runtime: the repo carries no baked-in ``/home/<user>`` literal (the
# skeleton scan forbids host-specific strings in tracked files), while this
# test still proves a real absolute host path never reaches a surfaced error.
LAB_HOST_PATH = str(Path("/home") / "lab" / "config.yaml")

import dashboard.model_catalog as mc  # noqa: E402

TOP_KEYS = {"ok", "profileId", "providers", "cost", "error"}
PROVIDER_KEYS = {"id", "label", "authenticated", "configured", "isCurrent",
                 "modelCount", "fetchedAt", "stale", "warning", "models"}
MODEL_KEYS = {"id", "label", "pricingStatus", "cost"}
COST_KEYS = {"unit", "input", "output", "cacheRead", "display", "free",
             "source", "pricingVersion"}


@pytest.fixture(autouse=True)
def isolated_catalog_home(hermes_home):
    return hermes_home


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
    """Canned offline pricing sources; records every lookup."""

    def __init__(self, *, picker=None, routes=None, entries=None,
                 models_dev=None, available=True, explode=()):
        self.picker = picker or {}
        self.routes = routes or {}
        self.entries = entries or {}
        self.models_dev = models_dev or {}
        self.available = available
        self.unavailable = () if available else ("pricing",)
        self.explode = set(explode)
        self.calls = []

    def picker_pricing(self, provider):
        self.calls.append(("picker", provider))
        if provider in self.explode:
            raise RuntimeError("Authorization: Bearer secret-test-token")
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


def _model(provider, model_id):
    for item in provider["models"]:
        if item["id"] == model_id:
            return item
    raise AssertionError(f"{model_id!r} not in provider {provider['id']!r}")


# -- enumeration -----------------------------------------------------------

def test_every_provider_row_once_in_native_order_with_exact_ids():
    payload = make_payload(
        make_row("nous", "Nous", models=["hermes-4-405b", "deepseek/deepseek-v4.1-flash"]),
        make_row("openrouter", "OpenRouter", models=["vendor/model-x"]),
        make_row("opencode-go", "OpenCode Go", models=["deepseek-v4.1-flash"]),
    )
    res = mc.get_model_catalog_all(FakeAdapter(payload), None)
    assert res["ok"] is True and res["error"] is None
    assert [p["id"] for p in res["providers"]] == ["nous", "openrouter", "opencode-go"]
    assert [p["label"] for p in res["providers"]] == ["Nous", "OpenRouter", "OpenCode Go"]
    assert [m["id"] for m in res["providers"][0]["models"]] == [
        "hermes-4-405b", "deepseek/deepseek-v4.1-flash"]
    assert [m["label"] for m in res["providers"][0]["models"]] == [
        "hermes-4-405b", "deepseek/deepseek-v4.1-flash"]


def test_duplicate_slug_keeps_the_authenticated_row_in_native_position():
    payload = make_payload(
        make_row("nous", "Nous skeleton", models=[], source="canonical",
                 authenticated=False, warning="paste NOUS_API_KEY to activate"),
        make_row("openrouter", "OpenRouter", models=["m"]),
        make_row("nous", "Nous", models=["hermes-4-405b"]),
    )
    res = mc.get_model_catalog_all(FakeAdapter(payload), None)
    assert [p["id"] for p in res["providers"]] == ["nous", "openrouter"]
    nous = res["providers"][0]
    assert nous["label"] == "Nous"
    assert nous["authenticated"] is True
    assert nous["modelCount"] == 1
    assert [m["id"] for m in nous["models"]] == ["hermes-4-405b"]
    assert nous["warning"] is None


def test_skeleton_row_is_not_configured():
    payload = make_payload(make_row(
        "opencode-go", "OpenCode Go", models=[], source="canonical",
        authenticated=False, warning="paste OPENCODE_GO_API_KEY to activate"))
    res = mc.get_model_catalog_all(FakeAdapter(payload), None)
    provider = res["providers"][0]
    assert provider["authenticated"] is False
    assert provider["configured"] is False
    assert provider["modelCount"] == 0
    assert provider["warning"] == "paste OPENCODE_GO_API_KEY to activate"


def test_dict_shaped_model_entries_normalize_to_exact_ids_and_drop_empties():
    payload = make_payload(make_row(
        "nous", "Nous",
        models=[{"id": "hermes-4-405b"}, "plain-id", {"name": "no id"}, "", None]))
    res = mc.get_model_catalog_all(FakeAdapter(payload), None)
    provider = res["providers"][0]
    assert [m["id"] for m in provider["models"]] == ["hermes-4-405b", "plain-id"]
    assert provider["modelCount"] == 2


# -- picker pricing --------------------------------------------------------

def test_picker_display_verbatim_and_numerics_from_raw_token_rates_only():
    """Display strings pass through byte-exact; numerics never parse '$' text."""
    payload = make_payload(make_row(
        "nous", "Nous", models=["hermes-4-405b"],
        pricing={"hermes-4-405b": {
            "input": "$10.00", "output": "$50.00", "cache": "$0.25", "free": False}}))
    probe = FakeProbe(picker={"nous": {"hermes-4-405b": {
        # Deliberately NOT 10/50/0.25: proves numerics come from raw rates.
        "prompt": "0.00002", "completion": "0.00004",
        "input_cache_read": "0.0000005"}}})
    res = mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    model = _model(res["providers"][0], "hermes-4-405b")
    assert model["pricingStatus"] == "priced"
    cost = model["cost"]
    assert set(cost) == COST_KEYS
    assert cost["unit"] == "USD_per_1M_tokens"
    assert cost["input"] == 20.0
    assert cost["output"] == 40.0
    assert cost["cacheRead"] == 0.5
    assert cost["display"] == {"input": "$10.00", "output": "$50.00", "cache": "$0.25"}
    assert cost["free"] is False
    assert cost["source"] == "picker"
    assert cost["pricingVersion"] is None


def test_sub_cent_picker_display_is_passed_through_and_formatted_from_raw():
    payload = make_payload(make_row(
        "nous", "Nous", models=["cheap"],
        pricing={"cheap": {"input": "$0.0046", "output": "free", "cache": None, "free": False}}))
    probe = FakeProbe(picker={"nous": {"cheap": {
        "prompt": "0.0000000046", "completion": "0"}}})
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0], "cheap")
    assert model["pricingStatus"] == "priced"
    assert model["cost"]["input"] == 0.0046
    assert model["cost"]["display"] == {
        "input": "$0.0046", "output": "free", "cache": None}


def test_free_only_on_explicit_flag_and_both_zero():
    payload = make_payload(make_row(
        "nous", "Nous", models=["free-model"],
        pricing={"free-model": {"input": "free", "output": "free", "cache": None, "free": True}}))
    probe = FakeProbe(picker={"nous": {"free-model": {"prompt": "0", "completion": "0"}}})
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0],
                   "free-model")
    assert model["pricingStatus"] == "free"
    assert model["cost"]["free"] is True
    assert model["cost"]["input"] == 0.0
    assert model["cost"]["output"] == 0.0
    assert model["cost"]["display"] == {"input": "free", "output": "free", "cache": None}


def test_free_input_but_paid_output_is_not_free():
    payload = make_payload(make_row(
        "nous", "Nous", models=["hybrid"],
        pricing={"hybrid": {"input": "free", "output": "$5.00", "cache": None, "free": False}}))
    probe = FakeProbe(picker={"nous": {"hybrid": {"prompt": "0", "completion": "0.000005"}}})
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0],
                   "hybrid")
    assert model["pricingStatus"] == "priced"
    assert model["cost"]["free"] is False
    assert model["cost"]["input"] == 0.0
    assert model["cost"]["output"] == 5.0


def test_missing_pricing_is_unknown_never_zero_dollars_or_free():
    payload = make_payload(make_row("nous", "Nous", models=["mystery"], pricing={}))
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload), None)["providers"][0], "mystery")
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None
    assert model["pricingStatus"] != "free"
    serialized = json.dumps(model)
    assert "$0.00" not in serialized and "$0" not in serialized


def test_zero_numeric_rates_without_free_flag_are_unknown_not_free():
    """0.0 cannot distinguish free from no-data; only an explicit flag may."""
    payload = make_payload(make_row(
        "openai", "OpenAI", models=["gpt-zero"],
        pricing={"gpt-zero": {"input": "$0.00", "output": "$0.00", "cache": None, "free": False}}))
    probe = FakeProbe(picker={"openai": {"gpt-zero": {"prompt": "0", "completion": "0"}}})
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0],
                   "gpt-zero")
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None


def test_picker_display_survives_a_cold_raw_pricing_cache():
    """The row's display price is not discarded when raw rates are cached out."""
    payload = make_payload(make_row(
        "nous", "Nous", models=["hermes-4-405b"],
        pricing={"hermes-4-405b": {
            "input": "$3.00", "output": "$15.00", "cache": "$0.30", "free": False}}))
    res = mc.get_model_catalog_all(FakeAdapter(payload, FakeProbe()), None)
    model = _model(res["providers"][0], "hermes-4-405b")
    assert model["pricingStatus"] == "priced"
    cost = model["cost"]
    assert set(cost) == COST_KEYS
    assert cost["display"] == {"input": "$3.00", "output": "$15.00", "cache": "$0.30"}
    # Display text is never parsed into numbers: no raw rate, no numeric claim.
    assert cost["input"] is None and cost["output"] is None and cost["cacheRead"] is None
    assert cost["free"] is False
    assert cost["source"] == "picker"
    assert cost["pricingVersion"] is None
    json.dumps(res, allow_nan=False)


def test_picker_display_only_free_honours_the_explicit_flag():
    payload = make_payload(make_row(
        "nous", "Nous", models=["free-model"],
        pricing={"free-model": {"input": "free", "output": "free", "cache": None, "free": True}}))
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, FakeProbe()), None)["providers"][0],
                   "free-model")
    assert model["pricingStatus"] == "free"
    cost = model["cost"]
    assert cost["free"] is True
    assert cost["display"] == {"input": "free", "output": "free", "cache": None}
    assert cost["input"] is None and cost["output"] is None


def test_picker_display_only_one_direction_free_is_not_a_free_model():
    payload = make_payload(make_row(
        "nous", "Nous", models=["hybrid"],
        pricing={"hybrid": {"input": "free", "output": "$5.00", "cache": None, "free": False}}))
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, FakeProbe()), None)["providers"][0],
                   "hybrid")
    assert model["pricingStatus"] == "priced"
    assert model["cost"]["free"] is False
    assert model["cost"]["display"] == {"input": "free", "output": "$5.00", "cache": None}
    assert model["cost"]["input"] is None and model["cost"]["output"] is None


def test_picker_all_free_display_without_flag_is_unknown_not_free():
    """Zero display strings cannot prove a free model any more than 0.0 can."""
    payload = make_payload(make_row(
        "nous", "Nous", models=["zero-display"],
        pricing={"zero-display": {"input": "free", "output": "free",
                                  "cache": None, "free": False}}))
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, FakeProbe()), None)["providers"][0],
                   "zero-display")
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None


# -- billing-route / models.dev fallbacks ----------------------------------

def test_subscription_route_is_included_not_free():
    payload = make_payload(make_row("openai-codex", "OpenAI Codex",
                                    models=["gpt-5.6-sol"]))
    probe = FakeProbe(routes={("openai-codex", "gpt-5.6-sol"): "subscription_included"})
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0],
                   "gpt-5.6-sol")
    assert model["pricingStatus"] == "included"
    cost = model["cost"]
    assert cost["source"] == "subscription_route"
    assert cost["pricingVersion"] == "included-route"
    assert cost["input"] == 0.0 and cost["output"] == 0.0
    assert cost["free"] is False
    assert cost["display"] == {"input": "$0.00", "output": "$0.00", "cache": "$0.00"}


def test_official_docs_snapshot_preserves_version_and_rates():
    payload = make_payload(make_row("openai", "OpenAI", models=["gpt-4o-mini"]))
    probe = FakeProbe(
        routes={("openai", "gpt-4o-mini"): "official_docs_snapshot"},
        entries={("openai", "gpt-4o-mini"): {
            "input": 0.15, "output": 0.6, "cache_read": 0.075,
            "source": "official_docs_snapshot",
            "pricing_version": "openai-pricing-2026-03-16"}},
    )
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0],
                   "gpt-4o-mini")
    assert model["pricingStatus"] == "priced"
    cost = model["cost"]
    assert cost["source"] == "official_docs_snapshot"
    assert cost["pricingVersion"] == "openai-pricing-2026-03-16"
    assert (cost["input"], cost["output"], cost["cacheRead"]) == (0.15, 0.6, 0.075)
    assert cost["display"] == {"input": "$0.15", "output": "$0.60", "cache": "$0.075"}


def test_models_dev_fallback_is_labelled_models_dev_not_snapshot():
    payload = make_payload(make_row("google", "Google", models=["gemini-lite"]))
    probe = FakeProbe(
        routes={("google", "gemini-lite"): "official_docs_snapshot"},
        # get_pricing_entry can fall back to models.dev internally; that data
        # must NOT be claimed as an official-docs snapshot.
        entries={("google", "gemini-lite"): {
            "input": 9.9, "output": 9.9, "cache_read": None,
            "source": "provider_models_api", "pricing_version": "models.dev"}},
        models_dev={("google", "gemini-lite"): {
            "input": 0.1, "output": 0.4, "cache_read": None}},
    )
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0],
                   "gemini-lite")
    assert model["pricingStatus"] == "priced"
    assert model["cost"]["source"] == "models_dev"
    assert model["cost"]["input"] == 0.1
    assert model["cost"]["display"] == {"input": "$0.10", "output": "$0.40", "cache": None}
    assert model["cost"]["pricingVersion"] is None


def test_models_dev_zero_cost_dict_is_unknown_not_free():
    payload = make_payload(make_row("openai", "OpenAI", models=["zero-model"]))
    probe = FakeProbe(models_dev={("openai", "zero-model"): {
        "input": 0.0, "output": 0.0, "cache_read": None}})
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0],
                   "zero-model")
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None


# -- numeric guard: non-finite / negative / bool rates ---------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                 -0.5, True])
def test_snapshot_cost_rejects_non_finite_negative_and_bool_rates(bad):
    entry = {"source": "official_docs_snapshot", "pricing_version": "v",
             "input": bad, "output": bad, "cache_read": bad}
    assert mc._snapshot_cost(entry) == {"pricingStatus": "unknown", "cost": None}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                 -1.0, True])
def test_models_dev_cost_rejects_non_finite_negative_and_bool_rates(bad):
    assert mc._models_dev_cost(
        {"input": bad, "output": bad, "cache_read": bad}) == {
            "pricingStatus": "unknown", "cost": None}


def test_a_bad_rate_only_nulls_that_field_and_keeps_the_rest_priced():
    resolved = mc._models_dev_cost(
        {"input": float("inf"), "output": 4.0, "cache_read": float("nan")})
    assert resolved["pricingStatus"] == "priced"
    cost = resolved["cost"]
    assert cost["input"] is None and cost["cacheRead"] is None
    assert cost["output"] == 4.0
    assert cost["display"] == {"input": None, "output": "$4.00", "cache": None}
    json.dumps(resolved, allow_nan=False)


@pytest.mark.parametrize("bad_rate", ["nan", "inf", "-inf", "-0.000001", "1e999"])
def test_picker_raw_bad_rates_degrade_to_unknown_and_serialize_as_valid_json(bad_rate):
    payload = make_payload(make_row(
        "nous", "Nous", models=["bad-rate"], pricing={"bad-rate": {}}))
    probe = FakeProbe(picker={"nous": {"bad-rate": {
        "prompt": bad_rate, "completion": bad_rate,
        "input_cache_read": bad_rate}}})
    res = mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    model = _model(res["providers"][0], "bad-rate")
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None
    # NaN/Infinity would make this raise (and are invalid JSON on the wire).
    json.dumps(res, allow_nan=False)


# -- exact-id join across providers ---------------------------------------

def test_same_model_id_under_two_providers_keeps_each_provider_price():
    payload = make_payload(
        make_row("provider-a", "A", models=["shared"], pricing={"shared": {
            "input": "$1.00", "output": "$2.00", "cache": None, "free": False}}),
        make_row("provider-b", "B", models=["shared"], pricing={"shared": {
            "input": "$7.00", "output": "$8.00", "cache": None, "free": False}}),
    )
    probe = FakeProbe(picker={
        "provider-a": {"shared": {"prompt": "0.000001", "completion": "0.000002"}},
        "provider-b": {"shared": {"prompt": "0.000007", "completion": "0.000008"}},
    })
    res = mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    a = _model(res["providers"][0], "shared")
    b = _model(res["providers"][1], "shared")
    assert (a["cost"]["input"], a["cost"]["output"]) == (1.0, 2.0)
    assert (b["cost"]["input"], b["cost"]["output"]) == (7.0, 8.0)
    assert ("picker", "provider-a") in probe.calls
    assert ("picker", "provider-b") in probe.calls


def test_exact_ids_are_not_fuzzy_normalized_across_case():
    payload = make_payload(make_row(
        "provider-a", "A", models=["Vendor/Model-X"], pricing={"Vendor/Model-X": {
            "input": "$3.00", "output": "$3.00", "cache": None, "free": False}}))
    probe = FakeProbe(picker={"provider-a": {"Vendor/Model-X": {
        "prompt": "0.000003", "completion": "0.000003"}}})
    model = _model(mc.get_model_catalog_all(FakeAdapter(payload, probe), None)["providers"][0],
                   "Vendor/Model-X")
    assert model["pricingStatus"] == "priced"
    assert probe.calls == [("picker", "provider-a")]  # no lookup for another case variant


# -- per-request cost memoization -----------------------------------------

def test_cost_lookups_are_memoized_per_provider_and_model_within_a_request():
    """A repeated id must not re-run billing/models.dev seam lookups."""
    payload = make_payload(make_row("nous", "Nous", models=["dup", "dup"]))
    adapter = FakeAdapter(payload)
    res = mc.get_model_catalog_all(adapter, None)
    assert adapter.probe.calls.count(("route", "dup", "nous")) == 1
    assert adapter.probe.calls.count(("models_dev", "nous", "dup")) == 1
    assert [m["pricingStatus"] for m in res["providers"][0]["models"]] == [
        "unknown", "unknown"]


def test_cost_memo_does_not_outlive_the_request():
    payload = make_payload(make_row("nous", "Nous", models=["dup", "dup"]))
    adapter = FakeAdapter(payload)
    mc.get_model_catalog_all(adapter, None)
    mc.get_model_catalog_all(adapter, None)
    # One lookup per request: the cache is per-request, never a module global.
    assert adapter.probe.calls.count(("route", "dup", "nous")) == 2
    assert adapter.probe.calls.count(("models_dev", "nous", "dup")) == 2


# -- failures / isolation --------------------------------------------------

def test_one_provider_failure_does_not_suppress_others():
    payload = make_payload(
        make_row("broken", "Broken", models=["m"], pricing={"m": {
            "input": "$1.00", "output": "$1.00", "cache": None, "free": False}}),
        make_row("healthy", "Healthy", models=["ok"], pricing={"ok": {
            "input": "$2.00", "output": "$2.00", "cache": None, "free": False}}),
    )
    probe = FakeProbe(picker={"healthy": {"ok": {"prompt": "0.000002", "completion": "0.000002"}}},
                      explode={"broken"})
    res = mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    assert res["ok"] is True
    assert [p["id"] for p in res["providers"]] == ["broken", "healthy"]
    broken, healthy = res["providers"]
    assert broken["modelCount"] == 0 and broken["models"] == []
    assert broken["stale"] is True and broken["fetchedAt"] is None
    assert broken["warning"] == "provider catalog unavailable: RuntimeError"
    assert "secret-test-token" not in json.dumps(res)
    assert _model(healthy, "ok")["pricingStatus"] == "priced"
    assert res["cost"]["unavailableProviders"] == ["broken"]


def test_zero_provider_rows_is_a_failure_not_empty_success():
    res = mc.get_model_catalog_all(FakeAdapter(make_payload()), None)
    assert res["ok"] is False
    assert res["providers"] == []
    assert res["error"] == "no providers in this profile's catalog"


def test_unknown_provider_filter_is_truthful_failure():
    payload = make_payload(make_row("nous", "Nous", models=["m"]))
    res = mc.get_model_catalog_all(FakeAdapter(payload), None, "does-not-exist")
    assert res["ok"] is False
    assert res["providers"] == []
    assert "does-not-exist" in res["error"]


def test_provider_filter_matches_alias_and_prefers_authenticated_duplicate():
    payload = make_payload(
        make_row("gemini", "Gemini", models=["a"], provider_id="google"),
        make_row("google", "Google skeleton", models=[], source="canonical",
                 authenticated=False, provider_id="google"),
    )
    res = mc.get_model_catalog_all(FakeAdapter(payload), None, "google")
    assert res["ok"] is True
    assert [p["id"] for p in res["providers"]] == ["gemini"]


def test_catalog_exception_is_sanitized():
    adapter = FakeAdapter({}, raise_on_payload=RuntimeError(
        f"Authorization: Bearer secret-test-token at {LAB_HOST_PATH}"))
    res = mc.get_model_catalog_all(adapter, "work")
    assert res["ok"] is False
    assert "RuntimeError" in res["error"]
    assert "secret-test-token" not in res["error"]
    assert "Bearer" not in res["error"]
    assert "/home/" not in res["error"]
    assert LAB_HOST_PATH not in res["error"]


def test_unavailable_pricing_seam_marks_providers_and_keeps_prices_unknown():
    payload = make_payload(make_row("nous", "Nous", models=["hermes-4-405b"]))
    probe = FakeProbe(available=False)
    res = mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    assert res["ok"] is True
    model = _model(res["providers"][0], "hermes-4-405b")
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None
    assert res["cost"]["unavailableProviders"] == ["nous"]


# -- scoping / refresh / read-only ----------------------------------------

def test_exactly_one_scoped_call_and_refresh_forwarded():
    adapter = FakeAdapter(make_payload(make_row("nous", "Nous", models=["m"])))
    mc.get_model_catalog_all(adapter, "work", refresh=True)
    assert adapter.scopes == ["work"]
    assert adapter.refresh_calls == [True]


def test_profile_id_is_echoed_and_none_stays_null():
    adapter = FakeAdapter(make_payload(make_row("nous", "Nous", models=["m"])))
    assert mc.get_model_catalog_all(adapter, "work")["profileId"] == "work"
    assert mc.get_model_catalog_all(adapter, None)["profileId"] is None


def test_pricing_probe_is_offline_and_provider_scoped_only():
    payload = make_payload(make_row(
        "nous", "Nous", models=["m"],
        pricing={"m": {"input": "$1.00", "output": "$1.00", "cache": None, "free": False}}))
    probe = FakeProbe(picker={"nous": {"m": {"prompt": "0.000001", "completion": "0.000001"}}})
    mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    assert probe.calls == [("picker", "nous")]
    # No URL/api-key material in any lookup argument.
    assert not any("http" in str(part) or "key" in str(part).lower()
                   for call in probe.calls for part in call)


def test_dto_allowlist_never_leaks_paths_urls_secrets_or_raw_rows():
    payload = make_payload(make_row(
        "nous", "Nous", models=["m"],
        pricing={"m": {"input": "$1.00", "output": "$1.00", "cache": None, "free": False}},
        api_url="http://127.0.0.1:8080/v1", key_env="NOUS_API_KEY",
        headers={"Authorization": "Bearer sk-live-secret"},
        base_url="http://internal.example/v1", raw_config={"model": {"provider": "nous"}},
        snapshot={"journal": "row"}))
    probe = FakeProbe(picker={"nous": {"m": {"prompt": "0.000001", "completion": "0.000001"}}})
    res = mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    assert set(res) == TOP_KEYS
    assert set(res["cost"]) == {"pending", "unavailableProviders"}
    provider = res["providers"][0]
    assert set(provider) == PROVIDER_KEYS
    assert set(provider["models"][0]) == MODEL_KEYS
    assert set(provider["models"][0]["cost"]) == COST_KEYS
    assert set(provider["models"][0]["cost"]["display"]) == {"input", "output", "cache"}
    serialized = json.dumps(res)
    for needle in ("http://", "https://", "sk-live-secret", "Bearer",
                   "/home/", "api_url", "base_url", "key_env", "headers",
                   "raw_config", "snapshot"):
        assert needle not in serialized


# -- freshness / pending ---------------------------------------------------

def test_freshness_is_per_provider_and_pending_is_surfaced(hermes_home):
    from hermes_constants import get_hermes_home

    now = time.time()
    (get_hermes_home() / "provider_models_cache.json").write_text(json.dumps({
        "nous": {"fp": "fp", "at": now, "models": ["m"]},
        "old": {"fp": "fp", "at": now - 7200, "models": ["m"]},
    }))
    payload = make_payload(
        make_row("nous", "Nous", models=["m"], pricing_pending=True),
        make_row("old", "Old", models=["m"]),
    )
    res = mc.get_model_catalog_all(FakeAdapter(payload), None)
    fresh, stale = res["providers"]
    assert fresh["stale"] is False and fresh["fetchedAt"]
    assert stale["stale"] is True and stale["fetchedAt"]
    assert res["cost"]["pending"] is True


def test_provider_filter_scopes_pending_to_the_filtered_provider():
    """One unrelated provider's pending flag must not taint a clean request."""
    payload = make_payload(
        make_row("pending-one", "Pending", models=["m"], pricing_pending=True),
        make_row("clean", "Clean", models=["m"]),
    )
    filtered = mc.get_model_catalog_all(FakeAdapter(payload), None, "clean")
    assert filtered["ok"] is True
    assert [p["id"] for p in filtered["providers"]] == ["clean"]
    assert filtered["cost"]["pending"] is False

    # A payload-level (global) pending flag is likewise not attributable to the
    # single provider this response describes.
    payload_with_global = dict(payload, pricing_pending=True)
    filtered_global = mc.get_model_catalog_all(
        FakeAdapter(payload_with_global), None, "clean")
    assert filtered_global["cost"]["pending"] is False

    # Unfiltered, the same payload still reports the pending work.
    unfiltered = mc.get_model_catalog_all(FakeAdapter(payload), None)
    assert unfiltered["cost"]["pending"] is True


def test_provider_filter_scopes_unavailable_providers_to_the_response():
    payload = make_payload(
        make_row("nous", "Nous", models=["m"]),
        make_row("openrouter", "OpenRouter", models=["m"]),
    )
    probe = FakeProbe(available=False)
    filtered = mc.get_model_catalog_all(FakeAdapter(payload, probe), None, "openrouter")
    assert filtered["cost"]["unavailableProviders"] == ["openrouter"]


def test_no_cache_entry_reports_stale_and_null_fetched_at(hermes_home):
    payload = make_payload(make_row("nous", "Nous", models=["m"]))
    provider = mc.get_model_catalog_all(FakeAdapter(payload), None)["providers"][0]
    assert provider["stale"] is True
    assert provider["fetchedAt"] is None


# -- adapter seam: import probe + graceful degradation ---------------------

def test_adapter_pricing_probe_uses_offline_calls_only(monkeypatch):
    """The probe must pass cached_only / allow_network=False and never base_url."""
    from types import SimpleNamespace
    import hermes_adapter

    seen = {}

    def fake_picker(provider, **kwargs):
        seen["picker"] = (provider, kwargs)
        return {}

    def fake_route(model, provider=None, **kwargs):
        seen["route"] = (model, provider, kwargs)
        return SimpleNamespace(billing_mode="unknown")

    def fake_entry(model, provider=None, **kwargs):
        seen["entry"] = (model, provider, kwargs)
        return None

    def fake_info(provider, model, **kwargs):
        seen["info"] = (provider, model, kwargs)
        return None

    monkeypatch.setattr(hermes_adapter, "_load_pricing_seams", lambda: {
        "get_pricing_for_provider": fake_picker,
        "resolve_billing_route": fake_route,
        "get_pricing_entry": fake_entry,
        "get_model_info": fake_info,
    })
    probe = hermes_adapter.HermesAdapter().pricing_probe()
    assert probe.available is True
    probe.picker_pricing("nous")
    probe.billing_mode("m", "nous")
    probe.pricing_entry("m", "nous")
    probe.models_dev_cost("nous", "m")
    assert seen["picker"] == ("nous", {"cached_only": True})
    assert seen["route"] == ("m", "nous", {})   # no base_url/api_key
    assert seen["entry"] == ("m", "nous", {})   # no base_url/api_key
    assert seen["info"] == ("nous", "m", {"allow_network": False})


def test_adapter_pricing_probe_degrades_on_import_error(monkeypatch):
    import hermes_adapter

    def boom():
        raise ImportError("No module named 'hermes_cli.models_pricing'")

    monkeypatch.setattr(hermes_adapter, "_load_pricing_seams", boom)
    probe = hermes_adapter.HermesAdapter().pricing_probe()
    assert probe.available is False
    assert probe.unavailable == ("pricing",)
    assert probe.reason == "ImportError"
    assert probe.picker_pricing("nous") == {}
    assert probe.billing_mode("m", "nous") is None
    assert probe.pricing_entry("m", "nous") is None
    assert probe.models_dev_cost("nous", "m") is None

    payload = make_payload(make_row("nous", "Nous", models=["hermes-4-405b"]))
    res = mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    assert res["ok"] is True
    model = _model(res["providers"][0], "hermes-4-405b")
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None
    assert res["cost"]["unavailableProviders"] == ["nous"]


def test_adapter_catalog_payload_raises_sanitized_seam_error(monkeypatch):
    import hermes_adapter

    def boom():
        raise ImportError("No module named 'hermes_cli.inventory'")

    monkeypatch.setattr(hermes_adapter, "_load_inventory", boom)
    adapter = hermes_adapter.HermesAdapter()
    with pytest.raises(hermes_adapter.CatalogSeamUnavailable):
        adapter.catalog_payload(False)
    res = mc.get_model_catalog_all(adapter, None)
    assert res["ok"] is False
    assert res["error"] == "catalog lookup failed: CatalogSeamUnavailable"


def test_adapter_models_dev_probe_requires_raw_cost_dict_presence(monkeypatch):
    """ModelInfo zero-fills costs; 0.0 alone must not prove a price exists."""
    from types import SimpleNamespace
    import hermes_adapter

    info = SimpleNamespace(cost_input=0.0, cost_output=0.0, cost_cache_read=None,
                           has_cost_data=lambda: False)
    monkeypatch.setattr(hermes_adapter, "_load_pricing_seams", lambda: {
        "get_pricing_for_provider": lambda provider, **kw: {},
        "resolve_billing_route": lambda model, provider=None, **kw: None,
        "get_pricing_entry": lambda model, provider=None, **kw: None,
        "get_model_info": lambda provider, model, **kw: info,
    })
    probe = hermes_adapter.HermesAdapter().pricing_probe()
    monkeypatch.setattr(hermes_adapter, "_raw_models_dev_cost", lambda p, m: None)
    assert probe.models_dev_cost("nous", "m") is None
    monkeypatch.setattr(hermes_adapter, "_raw_models_dev_cost",
                        lambda p, m: {"input": 0, "output": 0})
    assert probe.models_dev_cost("nous", "m") == {
        "input": 0.0, "output": 0.0, "cache_read": None}


def test_models_dev_private_helper_skew_degrades_to_unknown(monkeypatch):
    """Missing models.dev registry helpers must yield unknown, never a 500."""
    from types import SimpleNamespace
    import agent.models_dev as models_dev
    import hermes_adapter

    for name in ("_models_dev_id", "_registry_models", "_iter_model_entries"):
        monkeypatch.delattr(models_dev, name, raising=False)
    info = SimpleNamespace(cost_input=0.0, cost_output=0.0, cost_cache_read=None,
                           has_cost_data=lambda: False)
    monkeypatch.setattr(hermes_adapter, "_load_pricing_seams", lambda: {
        "get_pricing_for_provider": lambda provider, **kw: {},
        "resolve_billing_route": lambda model, provider=None, **kw: None,
        "get_pricing_entry": lambda model, provider=None, **kw: None,
        "get_model_info": lambda provider, model, **kw: info,
    })
    assert hermes_adapter._raw_models_dev_cost("nous", "m") is None
    probe = hermes_adapter.HermesAdapter().pricing_probe()
    assert probe.models_dev_cost("nous", "m") is None

    payload = make_payload(make_row("nous", "Nous", models=["m"]))
    res = mc.get_model_catalog_all(FakeAdapter(payload, probe), None)
    assert res["ok"] is True
    model = _model(res["providers"][0], "m")
    assert model["pricingStatus"] == "unknown"
    assert model["cost"] is None
    json.dumps(res, allow_nan=False)


def test_models_dev_helper_raising_at_call_time_degrades_to_unknown(monkeypatch):
    import agent.models_dev as models_dev
    import hermes_adapter

    def boom(*args, **kwargs):
        raise AttributeError("_iter_model_entries moved in this Hermes version")

    monkeypatch.setattr(models_dev, "_models_dev_id",
                        lambda provider: provider, raising=False)
    monkeypatch.setattr(models_dev, "_registry_models",
                        lambda *args, **kwargs: {"m": {}}, raising=False)
    monkeypatch.setattr(models_dev, "_iter_model_entries", boom, raising=False)
    assert hermes_adapter._raw_models_dev_cost("nous", "m") is None

