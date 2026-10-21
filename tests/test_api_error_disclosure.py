"""Regression: apply/undo 409 bodies must never echo raw exception text.

The preview path was fixed earlier (``RemovalRefused.public_reason``); the
apply/undo 409 handlers kept doing ``f"apply blocked: {exc}"`` where
``ApplyBlocked`` messages carried ``str(exc)`` from the store layer (absolute
paths, YAML parse text) or ``repr(target)`` (paths plus config values). That is
the same py/stack-trace-exposure class as the preview finding.

Contract under test: every ``ApplyBlocked`` carries a typed, constant
``public_reason``; the handler emits that constant only. The underlying
message stays available to direct callers (logs, engine tests) but never
reaches the client.

Written RED against the previous revision: each body below contained the
sentinel values before the fix.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

import dashboard.plugin_api as plugin_api  # noqa: E402
from model_switcher.engine import ApplyBlocked  # noqa: E402


SECRET = "sk-live-424242"
SECRET_PATH_SEGMENT = "leaked-secret-profile"


def _client():
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="")
    return TestClient(app)


def _plant_unreadable_store(home) -> str:
    """A real store file whose YAML cannot be parsed, under a secret-named dir."""
    secret_dir = home / SECRET_PATH_SEGMENT
    secret_dir.mkdir(parents=True, exist_ok=True)
    path = secret_dir / "config.yaml"
    # Unterminated flow sequence: yaml raises with the offending text inline.
    path.write_text("model: [unterminated\napi_key: %s\n" % SECRET)
    return str(path)


def test_typed_reasons_are_constants_not_messages():
    """Each ApplyBlocked subtype exposes a fixed public_reason."""
    from model_switcher.engine import (
        ApplyStoreUnreadable,
        ApplyTargetUnbound,
        ApplyUnknownOperation,
    )

    assert ApplyBlocked.public_reason
    for cls in (ApplyTargetUnbound, ApplyStoreUnreadable, ApplyUnknownOperation):
        assert issubclass(cls, ApplyBlocked)
        assert isinstance(cls.public_reason, str) and cls.public_reason
        # The public reason must be a constant, independent of the detail.
        detail = "boom %s /srv/private" % SECRET
        assert cls(detail).public_reason == cls.public_reason
        assert detail in str(cls(detail))  # diagnostics survive server-side


def test_apply_409_does_not_echo_store_error_text(hermes_home, monkeypatch):
    """An unreadable store blocks the apply without leaking its path or text."""
    path = _plant_unreadable_store(hermes_home.path)
    monkeypatch.setattr(plugin_api, "_resolve_apply", lambda body, profiles, host: [{
        "profile": "default",
        "store_kind": "yaml_config",
        "path": path,
        "changes": {"model.default": "hermes-4"},
    }])

    r = _client().post("/operations", json={"changes": []})

    assert r.status_code == 409, r.text
    assert SECRET not in r.text
    assert SECRET_PATH_SEGMENT not in r.text
    assert "/tmp/" not in r.text
    assert r.json()["detail"]["error"] == (
        "apply blocked: a target store could not be read")


def test_apply_409_does_not_echo_target_repr(hermes_home, monkeypatch):
    """A target missing its store binding must not be echoed as repr(dict)."""
    monkeypatch.setattr(plugin_api, "_resolve_apply", lambda body, profiles, host: [{
        "profile": "default",
        "store_kind": "yaml_config",
        # no "path": preflight blocks, and the old message was repr(target)
        "changes": {"providers.lablocal.api_key": SECRET},
    }])

    r = _client().post("/operations", json={"changes": []})

    assert r.status_code == 409, r.text
    assert SECRET not in r.text
    assert "providers.lablocal.api_key" not in r.text
    assert r.json()["detail"]["error"] == (
        "apply blocked: write target is missing its store binding")


def test_undo_409_does_not_echo_operation_id(hermes_home):
    """An unknown op id is refused generically, never quoted back."""
    r = _client().post("/operations/%s/undo" % SECRET)

    assert r.status_code == 409, r.text
    assert SECRET not in r.text
    assert r.json()["detail"]["error"] == "undo blocked: unknown operation"
