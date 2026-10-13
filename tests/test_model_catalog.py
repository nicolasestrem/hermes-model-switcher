"""Provider-aware model catalog — RED/GREEN contract for ``dashboard.model_catalog``.

The module is a thin, truthful adapter over the INSTALLED Hermes picker
substrate (``hermes_cli.inventory.build_model_options_payload`` /
``load_picker_context``) executed inside ``adapter.scoped(profile_id)``. These
tests never touch the network or read secrets: the installed builder is
monkeypatched with canned payloads, and every home is a throwaway dir minted by
the ``hermes_home`` fixture (outside ``~/.hermes``).

Only ``dashboard/model_catalog.py`` and this file are owned by this task.
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import dashboard.model_catalog as mc
from dashboard.model_catalog import get_model_catalog

RESULT_KEYS = {"ok", "provider", "models", "fetchedAt", "source", "stale", "error"}


# -- helpers ---------------------------------------------------------------


def make_row(slug, name=None, models=(), source="built-in", **extra):
    row = {
        "slug": slug,
        "name": name if name is not None else slug.replace("-", " ").title(),
        "models": list(models),
        "total_models": len(models),
        "source": source,
        "is_current": False,
        "is_user_defined": False,
    }
    row.update(extra)
    return row


def make_payload(*rows):
    return {"providers": list(rows), "model": "", "provider": ""}


class FakeAdapter:
    """Records ``scoped`` invocations; yields a minimal identity."""

    def __init__(self):
        self.calls = []

    @contextlib.contextmanager
    def scoped(self, profile):
        self.calls.append(profile)
        yield type("Ident", (), {"name": profile or "default"})()


@pytest.fixture()
def use_payload(monkeypatch):
    """Replace the installed-builder seam with a canned payload; return setter."""
    box = {"payload": make_payload(), "calls": []}

    def _seam(refresh):
        box["calls"].append(refresh)
        return box["payload"]

    monkeypatch.setattr(mc, "_load_picker_payload", _seam)

    def setter(payload):
        box["payload"] = payload
        return payload

    return setter, box


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# -- shape / happy path ----------------------------------------------------


def test_result_has_required_keys_and_types(hermes_home, use_payload):
    setter, _ = use_payload
    setter(make_payload(make_row(
        "nous", "Nous", models=["deepseek/deepseek-v4.1-flash", "nous/hermes-4-405b"])))
    res = get_model_catalog(FakeAdapter(), None, "nous")
    assert set(res) >= RESULT_KEYS
    assert res["ok"] is True
    assert res["provider"] == "nous"
    assert res["models"] == [
        {"id": "deepseek/deepseek-v4.1-flash", "name": "deepseek/deepseek-v4.1-flash"},
        {"id": "nous/hermes-4-405b", "name": "nous/hermes-4-405b"},
    ]
    assert isinstance(res["stale"], bool)
    assert isinstance(res["source"], str) and res["source"]
    assert res["error"] is None
    # No fetch evidence exists in this throwaway home: the catalog must say so
    # (None), never fabricate a "now" that reads like a fresh fetch.
    assert res["fetchedAt"] is None


def test_exact_native_ids_preserved_no_prefix_rewrite(use_payload):
    setter, _ = use_payload
    setter(make_payload(
        make_row("nous", models=["deepseek/deepseek-v4.1-flash"]),
        make_row("opencode-go", models=["deepseek-v4.1-flash"]),
    ))
    a = get_model_catalog(FakeAdapter(), None, "nous")
    b = get_model_catalog(FakeAdapter(), None, "opencode-go")
    assert a["models"] == [{"id": "deepseek/deepseek-v4.1-flash", "name": "deepseek/deepseek-v4.1-flash"}]
    assert b["models"] == [{"id": "deepseek-v4.1-flash", "name": "deepseek-v4.1-flash"}]


def test_provider_match_is_case_insensitive(use_payload):
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    res = get_model_catalog(FakeAdapter(), None, "  NOUS  ")
    assert res["ok"] is True
    assert res["provider"] == "nous"


def test_provider_matched_by_provider_id_alias(use_payload):
    setter, _ = use_payload
    setter(make_payload(make_row("gemini", models=["gemini-2.5-pro"], provider_id="google")))
    res = get_model_catalog(FakeAdapter(), None, "google")
    assert res["ok"] is True
    assert res["provider"] == "gemini"
    assert res["models"][0]["id"] == "gemini-2.5-pro"


# -- providers list --------------------------------------------------------


def test_optional_providers_list_shape(use_payload):
    setter, _ = use_payload
    setter(make_payload(
        make_row("nous", "Nous", models=["m1"]),
        make_row("opencode-go", "OpenCode Go", models=["m2"]),
    ))
    res = get_model_catalog(FakeAdapter(), None, "nous")
    assert res["providers"] == [
        {"id": "nous", "name": "Nous"},
        {"id": "opencode-go", "name": "OpenCode Go"},
    ]


# -- truthful empties / failures ------------------------------------------


def test_unknown_provider_is_truthful_failure(use_payload):
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    res = get_model_catalog(FakeAdapter(), None, "does-not-exist")
    assert res["ok"] is False
    assert res["models"] == []
    assert res["provider"] == "does-not-exist"
    assert res["error"] and "does-not-exist" in res["error"]


def test_empty_catalog_surfaces_row_warning(use_payload):
    setter, _ = use_payload
    warning = "paste OPENCODE_GO_API_KEY to activate"
    setter(make_payload(make_row(
        "opencode-go", "OpenCode Go", models=[], source="canonical",
        authenticated=False, warning=warning)))
    res = get_model_catalog(FakeAdapter(), None, "opencode-go")
    assert res["ok"] is True
    assert res["models"] == []
    assert res["error"] == warning
    assert res["source"] == "canonical"


def test_empty_without_warning_key_is_safe(use_payload):
    """A row with no ``warning``/``authenticated`` keys must not raise."""
    setter, _ = use_payload
    setter({"providers": [{"slug": "opencode-go", "name": "OpenCode Go", "models": []}],
            "model": "", "provider": ""})
    res = get_model_catalog(FakeAdapter(), None, "opencode-go")
    assert res["ok"] is True
    assert res["models"] == []
    assert res["error"] and "opencode-go" in res["error"]


def test_empty_provider_argument_is_truthful(use_payload):
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    res = get_model_catalog(FakeAdapter(), None, "")
    assert res["ok"] is False
    assert res["models"] == []
    assert res["error"]


def test_builder_exception_is_truthful_failure(monkeypatch):
    def boom(refresh):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(mc, "_load_picker_payload", boom)
    res = get_model_catalog(FakeAdapter(), "work", "nous")
    assert res["ok"] is False
    assert res["models"] == []
    assert "RuntimeError" in (res["error"] or "")


# -- scoping ---------------------------------------------------------------


def test_scoped_with_requested_profile(use_payload):
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    fake = FakeAdapter()
    get_model_catalog(fake, "work", "nous")
    assert fake.calls == ["work"]


def test_scoping_uses_real_adapter_and_isolated_home(hermes_home, monkeypatch, use_payload):
    """Real HermesAdapter.scoped enters the profile scope without touching live state."""
    from hermes_adapter import HermesAdapter
    hermes_home.make_profile("work")
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["deepseek/deepseek-v4.1-flash"])))
    res = get_model_catalog(HermesAdapter(), "work", "nous")
    assert res["ok"] is True
    assert res["models"][0]["id"] == "deepseek/deepseek-v4.1-flash"


# -- refresh / freshness ---------------------------------------------------


def test_refresh_forwarded_to_builder(use_payload):
    setter, box = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    get_model_catalog(FakeAdapter(), None, "nous", refresh=True)
    assert box["calls"] == [True]


def test_refresh_without_cache_evidence_is_unproven(hermes_home, use_payload):
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    res = get_model_catalog(FakeAdapter(), None, "nous", refresh=True)
    assert res["stale"] is True
    assert res["fetchedAt"] is None


def test_no_cache_entry_uncached_read_is_stale(hermes_home, use_payload):
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    res = get_model_catalog(FakeAdapter(), None, "nous", refresh=False)
    assert res["stale"] is True


def test_fresh_cache_entry_is_not_stale(hermes_home, use_payload):
    from hermes_constants import get_hermes_home
    now = time.time()
    (get_hermes_home() / "provider_models_cache.json").write_text(json.dumps(
        {"nous": {"fp": "fp", "at": now, "models": ["m1"]}}))
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    res = get_model_catalog(FakeAdapter(), None, "nous", refresh=False)
    assert res["stale"] is False
    assert abs(_parse(res["fetchedAt"]).timestamp() - now) < 5


def test_old_cache_entry_is_stale_and_reports_its_timestamp(hermes_home, use_payload):
    from hermes_constants import get_hermes_home
    old = time.time() - 7200  # 2h > 1h fresh TTL
    (get_hermes_home() / "provider_models_cache.json").write_text(json.dumps(
        {"nous": {"fp": "fp", "at": old, "models": ["m1"]}}))
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    res = get_model_catalog(FakeAdapter(), None, "nous", refresh=False)
    assert res["stale"] is True
    assert abs(_parse(res["fetchedAt"]).timestamp() - old) < 5


def test_refresh_fallback_preserves_old_cache_timestamp(hermes_home, use_payload):
    from hermes_constants import get_hermes_home
    old = time.time() - 7200
    (get_hermes_home() / "provider_models_cache.json").write_text(json.dumps(
        {"nous": {"fp": "fp", "at": old, "models": ["m1"]}}))
    setter, _ = use_payload
    setter(make_payload(make_row("nous", models=["m1"])))
    res = get_model_catalog(FakeAdapter(), None, "nous", refresh=True)
    assert res["stale"] is True
    assert abs(_parse(res["fetchedAt"]).timestamp() - old) < 5


def test_runtime_cache_seam_matches_supported_baseline():
    """Pin the private Hermes seam this module reads.

    ``_load_provider_models_cache`` / ``_PROVIDER_MODELS_CACHE_TTL`` are
    underscore-private today; a rename in a Hermes upgrade must fail loudly
    HERE instead of silently degrading freshness reporting to "unproven".
    """
    from hermes_cli.models import (  # noqa: PLC0415
        _load_provider_models_cache,
        _PROVIDER_MODELS_CACHE_TTL,
    )

    assert callable(_load_provider_models_cache)
    assert isinstance(_PROVIDER_MODELS_CACHE_TTL, (int, float))
    assert _PROVIDER_MODELS_CACHE_TTL > 0
    assert isinstance(_load_provider_models_cache() or {}, dict)
