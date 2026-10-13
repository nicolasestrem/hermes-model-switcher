"""hermes-model-switcher — dashboard backend routes (T7 wiring).

Mounted by the Dashboard at /api/plugins/hermes-model-switcher/ per the
official extension contract (website/docs/user-guide/features/
extending-the-dashboard.md): a dashboard plugin is
``<plugin>/dashboard/manifest.json`` + ``dashboard/dist/index.js`` (IIFE via
the Plugin SDK) + optional ``dashboard/plugin_api.py`` FastAPI routes.

Wiring (real, no mocks):
  GET  /state                      profiles + route inventory + presets
  POST /preview                    pure change preview (no writes)
  POST /presets                    persist a reviewed preset
  POST /operations                 journalled apply (engine)
  GET  /operations/{op_id}         operation record (sanitized DTO)
  POST /operations/{op_id}/undo    revision-guarded undo
  POST /probe                      new-session probe — EXPLICITLY
                                   AUTHORIZED only; blocked without an
                                   explicit route + cost approval. Never
                                   fabricates usage rows.

Privacy contract (Phase B review):
  - Public DTOs NEVER carry raw config documents, journal snapshots, or
    secrets. Only dotted route observations (provider/model/state) and
    sanitized storage names.
  - Every write target is bound to an EXPLICIT profile discovered through
    Hermes' own APIs (hermes_adapter.discover_profiles); a target whose path
    is not a discovered profile's config is refused before any write.
  - Errors are sanitized: no file contents, no stack traces to the client.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Import bootstrap for BOTH supported install layouts, because the modules the
# backend imports live in different places in each:
#   packaged: <plugin>/python/model_switcher  + <plugin>/{hermes_adapter,route_inventory}.py
#   checkout: <plugin>/src/model_switcher    + the same root modules
# A checkout is a src/ layout (pyproject packages.find where=["src"]), so
# omitting src/ left every API-backed action dead with
# ModuleNotFoundError: No module named 'model_switcher'.
_PKG_ROOT = Path(__file__).resolve().parent.parent
for _p in (_PKG_ROOT / "python", _PKG_ROOT / "src", _PKG_ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import copy
import json
import os

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from hermes_adapter import HermesAdapter
from hermes_adapter import discover_profiles
from route_inventory import (RouteEdit, SlotFamily, build_inventory,
                             preview_changes, _JUDGE_TYPED_KEYS)

from model_switcher.adapters import adapter_for
from model_switcher.adapters import _get_dotted
from model_switcher.adapters import _MISSING as _STORE_MISSING
from model_switcher.engine import (
    ApplyBlocked,
    apply_operation,
    plan_operation,
    reconcile,
    undo_operation,
)
from model_switcher.journal import (
    OP_APPLYING,
    OP_COMMITTED,
    OP_PARTIAL,
    OP_PENDING,
    OP_RECOVERY_REQUIRED,
    OperationJournal,
    to_public_operation,
)
from model_switcher.presets import (
    BillingPolicy,
    PresetStore,
    ReviewedPreset,
    RouteTarget,
    _route_key,
)

_ROUTE_FILTER_KEYS = ("provider", "model", "base_url", "api_mode")

# Typed route payload the UI may carry for ONE slot. ``chain`` is the ordered
# fallback chain (list of {provider, model}); ``null`` means inherit/unset,
# ``[]`` means explicitly disabled. Preview and apply MUST forward the same
# typed payload, so a chain edit is never silently narrowed to a bare
# provider/model pair (which would otherwise write an empty chain).
_TYPED_PAYLOAD_KEYS = ("provider", "model", "base_url", "api_mode", "chain")

# Fields a PUBLIC preview diff may expose out of a stored route value. A route
# dict can share its object with protected neighbours (threshold, enforcement)
# or secret-adjacent material (api_key, headers); only the route fields that
# this feature owns ever leave the backend.
_PUBLIC_ROUTE_KEYS = ("provider", "model", "base_url", "api_mode", "chain")

# route_inventory observation source -> the UI's state vocabulary
# ("configured" | "inherit" | "unset").
_UI_STATE_NAMES = {
    "configured": "configured",
    "inherited": "inherit",
    "default": "unset",
}

# Ordered-chain storage paths: their value is a list (configured), [] (explicitly
# disabled) or null (inherit-unset). They need three-state reporting — the flat
# configured|inherit|unset vocabulary would render [] as "configured".
_CHAIN_STORAGE_PATHS = {"fallback_providers", "delegation.fallback_providers"}

# UI family ids as the front-end's FAMILIES table spells them. The inventory's
# SlotFamily values differ for judge/cron/moa (hidden family); map them so a
# preset's scope summary never labels a non-main family as "Main".
_INVENTORY_FAMILY_TO_UI = {
    "main": "main", "delegation": "delegation", "auxiliary": "auxiliary",
    "judge": "judges", "fallback": "fallback", "moa": "fallback",
    "cron": "automation", "plugin": "plugin", "specialist": "specialist",
}

# A preview binds an apply to an EXACT prepared mutation. It is bound to the
# store revision + profile identity it was prepared from and expires, so a
# stale review can never be replayed onto a config that moved on.
_PREVIEW_TTL_SECONDS = 900
_PREVIEW_MAX = 32

router = APIRouter()

# -- process-local state ----------------------------------------------------
# The journal + preset store persist under a PRIVATE Hermes state dir that is
# resolved at runtime (via Hermes APIs; never a hardcoded user path) and is
# deliberately OUTSIDE the plugin source tree: the documented install clones
# this repository into <home>/plugins/, so any state file under the plugin
# package (or any git worktree) is refused by OperationJournal — place it in
# the home's own state location instead. ``HERMES_MODEL_SWITCHER_STATE_DIR``
# is an explicit operator override for exotic layouts.
_LOCK = threading.RLock()
_ADAPTER = HermesAdapter()
_PLUGIN_STATE_ENV = "HERMES_MODEL_SWITCHER_STATE_DIR"


def _state_dir_for(home: Path) -> Path:
    """Private state dir for an explicitly resolved Hermes home.

    Default: <home>/state/hermes-model-switcher/ — inside the Hermes home
    (profile-local, private) but NEVER inside the plugin checkout even when
    the plugin is cloned into <home>/plugins/. In unusual layouts where even
    the resolved home lives inside this plugin package (hosted isolation),
    fall back to the user's XDG state dir instead of a worktree path.
    """
    candidate = Path(home) / "state" / "hermes-model-switcher"
    if _is_inside_plugin_tree(candidate):
        candidate = Path.home() / ".local" / "state" / "hermes-model-switcher"
    candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        os.chmod(candidate, 0o700)  # private, never group/world readable
    except OSError:
        pass
    return candidate


def _is_inside_plugin_tree(path: Path) -> bool:
    """True when *path* would live inside the plugin's own source tree."""
    try:
        root = Path(_PKG_ROOT).resolve()
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _state_dir() -> Path:
    override = os.environ.get(_PLUGIN_STATE_ENV, "").strip()
    if override:
        d = Path(override).expanduser()
        d.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d
    from hermes_constants import get_hermes_home
    return _state_dir_for(Path(get_hermes_home()))


def _journal() -> OperationJournal:
    journal = OperationJournal(str(_state_dir() / "operations.db"))
    try:
        _reconcile_unfinished_on_restart(journal)
    except Exception:
        journal.close()
        raise
    return journal


# Crash-recovery restart path (P1 #6): a crash between a config write and the
# journalled step update leaves operations pending/applying/partial. The first
# journal use after a (re)start reconciles every unfinished operation against
# the actual files, so the documented crash-recovery contract holds even when
# no one ever clicks a specific operation again.
_STARTUP_RECONCILED: Optional[str] = None


def _reconcile_unfinished(journal: OperationJournal) -> List[Dict[str, Any]]:
    """Run engine.reconcile over every unfinished operation. Returns the
    reconciled records; a reconcile failure surfaces to the caller."""
    reconciled = []
    for op in journal.unfinished_operations():
        reconciled.append(reconcile(journal, op["op_id"]))
    return reconciled


def _reconcile_unfinished_on_restart(journal: OperationJournal) -> None:
    global _STARTUP_RECONCILED
    if _STARTUP_RECONCILED == journal.db_path:
        return
    if journal.unfinished_operations():
        _reconcile_unfinished(journal)
    _STARTUP_RECONCILED = journal.db_path


_PRESET_CORRUPT: Optional[bool] = None


def _quarantine_presets(path: Path) -> None:
    """A corrupt preset store is never silently dropped: move it aside so an
    operator can recover it, and surface the condition via /state."""
    global _PRESET_CORRUPT
    _PRESET_CORRUPT = True
    dest = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}")
    # Failure blocks saving rather than authorizing loss of recovery data.
    os.replace(path, dest)


def _preset_store() -> PresetStore:
    global _PRESET_CORRUPT
    store = PresetStore()
    path = _state_dir() / "presets.json"
    _PRESET_CORRUPT = bool(next(path.parent.glob("presets.json.corrupt-*"), None))
    if path.exists():
        try:
            records = json.loads(path.read_text())
            if not isinstance(records, list):
                raise ValueError("preset store is not a list")
        except Exception:
            _quarantine_presets(path)
            records = []
        for rec in records:
            try:
                preset = _preset_from_record(rec)
            except Exception:
                _quarantine_presets(path)
                return PresetStore()
            # ui_changes (the reviewed change intent incl. its slot identity)
            # is attached, not baked into the frozen RouteTarget list.
            ui = rec.get("ui_changes")
            preset.ui_changes = ui if isinstance(ui, list) and ui \
                else _ui_changes_from_targets(preset)
            store.save(preset)
    return store


