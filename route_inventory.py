"""Typed route inventory and deterministic preview from raw config and host data.

Only selected slots enter a preview; protected policy fields never enter an
edit. The same inputs yield the same sorted, timestamp-free preview.
Judge/reviewer slots reject plain chat replacements. cron.model pairs with
cron.model_provider (inference), never cron.provider (scheduler). Fallback
chains preserve order and null-versus-empty semantics. Invalid MoA removals
are rejected before writing.

Writable paths must have runtime consumers. Speech routes use the backend's
stt/tts storage key; model-less backends are read-only but may allow an explicit
backend switch. Auxiliary defaults come from public DEFAULT_CONFIG, while
plugin registrations and MoA names can be supplied by public host payloads.
"""
from __future__ import annotations

import contextvars
import copy
import enum
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


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

# Edits touching these policy paths are rejected, never silently applied.
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
    # Additive capability contract (dashboard DTO):
    #   selectable            — the row may issue an explicit assignment/backend
    #                           switch even when the CURRENT stored path is
    #                           read-only (e.g. an unset TTS row that resolves to
    #                           the model-less ``edge`` backend). Independent of
    #                           ``writable``, which stays False for the current
    #                           no-model section. The UI uses
    #                           ``selectable ?? writable`` for selection.
    #   requires_model_backend — a switch is only valid when it names an explicit,
    #                           supported provider+model; an unset/disable/chain/
    #                           remove of the model-less current section is refused.
    notes: str = ""
    # Optional additive metadata (e.g. plugin-registered aux tasks: label/
    # description/plugin/inheritFrom). Empty for core slots.
    metadata: Dict[str, Any] = field(default_factory=dict)
    # None preserves legacy descriptors: selection follows current writability.
    # Explicit True is reserved for assignments such as a speech backend switch.
    selectable: Optional[bool] = None
    requires_model_backend: bool = False


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

# Auxiliary selectors are DERIVED from the installed runtime, never frozen:
# built-ins from DEFAULT_CONFIG['auxiliary'] (public `hermes_cli.config`),
# plugin-registered tasks from the HOST auxiliary payload — the JSON body of
# `GET /api/model/auxiliary?profile=<p>` (the host owns plugin discovery and
# profile scoping). Keep goal_judge out of the selector set: judge.goal_judge.route
# below owns the auxiliary.goal_judge storage path, so a second row here would
# duplicate one selectable slot.
_JUDGE_OWNED_AUX_KEY = "goal_judge"

# Policy/scalar keys of the auxiliary section — settings, not model slots.
# auxiliary.openrouter_model is handled separately below (cross-cutting
# OpenRouter fallback override consumed by agent/auxiliary_client.py).
_AUX_POLICY_KEYS = frozenset({
    "transient_retries", "free_only", "openrouter_model", "stream_only_base_urls"})

# Obsolete aux blocks the runtime ignores at read time (config_defaults.py:
# "session_search no longer uses an aux LLM; leftover blocks in user config are
# ignored"). They surface neither as rows nor as edit targets.
_OBSOLETE_AUX_BLOCKS = frozenset({"session_search", "web_extract"})

# A plugin task key colliding with any of these crosses a DIFFERENT backend's
# semantics: cross-cutting scalar keys (openrouter_model …), obsolete ignored
# blocks and judge-owned goal_judge are all not selectable task slots. A
# plugin registration under their name cannot shadow them truthfully.
_AUX_DISALLOWED_TASK_KEYS = frozenset(
    _AUX_POLICY_KEYS | _OBSOLETE_AUX_BLOCKS | {_JUDGE_OWNED_AUX_KEY})

# /review reviewer is a full typed subagent; judge tasks speak typed decision.
_AUX_TYPING_KEYS = frozenset({"review"})

_BUILTIN_AUX_NOTES = {
    "vision": ("An explicit auxiliary vision backend makes agent.image_input_mode "
               "'auto' send a text interpretation of images to the main agent, "
               "even when the main model supports vision. Explicit native/text "
               "image modes remain unchanged. auxiliary.vision.download_timeout "
               "is preserved and is not edited by this model selector."),
}

# Truthful record of runtime discovery that FAILED (not "empty"): consumers
# must be able to distinguish "no plugin tasks registered" from "discovery
# exploded and plugin rows silently vanished" (fail closed, never silent).
#
# Warnings are PER-BUILD, not a process-global pending sink: the collector is a
# contextvar set for the duration of one ``build_inventory`` call, so a warning
# raised while inventorying profile A can never contaminate the next build (or
# a concurrent build in another thread/task for profile B). Consumers read the
# returned ``InventoryResult.warnings``.
_BUILD_WARNINGS: "contextvars.ContextVar[Optional[List[str]]]" = contextvars.ContextVar(
    "route_inventory_build_warnings", default=None)


