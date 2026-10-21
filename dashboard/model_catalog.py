"""Provider-aware model catalog for the dashboard — a PURE TRANSFORM.

Input is the HOST OPTIONS PAYLOAD: the JSON body of the host's

    GET /api/model/options?profile=<p>&include_unconfigured=1[&refresh=1]

route (built by ``hermes_cli.inventory.build_model_options_payload`` at the
tested revision: ``{providers: [...], model, provider}``).  This module imports
NOTHING from Hermes — no picker builder, no pricing seam, no disk cache — so it
cannot drift with a private Hermes import and it cannot start provider I/O.
The caller fetches the payload from the host and passes it in.

Shape relied on (defensively read; unknown/absent fields stay unknown):

    payload["providers"][i] = {
      "slug": str, "provider_id": str|absent, "name": str,
      "models": [str, ...], "total_models": int,
      "source": str, "is_current": bool, "is_user_defined": bool,
      "authenticated": bool,            # explicit False on skeleton rows
      "warning": str|None,              # e.g. "paste KEY to activate"
      "pricing": {model_id: {"input": str|None, "output": str|None,
                             "cache": str|None, "free": bool}},   # display text
      "pricing_pending": bool,          # host has not resolved prices yet
      "free_tier": bool, "free_tier_pending": bool,
      "unavailable_models": [str, ...],
    }

Contract of :func:`models_response` (frozen ``/models`` DTO)::

    models_response(options_payload, provider="", profile_id=None,
                    refresh=False) -> dict

    {
      "ok":        bool,   # True iff a provider row resolved for `provider`
      "provider":  str,    # native slug when resolved, else the request
      "models":    [{"id": str, "name": str}, ...],   # exact native ids
      "fetchedAt": None,   # the host payload carries no fetch timestamp
      "source":    str,    # picker row provenance ("canonical", "hermes", ...)
      "stale":     bool,   # True: freshness is unproven without a timestamp
      "error":     str|None,
      "providers": [{"id": str, "name": str}, ...],   # catalog index
    }

Contract of :func:`catalog_response` (frozen ``/catalog`` DTO)::

    catalog_response(options_payload, provider="", profile_id=None,
                     refresh=False) -> dict

    {
      "ok": bool, "profileId": str|None,
      "providers": [{
        "id": str, "label": str, "authenticated": bool, "configured": bool,
        "isCurrent": bool, "modelCount": int, "fetchedAt": None,
        "stale": bool, "warning": str|None,
        "models": [{"id": str, "label": str,
                    "pricingStatus": "priced"|"free"|"included"|"unknown",
                    "cost": {unit, input, output, cacheRead, display{input,
                             output, cache}, free, source, pricingVersion}|None}],
      }],
      "cost": {"pending": bool, "unavailableProviders": [str]},
      "error": str|None,
    }

Pricing truthfulness (the host payload is the ONLY source):

  * Display strings (``$x.xx`` / ``free`` / ``null``) pass through verbatim.
  * Numeric per-1M rates are NOT in the host payload, so ``cost.input`` /
    ``cost.output`` / ``cost.cacheRead`` are always ``None`` — never parsed out
    of ``$`` text, never guessed.  ``pricingVersion`` is likewise ``None``; the
    DTO's ``source`` reports where THIS module got the price ("picker"), never
    where the host originally resolved it.
  * ``free`` requires the host's explicit both-directions-free flag; a
    per-direction "free" string is a zero for that direction only.
  * A missing/empty pricing entry, and every model of a ``pricing_pending``
    row, is ``pricingStatus: "unknown"`` with ``cost: None`` — NEVER free and
    never ``$0.00``.  The legacy ``"included"`` status (subscription route) is
    unreachable: the host payload carries no billing-route evidence.
  * ``cost.pending`` is true when a row in THIS response is ``pricing_pending``
    (or its Nous entitlement is still ``free_tier_pending`` — every model of
    that row is locked until the host resolves it).
  * ``cost.unavailableProviders`` names providers in THIS response whose models
    came with no usable pricing at all (not merely pending).

Freshness: the host payload carries no fetch timestamp, so ``fetchedAt`` is
``None`` and ``stale`` is ``True`` ("unproven fresh") for every provider.  This
module never invents a timestamp.  ``refresh`` is accepted for call-site
compatibility and ignored: refreshing is the HOST's job (``&refresh=1`` on its
GET), never this transform's.

Truthfulness:
  * ``options_payload is None`` or malformed (not a mapping, no ``providers``
    list) -> ``ok=False`` with the fixed reason ``"hostCatalogUnavailable"`` —
    never an empty success.
  * A valid payload with zero rows is ``ok=False`` ("no providers in this
    profile's catalog").
  * ``ok`` is ``True`` only when a row for the requested provider was found
    (even with zero models); an absent provider is a truthful ``ok=False``.
  * ``error`` never carries exception text, paths, URLs or secrets.

Isolation: no writes, no secret reads, no Hermes import, no network.  Every
input is whatever the caller already fetched.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "catalog_response",
    "models_response",
    "provider_authenticated",
    "HOST_UNAVAILABLE",
    "RESULT_KEYS",
    "CATALOG_RESULT_KEYS",
    "PROVIDER_KEYS",
    "MODEL_KEYS",
    "COST_KEYS",
    "COST_DISPLAY_KEYS",
    "PRICING_STATUSES",
    "COST_SOURCES",
    "COST_UNIT",
]

#: Fixed, non-leaking failure reason for a missing/malformed host payload.
HOST_UNAVAILABLE = "hostCatalogUnavailable"

#: Keys always present on the ``/models`` DTO.
RESULT_KEYS = ("ok", "provider", "models", "fetchedAt", "source", "stale",
               "error", "providers")

# -- aggregate catalog DTO (frozen; the dashboard UI builds against it) ------
CATALOG_RESULT_KEYS = ("ok", "profileId", "providers", "cost", "error")
PROVIDER_KEYS = ("id", "label", "authenticated", "configured", "isCurrent",
                 "modelCount", "fetchedAt", "stale", "warning", "models")
MODEL_KEYS = ("id", "label", "pricingStatus", "cost")
COST_KEYS = ("unit", "input", "output", "cacheRead", "display", "free",
             "source", "pricingVersion")
COST_DISPLAY_KEYS = ("input", "output", "cache")
PRICING_STATUSES = ("priced", "free", "included", "unknown")
# Closed vocabulary of the DTO's ``cost.source``.  Only ``"picker"`` is
# producible from a host options payload; the rest are retained so consumers
# validating against this tuple keep working.
COST_SOURCES = ("picker", "official_docs_snapshot", "models_dev", "subscription_route")
COST_UNIT = "USD_per_1M_tokens"

_UNKNOWN_COST: Dict[str, Any] = {"pricingStatus": "unknown", "cost": None}


def _unknown_cost() -> dict:
    return {"pricingStatus": _UNKNOWN_COST["pricingStatus"], "cost": None}


# -- defensive readers for the host payload --------------------------------


def _payload_rows(payload: Any) -> Optional[List[Any]]:
    """The provider row list, or ``None`` when the payload is unusable.

    ``None``/non-mapping payloads and a missing/non-list ``providers`` key are
    malformed: the caller must report ``hostCatalogUnavailable`` instead of an
    empty success.
    """
    if not isinstance(payload, dict):
        return None
    rows = payload.get("providers")
    if not isinstance(rows, list):
        return None
    return rows


def _row_id(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("slug") or row.get("provider_id") or "").strip()


def _row_name(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("name") or _row_id(row)).strip()


def _row_identifiers(row: Any) -> set:
    """Every case-insensitive id a row answers to (``slug`` / ``provider_id``)."""
    if not isinstance(row, dict):
        return set()
    return {
        str(row.get("slug") or "").strip().lower(),
        str(row.get("provider_id") or "").strip().lower(),
    }


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


def _find_provider_row(rows: List[Any], provider: str) -> Optional[dict]:
    """First row matching ``provider`` by slug/provider_id, preferring an authenticated one."""
    wanted = str(provider or "").strip().lower()
    if not wanted:
        return None
    fallback: Optional[dict] = None
    for row in rows:
        if not isinstance(row, dict) or wanted not in _row_identifiers(row):
            continue
        if row.get("authenticated") is not False:
            return row
        if fallback is None:
            fallback = row
    return fallback


def _dedupe_provider_rows(rows: List[Any]) -> List[dict]:
    """Every provider row once, native order preserved.

    Duplicate slugs (the host picker can emit the same provider twice, e.g. an
    alias row beside its canonical twin) keep the AUTHENTICATED row — the same
    preference :func:`_find_provider_row` applies — while holding the slug's
    first-seen position.
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