def _persist_presets(store: PresetStore) -> None:
    path = _state_dir() / "presets.json"
    records = []
    for pid, p in getattr(store, "_presets", {}).items():
        records.append({
            "preset_id": p.preset_id, "name": p.name,
            "approved_classes": p.billing_policy.approved_classes,
            "allow_unknown": p.billing_policy.allow_unknown,
            "reviewed_profile_ids": p.reviewed_profile_ids,
            "targets": [{
                "profile": t.profile, "store_kind": t.store_kind,
                # bound to the discovered profile's own config path only
                "path": t.path, "changes": t.changes,
                "cost_class": t.cost_class,
                # P1 #2: persist the REAL slot identity of each target —
                # applying must restore cron/delegation/auxiliary/...
                # targets onto their own slots, never rebuild them as main.
                "slot_id": _slot_id_for_target(p, t),
            } for t in p.targets],
            # the original typed change intent (as reviewed), restored on
            # every one-click apply
            "ui_changes": _ui_changes_for_preset(p),
        })
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2))
    os.replace(tmp, path)


def _slot_id_for_target(p: ReviewedPreset, t: RouteTarget) -> str:
    """Slot id recorded with each target (falls back to structural inference
    for presets assembled without an explicit change list)."""
    ids = getattr(p, "target_slot_ids", None)
    if isinstance(ids, list):
        try:
            idx = p.targets.index(t)
            return str(ids[idx])
        except (ValueError, IndexError):
            pass
    return _ui_slot_name(_infer_slot_id(t.changes))


def _ui_changes_for_preset(p: ReviewedPreset):
    ui = getattr(p, "ui_changes", None)
    if isinstance(ui, list) and ui:
        return ui
    return _ui_changes_from_targets(p)


def build_slot_ids(changes: List[Dict[str, Any]],
                   targets: List[RouteTarget]) -> Optional[List[str]]:
    """Pair accepted changes with the bound targets (strict binding is
    positional: one RouteTarget per accepted change)."""
    if len(changes) != len(targets):
        return None
    return [_slot_id_of_change(c) for c in changes]


def _slot_id_of_change(c: Dict[str, Any]) -> str:
    sid = c.get("slotId")
    return "main" if sid in (None, "main", "main.default") else str(sid)


def _raw_slot_name(ui_name: str) -> str:
    return "main.default" if ui_name == "main" else ui_name


def _preset_from_record(rec: Dict[str, Any]) -> ReviewedPreset:
    return ReviewedPreset(
        preset_id=rec["preset_id"], name=rec["name"],
        billing_policy=BillingPolicy(
            approved_classes=rec.get("approved_classes", []),
            allow_unknown=rec.get("allow_unknown", False)),
        targets=[RouteTarget(
            profile=t["profile"], store_kind=t["store_kind"], path=t["path"],
            changes=t["changes"], cost_class=t["cost_class"])
            for t in rec["targets"]],
        reviewed_profile_ids=rec.get("reviewed_profile_ids", []))


# -- preset intent <-> slot identity (P1 #2) --------------------------------
def _infer_slot_id(changes: Dict[str, Any]) -> str:
    """Legacy records carry no ui_changes; infer the slot from the typed
    storage paths so a non-main target is NEVER reconstructed as main."""
    keys = set(changes)
    if "cron.model" in keys:
        return "cron.model"
    if "delegation.fallback_providers" in keys:
        return "delegation.fallback_chain"
    if "fallback_providers" in keys:
        return "fallback.main_chain"
    if "moa.default_preset" in keys:
        return "moa.default_preset"
    for key in sorted(keys):
        if key.startswith("moa.presets."):
            return f"moa.preset.{key.split('.')[2]}"
    if any(k.startswith("auxiliary.goal_judge") for k in keys):
        return "judge.goal_judge.route"
    aux = {k for k in keys if k.startswith("auxiliary.")}
    if aux:
        slot = sorted(aux)[0].split(".")[1]
        return f"auxiliary.{slot}"
    if any(k.startswith("delegation.") for k in keys):
        return "delegation.model"
    # Specialist routes live at the backend-specific runtime paths
    # (stt.<provider>.model / tts.<provider>.model); the legacy speech.* keys
    # are still recognised so records reviewed before the inventory rename
    # restore without a silent roundtrip.
    if any(key.startswith("stt.") and key.endswith((".model", ".model_id")) for key in keys) or "speech.model_id" in keys:
        return "specialist.stt"
    if any(key.startswith("tts.") and key.endswith((".model", ".model_id")) for key in keys) or "speech.streaming_model" in keys:
        return "specialist.tts"
    if "embedding.model" in keys:
        return "specialist.embedding"
    return "main.default"


def _ui_changes_from_targets(preset: ReviewedPreset) -> List[Dict[str, Any]]:
    """Restore the original typed change intent from frozen targets + slot
    identity (legacy-record path). Only MAIN targets may read
    model.provider/model.default — every other slot reads its own keys."""
    ui: List[Dict[str, Any]] = []
    for i, t in enumerate(preset.targets):
        ids = getattr(preset, "target_slot_ids", None)
        ui_name = (str(ids[i]) if isinstance(ids, list) and i < len(ids)
                   else _ui_slot_name(_infer_slot_id(t.changes)))
        ui.append({"profileId": t.profile, "slotId": ui_name,
                   "action": "set",
                   "to": _typed_to_from_changes(_raw_slot_name(ui_name),
                                                t.changes)})
    return ui


def _ui_slot_name(slot_raw: str) -> str:
    return "main" if slot_raw == "main.default" else slot_raw


def _typed_to_from_changes(slot_raw: str,
                           changes: Dict[str, Any]) -> Dict[str, Any]:
    def path(key: str) -> Any:
        return changes.get(f"{slot_raw}.{key}")

    if slot_raw == "main.default":
        return {"provider": changes.get("model.provider"),
                "model": changes.get("model.default")}
    if slot_raw == "cron.model":
        return {"provider": changes.get("cron.model_provider"),
                "model": changes.get("cron.model")}
    if slot_raw == "delegation.model":
        return {"provider": changes.get("delegation.provider"),
                "model": changes.get("delegation.model")}
    if slot_raw in ("delegation.fallback_chain", "fallback.main_chain"):
        return {"chain": changes.get({"delegation.fallback_chain":
                                      "delegation.fallback_providers",
                                      "fallback.main_chain":
                                      "fallback_providers"}[slot_raw])}
    if slot_raw == "moa.default_preset":
        return {"provider": "", "model": changes.get("moa.default_preset")}
    if slot_raw.startswith("moa.preset."):
        name = slot_raw[len("moa.preset."):]
        route = changes.get(f"moa.presets.{name}.aggregator", {})
        if isinstance(route, dict):
            return {"provider": route.get("provider"), "model": route.get("model")}
        return {"provider": None, "model": None}
    if slot_raw.startswith("auxiliary.") or slot_raw.startswith("judge."):
        return {"provider": path("provider"), "model": path("model")}
    # Specialist scalar routes: the typed intent lands on the backend-specific
    # runtime path, not the slot id. Restore the backend/provider pair;
    # ElevenLabs uses model_id, other supported backends use model.
    if slot_raw in ("specialist.stt", "specialist.tts"):
        section = slot_raw.rsplit(".", 1)[-1]
        for key, value in changes.items():
            if key.startswith(section + ".") and key.endswith((".model", ".model_id")):
                return {"provider": changes.get(section + ".provider") or key.split(".")[1], "model": value}
        legacy = "speech.model_id" if section == "stt" else "speech.streaming_model"
        return {"provider": "", "model": changes.get(legacy)}
    if slot_raw == "specialist.embedding":
        return {"provider": "", "model": changes.get("embedding.model")}
    return {"provider": "", "model": changes.get(slot_raw)}


# -- request models ---------------------------------------------------------
class PresetIn(BaseModel):
    name: str
    changes: List[Dict[str, Any]]
    billingPolicy: str = "reviewed"
    allowUnknownCost: bool = False


class ApplyIn(BaseModel):
    presetId: Optional[str] = None
    previewId: Optional[str] = None
    changes: Optional[List[Dict[str, Any]]] = None
    # Explicit acknowledgement that unknown-cost routes are never treated as
    # free; without it an unknown-cost route blocks the one-click apply.
    allowUnknownCost: bool = False


class ProbeIn(BaseModel):
    provider: str
    model: str
    authorized: bool = False
    cost_approval: Optional[Dict[str, Any]] = None


# slot aliases: a UI slotId may use the family shorthand; resolve it to the
# real inventory slot so edits bind to the actual slot, never a guess.
_SLOT_ALIASES = {"cron": "cron.model", "fallback": "fallback.main_chain"}


def _slot_alias(slot_id: Any) -> str:
    """Raw inventory slot id for a UI slotId ('main'/'cron' shorthand → the
    underlying slot, 'main' → 'main.default'). Unknown ids pass verbatim so
    an invalid slot is still reported as blocked, never substituted."""
    s = "main.default" if slot_id in (None, "", "main") else str(slot_id)
    return _SLOT_ALIASES.get(s, s)


# -- helpers ----------------------------------------------------------------
def _runtime_writable() -> bool:
    return bool(_ADAPTER.runtime.supported) and not _ADAPTER.runtime.read_only


_MODEL_CATALOG_MODULE: Optional[Any] = None


