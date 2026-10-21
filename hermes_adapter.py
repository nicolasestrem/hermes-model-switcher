"""Version-tolerant Hermes adapter — built ONLY on Hermes' public plugin seams.

Every version-sensitive Hermes import in this plugin lives here. The imports
this module makes are the ones Hermes documents for plugin authors
(``hermes_cli/plugin_validate_private_imports.py`` ``_PUBLIC``):

- ``hermes_cli.config`` — ``load_config``, ``save_config``,
  ``require_readable_config_before_write``, ``read_raw_config``,
  ``get_config_path``, ``DEFAULT_CONFIG``.
- ``hermes_constants`` — home/root resolution, the context-local home override,
  and named-profile identity/liveness helpers.
- ``agent.secret_scope`` — ``get_secret``, ``set_secret_scope``,
  ``reset_secret_scope``, ``build_profile_secret_scope``.

No private attribute is read through a public module (no ``_CONFIG_LOCK``, no
``_strip_default_values``, no ``_config_profile_scope``): the adapter scopes a
profile by installing the public context-local home override plus that
profile's secret scope, and serializes its own canonical saves with its own
re-entrant lock.

Contract:
- Homes resolve through public ``hermes_constants`` helpers only; global
  ``HERMES_HOME`` is NEVER mutated in a request handler — the context-local
  override is used instead (``set_hermes_home_override``).
- The credential scope follows the profile: ``build_profile_secret_scope`` over
  the profile's own ``.env``, installed with ``set_secret_scope`` so
  ``get_secret`` resolves the scoped profile and can never read another
  profile's value.
- Canonical saves go through ``config.save_config`` /
  ``require_readable_config_before_write``. No shell interpolation, no textual
  YAML replacement.
- Unsupported runtimes are READ-ONLY: the capability probe must find every
  required public seam importable and callable, otherwise no write path is
  opened. Nothing here depends on ``install-stamp.json`` or a version string.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


# The expected (pre-write) config revision is carried into the canonical save
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


# ---------------------------------------------------------------------------
# Capability probe: the public seams this plugin requires.
#
# `supported` is decided by CAPABILITY, never by a version string or a build
# stamp: a Hermes that exports every seam below is writable no matter what
# `hermes_cli.__version__` / a distribution's metadata reports, and a Hermes
# that is missing one is read-only no matter what version it claims.
# ---------------------------------------------------------------------------
CONFIG_CALLABLE_SEAMS = (
    "load_config",
    "save_config",
    "require_readable_config_before_write",
    "read_raw_config",
    "get_config_path",
)
CONFIG_VALUE_SEAMS = ("DEFAULT_CONFIG",)

CONSTANTS_CALLABLE_SEAMS = (
    "get_hermes_home",
    "get_default_hermes_root",
    "set_hermes_home_override",
    "reset_hermes_home_override",
    "get_hermes_home_override",
    "named_profile_has_identity",
    "named_profile_is_live",
    "named_profile_is_deleted",
    "profile_name_for_home",
    "named_profile_home",
)
# The canonical named-profile grammar; profile discovery mirrors it exactly.
CONSTANTS_VALUE_SEAMS = ("PROFILE_ID_RE",)

SECRET_SCOPE_CALLABLE_SEAMS = (
    "get_secret",
    "set_secret_scope",
    "reset_secret_scope",
    "build_profile_secret_scope",
)


class ReadOnlyRuntime(RuntimeError):
    """The running Hermes lacks the required public seams: writes are refused."""


class UnknownProfile(LookupError):
    """The requested profile does not exist in the discovered set."""


class ProfileIdentityChanged(RuntimeError):
    """The profile directory identity changed after discovery/preview."""


class StoreConflict(RuntimeError):
    """The store's revision changed underneath us — refuse, never force."""


def _version_label() -> str:
    """Best-effort, INFORMATIONAL Hermes package version ("" when unknown).

    Never used for gating: a git checkout without distribution metadata reports
    nothing here and is still fully writable.
    """
    try:
        from importlib.metadata import version

        return str(version("hermes-agent") or "")
    except Exception:
        return ""


def _import_seam_modules() -> Dict[str, Any]:
    """Import the public seam modules by name, one report per failure."""
    modules: Dict[str, Any] = {}
    errors: list = []
    try:
        from hermes_cli import config as config_mod

        modules["config"] = config_mod
    except Exception as exc:
        errors.append(f"hermes_cli.config import failed: {type(exc).__name__}")
    try:
        import hermes_constants as constants_mod

        modules["constants"] = constants_mod
    except Exception as exc:
        errors.append(f"hermes_constants import failed: {type(exc).__name__}")
    try:
        from agent import secret_scope as secret_scope_mod

        modules["secret_scope"] = secret_scope_mod
    except Exception as exc:
        errors.append(f"agent.secret_scope import failed: {type(exc).__name__}")
    return {"modules": modules, "errors": errors}


