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

Runtime-path discipline: every writable storage path must be a key the runtime
actually reads (``hermes_cli/config_defaults.py`` / the registry descriptors).
Chains live at ``fallback_providers`` and ``delegation.fallback_providers``;
specialist routes live at the backend-specific ``stt.<provider>.model`` /
``tts.<provider>.model``. A path with no runtime consumer is exposed explicitly
read-only (``writable=False``), never as a fake editable route.
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
    inherit_from: Optional[str] = None      # dotted base path for value inheritance
    inherit_provider_from: Optional[str] = None  # dotted base path for provider companion
    inherit_when_null: bool = False         # a literal null means "inherit" (chain slots)
    inherit_when_empty: bool = False         # empty/None means "inherit" (delegation.model)
    runtime_consumed: bool = True            # path read by the runtime; False => read-only
    notes: str = ""


@dataclass(frozen=True)
class SlotObservation:
    descriptor: SlotDescriptor
    configured: Any            # value stored in raw config (MISSING -> None sentinel kept)
    configured_present: bool
    effective: Any             # resolved value (inheritance applied)
    source: str                # "configured" | "inherited" | "default"
    effective_provider: Any = None   # resolved provider companion (inheritance applied)
    provider_source: str = "default"  # "configured" | "inherited" | "default"


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


def _meaningful(value: Any) -> bool:
    """Runtime evidence: a value that is genuinely set (not absent/empty)."""
    if value is MISSING:
        return False
    return value not in (None, "", {}, [])


# Storage paths whose canonical stored value is an ordered chain (list of route
# dicts and/or bare provider strings), or None (inherit-unset) / [] (disabled).
_CHAIN_SLOTS = {
    "delegation.fallback_providers",
    "fallback_providers",
}

_BUILTIN_AUX_SLOTS = (
    # config_defaults.py auxiliary tasks. "goal_judge" is intentionally absent:
    # judge.goal_judge.route below owns the auxiliary.goal_judge storage path, so
    # a second row here would duplicate the same selectable slot.
    ("auxiliary.review", PROTOCOL_TYPED_DECISION),
    ("auxiliary.compression", PROTOCOL_CHAT),
    ("auxiliary.title_generation", PROTOCOL_CHAT),
)

# Runtime-consumed, backend-specific specialist paths (tools/transcription_tools.py
# resolves stt.<provider>.model; tts_tool*.py resolves tts.<provider>.model).
_SPECIALIST_SLOTS = (
    ("specialist.stt", "stt.openai.model", PROTOCOL_SPEECH),
    ("specialist.tts", "tts.openai.model", PROTOCOL_SPEECH),
)

# Fields consumed by tools/transcription_tools.py and tools/tts_tool_*.
_SPEECH_BACKENDS = {
    "stt": {name: "model" for name in ("local", "groq", "openai", "mistral", "xai", "deepinfra")},
    "tts": {name: "model" for name in ("openai", "gemini", "mistral", "minimax", "kittentts", "neutts", "deepinfra")},
}
for _speech_fields in _SPEECH_BACKENDS.values():
    _speech_fields["elevenlabs"] = "model_id"


def _speech_descriptor(cfg: Dict[str, Any], slot_id: str, provider: Optional[str] = None) -> SlotDescriptor:
    section = slot_id.rsplit(".", 1)[-1]
    settings = cfg.get(section)
    if not isinstance(settings, dict):
        settings = {}
    backend = str(provider or settings.get("provider") or "openai")
    field = _SPEECH_BACKENDS[section].get(backend)
    return SlotDescriptor(slot_id, SlotFamily.SPECIALIST, PROTOCOL_SPEECH,
        f"{section}.{backend}.{field or 'model'}", companion_paths=(f"{section}.provider",),
        writable=field is not None, runtime_consumed=field is not None,
        notes="Backend-specific speech model; voice and language settings are preserved")

