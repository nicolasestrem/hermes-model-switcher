"""hermes-model-switcher — dashboard backend routes (T7 wiring).

Mounted by the Dashboard at /api/plugins/hermes-model-switcher/ per the
official extension contract (website/docs/user-guide/features/
extending-the-dashboard.md): a dashboard plugin is
``<plugin>/dashboard/manifest.json`` + ``dashboard/dist/index.js`` (IIFE via
the Plugin SDK) + optional ``dashboard/plugin_api.py`` FastAPI routes.

Wiring (real, no mocks):
  GET  /state                      profiles + route inventory + presets (config only)
  POST /state                      same DTO, enriched from the host bundle
  GET  /models                     legacy: 410 hostCatalogRequired
  POST /models                     provider-aware model ids (host options payload)
  GET  /catalog                    legacy: 410 hostCatalogRequired
  POST /catalog                    aggregate provider -> models -> cost catalog
  POST /preview                    pure change preview (no writes)
  POST /presets                    persist a reviewed preset
  POST /operations                 journalled apply (engine)
  POST /operations/{op_id}/host-result   report what a HOST step really did
  GET  /operations/{op_id}         operation record (sanitized DTO)
  POST /operations/{op_id}/recover reconcile a crash/lost-result window
  POST /operations/{op_id}/undo    revision-guarded undo

Public seam (why this module imports no model-selection internals): the
Dashboard plugin runs as a separate install and may not import ANY private
``hermes_cli`` module (the admission lint names them). So the main slot is not
written here at all: the browser performs the PUBLIC host call
``POST /api/model/set`` and reports the outcome, and the engine verifies the
store afterwards (see the HOST ROUTE MAPPING table below).

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

from contextlib import contextmanager
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
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
    ApplyHostCallUnbound,
    ApplyHostResultRejected,
    ApplyHostTokenMismatch,
    apply_operation,
    plan_operation,
    reconcile,
    submit_host_result,
    undo_operation,
)
from model_switcher.journal import (
    OP_APPLYING,
    OP_AWAITING_HOST,
    OP_COMMITTED,
    OP_PARTIAL,
    OP_PENDING,
    OP_RECOVERY_REQUIRED,
    OP_UNDONE,
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
# The journal + preset store persist under the profile's OWN private state dir:
# ``<HERMES_HOME>/state/hermes-model-switcher``. There is exactly ONE location —
# no environment override and no fallback: if the resolved path would live
# inside this plugin's source tree (or any git worktree, which
# OperationJournal refuses by design), the plugin reports itself read-only with
# a fixed reason instead of silently writing somewhere else. Tests get their
# isolation from a lab HERMES_HOME (``set_hermes_home_override`` / env), never
# from a plugin-specific state override.
_LOCK = threading.RLock()
_ADAPTER = HermesAdapter()
_STATE_REFUSED_REASON = (
    "plugin state directory would live inside the plugin source tree; "
    "the plugin is read-only")


class StateDirRefused(RuntimeError):
    """The only state dir would be inside the plugin tree / a git worktree."""

    public_reason = _STATE_REFUSED_REASON


def _state_dir_for(home: Path) -> Path:
    """Private state dir for an explicitly resolved Hermes home.

    ``<home>/state/hermes-model-switcher/`` — inside the Hermes home
    (profile-local, private) but NEVER inside the plugin checkout even when the
    plugin is cloned into ``<home>/plugins/``.
    """
    candidate = Path(home) / "state" / "hermes-model-switcher"
    if _state_path_refused(candidate):
        raise StateDirRefused(_STATE_REFUSED_REASON)
    candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(candidate, 0o700)  # errors must fail closed, not authorize writes
    import stat
    if stat.S_IMODE(candidate.stat().st_mode) != 0o700:
        raise StateDirRefused(_STATE_REFUSED_REASON)
    return candidate


def _state_path_refused(path: Path) -> bool:
    """True when the state dir would live inside the plugin tree or a worktree."""
    if _is_inside_plugin_tree(path):
        return True
    try:
        # Check lexical and resolved containment, and every component BEFORE
        # mkdir/chmod: a symlink must not redirect even preset-only writes.
        if any(p.is_symlink() for p in (path, *path.parents)):
            return True
        from model_switcher.journal import is_inside_git_worktree
        return (is_inside_git_worktree(str(path))
                or is_inside_git_worktree(str(path.resolve())))
    except Exception:
        return True


def _is_inside_plugin_tree(path: Path) -> bool:
    """True when *path* would live inside the plugin's own source tree."""
    try:
        root = Path(_PKG_ROOT).resolve()
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _state_dir() -> Path:
    """The ONE state dir: ``<get_hermes_home()>/state/hermes-model-switcher``."""
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


def _require_writable_state() -> None:
    """Refuse a state-writing request when the ONE state dir is unusable.

    The fixed reason is a constant: never a path, never exception text.
    """
    try:
        _state_dir()
    except (StateDirRefused, OSError, ValueError):
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": _STATE_REFUSED_REASON}) from None


# ---------------------------------------------------------------------------
# Host data bundle
# ---------------------------------------------------------------------------
# The browser is the only side that can call Hermes' own PUBLIC host routes
# (it holds the session token). Those bodies are the evidence this backend is
# allowed to use for display, credential status, the model catalog and pricing
# — NEVER as a write-target allowlist: a write still binds to a profile
# discovered through Hermes' own APIs.
#
#   "host": {"profiles": {"<profile>": {"options": <GET /api/model/options>|null,
#                                       "auxiliary": <GET /api/model/auxiliary>|null,
#                                       "moa": <GET /api/model/moa>|null}}}
#
# Validation is shape-only and fail-soft per part (a malformed part is simply
# absent evidence), with a hard cap so one request cannot balloon the process.
_MAX_HOST_BUNDLE_BYTES = 8 * 1024 * 1024
_MAX_HOST_PROFILE_BYTES = 2 * 1024 * 1024
_HOST_PART_KEYS = ("options", "auxiliary", "moa")


@dataclass(frozen=True)
class HostBundle:
    """Validated host evidence, keyed by profile name. Absent = no evidence."""

    profiles: Dict[str, Dict[str, Any]]
    warnings: Tuple[str, ...] = ()

    def supplied(self) -> bool:
        return bool(self.profiles)

    def options(self, profile: Any) -> Optional[dict]:
        entry = self.profiles.get(str(profile or ""))
        return entry.get("options") if isinstance(entry, dict) else None

    def auxiliary(self, profile: Any) -> Optional[dict]:
        entry = self.profiles.get(str(profile or ""))
        return entry.get("auxiliary") if isinstance(entry, dict) else None

    def moa(self, profile: Any) -> Optional[dict]:
        entry = self.profiles.get(str(profile or ""))
        return entry.get("moa") if isinstance(entry, dict) else None

    def public(self) -> dict:
        """Sanitized summary for a public DTO (names + counts, never payloads)."""
        return {
            "supplied": self.supplied(),
            "profiles": sorted(self.profiles),
            "parts": {p: sorted(k for k, v in entry.items() if v is not None)
                      for p, entry in sorted(self.profiles.items())},
            "warnings": list(self.warnings),
        }


_EMPTY_HOST = HostBundle(profiles={})


def host_bundle(body: Any) -> HostBundle:
    """Validate the optional ``host`` key of a request body.

    Returns an empty bundle when no host data was supplied (every consumer then
    degrades truthfully: no evidence, never a guess). A malformed container is a
    fixed 422; a malformed PART is dropped with a warning; unknown profile keys
    are ignored. Size caps are enforced on the serialized bundle and per
    profile.
    """
    if not isinstance(body, dict):
        return _EMPTY_HOST
    host = body.get("host")
    if host is None:
        return _EMPTY_HOST
    if not isinstance(host, dict):
        raise HTTPException(status_code=422, detail={
            "ok": False, "error": "host data must be an object"})
    profiles = host.get("profiles")
    if profiles is None:
        return _EMPTY_HOST
    if not isinstance(profiles, dict):
        raise HTTPException(status_code=422, detail={
            "ok": False, "error": "host.profiles must be an object"})
    try:
        encoded = json.dumps(host, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail={
            "ok": False, "error": "host data is not serializable"}) from None
    if len(encoded) > _MAX_HOST_BUNDLE_BYTES:
        raise HTTPException(status_code=413, detail={
            "ok": False, "error": "host data bundle is too large"})
    accepted: Dict[str, Dict[str, Any]] = {}
    known = discover_profiles()
    warnings: List[str] = []
    for name, entry in profiles.items():
        if not isinstance(name, str) or not name:
            warnings.append("host: ignored a profile entry without a name")
            continue
        if name not in known:
            continue
        if not isinstance(entry, dict):
            warnings.append("host: ignored a malformed profile entry")
            continue
        try:
            size = len(json.dumps(entry, separators=(",", ":"), default=str).encode("utf-8"))
        except (TypeError, ValueError):
            warnings.append(f"host: ignored a malformed entry for profile {name!r}")
            continue
        if size > _MAX_HOST_PROFILE_BYTES:
            raise HTTPException(status_code=413, detail={
                "ok": False, "error": "host data bundle is too large"})
        parts: Dict[str, Any] = {}
        for key in _HOST_PART_KEYS:
            value = entry.get(key)
            if value is None:
                parts[key] = None
            elif isinstance(value, dict):
                parts[key] = value
            else:
                parts[key] = None
                warnings.append(
                    f"host: ignored malformed {key!r} data for profile {name!r}")
        if any(v is not None for v in parts.values()):
            accepted[name] = parts
    return HostBundle(profiles=accepted, warnings=tuple(warnings))


