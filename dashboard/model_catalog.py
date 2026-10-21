"""Pure catalog transform of GET /api/model/options payloads; no Hermes I/O.

Missing/malformed payloads fail with hostCatalogUnavailable, never an empty
success. When the UI declares the host call failed with its fixed
``hostRestartRequired`` token (the host's 503 code-skew guard), the DTO's
``error`` names that instead — the token is a closed-vocabulary category the
plugin chose, never host response text. Provider aliases match
case-insensitively; model IDs match exactly.
Authenticated rows take precedence over unauthenticated duplicate rows.

Only exact Hermes $digits[.digits] display tokens yield numeric per-million
rates; exact "free" yields a directional zero. Unknown stays None. Model-level
free status still requires the host's explicit flag. Pending pricing or
entitlement makes all affected models unknown, even with a stale pricing map.
No host billing-route evidence means no subscription-included classification.

Display text is retained; source is "picker", not a claim about the host's
upstream source. The host supplies no timestamp or pricing version, so these
remain None and freshness is unproven. Refresh belongs to the caller's host
request, not this transform. DTO key constants define the public allowlists.
"""

from __future__ import annotations

import math
import re

from typing import Any, Dict, List, Optional

__all__ = [
    "catalog_response",
    "models_response",
    "provider_authenticated",
    "HOST_UNAVAILABLE",
    "HOST_RESTART_REQUIRED",
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

#: Fixed failure reason for the host's 503 code-skew guard: the running
#: dashboard process predates the checkout on disk, so ``/api/model/options``
#: refuses with ``detail="Restart required: …"`` (upstream
#: ``hermes_cli/web_routers/models.py`` `_dashboard_code_skew_guard`, introduced
#: by commit f293e7206b, 2026-08-14, #86207 — i.e. the guard predates this
#: plugin's Hermes pin). A restart fixes it; ``hostCatalogUnavailable`` alone
#: never told the user that. The UI classifies the host detail prefix and
#: forwards ONLY this fixed token — never the host's sentence.
HOST_RESTART_REQUIRED = "hostRestartRequired"

#: Closed vocabulary the plugin backend accepts for a caller-declared options
#: failure. Anything else is ignored (fail-soft back to HOST_UNAVAILABLE).
_OPTIONS_FAILURE_CATEGORIES = (HOST_RESTART_REQUIRED,)

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


# -- pricing ---------------------------------------------------------------


_PRICE_PER_MTOK = re.compile(r"\$\d+(\.\d+)?")


def _parse_price_per_mtok(value: Any) -> Optional[float]:
    """Parse only Hermes' exact per-million display tokens; unknown is not zero."""
    if value == "free":
        return 0.0
    if not isinstance(value, str) or _PRICE_PER_MTOK.fullmatch(value) is None:
        return None
    price = float(value[1:])
    return price if math.isfinite(price) else None


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


def _display_cost(entry: dict) -> dict:
    """Keep model-level free status separate from per-direction numeric rates."""
    display = _pricing_display(entry)
    free = entry.get("free") is True
    has_price_display = any(
        isinstance(value, str) and value.strip().startswith("$")
        for value in display.values())
    if not free and not has_price_display:
        return _unknown_cost()
    return {
        "pricingStatus": "free" if free else "priced",
        "cost": {
            "unit": COST_UNIT,
            "input": _parse_price_per_mtok(entry.get("input")),
            "output": _parse_price_per_mtok(entry.get("output")),
            "cacheRead": _parse_price_per_mtok(entry.get("cache")),
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
    return _display_cost(entry)


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
    options_failure: Optional[str] = None,
) -> dict:
    """Return the aggregate provider -> models -> cost catalog for a profile.

    ``options_payload`` is the host's ``GET /api/model/options`` body for that
    profile (``None`` when the host call failed).  ``profile_id`` is echoed as
    the DTO's ``profileId`` only.  ``refresh`` is accepted and ignored: the
    host owns refreshing.  ``options_failure`` is the caller's (UI's) fixed
    classification of WHY the host call failed — only the closed-vocabulary
    ``hostRestartRequired`` token is honoured, and only when the payload is
    absent; it is a category the plugin chose, never host response text.

    See the module docstring for the frozen DTO and the pricing rules.
    """
    requested = str(provider or "").strip().lower()
    declared_failure = (
        options_failure if options_failure in _OPTIONS_FAILURE_CATEGORIES else None)

    result: dict = {
        "ok": False,
        "profileId": _profile_id(profile_id),
        "providers": [],
        "cost": {"pending": False, "unavailableProviders": []},
        "error": None,
    }

    rows = _payload_rows(options_payload)
    if rows is None:
        result["error"] = (
            HOST_RESTART_REQUIRED if declared_failure else HOST_UNAVAILABLE)
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
    options_failure: Optional[str] = None,
) -> dict:
    """Return the provider-aware model list (frozen ``/models`` DTO).

    ``options_payload`` is the host's ``GET /api/model/options`` body for the
    profile the route resolved (``None`` when that host call failed).
    ``profile_id`` and ``refresh`` are accepted for one uniform call site and
    have no effect here (no scoping and no fetching happen in this transform).
    ``options_failure`` follows ``catalog_response``: only the fixed
    ``hostRestartRequired`` token is honoured, and only without a payload.
    """
    requested = str(provider or "").strip().lower()
    declared_failure = (
        options_failure if options_failure in _OPTIONS_FAILURE_CATEGORIES else None)

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
        result["error"] = (
            HOST_RESTART_REQUIRED if declared_failure else HOST_UNAVAILABLE)
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
