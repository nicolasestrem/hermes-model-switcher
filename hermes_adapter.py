"""Hermes access through public config, profile, and secret-scope APIs.

Profile scoping uses a context-local home override and matching secret scope;
request handlers never mutate process-global HERMES_HOME. Discovery follows
Hermes' identity and tombstone rules. Missing required capabilities make the
adapter read-only; package versions are informational.

Canonical saves recheck identity and revision, preserve unrelated leaves with
merge_existing when possible, and verify strict readback. The plugin-local
lock is not a host-wide transaction: the public API has no compare-and-save,
and full-state replacement still has a check-then-save race.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


# Carry the pre-write revision through the canonical save call's context.
# An explicit expected_revision argument takes precedence.
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
    """The store's revision changed before saving — no write was attempted."""


class PostSaveConflict(RuntimeError):
    """The strict readback is not the document this save intended to produce.

    A save WAS attempted. Never attach/adopt a post revision, retry the write,
    or compensate this step blindly. CanonicalConfigStoreAdapter translates
    this to StoreWriteAmbiguous (NOT StorePreWriteConflict). The engine must
    persist an ambiguous step and recovery_required, with no post_revision;
    any public failure category must be a fixed allowlisted value.
    """


def _version_label() -> str:
    """Informational package version, never a capability gate."""
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
    """Require every public seam; missing capabilities make writes read-only."""
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
    """Load the public config module lazily."""
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
        try:
            scope_token = set_secret_scope(
                build_profile_secret_scope(Path(home)), profile_home=str(home))
            try:
                if profile is not None:
                    _assert_identity(profile, ident)
                yield ident
            finally:
                reset_secret_scope(scope_token)
        finally:
            # Home is protected immediately: setup AND secret reset may fail.
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

        The revision check uses a plugin-local lock, NOT Hermes' lock. For
        merge-expressible edits, only changed leaves are sent to public
        ``save_config(..., merge_existing=True)``: its disk reread and rebase
        run under Hermes' own lock, preserving unrelated live leaves.

        LIMIT: Hermes exposes no public compare-and-save / locked transform.
        A same-key writer can still land between this check and the host's
        lock, then be overwritten undetectably. Deletions (and nested mapping
        -> null) cannot use its merge API; full-state saves retain that race
        even for unrelated edits. See _save_patch. Do not treat this lock as
        a host-wide transaction. Strict readback never adopts foreign edits.
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
                expected = _expected_sparse_document(
                    updated, fresh, config_mod.DEFAULT_CONFIG)
                patch = _save_patch(fresh, updated)
                if patch is None:
                    # Omissions deliberately remove keys. merge_existing would
                    # resurrect them; nested dict->None is ignored by its deep
                    # merge. The public full-state path is save_config (which
                    # uses atomic_config_replace), NOT atomic_config_write.
                    config_mod.save_config(updated)
                else:
                    config_mod.save_config(patch, merge_existing=True)
                try:
                    after = config_mod.require_readable_config_before_write()
                except Exception as exc:
                    raise PostSaveConflict(
                        "canonical save readback is unreadable") from exc
                if _rev(after) != _rev(expected):
                    raise PostSaveConflict(
                        "canonical save readback differs from intended document")
                return {"ok": True, "profile": profile,
                        "revision_before": fresh_rev, "revision_after": _rev(expected)}

    def _assert_writable(self) -> None:
        if self.runtime.read_only:
            raise ReadOnlyRuntime(
                "unsupported Hermes runtime; adapter is read-only: "
                + "; ".join(self.runtime.reasons))


def _save_patch(before: Dict[str, Any], desired: Dict[str, Any]) -> Optional[dict]:
    """Smallest merge payload, or None when a full-state save is required.

    A full stale document is NOT a partial payload: it overwrites unchanged
    siblings with their old values. Omission cannot delete through merge;
    replacing a mapping can erase concurrently added descendants, so use the
    explicit replacement path for those cases and report the public-CAS gap.
    """
    import copy

    if set(before) - set(desired):
        return None
    patch = {}
    for key, value in desired.items():
        if key in before and _rev({"v": value}) == _rev({"v": before[key]}):
            continue
        if key in before and isinstance(before[key], dict):
            if not isinstance(value, dict):
                return None
            nested = _save_patch(before[key], value)
            if nested is None:
                return None
            patch[key] = nested
        else:
            patch[key] = copy.deepcopy(value)
    return patch


def _expected_sparse_document(desired: dict, before: dict, defaults: dict) -> dict:
    """Predict default stripping using public DEFAULT_CONFIG and raw authorship.

    This is NOT a canonicalization pipeline. Inputs are raw canonical documents
    from the store plus edits to canonical slots. Legacy normalization or any
    other host transformation we cannot predict is conservatively ambiguous,
    never accepted by borrowing a foreign readback. Preserve authored leaves,
    including nulls and empty lists, exactly as Hermes' sparse save does.
    """
    import copy

    missing = object()
    dropped = object()
    def strip(value, old, default, path):
        if (old is not missing and not isinstance(old, dict)) or path == ("_config_version",):
            return copy.deepcopy(value)
        if isinstance(value, dict) and value:
            old_map = old if isinstance(old, dict) else {}
            default_map = default if isinstance(default, dict) else {}
            result = {}
            for key, child in value.items():
                kept = strip(child, old_map.get(key, missing),
                             default_map.get(key), (*path, key))
                if kept is not dropped:
                    result[key] = kept
            return result or dropped
        return dropped if value == default else copy.deepcopy(value)
    expected = strip(desired, before, defaults, ())
    return expected if isinstance(expected, dict) else {}


def _rev(cfg: Dict[str, Any]) -> str:
    import json

    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:16]
