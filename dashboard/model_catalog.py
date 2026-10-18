"""Provider-aware model catalog for the dashboard.

A thin, truthful adapter over the INSTALLED Hermes picker substrate — the same
``hermes_cli.inventory.build_model_options_payload`` / ``load_picker_context``
pair the real ``GET /api/model/options`` router calls (``web_routers/models.py``,
under ``_config_profile_scope``).  Those version-sensitive imports live behind
``HermesAdapter.catalog_payload`` / ``HermesAdapter.pricing_probe``; this module
never imports them itself.  We do not invent provider APIs and we do not
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

Contract of :func:`get_model_catalog_all` (frozen Dashboard DTO)::

    get_model_catalog_all(adapter, profile_id, provider=None, refresh=False)

    {
      "ok": bool, "profileId": str|None,
      "providers": [{
        "id": str, "label": str, "authenticated": bool, "configured": bool,
        "isCurrent": bool, "modelCount": int, "fetchedAt": str|None,
        "stale": bool, "warning": str|None,
        "models": [{"id": str, "label": str,
                    "pricingStatus": "priced"|"free"|"included"|"unknown",
                    "cost": {unit, input, output, cacheRead, display{input,
                             output, cache}, free, source, pricingVersion}|None}],
      }],
      "cost": {"pending": bool, "unavailableProviders": [str]},
      "error": str|None,
    }

Cost resolution is OFFLINE and ordered: (1) picker display strings verbatim +
raw cached per-token rates, (2) billing route — subscription-included, else
official-docs snapshot, (3) models.dev cost dict, (4) ``unknown`` with
``cost=None``.  ``free`` requires an explicit free flag (0.0 cannot distinguish
a free model from missing data), and ``unknown`` is never rendered as free or
``$0.00``.  Rates are USD per 1M tokens; there is no price timestamp.

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
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

__all__ = [
    "get_model_catalog",
    "get_model_catalog_all",
    "RESULT_KEYS",
    "CATALOG_RESULT_KEYS",
    "PROVIDER_KEYS",
    "MODEL_KEYS",
    "COST_KEYS",
]

#: Keys always present on the returned mapping.
RESULT_KEYS = ("ok", "provider", "models", "fetchedAt", "source", "stale", "error")

# -- aggregate catalog DTO (frozen; the dashboard UI builds against it) ------
CATALOG_RESULT_KEYS = ("ok", "profileId", "providers", "cost", "error")
PROVIDER_KEYS = ("id", "label", "authenticated", "configured", "isCurrent",
                 "modelCount", "fetchedAt", "stale", "warning", "models")
MODEL_KEYS = ("id", "label", "pricingStatus", "cost")
COST_KEYS = ("unit", "input", "output", "cacheRead", "display", "free",
             "source", "pricingVersion")
COST_DISPLAY_KEYS = ("input", "output", "cache")
PRICING_STATUSES = ("priced", "free", "included", "unknown")
COST_SOURCES = ("picker", "official_docs_snapshot", "models_dev", "subscription_route")
COST_UNIT = "USD_per_1M_tokens"
_INCLUDED_PRICING_VERSION = "included-route"
_ONE_MILLION = Decimal(1000000)

_FALLBACK_TTL_SECONDS = 3600  # mirrors the installed provider-model-id fresh TTL


def _load_picker_payload(refresh: bool) -> dict:
    """Call the installed picker builder for the *current* profile scope.

    Compatibility seam for callers/test doubles whose adapter predates
    ``HermesAdapter.catalog_payload``: it still routes through the adapter so
    this module never imports Hermes' version-sensitive modules itself. The
    builder is executed inside the caller's ``adapter.scoped(profile_id)``.
    """
    from hermes_adapter import HermesAdapter

    return HermesAdapter().catalog_payload(bool(refresh))


# Captured so an explicitly replaced module seam (tests, legacy integrations)
# can be detected and honored by ``_payload_for``.
_DEFAULT_PICKER_LOADER = _load_picker_payload


def _payload_for(adapter: Any, refresh: bool) -> Any:
    """The picker payload through the adapter's version-sensitive seam.

    ``HermesAdapter.catalog_payload`` owns the only ``hermes_cli.inventory``
    import in the plugin. A caller that explicitly replaced the module seam
    below keeps that injection point; otherwise adapters without
    ``catalog_payload`` (test doubles, older adapters) fall back to it, and it
    too delegates to the adapter.
    """
    if _load_picker_payload is not _DEFAULT_PICKER_LOADER:
        return _load_picker_payload(bool(refresh))
    seam = getattr(adapter, "catalog_payload", None)
    if callable(seam):
        return seam(bool(refresh))
    return _load_picker_payload(bool(refresh))


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
    ids: List[str] = []
    for entry in models:
        # Native rows normally carry bare id strings; dict-shaped metadata is
        # normalized to its exact ``id`` (never stringified as a dict).
        value = entry.get("id") if isinstance(entry, dict) else entry
        text = "" if value is None else str(value).strip()
        if text:
            ids.append(text)
    return ids


def _freshness(provider_slug: str, refresh: bool, now: datetime) -> tuple:
    """Return ``(fetchedAt | None, stale: bool)`` from the model-id disk cache.

    Freshness is evidence-based: WITHOUT a same-provider cache row there is no
    fetch to report, so ``fetchedAt`` is None (never a fabricated "now" that
    reads like a fresh fetch) and ``stale`` is True. ``refresh`` requests live
    discovery upstream, but a failed probe can still return a curated/stale
    fallback — so it never by itself makes the result proveably fresh.

    Best effort and read-only: a missing/unreadable cache degrades to
    ``(None, stale=True)`` rather than failing the catalog.
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
    return None, True


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
        "fetchedAt": None,
        "source": "unknown",
        "stale": True,
        "error": None,
    }

    if not requested:
        result["error"] = "no provider specified"
        return result

    try:
        with adapter.scoped(profile_id):
            payload = _payload_for(adapter, refresh)
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
            result["fetchedAt"] = fetched_at.isoformat() if fetched_at else None
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