def _model_catalog_module() -> Any:
    """Load dashboard/model_catalog.py by FILE PATH.

    The Dashboard loads plugin_api by module spec (a unique module name, not a
    package), so ``from dashboard.model_catalog import ...`` is not reliable.
    Loading by path works in both the installed and the checkout layout. The
    module is cached for the process."""
    global _MODEL_CATALOG_MODULE
    if _MODEL_CATALOG_MODULE is not None:
        return _MODEL_CATALOG_MODULE
    import importlib.util
    path = Path(__file__).resolve().parent / "model_catalog.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_model_switcher_model_catalog", str(path))
    if spec is None or spec.loader is None:
        raise ImportError("model_catalog module not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODEL_CATALOG_MODULE = module
    return module


def _config_path_for(profile: str, ident) -> Path:
    """A profile's config.yaml, bound to the DISCOVERED profile directory."""
    return Path(ident.path) / "config.yaml"


def _slot_configured_view(obs, cfg: Dict[str, Any],
                          d) -> Tuple[str, str]:
    """The value LITERALLY stored for the slot — explicit CONFIGURED only, no
    inheritance applied.

    Scalar slots (main.default, delegation.model, cron.model, specialist model
    ids …) carry a bare model string; their provider comes from the paired
    companion path (model.provider / delegation.provider / cron.model_provider)
    when one exists. Route-dict slots read provider and model out of the stored
    dict. Nothing is inferred: an absent provider reports as "" — never a guess
    and never an inherited value (the resolved route lives in effectiveRoute).
    """
    stored = obs.configured
    if stored is _STORE_MISSING:
        stored = None
    provider: Any = None
    model: Any = None
    if isinstance(stored, dict):
        provider = stored.get("provider")
        model = stored.get("model")
    elif isinstance(stored, str):
        model = stored
    if provider is None:
        for comp in d.companion_paths:
            # Only PROVIDER companions qualify: a route dict's neighbours can
            # include model.base_url/api_mode, and reporting the endpoint URL
            # as the provider would corrupt /state identities (and leak an
            # unsanitized URL into the inventory).
            if not comp.endswith((".provider", "_provider")):
                continue
            cv = _get_dotted(cfg, comp)
            if isinstance(cv, str) and cv.strip():
                provider = cv
                break
    return (provider if provider is not None else "",
            model if model is not None else "")


def _slot_effective_view(obs, configured_provider: Any = "") -> Dict[str, Any]:
    """The RESOLVED route (inheritance applied) the slot would actually use,
    safe to render: ``{provider, model}`` plus ``chain`` for chain slots.

    Provider precedence: the explicit inheritance resolver
    (``obs.effective_provider``) first, then a route dict's own provider, then
    the configured companion (main's ``model.provider`` involves no
    inheritance). Chain entries are allowlisted + URL-sanitized before leaving
    the backend.
    """
    eff = obs.effective
    provider = obs.effective_provider
    model: Any = None
    chain: Any = None
    if isinstance(eff, dict):
        model = eff.get("model")
        if provider is None:
            provider = eff.get("provider")
    elif isinstance(eff, list):
        chain = _public_diff_value(eff, "")
    elif isinstance(eff, str):
        model = eff
    if provider is None:
        provider = configured_provider
    out: Dict[str, Any] = {"provider": provider if provider is not None else "",
                           "model": model if model is not None else ""}
    if chain is not None:
        out["chain"] = chain
    return out


def _chain_slot_state(obs) -> Tuple[str, Any]:
    """Truthful three-state view of an ordered-chain slot.

    * a null chain INHERITS when a parent chain supplies the value, otherwise it
      is genuinely UNSET at the top level — never "disabled" or "removed";
    * ``[]`` is explicitly DISABLED (distinct from inherit);
    * any list is configured.
    The effective chain is returned alongside so the row can show it verbatim.
    """
    if obs.source == "inherited":
        return "inherit", obs.effective
    if not obs.configured_present or obs.configured is None:
        return "unset", obs.effective
    if obs.configured == []:
        return "disabled", []
    return "configured", obs.effective


def _ui_family_for_slot(slot_raw: Any) -> str:
    """UI family id for a raw slot id (main.default -> main, judge.* -> judges,
    cron.model -> automation …). Used to summarise a preset's real scope."""
    slot = "main.default" if slot_raw in (None, "", "main") else str(slot_raw)
    if slot in ("main.default",):
        return "main"
    prefix = slot.split(".", 1)[0]
    return _INVENTORY_FAMILY_TO_UI.get(prefix, prefix)


def _profile_inventory() -> Dict[str, Dict[str, Any]]:
    """Per discovered profile: sanitized route inventory. Never returns raw
    config documents."""
    profiles: Dict[str, Dict[str, Any]] = {}
    for name, ident in discover_profiles().items():
        try:
            cfg = _ADAPTER.load_raw_config(name)
        except Exception:
            profiles[name] = {"id": name, "name": name, "writable": False,
                              "error": "config unreadable", "slots": []}
            continue
        slots = []
        for obs in build_inventory(cfg):
            d = obs.descriptor
            # UI contract: the main route presents as slotId "main"; the raw
            # inventory slot is "main.default" (model) with provider as a
            # companion write (model.provider).
            slot_id = "main" if d.slot_id == "main.default" else d.slot_id
            provider, model = _slot_configured_view(obs, cfg, d)
            entry = {
                "profileId": name, "slotId": slot_id,
                "family": getattr(d.family, "value", str(d.family)),
                # CONFIGURED (explicit stored) route — never an inherited value.
                "provider": provider, "model": model,
                # The RESOLVED route (inheritance applied): what the slot would
                # actually use. The UI renders this separately in the expanded
                # row, so configured and effective can never be conflated.
                "effectiveRoute": _slot_effective_view(obs, provider),
                # UI vocabulary is configured | inherit | unset; map the
                # inventory's source names onto it exactly.
                "state": _UI_STATE_NAMES.get(obs.source, obs.source),
                "writable": _runtime_writable() and d.writable and not d.protected,
                "storagePath": d.storage_path,
                # No runtime usage evidence here: last-observed is genuinely
                # UNOBSERVED (never a copy of the configured value).
                "lastObserved": None,
                "protocol": d.protocol,
            }
            if d.storage_path in _CHAIN_STORAGE_PATHS:
                # Chains carry three states, not one: a stored null INHERITS
                # (or is unset at top level), [] is explicitly DISABLED, and a
                # list is configured. Reporting [] as "configured" and a null
                # as a bare empty route would make a disabled chain look
                # removed and an inherited chain look configured.
                state, chain = _chain_slot_state(obs)
                entry["state"] = state
                entry["chain"] = chain
            slots.append(entry)
        profiles[name] = {"id": name, "name": name,
                          "writable": _runtime_writable(), "slots": slots}
    return profiles


def _canonical_main_changes(cfg: Dict[str, Any], to: Dict[str, Any]) -> Dict[str, Any]:
    """P1 #4: a main-route switch is prepared by Hermes' canonical assignment
    seam (HermesAdapter.prepare_main_assignment) BEFORE any write — the same
    validation the /preview promises (provider alias canonicalization, custom
    endpoints via providers rows, model.base_url / api_mode side effects).
    Returns the typed ``model.*`` storage changes to apply. Raises on an
    invalid route: callers must block, never silently substitute.

    Canonical stale-key cleanup is represented by the engine's verified
    deletion contract. Never retain stale settings because a scratch probe
    failed; deletion correctness is tested alongside the shipped adapter.
    """
    provider = str(to.get("provider") or "").strip()
    model = str(to.get("model") or "").strip()
    if not provider or not model:
        raise ValueError("provider and model required for main")
    prepared = _ADAPTER.prepare_main_assignment(
        cfg, provider, model, base_url="", api_key="")
    result = prepared["result"]
    if not getattr(result, "success", True):
        raise ValueError(getattr(result, "error_message", "") or "rejected route")
    from hermes_cli.model_switch import apply_model_selection
    old_block = dict(cfg.get("model") or {})
    new_block = apply_model_selection(old_block, result)
    changes: Dict[str, Any] = {}
    for key in sorted(set(old_block) | set(new_block)):
        new_val = new_block.get(key)
        if key not in new_block or new_val is None:
            if key in old_block:
                changes[f"model.{key}"] = None  # stale-key cleanup
            continue
        if old_block.get(key) != new_val:
            changes[f"model.{key}"] = new_val
    if not changes:
        raise ValueError("change is a no-op for this slot")
    return changes


def _assert_removal_identity(desc, observed: Dict[str, Any],
                             from_payload: Optional[Any]) -> None:
    """Exact-removal identity: the reviewed route the user saw must still be
    the server's current route. The server snapshot is authoritative — a
    mismatch blocks (never inferred, never auto-repaired)."""
    if not isinstance(from_payload, dict):
        return
    for key in ("provider", "model"):
        want = from_payload.get(key)
        if want in (None, ""):
            continue
        if observed.get(key) != want:
            raise ValueError(
                f"slot {desc.slot_id!r} changed since review: expected "
                f"{key}={want!r}, found {observed.get(key)!r} — re-preview "
                "before removing")


def _removal_changes_for(desc, cfg: Dict[str, Any],
                         from_payload: Optional[Any] = None) -> Dict[str, Any]:
    """P1 #3: an exact-removal edit binds as an UNSET of this slot's own
    keys — per slot semantics, never a null/"None" route assignment:
      - route-dict slots (judge/aux/specialist households): ONLY the route
        fields (provider/model/base_url/api_mode) inside that dict are
        unset — protected neighbours beside them (threshold, enforcement)
        SURVIVE verbatim and the whole object is never dropped; the removal
        is also checked against the reviewed ``from`` (the server snapshot)
        before it binds;
      - scalar slots (main, cron, ...): the slot key plus its companions —
        the paired inference provider is unset with the pair even when it is
        currently absent (cron.model_provider), while opportunistic
        side-effect companions (model.base_url/api_mode) are unset only when
        present; cron.provider, the scheduler, is not a companion and is
        never touched;
      - chain slots: the stored value becomes None (inherit-unset semantics
        restored verbatim — config preserves null-vs-[] distinctions).
    Raises ValueError for an invalid MoA removal (preset still referenced
    by moa.default_preset) — reject-don't-repair, nothing written.
    """
    if not desc.writable or desc.protected:
        raise ValueError("slot is protected/read-only")
    if (desc.family is SlotFamily.MOA
            and desc.storage_path.startswith("moa.presets.")):
        raise ValueError("cannot remove a required aggregator route; remove the preset through Hermes")
    changes: Dict[str, Any] = {}
    current = _get_dotted(cfg, desc.storage_path)
    if isinstance(current, dict):
        # Route-dict slot: remove ONLY the route fields this slot owns.
        observed = {k: current[k] for k in _ROUTE_FILTER_KEYS if k in current}
        _assert_removal_identity(desc, observed, from_payload)
        for key in _ROUTE_FILTER_KEYS:
            if key in current:
                changes[f"{desc.storage_path}.{key}"] = None
        if not changes:
            raise ValueError("change is a no-op for this slot")
        return changes
    # Scalar / chain slots: unset the slot key plus its companions.
    # The reviewed ``from`` identity is validated against the CURRENT stored
    # route FIRST: when the route changed after review, the removal blocks
    # instead of deleting a route the user never selected. (Route-dict slots
    # run the same check above; scalar slots previously skipped it.)
    if isinstance(current, str):
        observed = {
            "provider": _provider_from_changes(
                {c: _get_dotted(cfg, c) for c in desc.companion_paths
                 if _get_dotted(cfg, c) is not _STORE_MISSING}),
            "model": current,
        }
        _assert_removal_identity(desc, observed, from_payload)
    changes[desc.storage_path] = None
    for comp in desc.companion_paths:
        if desc.family is SlotFamily.CRON and comp.endswith("model_provider"):
            # cron.model always pairs with cron.model_provider (inference
            # provider, mirroring route_inventory's preview companion rule);
            # the absent pair member is unset too. cron.provider (scheduler)
            # is not a companion and is never touched.
            changes[comp] = None
        elif _get_dotted(cfg, comp) is not _STORE_MISSING:
            changes[comp] = None
    if not changes:
        raise ValueError("change is a no-op for this slot")
    return changes


def _duplicate_slot_changes(changes: List[Dict[str, Any]]
                            ) -> List[Dict[str, Any]]:
    """Changes that repeat a (profile, slot) pair.

    Two intents for one slot (e.g. a set AND an exact-removal from the same
    editor session) would be coalesced by the engine's same-store grouping
    (later ``changes.update()`` wins), silently applying only one of them.
    Every entry path rejects them instead: browser, direct API, and preset.
    """
    seen = set()
    blocked: List[Dict[str, Any]] = []
    for c in changes:
        key = (c.get("profileId"), _slot_alias(c.get("slotId")))
        if key in seen:
            blocked.append({
                "profileId": c.get("profileId"),
                "slotId": c.get("slotId"),
                "reason": ("duplicate change for the same slot; combine the "
                           "set and removal into one intent"),
            })
        else:
            seen.add(key)
    return blocked


def _changes_to_targets(changes: List[Dict[str, Any]],
                        profiles: Dict[str, Dict[str, Any]],
                        strict: bool = True):
    """Validate + bind changes to explicit discovered-profile config paths.
    Returns (targets, blocked). A change for an unknown or non-writable
    profile, or a slot not present in that profile's inventory, is blocked —
    never silently substituted."""
    targets, blocked = [], []
    duplicates = _duplicate_slot_changes(changes)
    if duplicates:
        # Two intents for one slot would be coalesced (later wins) and only
        # ONE of them applied — reject the whole request instead.
        if strict:
            raise HTTPException(status_code=422, detail={
                "ok": False, "error": "preview blocked for some profiles",
                "blocked": duplicates})
        return [], duplicates
    for c in changes:
        pid = c.get("profileId")
        prof = profiles.get(pid)
        if prof is None or prof.get("error"):
            blocked.append({"profileId": pid, "reason": "unknown profile"})
            continue
        if not prof.get("writable"):
            blocked.append({"profileId": pid, "reason": "profile is read-only"})
            continue
        ident = discover_profiles().get(pid)
        if ident is None:
            blocked.append({"profileId": pid, "reason": "profile identity changed"})
            continue
        cfg_path = _config_path_for(pid, ident)
        slot_raw = _slot_alias(c.get("slotId"))
        slot = next((s for s in prof["slots"] if s["slotId"] == (
            "main" if slot_raw == "main.default" else slot_raw)), None)
        if slot is None:
            blocked.append({"profileId": pid,
                            "reason": f"unknown slot {c.get('slotId')!r}"})
            continue
        # The stored changes come from route_inventory.preview_changes — the
        # real typed diff over this profile's raw config — NOT from a generic
        # "{slot}.model" guess. Slot shape is respected: main writes
        # model.default (string) + model.provider; cron.model pairs with
        # cron.model_provider and NEVER touches cron.provider (scheduler).
        to = c.get("to")
        try:
            cfg = _ADAPTER.load_raw_config(pid)
        except Exception:
            blocked.append({"profileId": pid, "reason": "config unreadable"})
            continue
        desc = next((o.descriptor for o in build_inventory(cfg)
                     if o.descriptor.slot_id == slot_raw), None)
        if desc is None:
            blocked.append({"profileId": pid,
                            "reason": f"unknown slot {c.get('slotId')!r}"})
            continue
        removal = str(c.get("action") or "").lower() == "remove"
        if removal:
            # P1 #3: to:null is a REMOVAL intent, not a route assignment.
            try:
                stored = _removal_changes_for(desc, cfg, c.get("from"))
            except ValueError as exc:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": str(exc)})
                continue
            targets.append(RouteTarget(
                profile=str(pid), store_kind="canonical_config", path=str(cfg_path),
                changes=stored,
                cost_class=_route_cost_class(str(pid), stored, ident)))
            continue
        if desc.slot_id == "main.default":
            # P1 #4: canonical preparation instead of a blind
            # model.default/model.provider overwrite. A seam refusal is an
            # intent-level block (409 apply contract), not a request-binding
            # error — flagged with kind="route_refusal" so the apply endpoint
            # distinguishes it from unknown-slot/protected binding errors.
            try:
                stored = _canonical_main_changes(cfg, to or {})
            except HTTPException as exc:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": _clean_detail(exc),
                                "kind": "route_refusal"})
                continue
            except Exception as exc:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": str(exc)[:200] or "route rejected",
                                "kind": "route_refusal"})
                continue
        else:
            try:
                cp = preview_changes(cfg, [RouteEdit(slot_raw, _typed_payload(to or {}))])
            except HTTPException as exc:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": _clean_detail(exc)})
                continue
            except Exception as exc:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": str(exc)[:200] or "route rejected"})
                continue
            if cp.rejected:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": "; ".join(cp.rejected)})
                continue
            stored: Dict[str, Any] = {}
            for ch in cp.changes:
                value = ch.after
                # Hermes stores the MAIN route as plain strings: model.default is
                # the model id, model.provider the provider (never a route dict).
                if (ch.storage_path == "model.default"
                        and isinstance(value, dict) and "model" in value):
                    value = value["model"]
                stored[ch.storage_path] = value
            if not stored:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": "change is a no-op for this slot"})
                continue
        targets.append(RouteTarget(
            profile=str(pid), store_kind="canonical_config", path=str(cfg_path),
            changes=stored,
            cost_class=_route_cost_class(str(pid), stored, ident)))
    if strict and blocked:
        raise HTTPException(status_code=422, detail={
            "ok": False, "error": "preview blocked for some profiles",
            "blocked": blocked})
    return targets, blocked


