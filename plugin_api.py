"""hermes-model-switcher plugin public API (Phase A surface).

The Dashboard backend router (api/router.py, Phase B+) imports through this
module so version-sensitive Hermes imports stay in one place. Phase A
exposes the coverage registry only — no runtime writes exist yet.

All Hermes-facing imports (profile discovery, catalogs, canonical assignment)
MUST be added here, never in route handlers, and must resolve homes through
Hermes APIs rather than hardcoded paths.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:  # installed / on-path layout
    from model_switcher.registry import build_registry, REGISTRY_VERSION
except ImportError:  # repo checkout: src/ layout
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from model_switcher.registry import build_registry, REGISTRY_VERSION

__all__ = ["get_registry", "registry_version", "hermes_repo_path", "PLUGIN_NAME"]

PLUGIN_NAME = "hermes-model-switcher"

# Read-only inspection default; overridable for isolated lab homes. The
# default is derived from the running user's home, never a tracked literal.
_HERMES_REPO_ENV = "HERMES_MODEL_SWITCHER_HERMES_REPO"
_DEFAULT_HERMES_REPO = Path.home() / ".hermes" / "hermes-agent"


def hermes_repo_path() -> Path:
    """Location of the Hermes checkout scanned for the registry (read-only)."""
    return Path(os.environ.get(_HERMES_REPO_ENV, _DEFAULT_HERMES_REPO))


def get_registry() -> dict:
    """Build the versioned source-to-slot coverage registry."""
    return build_registry()


def registry_version() -> int:
    return REGISTRY_VERSION