# -- pricing: display text only, never a parsed number ----------------------


def _display_string(value: Any) -> Optional[str]:
    """Host display string verbatim (``$x.xx`` / ``free``), else ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pricing_display(entry: dict) -> dict:
    return {
        "input": _display_string(entry.get("input")),
        "output": _display_string(entry.get("output")),
        "cache": _display_string(entry.get("cache")),
    }


def _display_only_cost(display: dict, free: bool) -> dict:
    """Cost DTO from the host's own display strings (no numeric rates).

    ``free`` requires the host's explicit both-directions-free flag: a
    per-direction ``"free"`` is a zero for THAT direction only, so
    ``free input + $5.00 output`` stays ``priced``, and an all-``free`` display
    without the flag remains ``unknown`` (zero cannot prove a free model).
    Numeric fields stay ``None``: display text is never parsed into a number.
    """
    has_price_display = any(
        isinstance(value, str) and value.strip().startswith("$")
        for value in display.values())
    if not free and not has_price_display:
        return _unknown_cost()
    return {
        "pricingStatus": "free" if free else "priced",
        "cost": {
            "unit": COST_UNIT,
            "input": None,
            "output": None,
            "cacheRead": None,
            "display": display,
            "free": bool(free),
            "source": "picker",
            "pricingVersion": None,
        },
    }


def _model_cost(row: dict, model_id: str) -> dict:
    """``{pricingStatus, cost}`` for one model, from this row's pricing map only."""
    if row.get("pricing_pending") is True or row.get("free_tier_pending") is True:
        # The host has not resolved this provider's prices: every model is
        # unknown, even if a stale map is still attached.
        return _unknown_cost()
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return _unknown_cost()
    entry = pricing.get(model_id)
    if not isinstance(entry, dict):
        return _unknown_cost()
    return _display_only_cost(_pricing_display(entry), entry.get("free") is True)


