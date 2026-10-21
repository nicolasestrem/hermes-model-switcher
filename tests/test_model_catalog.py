"""``models_response`` — the frozen ``/models`` DTO, built from a host payload.

The input is the host's ``GET /api/model/options`` JSON body (the same body the
Dashboard routes now hold).  No adapter, no Hermes import, no provider I/O.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import dashboard.model_catalog as mc

REPO = Path(__file__).resolve().parent.parent
MODEL_CATALOG = REPO / "dashboard" / "model_catalog.py"


# -- host-payload fixtures (shaped exactly as build_model_options_payload emits) --


def row(slug: str, models=(), **extra) -> dict:
    """One provider row as the host picker emits it."""
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
        row("nous", ["hermes-4-405b", "deepseek/deepseek-v4.1-flash"], is_current=True),
        row("openrouter", ["anthropic/claude-sonnet-5", "openai/gpt-6"]),
    )


# -- keys and identity -------------------------------------------------------


def test_models_dto_has_exactly_the_frozen_keys(nous_payload):
    res = mc.models_response(nous_payload, "nous")
    assert set(res) == set(mc.RESULT_KEYS)
    assert set(res) == {"ok", "provider", "models", "fetchedAt", "source",
                        "stale", "error", "providers"}


def test_exact_native_ids_and_order_survive(nous_payload):
    res = mc.models_response(nous_payload, "nous")
    assert res["ok"] is True
    assert [m["id"] for m in res["models"]] == [
        "hermes-4-405b", "deepseek/deepseek-v4.1-flash",
    ]
    assert [m["name"] for m in res["models"]] == [m["id"] for m in res["models"]]


def test_ids_are_trimmed_but_never_rewritten():
    body = payload(row("nous", ["  hermes-4-405b  ", "z-ai/glm-5.3:free"]))
    res = mc.models_response(body, "nous")
    assert [m["id"] for m in res["models"]] == ["hermes-4-405b", "z-ai/glm-5.3:free"]


def test_dict_shaped_models_are_normalized_to_their_id():
    body = payload(row("nous", [{"id": "m1", "name": "ignored"}, "m2"]))
    assert [m["id"] for m in mc.models_response(body, "nous")["models"]] == ["m1", "m2"]


def test_provider_filter_is_case_and_whitespace_insensitive(nous_payload):
    res = mc.models_response(nous_payload, "  NOUS  ")
    assert res["ok"] is True and res["provider"] == "nous"


def test_provider_id_alias_resolves_and_reports_the_native_slug():
    body = payload(row("custom:lab", ["lab-7b"], provider_id="lab-endpoint"))
    res = mc.models_response(body, "LAB-ENDPOINT")
    assert res["ok"] is True
    assert res["provider"] == "custom:lab"
    assert [m["id"] for m in res["models"]] == ["lab-7b"]


def test_source_is_the_host_rows_provenance(nous_payload):
    assert mc.models_response(nous_payload, "nous")["source"] == "hermes"
    body = payload(row("openrouter", ["m"], source="canonical"))
    assert mc.models_response(body, "openrouter")["source"] == "canonical"
    assert mc.models_response(payload(row("x", ["m"], source="")), "x")["source"] == "unknown"


def test_providers_index_lists_every_row_in_native_order():
    body = payload(row("nous", ["m1"]), row("openrouter", ["m2"]), row("fireworks", ["m3"]))
    providers = mc.models_response(body, "nous")["providers"]
    assert providers == [
        {"id": "nous", "name": "NOUS"},
        {"id": "openrouter", "name": "OPENROUTER"},
        {"id": "fireworks", "name": "FIREWORKS"},
    ]


def test_providers_index_keeps_duplicate_slugs_visible_but_the_models_join_is_the_authenticated_row():
    body = payload(
        row("nous", [], authenticated=False, warning="paste NOUS_API_KEY to activate"),
        row("nous", ["hermes-4-405b"], authenticated=True),
    )
    res = mc.models_response(body, "nous")
    assert res["ok"] is True
    assert [m["id"] for m in res["models"]] == ["hermes-4-405b"]
    assert [p["id"] for p in res["providers"]] == ["nous", "nous"]


# -- truthful failures -------------------------------------------------------


def test_unknown_provider_is_a_truthful_failure_not_an_empty_success(nous_payload):
    res = mc.models_response(nous_payload, "does-not-exist")
    assert res["ok"] is False
    assert res["models"] == []
    assert res["error"]
    assert "does-not-exist" in res["error"]


def test_provider_missing_from_an_empty_catalog_is_a_failure():
    res = mc.models_response(payload(), "nous")
    assert res["ok"] is False
    assert res["providers"] == []
    assert res["error"]


def test_unauthenticated_skeleton_row_resolves_and_surfaces_its_warning():
    """The row resolved (ok=True) but carries no models and says why — the DTO's
    ``error`` is the visible message, never silence."""
    body = payload(row("anthropic", [], authenticated=False,
                       warning="paste ANTHROPIC_API_KEY to activate"))
    res = mc.models_response(body, "anthropic")
    assert res["ok"] is True
    assert res["models"] == []
    assert res["error"] == "paste ANTHROPIC_API_KEY to activate"


def test_authenticated_but_empty_catalog_reports_no_models():
    res = mc.models_response(payload(row("openai", [])), "openai")
    assert res["ok"] is True
    assert res["models"] == []
    assert res["error"]


def test_no_provider_argument_is_rejected_before_the_payload_is_read():
    for body in (payload(row("nous", ["m"])), None, {"providers": {}}):
        res = mc.models_response(body, "")
        assert res["ok"] is False
        assert res["error"] == "no provider specified"
    assert mc.models_response(payload(row("nous", ["m"])), "   ")["ok"] is False


@pytest.mark.parametrize("bad_payload", [
    None,
    [],
    "not-a-payload",
    42,
    {},
    {"providers": None},
    {"providers": {}},
    {"providers": "nous"},
])
def test_malformed_payloads_report_the_fixed_host_catalog_reason(bad_payload):
    res = mc.models_response(bad_payload, "nous")
    assert res["ok"] is False
    assert res["error"] == mc.HOST_UNAVAILABLE == "hostCatalogUnavailable"
    assert res["models"] == []
    assert res["fetchedAt"] is None and res["stale"] is True


def test_rows_that_are_not_mappings_are_not_providers():
    res = mc.models_response({"providers": ["nous", 7, None]}, "nous")
    assert res["ok"] is False
    assert res["providers"] == []
    assert res["error"]


# -- freshness is never claimed ---------------------------------------------


def test_freshness_is_unproven_because_the_host_payload_has_no_timestamp(nous_payload):
    for refresh in (False, True):
        res = mc.models_response(nous_payload, "nous", refresh=refresh)
        assert res["fetchedAt"] is None
        assert res["stale"] is True


def test_profile_id_and_refresh_are_inert_and_not_added_to_the_models_dto(nous_payload):
    plain = mc.models_response(nous_payload, "nous")
    scoped = mc.models_response(nous_payload, "nous", profile_id="work", refresh=True)
    assert plain == scoped
    assert "profileId" not in scoped


# -- DTO allowlist -----------------------------------------------------------


def test_no_raw_row_chrome_reaches_the_models_dto():
    body = payload(row(
        "custom:lab", ["lab-7b"], api_url="http://user:tok3n@127.0.0.1:9000/v1?t=abc",
        key_env="LAB_KEY", aliases=["lab"], capabilities={"lab-7b": {"fast": True}},
        usage={"windows": [{"used_percent": 10.0}]}, limit={"scope": "account"},
        featured_models=["lab-7b"], native_catalog_empty=False,
        headers={"Authorization": "Bearer secret-test-token"},
    ))
    dumped = json.dumps(mc.models_response(body, "custom:lab"))
    for forbidden in ("api_url", "http://", "tok3n", "key_env", "LAB_KEY",
                      "capabilities", "usage", "limit", "featured", "Authorization",
                      "secret-test-token", str(REPO)):
        assert forbidden not in dumped


# -- purity ------------------------------------------------------------------

PURE_SCRIPT = r"""
import importlib.util
import json
import sys

BLOCKED = {"hermes_cli", "hermes_constants", "agent", "gateway", "tui_gateway"}


class BlockHermes:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked hermes import: " + name)
        return None

    def find_module(self, name, path=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked hermes import: " + name)
        return None


sys.meta_path.insert(0, BlockHermes())
spec = importlib.util.spec_from_file_location("pure_model_catalog", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
out = module.models_response(
    {"providers": [{"slug": "nous", "name": "Nous", "models": ["m1"],
                    "authenticated": True}]}, "nous")
print(json.dumps({"ok": out["ok"], "models": out["models"]}))
"""


def test_module_needs_no_hermes_import_to_load_or_run():
    """The transform must stand alone: a Hermes import here would silently
    reintroduce picker/pricing/disk-cache coupling (the seam this replaces)."""
    proc = subprocess.run(
        [sys.executable, "-c", PURE_SCRIPT, str(MODEL_CATALOG)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"ok": True, "models": [{"id": "m1", "name": "m1"}]}