def _provider_from_changes(stored: Dict[str, Any]) -> str:
    for dotted, value in stored.items():
        if (dotted.endswith(".provider") or dotted.endswith("_provider")) \
                and isinstance(value, str):
            return value
    return ""


def _referenced_providers(stored: Dict[str, Any]) -> List[str]:
    """Every distinct provider a prepared target routes through, in order.

    Route/route-dict slots contribute their paired provider value
    (``model.provider`` / ``cron.model_provider`` / ``auxiliary.<x>.provider``);
    a route-dict VALUE carries its provider INSIDE the slot object
    (``{"auxiliary.compression": {"provider": "somecloud", "model": "x"}}``), so
    it is inspected too — otherwise such routes would vacuously pass
    credential checks and classify as metered;
    chain slots contribute EVERY entry's provider (a dict route's ``provider``
    or a bare ``provider/model`` string). A chain is never collapsed onto one
    provider — each hop is checked on its own.
    """
    out: List[str] = []
    seen = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    for dotted, value in stored.items():
        if dotted.endswith(".provider") or dotted.endswith("_provider"):
            if isinstance(value, str):
                add(value)
        elif isinstance(value, dict) and isinstance(value.get("provider"), str):
            add(value.get("provider"))
        elif dotted in _CHAIN_STORAGE_PATHS or dotted.endswith(".chain"):
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        add(entry.get("provider"))
                    elif isinstance(entry, str):
                        add(entry.split("/", 1)[0])
    return out


def _provider_is_custom_endpoint(cfg: Dict[str, Any], provider: str) -> bool:
    """True when ``provider`` is a user-declared endpoint — a ``providers:`` row
    carrying a ``base_url``. An explicitly configured custom endpoint needs no
    hosted credential (this is the "local/custom endpoint has no auth
    requirement" rule, derived from the config itself, not from a slug)."""
    table = cfg.get("providers")
    if not isinstance(table, dict):
        table = {}
    key = str(provider or "").strip()
    prov = table.get(key) or table.get(key.lower())
    return isinstance(prov, dict) and bool(str(prov.get("base_url") or "").strip())


def _profile_env_entries(profile: str, ident) -> Dict[str, str]:
    """Non-empty, non-comment KEY=VALUE entries from the profile's .env.

    Read only to decide credential PRESENCE; values are never returned to a
    client. Fail-soft on unreadable state (an empty mapping)."""
    env_file = Path(ident.path) / ".env"
    try:
        if not env_file.is_file():
            return {}
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for sep in ("=", ":"):
            if sep in stripped:
                key, _, value = stripped.partition(sep)
                key = key.strip()
                if key and value.strip():
                    out[key] = value.strip()
                break
    return out


def _runtime_provider_authenticated(profile: str, provider: str) -> Optional[bool]:
    """Provider-scoped runtime authentication via the installed picker catalog.

    Returns True when the picker reports the provider row authenticated, False
    when it explicitly reports it unauthenticated, and None when the provider is
    unknown to the catalog or the seam is unavailable. None is never treated as
    authenticated — the caller decides the unknown case explicitly.
    """
    provider = str(provider or "").strip().lower()
    if not provider:
        return None
    try:
        module = _model_catalog_module()
        with _ADAPTER.scoped(profile):
            payload = module._load_picker_payload(False)
    except Exception:
        return None
    rows = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    # Scan ALL matching rows before concluding: the picker payload can carry
    # duplicate rows for one slug (aliases/canonical entries), and returning
    # the FIRST match's flag would let an unauthenticated duplicate shadow a
    # genuinely authenticated row — refusing a valid connection.
    explicit_false = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        ids = {str(row.get("slug") or "").strip().lower(),
               str(row.get("provider_id") or "").strip().lower()}
        if provider not in ids:
            continue
        value = row.get("authenticated")
        if value is True:
            return True
        if value is False:
            explicit_false = True
    return False if explicit_false else None