def _pricing_unavailable(row: dict, ids: List[str]) -> bool:
    """Whether this provider's models arrived with no usable pricing at all.

    A pending row is awaiting prices (``cost.pending``), which is a different,
    temporary state; the host's virtual free-tier row deliberately carries no
    pricing, so it is never reported as unavailable.
    """
    if not ids or row.get("free_tier_row"):
        return False
    if row.get("pricing_pending") is True or row.get("free_tier_pending") is True:
        return False
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return True
    return not any(isinstance(pricing.get(model_id), dict) for model_id in ids)


# -- DTO builders -----------------------------------------------------------


def _provider_dto(row: dict) -> dict:
    """One provider row as the frozen DTO (no paths/URLs/raw config ever)."""
    slug = _row_id(row)
    ids = _model_ids(row)
    models = []
    for model_id in ids:
        resolved = _model_cost(row, model_id)
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
        # A canonical skeleton row (include_unconfigured=1) has no credential
        # and no models; it is catalog furniture, not a configured provider.
        "configured": row.get("authenticated") is not False or bool(ids),
        "isCurrent": bool(row.get("is_current")),
        "modelCount": len(ids),
        # The host payload carries no fetch timestamp: freshness is unproven.
        "fetchedAt": None,
        "stale": True,
        "warning": _provider_warning(row),
        "models": models,
    }


def _degraded_provider_dto(row: dict, exc: BaseException) -> dict:
    """A provider whose row could not be shaped — visible, sanitized, isolated."""
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


def _profile_id(profile_id: Any) -> Optional[str]:
    return profile_id if isinstance(profile_id, str) and profile_id else None


