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
    WRITE_PHASE_AMBIGUOUS,
    WRITE_PHASE_POST,
    WRITE_PHASE_PRE,
)

# A per-profile, per-store write target.
# {"profile": str, "store_kind": str, "path": str, "changes": {dotted: value}}


class ApplyBlocked(RuntimeError):
    """Preflight failed; no writes were started."""


class StepFailure(RuntimeError):
    """A write step failed after it started; triggers compensation."""
    post_revision: Optional[str] = None  # set when the write already landed


class _PreWriteConflict(StoreConflict):
    """Revision recheck refused BEFORE any write: known no-write.

    Subclasses StoreConflict so the failure category is unchanged; the
    ``write_phase`` tag is what tells reconcile this step provably never
    touched the store."""

    write_phase = WRITE_PHASE_PRE


class _PreWriteUnreadable(StoreUnreadable):
    """Fresh read failed BEFORE any write: known no-write (see above)."""

    write_phase = WRITE_PHASE_PRE


def snapshot_adapter(adapter: StoreAdapter) -> Optional[Dict[str, Any]]:
    """Private raw snapshot of the store before writing.
    missing-vs-present is preserved (None means the file did not exist)."""
    if not adapter.exists():
        return None
    data = adapter.read()  # may raise StoreUnreadable
    return data


def _adapter_for_step(step: Dict[str, Any]) -> StoreAdapter:
    identity = step.get("identity")
    if identity is None:
        return adapter_for(step["store_kind"], step["path"], step.get("profile"))
    if isinstance(identity, dict):
        from hermes_adapter import ProfileIdentity
        identity = ProfileIdentity(**identity)
    return adapter_for(step["store_kind"], step["path"], step.get("profile"),
                       identity=identity)


_FAILURE_CATEGORIES = (
    (StoreConflict, "revision_conflict"),
    (StoreUnreadable, "store_unreadable"),
    (StepFailure, "write_verification_failed"),
    (OSError, "io_error"),
)


