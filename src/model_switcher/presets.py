"""Reviewed presets and billing approval policy (T5).

A saved preset is a *reviewed* intent: exact profile identities, route
assignments, slot families and a billing policy. Subsequent Apply is
one click — no confirmation modal — but preflight still runs on every
click and blocks (never silently substitutes) when:
  - a preset target profile is missing credentials,
  - a route's cost class is unknown,
  - the preset's billing policy does not approve a route's cost class,
  - a cost class changed after review (unapproved change),
  - a new profile exists that is not in the frozen review set (it never
    silently joins; it appears as not-included until reviewed).

All blocking happens BEFORE any write: preflight is pure.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Cost classes a catalog route can carry.
COST_SUBSCRIPTION = "subscription_included"
COST_METERED = "metered"
COST_UNKNOWN = "unknown"

ALL_COST_CLASSES = (COST_SUBSCRIPTION, COST_METERED, COST_UNKNOWN)


class PresetBlocked(RuntimeError):
    """Preflight refused the apply. No writes were started."""


@dataclass
class RouteTarget:
    """One route assignment inside a preset."""
    profile: str
    store_kind: str            # config | cron | plugin
    path: str                  # file path of the owning store
    changes: Dict[str, Any]    # dotted storage path -> value
    cost_class: str            # one of ALL_COST_CLASSES at review time


@dataclass
class BillingPolicy:
    """Explicit billing policy bounding one-click approval."""
    approved_classes: List[str] = field(default_factory=list)
    allow_unknown: bool = False  # unknown-cost routes need explicit ack


@dataclass
class ReviewedPreset:
    """Saved preset: frozen reviewed identities, never auto-expanding."""
    preset_id: str
    name: str
    billing_policy: BillingPolicy
    targets: List[RouteTarget]
    reviewed_profile_ids: List[str]  # frozen at review time

    # -- preflight (pure, runs before any write) ----------------------
    def preflight(self, credentials: Dict[str, bool],
                  live_cost_classes: Dict[str, str],
                  discovered_profiles: Optional[List[str]] = None
                  ) -> List[str]:
        """Return list of blocking reasons; empty = allowed.

        credentials: profile -> has required provider credentials
        live_cost_classes: route key -> current cost class
        discovered_profiles: profiles that now exist in the installation
        """
        reasons: List[str] = []

        # new profiles never silently join
        if discovered_profiles is not None:
            for p in discovered_profiles:
                if p not in self.reviewed_profile_ids:
                    reasons.append(
                        f"new profile {p!r} is not included in preset review; "
                        "review and re-save the preset to include it")

        for t in self.targets:
            key = _route_key(t)
            # missing credentials block before writes
            if not credentials.get(t.profile, False):
                reasons.append(
                    f"profile {t.profile!r} is missing provider credentials "
                    f"for store {t.store_kind}")
            # unknown cost is never free — unless the preset's billing policy
            # explicitly acknowledges unknown-cost routes; when allowed it
            # produces no reason and no cost-class-mismatch false positive.
            live = live_cost_classes.get(key, t.cost_class)
            if live == COST_UNKNOWN:
                if not self.billing_policy.allow_unknown:
                    reasons.append(
                        f"route {key} has unknown cost; unknown cost is never "
                        "treated as free or approved")
                continue  # allow_unknown: no reason, no class-change check
            # unapproved cost change blocks
            if live != t.cost_class:
                reasons.append(
                    f"route {key} cost changed after review: reviewed "
                    f"{t.cost_class!r}, now {live!r}; not approved")
            elif t.cost_class not in self.billing_policy.approved_classes:
                reasons.append(
                    f"route {key} cost class {t.cost_class!r} is not in "
                    "the preset's approved billing policy")
        return reasons


def _route_key(t: RouteTarget) -> str:
    fields = ",".join(f"{k}={v}" for k, v in sorted(t.changes.items()))
    return f"{t.profile}:{t.store_kind}:{fields}"


class PresetStore:
    """Persists reviewed presets (no credentials, no backups, no
    user-private data beyond the reviewed intent itself)."""

    def __init__(self):
        self._presets: Dict[str, ReviewedPreset] = {}

    def save(self, preset: ReviewedPreset) -> None:
        self._presets[preset.preset_id] = copy.deepcopy(preset)

    def get(self, preset_id: str) -> ReviewedPreset:
        if preset_id not in self._presets:
            raise PresetBlocked(f"unknown preset {preset_id!r}")
        return self._presets[preset_id]

    def apply(self, preset_id: str) -> List[Dict[str, Any]]:
        """One-click apply: return the preset's journalled write targets.

        This is the one-click contract: no confirmation step here. The
        caller is expected to have run preflight() and refused on
        non-empty reasons (refusal performs no writes)."""
        preset = self.get(preset_id)
        return [
            {
                "profile": t.profile,
                "store_kind": t.store_kind,
                "path": t.path,
                "changes": copy.deepcopy(t.changes),
            }
            for t in preset.targets
        ]
