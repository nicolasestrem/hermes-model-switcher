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

# Import bootstrap so the packaged layout (<plugin>/python/…) and the repo
# checkout both resolve. In the packaged plugin, plugin_api.py lives at
# <plugin>/dashboard/plugin_api.py and the Python modules under <plugin>/python/.
_PKG_ROOT = Path(__file__).resolve().parent.parent
for _p in (_PKG_ROOT / "python", _PKG_ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import copy
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_adapter import HermesAdapter
from hermes_adapter import discover_profiles
from route_inventory import RouteEdit, SlotFamily, build_inventory, preview_changes

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
    _reconcile_unfinished_on_restart(journal)
    return journal


# Crash-recovery restart path (P1 #6): a crash between a config write and the
# journalled step update leaves operations pending/applying/partial. The first
# journal use after a (re)start reconciles every unfinished operation against
# the actual files, so the documented crash-recovery contract holds even when
# no one ever clicks a specific operation again.
_STARTUP_RECONCILED = False


def _reconcile_unfinished(journal: OperationJournal) -> List[Dict[str, Any]]:
    """Run engine.reconcile over every unfinished operation. Returns the
    reconciled records; a reconcile failure surfaces to the caller."""
    reconciled = []
    for op in journal.unfinished_operations():
        reconciled.append(reconcile(journal, op["op_id"]))
    return reconciled


def _reconcile_unfinished_on_restart(journal: OperationJournal) -> None:
    global _STARTUP_RECONCILED
    if _STARTUP_RECONCILED:
        return
    _STARTUP_RECONCILED = True
    if journal.unfinished_operations():
        _reconcile_unfinished(journal)


def _preset_store() -> PresetStore:
    store = PresetStore()
    path = _state_dir() / "presets.json"
    if path.exists():
        try:
            records = json.loads(path.read_text())
        except Exception:
            records = []
        for rec in records:
            try:
                preset = _preset_from_record(rec)
            except Exception:
                continue
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
    if "fallback.providers" in keys:
        return "fallback.main_chain"
    if "moa.default_preset" in keys:
        return "moa.default_preset"
    for key in sorted(keys):
        if key.startswith("moa.presets."):
            return f"moa.preset.{key.rsplit('.', 1)[-1]}"
    if any(k.startswith("auxiliary.goal_judge") for k in keys):
        return "judge.goal_judge.route"
    aux = {k for k in keys if k.startswith("auxiliary.")}
    if aux:
        slot = sorted(aux)[0].split(".")[1]
        return f"auxiliary.{slot}"
    if any(k.startswith("delegation.") for k in keys):
        return "delegation.model"
    if "speech.model_id" in keys:
        return "specialist.stt"
    if "speech.streaming_model" in keys:
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
                                      "fallback.providers"}[slot_raw])}
    if slot_raw == "moa.default_preset":
        return {"provider": "", "model": changes.get("moa.default_preset")}
    if slot_raw.startswith("moa.preset."):
        return {"provider": path("provider"), "model": path("model")}
    if slot_raw.startswith("auxiliary.") or slot_raw.startswith("judge."):
        return {"provider": path("provider"), "model": path("model")}
    return {"provider": "", "model": changes.get(slot_raw)}


# -- request models ---------------------------------------------------------
class PresetIn(BaseModel):
    name: str
    changes: List[Dict[str, Any]]
    billingPolicy: str = "reviewed"


class ApplyIn(BaseModel):
    presetId: Optional[str] = None
    previewId: Optional[str] = None
    changes: Optional[List[Dict[str, Any]]] = None


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