def _record_inventory_warning(message: str) -> None:
    """Append a warning to the ACTIVE build's collector (no-op outside a build).

    Deduplicated within one build so a repeated failure path cannot spam the
    same warning; callers pass sanitized text (never a raw exception string)."""
    collector = _BUILD_WARNINGS.get()
    if collector is not None and message not in collector:
        collector.append(message)


class InventoryResult(list):
    """List of observations with isolated per-build warnings."""

    def __init__(self, observations: Iterable[SlotObservation], warnings: List[str]):
        super().__init__(observations)
        self.warnings: List[str] = list(warnings)


def _installed_default_config() -> Dict[str, Any]:
    """Public runtime defaults, or an empty map when unavailable."""
    try:
        from hermes_cli.config import DEFAULT_CONFIG as _installed
    except Exception:
        return {}
    return _installed if isinstance(_installed, dict) else {}


def _installed_aux_defaults() -> Dict[str, Any]:
    """The installed DEFAULT_CONFIG['auxiliary'] section, or {} when the Hermes
    runtime is unavailable (degrades to no derivable aux selectors)."""
    defaults = _installed_default_config()
    aux = defaults.get("auxiliary", {}) if defaults else {}
    if not isinstance(aux, dict) or not aux:
        _record_inventory_warning(
            "installed auxiliary task defaults unavailable; built-in auxiliary "
            "slots are hidden until the runtime defaults import succeeds")
        return {}
    return aux


def _aux_placeholder_route() -> Dict[str, Any]:
    """Shape of an unset aux task block (hermes_cli/plugins_aux_tasks.py's
    non-inherit task_defaults: provider 'auto', empty model/base_url/api_key)."""
    return {"provider": "auto", "model": "", "base_url": "", "api_key": "",
            "timeout": 60, "extra_body": {}}


