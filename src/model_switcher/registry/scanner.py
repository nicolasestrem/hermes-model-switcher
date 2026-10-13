"""Source scanning: find every candidate model-selection field.

Scanners read (never write):
  - Hermes core ``hermes_cli/config_defaults.py`` (DEFAULT_CONFIG dict)
  - Installed plugin manifests (``plugin.yaml`` / ``plugin.json`` ``config_schema``)

Scanning finds *candidates*; only the descriptor registry authorizes writes.
A candidate without a descriptor or a registered exclusion is a visible
coverage gap and invalidates any "all fields" completeness claim.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Field names that denote a model selection when they hold a string.
MODEL_FIELD_NAMES = {"model", "model_id", "model_provider"}
# MoA shapes that hold route assignments.
MOA_LIST_FIELDS = {"reference_models"}
MOA_DICT_FIELDS = {"aggregator"}

# Path -> reason. These "model"-ish keys are NOT user-selectable model slots.
EXCLUSIONS: Dict[str, str] = {
    "model_thresholds": "metadata: per-model compaction threshold overrides",
    "model_catalog": "metadata: remote catalog manifest settings",
    "model_overrides": "metadata: per-model capability overrides",
    "models_dev": "metadata: models.dev registry URL",
    "models_max": "metadata: router residency cap, not a selection",
    "model_dir": "non-model: local directory path for wake-word assets",
    "wake_word.openwakeword.model": "non-model: wake keyword, never an LLM route",
}

# Ordered fallback chain shapes: list of {provider, model, ...} entries.
CHAIN_LIST_FIELDS = {"fallback_providers"}

_HERMES_SUBPATH = Path("hermes_cli") / "config_defaults.py"


def load_default_config(hermes_repo: Path) -> Dict[str, Any]:
    """Import DEFAULT_CONFIG from a Hermes checkout (read-only)."""
    path = Path(hermes_repo) / _HERMES_SUBPATH
    if not path.is_file():
        raise FileNotFoundError(f"config_defaults.py not found at {path}")
    spec = importlib.util.spec_from_file_location("hermes_config_defaults_scan", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DEFAULT_CONFIG


def _walk_core(node: Any, path: str, out: List[dict]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key in MODEL_FIELD_NAMES and isinstance(value, str):
                out.append({"path": child, "kind": "field", "default": value})
            elif key in MOA_LIST_FIELDS and isinstance(value, list):
                out.append({"path": child + "[].provider/model", "kind": "list", "default": "route"})
            elif key in MOA_DICT_FIELDS and isinstance(value, dict):
                out.append({"path": child + ".provider/model", "kind": "dict", "default": "route"})
            elif key in CHAIN_LIST_FIELDS and isinstance(value, list) and value and isinstance(value[0], dict) and "model" in value[0]:
                out.append({"path": child + "[].provider/model", "kind": "chain", "default": "route"})
            elif isinstance(value, (dict, list)):
                _walk_core(value, child, out)
    elif isinstance(node, list):
        for item in node[:2]:
            _walk_core(item, path + "[i]", out)


def scan_core(defaults: Dict[str, Any]) -> List[dict]:
    """Return every model-selection candidate path in DEFAULT_CONFIG."""
    out: List[dict] = []
    _walk_core(defaults, "", out)
    return out


_MODEL_KEY_RE = re.compile(r"^(?:.*_)?model(?:_id)?$")


def scan_plugin_manifest(manifest_path: Path) -> List[dict]:
    """Scan a plugin manifest's config_schema for model-selection fields.

    A schema key is a candidate when its name is/ends with ``model`` or
    ``model_id`` and its default is a string. Base-URL keys that pair with a
    model field are reported as companions, not slots.
    """
    manifest_path = Path(manifest_path)
    text = manifest_path.read_text(encoding="utf-8")
    if manifest_path.suffix == ".json":
        data = json.loads(text)
    else:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    schema = data.get("config_schema") or {}
    out: List[dict] = []
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        if _MODEL_KEY_RE.match(key) and isinstance(spec.get("default", ""), str):
            out.append({
                "path": f"{manifest_path.parent.name}:config_schema.{key}",
                "kind": "field",
                "default": spec.get("default", ""),
            })
    return out


def scan_plugins(plugins_dir: Path) -> Dict[str, List[dict]]:
    """Scan every installed plugin directory for model-selection candidates."""
    results: Dict[str, List[dict]] = {}
    for manifest in sorted(Path(plugins_dir).glob("*/plugin.*")):
        try:
            candidates = scan_plugin_manifest(manifest)
        except Exception:
            continue
        if candidates:
            results[manifest.parent.name] = candidates
    return results


def is_excluded(path: str) -> Optional[str]:
    """Return the exclusion reason for a path, or None if not excluded."""
    if path in EXCLUSIONS:
        return EXCLUSIONS[path]
    head = path.split(".")[0]
    if head in EXCLUSIONS:
        return EXCLUSIONS[head]
    return None