def _config_path_for(profile: str, ident) -> Path:
    """A profile's config.yaml, bound to the DISCOVERED profile directory."""
    return Path(ident.path) / "config.yaml"


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
            effective = obs.effective
            eff = effective if isinstance(effective, dict) else {}
            # UI contract: the main route presents as slotId "main"; the raw
            # inventory slot is "main.default" (model) with provider as a
            # companion write (model.provider).
            slot_id = "main" if d.slot_id == "main.default" else d.slot_id
            provider = eff.get("provider") if eff else (
                _get_dotted(cfg, "model.provider")
                if d.slot_id == "main.default" else None)
            model = eff.get("model") if eff else (
                obs.effective if d.slot_id == "main.default" else None)
            slots.append({
                "profileId": name, "slotId": slot_id,
                "family": getattr(d.family, "value", str(d.family)),
                "provider": provider, "model": model,
                "state": obs.source,
                "writable": _runtime_writable() and d.writable and not d.protected,
                "storagePath": d.storage_path,
                "lastObserved": {"provider": provider, "model": model},
                "protocol": d.protocol,
            })
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

    Note: the seam's stale-credential cleanup is expressed as key REMOVALS,
    which the engine's readback-checked canonical adapter cannot verify
    (None = "absent"); only value-carrying side effects are bounded here.
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
            continue  # removal-shaped side effects are not representable here
        if old_block.get(key) != new_val:
            changes[f"model.{key}"] = new_val
    if not changes:
        raise ValueError("change is a no-op for this slot")
    return changes