def _mirror_layer_route_over_inherited(inherited: Dict[str, Any],
                                       user: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror of agent/auxiliary_task_config.py ``_layer_over_inherited``:
    once the operator pins a route (non-auto provider, model or base_url) the
    whole route comes from the task's own block — a pinned task never picks up
    the base's base_url/api_key (that would send one vendor's key to another's
    endpoint). Empty-string route placeholders mean 'follow the base'."""
    route_keys = frozenset({"provider", "model", "base_url", "api_key", "api_mode",
                            "key_env", "api_key_env", "reasoning_effort"})
    provider = str(user.get("provider") or "").strip().lower()
    pinned = (provider not in ("", "auto") or bool(str(user.get("model") or "").strip())
              or bool(str(user.get("base_url") or "").strip()))
    merged = {k: v for k, v in inherited.items()
              if not (pinned and k in route_keys)}
    for key, value in user.items():
        if value == "" and not (pinned and key in route_keys):
            continue
        if key == "provider" and not pinned:
            continue
        merged[key] = value
    return merged


def _layer_route_over_inherited(inherited: Dict[str, Any],
                                user: Dict[str, Any]) -> Dict[str, Any]:
    """Layer locally; test_inventory_host_data checks parity with Hermes."""
    try:
        return _mirror_layer_route_over_inherited(inherited, user)
    except Exception:
        _record_inventory_warning(
            "runtime auxiliary inheritance layering failed; the exact-shape "
            "local mirror was used for this task")
        return dict(user) if isinstance(user, dict) else {}


def _aux_effective_route(cfg: Dict[str, Any], task_key: str,
                         entries_by_key: Mapping[str, Dict[str, Any]],
                         _visited: Optional[frozenset] = None) -> Dict[str, Any]:
    """Read-time effective route of auxiliary.<task_key>, mirroring
    agent/auxiliary_task_config.py:_get_auxiliary_task_config over the raw cfg
    document: defaults (installed or plugin-declared) layered under the user
    block; a registered ``inherit_from`` resolves over the base task's
    effective route. Returns defaults + user block for built-ins.

    A ``inherit_from`` CYCLE (A -> B -> A) is broken fail-closed: the row falls
    back to its own defaults with a per-build warning, never an infinite
    recursion / RecursionError."""
    visited = _visited or frozenset()
    aux = cfg.get("auxiliary") if isinstance(cfg, dict) else None
    aux = aux if isinstance(aux, dict) else {}
    raw = aux.get(task_key)
    user_block = dict(raw) if isinstance(raw, dict) else {}
    entry = entries_by_key.get(task_key)
    if task_key in visited:
        _record_inventory_warning(
            f"plugin auxiliary task {task_key!r} participates in a circular "
            "inherit_from chain; its effective route stops inheriting here")
        if entry is not None and isinstance(entry, dict):
            rd = entry.get("defaults")
            own = copy.deepcopy(rd) if isinstance(rd, dict) else {}
        else:
            own = {}
        return _layer_route_over_inherited({**_aux_placeholder_route(), **own},
                                           copy.deepcopy(user_block))
    if entry is not None and isinstance(entry, dict):
        raw_defaults = entry.get("defaults")
        own_defaults = copy.deepcopy(raw_defaults) if isinstance(raw_defaults, dict) else {}
        inherit = entry.get("inherit_from")
        if inherit and isinstance(inherit, str) and inherit != task_key:
            base = _aux_effective_route(cfg, inherit, entries_by_key,
                                        visited | {task_key})
            merged = {**base, **own_defaults}
        else:
            merged = {**_aux_placeholder_route(), **own_defaults}
        return _layer_route_over_inherited(merged, copy.deepcopy(user_block))
    installed = _installed_aux_defaults().get(task_key)
    base = copy.deepcopy(installed) if isinstance(installed, dict) else {}
    return {**_aux_placeholder_route(), **base, **copy.deepcopy(user_block)}


_HOST_AUX_MISSING_WARNING = (
    "host auxiliary data was not supplied; plugin-registered auxiliary rows "
    "are hidden")
_HOST_AUX_MALFORMED_WARNING = (
    "host auxiliary payload was malformed; plugin-registered auxiliary rows "
    "are hidden")
_HOST_AUX_ROW_MALFORMED_WARNING = (
    "host auxiliary payload contained a malformed row; that plugin-registered "
    "auxiliary row is hidden")

# Fields of the host's ``effective`` route that may enrich a row's display.
_HOST_EFFECTIVE_FIELDS = ("provider", "model", "base_url")


def _host_aux_entries(host_aux: Any) -> Dict[str, Dict[str, Any]]:
    """Plugin-registered auxiliary task entries derived from the HOST auxiliary
    payload — the JSON body of ``GET /api/model/auxiliary?profile=<p>``:

        {"tasks": [{task, provider, model, base_url, reasoning_effort,
                    local_endpoint, label?, hint?, plugin?, inherit_from?,
                    effective?}], "main": {provider, model}}

    The host owns plugin discovery and profile scoping (its router resolves
    both inside the request's profile scope), so a row with a non-empty
    ``plugin`` IS a plugin-registered task for that profile. Rows are mapped to
    the entry shape the old in-process registry returned (``key`` = task,
    ``display_name`` = label, ``description`` = hint, ``plugin``,
    ``inherit_from``), plus the host's ``effective`` route as a DISPLAY
    enrichment — stored values still come from the raw config.

    Fail-closed, never silent: a missing payload (``None``) means plugin rows
    are HIDDEN with a warning; a malformed one hides them with a different
    warning. Neither ever renders as a truthful-looking "no plugins
    registered".
    """
    if host_aux is None:
        _record_inventory_warning(_HOST_AUX_MISSING_WARNING)
        return {}
    tasks = host_aux.get("tasks") if isinstance(host_aux, dict) else None
    if not isinstance(tasks, list):
        _record_inventory_warning(_HOST_AUX_MALFORMED_WARNING)
        return {}
    entries: Dict[str, Dict[str, Any]] = {}
    for row in tasks:
        if not isinstance(row, dict):
            _record_inventory_warning(_HOST_AUX_ROW_MALFORMED_WARNING)
            continue
        key = row.get("task")
        # Match Hermes' register_auxiliary_task grammar exactly: a task is one
        # config key, never a dotted path. Do not repair/strip invalid input;
        # str.isalnum() intentionally allows the host's Unicode alphanumerics.
        if not (isinstance(key, str) and key
                and all(c.isalnum() or c == "_" for c in key)):
            _record_inventory_warning(_HOST_AUX_ROW_MALFORMED_WARNING)
            continue
        plugin = row.get("plugin")
        if plugin is None or plugin == "":
            # A built-in row: already derived from the installed defaults (the
            # host labels those client-side), so it is never a plugin slot.
            continue
        if not isinstance(plugin, str):
            _record_inventory_warning(_HOST_AUX_ROW_MALFORMED_WARNING)
            continue
        # Only plugin-owned rows become plugin slots.
        if not plugin.strip():
            continue
        entry: Dict[str, Any] = {
            "key": key,
            "display_name": str(row.get("label") or "").strip() or key,
            "description": str(row.get("hint") or ""),
            "plugin": plugin.strip(),
        }
        inherit = row.get("inherit_from")
        if isinstance(inherit, str) and inherit.strip():
            entry["inherit_from"] = inherit.strip()
        effective = row.get("effective")
        if isinstance(effective, dict):
            fields = {name: effective[name] for name in _HOST_EFFECTIVE_FIELDS
                      if isinstance(effective.get(name), str)}
            if fields:
                entry["effective"] = fields
        entries[entry["key"]] = entry
    return entries


def _host_effective_overlay(effective: Any,
                            entry: Optional[Dict[str, Any]]) -> Any:
    """Enrich a computed effective route with the host's resolved ``effective``
    route (an inheriting plugin row carries it). Display-only enrichment: the
    STORED values still come from the raw config, and an empty host field (the
    host's "unset" placeholder) never erases a resolved value."""
    if not isinstance(effective, dict) or not isinstance(entry, dict):
        return effective
    host_effective = entry.get("effective")
    if not isinstance(host_effective, dict):
        return effective
    overlay = {name: value for name, value in host_effective.items()
               if name in _HOST_EFFECTIVE_FIELDS
               and isinstance(value, str) and value}
    return {**effective, **overlay} if overlay else effective


def _entries_by_key_map(entries: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, str) and key and key not in _AUX_DISALLOWED_TASK_KEYS:
            # colliding keys (cross-cutting scalars, obsolete, judge-owned)
            # can never shadow the real row's storage/semantics
            by_key[key] = dict(entry)
    return by_key


_AUX_OPENROUTER_FALLBACK_SLOT = "auxiliary.openrouter_model"


def _aux_route_is_pinned(block: Any) -> bool:
    """Runtime pin test (agent.auxiliary_task_config._layer_over_inherited):
    a non-``auto`` provider, a non-empty ``model``, or a non-empty ``base_url``
    pins the route so it stops following its inherit base. ``""``/``auto`` and
    whitespace-only placeholders mean "follow the base"."""
    if not isinstance(block, dict):
        return False
    provider = str(block.get("provider") or "").strip().lower()
    return (provider not in ("", "auto")
            or bool(str(block.get("model") or "").strip())
            or bool(str(block.get("base_url") or "").strip()))


def _aux_user_block(cfg: Dict[str, Any], task: str) -> Dict[str, Any]:
    """The raw ``auxiliary.<task>`` block as a dict (``{}`` when absent)."""
    aux = cfg.get("auxiliary") if isinstance(cfg, dict) else None
    aux = aux if isinstance(aux, dict) else {}
    raw = aux.get(task)
    return raw if isinstance(raw, dict) else {}


def _aux_base_exists(base: str, entries_by_key: Mapping[str, Dict[str, Any]]) -> bool:
    if base in entries_by_key:
        return True
    defaults = _installed_aux_defaults().get(base)
    return (isinstance(defaults, dict) and base not in _AUX_POLICY_KEYS
            and base not in _OBSOLETE_AUX_BLOCKS
            and any(key in defaults for key in ("provider", "model", "base_url")))


def _aux_inherit_cycle(task: str, inherit: str,
                       entries_by_key: Mapping[str, Dict[str, Any]]) -> bool:
    """True when following ``inherit``'s own ``inherit_from`` chain reaches
    ``task`` (or repeats) — a cycle that must never read as inheritance."""
    seen = {task}
    current: Optional[str] = inherit
    while isinstance(current, str) and current:
        if current in seen:
            return True
        seen.add(current)
        nxt = entries_by_key.get(current)
        current = nxt.get("inherit_from") if isinstance(nxt, dict) else None
    return False


def _aux_observation_source(cfg: Dict[str, Any], task: str,
                            entries_by_key: Mapping[str, Dict[str, Any]]) -> Optional[str]:
    """Return ``"inherited"`` when an UNPINNED plugin task resolves through its
    declared ``inherit_from`` base; else ``None`` (keep the stored source).

    Mirrors the runtime: an unpinned task route (absent block, timeout-only, or
    the ``auto``/empty placeholders the picker persists) follows its declared
    base, so the row must report the base's provider/model as inherited — a
    ``default``/``configured`` source makes ``/state`` label it Unset and the UI
    hide the route the runtime actually uses. A pinned own route — whether the
    user block or the plugin's OWN declared defaults pin provider/model/base_url
    — stays configured/default. A missing base or a cycle never fabricates
    inheritance (a genuinely unresolved base
    warns; a cycle already warned in ``_aux_effective_route``)."""
    entry = entries_by_key.get(task)
    if not isinstance(entry, dict):
        return None  # built-in selectors carry no declared inheritance
    inherit = entry.get("inherit_from")
    if not (isinstance(inherit, str) and inherit and inherit != task):
        return None
    defaults = entry.get("defaults")
    defaults = defaults if isinstance(defaults, dict) else {}
    if _aux_route_is_pinned(_aux_user_block(cfg, task)) or _aux_route_is_pinned(defaults):
        return None  # own route (or own declared default) is pinned
    if _aux_inherit_cycle(task, inherit, entries_by_key):
        return None  # cycle: never claimed as inheritance
    if not _aux_base_exists(inherit, entries_by_key):
        _record_inventory_warning(
            f"plugin auxiliary task {task!r} declares inherit_from {inherit!r} "
            "which resolves no auxiliary task; its effective route stays "
            "unpinned (inheritance not claimed)")
        return None
    # A real base's automatic routing is still inherited. Report its auto
    # placeholders truthfully; do not invent a concrete provider/model.
    return "inherited"


def _aux_observation(cfg: Dict[str, Any], desc: SlotDescriptor,
                     selector_keys: frozenset, entries_by_key: Mapping[str, Dict[str, Any]]
                     ) -> SlotObservation:
    """Aux rows carry the read-time EFFECTIVE route (runtime defaults layered
    under the user block) so an unset task reports the real resolution —
    provider 'auto' with no model, matching `hermes model` / the web picker.
    Plugin-registered tasks resolve their declared defaults and inheritance; an
    unpinned task following a declared base reports source 'inherited' so the UI
    shows the provider/model the runtime uses."""
    obs = _obs(cfg, desc)
    # Only auxiliary selector rows reach here.
    task = desc.slot_id.split(".", 1)[1]
    if task not in selector_keys and task not in entries_by_key:
        return obs  # rows without selector semantics
    effective = _aux_effective_route(cfg, task, entries_by_key)
    effective = _host_effective_overlay(effective, entries_by_key.get(task))
    source = _aux_observation_source(cfg, task, entries_by_key)
    if source is None:
        return replace(obs, effective=effective)
    return replace(obs, effective=effective, source=source)

# Runtime-consumed, backend-specific specialist paths (tools/transcription_tools.py
# resolves stt.<provider>.model; tts_tool*.py resolves tts.<provider>.model).
_SPECIALIST_SLOTS = (
    ("specialist.stt", "stt.openai.model", PROTOCOL_SPEECH),
    ("specialist.tts", "tts.openai.model", PROTOCOL_SPEECH),
)

# Fields consumed by tools/transcription_tools.py and tools/tts_tool_*.
# Only backends with a REAL model config key appear here (per-provider keys
# read by tools/transcription_tools._BUILTIN_MODEL_KEYS and the per-provider
# tts sections of config_defaults.py); backends whose section carries NO model
# key (edge / xai / piper TTS voices) are read-only rows below, with no
# invented <section>.<backend>.model store key.
_SPEECH_BACKENDS = {
    "stt": {name: "model" for name in ("local", "groq", "openai", "mistral", "xai", "deepinfra")},
    "tts": {name: "model" for name in ("openai", "gemini", "mistral", "minimax", "kittentts", "neutts", "deepinfra")},
}
for _speech_fields in _SPEECH_BACKENDS.values():
    _speech_fields["elevenlabs"] = "model_id"

# Unset speech defaults, mirrored from the installed runtime and read through
# it whenever importable: STT has no seeded provider ("No seeded provider ...
# unset = autodetect", config_defaults.py stt section) and the resolver's
# default-first backend is tools/transcription_common.DEFAULT_PROVIDER = local;
# TTS defaults to the free Edge voice backend (tools/tts_tool.DEFAULT_PROVIDER
# and config_defaults.py tts.provider = edge).
_SPEECH_FALLBACK_BACKEND = {"stt": "local", "tts": "edge"}


def _speech_default_backend(cfg: Dict[str, Any], section: str) -> str:
    """How the runtime resolves section.provider when unset (a stored pick
    wins; unset = the runtime default backend: autodetect -> 'local' STT,
    'edge' TTS). Read from the public DEFAULT_CONFIG seam, with the local
    fallback constants as the floor."""
    settings = cfg.get(section) if isinstance(cfg, dict) else None
    if isinstance(settings, dict):
        stored = settings.get("provider")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
    fallback = _SPEECH_FALLBACK_BACKEND.get(section, "")
    if section == "stt":
        # tools/transcription_common.DEFAULT_PROVIDER == 'local' (private
        # module): the local constant is the whole contract we need.
        return fallback
    tts = _installed_default_config().get("tts")
    resolved = tts.get("provider") if isinstance(tts, dict) else None
    return str(resolved or fallback)


# Truthful no-model-backend notes (NOT "unsupported"): these voice backends
# carry NO model key at all, so the CURRENT section is presented read-only with
# the backend SECTION as the storage path — never an invented model key the
# runtime would ignore. The row stays SELECTABLE so an explicit supported
# provider+model can switch it to a model-bearing backend.
_SPEECH_NO_MODEL_NOTES = {
    "stt": {
        "local": ("unset stt.provider = autodetect, local-first "
                  "(tools/transcription_tools._get_provider) — stt.local.model "
                  "feeds the local faster-whisper backend"),
    },
    "tts": {
        "edge": ("unset/default TTS backend is 'edge' (tts_tool.DEFAULT_PROVIDER / "
                 "config_defaults tts.provider) — free Edge voices carry no model "
                 "config key: the current section is read-only, no invented "
                 "tts.edge.model store key; the row is still selectable to switch "
                 "to a model-bearing backend"),
        "xai": ("x.ai TTS voices are voice_id-only (no model config key) — current "
                "tts.xai section read-only, no invented tts.xai.model store key; "
                "still selectable to switch to a model-bearing backend"),
        "piper": ("Piper selects voice files, not model ids (voices_dir) — no "
                  "model config key: current tts.piper section read-only, no "
                  "invented tts.piper.model store key; still selectable to switch "
                  "to a model-bearing backend"),
    },
}


def _speech_descriptor(cfg: Dict[str, Any], slot_id: str, provider: Optional[str] = None) -> SlotDescriptor:
    section = slot_id.rsplit(".", 1)[-1]
    backend = str(provider or _speech_default_backend(cfg, section)).strip()
    field_kind = _SPEECH_BACKENDS.get(section, {}).get(backend)
    if field_kind is not None:
        notes = _SPEECH_NO_MODEL_NOTES.get(section, {}).get(backend) or (
            "Backend-specific speech model; voice and language settings are preserved")
        return SlotDescriptor(slot_id, SlotFamily.SPECIALIST, PROTOCOL_SPEECH,
            f"{section}.{backend}.{field_kind}", companion_paths=(f"{section}.provider",),
            writable=True, runtime_consumed=True, selectable=True,
            requires_model_backend=False, notes=notes)
    # No model config key for this backend: the CURRENT section is read-only with
    # an honest backend path (no invented key), but the row stays SELECTABLE so
    # an explicit supported provider+model can switch to a model-bearing backend.
    return SlotDescriptor(slot_id, SlotFamily.SPECIALIST, PROTOCOL_SPEECH,
        f"{section}.{backend}", companion_paths=(f"{section}.provider",),
        writable=False, runtime_consumed=False, selectable=True,
        requires_model_backend=True,
        notes=_SPEECH_NO_MODEL_NOTES.get(section, {}).get(backend) or
        f"backend {backend!r} declares no model config key — read-only, no "
        f"invented {section}.{backend}.model store key")


def _model_identifier_refusal(slot_id: str, model: Any, what: str) -> Optional[str]:
    """Shared guard for a LITERAL model id that will be stored byte-exact.

    A model identifier must be a non-empty string with no leading/trailing
    whitespace: it is persisted verbatim (never trimmed or repaired), so a
    malformed value would store an id the runtime cannot route — e.g.
    ``str(None)`` becomes the literal model id ``"None"``. Backs BOTH the speech
    assignment guard and the cross-cutting ``auxiliary.openrouter_model`` scalar
    so the model-string class cannot drift between them.
    """
    if not isinstance(model, str):
        return (f"{slot_id}: {what} requires a string 'model', not "
                f"{type(model).__name__}")
    if not model.strip():
        return f"{slot_id}: {what} requires a non-empty 'model'"
    if model != model.strip():
        return (f"{slot_id}: model has leading/trailing whitespace; "
                "refusing to store an untrimmed model id")
    return None


def _speech_assignment_refusal(slot_id: str, value: Dict[str, Any]) -> Optional[str]:
    """Refuse a speech SET that would persist an unusable model id.

    Validate the submitted identifiers before backend lookup. The caller also
    refuses a resolved model-less/voice-only section. An assignment must carry
    a usable, LITERAL model id:

      * ``provider`` (when the key is present) must be a string; ``""`` stays
        the documented unset/unspecified sentinel resolved to the runtime
        default backend, but an explicit invalid provider (``None``, a number,
        a list) is REFUSED rather than allowed to fall back to the resolved
        backend route, and a non-empty provider must be whitespace-clean;
      * ``model`` must be a non-empty string.

    Identifiers are stored byte-exact, so a provider/model id carrying leading
    or trailing whitespace is REJECTED rather than silently canonicalized: the
    value the client sent is the value that would be persisted, and a
    ``" openai "`` provider must never be recognized-but-stored-untrimmed.
    """
    if "provider" in value:
        provider = value["provider"]
        if not isinstance(provider, str):
            return (f"{slot_id}: 'provider' must be a string, not "
                    f"{type(provider).__name__}")
        if provider != provider.strip():
            return (f"{slot_id}: provider has leading/trailing "
                    "whitespace; refusing to store an untrimmed backend id")
    model = value.get("model")
    return _model_identifier_refusal(slot_id, model, "model-bearing speech route")


# Explicitly unsupported: there is no runtime-consumed embedding-model config key
# (hermes_cli/config_defaults.py has no ``embedding`` section), so this row is
# read-only rather than a fake editable route.
_UNSUPPORTED_SPECIALIST_SLOTS = (
    ("specialist.embedding", "embedding.model", PROTOCOL_EMBEDDING,
     "unsupported: no runtime-consumed embedding-model config key"),
)


def _build_inventory_impl(cfg: Dict[str, Any],
                          plugin_aux_entries: Optional[Iterable[Dict[str, Any]]] = None,
                          *, host_aux: Optional[dict] = None
                          ) -> List[SlotObservation]:
    """Typed inventory of every supported slot family, including unset slots.

    ``plugin_aux_entries`` overrides plugin auxiliary-task discovery outright
    (tests/DI). Otherwise plugin tasks come from the HOST ``host_aux`` payload;
    ``host_aux is None`` means the host supplied no auxiliary data, so plugin
    rows are hidden WITH a warning (never a false "no plugins registered").
    """
    if plugin_aux_entries is None:
        entries_by_key = _entries_by_key_map(_host_aux_entries(host_aux).values())
    else:
        entries_by_key = _entries_by_key_map(plugin_aux_entries)
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

    # Auxiliary: built-in model-slot tasks derived from the INSTALLED defaults
    # (route-dict keys of DEFAULT_CONFIG["auxiliary"]), plus the cross-cutting
    # OpenRouter fallback override scalar. 'goal_judge' is judge-owned (see
    # above); policy scalars and runtime-ignored obsolete blocks are excluded.
    # The genuine guard is the slot's protocol (see preview_changes), not an
    # unconditional read-only bool — valid typed routes must stay permitted.
    aux_defaults = _installed_aux_defaults()
    aux_selector_keys: frozenset = frozenset(
        key for key, block in aux_defaults.items()
        if isinstance(block, dict) and "provider" in block
        and key not in _AUX_POLICY_KEYS | _OBSOLETE_AUX_BLOCKS
        and key != _JUDGE_OWNED_AUX_KEY)
    for key in sorted(aux_selector_keys):
        protocol = (PROTOCOL_TYPED_DECISION if key in _AUX_TYPING_KEYS else PROTOCOL_CHAT)
        add(_aux_observation(
            cfg, SlotDescriptor(
                f"auxiliary.{key}", SlotFamily.AUXILIARY, protocol, f"auxiliary.{key}",
                notes=_BUILTIN_AUX_NOTES.get(key, "")), aux_selector_keys, entries_by_key))
    add(_obs(cfg, SlotDescriptor(
        _AUX_OPENROUTER_FALLBACK_SLOT, SlotFamily.AUXILIARY, PROTOCOL_CHAT,
        _AUX_OPENROUTER_FALLBACK_SLOT,
        notes=("cross-cutting OpenRouter fallback override for the auxiliary "
               "auto-chain — read by agent/auxiliary_client for ALL auxiliary "
               "tasks at once; a non-':free' model engages the paid lane "
               "(one-time runtime warning). Stored as the bare model id."))))

    # Plugin-registered auxiliary tasks (register_auxiliary_task): the same
    # auxiliary contract, plus optional metadata and declared inheritance.
    for key in sorted(entries_by_key):
        if key in aux_selector_keys:
            continue  # a plugin never shadows a built-in selector
        entry = entries_by_key[key]
        metadata = {
            "label": str(entry.get("display_name") or key),
            "description": str(entry.get("description") or ""),
            "plugin": str(entry.get("plugin") or entry.get("plugin_key") or ""),
            "inheritFrom": (str(entry.get("inherit_from")) if entry.get("inherit_from") else ""),
        }
        add(_aux_observation(
            cfg, SlotDescriptor(
                f"auxiliary.{key}", SlotFamily.AUXILIARY, PROTOCOL_CHAT,
                f"auxiliary.{key}",
                notes=("plugin-registered auxiliary task; effective route follows "
                       f"{metadata['inheritFrom']!r} until this task is pinned"
                       if metadata["inheritFrom"] else
                       "plugin-registered auxiliary task"),
                metadata=metadata), aux_selector_keys, entries_by_key))

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
            writable=False, runtime_consumed=False, selectable=False, notes=note)))

    return out


