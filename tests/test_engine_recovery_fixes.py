"""Lane-6 write-integrity fixes: crash-mid-rollback, unreadable stores,
ambiguous phases, and journal serialization.

Four defects, each with a RED-first regression test that drives REAL store
files in a throwaway lab under /tmp (never the live Hermes home, never the
scratch TMPDIR that sits inside it) and a real journal outside any git
worktree:

  D1  reconcile could never RESUME a compensation: a step recorded
      ``failed`` with a post_revision (a write the engine proved is ours)
      fell into the generic failed branch, where ``current != pre_revision``
      is always true for a landed write -> STEP_CONFLICT ->
      recovery_required. A crash in the middle of a rollback therefore
      stranded the write forever and undo refused permanently.
  D2  the per-step ``adapter.revision()`` in reconcile was unguarded: one
      deleted profile / corrupted config.yaml raised StoreUnreadable and
      aborted reconciliation of EVERY operation.
  D3  ``_finish_failure`` derived ``all_failed`` from "no step recorded a
      post_revision" instead of requiring ``write_phase == PRE``, so a
      WRITE_PHASE_AMBIGUOUS step could end OP_PENDING ("nothing wrote;
      retryable") even though a write had landed.
  D4  ``add_step`` serialized the snapshot with plain ``json.dumps`` while
      ``_rev`` uses ``default=str``: a config.yaml holding an unquoted date
      scalar (safe_load -> datetime.date) revised fine but made
      plan_operation raise TypeError, leaving the operation half-journalled.

Every test asserts the store's real bytes: an ambiguous write is never
silently adopted and never blindly restored.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from model_switcher import engine  # noqa: E402
from model_switcher.adapters import (  # noqa: E402
    JsonConfigStoreAdapter,
    StoreConflict,
    StoreUnreadable,
    WriteOutcome,
    _set_dotted,
)
from model_switcher.engine import (  # noqa: E402
    ApplySnapshotNotRestorable,
    ApplyUndoAmbiguous,
    apply_operation,
    plan_operation,
    reconcile,
    undo_operation,
)
from model_switcher.journal import (  # noqa: E402
    OP_APPLYING,
    OP_AWAITING_HOST,
    OP_COMMITTED,
    OP_COMPENSATED,
    OP_PENDING,
    OP_RECOVERY_REQUIRED,
    STEP_APPLIED,
    STEP_AWAITING_HOST,
    STEP_COMPENSATED,
    STEP_CONFLICT,
    STEP_FAILED,
    WRITE_PHASE_AMBIGUOUS,
    WRITE_PHASE_POST,
    WRITE_PHASE_PRE,
    OperationJournal,
)

LAB_DOC = {
    "main": {"provider": "openrouter", "model": "m/old"},
    "auxiliary": {"goal_judge": {"model": "j/old", "threshold": 0.8}},
}


# --------------------------------------------------------------- lab helpers

@pytest.fixture()
def lab():
    """Throwaway lab under /tmp: never the live home, never TMPDIR."""
    root = Path(tempfile.mkdtemp(prefix="engine-fixes-lab-", dir="/tmp"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _lab(lab, profiles=("default",)):
    paths = {}
    for name in profiles:
        d = lab / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        path = d / "config.json"
        path.write_text(json.dumps(LAB_DOC, indent=2, sort_keys=True))
        paths[name] = str(path)
    journal = OperationJournal(str(lab / "private" / "journal.db"))
    return paths, journal


def _backend_target(path, profile="default", changes=None):
    return {"profile": profile, "store_kind": "config", "path": path,
            "changes": dict(changes or {"main.model": "m/new"})}


def _host_target(path, profile="default"):
    return {
        "profile": profile, "store_kind": "config", "path": path,
        "changes": {"main.provider": "nous", "main.model": "m/new"},
        "executor": "host",
        "host_call": {"method": "POST", "path": "/api/model/set",
                      "query": {}, "body": {"scope": "main"}},
        "readback_keys": ["main.provider", "main.model"],
        "allowed_prefixes": ["main"],
    }


def _read(path):
    return json.loads(Path(path).read_text())


def _states(op):
    return [s["state"] for s in op["steps"]]


def _adapter_patch(monkeypatch, overrides):
    """Route specific lab paths to a fake adapter, everything else real."""
    real = engine.adapter_for

    def _adapter_for(kind, path, profile=None, identity=None):
        factory = overrides.get(path)
        if factory is not None:
            return factory(path)
        return real(kind, path, profile, identity=identity)

    monkeypatch.setattr(engine, "adapter_for", _adapter_for)


# =========================================================================== #
# D1 -- a failed-but-written step is a compensation to RESUME, not a conflict
# =========================================================================== #

class _CrashMidRollback(BaseException):
    """Process death: not an Exception, so nothing in the engine can catch it."""


_CRASH = {"armed": False}


class _SabotagedWriteAdapter(JsonConfigStoreAdapter):
    """The write lands, but the strict readback can never verify it.

    Exactly the shape ``_apply_step`` records as ``failed`` WITH a
    post_revision (phase POST): the engine has PROVEN the write is ours.
    """

    def apply_changes(self, changes):
        data = self.read() or {}
        for dotted, value in changes.items():
            _set_dotted(data, dotted, value)
        sabotaged = copy.deepcopy(data)
        sabotaged["main"] = {"sabotaged": True}
        self.write(sabotaged)
        post = self.revision()
        return WriteOutcome(applied=True, pre_revision=post,
                            post_revision=post, readback_ok=True)

    def compensate(self, snapshot, expected_revision):
        if _CRASH["armed"]:
            _CRASH["armed"] = False
            raise _CrashMidRollback()
        return super().compensate(snapshot, expected_revision)


def _crash_mid_rollback(lab, monkeypatch):
    """Leave a real journal behind: step 0 applied, step 1 failed+post, and
    the process died on the FIRST compensation (step 1, reverse order)."""
    paths, journal = _lab(lab, profiles=("default", "alpha"))
    _CRASH["armed"] = True
    _adapter_patch(monkeypatch, {paths["alpha"]: _SabotagedWriteAdapter})
    op = plan_operation(journal, [
        _backend_target(paths["default"], profile="default"),
        _backend_target(paths["alpha"], profile="alpha"),
    ])
    with pytest.raises(_CrashMidRollback):
        apply_operation(journal, op["op_id"])

    crashed = journal.get(op["op_id"])
    assert crashed["state"] == OP_APPLYING, crashed["state"]
    assert _states(crashed) == [STEP_APPLIED, STEP_FAILED]
    assert crashed["steps"][1]["post_revision"], \
        "the sabotaged write landed, so its revision is recorded"
    assert crashed["steps"][1]["write_phase"] == WRITE_PHASE_POST
    journal.close()

    # A restart: a fresh handle on the same private DB.
    return paths, op["op_id"], OperationJournal(journal.db_path)


def test_reconcile_resumes_a_compensation_interrupted_by_a_crash(lab,
                                                                monkeypatch):
    """D1: the recorded post-revision still matches the store, so the write is
    provably ours and the interrupted rollback must be RESUMED -- not escalated
    to recovery_required, which stranded the write and refused undo forever."""
    paths, op_id, journal = _crash_mid_rollback(lab, monkeypatch)
    try:
        rec = reconcile(journal, op_id)

        assert rec["state"] == OP_COMPENSATED, rec["state"]
        assert _states(rec) == [STEP_COMPENSATED, STEP_COMPENSATED]
        # Both writes are rolled back to their pre-operation snapshots.
        assert _read(paths["default"]) == LAB_DOC
        assert _read(paths["alpha"]) == LAB_DOC, \
            "the interrupted compensation must be finished, not stranded"

        # Terminal: a second reconcile is a no-op, and the op is closed.
        assert reconcile(journal, op_id)["state"] == OP_COMPENSATED
        assert op_id not in {o["op_id"] for o in journal.unfinished_operations()}
    finally:
        journal.close()


def test_interrupted_compensation_is_never_resumed_over_a_later_edit(
        lab, monkeypatch):
    """D1 control: if the store moved after the crash, the failed step is a
    later external edit -- recovery_required, no clobber, undo refused."""
    paths, op_id, journal = _crash_mid_rollback(lab, monkeypatch)
    try:
        # Someone else edits the alpha store after the crash.
        external = _read(paths["alpha"])
        external["external"] = {"edit": True}
        Path(paths["alpha"]).write_text(
            json.dumps(external, indent=2, sort_keys=True))

        rec = reconcile(journal, op_id)

        assert rec["state"] == OP_RECOVERY_REQUIRED, rec["state"]
        assert rec["steps"][1]["state"] == STEP_CONFLICT
        assert _read(paths["alpha"])["external"] == {"edit": True}, \
            "never restore a snapshot over a newer edit"
        with pytest.raises(ApplyUndoAmbiguous):
            undo_operation(journal, op_id)
        assert _read(paths["alpha"])["external"] == {"edit": True}
    finally:
        journal.close()


# =========================================================================== #
# D2 -- one unreadable store must not brick reconciliation of every operation
# =========================================================================== #

class _UnreadableRevisionAdapter(JsonConfigStoreAdapter):
    """A deleted profile / corrupted config.yaml: the store cannot be
    fingerprinted at all (the canonical seam raises StoreUnreadable here)."""

    def __init__(self, path, exc_type):
        super().__init__(path)
        self._exc_type = exc_type

    def revision(self):
        raise self._exc_type("store cannot be fingerprinted")


@pytest.mark.parametrize("exc_type", [StoreUnreadable, OSError])
def test_reconcile_fails_closed_instead_of_aborting_on_an_unreadable_store(
        lab, monkeypatch, exc_type):
    """D2: reconcile must decide THAT operation fail-closed
    (recovery_required / store_unreadable) instead of letting the exception
    escape -- which aborts recovery for every other operation too."""
    paths, journal = _lab(lab, profiles=("broken", "healthy"))
    try:
        op_broken = plan_operation(
            journal, [_host_target(paths["broken"], profile="broken")])
        op_healthy = plan_operation(
            journal, [_host_target(paths["healthy"], profile="healthy")])
        assert apply_operation(journal, op_broken["op_id"])["state"] == \
            OP_AWAITING_HOST
        assert apply_operation(journal, op_healthy["op_id"])["state"] == \
            OP_AWAITING_HOST

        _adapter_patch(monkeypatch, {
            paths["broken"]: lambda p: _UnreadableRevisionAdapter(p, exc_type),
        })

        try:
            rec = reconcile(journal, op_broken["op_id"])
        except (StoreUnreadable, OSError) as exc:  # the defect
            pytest.fail(
                "reconcile raised %s for one unreadable store; every recovery "
                "route would 500" % type(exc).__name__)

        assert rec["state"] == OP_RECOVERY_REQUIRED, rec["state"]
        assert rec["failure_category"] == "store_unreadable"
        assert rec["steps"][0]["state"] == STEP_CONFLICT
        # The decided step's token is retired: no late replay can resurrect it.
        assert journal.get(op_broken["op_id"])["steps"][0][
            "host_token_hash"] is None
        # Idempotent: a second recovery attempt still answers instead of raising.
        assert reconcile(journal, op_broken["op_id"])["state"] == \
            OP_RECOVERY_REQUIRED

        # The OTHER operation still reconciles: one bad store is contained.
        other = reconcile(journal, op_healthy["op_id"])
        assert other["state"] == OP_PENDING, other["state"]
        assert _read(paths["healthy"]) == LAB_DOC
    finally:
        journal.close()


# =========================================================================== #
# D2 (real seam) -- a DELETED canonical profile raises StoreUnreadable from
# adapter.revision(); reconcile must contain it, not abort.
# =========================================================================== #

CANON_CFG = "model:\n  provider: openai\n  default: gpt-4o-mini\n"


def test_reconcile_survives_a_deleted_canonical_profile(hermes_home):
    home = hermes_home.path
    pdir = hermes_home.make_profile("alpha", config={"_raw": CANON_CFG})
    path = pdir / "config.yaml"
    private = Path(tempfile.mkdtemp(prefix="engine-fixes-private-", dir="/tmp"))
    journal = OperationJournal(str(private / "journal.db"))
    try:
        op = plan_operation(journal, [{
            "profile": "alpha", "store_kind": "canonical_config",
            "path": str(path),
            "changes": {"main.provider": "nous", "main.model": "m/new"},
            "executor": "host",
            "host_call": {"method": "POST", "path": "/api/model/set",
                          "query": {}, "body": {"scope": "main"}},
            "readback_keys": ["main.provider", "main.model"],
            "allowed_prefixes": ["main"],
        }])
        assert apply_operation(journal, op["op_id"])["state"] == \
            OP_AWAITING_HOST

        # The profile is deleted while the host call is still in flight.
        shutil.rmtree(pdir)

        try:
            rec = reconcile(journal, op["op_id"])
        except (StoreUnreadable, OSError) as exc:  # the defect
            pytest.fail("reconcile raised %s for a vanished profile"
                        % type(exc).__name__)
        assert rec["state"] == OP_RECOVERY_REQUIRED, rec["state"]
        assert rec["failure_category"] == "store_unreadable"
        assert rec["steps"][0]["state"] == STEP_CONFLICT
    finally:
        journal.close()
        shutil.rmtree(private, ignore_errors=True)


# =========================================================================== #
# D3 -- an ambiguous phase is never reported as "nothing wrote"
# =========================================================================== #
class _UntaggedConflictAdapter(JsonConfigStoreAdapter):
    """Identity change / unknown profile: the canonical seam raises a plain
    StoreConflict with NO write-phase tag, before any write."""

    def apply_changes_guarded(self, changes, expected_revision=None):
        raise StoreConflict("profile identity changed")


class _UnreadableReadbackAdapter(JsonConfigStoreAdapter):
    """The write lands, then the store becomes unreadable: readback raises
    StoreUnreadable AFTER the engine already holds the post-revision."""

    def readback(self, changes):
        raise StoreUnreadable("post-write readback is unreadable")


def test_untagged_write_conflict_is_not_reported_as_a_known_no_write(
        lab, monkeypatch):
    """D3a: only WRITE_PHASE_PRE is a known no-write. An unclassified failure
    must fail closed to recovery_required, never OP_PENDING ('retryable')."""
    paths, journal = _lab(lab)
    path = paths["default"]
    _adapter_patch(monkeypatch, {path: _UntaggedConflictAdapter})
    try:
        op = plan_operation(journal, [_backend_target(path)])
        result = apply_operation(journal, op["op_id"])

        assert result["state"] == OP_RECOVERY_REQUIRED, result["state"]
        assert result["failure_category"] == "revision_conflict"
        step = result["steps"][0]
        assert step["state"] == STEP_CONFLICT
        assert step["post_revision"] is None
        assert step["write_phase"] == WRITE_PHASE_AMBIGUOUS
        # Nothing is compensated blind over an undecidable store.
        assert _read(path) == LAB_DOC
        # And no retry can turn it into a silent success.
        assert apply_operation(journal, op["op_id"])["state"] == \
            OP_RECOVERY_REQUIRED
        with pytest.raises(ApplyUndoAmbiguous):
            undo_operation(journal, op["op_id"])
    finally:
        journal.close()


def test_post_write_readback_failure_keeps_its_post_revision(lab,
                                                             monkeypatch):
    """D3b: the write landed and its revision was already computed. Losing it
    let the operation end OP_PENDING while the new bytes sat on disk."""
    paths, journal = _lab(lab)
    path = paths["default"]
    _adapter_patch(monkeypatch, {path: _UnreadableReadbackAdapter})
    try:
        op = plan_operation(journal, [_backend_target(path)])
        result = apply_operation(journal, op["op_id"])

        assert result["state"] == OP_RECOVERY_REQUIRED, result["state"]
        assert result["failure_category"] == "store_unreadable"
        step = result["steps"][0]
        assert step["state"] == STEP_CONFLICT
        assert step["post_revision"], \
            "the computed post-write revision must survive the readback failure"
        assert step["write_phase"] == WRITE_PHASE_AMBIGUOUS
        # The landed write is NOT silently adopted and NOT blindly restored.
        assert _read(path)["main"]["model"] == "m/new"
        with pytest.raises(ApplyUndoAmbiguous):
            undo_operation(journal, op["op_id"])
        assert _read(path)["main"]["model"] == "m/new"
    finally:
        journal.close()


# =========================================================================== #
# D4 -- the journal serializes documents with the SAME contract as _rev
# =========================================================================== #

def test_plan_operation_refuses_a_snapshot_it_cannot_restore_exactly(lab):
    """D4: an unquoted YAML date scalar is a datetime.date after safe_load.
    The journal stores snapshots as JSON (default=str, the _rev contract), so
    the date would come back as a string: compensation/Undo would silently
    change its type on disk. Planning must refuse BEFORE journalling anything
    (never the old opaque TypeError, never a half-journalled op)."""
    d = lab / "profiles" / "default"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "config.yaml"
    path.write_text(
        "main:\n"
        "  provider: openrouter\n"
        "  model: m/old\n"
        "last_used: 2026-10-21\n")
    before = path.read_bytes()
    journal = OperationJournal(str(lab / "private" / "journal.db"))
    try:
        with pytest.raises(ApplySnapshotNotRestorable) as info:
            plan_operation(journal, [{
                "profile": "default", "store_kind": "yaml_config",
                "path": str(path), "changes": {"main.model": "m/new"},
            }])
        # Fixed public wording, no config value or path in it.
        reason = type(info.value).public_reason
        assert "2026-10-21" not in reason and str(path) not in reason
        assert journal.unfinished_operations() == []
        assert path.read_bytes() == before
    finally:
        journal.close()


def test_plan_operation_journals_a_quoted_date_and_applies(lab):
    """D4 control: the same value quoted is a plain string, round-trips
    exactly, and the operation journals and commits normally."""
    d = lab / "profiles" / "default"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "config.yaml"
    path.write_text(
        "main:\n"
        "  provider: openrouter\n"
        "  model: m/old\n"
        "last_used: '2026-10-21'\n")
    journal = OperationJournal(str(lab / "private" / "journal.db"))
    try:
        op = plan_operation(journal, [{
            "profile": "default", "store_kind": "yaml_config",
            "path": str(path), "changes": {"main.model": "m/new"},
        }])
        step = journal.get(op["op_id"])["steps"][0]
        assert step["snapshot"]["last_used"] == "2026-10-21"
        result = apply_operation(journal, op["op_id"])
        assert result["state"] == OP_COMMITTED, result["state"]
        assert _read_yaml(path)["main"]["model"] == "m/new"
        assert _read_yaml(path)["last_used"] == "2026-10-21"
    finally:
        journal.close()


def _read_yaml(path):
    import yaml
    return yaml.safe_load(Path(path).read_text())