def _removal_changes_for(desc, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """P1 #3: an exact-removal edit binds as an UNSET of this slot's own
    keys — per slot semantics, never a null/"None" route assignment:
      - route-dict slots (judge/aux/specialist households): only the route
        fields (provider/model/base_url/api_mode) inside that dict are
        unset — protected neighbours beside them survive verbatim;
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
        name = desc.storage_path.rsplit(".", 1)[-1]
        default_preset = (cfg.get("moa") or {}).get("default_preset")
        if default_preset == name:
            raise ValueError(
                f"cannot remove preset {name!r} while moa.default_preset "
                "still references it (invalid MoA removal rejected)")
    changes: Dict[str, Any] = {}
    current = _get_dotted(cfg, desc.storage_path)
    is_dict = isinstance(current, dict)
    if is_dict:
        for key in _ROUTE_FILTER_KEYS:
            if key in current:
                changes[f"{desc.storage_path}.{key}"] = None
    if not is_dict or not changes:
        changes[desc.storage_path] = None
        for comp in desc.companion_paths:
            if desc.family is SlotFamily.CRON and comp.endswith("model_provider"):
                # cron.model always pairs with cron.model_provider (inference
                # provider, mirroring route_inventory's preview companion
                # rule); the absent pair member is unset too. cron.provider
                # (scheduler) is not a companion and is never touched.
                changes[comp] = None
            elif _get_dotted(cfg, comp) is not _STORE_MISSING:
                changes[comp] = None
    if not changes:
        raise ValueError("change is a no-op for this slot")
    return changes


def _changes_to_targets(changes: List[Dict[str, Any]],
                        profiles: Dict[str, Dict[str, Any]],
                        strict: bool = True):
    """Validate + bind changes to explicit discovered-profile config paths.
    Returns (targets, blocked). A change for an unknown or non-writable
    profile, or a slot not present in that profile's inventory, is blocked —
    never silently substituted."""
    targets, blocked = [], []
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
                stored = _removal_changes_for(desc, cfg)
            except ValueError as exc:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": str(exc)})
                continue
            cost_provider = _provider_from_changes(stored)
            targets.append(RouteTarget(
                profile=str(pid), store_kind="canonical_config", path=str(cfg_path),
                changes=stored,
                cost_class="subscription_included"
                if cost_provider == "anthropic" else "metered"))
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
                cp = preview_changes(cfg, [RouteEdit(slot_raw, to or {})])
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
        _provider_note = stored.get("model.provider") or (
            to.get("provider") if to else None)
        targets.append(RouteTarget(
            profile=str(pid), store_kind="canonical_config", path=str(cfg_path),
            changes=stored,
            cost_class="metered" if _provider_note != "anthropic"
            else "subscription_included"))
    if strict and blocked:
        raise HTTPException(status_code=422, detail={
            "ok": False, "error": "preview blocked for some profiles",
            "blocked": blocked})
    return targets, blocked


def _provider_from_changes(stored: Dict[str, Any]) -> str:
    for dotted, value in stored.items():
        if dotted.endswith(".provider") and isinstance(value, str):
            return value
    return ""


def _clean_detail(exc: HTTPException) -> str:
    detail = getattr(exc, "detail", "")
    text = detail if isinstance(detail, str) else str(detail)
    return (text or "route rejected")[:200]


def _observed_value(cfg: Dict[str, Any], dotted: str) -> Any:
    value = _get_dotted(cfg, dotted)
    return None if value is _STORE_MISSING else value


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
        } for s in steps],
        "undoable": undoable,
    }


# -- endpoints --------------------------------------------------------------
@router.get("/state")
async def state():
    try:
        with _LOCK:
            profiles = _profile_inventory()
            store = _preset_store()
            presets = []
            for pid, p in getattr(store, "_presets", {}).items():
                first = p.targets[0] if p.targets else None
                assignment = {}
                if first is not None:
                    # UI contract: per-family assignment map (main → route).
                    assignment = {"main": {
                        "provider": first.changes.get("model.provider"),
                        "model": first.changes.get("model.default"),
                    }}
                presets.append({
                    "id": pid,
                    "name": p.name,
                    "profileIds": sorted({t.profile for t in p.targets}),
                    "families": ["main"],
                    "assignments": assignment,
                    "billingPolicy": "reviewed",
                })
        return {"ok": True, "profiles": list(profiles.values()),
                "slots": [s for p in profiles.values() for s in p["slots"]],
                "presets": presets,
                "runtime": {"writable": _runtime_writable(),
                            "version": _ADAPTER.runtime.version_label,
                            "reasons": list(_ADAPTER.runtime.reasons)}}
    except Exception as exc:  # sanitized error surface
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"state unavailable: {type(exc).__name__}"}) from None


@router.post("/preview")
async def preview(body: Dict[str, Any]):
    try:
        profiles = _profile_inventory()
        changes = body.get("changes") or []
        # Pure preview over the raw configs; no writes anywhere. The response
        # echoes the REVIEWED change-set in the UI's shape (plus side effects
        # and blocks derived from the typed route diff) — never the internal
        # storage-path diff or raw config fragments.
        out_changes, blocked = [], []
        with _LOCK:
            for c in changes:
                pid = c.get("profileId")
                ident = discover_profiles().get(pid)
                if ident is None:
                    blocked.append({"profileId": pid, "reason": "unknown profile"})
                    continue
                cfg = _ADAPTER.load_raw_config(pid)
                slot_id = _slot_alias(c.get("slotId"))
                if str(c.get("action") or "").lower() == "remove":
                    # P1 #3: the exact-removal UI sends to:null — preview it as
                    # the slot-aware UNSET it is (not a null-route assignment).
                    desc = next((o.descriptor for o in build_inventory(cfg)
                                 if o.descriptor.slot_id == slot_id), None)
                    if desc is None:
                        blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                        "reason": f"unknown slot {c.get('slotId')!r}"})
                        continue
                    try:
                        removal = _removal_changes_for(desc, cfg)
                    except ValueError as exc:
                        blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                        "reason": str(exc)})
                        continue
                    out_changes.append({
                        **c, "sideEffects": [],
                        "diff": [{"path": dotted, "from": _observed_value(cfg, dotted),
                                  "to": None}
                                 for dotted in sorted(removal)]})
                    continue
                to = c.get("to") or {}
                edit = RouteEdit(slot_id=slot_id,
                                 value={"provider": to.get("provider"),
                                        "model": to.get("model")})
                cp = preview_changes(cfg, [edit])
                if cp.rejected:
                    blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                    "reason": "; ".join(cp.rejected)})
                    continue
                side_effects = []
                for ch in cp.changes:
                    if ch.storage_path == "model.provider" and ch.before != ch.after:
                        # canonical provider switch seeds tool defaults —
                        # disclosed here, applied only on Apply.
                        side_effects.append("canonical tool defaults seeded")
                out_changes.append({**c, "sideEffects": side_effects,
                                    # per-change typed storage-path diff
                                    # (relative config paths only)
                                    "diff": [{"path": ch.storage_path,
                                              "from": ch.before, "to": ch.after}
                                             for ch in cp.changes]})
        preview_id = "pv-" + uuid.uuid4().hex[:12]
        with _LOCK:
            _PREVIEW_CACHE[preview_id] = copy.deepcopy(changes)
            if len(_PREVIEW_CACHE) > 32:
                for k in list(_PREVIEW_CACHE)[:-32]:
                    _PREVIEW_CACHE.pop(k, None)
        return {"ok": True, "previewId": preview_id,
                "preview": {"id": preview_id, "changes": out_changes,
                            "blocked": blocked}}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"preview failed: {type(exc).__name__}"}) from None


_PREVIEW_CACHE: Dict[str, List[Dict[str, Any]]] = {}


@router.post("/presets")
async def save_preset(body: PresetIn):
    try:
        with _LOCK:
            profiles = _profile_inventory()
            targets, _blocked = _changes_to_targets(body.changes, profiles)
            store = _preset_store()
            pid = "p-" + uuid.uuid4().hex[:8]
            reviewed = sorted({t.profile for t in targets})
            preset = ReviewedPreset(
                preset_id=pid, name=body.name,
                billing_policy=BillingPolicy(
                    approved_classes=["subscription_included", "metered"]),
                targets=targets, reviewed_profile_ids=reviewed)
            # P1 #2: freeze the reviewed change intent WITH its slot identity,
            # so one-click apply restores every target onto its own slot.
            preset.target_slot_ids = build_slot_ids(body.changes, targets)
            preset.ui_changes = [dict(c) for c in body.changes]
            store.save(preset)
            _persist_presets(store)
        return {"ok": True, "preset": {"id": pid, "name": body.name,
                                       "profileIds": reviewed}}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"save failed: {type(exc).__name__}"}) from None


# -- F1: ReviewedPreset.preflight runs BEFORE a one-click apply -------------
def _profile_has_credentials(profile: str, ident) -> bool:
    """Presence-only credential check (never reads values into responses):
    a discovered profile is credit-bearing when its .env carries at least
    one non-empty, non-comment entry. Fail-closed on any unreadable state."""
    env_file = Path(ident.path) / ".env"
    try:
        if not env_file.is_file():
            return False
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for sep in ("=", ":"):
            key, _, value = stripped.partition(sep)
            if key.strip() and value.strip():
                return True
    return False


def _live_cost_class(t: RouteTarget) -> str:
    """The live cost class of a review-time route, derived the same way the
    review did (provider-based) — but only for classifiable route values.
    A drifted catalog feeds the fresh classification into preflight; an
    unclassifiable frozen class (e.g. 'unknown') passes through verbatim so
    the preset's billing policy — never a silent free pass — gates it."""
    if t.cost_class not in ("subscription_included", "metered"):
        return t.cost_class  # e.g. 'unknown': unknown cost is never treated as free
    provider = _provider_from_changes(t.changes)
    return "subscription_included" if provider == "anthropic" else "metered"


def _preset_preflight(preset: ReviewedPreset) -> List[str]:
    profiles = discover_profiles()
    credentials = {}
    for name, ident in profiles.items():
        try:
            credentials[name] = _profile_has_credentials(name, ident)
        except Exception:
            credentials[name] = False  # fail closed, never fabricate
    live_cost_classes = {_route_key(t): _live_cost_class(t) for t in preset.targets}
    return preset.preflight(credentials=credentials,
                            live_cost_classes=live_cost_classes,
                            discovered_profiles=sorted(profiles))


def _resolve_changes(body: ApplyIn) -> List[Dict[str, Any]]:
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
        reasons = _preset_preflight(preset)
        if reasons:
            raise HTTPException(status_code=409, detail={
                "ok": False, "blocked": True,
                "error": "apply blocked: preset preflight refused",
                "reasons": reasons})
        # P1 #2: the preset's own reviewed intent (real slots, typed values)
        # is authoritative — never re-derive a main-only route list.
        ui_changes = getattr(preset, "ui_changes", None)
        if isinstance(ui_changes, list) and ui_changes:
            return [dict(c) for c in ui_changes]
        return _ui_changes_from_targets(preset)
    if body.previewId and body.previewId in _PREVIEW_CACHE:
        return _PREVIEW_CACHE[body.previewId]
    if body.changes:
        return body.changes
    raise HTTPException(status_code=422, detail={
        "ok": False, "error": "no changes to apply"})


@router.post("/operations")
async def apply(body: ApplyIn):
    if not _runtime_writable():
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": "runtime is read-only; writes refused",
            "reasons": list(_ADAPTER.runtime.reasons)})
    try:
        with _LOCK:
            profiles = _profile_inventory()
            changes = _resolve_changes(body)
            targets, blocked = _changes_to_targets(changes, profiles, strict=False)
            if blocked:
                if all(b.get("kind") == "route_refusal" for b in blocked):
                    # 409 apply contract: the canonical seam refused the
                    # route — the apply block (no write, no journal row)
                    # with the blocked flag set; never a blind overwrite.
                    raise HTTPException(status_code=409, detail={
                        "ok": False, "blocked": True,
                        "error": "apply blocked: the canonical route was rejected",
                        "reasons": [b.get("reason", "") for b in blocked]})
                # Request-binding errors (unknown slot/profile, protected
                # slot, ...) keep the visible 422 block contract.
                raise HTTPException(status_code=422, detail={
                    "ok": False, "error": "preview blocked for some profiles",
                    "blocked": blocked})
            if not targets:
                raise HTTPException(status_code=422, detail={
                    "ok": False, "error": "no valid write targets"})
            journal = _journal()
            op = plan_operation(journal, [
                {"profile": t.profile, "store_kind": t.store_kind,
                 "path": t.path, "changes": t.changes} for t in targets])
            outcome = apply_operation(journal, op["op_id"])
            return {"ok": True, "operation": _op_dto(journal.get(outcome["op_id"]))}
    except HTTPException:
        raise
    except ApplyBlocked as exc:
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": f"apply blocked: {exc}"}) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"apply failed: {type(exc).__name__}"}) from None