def _missing_seams(modules: Dict[str, Any]) -> list:
    """Every required seam a successfully imported module fails to expose."""
    missing: list = []
    for module_name, module, callables, values in (
        ("hermes_cli.config", modules.get("config"), CONFIG_CALLABLE_SEAMS,
         CONFIG_VALUE_SEAMS),
        ("hermes_constants", modules.get("constants"), CONSTANTS_CALLABLE_SEAMS,
         CONSTANTS_VALUE_SEAMS),
        ("agent.secret_scope", modules.get("secret_scope"),
         SECRET_SCOPE_CALLABLE_SEAMS, ()),
    ):
        if module is None:
            continue  # already reported as an import failure
        for name in callables:
            if not callable(getattr(module, name, None)):
                missing.append(f"{module_name}.{name}")
        for name in values:
            if getattr(module, name, None) is None:
                missing.append(f"{module_name}.{name}")
    return missing


@dataclass(frozen=True)
class RuntimeInfo:
    importable: bool
    version_label: str
    supported: bool
    read_only: bool
    reasons: tuple = field(default=())


def probe_runtime() -> RuntimeInfo:
    """Capability probe over the public seams (no version comparison).

    ``supported`` iff every required public seam imports and is callable;
    ``read_only`` is its negation, so an unknown/future Hermes that keeps these
    seams keeps full write access, and a skewed one that loses any of them
    degrades to read-only instead of raising deep inside a write.
    """
    imported = _import_seam_modules()
    modules = imported["modules"]
    reasons = list(imported["errors"])
    missing = _missing_seams(modules)
    if missing:
        reasons.append("missing public Hermes seams: " + ", ".join(sorted(missing)))
    importable = not imported["errors"]
    supported = importable and not missing
    return RuntimeInfo(
        importable=importable,
        version_label=_version_label(),
        supported=supported,
        read_only=not supported,
        reasons=tuple(reasons),
    )


def _hermes():
    """The public ``hermes_cli.config`` module (the ONE import site)."""
    from hermes_cli import config as config_mod

    return config_mod


@dataclass(frozen=True)
class ProfileIdentity:
    """Cheap but spoof-resistant identity of a discovered profile directory."""
    name: str
    path: str
    fingerprint: str  # realpath + directory identity + marker presence


def _profiles_root(root) -> Path:
    return Path(root) / "profiles"


def _iter_named_profile_dirs(root) -> list:
    """Sorted named-profile dirs under ``<root>/profiles``, mirroring Hermes.

    Mirrors ``hermes_cli.profiles._iter_named_profile_dirs(live_only=True)`` at
    the tested revision using only public ``hermes_constants`` helpers: a
    directory is a profile only when it carries an identity marker, is not
    tombstoned, is not ``default``, and matches the canonical name grammar.
    Marker-less shells left by cron/logging side-effects are never listed.
    """
    import hermes_constants as constants_mod

    profiles_root = _profiles_root(root)
    try:
        if not profiles_root.is_dir():
            return []
        entries = sorted(profiles_root.iterdir())
    except OSError:
        return []
    out: list = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name == "default" or not constants_mod.PROFILE_ID_RE.match(entry.name):
            continue
        if not constants_mod.named_profile_has_identity(entry):
            continue
        if constants_mod.named_profile_is_deleted(entry):
            continue
        out.append(entry)
    return out


def discover_profiles() -> Dict[str, ProfileIdentity]:
    """All discovered profiles via Hermes' own rules (default home + named).

    Mirrors ``hermes_cli.profiles.list_profile_names()``: ``"default"`` first
    (the default/root home), then every live identity-bearing profile dir under
    ``<root>/profiles`` sorted by name. Read-only; never mutates HERMES_HOME.
    """
    import hermes_constants as constants_mod

    root = Path(constants_mod.get_default_hermes_root())
    out: Dict[str, ProfileIdentity] = {
        "default": ProfileIdentity(
            name="default", path=str(root),
            fingerprint=_identity_fingerprint(root)),
    }
    for pdir in _iter_named_profile_dirs(root):
        out[pdir.name] = ProfileIdentity(
            name=pdir.name, path=str(pdir),
            fingerprint=_identity_fingerprint(pdir))
    return out