# ---------------------------------------------------------------------------
# HOST ROUTE MAPPING — the ONE table deciding which changes a host step owns
# ---------------------------------------------------------------------------
# Source of truth (pinned audit: public-seams/HOST-ROUTES.md, Hermes c5a3c0f8;
# code: <HERMES_REF>/hermes_cli/web_routers/models.py ``set_model_assignment``,
# <HERMES_REF>/hermes_cli/web_server_config.py ``_prepare_main_assignment`` /
# ``_apply_main_model_assignment`` (513-526) / ``_apply_model_assignment_sync``
# (889-1000), <HERMES_REF>/hermes_cli/model_switch.py
# ``model_selection_config_updates`` (1773-1810)).
#
# ONE body shape for both scopes:
#   {"scope": "main"|"auxiliary", "task": "<task>" (aux only),
#    "provider", "model", "base_url"?, "reasoning_effort"? (aux only)}
# The root profile is ``?profile=default``. api_key is NEVER sent (the host
# owns credential resolution); confirm_expensive_model is never sent either —
# a confirmation reply is HTTP 200 with ok:false/confirm_required, and the
# browser reports it as a failure so the engine's store check classifies it.
#
# MAIN — readback (semantic): ``model.provider`` + ``model.default``
# (HOST-ROUTES §1, verified against ``model_selection_config_updates``: the
# final identities are stored exactly there). The assignment also owns
# ``model.{base_url,api_mode,context_length,api_key,api,key_env,api_key_env}``
# and canonicalisation may delete the legacy aliases ``model.model``,
# ``model.name``, ``model.api_base`` — all inside ``model``. Therefore:
#   allowed prefixes: "model"
#     + "custom_providers" ONLY when the final provider is bare custom/local,
#       the effective base_url is nonempty and that endpoint is NOT already
#       known in the pre-write document (new-endpoint registration,
#       HOST-ROUTES §1 "Writes OUTSIDE model.*")
#     + the exact Nous gateway leaves in _NOUS_GATEWAY_LEAVES ONLY when the
#       final provider is ``nous`` (never a whole ``tools``/``web`` prefix)
#     + the root LEGACY migration leaves actually present in the pre-write
#       document (provider/base_url/context_length/api_base/max_turns + the
#       ``agent`` subtree that max_turns moves into) — a whole-document save
#       effect of load_config/save_config, not a route decision.
# Everything else is a FOREIGN diff: the engine keeps the bytes, marks the
# operation recovery_required with ``external_edit`` and compensates nothing.
#
# AUXILIARY — readback: ``auxiliary.<task>.provider`` + ``.model``. The host
# stores the provider with its CASE PRESERVED and does no alias normalisation
# (HOST-ROUTES §1 "EXACT auxiliary mutations"). It writes ``.base_url`` only
# when one is supplied, and with an EMPTY base_url it DELETES base_url + the
# endpoint credentials/protocol (.api_key/.api/.key_env/.api_key_env/.api_mode)
# exactly when the lowercased provider changed AND the new provider is not
# exactly ``custom``. An aux change maps onto the host route only when the
# typed payload is exactly provider/model(/base_url) and the host's result
# equals the reviewed result:
#   - provider and model non-empty (the host 400s without them);
#   - no api_mode/chain/extra keys (those are backend-only writes);
#   - a supplied base_url is a clean http(s) URL (no userinfo/query);
#   - with NO base_url supplied: the provider is unchanged (case-insensitively)
#     or is exactly ``custom`` — the only two cases where the host preserves
#     the stored endpoint leaves instead of deleting them.
# allowed prefix: "auxiliary.<task>" (+ the same root legacy-migration leaves
# that apply to ANY save).
# REMOVALS/UNPINS (action remove, clearing a slot, the openrouter fallback
# scalar, a scalar aux value, resets) NEVER map: the host route assigns, it
# cannot unset a slot — those stay backend writes.
#
# One store, one executor: the engine journals ONE step per (profile, store) —
# a host call and a backend write cannot share a config in one operation. So a
# review that puts a main change next to other changes for the SAME profile is
# refused up front (fixed reason), and aux host steps are demoted to backend
# when the profile's review carries more than one target. See
# _host_execution_plan().
_HOST_ASSIGN_PATH = "/api/model/set"
_MAIN_READBACK_KEYS = ("model.provider", "model.default")
_MAIN_ALLOWED_PREFIXES: Tuple[str, ...] = ("model",)
# Leaves the NOUS gateway default seeding can move on a nous main assignment
# (HOST-ROUTES §1 "Nous managed tool defaults",
# hermes_cli/web_server_config.py:649-661 + hermes_cli/nous_subscription.py).
_NOUS_GATEWAY_LEAVES: Tuple[str, ...] = (
    "web.backend", "web.use_gateway",
    "tts.provider", "tts.use_gateway",
    "stt.provider", "stt.use_gateway",
    "browser.cloud_provider", "browser.use_gateway",
    "image_gen.provider", "image_gen.model", "image_gen.use_gateway",
    "video_gen.provider", "video_gen.use_gateway",
)
# Root-level LEGACY keys that ``load_config``/``save_config`` canonicalize INTO
# ``model`` on ANY save (main or auxiliary): they are moved, so the document
# differs at both ends even though neither the host route nor this backend
# chose to touch them (HOST-ROUTES §1 "Whole-document save effects",
# hermes_cli/config.py:1734-1822). Allowed only when the pre-write document
# actually carries the legacy key.
_LEGACY_MIGRATION_KEYS: Tuple[str, ...] = (
    "provider", "base_url", "context_length", "api_base",
)
_LEGACY_MAX_TURNS = "max_turns"
_LEGACY_MAX_TURNS_TARGET = "agent.max_turns"
_AUX_READBACK_SUFFIXES = ("provider", "model")
_AUX_PAYLOAD_KEYS = frozenset({"provider", "model", "base_url"})


def _host_profile_name(profile: Optional[str]) -> str:
    """The ``?profile=`` value the host routes expect.

    Hermes' root profile is addressed as ``"default"`` (``is_current_profile``
    also accepts ``""``/``"current"``, but only for the process's own home — a
    dashboard request must name the profile explicitly, and
    ``profiles.get_profile_dir('default')`` is the one alias that resolves to
    the root home everywhere).
    """
    name = str(profile or "").strip()
    return name or "default"


def main_host_call(profile: Optional[str], provider: str, model: str) -> dict:
    """The public host call for a main-slot assignment (never carries a key)."""
    return {
        "method": "POST",
        "path": _HOST_ASSIGN_PATH,
        "query": {"profile": _host_profile_name(profile)},
        "body": {"scope": "main", "provider": provider, "model": model},
    }


def aux_host_call(profile: Optional[str], task: str, provider: str, model: str,
                  base_url: Optional[str] = None) -> dict:
    """The public host call for one auxiliary task route."""
    body = {"scope": "auxiliary", "task": task, "provider": provider, "model": model}
    if base_url:
        body["base_url"] = base_url
    return {
        "method": "POST",
        "path": _HOST_ASSIGN_PATH,
        "query": {"profile": _host_profile_name(profile)},
        "body": body,
    }