def _provider_credentials_satisfied(profile: str, ident, cfg: Dict[str, Any],
                                    provider: str) -> bool:
    """Whether ``profile`` can authenticate routing to ``provider``.

    A user-declared custom endpoint needs no hosted credential. Otherwise the
    provider-scoped runtime authentication seam decides; when it cannot classify
    the provider, absence of scoped evidence blocks it. An unrelated env entry
    is never credential evidence for a different provider."""
    if not str(provider or "").strip():
        return True
    row = (cfg.get("providers") or {}).get(provider, {})
    if isinstance(row, dict):
        key_env = row.get("key_env") or row.get("api_key_env")
        if key_env:
            from agent.secret_scope import get_secret
            with _ADAPTER.scoped(profile):
                return bool(get_secret(str(key_env)))
        if row.get("api_key"):
            return True
    if _provider_is_custom_endpoint(cfg, provider):
        return True
    authed = _runtime_provider_authenticated(profile, provider)
    if authed is True:
        return True
    if authed is False:
        return False
    return False  # unrelated env entries cannot authenticate an unknown provider


def _route_cost_class(profile: str, stored: Dict[str, Any],
                      ident=None) -> str:
    """Live cost class for a prepared route, classified PER provider.

    Never infers a free/subscription tier from a provider slug. A custom
    endpoint is metered; a credentialed hosted provider is metered; a provider
    that cannot be classified at all (no custom endpoint, no runtime auth, no
    credential evidence) is "unknown" — unknown cost is never treated as free.
    """
    if ident is None:
        ident = discover_profiles().get(profile)
    providers = _referenced_providers(stored)
    if not providers:
        return "metered"  # no hosted provider to classify (e.g. scalar edits)
    try:
        cfg = _ADAPTER.load_raw_config(profile)
    except Exception:
        return "unknown"
    for provider in providers:
        try:
            if not _provider_credentials_satisfied(profile, ident, cfg, provider):
                return "unknown"
        except Exception:
            return "unknown"
    return "metered"


def _clean_detail(exc: HTTPException) -> str:
    detail = getattr(exc, "detail", "")
    text = detail if isinstance(detail, str) else str(detail)
    return (text or "route rejected")[:200]


def _observed_value(cfg: Dict[str, Any], dotted: str) -> Any:
    value = _get_dotted(cfg, dotted)
    return None if value is _STORE_MISSING else value


def _typed_payload(to: Dict[str, Any]) -> Dict[str, Any]:
    """Project a UI ``to`` onto the typed route fields this feature owns —
    including ``chain`` — so the exact reviewed intent (a chain edit, base_url,
    api_mode) reaches route_inventory verbatim and is never narrowed to a bare
    provider/model pair. Unknown/foreign keys never reach the prepared
    mutation.

    Typed-decision VALIDATION markers (``verdict``/``labels``/``schema``/
    ``typed``/``structured_output``) ride along so route_inventory's protocol
    check can see them — they are never STORED: the stored shape is built from
    ``_route_payload_fields`` (provider/model/base_url/api_mode) only.
    """
    if not isinstance(to, dict):
        return {}
    out = {k: to[k] for k in _TYPED_PAYLOAD_KEYS if k in to}
    for key in _JUDGE_TYPED_KEYS:
        if key in to:
            out[key] = to[key]
    return out


def _sanitize_public_url(value: Any) -> Any:
    """Strip userinfo (credentials) and query/fragment from a URL before it
    leaves the backend. A ``base_url`` can embed credentials or a query token;
    the public diff only ever needs ``scheme://host[:port]/path``."""
    if not isinstance(value, str) or not value:
        return value
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(value)
    except Exception:
        return value
    if not parts.scheme and not parts.netloc:
        return value  # not a URL — leave scalars untouched
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _public_route_field(key: str, value: Any) -> Any:
    return _sanitize_public_url(value) if key == "base_url" else value


def _sanitize_change(change: Dict[str, Any]) -> Dict[str, Any]:
    """A change echoed to the client with its route payloads allowlisted and
    URL-sanitized — a ``to``/``from`` ``base_url`` never carries credentials or
    a query token out of the backend."""
    out = dict(change)
    for key in ("to", "from"):
        val = out.get(key)
        if isinstance(val, (dict, list)):
            out[key] = _public_diff_value(val, "")
    return out


def _public_diff_value(value: Any, dotted: str) -> Any:
    """Allowlist a stored route value for a PUBLIC diff. A route dict can
    share its object with protected neighbours (threshold/enforcement) or
    secret-adjacent material (api_key, headers); only the route fields this
    feature owns are ever projected, and any ``base_url`` is URL-sanitized
    (userinfo/query removed). Scalars pass through unchanged."""
    if isinstance(value, dict):
        return {k: _public_route_field(k, v)
                for k, v in value.items() if k in _PUBLIC_ROUTE_KEYS}
    if isinstance(value, list):
        # Ordered chains: project/sanitize each entry; bare provider strings
        # pass through.
        out: List[Any] = []
        for entry in value:
            if isinstance(entry, dict):
                out.append({k: _public_route_field(k, v)
                            for k, v in entry.items() if k in _PUBLIC_ROUTE_KEYS})
            else:
                out.append(entry)
        return out
    if str(dotted).endswith("base_url"):
        return _sanitize_public_url(value)
    return value


