"""Per-store revision/readback/compensation adapters.

Each adapter owns one store (a file on disk in a Hermes home: config,
cron jobs, plugin routing). Adapters supply:
  - revision(store): monotonically-derivable fingerprint of store state
  - read(store): raw parsed content
  - apply(store, changes): write changes, return post-write revision
  - readback(store): re-read and verify the written value
  - compensate(store, snapshot): restore snapshot content

A store without an adapter blocks the operation (no blind writes).
Revision is a hash of normalized file bytes: cross-process edits that
change the file change the revision, so stale-write risk is detected.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional


class StoreConflict(RuntimeError):
    """Revision changed underneath us — refuse, never force."""


class StoreUnreadable(RuntimeError):
    """The raw file could not be parsed; this is a failure, not empty state."""


class StoreWriteAmbiguous(StoreConflict):
    """A write landed but the store no longer reflects the exact document we
    intended (a concurrent writer replaced the file between our save and our
    revision capture). This is deliberately a StoreConflict subclass so the
    engine records a written-but-ambiguous step, but it carries NO
    post_revision: the recorded revision is NEVER adopted as ours, because a
    fresh re-read cannot tell our bytes from an interloper's. The engine and
    reconcile then fail closed to recovery_required instead of letting a later
    undo restore a snapshot over an external edit."""

    post_revision = None


class StorePreWriteConflict(StoreConflict):
    """A guarded revision recheck refused BEFORE any write was attempted.

    The base ``apply_changes_guarded`` rechecks the revision and only then
    delegates to ``apply_changes`` — so a refusal there provably never touched
    the store (a concurrent writer landed in the check-then-save window between
    the engine's recheck and ours). The ``write_phase`` tag tells the engine and
    reconcile this is a known no-write step, NOT a written-but-
    ambiguous one. Deliberately bounded: it does NOT cover post-write ambiguity
    (:class:`StoreWriteAmbiguous`), which must keep failing closed to
    ``recovery_required``.
    """

    write_phase = "pre_write"


class _DeleteSentinel:
    """Explicit ``remove this key`` marker. Distinct from a literal ``None``
    value: ``DELETE`` means the key must be ABSENT after apply/readback, while
    a present ``None`` value is a real stored null and not a deletion."""
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "DELETE"

    def __bool__(self) -> bool:
        return False


#: Public explicit-delete sentinel (see :class:`_DeleteSentinel`).
DELETE = _DeleteSentinel()


def _is_delete_sentinel(value: Any) -> bool:
    """True for an explicit delete. ``None`` is kept as a backwards-compatible
    alias for the API's existing ``{dotted_path: None}`` removal contract."""
    return value is None or value is DELETE


def _revision_of_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class WriteOutcome:
    applied: bool
    pre_revision: str
    post_revision: str
    readback_ok: bool


class StoreAdapter:
    """Base contract. Concrete adapters bind to one file path."""

    store_kind: str = "abstract"

    def __init__(self, path: str):
        self.path = path

    # -- primitives -------------------------------------------------
    def _raw_bytes(self) -> Optional[bytes]:
        if not os.path.exists(self.path):
            return None
        with open(self.path, "rb") as fh:
            return fh.read()

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def revision(self) -> str:
        raw = self._raw_bytes()
        return _revision_of_bytes(raw) if raw is not None else "absent"

    # -- abstract behaviors -----------------------------------------
    def read(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def write(self, data: Dict[str, Any],
              expected_revision: Optional[str] = None) -> Optional[str]:
        """Persist ``data``. Returns the authoritative post-write revision when
        the store can supply one (e.g. a save seam that reports it from under
        its own lock); ``None`` means the caller must bind the revision itself.
        ``expected_revision`` lets a store re-check staleness inside its own
        write lock (default stores ignore it — the engine already rechecked)."""
        raise NotImplementedError

    def _intended_revision(self, data: Dict[str, Any]) -> Optional[str]:
        """Digest of the EXACT document we intended to persist, when the store
        can compute it without a re-read. ``None`` -> undecidable."""
        if hasattr(self, "expected_revision"):
            return self.expected_revision(data)  # type: ignore[attr-defined]
        return None

    def apply_changes(self, changes: Dict[str, Any]) -> WriteOutcome:
        pre = self.revision()
        data = self.read() or {}
        # changes is a mapping of dotted storage paths -> new value;
        # None/DELETE means "remove this key".
        for dotted, value in changes.items():
            _set_dotted(data, dotted, value)
        saved_revision = self.write(data)
        # Bind the post-write revision to the EXACT document we intended —
        # never to a fresh re-read, which cannot tell our bytes from a
        # concurrent writer's that replaced the file after our save.
        if saved_revision is None:
            saved_revision = self._intended_revision(data)
        if saved_revision is None:
            raise StoreWriteAmbiguous(
                f"{self.path}: cannot bind a post-write revision for the "
                "intended document (store is undecidable)")
        on_disk = self.revision()
        if on_disk != saved_revision:
            # our document is not what is on disk -> an interloper landed;
            # record NO post revision so no compensation/undo adopts it.
            raise StoreWriteAmbiguous(
                f"{self.path}: written document ({saved_revision}) does not "
                f"match the store after write ({on_disk}); concurrent write "
                "detected — refusing to claim ownership")
        return WriteOutcome(applied=True, pre_revision=pre,
                            post_revision=saved_revision, readback_ok=True)

    def apply_changes_guarded(self, changes: Dict[str, Any],
                              expected_revision: Optional[str] = None) -> WriteOutcome:
        """Apply ``changes`` with a revision guard. The base implementation
        re-checks the current revision then delegates; a store with its own
        write lock (the canonical config store) overrides this so the guard
        runs INSIDE that lock (closing the check-then-save race)."""
        if expected_revision is not None and self.revision() != expected_revision:
            # The recheck runs BEFORE apply_changes -> provably no write, so
            # this refusal is tagged pre-write (known no-write), never the
            # post-write ambiguity that must fail closed to recovery_required.
            raise StorePreWriteConflict(
                f"{self.path}: revision changed before write "
                f"({self.revision()} != {expected_revision})")
        return self.apply_changes(changes)

    def readback(self, changes: Dict[str, Any]) -> bool:
        data = self.read()
        if data is None:
            return False
        for dotted, value in changes.items():
            actual = _get_dotted(data, dotted)
            if _is_delete_sentinel(value):
                # a deletion is verified only by ABSENCE — a present literal
                # null (or any value) is NOT a successful removal.
                if actual is not _MISSING:
                    return False
            elif actual != value:
                return False
        return True

    def compensate(self, snapshot: Optional[Dict[str, Any]],
                   expected_revision: str) -> WriteOutcome:
        """Restore snapshot only if current revision matches the recorded
        post-write state. Otherwise raise StoreConflict."""
        current = self.revision()
        if current != expected_revision:
            raise StoreConflict(
                f"{self.path}: revision {current} != recorded {expected_revision}")
        pre = current
        if snapshot is None:
            if self.exists():
                os.remove(self.path)
        else:
            written = self.write(snapshot)
            # Content-verify the restore before reporting a post revision: a
            # writer landing between our write and this check makes the undo
            # unverifiable — surface a conflict (recovery_required), never a
            # blind success whose revision belongs to someone else.
            if written is None and self.read() != snapshot:
                raise StoreConflict(
                    f"{self.path}: store changed during restore; undo refused")
        return WriteOutcome(applied=True, pre_revision=pre,
                            post_revision=self.revision(), readback_ok=True)

    def intended_write_matches(self, snapshot: Optional[Dict[str, Any]],
                               changes: Dict[str, Any],
                               current_revision: str) -> bool:
        """Decidable crash-window predicate for engine.reconcile: True iff the
        on-disk document is EXACTLY what an apply of *changes* on *snapshot*
        would have produced and the intended values read back. False (or an
        undecidable store) means the file differs from our intended write —
        an external edit — and the caller treats the step as ambiguous."""
        if not self.exists():
            return False
        if hasattr(self, "expected_revision"):
            expected = _changes_applied(snapshot if snapshot is not None else {},
                                        changes)
            if (self.expected_revision(expected) == current_revision
                    and self.readback(changes)):
                return True
        return False


class JsonFileStoreAdapter(StoreAdapter):
    """JSON-serializable file store (test/lab stand-in for config.yaml;
    the real YAML adapter shares this contract)."""

    def read(self) -> Optional[Dict[str, Any]]:
        raw = self._raw_bytes()
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # malformed raw file is a hard failure
            raise StoreUnreadable(f"{self.path}: {exc}") from exc

    def write(self, data: Dict[str, Any],
              expected_revision: Optional[str] = None) -> Optional[str]:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(self.serialize(data))
        os.replace(tmp, self.path)  # atomic replacement
        # the caller binds the post-write revision from the intended digest
        return None

    def serialize(self, data: Dict[str, Any]) -> bytes:
        """Canonical byte serialization; revision is derived from this,
        so an on-disk file equals what our write would have produced iff
        revisions match (used for crash reconciliation)."""
        return json.dumps(data, indent=2, sort_keys=True).encode("utf-8")

    def expected_revision(self, data: Dict[str, Any]) -> str:
        return _revision_of_bytes(self.serialize(data))


class JsonConfigStoreAdapter(JsonFileStoreAdapter):
    store_kind = "config"


class JsonCronStoreAdapter(JsonFileStoreAdapter):
    store_kind = "cron"


class JsonPluginStoreAdapter(JsonFileStoreAdapter):
    store_kind = "plugin"


class YamlFileStoreAdapter(JsonFileStoreAdapter):
    """YAML store — the real on-disk format of a Hermes home's config.yaml.

    Shares the whole revision/snapshot/compensation contract with the JSON
    adapter; only serialization differs. Reads are strict: a malformed YAML
    file raises StoreUnreadable (never silently treated as empty)."""

    store_kind = "yaml_config"

    def read(self) -> Optional[Dict[str, Any]]:
        raw = self._raw_bytes()
        if raw is None:
            return None
        try:
            import yaml
            data = yaml.safe_load(raw.decode("utf-8"))
        except Exception as exc:
            raise StoreUnreadable(f"{self.path}: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise StoreUnreadable(f"{self.path}: top-level YAML is not a mapping")
        return data

    def serialize(self, data: Dict[str, Any]) -> bytes:
        import yaml
        return yaml.safe_dump(data, sort_keys=True, default_flow_style=False).encode("utf-8")


class CanonicalConfigStoreAdapter(YamlFileStoreAdapter):
    """config.yaml writes that go through Hermes' own canonical save seam.

    Every write/restore calls ``HermesAdapter.apply_canonical_save``, which
    runs inside the explicit profile's public home/secret scope under this
    adapter set's own re-entrant write lock, re-checks the revision
    immediately before ``save_config``, and normalizes/strips defaults exactly
    as ``hermes_cli.config.save_config`` would. There is NO raw-YAML
    fallback: if the runtime is unsupported or no profile is bound, the
    write raises — the caller sees a visible block, never a textual YAML
    rewrite behind its back.
    """

    store_kind = "canonical_config"

    def __init__(self, path: str, profile: str, hermes_adapter=None,
                 identity=None):
        if not profile:
            raise RuntimeError(
                "canonical_config store requires an explicit profile; "
                "raw YAML writes are refused")
        super().__init__(path)
        self.profile = profile
        self._identity = identity
        self._hermes_adapter = hermes_adapter

    def _adapter(self):
        if self._hermes_adapter is None:
            from hermes_adapter import HermesAdapter
            self._hermes_adapter = HermesAdapter()
        return self._hermes_adapter

    def revision(self) -> str:
        """The canonical store's revision is the SAME fingerprint the save's
        guard compares (``_rev`` of the strictly re-read config document),
        NOT a raw-bytes hash: the engine carries this value into the canonical
        save so the stale-write check runs inside the adapter's save lock.
        A raw-bytes digest would never match the guard, leaving it dead."""
        try:
            return self._adapter().config_revision(self.profile)
        except StoreUnreadable:
            raise
        except Exception as exc:
            raise StoreUnreadable(f"{self.path}: {exc}") from exc

    def write(self, data: Dict[str, Any],
              expected_revision: Optional[str] = None) -> Optional[str]:
        # Full-document canonical save through Hermes' own seam: explicit
        # profile scope + adapter save lock + revision/provenance readback live
        # inside apply_canonical_save. ``expected_revision`` is carried INTO
        # that lock: the check-then-save race is closed there, not here.
        # Raises ReadOnlyRuntime / UnknownProfile / ProfileIdentityChanged on
        # any violation — never a raw file write. StoreConflict is raised ONLY
        # at the guarded fresh-revision check, BEFORE save_config, so it is
        # translated to the pre-write-tagged StorePreWriteConflict;
        # identity/scope violations keep the ordinary (untagged) StoreConflict.
        from hermes_adapter import (
            StoreConflict as _CanonSegmentConflict,
            ProfileIdentityChanged,
            UnknownProfile,
            expected_revision_scope,
        )
        try:
            # carry the expected revision INTO the save's lock via the
            # revision scope (keeps apply_canonical_save's call signature
            # stable for existing test doubles while the guard still runs
            # under the adapter's write lock).
            with expected_revision_scope(expected_revision):
                result = self._adapter().apply_canonical_save(
                    self.profile, data, identity=self._identity)
        except _CanonSegmentConflict as exc:
            # The canonical save raises StoreConflict ONLY at the guarded
            # fresh-revision check, BEFORE config_mod.save_config — provably no
            # write was attempted. Tag it pre-write so the engine records a
            # known no-write step instead of escalating to recovery_required.
            # (Post-write ambiguity keeps failing closed via
            # StoreWriteAmbiguous, which is never tagged here.)
            raise StorePreWriteConflict(str(exc)) from exc
        except (ProfileIdentityChanged, UnknownProfile) as exc:
            # identity/scope violations keep the conservative ordinary
            # conflict contract (no pre-write tag).
            raise StoreConflict(str(exc)) from exc
        self.last_canonical_result = result
        # authoritative post-write revision reported by the save under its lock
        return result.get("revision_after")

    def apply_changes_guarded(self, changes: Dict[str, Any],
                              expected_revision: Optional[str] = None) -> WriteOutcome:
        """Canonical apply: the revision guard is enforced INSIDE the canonical
        save lock (via ``write(expected_revision=...)``), so a concurrent save
        cannot slip between the engine's recheck and ours."""
        pre = self.revision()
        data = self.read() or {}
        for dotted, value in changes.items():
            _set_dotted(data, dotted, value)
        saved_revision = self.write(data, expected_revision=expected_revision)
        if saved_revision is None:
            raise StoreWriteAmbiguous(
                f"{self.path}: canonical save returned no post-write revision")
        on_disk = self.revision()
        if on_disk != saved_revision:
            raise StoreWriteAmbiguous(
                f"{self.path}: canonical save's revision {saved_revision} does "
                f"not match the store after write ({on_disk})")
        return WriteOutcome(applied=True, pre_revision=pre,
                            post_revision=saved_revision, readback_ok=True)

    def compensate(self, snapshot: Optional[Dict[str, Any]],
                   expected_revision: str) -> WriteOutcome:
        if snapshot is None:
            # A profile's config.yaml always exists; "restore absent" would
            # mean deleting it — refuse rather than remove live config.
            raise StoreConflict(
                f"{self.path}: canonical config store cannot be removed")
        pre = self.revision()
        if pre != expected_revision:
            raise StoreConflict(
                f"{self.path}: revision {pre} != recorded {expected_revision}")
        # Restore THROUGH the canonical seam carrying the recorded post-write
        # revision, so the guard is re-checked under Hermes' config lock: an
        # intervening external edit is preserved, never clobbered.
        written = self.write(snapshot, expected_revision=expected_revision)
        # Report the revision OUR restore produced, never a blind re-read: a
        # writer landing right after the save must surface as a conflict (the
        # engine then records recovery_required) instead of being adopted as
        # this operation's post-write state.
        post = written if written is not None else self.revision()
        if written is not None and self.revision() != written:
            raise StoreConflict(
                f"{self.path}: store changed during restore; undo refused")
        return WriteOutcome(applied=True, pre_revision=pre,
                            post_revision=post, readback_ok=True)

    def intended_write_matches(self, snapshot: Optional[Dict[str, Any]],
                               changes: Dict[str, Any],
                               current_revision: str) -> bool:
        """Canonical-store crash-window predicate, decided from the documents.

        The write landed through Hermes' canonical save seam, so a naive
        byte-projection never matches; instead this compares the strictly
        re-read on-disk document against the pre-write snapshot:

        - every intended change must read back (``readback(changes)``), and
        - every dotted path that DIFFERS between the snapshot and the on-disk
          document must be covered by a key in *changes* (itself, a descendant
          or an ancestor of it).

        So a write that only made our changes is recognisable after a crash,
        while any foreign edit (a path we never planned to touch) makes the
        comparison False. Undecidable (unreadable store, missing profile) is
        also False, so the reconcile caller fails closed.
        """
        if not self.exists() or not self.readback(changes):
            return False
        try:
            on_disk = self._adapter().load_raw_config(self.profile)
        except Exception:
            return False  # undecidable -> ambiguous, never guessed
        base = snapshot if isinstance(snapshot, dict) else {}
        for path in _differing_paths(base, on_disk):
            if not _covered_by_changes(path, changes):
                return False
        return True


def adapter_for(store_kind: str, path: str, profile: Optional[str] = None,
                hermes_adapter=None, identity=None) -> StoreAdapter:
    if store_kind == "canonical_config":
        # requires an explicit profile — refuse (no raw-YAML fallback)
        return CanonicalConfigStoreAdapter(
            path, profile or "", hermes_adapter=hermes_adapter,
            identity=identity)
    kinds = {
        "config": JsonConfigStoreAdapter,
        "cron": JsonCronStoreAdapter,
        "plugin": JsonPluginStoreAdapter,
        "yaml_config": YamlFileStoreAdapter,
    }
    cls = kinds.get(store_kind)
    if cls is None:
        raise RuntimeError(f"no adapter for store kind {store_kind!r}")
    return cls(path)


# -- dotted-path helpers --------------------------------------------

def _set_dotted(data: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for key in parts[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    if _is_delete_sentinel(value):
        node.pop(parts[-1], None)  # explicit removal, not a literal null write
    else:
        node[parts[-1]] = value


def _get_dotted(data: Dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


_MISSING = object()


def _differing_paths(left: Any, right: Any, prefix: str = "") -> list:
    """Dotted paths where two config documents disagree (scalars/lists are leaves).

    ``{"a": {"b": 1}}`` vs ``{"a": {"b": 2}}`` -> ``["a.b"]``; a key present on
    one side only -> that path; list values compare as whole leaves (their order
    and contents are the stored value, not a path space).
    """
    if isinstance(left, dict) and isinstance(right, dict):
        out: list = []
        for key in sorted(set(left) | set(right), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                out.append(path)
                continue
            out.extend(_differing_paths(left[key], right[key], path))
        return out
    return [] if left == right else [prefix]


def _covered_by_changes(path: str, changes: Dict[str, Any]) -> bool:
    """True when *path* is a change key, or a descendant/ancestor of one."""
    for dotted in changes:
        dotted = str(dotted)
        if (path == dotted or path.startswith(dotted + ".")
                or dotted.startswith(path + ".")):
            return True
    return False


def _changes_applied(base: Dict[str, Any],
                     changes: Dict[str, Any]) -> Dict[str, Any]:
    """In-memory projection of snapshot + planned changes (no I/O)."""
    import copy
    data = copy.deepcopy(base)
    for dotted, value in changes.items():
        _set_dotted(data, dotted, value)
    return data
