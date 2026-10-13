"""Source-to-slot coverage registry for hermes-model-switcher.

Public API:
    build_registry()                       -> versioned registry dict
    all_slots() / slot_index()             -> descriptors
    coverage_gaps(candidates, registry)    -> uncovered candidate paths
    normalize_candidate(path)              -> candidate path to slot id
"""
from .scanner import (
    EXCLUSIONS,
    load_default_config,
    scan_core,
    scan_plugin_manifest,
    scan_plugins,
    is_excluded,
)
from .descriptors import (
    REGISTRY_VERSION,
    HERMES_BASELINE,
    SlotDescriptor,
    all_slots,
    build_registry,
    slot_index,
    CORE_SLOTS,
    PLUGIN_SLOTS,
)

import re

# Candidate-path -> slot-id normalizations for family descriptors.
_TTS_STT_RE = re.compile(r"^(tts|stt)\.([^.]+)\.(model|model_id)$")
_MOA_RE = re.compile(r"^moa\.presets\.[^.]+\.(reference_models|aggregator)")
_PLUGIN_SCHEMA_RE = re.compile(r"^([^:]+):(config_schema\..+)$")


def normalize_candidate(path: str) -> str:
    """Collapse per-backend/per-preset candidates into their family slot id."""
    m = _PLUGIN_SCHEMA_RE.match(path)
    if m:
        return f"plugin:{m.group(1)}:{m.group(2)}"
    m = _TTS_STT_RE.match(path)
    if m:
        return f"{m.group(1)}.<backend>.model|model_id"
    if _MOA_RE.match(path):
        suffix = "reference_models[].provider/model" if ".reference_models" in path \
            else "aggregator.provider/model"
        return f"moa.presets.<name>.{suffix}"
    return path


def coverage_gaps(candidates, registry: dict) -> list:
    """Candidate paths (normalized) with no descriptor and no exclusion."""
    slots = {s["slot_id"] for s in registry["slots"]}
    exclusions = registry["exclusions"]
    gaps = []
    for cand in candidates:
        path = cand["path"] if isinstance(cand, dict) else cand
        if is_excluded(path):
            continue
        slot_id = normalize_candidate(path)
        if slot_id in slots:
            continue
        gaps.append(path)
    return gaps


__all__ = [
    "REGISTRY_VERSION", "HERMES_BASELINE", "SlotDescriptor",
    "all_slots", "build_registry", "slot_index", "CORE_SLOTS", "PLUGIN_SLOTS",
    "load_default_config", "scan_core", "scan_plugin_manifest", "scan_plugins",
    "is_excluded", "EXCLUSIONS", "normalize_candidate", "coverage_gaps",
]
