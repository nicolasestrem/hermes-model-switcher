"""Version-tested Hermes adapter — all version-sensitive Hermes imports and
explicit target-profile/credential scoping live here (plan T2).

Contract (from the accepted plan, evidence lines referenced):
- Resolve homes through Hermes APIs only (``hermes_cli.profiles``,
  ``hermes_constants.get_hermes_home``); never hardcode this installation's
  paths and never change global ``HERMES_HOME`` in request handlers
  (web_routers/models.py:138-168 uses ``_config_profile_scope`` the same way).
- Credential/secret scope follows the profile: reads and writes run inside
  ``hermes_cli.web_server_profiles._config_profile_scope`` so ``get_secret``
  resolves the scoped profile's ``.env`` and is fail-closed across profiles
  (agent/secret_scope.py; web_server_dashboard.py:812-851 boundary).
- Canonical route preparation reuses Hermes' own seams —
  ``web_server_config._prepare_main_assignment`` (models.py:376-427) and the
  canonical save path ``config.save_config`` / ``require_readable_config_before_write``
  (config.py:2064-2069, 2456-2498). No shell interpolation, no textual YAML
  replacement.
- Unsupported runtimes are READ-ONLY: anything below the tested baseline or a
  failed import never gains write access (web_server_config's code-skew refusal
  at models.py:151-154 is the in-core pattern this mirrors).
"""
from __future__ import annotations

import contextlib
import contextvars
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional


# The expected (pre-write) config revision is carried into the canonical lock
# guard WITHOUT changing the seam's call signature: the config adapter sets it
# for the duration of the unmodified apply_canonical_save() call, so a
# monkeypatched seam that keeps the historical (profile, updated, identity)
# signature still receives the guard. Callers may equally pass
# expected_revision= explicitly; an explicit value wins.
_EXPECTED_REVISION: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hermes_adapter_expected_revision", default=None)


@contextlib.contextmanager
def expected_revision_scope(revision: Optional[str]) -> Iterator[None]:
    token = _EXPECTED_REVISION.set(revision)
    try:
        yield
    finally:
        _EXPECTED_REVISION.reset(token)

# The inspected, tested baseline (PLAN.md "Evidence"): Hermes v0.21.7,
# commit 590af66a58c2b3eb3c5edd1dcfefa09c625ae18a.
SUPPORTED_BASELINE_VERSION = (0, 21, 7)
SUPPORTED_BASELINE_LABEL = "v0.21.7 (590af66a58c2b3eb3c5edd1dcfefa09c625ae18a)"

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class ReadOnlyRuntime(RuntimeError):
    """The running Hermes is not a supported baseline: writes are refused."""


class UnknownProfile(LookupError):
    """The requested profile does not exist in the discovered set."""


class ProfileIdentityChanged(RuntimeError):
    """The profile directory identity changed after discovery/preview."""


class StoreConflict(RuntimeError):
    """The store's revision changed underneath us — refuse, never force."""


class CatalogSeamUnavailable(RuntimeError):
    """The installed Hermes picker seam is missing (version skew).

    Raised by :meth:`HermesAdapter.catalog_payload`; callers surface it as a
    truthful sanitized failure (``ok=False``), never as an empty catalog.
    """


def _load_inventory():
    """Import the installed picker builder module (the ONE import site)."""
    from hermes_cli import inventory

    return inventory


def _load_pricing_seams() -> Dict[str, Any]:
    """Import every offline pricing seam this plugin reads (the ONE import site).

    Version skew must fail HERE, behind the adapter, so no other module carries
    a Hermes-private import and a missing attribute degrades to "unknown cost"
    rather than a 500.
    """
    from hermes_cli.models_pricing import get_pricing_for_provider
    from agent import usage_pricing
    from agent import models_dev

    resolve_billing_route = getattr(usage_pricing, "resolve_billing_route", None)
    get_pricing_entry = getattr(usage_pricing, "get_pricing_entry", None)
    get_model_info = getattr(models_dev, "get_model_info", None)
    for name, value in (
        ("resolve_billing_route", resolve_billing_route),
        ("get_pricing_entry", get_pricing_entry),
        ("get_model_info", get_model_info),
    ):
        if not callable(value):
            raise AttributeError(f"agent models seam missing {name}")
    return {
        "get_pricing_for_provider": get_pricing_for_provider,
        "resolve_billing_route": resolve_billing_route,
        "get_pricing_entry": get_pricing_entry,
        "get_model_info": get_model_info,
    }