def _save_normalization_prefixes(cfg: Dict[str, Any]) -> List[str]:
    """Root LEGACY leaves that ANY save (host or backend) migrates into model.

    ``load_config``/``save_config`` canonicalize root ``provider``/``base_url``/
    ``context_length``/``api_base`` into the ``model`` block and ``max_turns``
    into ``agent.max_turns`` (HOST-ROUTES §1 "Whole-document save effects").
    Those leaves may therefore differ after a successful assignment that never
    decided anything about them — but only when the pre-write document actually
    carried them, so the allowance is computed from THIS document.
    """
    # load_config merges DEFAULT_CONFIG's version stamp and save_config's
    # strip-defaults pass explicitly preserves it (config.py:2510-2513).
    # This exact scalar is a host whole-save effect, not a foreign edit.
    allowed = ["_config_version"]
    allowed.extend(key for key in _LEGACY_MIGRATION_KEYS if cfg.get(key) is not None)
    # Whole-save canonicalization can also move those legacy values INTO the
    # model section during an auxiliary-only request. Permit only destinations
    # whose pre-write document actually needs that normalization, not model.*.
    model = cfg.get("model")
    if isinstance(model, dict):
        for key in ("provider", "base_url", "context_length"):
            if cfg.get(key) and not model.get(key):
                allowed.append("model." + key)
        if (cfg.get("api_base") or model.get("api_base")) and not model.get("base_url"):
            allowed.append("model.base_url")
        if model.get("api_base") is not None:
            allowed.append("model.api_base")
        aliases = ("model", "name")
        if any(model.get(key) for key in aliases):
            allowed.extend("model." + key for key in aliases if key in model)
            if not model.get("default"):
                allowed.append("model.default")
        nested = [key for key in ("default", *aliases)
                  if isinstance(model.get(key), dict)]
        allowed.extend("model." + key for key in nested)
        if (nested and model.get("provider") in (None, "", "auto")) or (
                model.get("provider") is not None and not isinstance(model["provider"], str)):
            allowed.append("model.provider")
    if cfg.get(_LEGACY_MAX_TURNS) is not None:
        allowed.append(_LEGACY_MAX_TURNS)
        allowed.append(_LEGACY_MAX_TURNS_TARGET)
    return allowed


