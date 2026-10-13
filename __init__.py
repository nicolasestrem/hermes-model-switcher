"""Hermes Model Switcher — plugin entry point.

This plugin's whole code surface is the Dashboard extension:
``dashboard/manifest.json`` declares the tab and its pre-built bundle, and
``dashboard/plugin_api.py`` supplies the FastAPI routes the Dashboard mounts at
``/api/plugins/hermes-model-switcher/``.

Hermes nevertheless requires an importable entry point beside ``plugin.yaml``:
a manifest with no ``__init__.py``, ``desktop/plugin.js`` or ``plugin.json``
next to it is rejected by ``hermes plugins validate``, and the agent loader
records a load failure ("No __init__.py in <dir>") for it at every startup.
This module is that entry point, and it is deliberately a no-op — the plugin
registers no agent tools, hooks or middleware, so nothing here runs on the
agent loop.
"""
from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Register no agent surface; the Dashboard loads this plugin's own assets."""
    return None