def _failure_category(exc: BaseException) -> str:
    """Map a step failure to a FIXED, non-sensitive category label.

    The exception message can embed config bytes or credentials, so it must
    never be persisted or reported. Only one of these fixed labels leaves the
    engine (contract: a safe failure category, never arbitrary exception text).
    """
    for exc_type, label in _FAILURE_CATEGORIES:
        if isinstance(exc, exc_type):
            return label
    return "step_failed"


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
    group_index: Dict[Any, int] = {}
    for t in targets:
        t = dict(t)
        if t.get("store_kind") == "canonical_config" and not t.get("identity"):
            from dataclasses import asdict
            from hermes_adapter import discover_profiles
            identity = discover_profiles().get(t.get("profile") or "")
            if identity is None:
                raise ApplyBlocked("target profile vanished before planning")
            t["identity"] = asdict(identity)
        if "store_kind" not in t or "path" not in t:
            raise ApplyBlocked(f"target missing store binding: {t!r}")
        # Coalesce targets that address the same (profile, store_kind, path):
        # multi-slot presets against one config store become ONE journalled
        # step, so a single pre_revision guards every change atomically.
        key = (t.get("profile"), t["store_kind"], t["path"])
        if key in group_index:
            # Coalesce same-store targets into ONE journalled step, but never
            # let two targets silently disagree on a dotted key: last-writer
            # wins would hide a conflicting preset behind an arbitrary order.
            # Identical duplicate values remain fine (common in multi-slot
            # presets that share a route field).
            merged = prepared[group_index[key]]["changes"]
            for dotted, value in t["changes"].items():
                if dotted in merged and merged[dotted] != value:
                    # Report only the (non-secret) dotted key name — a value
                    # such as base_url may embed credentials.
                    raise ApplyBlocked(
                        "conflicting values for merged target key %r" % dotted)
                merged[dotted] = value
            continue
        try:
            adapter = _adapter_for_step(t)
        except RuntimeError as exc:
            raise ApplyBlocked(str(exc)) from exc
        try:
            snapshot_revision = adapter.revision()
            snap = snapshot_adapter(adapter)
        except StoreUnreadable as exc:
            # an unreadable raw file is a preflight failure, not empty state
            raise ApplyBlocked(str(exc)) from exc
        group_index[key] = len(prepared)
        revision = adapter.revision()
        if revision != snapshot_revision:
            raise ApplyBlocked("store revision changed while capturing recovery snapshot")
        if t.get("expected_revision") is not None and t["expected_revision"] != revision:
            raise ApplyBlocked("reviewed revision changed before planning")
        prepared.append({
            "profile": t.get("profile"),
            "store_kind": t["store_kind"],
            "path": t["path"],
            "changes": dict(t["changes"]),
            "snapshot": snap,
            "pre_revision": revision,
            "identity": t.get("identity"),
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
                         p["pre_revision"], identity=p.get("identity"))
    return journal.get(op["op_id"])  # type: ignore[return-value]


def _apply_step(adapter: StoreAdapter, changes: Dict[str, Any],
                pre_revision: str) -> str:
    """Sequential apply with revision recheck + fresh read + readback.

    A failure in the recheck/fresh-read phase happens BEFORE any write, so it
    is tagged ``WRITE_PHASE_PRE`` (known no-write). Everything from the guarded
    write onward is left untagged: the caller then records it as ambiguous
    unless a post_revision proves the write landed. We classify ONLY on the
    evidenced phase -- never assume every failure wrote, and never assume none
    did.
    """
    current = adapter.revision()
    if current != pre_revision:
        raise _PreWriteConflict(
            f"revision changed before write: {current} != {pre_revision}")
    # fresh read is part of the recheck (also surfaces unreadable files);
    # it runs before the write, so a failure here is also known no-write.
    if adapter.exists():
        try:
            adapter.read()
        except StoreUnreadable as exc:
            raise _PreWriteUnreadable(str(exc)) from exc
    outcome = adapter.apply_changes_guarded(
        changes, expected_revision=pre_revision)
    if not adapter.readback(changes):
        # The write already landed on disk but could not be verified.
        # Carry the post-write revision so the caller records the step as
        # written-but-failed and compensates it back to the snapshot —
        # no unverified write may survive.
        exc = StepFailure("readback verification failed after write")
        exc.post_revision = outcome.post_revision
        raise exc
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
            # The injected fault fires BEFORE the step attempts a write, so
            # this step provably never wrote.
            journal.set_step_state(op_id, step["seq"], STEP_FAILED)
            journal.set_step_phase(op_id, step["seq"], WRITE_PHASE_PRE)
            failure = StepFailure("injected failure before step "
                                  f'{step["seq"]}')
            break
        adapter = _adapter_for_step(step)
        try:
            post = _apply_step(adapter, step["changes"], step["pre_revision"])
        except (StoreConflict, StoreUnreadable, StepFailure, OSError) as exc:
            failure = exc
            post_rev = getattr(exc, "post_revision", None)
            # A post-write failure (e.g. readback) means the write DID land:
            # record the post-write revision so this step participates in
            # reverse-order compensation below.
            # Durable write phase: ``_apply_step`` tags the recheck/fresh-read
            # failures WRITE_PHASE_PRE (provably no write). Anything else
            # carries a post_revision (write landed) or is genuinely ambiguous
            # -> fail closed. Never assume "no write" for an unclassified
            # failure.
            phase = getattr(exc, "write_phase", None)
            if phase is None:
                phase = (WRITE_PHASE_POST if post_rev
                         else WRITE_PHASE_AMBIGUOUS)
            journal.set_step_state(op_id, step["seq"], STEP_FAILED, post_rev)
            journal.set_step_phase(op_id, step["seq"], phase)
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
    applied_now = [s for s in fresh["steps"]
                   if s["state"] == STEP_APPLIED
                   or (s["state"] == STEP_FAILED and s.get("post_revision"))]
    final_state = _compensate_applied(journal, op_id, applied_now,
                                      all_failed=(len(applied_now) == 0))
    category = _failure_category(failure)
    journal.set_failure_category(op_id, category)
    result = journal.get(op_id)
    # Safe, fixed category only — the raw exception text (which may embed
    # config bytes or credentials) never leaves the engine.
    result["failure_category"] = category  # type: ignore[index]
    result["final_state"] = final_state  # type: ignore[index]
    return result


def _compensate_applied(journal: OperationJournal, op_id: str,
                        applied_steps: List[Dict[str, Any]],
                        all_failed: bool = False) -> str:
    """Reverse-order compensation guarded by post-write revisions.
    Returns the final operation state."""
    conflicts = 0
    for step in reversed(applied_steps):
        # Applied steps and failed-with-post-revision steps (write landed,
        # verification or a later step then failed) must be compensated.
        if step["state"] != STEP_APPLIED and \
                not (step["state"] == STEP_FAILED and step.get("post_revision")):
            continue
        adapter = _adapter_for_step(step)
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
        adapter = _adapter_for_step(step)
        current = adapter.revision()
        if step["state"] == STEP_APPLIED:
            if current != step["post_revision"] or \
                    not adapter.readback(step["changes"]):
                ambiguous = True
                journal.set_step_state(op_id, step["seq"], STEP_CONFLICT)
        elif step["state"] == STEP_FAILED:
            # A step that failed BEFORE any write (WRITE_PHASE_PRE) provably
            # never touched the store: a changed revision here is an external
            # edit, NOT evidence of our write, so it is a known no-write step
            # and must NOT be escalated to recovery_required.
            if step.get("write_phase") == WRITE_PHASE_PRE:
                continue
            # Otherwise the write may have landed ambiguously (a concurrent
            # write detected after our save, or an exception after disk
            # replacement) with NO post revision, so it is skipped by ordinary
            # compensation. If the store no longer matches the pre-write
            # revision we cannot tell our write from an interloper's -> fail
            # closed to recovery_required, never a false success.
            if current != step["pre_revision"]:
                ambiguous = True
                journal.set_step_state(op_id, step["seq"], STEP_CONFLICT)
        elif step["state"] == "pending":
            if current != step["pre_revision"]:
                # The file changed since our recheck. Determine whether the
                # on-disk document is exactly what our write would have
                # produced (crash between write and journal update) — the
                # adapter-level intended-write predicate makes this decidable
                # (including canonical stores, whose bytes come from Hermes'
                # normalize/strip-default save seam, not a naive dump).
                # Anything else is an external edit -> ambiguous.
                if adapter.intended_write_matches(
                        step["snapshot"], step["changes"], current):
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
                # Nothing was written and nothing is ambiguous: this operation
                # never touched a store. Keep it OP_PENDING so it stays
                # retryable. OP_PARTIAL ("some applied, compensation done")
                # would mislabel zero-applied AND make the op un-retryable,
                # since apply_operation only resumes pending/applying.
                journal.finish(op_id, OP_PENDING)
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
        adapter = _adapter_for_step(step)
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


# -- safe public revision interface ----------------------------------

def revision_summary(op: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Safe per-step before/after revision view for public operation results.

    Allowlist only — profile, store kind and the recorded pre/post revisions.
    NEVER snapshots, storage paths, changed values, or raw config contents, so
    an API layer can expose write provenance without leaking private state.
    """
    if not op:
        return []
    return [
        {
            "profile": s.get("profile"),
            "store": s.get("store_kind"),
            "revision_before": s.get("pre_revision"),
            "revision_after": s.get("post_revision"),
        }
        for s in op.get("steps", [])
    ]
