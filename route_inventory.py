"""Typed route inventory + deterministic change preview (plan T3).

Builds a typed descriptor inventory of every supported model-selection slot
family — main, delegation, auxiliary, judge, fallback, MoA, cron, plugin,
specialist — from a raw profile config, and produces a deterministic
before/after preview for explicitly selected edits.

Determinism contract: previews are sorted lists of plain dataclasses, carry no
timestamps, no random IDs and no process state; the same (config, edits) pair
always yields the same preview. Only explicitly selected slots appear; protected
fields (thresholds, identity, enforcement, etc.) can never enter an edit.

Protocol discipline (PLAN.md slot registry): judge/reviewer slots speak a typed
decision protocol and reject plain chat-model replacements; cron's
``cron.model`` pairs with ``cron.model_provider`` (the inference provider) and
never touches ``cron.provider`` (the scheduler); delegation fallback chains
preserve order and the null-vs-[] semantics (config_defaults.py:1327-1343);
invalid MoA removals are rejected before any write (models.py:323-338's
reject-don't-repair rule).
"""
from __future__ import annotations

import copy
import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class SlotFamily(str, enum.Enum):
    MAIN = "main"
    DELEGATION = "delegation"
    AUXILIARY = "auxiliary"
    JUDGE = "judge"
    FALLBACK = "fallback"
    MOA = "moa"
    CRON = "cron"
    PLUGIN = "plugin"
    SPECIALIST = "specialist"


# Wire protocols a slot can speak. A chat-model replacement is only compatible
# with "chat"; typed decision slots demand their own protocol.
PROTOCOL_CHAT = "chat"
PROTOCOL_TYPED_DECISION = "typed_decision"
PROTOCOL_EMBEDDING = "embedding"
PROTOCOL_SPEECH = "speech"
PROTOCOL_IMAGE = "image"

# Fields that are protected by default (PLAN.md "Protected by default") — an
# edit touching any of these paths is rejected, never silently applied.
_PROTECTED_PATHS = {
    "auxiliary.goal_judge.threshold",
    "auxiliary.goal_judge.enforcement",
    "auxiliary.review.threshold",
    "moa.reference_temperature",
    "moa.aggregator_temperature",
    "moa.reference_timeout",
    "moa.degraded_reference_policy",
    "moa.fanout",
}

MISSING = object()


@dataclass(frozen=True)
class SlotDescriptor:
    slot_id: str
    family: SlotFamily
    protocol: str
    storage_path: str          # dotted path in the raw config document
    protected: bool = False    # never writable
    writable: bool = True
    companion_paths: Tuple[str, ...] = ()   # written together (e.g. cron.model_provider)
    inherit_from: Optional[str] = None      # delegation/aux inheritance base slot
    notes: str = ""


@dataclass(frozen=True)
class SlotObservation:
    descriptor: SlotDescriptor
    configured: Any            # value stored in raw config (MISSING -> None sentinel kept)
    configured_present: bool
    effective: Any             # resolved value (inheritance applied)
    source: str                # "configured" | "inherited" | "default"


@dataclass(frozen=True)
class RouteEdit:
    """An explicitly selected change. ``value`` is the new route payload dict."""
    slot_id: str
    value: Dict[str, Any]


@dataclass(frozen=True)
class PreviewChange:
    slot_id: str
    family: SlotFamily
    storage_path: str
    before: Any
    after: Any


@dataclass(frozen=True)
class ChangePreview:
    changes: Tuple[PreviewChange, ...]
    rejected: Tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.rejected


