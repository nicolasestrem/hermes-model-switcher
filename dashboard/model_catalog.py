"""Provider-aware model catalog for the dashboard.

A thin, truthful adapter over the INSTALLED Hermes picker substrate — the same
``hermes_cli.inventory.build_model_options_payload`` / ``load_picker_context``
pair the real ``GET /api/model/options`` router calls (``web_routers/models.py``,
under ``_config_profile_scope``).  We do not invent provider APIs and we do not
rewrite model ids: whatever native catalog the picker builder reports is passed
through verbatim.

Contract of :func:`get_model_catalog`::

    get_model_catalog(adapter, profile_id, provider, refresh=False) -> dict

    {
      "ok":        bool,   # True iff a provider row resolved for `provider`
      "provider":  str,    # native slug when resolved, else the request
      "models":    [{"id": str, "name": str}, ...],   # exact native ids, name == id
      "fetchedAt": str,    # ISO-8601 UTC — snapshot timestamp (see freshness below)
      "source":    str,    # picker row provenance ("canonical", "hermes", ...)
      "stale":     bool,   # True when the snapshot is cache-backed / unproven-fresh
      "error":     str|None,  # truthful warning on empty; truthful failure cause
      "providers": [{"id": str, "name": str}, ...],   # optional: catalog index
    }

Truthfulness rules:
  * ``ok`` is ``True`` only when a row for the requested provider was found (even
    if that row exposes zero models).  A provider that is absent from the
    profile's catalog yields ``ok=False`` with an explanatory ``error``.
  * An empty model list is never silently "fine": ``error`` carries the row's
    own ``warning`` when present, else a generic "reported no models" note.
  * ``error`` is populated for a real lookup failure (unknown profile, builder
    exception) without leaking secrets or stack traces.

Freshness:
  * ``refresh=True`` requests a live refresh, but freshness still requires
    cache evidence: a failed probe can return a curated or stale fallback.
  * ``refresh=False`` is the non-blocking cache read path → the provider's
    model-id disk-cache timestamp is used for ``fetchedAt`` when available and
    ``stale`` reflects its age against the installed fresh-TTL.  With no cache
    entry the freshness is unproven, so ``stale=True``.

Isolation: every read happens inside ``adapter.scoped(profile_id)`` (Hermes'
own profile/secret scope).  This module performs no writes and reads no
secrets — it only shapes the picker builder's public payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

__all__ = ["get_model_catalog", "RESULT_KEYS"]

#: Keys always present on the returned mapping.
RESULT_KEYS = ("ok", "provider", "models", "fetchedAt", "source", "stale", "error")

_FALLBACK_TTL_SECONDS = 3600  # mirrors the installed provider-model-id fresh TTL


def _load_picker_payload(refresh: bool) -> dict:
    """Call the installed picker builder for the *current* profile scope.

    Kept as a module-level seam: the caller (:func:`get_model_catalog`) invokes
    it inside ``adapter.scoped(profile_id)`` so the builder reads the requested
    profile's config and credential scope.  Tests replace this seam to avoid
    network access; callers must not.
    """
    from hermes_cli.inventory import build_model_options_payload, load_picker_context

    return build_model_options_payload(
        load_picker_context(),
        explicit_only=False,
        include_unconfigured=True,
        refresh=bool(refresh),
    )


def _row_id(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("slug") or row.get("provider_id") or "").strip()


def _row_name(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("name") or _row_id(row)).strip()


def _find_provider_row(rows: List[Any], provider: str) -> Optional[dict]:
    """First row whose slug / provider_id matches, preferring an authenticated one."""
    wanted = provider.strip().lower()
    fallback: Optional[dict] = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifiers = {_row_id(row).lower()}
        pid = str(row.get("provider_id") or "").strip().lower()
        if pid:
            identifiers.add(pid)
        if wanted not in identifiers:
            continue
        if row.get("authenticated") is not False:
            return row
        if fallback is None:
            fallback = row
    return fallback


def _model_ids(row: dict) -> List[str]:
    models = row.get("models")
    if not isinstance(models, (list, tuple)):
        return []
    return [str(m).strip() for m in models if str(m).strip()]


def _freshness(provider_slug: str, refresh: bool, now: datetime) -> tuple:
    """Return ``(fetchedAt: datetime, stale: bool)`` from the model-id disk cache.

    Best effort and read-only: a missing/unreadable cache degrades to
    ``(now, stale=True)`` rather than failing the catalog.
    """
    try:
        from hermes_cli.models import _load_provider_models_cache

        cache = _load_provider_models_cache() or {}
        entry = cache.get(provider_slug) or cache.get(provider_slug.lower())
        if isinstance(entry, dict):
            raw_at = entry.get("at")
            try:
                at: Optional[float] = float(raw_at) if raw_at is not None else None
            except (TypeError, ValueError):
                at = None
            if at:
                ttl = _FALLBACK_TTL_SECONDS
                try:
                    from hermes_cli.models import _PROVIDER_MODELS_CACHE_TTL

                    ttl = int(_PROVIDER_MODELS_CACHE_TTL)
                except Exception:
                    pass
                fetched = datetime.fromtimestamp(at, tz=timezone.utc)
                return fetched, bool(entry.get("fallback")) or (now - fetched).total_seconds() > ttl
    except Exception:
        pass
    return now, True


def get_model_catalog(
    adapter: Any,
    profile_id: Optional[str],
    provider: str,
    refresh: bool = False,
) -> dict:
    """Return the provider-aware model catalog for ``provider``.

    See the module docstring for the full result contract.  ``adapter`` must
    expose ``scoped(profile_id)`` (a :class:`hermes_adapter.HermesAdapter`).
    """
    requested = str(provider or "").strip().lower()
    refresh = bool(refresh)
    now = datetime.now(timezone.utc)

    result: dict = {
        "ok": False,
        "provider": requested,
        "models": [],
        "fetchedAt": now.isoformat(),
        "source": "unknown",
        "stale": True,
        "error": None,
    }

    if not requested:
        result["error"] = "no provider specified"
        return result

    try:
        with adapter.scoped(profile_id):
            payload = _load_picker_payload(refresh)
            rows = payload.get("providers") if isinstance(payload, dict) else None
            rows = rows if isinstance(rows, list) else []

            result["providers"] = [
                {"id": _row_id(r), "name": _row_name(r)} for r in rows if _row_id(r)
            ]

            row = _find_provider_row(rows, requested)
            if row is None:
                result["error"] = (
                    f"provider '{requested}' is not available in this profile's "
                    "catalog (not configured or not authenticated)"
                )
                return result

            native_slug = _row_id(row) or requested
            ids = _model_ids(row)
            fetched_at, stale = _freshness(native_slug, refresh, now)

            result["provider"] = native_slug
            result["models"] = [{"id": m, "name": m} for m in ids]
            source = row.get("source")
            result["source"] = str(source) if source else "unknown"
            result["fetchedAt"] = fetched_at.isoformat()
            result["stale"] = bool(stale)
            result["ok"] = True
            if ids:
                result["error"] = str(row["warning"]) if row.get("warning") else None
            else:
                warning = row.get("warning")
                result["error"] = (
                    str(warning) if warning else f"provider '{native_slug}' reported no models"
                )
            return result
    except Exception as exc:  # noqa: BLE001 — surfaced truthfully, never as an empty 200
        result["error"] = f"catalog lookup failed: {type(exc).__name__}; retry or enter a custom model"
        return result