def _known_endpoint_base_urls(cfg: Dict[str, Any]) -> List[str]:
    """Normalized base_urls the document already declares for custom endpoints."""
    from urllib.parse import urlsplit
    known: List[str] = []

    def _norm(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        try:
            parts = urlsplit(raw)
        except ValueError:
            return raw
        path = (parts.path or "").rstrip("/")
        return f"{parts.scheme}://{parts.netloc}{path}"

    rows = cfg.get("custom_providers")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                known.append(_norm(row.get("base_url")))
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for row in providers.values():
            if isinstance(row, dict):
                known.append(_norm(row.get("base_url")))
    model_block = cfg.get("model")
    if isinstance(model_block, dict):
        known.append(_norm(model_block.get("base_url")))
    return [k for k in known if k]


def main_allowed_prefixes(cfg: Dict[str, Any], provider: str,
                          options: Any = None) -> List[str]:
    """Dotted prefixes a main assignment may provably move for *provider*.

    See the HOST ROUTE MAPPING table: ``model`` always; ``custom_providers``
    only for a new bare custom/local endpoint; the exact Nous gateway leaves
    only for a nous target; plus the legacy root-migration leaves this document
    actually carries.
    """
    prefixes = list(_MAIN_ALLOWED_PREFIXES)
    prefixes.extend(_save_normalization_prefixes(cfg))
    slug = str(provider or "").strip()
    lowered = slug.lower()
    if lowered in ("custom", "local"):
        row = _host_option_row(options, slug) or {}
        endpoint = (str(row.get("base_url") or "").strip()
                    or str(((cfg.get("providers") or {}).get(slug) or {}).get("base_url") or "").strip())
        if endpoint:
            from urllib.parse import urlsplit
            try:
                parts = urlsplit(endpoint.lower())
            except ValueError:
                parts = None
            norm = (f"{parts.scheme}://{parts.netloc}{(parts.path or '').rstrip('/')}"
                    if parts else endpoint.lower())
            if norm not in _known_endpoint_base_urls(cfg):
                prefixes.append("custom_providers")
    elif lowered == "nous":
        prefixes.extend(_NOUS_GATEWAY_LEAVES)
    return prefixes


def aux_allowed_prefixes(cfg: Dict[str, Any], storage_path: str) -> List[str]:
    """Dotted prefixes an aux assignment may provably move for one task."""
    return [storage_path, *_save_normalization_prefixes(cfg)]


def _clean_endpoint_url(value: Any) -> Optional[str]:
    """A base_url safe to hand to the host: http(s), no userinfo/query/fragment."""
    from urllib.parse import urlsplit
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    return raw


def aux_host_route(desc: Any, payload: Any, current: Any) -> Optional[dict]:
    """The host call an aux change maps exactly onto, or None (stay backend).

    "Exactly" is the whole rule: the host's own result must EQUAL the reviewed
    result (HOST-ROUTES §1 "EXACT auxiliary mutations"). Removals, extra keys,
    an endpoint that needs a protocol/api_mode the route will not write, and a
    provider change whose cleanup would delete leaves the review keeps all stay
    BACKEND.
    """
    if desc is None or getattr(desc, "family", None) is not SlotFamily.AUXILIARY:
        return None
    path = str(getattr(desc, "storage_path", "") or "")
    if not path.startswith("auxiliary."):
        return None
    if getattr(desc, "protected", False) or not getattr(desc, "writable", True):
        return None
    task = path[len("auxiliary."):]
    if not task or "." in task or task == "openrouter_model":
        return None  # the cross-cutting fallback override is a bare model id
    if not isinstance(payload, dict):
        return None
    if not set(payload).issubset(_AUX_PAYLOAD_KEYS):
        return None  # api_mode/chain/anything else the host route cannot carry
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not provider or not model:
        return None
    stored = current if isinstance(current, dict) else {}
    stored_provider = str(stored.get("provider") or "").strip()
    base_url = payload.get("base_url")
    clean_base = None
    if base_url not in (None, ""):
        clean_base = _clean_endpoint_url(base_url)
        if clean_base is None:
            return None  # credentials-looking or malformed endpoint: backend
    elif "base_url" in payload:
        return None  # an explicit empty base_url is a CLEAR, not an assign
    if clean_base is None:
        # No endpoint supplied: the host preserves the stored endpoint leaves
        # only when the lowercased provider is unchanged, or the new provider is
        # exactly ``custom``. Anything else makes it delete base_url + the
        # credential/protocol leaves, which the reviewed diff does not intend.
        if provider.lower() != stored_provider.lower() and provider != "custom":
            return None
    return {"task": task, "provider": provider, "model": model,
            "base_url": clean_base}


def _host_option_rows(options: Any) -> List[dict]:
    rows = options.get("providers") if isinstance(options, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _host_option_row(options: Any, provider: str) -> Optional[dict]:
    """The host options row for *provider* (slug/provider_id, case-insensitive)."""
    want = str(provider or "").strip().lower()
    if not want:
        return None
    for row in _host_option_rows(options):
        for key in ("slug", "provider_id", "id"):
            if str(row.get(key) or "").strip().lower() == want:
                return row
    return None


def _host_option_model_ids(options: Any, provider: str) -> Optional[List[str]]:
    """Model ids the host catalog lists for *provider* (None = no evidence)."""
    row = _host_option_row(options, provider)
    if row is None:
        return None
    ids: List[str] = []
    for key in ("model_ids", "models"):
        value = row.get(key)
        if isinstance(value, list):
            ids.extend(str(m) for m in value if isinstance(m, str))
        elif isinstance(value, dict):
            ids.extend(str(m) for m in value if isinstance(m, str))
    return [m for m in ids if m]


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


def _salvage_presets(path: Path) -> Tuple[List[ReviewedPreset], bool]:
    """Parse the preset store, returning every salvageable preset plus whether
    any record had to be dropped.

    The ORIGINAL file is never mutated here: when a drop is reported the caller
    quarantines it, so a corrupt record stays recoverable while the valid
    presets remain usable (no whole-store loss on a single bad record).
    """
    records = json.loads(path.read_text())
    if not isinstance(records, list):
        raise ValueError("preset store is not a list")
    salvaged: List[ReviewedPreset] = []
    dropped = False
    for rec in records:
        try:
            preset = _preset_from_record(rec)
        except Exception:
            dropped = True
            continue
        # ui_changes (the reviewed change intent incl. its slot identity) is
        # attached, not baked into the frozen RouteTarget list.
        ui = rec.get("ui_changes")
        preset.ui_changes = ui if isinstance(ui, list) and ui \
            else _ui_changes_from_targets(preset)
        salvaged.append(preset)
    return salvaged, dropped


def _preset_store() -> PresetStore:
    global _PRESET_CORRUPT
    store = PresetStore()
    path = _state_dir() / "presets.json"
    _PRESET_CORRUPT = bool(next(path.parent.glob("presets.json.corrupt-*"), None))
    if path.exists():
        persist_cleaned = False
        try:
            salvaged, dropped = _salvage_presets(path)
        except Exception:
            # Whole file unparseable: quarantine it (bytes preserved) and
            # start empty; the condition is surfaced via presetsCorrupt.
            _quarantine_presets(path)
            salvaged = []
        else:
            if dropped:
                # A single bad record must not take the valid presets with it:
                # move the ORIGINAL aside (its bytes stay intact for an
                # operator) and salvage what still parses. A failed rename
                # raises rather than authorizing silent data loss.
                _quarantine_presets(path)
                persist_cleaned = True
        for preset in salvaged:
            store.save(preset)
        if persist_cleaned:
            # Only the valid records survived and the quarantined ORIGINAL is
            # already preserved above, so rewrite the ACTIVE store. Skipping
            # this left presets.json absent: a later read (a second /state or a
            # one-click apply) then saw no file and silently returned an EMPTY
            # store. A failed persist raises — the quarantine still holds the
            # recovery bytes, so this is never a false successful save.
            _persist_presets(store)
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
                "executor": t.executor, "host_call": t.host_call,
                "readback_keys": t.readback_keys,
                "allowed_prefixes": t.allowed_prefixes,
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
            changes=t["changes"], cost_class=t["cost_class"],
            executor=t.get("executor", "backend"), host_call=t.get("host_call"),
            readback_keys=t.get("readback_keys"),
            allowed_prefixes=t.get("allowed_prefixes"))
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
    # The browser's host evidence bundle (validated by host_bundle(); it is
    # display/credential/catalog evidence, never a write-target allowlist).
    host: Optional[Dict[str, Any]] = None


class ApplyIn(BaseModel):
    presetId: Optional[str] = None
    previewId: Optional[str] = None
    changes: Optional[List[Dict[str, Any]]] = None
    # Explicit acknowledgement that unknown-cost routes are never treated as
    # free; without it an unknown-cost route blocks the one-click apply.
    allowUnknownCost: bool = False
    # The browser's host evidence bundle (see PresetIn.host).
    host: Optional[Dict[str, Any]] = None


class HostResultIn(BaseModel):
    """What the browser's public host call actually did.

    ``outcome`` is the browser's OBSERVATION; the engine decides the verdict
    from the store. ``status`` is the host HTTP status when known (used only
    when ``outcome`` is absent).
    """

    seq: int
    token: str
    outcome: Literal["ok", "declined", "error"]
    status: Optional[int] = None


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
def _state_writable() -> bool:
    try:
        _state_dir()
        return True
    except (StateDirRefused, OSError, ValueError):
        return False


def _runtime_reasons() -> List[str]:
    return list(_ADAPTER.runtime.reasons) + (
        [] if _state_writable() else [_STATE_REFUSED_REASON])


def _runtime_writable() -> bool:
    return (bool(_ADAPTER.runtime.supported) and not _ADAPTER.runtime.read_only
            and _state_writable())


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
    dict. The OpenRouter auxiliary scalar has a fixed implicit provider, not
    a companion to write. Otherwise an absent provider reports as "" — never
    a guess or an inherited value (the resolved route lives in effectiveRoute).
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
        if d.storage_path == "auxiliary.openrouter_model" and stored.strip():
            provider = "openrouter"
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


@contextmanager
def _plugin_home_scope(home: Any):
    """Scope plugin-registry / resolver lookups to a specific Hermes home for
    the block, via Hermes' own context-local override (never ``os.environ``).

    ``hermes_cli.plugins.get_plugin_manager`` keys its manager on the
    context-local home (``_plugin_home_key`` -> ``get_hermes_home()``), so
    plugin auxiliary-task discovery must run under the home of the profile being
    inventoried — otherwise every profile's DTO would report the process-active
    profile's plugin rows. No config is written and no ``os.environ`` is mutated.
    """
    try:
        from hermes_constants import (set_hermes_home_override,
                                      reset_hermes_home_override)
    except Exception:
        yield
        return
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


# ---------------------------------------------------------------------------
# route_inventory seam probe
# ---------------------------------------------------------------------------
# The host-evidence keywords (host_aux / host_moa) are a public seam of
# route_inventory. A plugin can be installed beside an older build, so the
# presence of each keyword is probed ONCE at import. It only decides whether
# host evidence can reach the inventory — never whether a write is allowed: a
# missing seam degrades to "no plugin rows / no host preset names", which is
# the pre-seam behaviour and is reported by route_inventory itself.
def _accepts_kwarg(fn, name: str) -> bool:
    import inspect
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


_HOST_AUX_KWARG = _accepts_kwarg(build_inventory, "host_aux")
_HOST_MOA_KWARG = _accepts_kwarg(preview_changes, "host_moa")


def _build_inventory(cfg: Dict[str, Any], host_aux: Optional[dict]) -> Any:
    if _HOST_AUX_KWARG:
        return build_inventory(cfg, host_aux=host_aux)
    return build_inventory(cfg)


def _preview_changes(cfg: Dict[str, Any], edits: List[RouteEdit],
                     host_aux: Optional[dict],
                     host_moa: Optional[dict]) -> Any:
    if _HOST_AUX_KWARG or _HOST_MOA_KWARG:
        kwargs: Dict[str, Any] = {}
        if _HOST_AUX_KWARG:
            kwargs["host_aux"] = host_aux
        if _HOST_MOA_KWARG:
            kwargs["host_moa"] = host_moa
        return preview_changes(cfg, edits, **kwargs)
    return preview_changes(cfg, edits)


def _profile_inventory(host: Optional[HostBundle] = None) -> Dict[str, Dict[str, Any]]:
    """Per discovered profile: sanitized route inventory. Never returns raw
    config documents.

    ``host`` supplies the browser-side evidence this backend cannot read for
    itself: plugin-registered auxiliary rows come from the host's
    ``/api/model/auxiliary`` payload for THAT profile (the host owns plugin
    discovery and profile scoping). With no host data the inventory degrades
    truthfully — route_inventory records a warning instead of pretending no
    plugins are registered.
    """
    host = host or _EMPTY_HOST
    profiles: Dict[str, Dict[str, Any]] = {}
    for name, ident in discover_profiles().items():
        try:
            cfg = _ADAPTER.load_raw_config(name)
        except Exception:
            profiles[name] = {"id": name, "name": name, "writable": False,
                              "error": "config unreadable", "slots": []}
            continue
        slots = []
        # Scope THIS profile's home so registry/inventory resolution keys on
        # the right profile and read the per-build warnings off the result
        # (no process-global pending sink).
        with _plugin_home_scope(getattr(ident, "path", None)):
            result = _build_inventory(cfg, host.auxiliary(name))
        for obs in result:
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
                # Additive capability contract: a row may be SELECTABLE (an
                # explicit assignment/backend switch is permitted) even when its
                # CURRENT stored path is read-only — e.g. an unset TTS row that
                # resolves to the model-less edge backend. The UI selects on
                # ``selectable ?? writable``. ``requiresModelBackend`` marks such
                # a row as only accepting a set with an explicit supported
                # provider+model (never an unset/disable/chain/remove of the
                # model-less current section).
                "selectable": (_runtime_writable() and not d.protected
                               and (d.writable if d.selectable is None else d.selectable)),
                "requiresModelBackend": bool(getattr(d, "requires_model_backend", False)),
                "storagePath": d.storage_path,
                # No runtime usage evidence here: last-observed is genuinely
                # UNOBSERVED (never a copy of the configured value).
                "lastObserved": None,
                "protocol": d.protocol,
                "note": d.notes,
                # Plugin-registered task metadata (label/description/plugin/
                # inheritFrom) is projected from the descriptor when present —
                # the descriptor is the only truthful source, so it must reach
                # the DTO and not stay hidden on the inventory object.
                "metadata": (dict(d.metadata) if getattr(d, "metadata", None) else None),
            }
            if d.storage_path in _CHAIN_STORAGE_PATHS:
                # Chains carry three states, not one: a stored null INHERITS
                # (or is unset at top level), [] is explicitly DISABLED, and a
                # list is configured. Reporting [] as "configured" and a null
                # as a bare empty route would make a disabled chain look
                # removed and an inherited chain look configured.
                state, chain = _chain_slot_state(obs)
                entry["state"] = state
                # A stored chain can carry route METADATA beside provider/model
                # (base_url with userinfo, api_key, headers). The public state
                # DTO is allowlisted + URL-sanitized exactly like the preview
                # diff and the effective route — never the raw document value.
                entry["chain"] = _public_diff_value(chain, "")
            slots.append(entry)
        profiles[name] = {"id": name, "name": name,
                          "writable": _runtime_writable(), "slots": slots}
        warnings = list(getattr(result, "warnings", ()) or ())
        if warnings:
            # Truthful fail-closed discovery status: a failed runtime
            # discovery is REPORTED, never silently rendered as an empty
            # selector set. These are THIS build's warnings — a failed build
            # for another profile can never leak into this DTO.
            profiles[name]["inventoryWarnings"] = warnings
    return profiles