def _get(cfg: Dict[str, Any], dotted: str) -> Any:
    node: Any = cfg
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def _set(cfg: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = cfg
    for key in parts[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[parts[-1]] = value


_BUILTIN_AUX_SLOTS = (
    # config_defaults.py:763-800 — goal judge and review are typed slots.
    ("auxiliary.goal_judge", PROTOCOL_TYPED_DECISION),
    ("auxiliary.review", PROTOCOL_TYPED_DECISION),
    ("auxiliary.summarize", PROTOCOL_CHAT),
    ("auxiliary.title", PROTOCOL_CHAT),
)

_SPECIALIST_SLOTS = (
    # Speech fields use model_id/streaming_model (config_defaults.py:1095-1222).
    ("specialist.stt", "speech.model_id", PROTOCOL_SPEECH),
    ("specialist.tts", "speech.streaming_model", PROTOCOL_SPEECH),
    ("specialist.embedding", "embedding.model", PROTOCOL_EMBEDDING),
)


def build_inventory(cfg: Dict[str, Any]) -> List[SlotObservation]:
    """Typed inventory of every supported slot family, including unset slots."""
    out: List[SlotObservation] = []
    add = out.append

    # Main
    add(_obs(cfg, SlotDescriptor(
        "main.default", SlotFamily.MAIN, PROTOCOL_CHAT, "model.default",
        companion_paths=("model.provider", "model.base_url", "model.api_mode"))))

    # Delegation: parent/child pins + fallback chain (null vs [] semantics preserved
    # verbatim — config_defaults.py:1327-1343).
    add(_obs(cfg, SlotDescriptor(
        "delegation.model", SlotFamily.DELEGATION, PROTOCOL_CHAT, "delegation.model",
        companion_paths=("delegation.provider",))))
    add(_obs(cfg, SlotDescriptor(
        "delegation.fallback_chain", SlotFamily.DELEGATION, PROTOCOL_CHAT,
        "delegation.fallback_providers",
        notes="null means inherit-unset; [] means explicitly disabled — never normalized")))

    # Auxiliary built-ins (stores its own route; 'auto' is NOT 'same as main').
    for slot_id, protocol in _BUILTIN_AUX_SLOTS:
        add(_obs(cfg, SlotDescriptor(
            slot_id, SlotFamily.AUXILIARY, protocol, slot_id,
            protected=protocol == PROTOCOL_TYPED_DECISION and slot_id.split(".")[-1] in
            {"goal_judge", "review"} and False)))

    # Judges: core typed slots + thresholds are protected.
    add(_obs(cfg, SlotDescriptor(
        "judge.goal_judge.route", SlotFamily.JUDGE, PROTOCOL_TYPED_DECISION,
        "auxiliary.goal_judge", companion_paths=("auxiliary.goal_judge.provider",))))
    add(_obs(cfg, SlotDescriptor(
        "judge.threshold", SlotFamily.JUDGE, PROTOCOL_TYPED_DECISION,
        "auxiliary.goal_judge.threshold", protected=True, writable=False)))

    # Fallback chains: main-level fallback
    add(_obs(cfg, SlotDescriptor(
        "fallback.main_chain", SlotFamily.FALLBACK, PROTOCOL_CHAT, "fallback.providers",
        notes="order-preserving chain; null inherit vs [] disabled")))

    # MoA: top-level routes and named presets, references + aggregator.
    moa = cfg.get("moa") if isinstance(cfg.get("moa"), dict) else {}
    assert isinstance(moa, dict)
    presets = moa.get("presets") if isinstance(moa.get("presets"), dict) else {}
    assert isinstance(presets, dict)
    add(_obs(cfg, SlotDescriptor(
        "moa.default_preset", SlotFamily.MOA, PROTOCOL_CHAT, "moa.default_preset")))
    for name in sorted(presets):
        add(_obs(cfg, SlotDescriptor(
            f"moa.preset.{name}", SlotFamily.MOA, PROTOCOL_CHAT, f"moa.presets.{name}")))
    # legacy flat routes
    add(_obs(cfg, SlotDescriptor(
        "moa.legacy.reference_models", SlotFamily.MOA, PROTOCOL_CHAT,
        "moa.reference_models")))
    add(_obs(cfg, SlotDescriptor(
        "moa.legacy.aggregator", SlotFamily.MOA, PROTOCOL_CHAT, "moa.aggregator")))

    # Cron: cron.model pairs with cron.model_provider; cron.provider is the
    # SCHEDULER and is never a model slot (config_defaults.py:1812-1823).
    add(_obs(cfg, SlotDescriptor(
        "cron.model", SlotFamily.CRON, PROTOCOL_CHAT, "cron.model",
        companion_paths=("cron.model_provider",),
        notes="cron.provider is the scheduler binary/policy, not an inference route")))

    # Plugin-owned selectors: any plugin section carrying model fields.
    plugins_cfg = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
    assert isinstance(plugins_cfg, dict)
    for name in sorted(plugins_cfg):
        psec = plugins_cfg[name]
        if isinstance(psec, dict):
            for key in sorted(psec):
                if key.endswith("_model") and isinstance(psec.get(key), (str, dict, type(None))):
                    add(_obs(cfg, SlotDescriptor(
                        f"plugin.{name}.{key}", SlotFamily.PLUGIN, PROTOCOL_CHAT,
                        f"plugins.{name}.{key}",
                        notes="thresholds/keys beside *_model fields stay protected")))

    # Specialist
    for slot_id, path, protocol in _SPECIALIST_SLOTS:
        add(_obs(cfg, SlotDescriptor(slot_id, SlotFamily.SPECIALIST, protocol, path)))

    return out


def _obs(cfg: Dict[str, Any], desc: SlotDescriptor) -> SlotObservation:
    stored = _get(cfg, desc.storage_path)
    present = stored is not MISSING
    inherited = None
    source = "configured" if present else "default"
    if not present and desc.inherit_from:
        base = _get(cfg, desc.inherit_from)
        if base is not MISSING:
            inherited, source = base, "inherited"
    return SlotObservation(
        descriptor=desc, configured=stored if present else None,
        configured_present=present,
        effective=stored if present else inherited,
        source=source)


# ----------------------------------------------------------------- preview

def _route_payload_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in payload.items() if k in {"provider", "model", "base_url", "api_mode"}}


def _is_route_payload(value: Any) -> bool:
    return isinstance(value, dict) and "model" in value


_JUDGE_TYPED_KEYS = {"verdict", "labels", "schema", "typed", "structured_output"}


def _judge_payload_is_typed(payload: Dict[str, Any]) -> bool:
    """A judge replacement must speak the typed decision protocol: either it
    declares a decision schema/labels or it is a model registered as a typed
    decision model (provider prefix ``typed-decision/``)."""
    if not isinstance(payload, dict):
        return False
    if _JUDGE_TYPED_KEYS & set(payload):
        return True
    model = str(payload.get("model", ""))
    provider = str(payload.get("provider", ""))
    return provider.startswith("typed-decision") or model.startswith("typed-decision/")


def preview_changes(cfg: Dict[str, Any], edits: List[RouteEdit]) -> ChangePreview:
    """Deterministic before/after preview. Validates first; any rejected edit
    fails the whole preview (invalid preflight performs no writes)."""
    inventory = {obs.descriptor.slot_id: obs for obs in build_inventory(cfg)}
    rejected: List[str] = []
    accepted: List[Tuple[str, SlotDescriptor, Any]] = []

    for edit in edits:
        obs = inventory.get(edit.slot_id)
        if obs is None:
            rejected.append(f"{edit.slot_id}: unknown slot (no descriptor, no write path)")
            continue
        desc = obs.descriptor
        if not desc.writable or desc.protected:
            rejected.append(f"{edit.slot_id}: slot is protected/read-only")
            continue
        if not isinstance(edit.value, dict) or not _is_route_payload(edit.value):
            rejected.append(f"{edit.slot_id}: edit value must be a route payload with a 'model'")
            continue
        if desc.protocol != PROTOCOL_CHAT and not _judge_payload_is_typed(edit.value):
            rejected.append(
                f"{edit.slot_id}: slot protocol {desc.protocol!r} rejects a plain chat route")
            continue
        # Protected companion/neighbor fields may not ride along in the payload.
        for key in edit.value:
            dotted = f"{desc.storage_path}.{key}"
            if dotted in _PROTECTED_PATHS:
                rejected.append(f"{edit.slot_id}: field {dotted} is protected by default")
                break
        else:
            accepted.append((edit.slot_id, desc, edit.value))
    if rejected:
        return ChangePreview(changes=(), rejected=tuple(sorted(rejected)))

    # Build the after-document and diff only the selected storage paths.
    after_cfg = copy.deepcopy(cfg)
    changes: List[PreviewChange] = []
    for slot_id, desc, payload in accepted:
        # Invalid MoA removal: a preset may not be removed while it is still the
        # referenced default (models.py reject-don't-repair rule) — reject the
        # whole preview before any document mutation.
        if desc.family is SlotFamily.MOA and desc.storage_path.startswith("moa.presets."):
            name = desc.storage_path.rsplit(".", 1)[-1]
            removing = not _route_payload_fields(payload).get("model")
            default_preset = (cfg.get("moa") or {}).get("default_preset")
            if removing and default_preset == name:
                return ChangePreview(changes=(), rejected=(
                    f"{slot_id}: cannot remove preset {name!r} while moa.default_preset "
                    f"still references it (invalid MoA removal rejected, nothing written)",))

        value = _route_payload_fields(payload)
        new_stored = _stored_value_for(desc, payload)
        _set(after_cfg, desc.storage_path, new_stored)
        companion_writes: list = []
        for comp in desc.companion_paths:
            ck = comp.rsplit(".", 1)[-1]
            if desc.family is SlotFamily.CRON and comp.endswith("model_provider"):
                # cron.model always pairs with cron.model_provider (inference
                # provider); cron.provider (scheduler) is never touched.
                comp_val = payload["provider"]
            elif ck in payload:
                comp_val = payload[ck]
            else:
                continue
            _set(after_cfg, comp, comp_val)
            comp_before = _get(cfg, comp)
            comp_before = None if comp_before is MISSING else comp_before
            if comp_before != comp_val:
                companion_writes.append((comp, comp_val))
        before = _get(cfg, desc.storage_path)
        before = None if before is MISSING else before
        if before != new_stored:
            changes.append(PreviewChange(
                slot_id=slot_id, family=desc.family, storage_path=desc.storage_path,
                before=before, after=new_stored))
        for comp, comp_val in companion_writes:
            changes.append(PreviewChange(
                slot_id=slot_id, family=desc.family, storage_path=comp,
                before=None if _get(cfg, comp) is MISSING else _get(cfg, comp),
                after=comp_val))
    changes.sort(key=lambda c: (c.family.value, c.slot_id, c.storage_path))
    return ChangePreview(changes=tuple(changes), rejected=())


def _stored_value_for(desc: SlotDescriptor, payload: Dict[str, Any]) -> Any:
    """Canonical stored shape per slot family."""
    route = _route_payload_fields(payload)
    if desc.storage_path == "delegation.fallback_providers":
        chain = payload.get("chain", [])
        if chain is None:
            return None  # inherit-unset semantics preserved verbatim
        return list(chain)
    if desc.storage_path == "fallback.providers":
        chain = payload.get("chain", [])
        if chain is None:
            return None
        return list(chain)
    if desc.storage_path == "moa.default_preset":
        return str(route.get("model", ""))
    if desc.family is SlotFamily.CRON:
        return str(route.get("model", ""))
    return route