# -- aggregate provider -> models -> cost catalog ---------------------------

def _usd_display(value: Optional[float]) -> Optional[str]:
    """Format a USD/1M rate as ``$x.xx`` (up to 4 decimals, >=2 kept).

    Numeric rates are the source of truth; a missing rate is ``None`` — never
    rendered as ``$0.00``.
    """
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None
    text = f"{dec:.4f}".rstrip("0")
    if "." not in text:
        text += ".00"
    else:
        head, frac = text.split(".", 1)
        text = f"{head}.{frac.ljust(2, '0')}"
    return f"${text}"


def _per_million(raw: Any) -> Optional[float]:
    """Raw per-token price string → USD/1M float, or None when absent/invalid."""
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        value = Decimal(str(raw)) * _ONE_MILLION
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value < 0:
        return None
    try:
        return float(value)
    except (OverflowError, ValueError):
        return None


def _display_string(value: Any) -> Optional[str]:
    """Picker display string verbatim (``$x.xx`` / ``free`` / ``?``)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _picker_display(entry: dict) -> dict:
    return {
        "input": _display_string(entry.get("input")),
        "output": _display_string(entry.get("output")),
        "cache": _display_string(entry.get("cache")),
    }


def _cost_from_rates(*, source: str, inp: Optional[float],
                     out: Optional[float], cache: Optional[float],
                     display: dict, free: bool,
                     pricing_version: Optional[str],
                     status_when_positive: str = "priced") -> dict:
    """DTO cost for numeric rates; zero/absent rates are never "free" by default.

    ``free`` may only come from an explicit source flag. Without one, a
    non-positive rate set is ``unknown`` (cost null) — 0.0 cannot distinguish a
    free model from missing data.
    """
    positive = any(v is not None and v > 0 for v in (inp, out, cache))
    if not free and not positive:
        return {"pricingStatus": "unknown", "cost": None}
    return {
        "pricingStatus": "free" if free else status_when_positive,
        "cost": {
            "unit": COST_UNIT,
            "input": inp,
            "output": out,
            "cacheRead": cache,
            "display": display,
            "free": bool(free),
            "source": source,
            "pricingVersion": pricing_version,
        },
    }


def _picker_cost(entry: Any, raw: Any) -> dict:
    """Step (1): picker display strings verbatim + numeric raw per-token rates."""
    if not isinstance(entry, dict):
        return {"pricingStatus": "unknown", "cost": None}
    raw = raw if isinstance(raw, dict) else {}
    return _cost_from_rates(
        source="picker",
        inp=_per_million(raw.get("prompt")),
        out=_per_million(raw.get("completion")),
        cache=_per_million(raw.get("input_cache_read")),
        display=_picker_display(entry),
        free=entry.get("free") is True,
        pricing_version=None,
    )


def _included_cost() -> dict:
    """Step (2a): a subscription route bills no per-token rate."""
    display = {key: _usd_display(0.0) for key in COST_DISPLAY_KEYS}
    return {
        "pricingStatus": "included",
        "cost": {
            "unit": COST_UNIT,
            "input": 0.0,
            "output": 0.0,
            "cacheRead": 0.0,
            "display": display,
            "free": False,
            "source": "subscription_route",
            "pricingVersion": _INCLUDED_PRICING_VERSION,
        },
    }


def _snapshot_cost(entry: Any) -> dict:
    """Step (2b): official-docs snapshot entry (USD/1M Decimals).

    ``get_pricing_entry`` falls back to models.dev internally; only a true
    official-docs snapshot is claimed here, so the source label stays exact and
    the models.dev path (step 3) owns that data.
    """
    if not isinstance(entry, dict) or entry.get("source") != "official_docs_snapshot":
        return {"pricingStatus": "unknown", "cost": None}
    inp = entry.get("input")
    out = entry.get("output")
    cache = entry.get("cache_read")
    return _cost_from_rates(
        source="official_docs_snapshot",
        inp=inp if isinstance(inp, (int, float)) else None,
        out=out if isinstance(out, (int, float)) else None,
        cache=cache if isinstance(cache, (int, float)) else None,
        display={key: _usd_display(value) for key, value in
                 (("input", entry.get("input")),
                  ("output", entry.get("output")),
                  ("cache", entry.get("cache_read")))},
        free=False,
        pricing_version=entry.get("pricing_version"),
    )


def _models_dev_cost(entry: Any) -> dict:
    """Step (3): models.dev rate row (USD/1M), only on a present cost dict."""
    if not isinstance(entry, dict):
        return {"pricingStatus": "unknown", "cost": None}
    inp = entry.get("input")
    out = entry.get("output")
    cache = entry.get("cache_read")
    return _cost_from_rates(
        source="models_dev",
        inp=inp if isinstance(inp, (int, float)) else None,
        out=out if isinstance(out, (int, float)) else None,
        cache=cache if isinstance(cache, (int, float)) else None,
        display={"input": _usd_display(inp if isinstance(inp, (int, float)) else None),
                 "output": _usd_display(out if isinstance(out, (int, float)) else None),
                 "cache": _usd_display(cache if isinstance(cache, (int, float)) else None)},
        free=False,
        pricing_version=None,
    )


def _cost_for_model(model_id: str, picker_entry: Any, raw_picker_pricing: Any,
                    provider_slug: str, probe: Any) -> dict:
    """Resolve one model's ``{pricingStatus, cost}`` in the documented order.

    (1) picker row pricing (display verbatim + raw cached per-token rates),
    (2) billing route: subscription-included, else official-docs snapshot,
    (3) models.dev cost dict, (4) ``unknown`` with ``cost=None``.

    Every step is offline; nothing here can start provider I/O.
    """
    resolved = _picker_cost(picker_entry, raw_picker_pricing)
    if resolved["cost"] is not None:
        return resolved

    mode = probe.billing_mode(model_id, provider_slug)
    if mode == "subscription_included":
        return _included_cost()
    if mode == "official_docs_snapshot":
        resolved = _snapshot_cost(probe.pricing_entry(model_id, provider_slug))
        if resolved["cost"] is not None:
            return resolved

    resolved = _models_dev_cost(probe.models_dev_cost(provider_slug, model_id))
    if resolved["cost"] is not None:
        return resolved
    return {"pricingStatus": "unknown", "cost": None}


def _dedupe_provider_rows(rows: List[Any]) -> List[dict]:
    """Every provider row once, native order preserved.

    Duplicate slugs (the installed picker can emit the same provider twice)
    keep the AUTHENTICATED row — the same preference ``_find_provider_row``
    applies — while holding the slug's first-seen position.
    """
    out: List[dict] = []
    index: Dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = _row_id(row).lower()
        if not slug:
            continue
        if slug not in index:
            index[slug] = len(out)
            out.append(row)
            continue
        existing = out[index[slug]]
        if existing.get("authenticated") is False and row.get("authenticated") is not False:
            out[index[slug]] = row
    return out


def _provider_warning(row: dict) -> Optional[str]:
    warning = row.get("warning")
    if warning is None:
        return None
    text = str(warning).strip()
    return text or None


def _provider_dto(row: dict, probe: Any, refresh: bool,
                  now: datetime) -> dict:
    """One provider row as the frozen DTO (no paths/URLs/raw config ever)."""
    slug = _row_id(row)
    ids = _model_ids(row)
    fetched_at, stale = _freshness(slug, refresh, now)
    pricing = row.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    # Raw cached per-token rates are read ONCE per provider, and only when the
    # picker actually exposed display pricing for this row.
    raw_pricing = probe.picker_pricing(slug) if pricing else {}
    models = []
    for model_id in ids:
        resolved = _cost_for_model(
            model_id, pricing.get(model_id),
            raw_pricing.get(model_id) if isinstance(raw_pricing, dict) else None,
            slug, probe)
        models.append({
            "id": model_id,
            "label": model_id,
            "pricingStatus": resolved["pricingStatus"],
            "cost": resolved["cost"],
        })
    return {
        "id": slug,
        "label": _row_name(row),
        "authenticated": row.get("authenticated") is not False,
        # A canonical skeleton row (include_unconfigured=True) has no credential
        # and no models; it is catalog furniture, not a configured provider.
        "configured": row.get("authenticated") is not False or bool(ids),
        "isCurrent": bool(row.get("is_current")),
        "modelCount": len(ids),
        "fetchedAt": fetched_at.isoformat() if fetched_at else None,
        "stale": bool(stale),
        "warning": _provider_warning(row),
        "models": models,
    }


def _degraded_provider_dto(row: dict, exc: BaseException) -> dict:
    """A provider whose row could not be built — visible, sanitized, isolated."""
    slug = _row_id(row)
    return {
        "id": slug,
        "label": _row_name(row) or slug,
        "authenticated": row.get("authenticated") is not False,
        "configured": False,
        "isCurrent": bool(row.get("is_current")),
        "modelCount": 0,
        "fetchedAt": None,
        "stale": True,
        "warning": f"provider catalog unavailable: {type(exc).__name__}",
        "models": [],
    }


def get_model_catalog_all(
    adapter: Any,
    profile_id: Optional[str],
    provider: Optional[str] = None,
    refresh: bool = False,
) -> dict:
    """Return the aggregate provider -> models -> cost catalog for a profile.

    See the module docstring for the frozen DTO. ``provider`` filters with the
    same identity logic as :func:`get_model_catalog` (slug/provider_id, case
    insensitive, authenticated duplicate preferred); omitting it enumerates
    every provider row exactly once in native order.

    Read-only: one ``adapter.scoped(profile_id)`` per request, one picker
    payload, an offline pricing probe, no journal/preset/state writes.
    """
    requested = str(provider or "").strip().lower()
    refresh = bool(refresh)
    now = datetime.now(timezone.utc)

    result: dict = {
        "ok": False,
        "profileId": profile_id if isinstance(profile_id, str) and profile_id else None,
        "providers": [],
        "cost": {"pending": False, "unavailableProviders": []},
        "error": None,
    }

    try:
        with adapter.scoped(profile_id):
            payload = _payload_for(adapter, refresh)
            rows = payload.get("providers") if isinstance(payload, dict) else None
            rows = _dedupe_provider_rows(rows if isinstance(rows, list) else [])
            if not rows:
                result["error"] = "no providers in this profile's catalog"
                return result

            probe = adapter.pricing_probe()
            unavailable = set()
            if not getattr(probe, "available", True):
                # Import probe failed (version skew): every price is unknown,
                # never guessed, and the affected providers are named.
                unavailable.update(_row_id(r) for r in rows if _row_id(r))
            pending = bool(payload.get("pricing_pending")) if isinstance(payload, dict) else False
            pending = pending or any(r.get("pricing_pending") is True for r in rows)

            if requested:
                row = _find_provider_row(rows, requested)
                if row is None:
                    result["error"] = (
                        f"provider '{requested}' is not available in this profile's "
                        "catalog (not configured or not authenticated)"
                    )
                    return result
                rows = [row]

            providers = []
            for row in rows:
                if row.get("pricing_pending") is True:
                    pending = True
                try:
                    providers.append(_provider_dto(row, probe, refresh, now))
                except Exception as exc:  # one bad row must not suppress the rest
                    providers.append(_degraded_provider_dto(row, exc))
                    slug = _row_id(row)
                    if slug:
                        unavailable.add(slug)

            result["providers"] = providers
            result["cost"] = {"pending": bool(pending),
                              "unavailableProviders": sorted(unavailable)}
            result["ok"] = True
            return result
    except Exception as exc:  # noqa: BLE001 — sanitized, never an empty success
        result["error"] = f"catalog lookup failed: {type(exc).__name__}"
        return result