def _float_or_none(value: Any) -> Optional[float]:
    """Nullable numeric seam value as a float; unparsable → None (never 0)."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _raw_models_dev_cost(provider: str, model: str) -> Optional[Dict[str, Any]]:
    """The RAW models.dev cost dict for provider/model, offline, or None.

    ``ModelInfo`` zero-fills missing costs (``has_cost_data()`` is only input>0
    or output>0), so a 0.0 rate cannot distinguish a free model from one with
    no catalog data. Presence of the registry's own ``cost`` mapping is the
    evidence; this probe never allows network access and never guesses.
    """
    try:
        from agent import models_dev

        resolve: Any = getattr(models_dev, "_models_dev_id", None)
        registry: Any = getattr(models_dev, "_registry_models", None)
        entries: Any = getattr(models_dev, "_iter_model_entries", None)
        if not (callable(resolve) and callable(registry) and callable(entries)):
            return None
        mdev_id = resolve(provider) or provider
        models = registry(mdev_id, allow_network=False)
        if not isinstance(models, dict):
            return None
        entry = next(
            (e for _mid, e in entries(
                models, model, suffix_fallback=False, provider=provider)),
            None)
        cost = entry.get("cost") if isinstance(entry, dict) else None
        if not isinstance(cost, dict) or not any(k in cost for k in ("input", "output")):
            return None
        return cost
    except Exception:
        return None


class PricingProbe:
    """Exception-safe, OFFLINE pricing probe over the installed Hermes rate sources.

    Every method answers from caches only: the picker probe passes
    ``cached_only=True``, billing routes are resolved WITHOUT ``base_url`` /
    ``api_key`` (those arguments enable provider metadata fetches), and
    models.dev is read with ``allow_network=False``. A seam that cannot answer
    returns ``{}`` / ``None``; callers must render that as ``unknown``, never as
    free or ``$0.00``. ``available`` is False only when the pricing imports
    themselves failed (version skew).
    """

    def __init__(self, *, available: bool = True, unavailable: tuple = (),
                 reason: Optional[str] = None,
                 picker: Optional[Any] = None, route: Optional[Any] = None,
                 entry: Optional[Any] = None, models_dev: Optional[Any] = None):
        self.available = bool(available)
        self.unavailable = tuple(unavailable)
        self.reason = reason
        self._picker = picker
        self._route = route
        self._entry = entry
        self._models_dev = models_dev

    def picker_pricing(self, provider: str) -> Dict[str, Any]:
        """Raw per-token pricing rows ``{model: {prompt, completion, ...}}``."""
        if self._picker is None:
            return {}
        try:
            rows = self._picker(str(provider or ""))
        except Exception:
            return {}
        return rows if isinstance(rows, dict) else {}

    def billing_mode(self, model: str, provider: str) -> Optional[str]:
        """``resolve_billing_route(...).billing_mode`` for provider-only lookup."""
        if self._route is None:
            return None
        try:
            mode = self._route(str(model or ""), str(provider or ""))
        except Exception:
            return None
        return str(mode) if mode else None

    def pricing_entry(self, model: str, provider: str) -> Optional[Dict[str, Any]]:
        """Official-docs snapshot entry as USD/1M numbers (provider-only call)."""
        if self._entry is None:
            return None
        try:
            entry = self._entry(str(model or ""), str(provider or ""))
        except Exception:
            return None
        if entry is None:
            return None
        return entry if isinstance(entry, dict) else None

    def models_dev_cost(self, provider: str, model: str) -> Optional[Dict[str, Any]]:
        """models.dev rate row (USD/1M) only when the catalog defines a cost dict."""
        if self._models_dev is None:
            return None
        try:
            cost = self._models_dev(str(provider or ""), str(model or ""))
        except Exception:
            return None
        return cost if isinstance(cost, dict) else None

    def __repr__(self) -> str:  # never include seam callables/errors
        return (f"PricingProbe(available={self.available!r}, "
                f"unavailable={self.unavailable!r}, reason={self.reason!r})")


@dataclass(frozen=True)
class RuntimeInfo:
    importable: bool
    version_label: str
    supported: bool
    read_only: bool
    reasons: tuple = field(default=())


def _version_tuple(label: str) -> Optional[tuple]:
    m = _VERSION_RE.search(label or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def probe_runtime() -> RuntimeInfo:
    """Import-test the version-sensitive seams and compare against the baseline."""
    reasons = []
    importable = True
    version_label = ""
    try:
        import hermes_cli  # noqa: F401  (version seam)
        from hermes_cli import config as _config  # noqa: F401  (canonical save seam)
        from hermes_cli import web_server_config  # noqa: F401  (assignment seam)
        version_label = str(getattr(hermes_cli, "__version__", "") or "")
    except Exception as exc:  # import failure can never gain write access
        importable = False
        reasons.append(f"hermes import failed: {exc!r}")
    vt = _version_tuple(version_label)
    if importable and vt is None:
        reasons.append(f"unparsable Hermes version {version_label!r}")
    supported = importable and vt is not None and vt >= SUPPORTED_BASELINE_VERSION and vt[0] == 0
    if importable and not supported:
        reasons.append(
            f"Hermes {version_label} outside tested baseline {SUPPORTED_BASELINE_LABEL}")
    return RuntimeInfo(
        importable=importable, version_label=version_label, supported=supported,
        read_only=not supported, reasons=tuple(reasons))


def _hermes():
    from hermes_cli import config as config_mod
    return config_mod


@dataclass(frozen=True)
class ProfileIdentity:
    """Cheap but spoof-resistant identity of a discovered profile directory."""
    name: str
    path: str
    fingerprint: str  # realpath + directory identity + marker presence


def discover_profiles() -> Dict[str, ProfileIdentity]:
    """All discovered profiles via Hermes' own discovery APIs (default home +
    every named profile). Read-only; never mutates global HERMES_HOME."""
    from hermes_cli.profiles import list_profile_names, get_profile_dir
    out: Dict[str, ProfileIdentity] = {}
    for name in list_profile_names():
        try:
            pdir = get_profile_dir(name)
        except Exception:
            continue
        out[name] = ProfileIdentity(name=name, path=str(pdir),
                                    fingerprint=_identity_fingerprint(name, pdir))
    return out


def _identity_fingerprint(name: str, pdir) -> str:
    import hashlib
    from pathlib import Path
    from hermes_cli.profiles import named_profile_has_identity
    h = hashlib.sha256()
    h.update(str(Path(pdir).resolve()).encode())
    stat = Path(pdir).stat()
    h.update(f"{stat.st_dev}:{stat.st_ino}".encode())
    try:
        marker_present = bool(named_profile_has_identity(pdir))
    except Exception:
        marker_present = False
    h.update(b"identity" if marker_present else b"no-identity")
    return h.hexdigest()[:16]


def _assert_identity(name: str, expected: ProfileIdentity) -> None:
    current = discover_profiles().get(name)
    if current is None or current.fingerprint != expected.fingerprint:
        raise ProfileIdentityChanged(
            f"profile {name!r} identity changed or vanished after review "
            "(the profile directory was recreated, moved, or restored from a "
            "backup); re-review the change before applying or undoing")


class HermesAdapter:
    """Explicitly-scoped Hermes access. Every call names its target profile;
    there is no implicit 'whatever home the process launched with' path."""

    def __init__(self, runtime: Optional[RuntimeInfo] = None):
        self.runtime = runtime or probe_runtime()
        self._write_lock = threading.RLock()

    # -- scoping -----------------------------------------------------
    @contextlib.contextmanager
    def scoped(self, profile: Optional[str]) -> Iterator[ProfileIdentity]:
        """Enter the profile's home AND secret scope through Hermes' own seam.

        Yields the resolved profile identity. Mutating callers must pass an
        explicit profile — ``None`` is only valid for read-only use against
        the process's own home (same rule as web_routers/_common.destructive_profile).
        """
        profiles = discover_profiles()
        if profile is None:
            # Read-only current-home scope (the dashboard's own profile).
            from tui_gateway.launch_profile_policy import launch_secret_scope
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
            ident = ProfileIdentity(name="default", path=str(home), fingerprint="current")
            from agent.secret_scope import set_secret_scope, reset_secret_scope
            token = set_secret_scope(launch_secret_scope(home), profile_home=str(home))
            try:
                yield ident
            finally:
                reset_secret_scope(token)
            return
        if profile not in profiles:
            raise UnknownProfile(profile)
        ident = profiles[profile]
        from hermes_cli.web_server_profiles import _config_profile_scope
        with _config_profile_scope(profile):
            _assert_identity(profile, ident)
            yield ident

    # -- reads -------------------------------------------------------
    def catalog_payload(self, refresh: bool = False) -> Any:
        """The installed picker payload for the CURRENT profile scope.

        Call inside :meth:`scoped` so the builder reads the requested profile's
        config and credential scope. ``hermes_cli.inventory`` is imported here
        and nowhere else in the plugin; version skew raises
        :class:`CatalogSeamUnavailable` (sanitized by the API layer) instead of
        surfacing an import traceback or an empty-success catalog.
        """
        try:
            inventory = _load_inventory()
            build = getattr(inventory, "build_model_options_payload", None)
            load_ctx = getattr(inventory, "load_picker_context", None)
            if not callable(build) or not callable(load_ctx):
                raise AttributeError("hermes_cli.inventory picker seam missing")
        except (ImportError, AttributeError) as exc:
            raise CatalogSeamUnavailable(type(exc).__name__) from None
        return build(
            load_ctx(),
            explicit_only=False,
            include_unconfigured=True,
            refresh=bool(refresh),
        )

    def pricing_probe(self) -> PricingProbe:
        """Build an OFFLINE pricing probe over the installed rate sources.

        Import failure (version skew) degrades to ``available=False`` with no
        callables: callers then report every price as ``unknown`` and list the
        affected providers in ``cost.unavailableProviders``. Never raises.
        """
        try:
            seams = _load_pricing_seams()
        except (ImportError, AttributeError) as exc:
            return PricingProbe(
                available=False,
                unavailable=("pricing",),
                reason=type(exc).__name__,
            )

        def _picker(provider: str) -> Dict[str, Any]:
            # cached_only=True: never start provider I/O on a read path.
            return seams["get_pricing_for_provider"](
                provider, cached_only=True) or {}

        def _route(model: str, provider: str) -> Optional[str]:
            # provider-only: base_url/api_key would enable a network metadata
            # fetch inside the resolver; the catalog must stay offline.
            route = seams["resolve_billing_route"](model, provider=provider)
            return getattr(route, "billing_mode", None)

        def _entry(model: str, provider: str) -> Optional[Dict[str, Any]]:
            entry = seams["get_pricing_entry"](model, provider=provider)
            if entry is None:
                return None

            def dec(name: str) -> Optional[float]:
                value = getattr(entry, name, None)
                return float(value) if value is not None else None

            return {
                "input": dec("input_cost_per_million"),
                "output": dec("output_cost_per_million"),
                "cache_read": dec("cache_read_cost_per_million"),
                "cache_write": dec("cache_write_cost_per_million"),
                "source": getattr(entry, "source", None),
                "pricing_version": getattr(entry, "pricing_version", None),
            }

        def _models_dev(provider: str, model: str) -> Optional[Dict[str, Any]]:
            info = seams["get_model_info"](provider, model, allow_network=False)
            if info is None:
                return None
            # ModelInfo defaults costs to 0.0, so 0.0 alone cannot distinguish
            # "free" from "no data". Presence is proven by a raw cost dict in
            # the offline registry or by parsed positive rates.
            raw_present = _raw_models_dev_cost(provider, model) is not None
            try:
                has_cost = bool(info.has_cost_data())
            except Exception:
                has_cost = False
            if not raw_present and not has_cost:
                return None
            return {
                "input": _float_or_none(getattr(info, "cost_input", None)),
                "output": _float_or_none(getattr(info, "cost_output", None)),
                "cache_read": _float_or_none(getattr(info, "cost_cache_read", None)),
            }

        return PricingProbe(
            available=True,
            picker=_picker,
            route=_route,
            entry=_entry,
            models_dev=_models_dev,
        )

    def load_raw_config(self, profile: Optional[str] = None) -> Dict[str, Any]:
        """Strict raw-config read inside the profile scope (no defaults merge;
        failed reads surface as errors, never as an empty config)."""
        with self.scoped(profile):
            cfg = _hermes().require_readable_config_before_write()
            import copy
            return copy.deepcopy(dict(cfg))

    def config_revision(self, profile: Optional[str] = None) -> str:
        """The canonical-config revision fingerprint: ``_rev`` of the strictly
        re-read config document. This is the EXACT domain the save guard in
        ``apply_canonical_save`` compares under the lock, so callers can carry
        a revision across the lock boundary and have it match."""
        return _rev(self.load_raw_config(profile))

    # -- canonical route preparation/validation (no writes) ----------
    def prepare_main_assignment(
            self, cfg: Dict[str, Any], provider: str, model: str,
            base_url: str = "", api_key: str = "") -> Dict[str, Any]:
        """Canonical validation half of POST /api/model/set (web_server_config.
        _prepare_main_assignment): returns {base_url, result} without writing."""
        self._assert_writable()
        from hermes_cli.web_server_config import _prepare_main_assignment
        base_url, result = _prepare_main_assignment(cfg, provider, model, base_url, api_key)
        return {"base_url": base_url, "result": result}

    def plan_canonical_save(self, profile: str, updated: Dict[str, Any]) -> Dict[str, Any]:
        """Dry-run of the canonical complete-state save (config.py:2456-2498):
        normalize + strip-defaults the way save_config would, and report the
        exact paths that would change — no lock, no write."""
        self._assert_writable()
        from hermes_cli import config as config_mod
        raw = self.load_raw_config(profile)
        stripped = config_mod._strip_default_values(
            config_mod._canonicalize_config(updated), config_mod.DEFAULT_CONFIG,
            preserve_keys=set(config_mod._explicit_config_paths(raw)))
        return {"planned_document": stripped}

    # -- writes ------------------------------------------------------
    def apply_canonical_save(self, profile: str, updated: Dict[str, Any],
                             identity: Optional[ProfileIdentity] = None,
                             expected_revision: Optional[str] = None
                             ) -> Dict[str, Any]:
        """Journalled-style single-profile canonical save inside Hermes' own
        profile scope + config lock, with an identity recheck against the
        discovered profile set and a post-write readback of the exact target.

        expected_revision: when given, the revision fingerprint of the freshly
        read config must still match, otherwise a StoreConflict is raised
        BEFORE any write (refuse-stale, never overwrite an intervening edit)."""
        self._assert_writable()
        if not profile:
            raise ValueError("explicit profile required for writes")
        if expected_revision is None:
            expected_revision = _EXPECTED_REVISION.get()
        if identity is not None:
            _assert_identity(profile, identity)
        config_mod = _hermes()
        with self.scoped(profile):
            with self._write_lock, config_mod._CONFIG_LOCK:
                # Re-assert the discovered identity INSIDE the lock: the
                # pre-lock check is only a fast fail; the guard that matters
                # must run under the same lock as the save.
                if identity is not None:
                    _assert_identity(profile, identity)
                fresh = config_mod.require_readable_config_before_write()
                if expected_revision is not None and _rev(fresh) != expected_revision:
                    raise StoreConflict(
                        f"profile {profile!r}: config changed since the revision "
                        f"was taken (current {(_rev(fresh))!r}, expected "
                        f"{expected_revision!r}) — refusing the save; re-read "
                        "and re-plan the edit")
                config_mod.save_config(updated)
                after = config_mod.require_readable_config_before_write()
                return {"ok": True, "profile": profile,
                        "revision_before": _rev(fresh), "revision_after": _rev(after)}

    # -- plugin auth/enablement gate (web_server.py:650-688 semantics) --
    def plugin_backend_allowed(self, plugin_name: str) -> bool:
        """Mirror of the runtime plugin gate: enabled set / disabled set from
        the config, unknown plugin = user plugin = blocked (safe default)."""
        from hermes_cli.plugins_cmd import _get_enabled_set, _get_disabled_set
        try:
            from hermes_cli.web_server import _get_dashboard_plugins
        except Exception:
            _get_dashboard_plugins = None  # no dashboard process: all names are user plugins
        name = (plugin_name or "").strip()
        if not name:
            return False
        if name in _get_disabled_set():
            return False
        if _get_dashboard_plugins is not None:
            plugin = next(
                (p for p in _get_dashboard_plugins() if p.get("name") == name), None)
            source = plugin.get("source") if plugin else "user"
        else:
            source = "user"
        if source == "user" and name not in _get_enabled_set():
            return False
        return True

    def _assert_writable(self) -> None:
        if self.runtime.read_only:
            raise ReadOnlyRuntime(
                "unsupported Hermes runtime; adapter is read-only: "
                + "; ".join(self.runtime.reasons))


def _rev(cfg: Dict[str, Any]) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:16]