# Explicitly unsupported: there is no runtime-consumed embedding-model config key
# (hermes_cli/config_defaults.py has no ``embedding`` section), so this row is
# read-only rather than a fake editable route.
_UNSUPPORTED_SPECIALIST_SLOTS = (
    ("specialist.embedding", "embedding.model", PROTOCOL_EMBEDDING,
     "unsupported: no runtime-consumed embedding-model config key"),
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
    # verbatim — config_defaults.py:1327-1343). An empty pin inherits the parent
    # route; a null chain inherits the parent chain — both backed by runtime
    # evidence (the configured base value).
    add(_obs(cfg, SlotDescriptor(
        "delegation.model", SlotFamily.DELEGATION, PROTOCOL_CHAT, "delegation.model",
        companion_paths=("delegation.provider",),
        inherit_from="model.default", inherit_provider_from="model.provider",
        inherit_when_empty=True,
        notes="empty = inherit parent route/provider (config_defaults.py:1331-1332)")))
    add(_obs(cfg, SlotDescriptor(
        "delegation.fallback_chain", SlotFamily.DELEGATION, PROTOCOL_CHAT,
        "delegation.fallback_providers",
        inherit_from="fallback_providers", inherit_when_null=True,
        notes="null means inherit-unset; [] means explicitly disabled — never normalized")))

    # Auxiliary built-ins (stores its own route; 'auto' is NOT 'same as main').
    # The genuine guard is the slot's protocol (see preview_changes), not an
    # unconditional read-only bool — valid typed routes must stay permitted.
    for slot_id, protocol in _BUILTIN_AUX_SLOTS:
        add(_obs(cfg, SlotDescriptor(
            slot_id, SlotFamily.AUXILIARY, protocol, slot_id)))

    # Judges: core typed slots + thresholds are protected.
    add(_obs(cfg, SlotDescriptor(
        "judge.goal_judge.route", SlotFamily.JUDGE, PROTOCOL_TYPED_DECISION,
        "auxiliary.goal_judge", companion_paths=("auxiliary.goal_judge.provider",))))
    add(_obs(cfg, SlotDescriptor(
        "judge.threshold", SlotFamily.JUDGE, PROTOCOL_TYPED_DECISION,
        "auxiliary.goal_judge.threshold", protected=True, writable=False)))

    # Fallback chains: main-level fallback (top-level runtime key fallback_providers).
    add(_obs(cfg, SlotDescriptor(
        "fallback.main_chain", SlotFamily.FALLBACK, PROTOCOL_CHAT, "fallback_providers",
        notes="order-preserving chain; null inherit vs [] disabled")))

    # MoA: default preset + named presets (references + aggregator live inside
    # the preset dict; the route merge preserves those neighbours).
    moa = cfg.get("moa") if isinstance(cfg.get("moa"), dict) else {}
    assert isinstance(moa, dict)
    presets = moa.get("presets") if isinstance(moa.get("presets"), dict) else {}
    assert isinstance(presets, dict)
    add(_obs(cfg, SlotDescriptor(
        "moa.default_preset", SlotFamily.MOA, PROTOCOL_CHAT, "moa.default_preset")))
    for name in sorted(presets):
        add(_obs(cfg, SlotDescriptor(
            f"moa.preset.{name}", SlotFamily.MOA, PROTOCOL_CHAT, f"moa.presets.{name}.aggregator",
            notes="Preset aggregator route; reference models and policy settings are preserved")))

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

    # Specialist (writable runtime paths + explicitly read-only unsupported rows).
    for slot_id, path, protocol in _SPECIALIST_SLOTS:
        add(_obs(cfg, _speech_descriptor(cfg, slot_id)))
    for slot_id, path, protocol, note in _UNSUPPORTED_SPECIALIST_SLOTS:
        add(_obs(cfg, SlotDescriptor(
            slot_id, SlotFamily.SPECIALIST, protocol, path,
            writable=False, runtime_consumed=False, notes=note)))

    return out


def _obs(cfg: Dict[str, Any], desc: SlotDescriptor) -> SlotObservation:
    stored = _get(cfg, desc.storage_path)
    present = stored is not MISSING
    # Owning evidence: a present value that is not the descriptor's explicit
    # "inherit" marker (a literal null for chain slots, an empty pin for
    # delegation). Only real evidence stops inheritance.
    own_evidence = present
    if desc.inherit_when_null and stored is None:
        own_evidence = False
    if desc.inherit_when_empty and stored in ("", None):
        own_evidence = False

    effective: Any = stored if present else None
    source = "configured" if own_evidence else "default"
    if not own_evidence and desc.inherit_from:
        base = _get(cfg, desc.inherit_from)
        if _meaningful(base):
            effective, source = base, "inherited"
        elif present:
            # explicit null / empty pin with no inheritance evidence: keep the
            # literal configured value rather than inventing a route.
            effective, source = stored, "configured"

    effective_provider: Any = None
    provider_source = "default"
    for companion in desc.companion_paths:
        if companion.endswith((".provider", ".model_provider")):
            provider = _get(cfg, companion)
            if _meaningful(provider):
                effective_provider, provider_source = provider, "configured"
                break
    if desc.inherit_provider_from:
        own_provider = _get(cfg, desc.storage_path.rsplit(".", 1)[0] + ".provider")
        if _meaningful(own_provider):
            effective_provider, provider_source = own_provider, "configured"
        else:
            base_provider = _get(cfg, desc.inherit_provider_from)
            if _meaningful(base_provider):
                effective_provider, provider_source = base_provider, "inherited"

    return SlotObservation(
        descriptor=desc, configured=stored if present else None,
        configured_present=present,
        effective=effective, source=source,
        effective_provider=effective_provider, provider_source=provider_source)


# ----------------------------------------------------------------- preview

def _route_payload_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in payload.items() if k in {"provider", "model", "base_url", "api_mode"}}


def _is_route_payload(value: Any) -> bool:
    return isinstance(value, dict) and "model" in value


def _is_chain_payload(value: Any) -> bool:
    return isinstance(value, dict) and "chain" in value