class MainRouteRefused(ValueError):
    """A main-slot intent that cannot become a host call, with a FIXED reason."""

    public_reason = "main route rejected"


class MainRouteIncomplete(MainRouteRefused):
    public_reason = "provider and model are required for the main slot"


class MainRouteNoOp(MainRouteRefused):
    public_reason = "change is a no-op for this slot"


class MainRouteUnauthenticated(MainRouteRefused):
    public_reason = "provider is not authenticated for this profile"


def _stored_main_route(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """The stored ``model.provider`` / ``model.default`` as plain strings."""
    provider = _get_dotted(cfg, "model.provider")
    model = _get_dotted(cfg, "model.default")
    if isinstance(model, dict):  # legacy dict-form model: block
        model = model.get("default") or model.get("name")
    return ("" if provider is _STORE_MISSING or provider is None else str(provider).strip(),
            "" if model is _STORE_MISSING or model is None else str(model).strip())


def _main_host_target(cfg: Dict[str, Any], to: Dict[str, Any],
                      profile: str, host: Optional[HostBundle]) -> Dict[str, Any]:
    """The host call a main-slot assignment becomes, plus its verification keys.

    Read from the host's OWN write path
    (<HERMES_REF>/hermes_cli/web_server_config.py `_apply_main_model_assignment`
    + <HERMES_REF>/hermes_cli/model_switch.py `model_selection_config_updates`):
    the assignment stores ``model.default`` (the model id) and
    ``model.provider`` (the provider) — those two are the semantic readback.
    Everything else the route writes (base_url, api_mode, context_length and
    the credential pointer keys) stays inside ``model`` and is the host's
    decision, which is why the diff is these two keys and the allowed prefixes
    are ``model`` (+ the route's own side-effect namespaces).

    Validation here is deliberately narrow: the host owns canonicalization and
    model validation. A provider row the host catalog reports as
    UNAUTHENTICATED refuses the preview; a provider/model the catalog does not
    know is only a WARNING (the host validates at write time).
    """
    provider = str((to or {}).get("provider") or "").strip()
    model = str((to or {}).get("model") or "").strip()
    if not provider or not model:
        raise MainRouteIncomplete(MainRouteIncomplete.public_reason)
    stored_provider, stored_model = _stored_main_route(cfg)
    if stored_provider == provider and stored_model == model:
        raise MainRouteNoOp(MainRouteNoOp.public_reason)
    options = (host or _EMPTY_HOST).options(profile)
    warnings: List[str] = []
    row = _host_option_row(options, provider)
    if _host_provider_authenticated(options, provider) is False:
        raise MainRouteUnauthenticated(MainRouteUnauthenticated.public_reason)
    if isinstance(options, dict):
        if row is None:
            warnings.append(
                "provider is not in this profile's host catalog; the host "
                "validates the assignment at write time")
        else:
            ids = _host_option_model_ids(options, provider)
            if ids is not None and model not in ids:
                warnings.append(
                    "model is not in this profile's host catalog; the host "
                    "validates the assignment at write time")
    return {
        "changes": {"model.provider": provider, "model.default": model},
        "host_call": main_host_call(profile, provider, model),
        "readback_keys": list(_MAIN_READBACK_KEYS),
        "allowed_prefixes": main_allowed_prefixes(cfg, provider, options),
        "warnings": warnings,
    }


def _host_execution_plan(targets: List[RouteTarget]
                         ) -> Tuple[List[RouteTarget], List[Dict[str, Any]]]:
    """Enforce the engine's ONE-executor-per-store rule, truthfully.

    The engine journals one step per (profile, store) so that a single
    pre-revision guard covers every key that store moves. A host call and a
    backend write therefore cannot share one profile's config in one
    operation:

    - a review whose profile carries exactly one target keeps its executor;
    - a profile carrying a main assignment plus anything else is REFUSED with a
      fixed reason (the main slot cannot be written from this process at all);
    - a profile carrying several non-main targets is written BACKEND (those
      writes are expressible both ways, so the single-step guarantee wins).

    Nothing is ever dropped or silently re-executed.
    """
    grouped: Dict[str, List[RouteTarget]] = {}
    order: List[str] = []
    for t in targets:
        if t.profile not in grouped:
            grouped[t.profile] = []
            order.append(t.profile)
        grouped[t.profile].append(t)
    out: List[RouteTarget] = []
    blocked: List[Dict[str, Any]] = []
    for profile in order:
        group = grouped[profile]
        host_targets = [t for t in group if t.executor == "host"]
        if not host_targets or len(group) == 1:
            out.extend(group)
            continue
        if any(((t.host_call or {}).get("body") or {}).get("scope") == "main"
               for t in host_targets):
            blocked.append({
                "profileId": profile, "slotId": "main", "kind": "host_scope",
                "reason": (
                    "the main assignment must be applied on its own for this "
                    "profile: a host assignment and another config change "
                    "cannot share one atomic step — apply them separately"),
            })
            continue
        for t in group:
            t.executor = "backend"
            t.host_call = None
            t.readback_keys = None
            t.allowed_prefixes = None
        out.extend(group)
    accepted = {id(t) for t in out}
    return [t for t in targets if id(t) in accepted], blocked


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
            raise RemovalStaleIdentity(
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
        raise RemovalProtected("slot is protected/read-only")
    if (desc.family is SlotFamily.MOA
            and desc.storage_path.startswith("moa.presets.")):
        raise RemovalMoaPreset("cannot remove a required aggregator route; remove the preset through Hermes")
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
            raise RemovalNoOp("change is a no-op for this slot")
        return changes
    # Scalar / chain slots: unset the slot key plus its companions.
    # The reviewed ``from`` identity is validated against the CURRENT stored
    # route FIRST: when the route changed after review, the removal blocks
    # instead of deleting a route the user never selected. (Route-dict slots
    # run the same check above; scalar slots previously skipped it.)
    if isinstance(current, str):
        observed = {
            "provider": (
                "openrouter" if desc.storage_path == "auxiliary.openrouter_model"
                else _provider_from_changes(
                    {c: _get_dotted(cfg, c) for c in desc.companion_paths
                     if _get_dotted(cfg, c) is not _STORE_MISSING})),
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
        raise RemovalNoOp("change is a no-op for this slot")
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
                        strict: bool = True,
                        host: Optional[HostBundle] = None):
    """Validate + bind changes to explicit discovered-profile config paths.

    Returns (targets, blocked). A change for an unknown or non-writable
    profile, or a slot not present in that profile's inventory, is blocked —
    never silently substituted.

    Each target names its EXECUTOR (HOST ROUTE MAPPING above): the main slot
    and an auxiliary task route that maps exactly onto ``POST /api/model/set``
    are handed to the browser as one public host call the engine verifies
    afterwards; everything else is written here through the canonical save.
    """
    host = host or _EMPTY_HOST
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
        with _plugin_home_scope(ident.path):
            desc = next((o.descriptor for o in _build_inventory(cfg, host.auxiliary(pid))
                         if o.descriptor.slot_id == slot_raw), None)
        if desc is None:
            blocked.append({"profileId": pid,
                            "reason": f"unknown slot {c.get('slotId')!r}"})
            continue
        removal = str(c.get("action") or "").lower() == "remove"
        if removal:
            # P1 #3: to:null is a REMOVAL intent, not a route assignment.
            # Removals NEVER become host steps: the host route assigns, it
            # cannot unset a slot.
            try:
                stored = _removal_changes_for(desc, cfg, c.get("from"))
            except RemovalRefused as exc:
                # A fixed class-level reason, never str(exc) (py/stack-trace-exposure).
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": type(exc).public_reason})
                continue
            except ValueError:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": "removal rejected for this slot"})
                continue
            targets.append(RouteTarget(
                profile=str(pid), store_kind="canonical_config", path=str(cfg_path),
                changes=stored,
                cost_class=_route_cost_class(str(pid), stored, ident, host)))
            continue
        if desc.slot_id == "main.default":
            # The main slot is NEVER written here: it is the host's own
            # canonical assignment (endpoint resolution, credential wiring and
            # context-pin cleanup all live behind POST /api/model/set). This
            # backend only names the semantic keys and hands the call out. A
            # refusal is an intent-level block (flagged kind="route_refusal"
            # so apply can tell it from a binding error).
            try:
                main_route = _main_host_target(cfg, to or {}, pid, host)
            except MainRouteRefused as exc:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": type(exc).public_reason,
                                "kind": "route_refusal"})
                continue
            except Exception as exc:
                blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                                "reason": "route rejected",
                                "kind": "route_refusal"})
                continue
            stored = main_route["changes"]
            targets.append(RouteTarget(
                profile=str(pid), store_kind="canonical_config", path=str(cfg_path),
                changes=stored, cost_class=_route_cost_class(str(pid), stored, ident, host),
                executor="host", host_call=main_route["host_call"],
                readback_keys=main_route["readback_keys"],
                allowed_prefixes=main_route["allowed_prefixes"],
                warnings=list(main_route["warnings"])))
            continue
        payload = _typed_payload(to or {})
        try:
            with _plugin_home_scope(ident.path):
                cp = _preview_changes(cfg, [RouteEdit(slot_raw, payload)],
                                      host.auxiliary(pid), host.moa(pid))
        except HTTPException as exc:
            blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                            "reason": _clean_detail(exc)})
            continue
        except Exception as exc:
            blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                            "reason": "route rejected"})
            continue
        if cp.rejected:
            blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                            "reason": "; ".join(cp.rejected)})
            continue
        if not cp.changes:
            blocked.append({"profileId": pid, "slotId": c.get("slotId"),
                            "reason": "change is a no-op for this slot"})
            continue
        route = aux_host_route(desc, payload, _get_dotted(cfg, desc.storage_path))
        if route is not None:
            # An auxiliary task route the host route expresses EXACTLY: the
            # host writes provider/model(/base_url) inside auxiliary.<task>,
            # and the engine verifies exactly those keys afterwards.
            stored = {f"{desc.storage_path}.provider": route["provider"],
                      f"{desc.storage_path}.model": route["model"]}
            if route["base_url"]:
                stored[f"{desc.storage_path}.base_url"] = route["base_url"]
            targets.append(RouteTarget(
                profile=str(pid), store_kind="canonical_config", path=str(cfg_path),
                changes=stored, cost_class=_route_cost_class(str(pid), stored, ident, host),
                executor="host",
                host_call=aux_host_call(pid, route["task"], route["provider"],
                                        route["model"], route["base_url"]),
                readback_keys=[f"{desc.storage_path}.{key}"
                               for key in _AUX_READBACK_SUFFIXES],
                allowed_prefixes=aux_allowed_prefixes(cfg, desc.storage_path)))
            continue
        stored = {}
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
            cost_class=_route_cost_class(str(pid), stored, ident, host)))
    targets, plan_blocked = _host_execution_plan(targets)
    blocked.extend(plan_blocked)
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
        elif (dotted == "auxiliary.openrouter_model"
                and isinstance(value, str) and value.strip()):
            # The SCALAR OpenRouter override ROUTES THROUGH OpenRouter
            # (agent/auxiliary_client consumes it for every aux auto-chain
            # task), so a non-empty value must authenticate like any routed
            # provider — never vacuously pass the credential gate. Empty/
            # unset values write no route and stay vacuous.
            add("openrouter")
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