def _op_dto(op: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitized operation DTO built on journal.to_public_operation's
    allowlist projection: only status/profile/store-kind per step — NEVER
    journal snapshots, storage paths, changes, or raw config contents."""
    pub = to_public_operation(op) or {}
    steps = pub.get("steps", [])
    undoable = (pub.get("state") == OP_COMMITTED
                and any(s.get("state") == "applied" for s in steps))
    return {
        "id": pub.get("op_id"), "state": pub.get("state"),
        "results": [{
            "profileId": s.get("profile"), "status": s.get("state"),
            "store": s.get("store_kind"),
            # The revision the review was bound to and the revision the write
            # left behind, straight from the journal step (engine.revision_summary
            # carries the same pair). The UI renders rev before → after per
            # result, so a no-change write is visibly a no-op.
            "revisionBefore": _step_revision(s, "revision_before", "pre_revision"),
            "revisionAfter": _step_revision(s, "revision_after", "post_revision"),
        } for s in steps],
        "undoable": undoable,
    }


def _step_revision(step: Dict[str, Any], *keys: str) -> Optional[str]:
    """The pre/post revision of a journal step, tolerant of both the public
    view's step shape and engine.revision_summary's key spelling."""
    for key in keys:
        val = step.get(key)
        if val is not None:
            return val
    return None


# -- endpoints --------------------------------------------------------------
# Route handlers are deliberately PLAIN DEFS, not ``async def``: every handler
# here performs blocking work (strict config reads, canonical saves inside
# Hermes' lock, and — on refresh — live provider probes). A sync handler runs
# in FastAPI's threadpool, so a slow provider or lock never blocks the
# dashboard's event loop for every other request.
@router.get("/state")
def state():
    try:
        with _LOCK:
            profiles = _profile_inventory()
            store = _preset_store()
            presets = []
            for pid, p in getattr(store, "_presets", {}).items():
                # Exact reviewed slot-scope intents: the UI previews a preset
                # from ``preset.changes`` (the typed change list) when present,
                # so the DTO must carry the REAL nested intents
                # ({profileId, slotId, action, to}) — never just a main route.
                changes = _ui_changes_for_preset(p)
                families: List[str] = []
                for c in changes:
                    fam = _ui_family_for_slot(c.get("slotId"))
                    if fam and fam not in families:
                        families.append(fam)
                if not families:
                    families = sorted({_ui_family_for_slot(
                        _slot_id_for_target(p, t)) for t in p.targets})
                # Legacy family-scoped fallback: only a genuine MAIN target may
                # contribute a main assignment; a non-main preset must not
                # masquerade as one.
                assignment = {}
                for t in p.targets:
                    if _slot_id_for_target(p, t) in ("main", "main.default"):
                        assignment = {"main": {
                            "provider": t.changes.get("model.provider"),
                            "model": t.changes.get("model.default"),
                        }}
                        break
                presets.append({
                    "id": pid,
                    "name": p.name,
                    "profileIds": sorted({t.profile for t in p.targets}),
                    "families": families,
                    "changes": [_sanitize_change(c) for c in changes],
                    "assignments": assignment,
                    "billingPolicy": "reviewed",
                })
        return {"ok": True, "profiles": list(profiles.values()),
                "slots": [s for p in profiles.values() for s in p["slots"]],
                "presets": presets,
                "presetsCorrupt": bool(_PRESET_CORRUPT),
                "runtime": {"writable": _runtime_writable(),
                            "version": _ADAPTER.runtime.version_label,
                            "reasons": list(_ADAPTER.runtime.reasons)}}
    except Exception as exc:  # sanitized error surface
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"state unavailable: {type(exc).__name__}"}) from None


@router.get("/models")
def models(response: Response, provider: str = "",
          profileId: Optional[str] = None, refresh: bool = False):
    """Provider-aware model catalog — a thin, truthful passthrough.

    Shape: ``{ok, provider, models:[{id,name}], fetchedAt, source, stale,
    error}``. The result is the catalog module's own dict, returned verbatim;
    ``ok=False`` (with an explanatory ``error``) is a visible failure, never a
    silently empty success. The response is never cached.
    """
    response.headers["Cache-Control"] = "no-store"
    provider = str(provider or "")
    try:
        catalog = _model_catalog_module()
    except Exception:
        return {"ok": False, "provider": provider, "models": [],
                "fetchedAt": None, "source": "unavailable", "stale": True,
                "error": "model catalog module unavailable"}
    # Explicit profile-discovery validation: an unknown profile is a truthful
    # visible error, never a silent empty success.
    if profileId and profileId not in discover_profiles():
        return {"ok": False, "provider": provider, "models": [],
                "fetchedAt": None, "source": "unknown", "stale": True,
                "error": f"unknown profile {profileId!r}"}
    try:
        result = catalog.get_model_catalog(
            _ADAPTER, profileId, provider, refresh=bool(refresh))
    except Exception as exc:
        return {"ok": False, "provider": provider, "models": [],
                "fetchedAt": None, "source": "unknown", "stale": True,
                "error": f"catalog lookup failed: {type(exc).__name__}"}
    if not isinstance(result, dict):
        result = {"ok": False, "provider": provider, "models": [],
                  "fetchedAt": None, "source": "unknown", "stale": True,
                  "error": "catalog returned an unexpected payload"}
    return result


def _watched_store(change: Dict[str, Any], known) -> Optional[Tuple[str, str, str]]:
    """The canonical store a change is prepared from (profile, kind, path)."""
    pid = change.get("profileId")
    if not isinstance(pid, str):
        return None
    ident = known.get(pid)
    if ident is None:
        return None
    return (pid, "canonical_config", str(_config_path_for(pid, ident)))


def _store_revision(watched: Optional[Tuple[str, str, str]]) -> Optional[str]:
    if watched is None:
        return None
    profile, kind, path = watched
    try:
        return adapter_for(kind, path, profile).revision()
    except Exception:
        return None


def _watch_stores(changes: List[Dict[str, Any]], known
                  ) -> Dict[Tuple[str, str, str], Optional[str]]:
    """Revision snapshot of every canonical store the changes touch, taken
    BEFORE preparation, so the caller can pin and verify it afterwards."""
    watched: Dict[Tuple[str, str, str], Optional[str]] = {}
    for c in changes:
        store = _watched_store(c, known)
        if store is not None:
            watched[store] = _store_revision(store)
    return watched


def _revision_drift(watched: Dict[Tuple[str, str, str], Optional[str]]
                    ) -> Optional[str]:
    """Profile whose watched store no longer matches its pre-preparation
    revision, or None when every store still matches.

    FAILS CLOSED: an unreadable/absent revision is treated as drift — an
    unverifiable guard must not silently disable the race check.
    """
    for (profile, kind, path), before in watched.items():
        now = _store_revision((profile, kind, path))
        if before is None or now != before:
            return profile
    return None


@router.post("/preview")
def preview(body: Dict[str, Any]):
    try:
        profiles = _profile_inventory()
        changes = body.get("changes") or []
        duplicates = _duplicate_slot_changes(changes)
        if duplicates:
            # A request repeating one slot is refused as a whole: applying the
            # coalesced subset would silently drop the other intent.
            preview_id = "pv-" + uuid.uuid4().hex[:12]
            with _LOCK:
                _PREVIEW_CACHE[preview_id] = _bind_preview([], profiles,
                                                           duplicates)
                _evict_previews()
            return {"ok": True, "previewId": preview_id,
                    "preview": {"id": preview_id, "changes": [],
                                "blocked": duplicates}}
        # A preview is the SAME prepared mutation an apply would run (one code
        # path: _changes_to_targets) — so the diff shown is exactly what Apply
        # writes, chain edits, base_url/api_mode side effects and canonical
        # main preparation included. No writes happen here.
        out_changes, blocked, prepared = [], [], []
        known = discover_profiles()
        with _LOCK:
            for c in changes:
                # Watch the target's canonical store ACROSS preparation: the
                # prepared mutation (canonical cleanup deletions included) is
                # derived from that document, so a write landing between the
                # read and the bind must abort the preview — never be adopted
                # as the mutation's expected revision.
                watched = _watch_stores([c], known)
                targets, blk = _changes_to_targets([c], profiles, strict=False)
                if blk:
                    blocked.extend(blk)
                    continue
                if not targets:
                    blocked.append({"profileId": c.get("profileId"),
                                    "slotId": c.get("slotId"),
                                    "reason": "change is a no-op for this slot"})
                    continue
                pid = targets[0].profile
                try:
                    cfg = _ADAPTER.load_raw_config(pid)
                except Exception:
                    blocked.append({"profileId": pid,
                                    "reason": "config unreadable"})
                    continue
                diff, side_effects = _diff_for_targets(targets, cfg, c)
                drift = _revision_drift(watched)
                if drift is not None:
                    blocked.append({
                        "profileId": drift,
                        "slotId": c.get("slotId"),
                        "kind": "raced",
                        "reason": ("config changed while the preview was "
                                   "prepared; retry the preview")})
                    continue
                for t in targets:
                    entry = {"profile": t.profile,
                             "store_kind": t.store_kind,
                             "path": t.path, "changes": t.changes}
                    key = (t.profile, t.store_kind, t.path)
                    if watched.get(key) is not None:
                        # Pin the revision the mutation was prepared FROM, not
                        # whatever the store holds at bind time.
                        entry["expected_revision"] = watched[key]
                    prepared.append(entry)
                out_changes.append({**_sanitize_change(c),
                                    "sideEffects": side_effects,
                                    "diff": diff})
        preview_id = "pv-" + uuid.uuid4().hex[:12]
        with _LOCK:
            _PREVIEW_CACHE[preview_id] = _bind_preview(prepared, profiles, blocked)
            _evict_previews()
        return {"ok": True, "previewId": preview_id,
                "preview": {"id": preview_id, "changes": out_changes,
                            "blocked": blocked}}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"preview failed: {type(exc).__name__}"}) from None


# A preview entry is the EXACT prepared mutation an apply will run, bound to
# the store revisions + profile identities it was prepared from, with an
# expiry. It is never the raw request the client sent.
_PREVIEW_CACHE: Dict[str, Dict[str, Any]] = {}


# Canonical main-routing side effects: the seam writes these model.* keys when
# the chosen provider needs a custom endpoint. They are part of the SAME
# prepared mutation (Apply writes them) and disclosed as side effects — the
# route diff keeps the UI contract of showing only the reviewed route fields.
_MAIN_SIDE_EFFECT_KEYS = ("model.base_url", "model.api_mode")


def _diff_for_targets(targets, cfg: Dict[str, Any],
                      change: Dict[str, Any]
                      ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Public, allowlisted diff of the prepared mutation (relative config
    paths only — never raw config fragments or secret neighbours)."""
    diff: List[Dict[str, Any]] = []
    side_effects: List[str] = []
    removal = str(change.get("action") or "").lower() == "remove"
    for t in targets:
        for dotted, value in t.changes.items():
            if dotted in _MAIN_SIDE_EFFECT_KEYS:
                continue
            diff.append({
                "path": dotted,
                "from": _public_diff_value(_observed_value(cfg, dotted), dotted),
                "to": _public_diff_value(value, dotted),
            })
        if not removal:
            if any(k in t.changes for k in _MAIN_SIDE_EFFECT_KEYS):
                side_effects.append("canonical endpoint settings applied")
            if "model.provider" in t.changes:
                # a canonical provider switch seeds tool defaults — disclosed
                # at preview, applied only on Apply.
                side_effects.append("canonical tool defaults seeded")
    diff.sort(key=lambda d: d["path"])
    return diff, side_effects


def _bind_preview(prepared: List[Dict[str, Any]],
                  profiles: Dict[str, Dict[str, Any]],
                  blocked: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Bind a prepared mutation to the store revision + profile identity it
    was prepared from, with an expiry."""
    now = time.time()
    revisions: Dict[Tuple[str, str, str], Any] = {}
    fingerprints: Dict[str, str] = {}
    known = discover_profiles()
    bound_targets: List[Dict[str, Any]] = []
    for t in prepared:
        key = (t["profile"], t["store_kind"], t["path"])
        # Prefer the revision the mutation was PREPARED from (pinned by the
        # caller around its config read); only targets without one fall back
        # to a read here. Re-reading unconditionally would adopt an external
        # write that landed during preparation as the expected revision.
        rev = t.get("expected_revision")
        if rev is None:
            try:
                rev = adapter_for(t["store_kind"], t["path"], t["profile"]).revision()
            except Exception:
                rev = None
        revisions[key] = rev
        ident = known.get(t["profile"])
        if ident is not None:
            fingerprints[t["profile"]] = ident.fingerprint
        bound = dict(t)
        if ident is not None:
            # Carry the CACHED identity so the engine plans against exactly the
            # profile the review saw, and pin the reviewed revision via
            # ``expected_revision`` so a store that moved between verify and
            # plan blocks instead of silently rebasing.
            bound["identity"] = _identity_dict(ident)
            if rev is not None and "expected_revision" not in bound:
                bound["expected_revision"] = rev
        bound_targets.append(bound)
    return {"targets": bound_targets, "revisions": revisions,
            "fingerprints": fingerprints,
            # A preview that contained blocked changes is remembered as such:
            # applying only its valid subset would silently drop the blocked
            # entries, so the apply path refuses it (all-or-nothing review).
            "blocked": list(blocked or []),
            "created_at": now,
            "expires_at": now + _PREVIEW_TTL_SECONDS}


def _identity_dict(ident) -> Dict[str, Any]:
    """Serialized profile identity (name, path, fingerprint) — the keys the
    engine's plan records privately on each step and re-checks at apply."""
    try:
        from dataclasses import asdict
        return asdict(ident)
    except Exception:
        return {"name": getattr(ident, "name", None),
                "path": getattr(ident, "path", None),
                "fingerprint": getattr(ident, "fingerprint", None)}


def _evict_previews() -> None:
    """Callers MUST hold ``_LOCK`` (all current callers do)."""
    if len(_PREVIEW_CACHE) <= _PREVIEW_MAX:
        return
    for k in list(_PREVIEW_CACHE)[:-_PREVIEW_MAX]:
        _PREVIEW_CACHE.pop(k, None)


def _get_preview_entry(preview_id: str) -> Dict[str, Any]:
    """Callers MUST hold ``_LOCK`` (all current callers do); the cache is
    process-global and handlers now run on FastAPI's threadpool."""
    entry = _PREVIEW_CACHE.get(preview_id)
    if entry is None:
        raise HTTPException(status_code=404, detail={
            "ok": False, "error": "unknown or expired preview"})
    if time.time() > entry.get("expires_at", 0):
        _PREVIEW_CACHE.pop(preview_id, None)
        raise HTTPException(status_code=409, detail={
            "ok": False, "blocked": True, "stale": True,
            "error": "preview expired; re-preview before applying"})
    return entry


def _verify_preview_fresh(entry: Dict[str, Any]) -> None:
    """Explicit stale rejection: a preview whose store revision or profile
    identity moved on can never be replayed. No silent re-derivation."""
    reasons: List[str] = []
    known = discover_profiles()
    for profile, fp in entry.get("fingerprints", {}).items():
        cur = known.get(profile)
        if cur is None or cur.fingerprint != fp:
            reasons.append(f"profile {profile!r} identity changed since preview")
    for (profile, kind, path), rev in entry.get("revisions", {}).items():
        try:
            cur = adapter_for(kind, path, profile).revision()
        except Exception:
            cur = None
        if cur != rev:
            reasons.append(
                f"store for profile {profile!r} changed since preview")
    if reasons:
        raise HTTPException(status_code=409, detail={
            "ok": False, "blocked": True, "stale": True,
            "error": ("preview is stale; the configuration changed since "
                      "preview — re-preview before applying"),
            "reasons": sorted(set(reasons))})


@router.post("/presets")
def save_preset(body: PresetIn):
    try:
        with _LOCK:
            profiles = _profile_inventory()
            targets, _blocked = _changes_to_targets(body.changes, profiles)
            store = _preset_store()
            pid = "p-" + uuid.uuid4().hex[:8]
            # Discovery-at-review: remember every profile that existed when
            # the preset was reviewed, so preflight's "new profile" gate flags
            # only profiles discovered AFTER review. An existing but
            # unselected profile must NOT block an alpha-only preset; the
            # apply SCOPE stays exactly the reviewed targets.
            reviewed = sorted(discover_profiles())
            selected = sorted({t.profile for t in targets})
            preset = ReviewedPreset(
                preset_id=pid, name=body.name,
                billing_policy=BillingPolicy(
                    approved_classes=["subscription_included", "metered"],
                    allow_unknown=body.allowUnknownCost),
                targets=targets, reviewed_profile_ids=reviewed)
            # P1 #2: freeze the reviewed change intent WITH its slot identity,
            # so one-click apply restores every target onto its own slot.
            preset.target_slot_ids = build_slot_ids(body.changes, targets)
            preset.ui_changes = [dict(c) for c in body.changes]
            store.save(preset)
            _persist_presets(store)
        return {"ok": True, "preset": {"id": pid, "name": body.name,
                                       "profileIds": selected}}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"save failed: {type(exc).__name__}"}) from None


