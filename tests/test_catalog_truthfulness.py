"""Catalog refresh requests aren't evidence of successful live discovery."""
from contextlib import nullcontext
from datetime import datetime, timezone
import pytest

from dashboard import model_catalog


@pytest.fixture(autouse=True)
def isolated_catalog_home(hermes_home):
    return hermes_home


class Scoped:
    def scoped(self, profile_id):
        return nullcontext()


def test_failure_message_does_not_publish_exception_contents(monkeypatch):
    def fail(refresh):
        raise RuntimeError("Authorization: Bearer secret-test-token")
    monkeypatch.setattr(model_catalog, "_load_picker_payload", fail)
    result = model_catalog.get_model_catalog(Scoped(), "default", "nous", True)
    assert result["ok"] is False
    assert "secret-test-token" not in result["error"]
    assert result["stale"] is True


def test_refresh_without_discovery_evidence_is_not_fresh(monkeypatch):
    from hermes_cli import models
    monkeypatch.setattr(models, "_load_provider_models_cache", lambda: {})
    fetched, stale = model_catalog._freshness("nous", True, datetime.now(timezone.utc))
    assert stale is True


def test_warning_survives_a_nonempty_curated_catalog(monkeypatch):
    monkeypatch.setattr(model_catalog, "_load_picker_payload", lambda refresh: {
        "providers": [{"slug": "nous", "models": ["deepseek/deepseek-v4.1-flash"],
                       "warning": "Live lookup failed; curated fallback", "source": "hermes"}]})
    result = model_catalog.get_model_catalog(Scoped(), "default", "nous")
    assert result["error"] == "Live lookup failed; curated fallback"