def _host_provider_authenticated(options_payload: Any, provider: str) -> Optional[bool]:
    """Provider-scoped authentication from the HOST options payload (C).

    True / False / None (no evidence). ``None`` is never treated as
    authenticated — the caller decides the unknown case explicitly. The host
    payload can carry duplicate rows for one slug: an explicit True wins over an
    explicit False, so an unauthenticated alias can never shadow a real one.
    """
    provider = str(provider or "").strip().lower()
    if not provider or not isinstance(options_payload, dict):
        return None
    try:
        result = _model_catalog_module().provider_authenticated(options_payload, provider)
    except Exception:
        return None
    if result is True or result is False:
        return result
    # Fall back to the payload's own rows when the transform is unavailable:
    # the evidence is the same, and None stays None.
    explicit_false = False
    for row in _host_option_rows(options_payload):
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
                                    provider: str, host: Optional[HostBundle] = None) -> bool:
    """Whether ``profile`` can authenticate routing to ``provider``.

    Evidence, in order: a user-declared custom endpoint needs no hosted
    credential; a ``providers:`` row's ``key_env``/``api_key_env`` is read
    through the scoped secret seam (never the process env, never a sibling
    profile's .env); an inline ``api_key`` on that row is config evidence; then
    the HOST options payload, through ``provider_authenticated``. When nothing
    can classify the provider, absence of evidence blocks it — an unrelated env
    entry is never credential evidence for a different provider.
    """
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
    authed = _host_provider_authenticated(
        (host or _EMPTY_HOST).options(profile), provider)
    return True if authed is True else False


def _route_cost_class(profile: str, stored: Dict[str, Any],
                      ident=None, host: Optional[HostBundle] = None) -> str:
    """Live cost class for a prepared route, classified PER provider.

    Never infers a free/subscription tier from a provider slug. A custom
    endpoint is metered; a credentialed hosted provider is metered; a provider
    that cannot be classified at all (no custom endpoint, no host
    authentication evidence) is "unknown" — unknown cost is never treated as
    free.
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
            if not _provider_credentials_satisfied(profile, ident, cfg, provider, host):
                return "unknown"
        except Exception:
            return "unknown"
    return "metered"


def _clean_detail(exc: HTTPException) -> str:
    # Exceptions from a route helper are never public DTOs, even when their
    # current implementation happens to construct a fixed string.
    return "route rejected"


class RemovalRefused(ValueError):
    """A removal that cannot bind, with a FIXED public reason.

    The API must never echo ``str(exc)`` into a response (CodeQL
    py/stack-trace-exposure): exception text may carry paths or config values.
    Each subclass fixes its public wording as a class attribute, so the handler
    reads ``type(exc).public_reason`` — a constant — instead of the message.
    The message stays available to direct callers and tests.
    """

    public_reason = "removal rejected for this slot"


class RemovalProtected(RemovalRefused):
    public_reason = "slot is protected/read-only"


class RemovalMoaPreset(RemovalRefused):
    public_reason = ("cannot remove a required aggregator route; "
                     "remove the preset through Hermes")


class RemovalStaleIdentity(RemovalRefused):
    public_reason = "route changed since review; re-preview before removing"


class RemovalNoOp(RemovalRefused):
    public_reason = "change is a no-op for this slot"


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
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        return "<unparseable URL>"  # malformed containers may contain secrets
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(value)
        if not parts.netloc:
            return "<unparseable URL>"  # preserve set-vs-cleared semantics
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"  # retain valid IPv6 authority syntax
        port = parts.port
        netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except (ValueError, TypeError):
        return "<unparseable URL>"  # never echo potentially secret-bearing input


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
    journal snapshots, storage paths, changes, or raw config contents. The
    terminal ``final_state`` and a FIXED ``failure`` category (never a raw
    exception message) are surfaced for the UI's failure contract."""
    pub = to_public_operation(op) or {}
    steps = pub.get("steps", [])
    undoable = (pub.get("state") == OP_COMMITTED
                and any(s.get("state") == "applied" for s in steps))
    return {
        "id": pub.get("op_id"), "state": pub.get("state"),
        "results": [{
            "profileId": s.get("profile"), "status": s.get("state"),
            "seq": s.get("seq"),
            "store": s.get("store_kind"),
            # The revision the review was bound to and the revision the write
            # left behind, straight from the journal step (engine.revision_summary
            # carries the same pair). The UI renders rev before → after per
            # result, so a no-change write is visibly a no-op.
            "revisionBefore": _step_revision(s, "revision_before", "pre_revision"),
            "revisionAfter": _step_revision(s, "revision_after", "post_revision"),
        } for s in steps],
        "undoable": undoable,
        # Terminal classification (committed/compensated/partial/
        # recovery_required/undone) plus a fixed failure category label.
        "final_state": pub.get("state"),
        "failure": pub.get("failure_category"),
    }


def _op_ok(op: Optional[Dict[str, Any]]) -> bool:
    """True only when the operation's outcome is a durable success.

    ``ok`` reflects committed (including an idempotent re-read of an
    already-committed op) or a clean undo — a compensated / partial /
    recovery_required / pending outcome is NOT ok, so the caller surfaces the
    safe ``failure`` category and ``final_state`` instead of a bare success.
    """
    return (op or {}).get("state") in (OP_COMMITTED, OP_UNDONE)