# -- F1: ReviewedPreset.preflight runs BEFORE a one-click apply -------------
def _profile_has_credentials(profile: str, ident,
                             providers: Optional[List[str]] = None) -> bool:
    """Provider-scoped credential readiness for a profile.

    Empty ``providers`` (a scalar route with no hosted provider, or a removal)
    is satisfied vacuously. Otherwise every referenced provider must be
    explicable — a user-declared custom endpoint, a runtime-authenticated
    provider, or backed by the profile's own credential material. Fail-closed
    on unreadable state; never a blanket "any .env key ⇒ authenticated"."""
    needed = [p for p in (providers or []) if str(p or "").strip()]
    if not needed:
        return True
    try:
        cfg = _ADAPTER.load_raw_config(profile)
    except Exception:
        return False
    for provider in needed:
        try:
            if not _provider_credentials_satisfied(profile, ident, cfg, provider):
                return False
        except Exception:
            return False
    return True


def _live_cost_class(t: RouteTarget) -> str:
    """The live cost class of a review-time route, re-derived PER provider.
    A frozen unclassifiable class (e.g. 'unknown') passes through verbatim so
    the preset's billing policy — never a silent free pass — gates it."""
    if t.cost_class not in ("subscription_included", "metered"):
        return t.cost_class  # e.g. 'unknown': unknown cost is never treated as free
    return _route_cost_class(t.profile, t.changes)


def _preset_preflight(preset: ReviewedPreset,
                      allow_unknown: bool = False) -> List[str]:
    profiles = discover_profiles()
    # Apply cannot broaden a saved policy. Save a newly reviewed preset to
    # approve unknown costs; request acknowledgements are for custom reviews.
    # Credentials are checked PER PROVIDER: every provider a profile's preset
    # targets route through (chain hops included) must be explicable for that
    # profile — not just "the profile has some .env".
    providers_by_profile: Dict[str, List[str]] = {}
    for t in preset.targets:
        bucket = providers_by_profile.setdefault(t.profile, [])
        for p in _referenced_providers(t.changes):
            if p not in bucket:
                bucket.append(p)
    credentials = {}
    for name, ident in profiles.items():
        try:
            credentials[name] = _profile_has_credentials(
                name, ident, providers_by_profile.get(name, []))
        except Exception:
            credentials[name] = False  # fail closed, never fabricate
    live_cost_classes = {_route_key(t): _live_cost_class(t) for t in preset.targets}
    return preset.preflight(credentials=credentials,
                            live_cost_classes=live_cost_classes,
                            discovered_profiles=sorted(profiles))


def _preset_target_dicts(preset: ReviewedPreset,
                         profiles: Dict[str, Dict[str, Any]]
                         ) -> List[Dict[str, Any]]:
    """The preset's reviewed changes re-prepared against the CURRENT documents.

    Derived mutations (canonical main cleanup deletions, exact-removal
    deletions, no-op drift) are REGENERATED from the reviewed typed intents
    (``ui_changes``) at apply time — the same code path an interactive
    preview+apply uses — so a frozen deletion can never fire against a
    document it was not reviewed against. A preset whose reviewed changes no
    longer apply cleanly (identity drift, no-op, protected slot) blocks
    wholesale with the reviewed reasons. Legacy records without reviewed
    intents fall back to their frozen target changes.
    """
    known = discover_profiles()
    ui = getattr(preset, "ui_changes", None)
    if isinstance(ui, list) and ui:
        watched = _watch_stores(ui, known)
        derived, blocked = _changes_to_targets(copy.deepcopy(ui), profiles,
                                               strict=False)
        if blocked:
            raise HTTPException(status_code=409, detail={
                "ok": False, "blocked": True,
                "error": "apply blocked: the preset no longer applies cleanly",
                "reasons": [str(b.get("reason") or "blocked change")
                            for b in blocked]})
        drift = _revision_drift(watched)
        if drift is not None:
            raise HTTPException(status_code=409, detail={
                "ok": False, "blocked": True, "stale": True,
                "error": ("apply blocked: the configuration changed while the "
                          "preset was prepared; retry the apply"),
                "reasons": [f"store for profile {drift!r} changed during "
                            "preparation"]})
        out: List[Dict[str, Any]] = []
        for t in derived:
            ident = known.get(t.profile)
            path = _config_path_for(t.profile, ident) if ident is not None else t.path
            target = {"profile": t.profile, "store_kind": t.store_kind,
                      "path": str(path), "changes": copy.deepcopy(t.changes)}
            if ident is not None:
                target["identity"] = _identity_dict(ident)
            key = (t.profile, t.store_kind, str(path))
            if watched.get(key) is not None:
                target["expected_revision"] = watched[key]
            out.append(target)
        return out
    out = []
    for t in preset.targets:
        ident = known.get(t.profile)
        path = _config_path_for(t.profile, ident) if ident is not None else t.path
        target = {"profile": t.profile, "store_kind": t.store_kind,
                  "path": str(path), "changes": copy.deepcopy(t.changes)}
        # Bind the discovered identity so the engine plans against exactly the
        # profile the preset was reviewed for (no re-derivation at plan time).
        if ident is not None:
            target["identity"] = _identity_dict(ident)
        out.append(target)
    return out


def _same_prepared_scope(a: List[Dict[str, Any]],
                         b: List[Dict[str, Any]]) -> bool:
    """Exact scope + typed-INTENT match between two prepared target sets.

    Only the canonical scope (profile / store_kind / path) and the reviewed
    route changes are compared. Two kinds of key are deliberately excluded
    because they are NOT reviewed intent:

    * engine GUARD metadata — ``identity`` / ``expected_revision`` — which a
      cached preview target carries but a freshly rebuilt preset target does
      not; and
    * canonical MAIN side effects — ``model.base_url`` / ``model.api_mode`` —
      which the canonical seam derives from the provider (they are excluded
      from the public diff too), so a preview target and its frozen preset
      target legitimately differ on them.
    """
    def norm(ts):
        return sorted(
            (t["profile"], t["store_kind"], t["path"],
             tuple(sorted(
                 (k, json.dumps(v, sort_keys=True, default=str))
                 for k, v in (t.get("changes") or {}).items()
                 if k not in _MAIN_SIDE_EFFECT_KEYS)))
            for t in ts)
    return norm(a) == norm(b)


def _unchanged_provider_hops(profile: str,
                             changes: Dict[str, Any]) -> List[str]:
    """Providers of scalar route assignments whose companion provider field is
    NOT part of the write map (same-provider model edits).

    A model-only edit omits the unchanged ``X.provider`` companion, so the
    credential gate would otherwise be skipped. The provider is resolved from
    the CURRENT document through the slot's OWN descriptor companions — never
    a generic path guess (``cron.provider`` is the scheduler, not a model
    provider). Returns [] for deletions/unset/disable (no route is written)
    and for slots without a provider companion.
    """
    if not isinstance(changes, dict) or not changes:
        return []
    try:
        cfg = _ADAPTER.load_raw_config(profile)
        inventory = build_inventory(cfg)
    except Exception:
        return []
    out: List[str] = []
    for dotted, value in changes.items():
        if not isinstance(value, str) or not value.strip():
            continue  # deletion / unset / disable writes no route
        for obs in inventory:
            desc = obs.descriptor
            if desc.storage_path != dotted:
                continue
            found_any = False
            for comp in desc.companion_paths:
                if not comp.endswith((".provider", "_provider")):
                    continue
                found = _get_dotted(cfg, comp)
                if isinstance(found, str) and found.strip() and found not in out:
                    out.append(found)
                if isinstance(found, str) and found.strip():
                    found_any = True
            if not found_any and desc.inherit_provider_from:
                # The route INHERITS its provider (e.g. delegation.model
                # with no delegation.provider inherits model.provider):
                # the gate must follow the inheritance, or an edit routing
                # through an unauthenticated hosted parent would pass.
                base = _get_dotted(cfg, desc.inherit_provider_from)
                if isinstance(base, str) and base.strip() and base not in out:
                    out.append(base)
            break
    return out