@router.get("/operations/{op_id}")
async def get_operation(op_id: str):
    try:
        journal = _journal()
        op = journal.get(op_id)
        if op is None:
            raise HTTPException(status_code=404, detail={
                "ok": False, "error": "unknown operation"})
        # P1 #6: crash-recovery contract — a pending/applying/partial record
        # is reconciled against the actual files before it is surfaced; the
        # written configuration never stays ambiguous indefinitely and
        # recovery_required is reported verbatim through the operation state.
        if op["state"] in (OP_PENDING, OP_APPLYING, OP_PARTIAL):
            op = reconcile(journal, op_id)
        return {"ok": True, "operation": _op_dto(op)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"read failed: {type(exc).__name__}"}) from None


@router.post("/operations/{op_id}/recover")
async def recover_operation(op_id: str):
    """Explicit restart path: reconcile one operation's persisted intent
    against the actual files (committed/compensated/recovery_required)."""
    try:
        journal = _journal()
        op = journal.get(op_id)
        if op is None:
            raise HTTPException(status_code=404, detail={
                "ok": False, "error": "unknown operation"})
        op = reconcile(journal, op_id)
        return {"ok": True, "operation": _op_dto(op)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"recover failed: {type(exc).__name__}"}) from None


@router.post("/operations/{op_id}/undo")
async def undo(op_id: str):
    if not _runtime_writable():
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": "runtime is read-only; writes refused"})
    try:
        with _LOCK:
            journal = _journal()
            res = undo_operation(journal, op_id)
            return {"ok": True, "operation": _op_dto(journal.get(op_id))}
    except ApplyBlocked as exc:
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": f"undo blocked: {exc}"}) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"undo failed: {type(exc).__name__}"}) from None


@router.post("/probe")
async def probe(body: ProbeIn):
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