def _shape_refusal(slot_id: str, current: Any, new_stored: Any) -> Optional[str]:
    """Refuse to change a stored value's container shape (e.g. a list replaced
    by a scalar route dict — the MoA reference_models failure mode)."""
    if current is MISSING:
        return None
    if isinstance(current, list) and not isinstance(new_stored, list) and new_stored is not None:
        return (f"{slot_id}: stored value is a list; refusing to replace it with "
                f"{type(new_stored).__name__}")
    if isinstance(current, dict) and not isinstance(new_stored, dict):
        return (f"{slot_id}: stored value is a mapping; refusing to replace it with "
                f"{type(new_stored).__name__}")
    return None


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
        if edit.slot_id in ("specialist.stt", "specialist.tts") and isinstance(edit.value, dict):
            desc = _speech_descriptor(cfg, edit.slot_id, edit.value.get("provider"))
        if desc.storage_path == "moa.default_preset" and isinstance(edit.value, dict):
            try:
                from hermes_cli.moa_config import normalize_moa_config
                names = normalize_moa_config(cfg.get("moa"))["presets"]
            except (ImportError, KeyError, TypeError):
                rejected.append(f"{edit.slot_id}: runtime preset catalog unavailable")
                continue
            if edit.value.get("model") not in names:
                rejected.append(f"{edit.slot_id}: unknown preset {edit.value.get('model')!r}")
                continue
        if not desc.writable or desc.protected:
            rejected.append(f"{edit.slot_id}: slot is protected/read-only")
            continue
        is_chain = desc.storage_path in _CHAIN_SLOTS
        if not isinstance(edit.value, dict):
            rejected.append(f"{edit.slot_id}: edit value must be a mapping")
            continue
        if is_chain:
            # A chain slot must carry the chain EXPLICITLY: an absent ``chain``
            # key would otherwise silently clear the stored chain to [].
            if "chain" not in edit.value:
                rejected.append(
                    f"{edit.slot_id}: chain edit requires an explicit 'chain' "
                    "(list or null); a missing 'chain' would silently clear the "
                    "stored chain")
                continue
            chain = edit.value["chain"]
            if chain is not None and not isinstance(chain, list):
                rejected.append(f"{edit.slot_id}: 'chain' must be a list or null")
                continue
        elif not _is_route_payload(edit.value):
            rejected.append(f"{edit.slot_id}: edit value must be a route payload with a 'model'")
            continue
        if (not is_chain) and desc.protocol == PROTOCOL_TYPED_DECISION \
                and not _judge_payload_is_typed(edit.value):
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
            name = desc.storage_path.split(".")[2]
            removing = not _route_payload_fields(payload).get("model")
            default_preset = (cfg.get("moa") or {}).get("default_preset")
            if removing and default_preset == name:
                return ChangePreview(changes=(), rejected=(
                    f"{slot_id}: cannot remove preset {name!r} while moa.default_preset "
                    f"still references it (invalid MoA removal rejected, nothing written)",))
            if removing:
                return ChangePreview(changes=(), rejected=(
                    f"{slot_id}: cannot remove a required aggregator route; remove the preset through Hermes",))

        # The FULL typed payload reaches _stored_value_for (chain keys intact).
        new_stored = _stored_value_for(desc, payload)
        current = _get(cfg, desc.storage_path)
        refusal = _shape_refusal(slot_id, current, new_stored)
        if refusal is not None:
            return ChangePreview(changes=(), rejected=(refusal,))
        # Route-dict slots: merge into the existing stored dict so protected
        # neighbors beside the route (threshold/enforcement/...) survive the
        # swap — the edit is selected by address, not a full-object replace.
        if isinstance(current, dict) and isinstance(new_stored, dict):
            new_stored = {**copy.deepcopy(current), **new_stored}
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
    """Canonical stored shape per slot family. The FULL payload is inspected —
    chain keys are not stripped by the route-field projection."""
    route = _route_payload_fields(payload)
    if desc.storage_path in _CHAIN_SLOTS:
        # Explicit key (validated upstream). None = inherit-unset, [] = disabled;
        # entry shapes (dict routes and bare provider strings) are preserved
        # verbatim — never normalized or rewritten.
        chain = payload["chain"]
        if chain is None:
            return None
        return list(chain)
    if desc.storage_path == "moa.default_preset":
        return str(route.get("model", ""))
    if desc.family is SlotFamily.CRON:
        return str(route.get("model", ""))
    # Scalar model slots store the bare model id (config_defaults.py
    # 1095-1222 shape): model.default, delegation.model and the specialist
    # stt/tts routes are plain strings in the raw config, never route dicts.
    if desc.storage_path in _SCALAR_ROUTE_SLOTS or desc.slot_id in ("specialist.stt", "specialist.tts"):
        return str(route.get("model", ""))
    return route


# Slots whose canonical stored value is the bare model-id string.
_SCALAR_ROUTE_SLOTS = (
    {"model.default", "delegation.model"}
    | {path for _slot, path, _proto in _SPECIALIST_SLOTS}
)
