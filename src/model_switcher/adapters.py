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

    def write(self, data: Dict[str, Any]) -> None:
        raise NotImplementedError

    def apply_changes(self, changes: Dict[str, Any]) -> WriteOutcome:
        pre = self.revision()
        data = self.read() or {}
        # changes is a mapping of dotted storage paths -> new value;
        # None means "remove this key".
        for dotted, value in changes.items():
            _set_dotted(data, dotted, value)
        self.write(data)
        post = self.revision()
        return WriteOutcome(applied=True, pre_revision=pre,
                            post_revision=post, readback_ok=True)

    def readback(self, changes: Dict[str, Any]) -> bool:
        data = self.read()
        if data is None:
            return False
        for dotted, value in changes.items():
            if _get_dotted(data, dotted) != value:
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
            self.write(snapshot)
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

    def write(self, data: Dict[str, Any]) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(self.serialize(data))
        os.replace(tmp, self.path)  # atomic replacement

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
    runs inside the explicit profile's scope AND Hermes' ``_CONFIG_LOCK``
    (plus this adapter set's own write lock) and normalizes/strips defaults
    exactly as ``hermes_cli.config.save_config`` would. There is NO raw-YAML
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

    def write(self, data: Dict[str, Any]) -> None:
        # Full-document canonical save through Hermes' own seam: explicit
        # profile scope + config lock + revision readback live inside
        # apply_canonical_save. Raises ReadOnlyRuntime / UnknownProfile /
        # ProfileIdentityChanged on any violation — never a raw file write.
        result = self._adapter().apply_canonical_save(
            self.profile, data, identity=self._identity)
        self.last_canonical_result = result

    def compensate(self, snapshot: Optional[Dict[str, Any]],
                   expected_revision: str) -> WriteOutcome:
        if snapshot is None:
            # A profile's config.yaml always exists; "restore absent" would
            # mean deleting it — refuse rather than remove live config.
            raise StoreConflict(
                f"{self.path}: canonical config store cannot be removed")
        return super().compensate(snapshot, expected_revision)

    def intended_write_matches(self, snapshot: Optional[Dict[str, Any]],
                               changes: Dict[str, Any],
                               current_revision: str) -> bool:
        """Canonical-store crash-window predicate. The write landed through
        Hermes' canonical save seam (normalize + strip-defaults), so raw
        serialized bytes from a naive projection never match: decide from the
        documents themselves — the intended document is run through the SAME
        canonical pipeline (plan_canonical_save) and compared against the
        strictly re-read on-disk document. Anything undecidable is False, so
        the reconcile caller fails closed to recovery_required."""
        if not self.exists() or not self.readback(changes):
            return False
        try:
            intended = _changes_applied(
                snapshot if snapshot is not None else {}, changes)
            adapter = self._adapter()
            planned = adapter.plan_canonical_save(
                self.profile, intended)["planned_document"]
            on_disk = adapter.load_raw_config(self.profile)
        except Exception:
            return False  # undecidable -> ambiguous, never guessed
        return planned == on_disk


def adapter_for(store_kind: str, path: str, profile: Optional[str] = None,
                hermes_adapter=None) -> StoreAdapter:
    if store_kind == "canonical_config":
        # requires an explicit profile — refuse (no raw-YAML fallback)
        return CanonicalConfigStoreAdapter(
            path, profile or "", hermes_adapter=hermes_adapter)
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
    if value is None:
        node.pop(parts[-1], None)  # removal, not a literal null write
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


def _changes_applied(base: Dict[str, Any],
                     changes: Dict[str, Any]) -> Dict[str, Any]:
    """In-memory projection of snapshot + planned changes (no I/O)."""
    import copy
    data = copy.deepcopy(base)
    for dotted, value in changes.items():
        _set_dotted(data, dotted, value)
    return data
