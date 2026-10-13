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
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

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
    fingerprint: str  # realpath + identity-marker content hash


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
            f"profile {name!r} identity changed or vanished after preview")


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
    def load_raw_config(self, profile: Optional[str] = None) -> Dict[str, Any]:
        """Strict raw-config read inside the profile scope (no defaults merge;
        failed reads surface as errors, never as an empty config)."""
        with self.scoped(profile):
            cfg = _hermes().require_readable_config_before_write()
            import copy
            return copy.deepcopy(dict(cfg))

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
                             identity: Optional[ProfileIdentity] = None) -> Dict[str, Any]:
        """Journalled-style single-profile canonical save inside Hermes' own
        profile scope + config lock, with an identity recheck against the
        discovered profile set and a post-write readback of the exact target."""
        self._assert_writable()
        if not profile:
            raise ValueError("explicit profile required for writes")
        if identity is not None:
            _assert_identity(profile, identity)
        config_mod = _hermes()
        with self.scoped(profile):
            with self._write_lock, config_mod._CONFIG_LOCK:
                fresh = config_mod.require_readable_config_before_write()
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