def _identity_fingerprint(pdir) -> str:
    """SHA-256 prefix over realpath + (dev, ino) + identity-marker presence."""
    import hermes_constants as constants_mod

    h = hashlib.sha256()
    h.update(str(Path(pdir).resolve()).encode())
    try:
        stat = Path(pdir).stat()
        h.update(f"{stat.st_dev}:{stat.st_ino}".encode())
    except OSError:
        h.update(b"no-stat")
    try:
        marker_present = bool(constants_mod.named_profile_has_identity(pdir))
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
    def profile_home(self, profile: Optional[str] = None) -> str:
        """The discovered home path of *profile* (``None`` = current home).

        Raises :class:`UnknownProfile` for a name that is not in the discovered
        set — never a fabricated path under the profiles root.
        """
        if not profile:
            import hermes_constants as constants_mod

            return str(constants_mod.get_hermes_home())
        ident = discover_profiles().get(profile)
        if ident is None:
            raise UnknownProfile(profile)
        return ident.path

    @contextlib.contextmanager
    def scoped(self, profile: Optional[str]) -> Iterator[ProfileIdentity]:
        """Enter the profile's home AND secret scope through public seams.

        Installs ``set_hermes_home_override(<profile home>)`` plus
        ``set_secret_scope(build_profile_secret_scope(home), profile_home=...)``
        and resets BOTH on exit. Yields the resolved profile identity. Mutating
        callers must pass an explicit profile — ``None`` is only valid for
        read-only use against the process's own home.
        """
        if not profile:
            import hermes_constants as constants_mod

            home = Path(constants_mod.get_hermes_home())
            ident = ProfileIdentity(name="default", path=str(home),
                                    fingerprint="current")
            with self._enter_scope(home, ident, None) as entered:
                yield entered
            return
        ident = discover_profiles().get(profile)
        if ident is None:
            raise UnknownProfile(profile)
        with self._enter_scope(Path(ident.path), ident, profile) as entered:
            yield entered

    @contextlib.contextmanager
    def _enter_scope(self, home: Path, ident: ProfileIdentity,
                     profile: Optional[str]) -> Iterator[ProfileIdentity]:
        """Install the home override + profile secret scope, then undo both."""
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_secret_scope,
        )

        home_token = set_hermes_home_override(home)
        scope_token = set_secret_scope(
            build_profile_secret_scope(Path(home)), profile_home=str(home))
        try:
            if profile is not None:
                _assert_identity(profile, ident)
            yield ident
        finally:
            # Reverse order of installation; both resets run even when the
            # identity assert (or the caller's body) raised.
            reset_secret_scope(scope_token)
            reset_hermes_home_override(home_token)

    # -- reads -------------------------------------------------------
    def load_raw_config(self, profile: Optional[str] = None) -> Dict[str, Any]:
        """Strict raw-config read inside the profile scope (no defaults merge;
        failed reads surface as errors, never as an empty config)."""
        import copy

        with self.scoped(profile):
            cfg = _hermes().require_readable_config_before_write()
            return copy.deepcopy(dict(cfg))

    def config_revision(self, profile: Optional[str] = None) -> str:
        """The canonical-config revision fingerprint: ``_rev`` of the strictly
        re-read config document. This is the EXACT domain the save guard in
        ``apply_canonical_save`` compares under the lock, so callers can carry
        a revision across the lock boundary and have it match."""
        return _rev(self.load_raw_config(profile))

    # -- writes ------------------------------------------------------
    def apply_canonical_save(self, profile: str, updated: Dict[str, Any],
                             identity: Optional[ProfileIdentity] = None,
                             expected_revision: Optional[str] = None
                             ) -> Dict[str, Any]:
        """Journalled-style single-profile canonical save inside the profile
        scope, with an identity recheck against the discovered profile set and
        a post-write readback of the exact target.

        The stale-write guard runs under this adapter's re-entrant write lock
        immediately before ``save_config``: the revision is re-read and
        compared there, so a writer that slipped in after the caller's recheck
        is still refused BEFORE any write (refuse-stale, never overwrite an
        intervening edit). ``save_config`` then performs Hermes' own atomic
        replace; the target is re-read and its revision reported back.
        """
        self._assert_writable()
        if not profile:
            raise ValueError("explicit profile required for writes")
        if expected_revision is None:
            expected_revision = _EXPECTED_REVISION.get()
        if identity is not None:
            _assert_identity(profile, identity)
        config_mod = _hermes()
        with self.scoped(profile):
            with self._write_lock:
                # Re-assert the discovered identity INSIDE the lock: the
                # pre-lock check is only a fast fail; the guard that matters
                # must run in the same critical section as the save.
                if identity is not None:
                    _assert_identity(profile, identity)
                fresh = config_mod.require_readable_config_before_write()
                fresh_rev = _rev(fresh)
                if expected_revision is not None and fresh_rev != expected_revision:
                    raise StoreConflict(
                        f"profile {profile!r}: config changed since the revision "
                        f"was taken (current {fresh_rev!r}, expected "
                        f"{expected_revision!r}) — refusing the save; re-read "
                        "and re-plan the edit")
                config_mod.save_config(updated)
                after = config_mod.require_readable_config_before_write()
                return {"ok": True, "profile": profile,
                        "revision_before": fresh_rev, "revision_after": _rev(after)}

    def _assert_writable(self) -> None:
        if self.runtime.read_only:
            raise ReadOnlyRuntime(
                "unsupported Hermes runtime; adapter is read-only: "
                + "; ".join(self.runtime.reasons))


def _rev(cfg: Dict[str, Any]) -> str:
    import json

    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:16]
