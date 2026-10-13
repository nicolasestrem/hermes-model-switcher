"""Journalled apply / compensation / undo engine.

Contract (plan §Mutation, failure and rollback contract):
- Resolve targets strictly; every target must have an adapter or the
  operation blocks before any write.
- Journal intent + snapshots BEFORE writing.
- Apply profiles sequentially: revision recheck -> fresh read -> apply
  -> readback -> record post-write revision.
- Repeated operation IDs return the existing operation (no duplicate
  writes).
- On failure, stop and compensate completed steps in reverse order, but
  only when the store's current revision still matches the recorded
  post-write revision; otherwise that step is a conflict and the whole
  operation becomes recovery_required (never a forced restore).
- Crash/timeout reconciliation compares persisted intent against actual
  files; ambiguous state is reported, not guessed.
- Undo re-applies snapshots under the same revision guard, so a later
  external edit is preserved, not clobbered.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .adapters import (
    StoreConflict,
    StoreUnreadable,
    StoreAdapter,
    _set_dotted,
    adapter_for,
)
from .journal import (
    OperationJournal,
    OP_COMMITTED,
    OP_COMPENSATED,
    OP_PARTIAL,
    OP_PENDING,
    OP_RECOVERY_REQUIRED,
    OP_UNDONE,
    STEP_APPLIED,
    STEP_COMPENSATED,
    STEP_CONFLICT,
    STEP_FAILED,
)

# A per-profile, per-store write target.
# {"profile": str, "store_kind": str, "path": str, "changes": {dotted: value}}


class ApplyBlocked(RuntimeError):
    """Preflight failed; no writes were started."""


class StepFailure(RuntimeError):
    """A write step failed after it started; triggers compensation."""


def snapshot_adapter(adapter: StoreAdapter) -> Optional[Dict[str, Any]]:
    """Private raw snapshot of the store before writing.
    missing-vs-present is preserved (None means the file did not exist)."""
    if not adapter.exists():
        return None
    data = adapter.read()  # may raise StoreUnreadable
    return data


def plan_operation(journal: OperationJournal,
                   targets: List[Dict[str, Any]],
                   payload: Optional[Dict[str, Any]] = None,
                   op_id: Optional[str] = None) -> Dict[str, Any]:
    """Journal intent + snapshots before any write.

    targets: ordered list of write targets. Raises ApplyBlocked (with no
    journal entry committed as writable) if any target lacks an adapter
    or is unreadable.
    """
    prepared: List[Dict[str, Any]] = []
    adapters: Dict[str, StoreAdapter] = {}
    for t in targets:
        if "store_kind" not in t or "path" not in t:
            raise ApplyBlocked(f"target missing store binding: {t!r}")
        key = f'{t["profile"]}:{t["store_kind"]}:{t["path"]}'
        try:
            adapter = adapter_for(t["store_kind"], t["path"], t.get("profile"))
        except RuntimeError as exc:
            raise ApplyBlocked(str(exc)) from exc
        try:
            snap = snapshot_adapter(adapter)
        except StoreUnreadable as exc:
            # an unreadable raw file is a preflight failure, not empty state
            raise ApplyBlocked(str(exc)) from exc
        adapters[key] = adapter
        prepared.append({
            "profile": t["profile"],
            "store_kind": t["store_kind"],
            "path": t["path"],
            "changes": t["changes"],
            "snapshot": snap,
            "pre_revision": adapter.revision(),
        })

    op_id_local = op_id
    if op_id_local is not None:
        existing = journal.get(op_id_local)
        if existing is not None:
            return existing  # idempotent: never re-plan an existing op

    op = journal.begin(payload or {}, op_id=op_id_local)
    for seq, p in enumerate(prepared):
        journal.add_step(op["op_id"], seq, p["profile"], p["store_kind"],
                         p["path"], p["changes"], p["snapshot"],
                         p["pre_revision"])
    return journal.get(op["op_id"])  # type: ignore[return-value]


def _apply_step(adapter: StoreAdapter, changes: Dict[str, Any],
                pre_revision: str) -> str:
    """Sequential apply with revision recheck + fresh read + readback."""
    current = adapter.revision()
    if current != pre_revision:
        raise StoreConflict(
            f"revision changed before write: {current} != {pre_revision}")
    # fresh read is part of the recheck (also surfaces unreadable files)
    if adapter.exists():
        adapter.read()
    outcome = adapter.apply_changes(changes)
    if not adapter.readback(changes):
        raise StepFailure("readback verification failed after write")
    return outcome.post_revision


def apply_operation(journal: OperationJournal, op_id: str,
                    fail_after: Optional[int] = None) -> Dict[str, Any]:
    """Apply the journalled operation sequentially.

    fail_after: injected fault for tests — raise StepFailure after N
    successful steps (simulates a mid-rollout failure/timeout).
    """
    op = journal.get(op_id)
    if op is None:
        raise ApplyBlocked(f"unknown operation {op_id}")
    if op["state"] not in (OP_PENDING, "applying"):
        # completed / already-compensated / recovery: a retry with the same
        # op_id returns the existing record instead of duplicating writes
        return op

    applied_steps: List[Dict[str, Any]] = []
    failure: Optional[BaseException] = None
    for step in op["steps"]:
        if step["state"] == STEP_APPLIED:
            applied_steps.append(step)
            continue
        if step["state"] != "pending":
            failure = StepFailure(
                f'step {step["seq"]} in non-applyable state {step["state"]}')
            break
        if fail_after is not None and len(applied_steps) >= fail_after:
            journal.set_step_state(op_id, step["seq"], STEP_FAILED)
            failure = StepFailure("injected failure before step "
                                  f'{step["seq"]}')
            break
        adapter = adapter_for(step["store_kind"], step["path"], step.get("profile"))
        try:
            post = _apply_step(adapter, step["changes"], step["pre_revision"])
        except (StoreConflict, StoreUnreadable, StepFailure, OSError) as exc:
            failure = exc
            journal.set_step_state(op_id, step["seq"], STEP_FAILED)
            break
        journal.set_step_state(op_id, step["seq"], STEP_APPLIED, post)
        step["post_revision"] = post  # keep in-memory copy consistent
        applied_steps.append(step)

    if failure is None:
        journal.finish(op_id, OP_COMMITTED)
        return journal.get(op_id)  # type: ignore[return-value]

    # Partial failure: compensate completed writes in reverse order,
    # re-reading the journal so post-write revisions are current.
    fresh = journal.get(op_id)
    applied_now = [s for s in fresh["steps"] if s["state"] == STEP_APPLIED]
    final_state = _compensate_applied(journal, op_id, applied_now,
                                      all_failed=(len(applied_now) == 0))
    result = journal.get(op_id)
    result["failure"] = str(failure)  # type: ignore[index]
    result["final_state"] = final_state  # type: ignore[index]
    return result


def _compensate_applied(journal: OperationJournal, op_id: str,
                        applied_steps: List[Dict[str, Any]],
                        all_failed: bool = False) -> str:
    """Reverse-order compensation guarded by post-write revisions.
    Returns the final operation state."""
    conflicts = 0
    for step in reversed(applied_steps):
        if step["state"] != STEP_APPLIED:
            continue
        adapter = adapter_for(step["store_kind"], step["path"], step.get("profile"))
        try:
            adapter.compensate(step["snapshot"], step["post_revision"])
        except StoreConflict:
            conflicts += 1
            journal.set_step_state(op_id, step["seq"], STEP_CONFLICT)
            continue
        journal.set_step_state(op_id, step["seq"], STEP_COMPENSATED)
    if conflicts:
        journal.finish(op_id, OP_RECOVERY_REQUIRED)
        return OP_RECOVERY_REQUIRED
    if all_failed:
        journal.finish(op_id, OP_PENDING)  # nothing wrote; op is retryable
        return OP_PENDING
    journal.finish(op_id, OP_COMPENSATED)
    return OP_COMPENSATED


# -- crash / timeout reconciliation ----------------------------------

def reconcile(journal: OperationJournal, op_id: str) -> Dict[str, Any]:
    """After a crash/timeout: compare persisted intent against actual files.

    For every applied step:
      - revision still == post_revision and readback holds -> commit is
        intact; keep it as applied (operation can be completed or undone).
      - revision changed -> an external writer intervened after the
        commit; the step is ambiguous -> recovery_required.
    For pending steps: verify no write happened (revision unchanged from
    pre_revision); if it changed, ambiguous -> recovery_required.
    """
    op = journal.get(op_id)
    if op is None:
        raise ApplyBlocked(f"unknown operation {op_id}")
    if op["state"] in (OP_COMMITTED, OP_COMPENSATED, OP_UNDONE,
                       OP_RECOVERY_REQUIRED):
        return op

    ambiguous = False
    for step in op["steps"]:
        adapter = adapter_for(step["store_kind"], step["path"], step.get("profile"))
        current = adapter.revision()
        if step["state"] == STEP_APPLIED:
            if current != step["post_revision"] or \
                    not adapter.readback(step["changes"]):
                ambiguous = True
                journal.set_step_state(op_id, step["seq"], STEP_CONFLICT)
        elif step["state"] == "pending":
            if current != step["pre_revision"]:
                # the file changed since our recheck. Determine whether the
                # on-disk bytes are exactly what our write would have
                # produced (crash between write and journal update) —
                # canonical serialization makes this decidable. Anything
                # else is an external edit -> ambiguous.
                base = step["snapshot"] if step["snapshot"] is not None else {}
                expected = _changes_applied(base, step["changes"])
                if (hasattr(adapter, "expected_revision")
                        and adapter.exists()
                        and adapter.expected_revision(expected) == current
                        and adapter.readback(step["changes"])):
                    journal.set_step_state(op_id, step["seq"], STEP_APPLIED,
                                           current)
                else:
                    ambiguous = True
                    journal.set_step_state(op_id, step["seq"], STEP_CONFLICT)

    if ambiguous:
        journal.finish(op_id, OP_RECOVERY_REQUIRED)
    else:
        fresh = journal.get(op_id)
        if all(s["state"] == STEP_APPLIED for s in fresh["steps"]):
            journal.finish(op_id, OP_COMMITTED)
        else:
            applied = [s for s in fresh["steps"] if s["state"] == STEP_APPLIED]
            state = _compensate_applied(journal, op_id, applied,
                                        all_failed=len(applied) == 0)
            if state == OP_PENDING:
                journal.finish(op_id, OP_PARTIAL)
    return journal.get(op_id)  # type: ignore[return-value]


def _changes_applied(base: Dict[str, Any],
                     changes: Dict[str, Any]) -> Dict[str, Any]:
    """In-memory projection of snapshot + planned changes (no I/O)."""
    import copy
    data = copy.deepcopy(base)
    for dotted, value in changes.items():
        _set_dotted(data, dotted, value)
    return data


# -- explicit, revision-guarded undo ----------------------------------

def undo_operation(journal: OperationJournal, op_id: str) -> Dict[str, Any]:
    """Undo a committed operation: restore snapshots in reverse order,
    only where the current revision still equals the recorded post-write
    revision. A later external edit is preserved -> conflict, not clobber."""
    op = journal.get(op_id)
    if op is None:
        raise ApplyBlocked(f"unknown operation {op_id}")
    applied = [s for s in op["steps"] if s["state"] == STEP_APPLIED]
    if not applied:
        raise ApplyBlocked("operation has no applied steps to undo")
    conflicts = 0
    for step in reversed(applied):
        adapter = adapter_for(step["store_kind"], step["path"], step.get("profile"))
        try:
            adapter.compensate(step["snapshot"], step["post_revision"])
        except StoreConflict:
            conflicts += 1
            journal.set_step_state(op_id, step["seq"], STEP_CONFLICT)
            continue
        journal.set_step_state(op_id, step["seq"], STEP_COMPENSATED)
    if conflicts:
        journal.finish(op_id, OP_RECOVERY_REQUIRED)
    else:
        journal.finish(op_id, OP_UNDONE)
    return journal.get(op_id)  # type: ignore[return-value]
