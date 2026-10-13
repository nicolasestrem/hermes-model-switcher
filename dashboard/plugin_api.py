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
from route_inventory import RouteEdit, build_inventory, preview_changes

from model_switcher.adapters import YamlFileStoreAdapter, _get_dotted
from model_switcher.engine import (
    ApplyBlocked,
    apply_operation,
    plan_operation,
    undo_operation,
)
from model_switcher.journal import OP_COMMITTED, OperationJournal, to_public_operation
from model_switcher.presets import (
    BillingPolicy,
    PresetStore,
    ReviewedPreset,
    RouteTarget,
)

router = APIRouter()

# -- process-local state ----------------------------------------------------
# The journal + preset store persist under the CURRENT Hermes home's plugin
# state dir (resolved via Hermes APIs; never hardcoded user paths).
_LOCK = threading.RLock()
_ADAPTER = HermesAdapter()


def _state_dir() -> Path:
    from hermes_constants import get_hermes_home
    d = Path(get_hermes_home()) / "plugins" / "hermes-model-switcher"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _journal() -> OperationJournal:
    return OperationJournal(str(_state_dir() / "operations.db"))


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
                store.save(_preset_from_record(rec))
            except Exception:
                continue
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
            } for t in p.targets],
        })
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2))
    os.replace(tmp, path)


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
        slot = next((s for s in prof["slots"] if s["slotId"] == c.get("slotId")), None)
        if slot is None:
            blocked.append({"profileId": pid,
                            "reason": f"unknown slot {c.get('slotId')!r}"})
            continue
        # The stored changes come from route_inventory.preview_changes — the
        # real typed diff over this profile's raw config — NOT from a generic
        # "{slot}.model" guess. Slot shape is respected: main writes
        # model.default (string) + model.provider; cron.model pairs with
        # cron.model_provider and NEVER touches cron.provider (scheduler).
        slot_raw = "main.default" if c.get("slotId") == "main" else str(c.get("slotId"))
        to = c.get("to") or {}
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
        cp = preview_changes(cfg, [RouteEdit(slot_raw, to)])
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
            cost_class="metered" if to.get("provider") != "anthropic"
            else "subscription_included"))
    if strict and blocked:
        raise HTTPException(status_code=422, detail={
            "ok": False, "error": "preview blocked for some profiles",
            "blocked": blocked})
    return targets, blocked


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
                    assignment = {
                        "provider": first.changes.get("model.provider"),
                        "model": first.changes.get("model.default"),
                    }
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
        # Pure preview over the raw configs; no writes anywhere.
        previews, blocked = [], []
        with _LOCK:
            for c in changes:
                pid = c.get("profileId")
                ident = discover_profiles().get(pid)
                if ident is None:
                    blocked.append({"profileId": pid, "reason": "unknown profile"})
                    continue
                cfg = _ADAPTER.load_raw_config(pid)
                slot_id = c.get("slotId") or "main"
                if slot_id == "main":
                    slot_id = "main.default"
                to = c.get("to") or {}
                edit = RouteEdit(slot_id=slot_id,
                                 value={"provider": to.get("provider"),
                                        "model": to.get("model")})
                cp = preview_changes(cfg, [edit])
                previews.append({
                    "profileId": pid, "slotId": slot_id,
                    "changes": [{"path": ch.storage_path, "from": ch.before,
                                 "to": ch.after} for ch in cp.changes],
                    "blocked": list(cp.rejected),
                })
        preview_id = "pv-" + uuid.uuid4().hex[:12]
        with _LOCK:
            _PREVIEW_CACHE[preview_id] = copy.deepcopy(changes)
            if len(_PREVIEW_CACHE) > 32:
                for k in list(_PREVIEW_CACHE)[:-32]:
                    _PREVIEW_CACHE.pop(k, None)
        return {"ok": True, "previewId": preview_id,
                "preview": {"id": preview_id, "changes": previews,
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
            store.save(ReviewedPreset(
                preset_id=pid, name=body.name,
                billing_policy=BillingPolicy(
                    approved_classes=["subscription_included", "metered"]),
                targets=targets, reviewed_profile_ids=reviewed))
            _persist_presets(store)
        return {"ok": True, "preset": {"id": pid, "name": body.name,
                                       "profileIds": reviewed}}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"save failed: {type(exc).__name__}"}) from None


def _resolve_changes(body: ApplyIn) -> List[Dict[str, Any]]:
    if body.presetId:
        store = _preset_store()
        try:
            preset = store.get(body.presetId)
        except Exception:
            raise HTTPException(status_code=404, detail={
                "ok": False, "error": "unknown preset"}) from None
        return [{"profileId": t.profile, "slotId": "main",
                 "to": {"provider": t.changes.get("model.provider"),
                        "model": t.changes.get("model.default")}}
                for t in preset.targets]
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
            targets, _blocked = _changes_to_targets(changes, profiles)
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
        op = _journal().get(op_id)
        if op is None:
            raise HTTPException(status_code=404, detail={
                "ok": False, "error": "unknown operation"})
        return {"ok": True, "operation": _op_dto(op)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": f"read failed: {type(exc).__name__}"}) from None


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