def build_inventory(cfg: Dict[str, Any],
                    plugin_aux_entries: Optional[Iterable[Dict[str, Any]]] = None,
                    *, host_aux: Optional[dict] = None
                    ) -> InventoryResult:
    """Typed inventory of every supported slot family, including unset slots.

    ``host_aux`` is the HOST auxiliary payload (the JSON body of
    ``GET /api/model/auxiliary?profile=<p>``) for the profile being
    inventoried; plugin-registered task rows come from it. ``None`` (no host
    data) hides plugin rows and records a warning. ``plugin_aux_entries``
    overrides discovery outright (tests/DI).

    Returns an ``InventoryResult`` — a ``list`` subclass, so existing
    ``build_inventory`` callers are unchanged — whose ``warnings`` carry the
    per-build discovery warnings (never a process-global pending sink).
    """
    warnings: List[str] = []
    token = _BUILD_WARNINGS.set(warnings)
    try:
        return InventoryResult(
            _build_inventory_impl(cfg, plugin_aux_entries, host_aux=host_aux),
            warnings)
    finally:
        _BUILD_WARNINGS.reset(token)


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


def _bad_chain_entry(chain: List[Any]) -> Optional[str]:
    """First invalid chain entry, described for the rejection message.

    The ONLY valid entry is a route object carrying non-empty string
    ``provider`` and ``model``: the runtime chain normalizer silently drops
    every other shape (fallback-providers.md: "Each entry requires both
    provider and model. Entries missing either field are ignored."), so
    anything else would persist as a dead chain entry.
    """
    for index, entry in enumerate(chain, 1):
        if isinstance(entry, dict):
            provider = entry.get("provider")
            model = entry.get("model")
            if (isinstance(provider, str) and provider.strip()
                    and isinstance(model, str) and model.strip()):
                continue
            return (f"chain entry {index} needs non-empty 'provider' and "
                    f"'model' strings")
        # The runtime chain normalizer requires complete route objects and
        # silently DROPS anything else (fallback-providers.md: "Each entry
        # requires both provider and model. Entries missing either field are
        # ignored."). Storing a bare provider string or scalar would be a
        # silently dead chain entry — reject instead.
        return (f"chain entry {index} must be a route object with 'provider' "
                f"and 'model' (got {type(entry).__name__}); the runtime "
                f"ignores other shapes")
    return None