def catalog_response(
    options_payload: Any,
    provider: str = "",
    profile_id: Optional[str] = None,
    refresh: bool = False,
) -> dict:
    """Return the aggregate provider -> models -> cost catalog for a profile.

    ``options_payload`` is the host's ``GET /api/model/options`` body for that
    profile (``None`` when the host call failed).  ``profile_id`` is echoed as
    the DTO's ``profileId`` only.  ``refresh`` is accepted and ignored: the
    host owns refreshing.

    See the module docstring for the frozen DTO and the pricing rules.
    """
    requested = str(provider or "").strip().lower()

    result: dict = {
        "ok": False,
        "profileId": _profile_id(profile_id),
        "providers": [],
        "cost": {"pending": False, "unavailableProviders": []},
        "error": None,
    }

    rows = _payload_rows(options_payload)
    if rows is None:
        result["error"] = HOST_UNAVAILABLE
        return result

    rows = _dedupe_provider_rows(rows)
    if not rows:
        result["error"] = "no providers in this profile's catalog"
        return result

    if requested:
        row = _find_provider_row(rows, requested)
        if row is None:
            result["error"] = (
                f"provider '{requested}' is not available in this profile's "
                "catalog (not configured or not authenticated)"
            )
            return result
        rows = [row]

    # Aggregate flags describe THIS response: a single-provider filter must
    # never inherit another provider's pending/unavailable state, so they are
    # computed AFTER the filter.
    pending = any(
        r.get("pricing_pending") is True or r.get("free_tier_pending") is True
        for r in rows)
    if not requested and isinstance(options_payload, dict) and options_payload.get("pricing_pending"):
        pending = True

    unavailable = set()
    providers = []
    for row in rows:
        try:
            dto = _provider_dto(row)
        except Exception as exc:  # one bad row must not suppress the rest
            dto = _degraded_provider_dto(row, exc)
            if dto["id"]:
                unavailable.add(dto["id"])
        else:
            if _pricing_unavailable(row, _model_ids(row)) and dto["id"]:
                unavailable.add(dto["id"])
        if not dto["id"]:
            continue
        providers.append(dto)

    result["providers"] = providers
    result["cost"] = {"pending": bool(pending),
                      "unavailableProviders": sorted(unavailable)}
    result["ok"] = True
    return result


def models_response(
    options_payload: Any,
    provider: str = "",
    profile_id: Optional[str] = None,
    refresh: bool = False,
) -> dict:
    """Return the provider-aware model list (frozen ``/models`` DTO).

    ``options_payload`` is the host's ``GET /api/model/options`` body for the
    profile the route resolved (``None`` when that host call failed).
    ``profile_id`` and ``refresh`` are accepted for one uniform call site and
    have no effect here (no scoping and no fetching happen in this transform).
    """
    requested = str(provider or "").strip().lower()

    result: dict = {
        "ok": False,
        "provider": requested,
        "models": [],
        "fetchedAt": None,
        "source": "unknown",
        "stale": True,
        "error": None,
        "providers": [],
    }

    if not requested:
        result["error"] = "no provider specified"
        return result

    rows = _payload_rows(options_payload)
    if rows is None:
        result["error"] = HOST_UNAVAILABLE
        return result
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
    result["provider"] = native_slug
    result["models"] = [{"id": m, "name": m} for m in ids]
    source = row.get("source")
    result["source"] = str(source) if source else "unknown"
    result["ok"] = True
    if ids:
        result["error"] = _provider_warning(row)
    else:
        result["error"] = (
            _provider_warning(row) or f"provider '{native_slug}' reported no models"
        )
    return result


def provider_authenticated(options_payload: Any, provider: str) -> Optional[bool]:
    """Credential evidence for ``provider`` from the host options payload.

    Scans ALL matching rows by ``slug`` or ``provider_id`` (case-insensitive):
    True if any row says True, False if any row says an explicit False, else
    None (provider unknown to the catalog, no payload, or no explicit flag).
    None is never treated as authenticated — the caller decides that case.
    """
    wanted = str(provider or "").strip().lower()
    if not wanted:
        return None
    rows = _payload_rows(options_payload)
    if rows is None:
        return None
    explicit_false = False
    for row in rows:
        if not isinstance(row, dict) or wanted not in _row_identifiers(row):
            continue
        value = row.get("authenticated")
        if value is True:
            return True
        if value is False:
            explicit_false = True
    return False if explicit_false else None