def _op_envelope(op: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Response envelope for an operation: top-level ``ok`` (outcome-derived)
    plus the sanitized DTO."""
    envelope = {"ok": _op_ok(op), "operation": _op_dto(op or {})}
    call = (op or {}).get("host_call")
    if (op or {}).get("state") == OP_AWAITING_HOST and isinstance(call, dict):
        # Never expose the journal intent wholesale (or a snapshot). The only
        # plaintext token is the engine's current, ephemeral continuation.
        body = call.get("body") or {}
        allowed = ("scope", "provider", "model", "task", "base_url", "reasoning_effort")
        envelope["hostCall"] = {
            "opId": call.get("opId"), "seq": call.get("seq"),
            "token": call.get("token"), "method": call.get("method"),
            "path": call.get("path"),
            "query": {"profile": (call.get("query") or {}).get("profile")},
            "body": {key: body[key] for key in allowed if key in body},
        }
    return envelope


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
def _state_dto(host: Optional[HostBundle] = None) -> Dict[str, Any]:
    """The /state DTO. ``host`` only ENRICHES it (plugin aux rows, credential
    status); everything config-derived is identical without it."""
    host = host or _EMPTY_HOST
    profiles = _profile_inventory(host)
    for name, entry in profiles.items():
        if isinstance(entry, dict) and host.supplied():
            entry["credentials"] = _profile_credential_status(name, host)
    with _LOCK:
        store = _preset_store() if _state_writable() else PresetStore()
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
        corrupt = bool(_PRESET_CORRUPT) if _state_writable() else False
    return {"ok": True, "profiles": list(profiles.values()),
            "slots": [s for p in profiles.values() for s in p["slots"]],
            "presets": presets,
            "presetsCorrupt": corrupt,
            "host": host.public(),
            "runtime": {"writable": _runtime_writable(),
                        "version": _ADAPTER.runtime.version_label,
                        "reasons": _runtime_reasons()}}


def _profile_credential_status(profile: str, host: HostBundle) -> Dict[str, bool]:
    """provider -> host-authenticated? for the providers this profile uses.

    Evidence is the host options payload only (this backend never probes a
    provider). A provider the payload does not classify is absent, never
    guessed to be authenticated.
    """
    status: Dict[str, bool] = {}
    options = host.options(profile)
    if not isinstance(options, dict):
        return status
    try:
        cfg = _ADAPTER.load_raw_config(profile)
    except Exception:
        cfg = {}
    for provider in _referenced_providers(cfg.get("model") or {}) or []:
        value = _host_provider_authenticated(options, provider)
        if value is not None:
            status[provider] = bool(value)
    for row in _host_option_rows(options):
        slug = str(row.get("slug") or row.get("provider_id") or "").strip().lower()
        if slug and slug not in status and isinstance(row.get("authenticated"), bool):
            status[slug] = row["authenticated"]
    return status


@router.get("/state")
def state():
    """Config-only state: host-dependent parts degrade truthfully."""
    try:
        with _LOCK:
            return _state_dto(_EMPTY_HOST)
    except Exception as exc:  # sanitized error surface
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": "state unavailable"}) from None


@router.post("/state")
def state_with_host(body: Dict[str, Any]):
    """Same DTO, enriched with the browser's host evidence."""
    try:
        host = host_bundle(body)
        with _LOCK:
            return _state_dto(host)
    except HTTPException:
        raise
    except Exception as exc:  # sanitized error surface
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": "state unavailable"}) from None


def _host_catalog_required() -> JSONResponse:
    """The legacy GET catalog routes: the HOST owns the catalog now."""
    return JSONResponse(
        status_code=410,
        content={"ok": False, "reason": "hostCatalogRequired"},
        headers={"Cache-Control": "no-store"})


def _catalog_request(body: Dict[str, Any], host: HostBundle) -> Tuple[Optional[str], str]:
    """(profileId, provider) from a catalog request body (§C payload shape)."""
    profile_id = body.get("profile") or body.get("profileId")
    profile_id = profile_id if isinstance(profile_id, str) and profile_id else None
    provider = str(body.get("provider") or "")
    return profile_id, provider


def _catalog_options(body: Dict[str, Any], host: HostBundle,
                     profile_id: Optional[str]) -> Optional[dict]:
    """The host ``/api/model/options`` payload for this request.

    ``body.options`` wins (the UI can post it directly); otherwise the profile's
    bundled host data is used. ``None`` is passed through as "no evidence": the
    catalog transform reports ``hostCatalogUnavailable`` rather than inventing
    an empty catalog.
    """
    options = body.get("options")
    if isinstance(options, dict):
        return options
    return host.options(profile_id) if profile_id else None


@router.get("/models")
def models_legacy():
    """Legacy GET: the model catalog requires the host's options payload."""
    return _host_catalog_required()


@router.post("/models")
def models(body: Dict[str, Any], response: Response):
    """Provider-aware model ids — the catalog module's own DTO, verbatim.

    Shape: ``{ok, provider, models:[{id,name}], fetchedAt, source, stale,
    error, providers}``. The host ``options`` payload is required evidence;
    ``ok=False`` (with an explanatory ``error``) is a visible failure, never a
    silently empty success. The response is never cached, and no
    journal/preset/state path is touched.
    """
    response.headers["Cache-Control"] = "no-store"
    host = host_bundle(body)
    profile_id, provider = _catalog_request(body, host)
    failure = {"ok": False, "provider": provider, "models": [],
               "fetchedAt": None, "source": "unknown", "stale": True}
    try:
        module = _model_catalog_module()
    except Exception:
        return {**failure, "error": "model catalog module unavailable"}
    if profile_id and profile_id not in discover_profiles():
        return {**failure, "error": f"unknown profile {profile_id!r}"}
    try:
        result = module.models_response(
            _catalog_options(body, host, profile_id), provider,
            profile_id=profile_id, refresh=bool(body.get("refresh")))
    except Exception as exc:
        return {**failure, "error": "catalog lookup failed"}
    if not isinstance(result, dict):
        return {**failure, "error": "catalog returned an unexpected payload"}
    return result


@router.get("/catalog")
def catalog_legacy():
    """Legacy GET: the aggregate catalog requires the host's options payload."""
    return _host_catalog_required()


@router.post("/catalog")
def catalog(body: Dict[str, Any], response: Response):
    """Aggregate provider -> models -> cost catalog — read-only, truthful.

    Shape: ``{ok, profileId, providers:[{id, label, authenticated, configured,
    isCurrent, modelCount, fetchedAt, stale, warning, models:[{id, label,
    pricingStatus, cost}]}], cost:{pending, unavailableProviders}, error}``.
    No host options -> ``ok=false`` with ``hostCatalogUnavailable``; unknown
    profile / module failure / zero provider rows are ``ok=false`` with a
    sanitized ``error`` (HTTP 200), never an empty success. Deliberately NOT
    gated on ``_runtime_writable()``: reading a catalog is safe on a read-only
    runtime. The response is never cached and this path performs no
    journal/preset/state writes.
    """
    response.headers["Cache-Control"] = "no-store"
    host = host_bundle(body)
    profile_id, provider = _catalog_request(body, host)
    failure = {"ok": False,
               "profileId": profile_id,
               "providers": [],
               "cost": {"pending": False, "unavailableProviders": []}}
    try:
        module = _model_catalog_module()
    except Exception:
        return {**failure, "error": "model catalog module unavailable"}
    if profile_id and profile_id not in discover_profiles():
        return {**failure, "error": f"unknown profile {profile_id!r}"}
    try:
        result = module.catalog_response(
            _catalog_options(body, host, profile_id), provider,
            profile_id=profile_id, refresh=bool(body.get("refresh")))
    except Exception as exc:
        return {**failure, "error": "catalog lookup failed"}
    if not isinstance(result, dict):
        return {**failure, "error": "catalog returned an unexpected payload"}
    if "profileId" not in result:
        result["profileId"] = profile_id
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
        host = host_bundle(body)
        profiles = _profile_inventory(host)
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
        out_changes, blocked, prepared, all_targets = [], [], [], []
        known = discover_profiles()
        with _LOCK:
            for c in changes:
                # Watch the target's canonical store ACROSS preparation: the
                # prepared mutation (canonical cleanup deletions included) is
                # derived from that document, so a write landing between the
                # read and the bind must abort the preview — never be adopted
                # as the mutation's expected revision.
                watched = _watch_stores([c], known)
                targets, blk = _changes_to_targets([c], profiles, strict=False, host=host)
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
                all_targets.extend(targets)
                for t in targets:
                    entry = _target_dict(t)
                    key = (t.profile, t.store_kind, t.path)
                    if watched.get(key) is not None:
                        # Pin the revision the mutation was prepared FROM, not
                        # whatever the store holds at bind time.
                        entry["expected_revision"] = watched[key]
                    prepared.append(entry)
                out_changes.append({**_sanitize_change(c),
                                    "executor": targets[0].executor,
                                    "warnings": list(targets[0].warnings),
                                    "sideEffects": side_effects,
                                    "diff": diff})
        planned, execution_blocks = _host_execution_plan(all_targets)
        blocked.extend(execution_blocks)
        refused_profiles = {b["profileId"] for b in execution_blocks}
        prepared = [t for t in prepared if t["profile"] not in refused_profiles]
        out_changes = [c for c in out_changes if c["profileId"] not in refused_profiles]
        # Multi-slot non-main reviews demote compatible aux steps to backend;
        # keep both the reviewed display and the cached journal plan exact.
        for entry, target, change in zip(prepared, planned, out_changes):
            entry.update(_target_dict(target))
            change["executor"] = target.executor
            if target.executor != "host":
                change["sideEffects"] = [
                    effect for effect in change["sideEffects"]
                    if effect != "host assignment; exact persisted diff is available after readback"]
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
            "ok": False, "error": "preview failed"}) from None


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
        if t.executor == "host":
            side_effects.append("host assignment; exact persisted diff is available after readback")
            side_effects.extend(t.warnings or [])
            continue
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
        # An UNVERIFIABLE recorded revision (None — the read failed when the
        # preview was bound) fails closed: None == None must never pass as
        # "unchanged", and an unreadable store now is equally stale.
        if rev is None or cur != rev:
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
            _require_writable_state()
            host = host_bundle({"host": body.host})
            profiles = _profile_inventory(host)
            targets, _blocked = _changes_to_targets(body.changes, profiles, host=host)
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
            "ok": False, "error": "save failed"}) from None