def _moa_preset_names(cfg: Dict[str, Any],
                      host_moa: Optional[dict] = None) -> Optional[frozenset]:
    """Preset names ``moa.default_preset`` may select, or ``None`` (unavailable).

    The HOST MoA payload (``GET /api/model/moa?profile=<p>``) is authoritative
    when it carries a ``presets`` mapping — the host normalizes defaults the raw
    document may not spell out. Otherwise the raw ``cfg['moa']['presets']``
    mapping decides; with neither there is no catalog to validate against.
    """
    if isinstance(host_moa, dict):
        presets = host_moa.get("presets")
        if isinstance(presets, dict):
            return frozenset(str(name) for name in presets)
    moa = cfg.get("moa") if isinstance(cfg, dict) else None
    presets = moa.get("presets") if isinstance(moa, dict) else None
    if isinstance(presets, dict):
        return frozenset(str(name) for name in presets)
    return None


def preview_changes(cfg: Dict[str, Any], edits: List[RouteEdit],
                    plugin_aux_entries: Optional[Iterable[Dict[str, Any]]] = None,
                    *, host_aux: Optional[dict] = None,
                    host_moa: Optional[dict] = None) -> ChangePreview:
    """Deterministic before/after preview. Validates first; any rejected edit
    fails the whole preview (invalid preflight performs no writes).

    ``host_aux`` / ``host_moa`` are the host payloads for the profile being
    previewed (see ``build_inventory`` / ``_moa_preset_names``)."""
    inventory = {obs.descriptor.slot_id: obs
                 for obs in build_inventory(cfg, plugin_aux_entries=plugin_aux_entries,
                                            host_aux=host_aux)}
    rejected: List[str] = []
    accepted: List[Tuple[str, SlotDescriptor, Any]] = []

    for edit in edits:
        obs = inventory.get(edit.slot_id)
        if obs is None:
            rejected.append(f"{edit.slot_id}: unknown slot (no descriptor, no write path)")
            continue
        desc = obs.descriptor
        if edit.slot_id in ("specialist.stt", "specialist.tts") and isinstance(edit.value, dict):
            refusal = _speech_assignment_refusal(edit.slot_id, edit.value)
            if refusal is not None:
                rejected.append(refusal)
                continue
            desc = _speech_descriptor(cfg, edit.slot_id, edit.value.get("provider"))
            if not desc.writable:
                backend = desc.storage_path.split(".")[1]
                rejected.append(
                    f"{edit.slot_id}: backend {backend!r} declares no model config key "
                    f"(voice-only) — set {desc.storage_path.split('.')[0]}.provider to a "
                    "model-bearing backend first; no invented store key is written")
                continue
        if desc.storage_path == _AUX_OPENROUTER_FALLBACK_SLOT and isinstance(edit.value, dict):
            # The cross-cutting override is ONE bare model id: it is NOT a task
            # route block and carries no provider/base_url of its own.
            if any(str(edit.value.get(k) or "").strip() for k in ("provider", "base_url", "api_mode")):
                rejected.append(
                    f"{edit.slot_id}: the OpenRouter fallback override accepts only a "
                    "'model' id — it is a shared auto-chain setting, not a task route")
                continue
            # The single stored value IS the model id: reject a malformed model
            # (None / number / list / dict / empty / untrimmed) BEFORE
            # ``_stored_value_for`` stringifies it — otherwise ``model: null``
            # persists the literal id "None" and clobbers a valid fallback.
            model_refusal = _model_identifier_refusal(
                edit.slot_id, edit.value.get("model"),
                "the OpenRouter fallback override")
            if model_refusal is not None:
                rejected.append(model_refusal)
                continue
        if desc.storage_path == "moa.default_preset" and isinstance(edit.value, dict):
            names = _moa_preset_names(cfg, host_moa)
            if names is None:
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
            if isinstance(chain, list):
                # Every entry must be a shape the runtime chain resolver
                # accepts: a COMPLETE route object with provider and model.
                # Strings, integers, lists, or dicts missing either field
                # would otherwise persist verbatim and be silently DROPPED by
                # the runtime normalizer, leaving a dead chain entry.
                bad = _bad_chain_entry(chain)
                if bad is not None:
                    rejected.append(f"{edit.slot_id}: {bad}")
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
            if (desc.family is SlotFamily.SPECIALIST and comp.endswith(".provider")
                    and not str(comp_val).strip()):
                # "": unset/unspecified provider — never persist an explicit
                # empty pick (the runtime treats a present-but-empty provider
                # key as a broken explicit selection).
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
    {"model.default", "delegation.model", _AUX_OPENROUTER_FALLBACK_SLOT}
    | {path for _slot, path, _proto in _SPECIALIST_SLOTS}
)