def _ensure_prepared_credentials(prepared: List[Dict[str, Any]]) -> None:
    """Provider-scoped credential gate for prepared write targets.

    Every provider a target routes through (chain hops and route-dict values
    included) must be explicable for that target's profile — never a blanket
    "profile has some .env". Raises the 409 apply-block contract when any
    profile cannot authenticate a referenced provider.
    """
    known = discover_profiles()
    missing: List[str] = []
    for t in prepared:
        changes = t.get("changes") or {}
        needed = _referenced_providers(changes)
        profile = t.get("profile")
        if not needed and profile:
            # Same-provider model edits omit the unchanged provider field.
            needed = _unchanged_provider_hops(profile, changes)
        if not needed:
            continue
        ident = known.get(profile)
        if ident is None or not _profile_has_credentials(profile, ident, needed):
            missing.append(str(profile))
    if missing:
        raise HTTPException(status_code=409, detail={
            "ok": False, "blocked": True,
            "error": "apply blocked: missing provider credentials",
            "reasons": [f"profile {p!r} is missing provider credentials"
                        for p in sorted(set(missing))]})


def _resolve_apply(body: ApplyIn, profiles: Dict[str, Dict[str, Any]]
                   ) -> List[Dict[str, Any]]:
    """Resolve the EXACT prepared write targets for an apply.

    Precedence is explicit and never silently drops a supplied id:
      * a previewId binds the apply to the EXACT prepared mutation that was
        reviewed (revision/identity checked, expiry enforced);
      * a presetId binds it to the preset's frozen reviewed scope (preflight
        gated; an explicit allowUnknownCost acknowledgement is honored);
      * when BOTH are present they must describe the same scope — an exact
        match, otherwise the apply is rejected rather than the preview being
        ignored.
    """
    prepared: Optional[List[Dict[str, Any]]] = None
    if body.previewId:
        entry = _get_preview_entry(body.previewId)
        _verify_preview_fresh(entry)
        if entry.get("blocked"):
            # All-or-nothing review: the preview refused part of the requested
            # change-set; replaying only its valid subset would silently drop
            # the blocked entries, so the apply is refused with the reviewed
            # reasons until the user fixes them and re-previews.
            raise HTTPException(status_code=409, detail={
                "ok": False, "blocked": True,
                "error": "apply blocked: the preview contained blocked changes",
                "reasons": [str(b.get("reason") or "blocked change")
                            for b in entry["blocked"]]})
        prepared = copy.deepcopy(entry["targets"])
    if body.presetId:
        store = _preset_store()
        try:
            preset = store.get(body.presetId)
        except Exception:
            raise HTTPException(status_code=404, detail={
                "ok": False, "error": "unknown preset"}) from None
        # P1 #1: preflight gates EVERY one-click apply — missing credentials,
        # drifted/unknown cost classes and newly discovered profiles block
        # before any write, never silently substituted.
        reasons = _preset_preflight(
            preset, allow_unknown=bool(body.allowUnknownCost))
        if reasons:
            raise HTTPException(status_code=409, detail={
                "ok": False, "blocked": True,
                "error": "apply blocked: preset preflight refused",
                "reasons": reasons})
        preset_targets = _preset_target_dicts(preset, profiles)
        if prepared is not None:
            if not _same_prepared_scope(prepared, preset_targets):
                raise HTTPException(status_code=409, detail={
                    "ok": False, "blocked": True, "stale": True,
                    "error": ("preview does not match the reviewed preset "
                              "scope; re-preview the preset before applying")})
        else:
            prepared = preset_targets
    if prepared is None:
        if not body.changes:
            raise HTTPException(status_code=422, detail={
                "ok": False, "error": "no changes to apply"})
        known = discover_profiles()
        watched = _watch_stores(body.changes, known)
        targets, blocked = _changes_to_targets(body.changes, profiles,
                                               strict=False)
        if blocked:
            if all(b.get("kind") == "route_refusal" for b in blocked):
                # 409 apply contract: the canonical seam refused the route —
                # the apply block (no write, no journal row) with the blocked
                # flag set; never a blind overwrite.
                raise HTTPException(status_code=409, detail={
                    "ok": False, "blocked": True,
                    "error": "apply blocked: the canonical route was rejected",
                    "reasons": [b.get("reason", "") for b in blocked]})
            # Request-binding errors (unknown slot/profile, protected slot,
            # ...) keep the visible 422 block contract.
            raise HTTPException(status_code=422, detail={
                "ok": False, "error": "preview blocked for some profiles",
                "blocked": blocked})
        prepared = [{"profile": t.profile, "store_kind": t.store_kind,
                     "path": t.path, "changes": t.changes} for t in targets]
        # Direct applies are revision-pinned exactly like previews: the
        # prepared mutation (canonical cleanup deletions included) is derived
        # from these documents, so a write landing between preparation and
        # planning must block instead of being written onto.
        drift = _revision_drift(watched)
        if drift is not None:
            raise HTTPException(status_code=409, detail={
                "ok": False, "blocked": True, "stale": True,
                "error": ("apply blocked: the configuration changed while it "
                          "was prepared; retry the apply"),
                "reasons": [f"store for profile {drift!r} changed during "
                            "preparation"]})
        for t in prepared:
            key = (t["profile"], t["store_kind"], t["path"])
            if watched.get(key) is not None:
                t["expected_revision"] = watched[key]
    if not prepared:
        raise HTTPException(status_code=422, detail={
            "ok": False, "error": "no valid write targets"})
    # Provider-scoped credential gate for EVERY resolution path. A cached
    # preview can outlive the credential it was prepared under (or be replayed
    # by a client that never held one), so preview-based applies are gated
    # exactly like direct ones. Removals/unset carry no provider and are not
    # gated.
    _ensure_prepared_credentials(prepared)
    return prepared


@router.post("/operations")
def apply(body: ApplyIn):
    if not _runtime_writable():
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": "runtime is read-only; writes refused",
            "reasons": list(_ADAPTER.runtime.reasons)})
    try:
        with _LOCK:
            profiles = _profile_inventory()
            prepared = _resolve_apply(body, profiles)
            journal = _journal()
            try:
                op = plan_operation(journal, prepared)
                outcome = apply_operation(journal, op["op_id"])
                return {"ok": True,
                        "operation": _op_dto(journal.get(outcome["op_id"]))}
            finally:
                journal.close()
    except HTTPException:
        raise
    except ApplyBlocked as exc:
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": f"apply blocked: {exc}"}) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"apply failed: {type(exc).__name__}"}) from None


@router.get("/operations/{op_id}")
def get_operation(op_id: str):
    try:
        journal = _journal()
        try:
            op = journal.get(op_id)
            if op is None:
                raise HTTPException(status_code=404, detail={
                    "ok": False, "error": "unknown operation"})
            # P1 #6: crash-recovery contract — a pending/applying/partial
            # record is reconciled against the actual files before it is
            # surfaced; the written configuration never stays ambiguous
            # indefinitely and recovery_required is reported verbatim.
            if op["state"] in (OP_PENDING, OP_APPLYING, OP_PARTIAL):
                op = reconcile(journal, op_id)
            return {"ok": True, "operation": _op_dto(op)}
        finally:
            journal.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"read failed: {type(exc).__name__}"}) from None


@router.post("/operations/{op_id}/recover")
def recover_operation(op_id: str):
    """Explicit restart path: reconcile one operation's persisted intent
    against the actual files (committed/compensated/recovery_required)."""
    try:
        journal = _journal()
        try:
            op = journal.get(op_id)
            if op is None:
                raise HTTPException(status_code=404, detail={
                    "ok": False, "error": "unknown operation"})
            op = reconcile(journal, op_id)
            return {"ok": True, "operation": _op_dto(op)}
        finally:
            journal.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"recover failed: {type(exc).__name__}"}) from None


@router.post("/operations/{op_id}/undo")
def undo(op_id: str):
    if not _runtime_writable():
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": "runtime is read-only; writes refused"})
    try:
        with _LOCK:
            journal = _journal()
            try:
                undo_operation(journal, op_id)
                return {"ok": True,
                        "operation": _op_dto(journal.get(op_id))}
            finally:
                journal.close()
    except ApplyBlocked as exc:
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": f"undo blocked: {exc}"}) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"undo failed: {type(exc).__name__}"}) from None


@router.post("/probe")
def probe(body: ProbeIn):
    """New-session probe — EXPLICIT AUTHORIZATION REQUIRED.

    A probe starts a NEW session against the selected provider/model and
    therefore incurs real, possibly metered cost. It runs ONLY when the
    request carries authorized=true AND a cost approval naming the exact
    route. Without that, the probe is BLOCKED — it never fabricates usage
    rows, never fakes a session_model_usage record, and never performs an
    unapproved paid call.
    """
    if not body.authorized:
        raise HTTPException(status_code=403, detail={
            "ok": False, "blocked": True,
            "error": ("probe blocked: no explicit authorization — a new "
                      "session would incur real cost on "
                      f"{body.provider}/{body.model}")})
    approval = body.cost_approval or {}
    if approval.get("provider") != body.provider or approval.get("model") != body.model:
        raise HTTPException(status_code=403, detail={
            "ok": False, "blocked": True,
            "error": "probe blocked: cost approval does not match the requested route"})
    # Authorization is present, but actually starting a paid session from a
    # dashboard plugin needs a live provider credential and an interactive
    # approval surface — reported as blocked in this environment rather than
    # silently skipped or fabricated.
    raise HTTPException(status_code=403, detail={
        "ok": False, "blocked": True,
        "error": ("probe blocked: authorized but not performable here — "
                  "no live provider session runner in the plugin sandbox; "
                  "run the authorized probe via the CLI, then inspect "
                  "session_model_usage")})