# -- F1: ReviewedPreset.preflight runs BEFORE a one-click apply -------------
def _profile_has_credentials(profile: str, ident,
                             providers: Optional[List[str]] = None,
                             host: Optional[HostBundle] = None) -> bool:
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
            if not _provider_credentials_satisfied(profile, ident, cfg, provider, host):
                return False
        except Exception:
            return False
    return True


def _live_cost_class(t: RouteTarget, host: Optional[HostBundle] = None) -> str:
    """The live cost class of a review-time route, re-derived PER provider.
    A frozen unclassifiable class (e.g. 'unknown') passes through verbatim so
    the preset's billing policy — never a silent free pass — gates it."""
    if t.cost_class not in ("subscription_included", "metered"):
        return t.cost_class  # e.g. 'unknown': unknown cost is never treated as free
    return _route_cost_class(t.profile, t.changes, host=host)


def _preset_preflight(preset: ReviewedPreset,
                      allow_unknown: bool = False,
                      host: Optional[HostBundle] = None) -> List[str]:
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
                name, ident, providers_by_profile.get(name, []), host)
        except Exception:
            credentials[name] = False  # fail closed, never fabricate
    live_cost_classes = {_route_key(t): _live_cost_class(t, host) for t in preset.targets}
    return preset.preflight(credentials=credentials,
                            live_cost_classes=live_cost_classes,
                            discovered_profiles=sorted(profiles))


def _preset_target_dicts(preset: ReviewedPreset,
                         profiles: Dict[str, Dict[str, Any]],
                         host: Optional[HostBundle] = None
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
                                               strict=False, host=host)
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
            target = _target_dict(t)
            target["path"] = str(path)
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
        target = _target_dict(t)
        target["path"] = str(path)
        # Bind the discovered identity so the engine plans against exactly the
        # profile the preset was reviewed for (no re-derivation at plan time).
        if ident is not None:
            target["identity"] = _identity_dict(ident)
        out.append(target)
    return out


def _target_dict(t: RouteTarget) -> Dict[str, Any]:
    """Private plan DTO: execution/verification metadata travels with intent."""
    return {"profile": t.profile, "store_kind": t.store_kind, "path": t.path,
            "changes": copy.deepcopy(t.changes), "executor": t.executor,
            "host_call": copy.deepcopy(t.host_call),
            "readback_keys": copy.deepcopy(t.readback_keys),
            "allowed_prefixes": copy.deepcopy(t.allowed_prefixes)}


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
                 if k not in _MAIN_SIDE_EFFECT_KEYS)),
             json.dumps({"executor": t.get("executor", "backend"),
                         "host_call": t.get("host_call"),
                         "readback_keys": t.get("readback_keys"),
                         "allowed_prefixes": t.get("allowed_prefixes")},
                        sort_keys=True, default=str))
            for t in ts)
    return norm(a) == norm(b)


def _unchanged_provider_hops(profile: str,
                             changes: Dict[str, Any],
                             host: Optional[HostBundle] = None) -> List[str]:
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
        ident = discover_profiles().get(profile)
        if ident is None:
            return []
        with _plugin_home_scope(ident.path):
            inventory = _build_inventory(cfg, (host or _EMPTY_HOST).auxiliary(profile))
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


def _ensure_prepared_credentials(prepared: List[Dict[str, Any]],
                                 host: Optional[HostBundle] = None) -> None:
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
            needed = _unchanged_provider_hops(profile, changes, host)
        if not needed:
            continue
        ident = known.get(profile)
        if ident is None or not _profile_has_credentials(profile, ident, needed, host):
            missing.append(str(profile))
    if missing:
        raise HTTPException(status_code=409, detail={
            "ok": False, "blocked": True,
            "error": "apply blocked: missing provider credentials",
            "reasons": [f"profile {p!r} is missing provider credentials"
                        for p in sorted(set(missing))]})


def _resolve_apply(body: ApplyIn, profiles: Dict[str, Dict[str, Any]],
                   host: Optional[HostBundle] = None
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
            preset, allow_unknown=bool(body.allowUnknownCost), host=host)
        if reasons:
            raise HTTPException(status_code=409, detail={
                "ok": False, "blocked": True,
                "error": "apply blocked: preset preflight refused",
                "reasons": reasons})
        preset_targets = _preset_target_dicts(preset, profiles, host)
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
                                               strict=False, host=host)
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
        prepared = [_target_dict(t) for t in targets]
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
    _ensure_prepared_credentials(prepared, host)
    return prepared


@router.post("/operations")
def apply(body: ApplyIn):
    if not _runtime_writable():
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": "runtime is read-only; writes refused",
            "reasons": _runtime_reasons()})
    try:
        with _LOCK:
            _require_writable_state()
            host = host_bundle({"host": body.host})
            profiles = _profile_inventory(host)
            prepared = _resolve_apply(body, profiles, host)
            journal = _journal()
            try:
                op = plan_operation(journal, prepared)
                outcome = apply_operation(journal, op["op_id"])
                return _op_envelope(outcome)
            finally:
                journal.close()
    except HTTPException:
        raise
    except ApplyBlocked as exc:
        # Fixed class-level reason, never str(exc) (py/stack-trace-exposure):
        # preflight messages can carry store paths and config values.
        raise HTTPException(status_code=409, detail={
            "ok": False,
            "error": f"apply blocked: {type(exc).public_reason}"}) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": "apply failed"}) from None


@router.post("/operations/{op_id}/host-result")
def host_result(op_id: str, body: HostResultIn):
    """Accept only fixed observations; actual config readback is authoritative."""
    try:
        with _LOCK:
            _require_writable_state()
            journal = _journal()
            try:
                if journal.get(op_id) is None:
                    raise HTTPException(status_code=404, detail={
                        "ok": False, "error": "unknown operation"})
                outcome = submit_host_result(
                    journal, op_id, body.seq, body.token, body.outcome,
                    status=body.status)
                return _op_envelope(outcome)
            finally:
                journal.close()
    except HTTPException:
        raise
    except ApplyHostTokenMismatch:
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": "host_token_mismatch",
            "failure": "host_token_mismatch"}) from None
    except ApplyBlocked as exc:
        raise HTTPException(status_code=409, detail={
            "ok": False, "error": type(exc).public_reason}) from None
    except Exception:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": "host result failed"}) from None


@router.get("/operations/{op_id}")
def get_operation(op_id: str):
    try:
        # Reconciliation COMPENSATES applied steps: it must never interleave
        # with an in-flight Apply/Undo (threadpool handlers made that possible)
        # or it could roll back steps while the writer continues and still
        # report committed. Same mutation lock as every write path.
        with _LOCK:
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
                return _op_envelope(op)
            finally:
                journal.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": "read failed"}) from None


@router.post("/operations/{op_id}/recover")
def recover_operation(op_id: str):
    """Explicit restart path: reconcile one operation's persisted intent
    against the actual files (committed/compensated/recovery_required)."""
    try:
        # See get_operation: reconciliation compensates, so it serializes with
        # the mutation handlers via the same lock.
        with _LOCK:
            journal = _journal()
            try:
                op = journal.get(op_id)
                if op is None:
                    raise HTTPException(status_code=404, detail={
                        "ok": False, "error": "unknown operation"})
                op = reconcile(journal, op_id)
                return _op_envelope(op)
            finally:
                journal.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": "recover failed"}) from None


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
                return _op_envelope(journal.get(op_id))
            finally:
                journal.close()
    except ApplyBlocked as exc:
        # See apply(): a fixed public reason, never the exception message.
        raise HTTPException(status_code=409, detail={
            "ok": False,
            "error": f"undo blocked: {type(exc).public_reason}"}) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "ok": False, "error": "undo failed"}) from None
