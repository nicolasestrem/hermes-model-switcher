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

HOST-EXECUTOR STEP PROTOCOL (public seam; dashboard/plugin_api.py builds on it)
-----------------------------------------------------------------------------
Every step names its EXECUTOR. ``executor='backend'`` (the default) is the
original path: the engine writes through the store adapter. ``executor='host'``
means the engine must NOT write: the browser performs one PUBLIC Hermes host
call and reports the outcome back. The engine keeps the guarantee that no
unverified or foreign write survives.

    plan_operation(journal, targets, payload=None, op_id=None) -> op
        Each target may carry:
          executor:          'backend' (default) | 'host'
          host_call:         {"method","path","query","body"}  (host only)
          readback_keys:     dotted config keys the route's write must satisfy
          allowed_prefixes:  dotted key prefixes the route may legitimately
                             touch (default: exactly the step's changes)
        All of it is journalled before any write; ``host_call`` and the
        readback keys are persisted so a restart can still verify the call.

    apply_operation(journal, op_id, fail_after=None) -> op
        Runs backend steps. On reaching a host step it rechecks the revision
        (a mismatch takes the ordinary PRE-WRITE conflict path), mints a
        one-time token, stores ONLY its SHA-256 and returns
            op["state"] == "awaiting_host"
            op["host_call"] == {"opId","seq","token","method","path","query","body"}
        The plaintext token exists only in that return value: it is never
        persisted, journal-read or logged. Calling apply_operation again on an
        awaiting step re-issues a fresh token (the previous one is retired) if
        the store is still untouched, and otherwise resolves the step exactly
        like a lost result (see reconcile).

    submit_host_result(journal, op_id, seq, token, outcome,
                       status=None, readback_keys=None, allowed_prefixes=None)
        -> op
        ``outcome`` is 'ok' | 'no_write' | 'declined' | 'error' (None derives
        it from ``status``). Evaluation, in order:
          1. revision unmoved  -> provably no write; the step fails with
             host_no_write / host_declined / host_error (write_phase PRE).
          2. store moved and the step's write verifies (planned changes read
             back, semantic readback keys hold, nothing outside the allowed
             prefixes moved):
               outcome ok          -> applied; continue to the next step (which
                                      may be another host call)
               outcome not ok      -> the write landed anyway: recorded with its
                                      post-revision and compensated back
                                      (host_no_write/host_declined/host_error)
          3. store moved but the document is exactly the intended write while a
             semantic key fails -> UNVERIFIED write: recorded and compensated
             back (write_verification_failed); no unverified write survives.
          4. store moved any other way -> FOREIGN diff: the step is a conflict,
             the operation becomes recovery_required and NOTHING is compensated
             (external_edit; never a blind restore over someone else's edit).
        A token that is missing, wrong, retired or replayed raises
        ApplyHostTokenMismatch (category host_token_mismatch) with NO state
        change; comparison is hmac.compare_digest over the SHA-256.

    reconcile(journal, op_id) -> op
        An ``awaiting_host`` step (lost result / restart) is classified exactly
        like outcome 'error': revision unmoved -> known no-write; our intended
        write -> written-but-unconfirmed, so it is compensated back per the
        reverse-order rules; anything else -> external_edit/recovery_required.

Compensation and undo of a host step restore the step's snapshot through the
step's backend adapter exactly as a backend step does, under the same
post-write revision guard.

AMBIGUOUS WRITES (backend and host alike)
-----------------------------------------
A save whose strict readback is NOT the document we intended is ambiguous, not
ours. ``adapters.StoreWriteAmbiguous`` (no post_revision) and a host step's
foreign diff both take the SAME path, at the moment they are detected:

  * the step is durably marked ``conflict`` with ``write_phase`` ambiguous;
  * the operation becomes ``recovery_required`` with the fixed category
    ``external_edit``;
  * NOTHING is compensated -- not that step (its bytes are not ours) and not
    the clean steps before it (their snapshots would be restored without ever
    having seen the ambiguous store);
  * the step is never retried and never recorded as pending;
  * ``undo_operation`` REFUSES the operation (``ApplyUndoAmbiguous``), because a
    snapshot restore would clobber bytes we cannot prove are ours;
  * ``reconcile`` never downgrades a conflict step.

A human resolves the store and then plans a fresh operation.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any, Dict, List, Optional, Tuple

from .adapters import (
    StoreConflict,
    StoreUnreadable,
    StoreWriteAmbiguous,
    StoreAdapter,
    _MISSING,
    _get_dotted,
    _set_dotted,
    adapter_for,
)
from .journal import (
    CALL_BACKEND,
    CALL_HOST,
    FAILURE_CATEGORIES,
    OperationJournal,
    OP_APPLYING,
    OP_AWAITING_HOST,
    STEP_PENDING,
    OP_COMMITTED,
    OP_COMPENSATED,
    OP_PARTIAL,
    OP_PENDING,
    OP_RECOVERY_REQUIRED,
    OP_UNDONE,
    STEP_APPLIED,
    STEP_AWAITING_HOST,
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
    """Preflight failed; no writes were started.

    The API must never echo ``str(exc)`` into a response (CodeQL
    py/stack-trace-exposure): preflight messages carry store paths, YAML parser
    text and ``repr()`` of target dicts (which can hold config values). Each
    subtype therefore fixes its public wording as a class attribute, so the
    handlers read ``type(exc).public_reason`` -- a constant -- instead of the
    message. The detail stays available to direct callers and tests.
    """

    public_reason = "operation blocked"


class ApplyTargetProfileGone(ApplyBlocked):
    public_reason = "target profile no longer exists"


class ApplyTargetUnbound(ApplyBlocked):
    public_reason = "write target is missing its store binding"


class ApplyTargetConflict(ApplyBlocked):
    public_reason = "conflicting values for a merged target key"


class ApplyAdapterUnavailable(ApplyBlocked):
    public_reason = "target store is not writable in this runtime"


class ApplyStoreUnreadable(ApplyBlocked):
    public_reason = "a target store could not be read"


class ApplySnapshotRace(ApplyBlocked):
    public_reason = "a target store changed while the recovery snapshot was captured"


class ApplyRevisionMoved(ApplyBlocked):
    public_reason = "the reviewed revision changed before planning"


class ApplyUnknownOperation(ApplyBlocked):
    public_reason = "unknown operation"


class ApplyNothingToUndo(ApplyBlocked):
    public_reason = "operation has no applied steps to undo"


class ApplyUndoAmbiguous(ApplyBlocked):
    """Undo is refused for an operation that carries an ambiguous write.

    A step whose save landed but whose strict readback shows a foreign or
    undecidable document (``StoreWriteAmbiguous``) is never adopted as ours: its
    snapshot must not be restored over someone else's bytes, and the other steps
    of that operation must not be undone piecemeal either. A human resolves the
    operation first.
    """

    public_reason = "operation is ambiguous and cannot be undone"


class ApplyHostCallUnbound(ApplyBlocked):
    public_reason = "host step is missing its host call"


class ApplyHostTokenMismatch(ApplyBlocked):
    """The submitted token is not the one this step is awaiting.

    Covers a forged/absent token, a token for another step, and the REPLAY of a
    token that was already used (the hash is retired on acceptance). Deliberately
    a fixed ApplyBlocked subtype with a fixed category: the rejection makes NO
    state change, so a replay can never advance or damage the operation, and no
    exception text (which could echo the presented token) leaves the engine.
    """

    public_reason = "host result token is not valid for this step"
    category = "host_token_mismatch"


class ApplyHostResultRejected(ApplyBlocked):
    """The step is not awaiting a host result, or the outcome is unknown."""

    public_reason = "host result is not accepted for this step"



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
    # An explicit category is only honoured when it is in the ONE fixed
    # vocabulary (journal.FAILURE_CATEGORIES) -- an arbitrary attribute can
    # never smuggle a free-form string into the journal or the public DTO.
    explicit = getattr(exc, "category", None)
    if isinstance(explicit, str) and explicit in FAILURE_CATEGORIES:
        return explicit
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
                raise ApplyTargetProfileGone(
                    "target profile vanished before planning")
            t["identity"] = asdict(identity)
        if "store_kind" not in t or "path" not in t:
            # Never repr() the target: it carries the store path and the
            # change values (which can be credentials).
            raise ApplyTargetUnbound("target missing store binding")
        executor = t.get("executor") or CALL_BACKEND
        if executor not in (CALL_BACKEND, CALL_HOST):
            # Fail closed at PLAN time: an unknown executor could otherwise be
            # treated as backend and silently write behind the reviewed intent.
            raise ApplyAdapterUnavailable("unknown step executor")
        host_call = t.get("host_call") or None
        if executor == CALL_HOST:
            if not isinstance(host_call, dict) or not host_call.get("path"):
                raise ApplyHostCallUnbound("host step without a host call")
            host_call = {
                "method": str(host_call.get("method") or "POST").upper(),
                "path": host_call["path"],
                "query": dict(host_call.get("query") or {}),
                "body": dict(host_call.get("body") or {}),
            }
        else:
            host_call = None
        # Coalesce targets that address the same (profile, store_kind, path):
        # multi-slot presets against one config store become ONE journalled
        # step, so a single pre_revision guards every change atomically.
        key = (t.get("profile"), t["store_kind"], t["path"])
        if key in group_index:
            grouped = prepared[group_index[key]]
            if grouped["executor"] != executor:
                # One store, two executors: the second step's revision recheck
                # could never hold, and merging them would silently decide who
                # writes. Refuse instead of guessing.
                raise ApplyTargetConflict(
                    "conflicting executors for one merged store target")
            if grouped["host_call"] != host_call:
                # Two different host calls for one store cannot be one step:
                # only one of them would ever run, silently dropping intent.
                raise ApplyTargetConflict(
                    "conflicting host calls for one merged store target")

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
                    raise ApplyTargetConflict(
                        "conflicting values for merged target key %r" % dotted)
                merged[dotted] = value
            continue
        try:
            adapter = _adapter_for_step(t)
        except RuntimeError as exc:
            raise ApplyAdapterUnavailable(str(exc)) from exc
        try:
            snapshot_revision = adapter.revision()
            snap = snapshot_adapter(adapter)
        except StoreUnreadable as exc:
            # an unreadable raw file is a preflight failure, not empty state
            raise ApplyStoreUnreadable(str(exc)) from exc
        group_index[key] = len(prepared)
        revision = adapter.revision()
        if revision != snapshot_revision:
            raise ApplySnapshotRace(
                "store revision changed while capturing recovery snapshot")
        if t.get("expected_revision") is not None and t["expected_revision"] != revision:
            raise ApplyRevisionMoved("reviewed revision changed before planning")
        prepared.append({
            "profile": t.get("profile"),
            "store_kind": t["store_kind"],
            "path": t["path"],
            "changes": dict(t["changes"]),
            "snapshot": snap,
            "pre_revision": revision,
            "identity": t.get("identity"),
            "executor": executor,
            "host_call": host_call,
            "readback_keys": list(t.get("readback_keys") or []) or None,
            "allowed_prefixes": list(t.get("allowed_prefixes") or []) or None,
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
                         p["pre_revision"], identity=p.get("identity"),
                         executor=p["executor"], host_call=p["host_call"],
                         readback_keys=p["readback_keys"],
                         allowed_prefixes=p["allowed_prefixes"])
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


_HOST_OUTCOMES = ("ok", "no_write", "declined", "error")

# The category a host outcome takes when the store proves NO write landed
# (and, symmetrically, when a write that DID land contradicts the report).
# The host reports WHAT IT OBSERVED; the store decides what actually happened
# (never the other way round).
_HOST_FAILURE_CATEGORY = {
    "ok": "host_no_write",       # claimed success, nothing was written
    "no_write": "host_no_write",
    "declined": "host_declined",
    "error": "host_error",
}


def _hash_token(token: str) -> str:
    """SHA-256 of the one-time host submission token. Only this is persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _under_prefixes(dotted: str, prefixes: List[str]) -> bool:
    return any(dotted == p or dotted.startswith(p + ".") for p in prefixes)


def _leaf_keys(value: Any, prefix: str) -> List[str]:
    if isinstance(value, dict) and value:
        keys: List[str] = []
        for name, sub in value.items():
            keys.extend(_leaf_keys(sub, "%s.%s" % (prefix, name)
                                   if prefix else str(name)))
        return keys
    return [prefix]


def _changed_keys(before: Any, after: Any, prefix: str = "") -> List[str]:
    """Dotted leaf paths whose value differs between two documents.

    Added and removed subtrees expand to their leaves, so a host route that
    dropped ``model.base_url`` is reported as that key, not as ``model``.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        keys: List[str] = []
        for name in set(before) | set(after):
            sub = "%s.%s" % (prefix, name) if prefix else str(name)
            if name in before and name in after:
                keys.extend(_changed_keys(before[name], after[name], sub))
            else:
                keys.extend(_leaf_keys(before.get(name, after.get(name)), sub))
        return keys
    return [prefix] if before != after else []


def _host_expectations(step: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(semantic readback keys, allowed key prefixes) for a host step.

    Both are journalled at plan time (or adopted from the first submission) so
    reconcile can still verify the call after a restart. The default is
    deliberately narrow: the step's own changes are the only keys it may move.
    """
    readback_keys = [str(k) for k in (step.get("host_readback_keys") or [])
                     if str(k).strip()]
    # Tolerate a route prefix written as "model." (trailing dot) -- callers pass
    # the route's own vocabulary -- and never let the list collapse to "nothing
    # is allowed", which would make every write look foreign.
    allowed: List[str] = []
    for prefix in (list(step.get("host_allowed_prefixes") or [])
                   or sorted(step.get("changes") or {})):
        cleaned = str(prefix).strip().rstrip(".")
        if cleaned:
            allowed.append(cleaned)
    if not allowed:
        allowed = sorted(step.get("changes") or {})
    return readback_keys, allowed


def _host_write_verified(adapter: StoreAdapter, step: Dict[str, Any],
                         data: Optional[Dict[str, Any]]) -> bool:
    """True iff the on-disk document is THIS step's verified write.

    Three independent conditions, all required:
      1. every planned dotted change reads back to its intended value;
      2. every semantic readback key holds the intended document's value (the
         route's own contract, e.g. provider+model);
      3. nothing outside the route's allowed key prefixes moved.
    Anything that fails 3 is a FOREIGN diff (someone else's edit), which the
    caller must never compensate over.
    """
    if data is None:
        return False
    try:
        if not adapter.readback(step["changes"]):
            return False
    except StoreUnreadable:
        return False
    readback_keys, allowed = _host_expectations(step)
    intended = _changes_applied(step.get("snapshot") or {}, step["changes"])
    for dotted in readback_keys:
        want = _get_dotted(intended, dotted)
        got = _get_dotted(data, dotted)
        if want is _MISSING or got is _MISSING or got != want:
            return False
    for dotted in _changed_keys(step.get("snapshot") or {}, data):
        if not _under_prefixes(dotted, allowed):
            return False
    return True


def _evaluate_host_step(step: Dict[str, Any], outcome: str) -> Tuple[str, Optional[str], str]:
    """Decide from the STORE what a host call actually did.

    Returns ``(kind, post_revision, category)``:
      "applied"  -> our verified write is on disk (post_revision recorded)
      "failed"   -> decided failure; post_revision set iff the write landed
      "conflict" -> a foreign diff: recovery_required, NO compensation
    """
    adapter = _adapter_for_step(step)
    try:
        current = adapter.revision()
    except (StoreUnreadable, OSError):
        # The store cannot be fingerprinted after the call: undecidable.
        return "conflict", None, "store_unreadable"
    if current == step["pre_revision"]:
        # Revision identical before and after the call: provably no write, so
        # the step is a KNOWN no-write (a later revision change is somebody
        # else's edit, never evidence of ours).
        return "failed", None, _HOST_FAILURE_CATEGORY[outcome]
    try:
        data = adapter.read()
    except StoreUnreadable:
        # It moved AND cannot be read: never guessed -> recovery_required.
        return "conflict", None, "store_unreadable"
    if _host_write_verified(adapter, step, data):
        if outcome == "ok":
            return "applied", current, ""
        # The host reported failure but ITS write is on disk. We own it, so
        # record the post-revision: compensation reverts it below.
        return "failed", current, _HOST_FAILURE_CATEGORY[outcome]
    if data is not None and adapter.intended_write_matches(
            step.get("snapshot"), step["changes"], current):
        # Exactly our intended document, yet a semantic key does not verify:
        # an UNVERIFIED write. It must not survive -> recorded and reverted.
        return "failed", current, "write_verification_failed"
    return "conflict", None, "external_edit"


def _mint_host_call(journal: OperationJournal, op_id: str,
                    step: Dict[str, Any]) -> Dict[str, Any]:
    """Hand a host step to the caller with a fresh one-time token.

    The plaintext token is returned ONCE, in ``op["host_call"]``. Only its
    SHA-256 is journalled, so neither a journal read nor a crash can leak it.
    """
    token = secrets.token_urlsafe(32)
    journal.set_awaiting_host(op_id, step["seq"], _hash_token(token))
    op = _require_op(journal, op_id)
    call = dict(step.get("host_call") or {})
    op["host_call"] = {
        "opId": op_id,
        "seq": step["seq"],
        "token": token,
        "method": call.get("method") or "POST",
        "path": call.get("path"),
        "query": dict(call.get("query") or {}),
        "body": dict(call.get("body") or {}),
    }
    return op


def _failure_result(journal: OperationJournal, op_id: str, category: str,
                    final_state: str) -> Dict[str, Any]:
    """Safe result shape: fixed category + final_state, never exception text."""
    result = _require_op(journal, op_id)
    result["failure_category"] = category
    result["final_state"] = final_state
    return result


def _finish_failure(journal: OperationJournal, op_id: str,
                    category: str) -> Dict[str, Any]:
    """Compensate the written steps in reverse order and record the category."""
    fresh = _require_op(journal, op_id)
    applied_now = [s for s in fresh["steps"]
                   if s["state"] == STEP_APPLIED
                   or (s["state"] == STEP_FAILED and s.get("post_revision"))]
    final_state = _compensate_applied(journal, op_id, applied_now,
                                      all_failed=(len(applied_now) == 0))
    journal.set_failure_category(op_id, category)
    return _failure_result(journal, op_id, category, final_state)


def _record_host_outcome(journal: OperationJournal, op_id: str,
                         step: Dict[str, Any],
                         outcome: str) -> Optional[Dict[str, Any]]:
    """Persist the classified host outcome.

    Returns None when the step was applied (the caller keeps driving), else the
    terminal operation result.
    """
    kind, post_rev, category = _evaluate_host_step(step, outcome)
    seq = step["seq"]
    # Whatever the verdict, the token is spent: retire it BEFORE recording, so
    # a replay can never observe an accepted-but-still-live token.
    journal.clear_host_token(op_id, seq)
    if kind == "applied":
        journal.set_step_state(op_id, seq, STEP_APPLIED, post_rev)
        return None
    if kind == "conflict":
        # A foreign diff: the operation needs a human, and NOTHING is
        # compensated blind over an edit that is not ours.
        journal.set_step_phase(op_id, seq, WRITE_PHASE_AMBIGUOUS)
        journal.set_step_state(op_id, seq, STEP_CONFLICT)
        journal.set_failure_category(op_id, category)
        journal.finish(op_id, OP_RECOVERY_REQUIRED)
        return _failure_result(journal, op_id, category, OP_RECOVERY_REQUIRED)
    journal.set_step_state(op_id, seq, STEP_FAILED, post_rev)
    journal.set_step_phase(op_id, seq,
                           WRITE_PHASE_POST if post_rev else WRITE_PHASE_PRE)
    return _finish_failure(journal, op_id, category)


def _require_op(journal: OperationJournal, op_id: str) -> Dict[str, Any]:
    """Journal lookup that fails as a fixed preflight, never as a TypeError."""
    op = journal.get(op_id)
    if op is None:
        raise ApplyUnknownOperation("unknown operation")
    return op


def _step_by_seq(op: Dict[str, Any], seq: int) -> Optional[Dict[str, Any]]:
    for step in op.get("steps", []):
        if step.get("seq") == seq:
            return step
    return None


def _normalize_host_outcome(outcome: Optional[str],
                            status: Optional[int]) -> str:
    """Validate the reported outcome (or derive it from an HTTP status)."""
    if outcome is None and status is not None:
        try:
            code = int(status)
        except (TypeError, ValueError):
            raise ApplyHostResultRejected("unparseable host status")
        if 200 <= code < 300:
            return "ok"
        if 400 <= code < 500:
            return "declined"
        return "error"
    if not isinstance(outcome, str) or outcome not in _HOST_OUTCOMES:
        raise ApplyHostResultRejected("unknown host outcome")
    return outcome


def _drive(journal: OperationJournal, op_id: str,
           fail_after: Optional[int] = None) -> Dict[str, Any]:
    """Advance an operation from its current position.

    Runs backend steps, stops at a host step (returning the one-time
    ``host_call``), and finishes or compensates. Shared by apply_operation,
    submit_host_result and host-result recovery, so ONE state machine decides
    every transition.
    """
    op = _require_op(journal, op_id)
    applied_steps = 0
    failure: Optional[BaseException] = None
    for step in op["steps"]:
        seq = step["seq"]
        if step["state"] == STEP_APPLIED:
            applied_steps += 1
            continue
        if step["state"] == STEP_AWAITING_HOST:
            # Resume: the plaintext token is gone (never persisted), so a new
            # one is minted -- but ONLY while the store is still untouched.
            # Anything else is a lost result and is decided from the store,
            # exactly like a host error.
            adapter = _adapter_for_step(step)
            try:
                current = adapter.revision()
            except StoreUnreadable:
                return _finish_failure(journal, op_id, "store_unreadable")
            if current == step["pre_revision"]:
                return _mint_host_call(journal, op_id, step)
            resolved = _record_host_outcome(journal, op_id, step, "error")
            if resolved is not None:
                return resolved
            applied_steps += 1
            continue
        if step["state"] != STEP_PENDING:
            failure = StepFailure(
                f'step {seq} in non-applyable state {step["state"]}')
            break
        if fail_after is not None and applied_steps >= fail_after:
            # The injected fault fires BEFORE the step attempts a write, so
            # this step provably never wrote.
            journal.set_step_state(op_id, seq, STEP_FAILED)
            journal.set_step_phase(op_id, seq, WRITE_PHASE_PRE)
            failure = StepFailure("injected failure before step "
                                  f"{seq}")
            break
        if step.get("executor") == CALL_HOST:
            # Recheck the revision BEFORE handing the call out: if the store
            # already moved, this takes the ordinary PRE-WRITE conflict path
            # (known no-write, compensate what landed, never a blind host call
            # against a stale revision).
            try:
                adapter = _adapter_for_step(step)
                current = adapter.revision()
                if current != step["pre_revision"]:
                    raise _PreWriteConflict(
                        f"revision changed before host call: {current} != "
                        f'{step["pre_revision"]}')
                if adapter.exists():
                    try:
                        adapter.read()
                    except StoreUnreadable as exc:
                        raise _PreWriteUnreadable(str(exc)) from exc
            except (StoreConflict, StoreUnreadable) as exc:
                failure = exc
                phase = getattr(exc, "write_phase", None) or WRITE_PHASE_AMBIGUOUS
                journal.set_step_state(op_id, seq, STEP_FAILED)
                journal.set_step_phase(op_id, seq, phase)
                break
            return _mint_host_call(journal, op_id, step)
        adapter = _adapter_for_step(step)
        try:
            post = _apply_step(adapter, step["changes"], step["pre_revision"])
        except StoreWriteAmbiguous:
            # A save WAS attempted and the strict readback is not the document
            # we intended (a foreign/undecidable write). Never adopt a
            # post-revision, never retry, never mark it pending, and never
            # compensate blindly -- not this step and not the ones before it
            # (their snapshots would be restored without ever having seen the
            # ambiguous store). The operation goes to recovery_required with
            # the fixed category used for every foreign-diff verdict.
            journal.set_step_phase(op_id, seq, WRITE_PHASE_AMBIGUOUS)
            journal.set_step_state(op_id, seq, STEP_CONFLICT)
            journal.set_failure_category(op_id, "external_edit")
            journal.finish(op_id, OP_RECOVERY_REQUIRED)
            return _failure_result(journal, op_id, "external_edit",
                                   OP_RECOVERY_REQUIRED)
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
            journal.set_step_state(op_id, seq, STEP_FAILED, post_rev)
            journal.set_step_phase(op_id, seq, phase)
            break
        journal.set_step_state(op_id, seq, STEP_APPLIED, post)
        applied_steps += 1

    if failure is not None:
        # Partial failure: compensate completed writes in reverse order,
        # re-reading the journal so post-write revisions are current.
        return _finish_failure(journal, op_id, _failure_category(failure))
    journal.finish(op_id, OP_COMMITTED)
    return journal.get(op_id)  # type: ignore[return-value]


def apply_operation(journal: OperationJournal, op_id: str,
                    fail_after: Optional[int] = None) -> Dict[str, Any]:
    """Apply the journalled operation sequentially.

    Stops at a host-executor step and returns it as ``awaiting_host`` with a
    one-time ``host_call``; backend steps run as before.

    fail_after: injected fault for tests — raise StepFailure after N
    successful steps (simulates a mid-rollout failure/timeout).
    """
    op = journal.get(op_id)
    if op is None:
        raise ApplyUnknownOperation("unknown operation")
    if op["state"] not in (OP_PENDING, OP_APPLYING, OP_AWAITING_HOST):
        # completed / already-compensated / recovery: a retry with the same
        # op_id returns the existing record instead of duplicating writes
        return op
    return _drive(journal, op_id, fail_after=fail_after)


def submit_host_result(journal: OperationJournal, op_id: str, seq: int,
                       token: str, outcome: Optional[str] = None,
                       status: Optional[int] = None,
                       readback_keys: Optional[List[str]] = None,
                       allowed_prefixes: Optional[List[str]] = None
                       ) -> Dict[str, Any]:
    """Report what the browser's PUBLIC host call actually did.

    The store -- never the report -- decides the verdict:
      - no write landed          -> decided failure (host_no_write /
                                    host_declined / host_error), known no-write;
      - our verified write landed -> applied (outcome ok) or, when the host
                                    reported failure anyway, recorded and
                                    compensated back;
      - an unverified write landed -> recorded and compensated back;
      - anything else moved       -> external_edit: recovery_required, and NO
                                    compensation of anybody's bytes.
    Then the engine keeps driving: backend steps run, the next host step becomes
    the new ``host_call``, or the operation commits.

    A missing/foreign/retired/replayed token raises ApplyHostTokenMismatch with
    NO state change.
    """
    op = journal.get(op_id)
    if op is None:
        raise ApplyUnknownOperation("unknown operation")
    step = _step_by_seq(op, seq)
    if step is None:
        raise ApplyHostResultRejected("no such step")
    stored = step.get("host_token_hash")
    presented = _hash_token(token if isinstance(token, str) else "")
    # Constant-time comparison over the digest. A retired token (accepted,
    # failed, or already submitted) has no stored hash -> never matches, so a
    # replay can neither advance nor damage the operation.
    if not isinstance(stored, str) or not hmac.compare_digest(stored, presented):
        raise ApplyHostTokenMismatch("host token rejected")
    if step["state"] != STEP_AWAITING_HOST:
        raise ApplyHostResultRejected("step is not awaiting a host result")
    # Consume the token ATOMICALLY before evaluating: a second concurrent
    # submission with the same token loses here and gets the fixed mismatch
    # refusal with no state change, instead of evaluating the same store twice.
    if not journal.consume_host_token(op_id, seq, presented):
        raise ApplyHostTokenMismatch("host token already consumed")
    normalized = _normalize_host_outcome(outcome, status)
    # The step's journalled expectations are authoritative (they survive a
    # restart); a caller may only SUPPLY them when the planner had none.
    if readback_keys and step.get("host_readback_keys") is None:
        journal.set_host_expectations(op_id, seq, readback_keys=readback_keys)
    if allowed_prefixes and step.get("host_allowed_prefixes") is None:
        journal.set_host_expectations(op_id, seq,
                                      allowed_prefixes=allowed_prefixes)
    step = _step_by_seq(_require_op(journal, op_id), seq) or step
    terminal = _record_host_outcome(journal, op_id, step, normalized)
    if terminal is not None:
        return terminal
    return _drive(journal, op_id)


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
    For ``awaiting_host`` steps (a host call with no recorded result: restart
    or a lost response) the store decides, exactly as for a host ERROR -- see
    the branch below. Never a blind compensation.
    """
    op = journal.get(op_id)
    if op is None:
        raise ApplyUnknownOperation("unknown operation")
    if op["state"] in (OP_COMMITTED, OP_COMPENSATED, OP_UNDONE,
                       OP_RECOVERY_REQUIRED):
        return op

    ambiguous = False
    # Host steps resolved in THIS call whose write landed but whose result was
    # lost: written-but-unconfirmed, so they join reverse-order compensation
    # exactly like applied steps.
    landed_host: List[Dict[str, Any]] = []
    for step in op["steps"]:
        adapter = _adapter_for_step(step)
        current = adapter.revision()
        if step["state"] == STEP_CONFLICT:
            # Already ambiguous (a foreign diff, an unverifiable host write, or
            # a StoreWriteAmbiguous save). reconcile NEVER downgrades it to
            # pending/compensated: a human decides.
            ambiguous = True
        elif step["state"] == STEP_APPLIED:
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
        elif step["state"] == STEP_AWAITING_HOST:
            # A host call was handed out and no result was ever recorded
            # (restart, crash, lost response). Decided exactly like a host
            # ERROR because we cannot know whether the browser's route ran:
            #   - revision unmoved        -> provably no write;
            #   - our intended write      -> written but unconfirmed, recorded
            #                                with its post-revision so the
            #                                reverse-order compensation below
            #                                reverts it;
            #   - anything else moved     -> a foreign diff -> recovery_required.
            kind, post_rev, category = _evaluate_host_step(step, "error")
            # The token is spent the moment the step is decided, so a late
            # submission cannot resurrect an already-reconciled step.
            journal.clear_host_token(op_id, step["seq"])
            if kind == "conflict":
                ambiguous = True
                journal.set_step_phase(op_id, step["seq"],
                                       WRITE_PHASE_AMBIGUOUS)
                journal.set_step_state(op_id, step["seq"], STEP_CONFLICT)
                journal.set_failure_category(op_id, category)
            else:
                journal.set_step_state(op_id, step["seq"], STEP_FAILED,
                                       post_rev)
                journal.set_step_phase(
                    op_id, step["seq"],
                    WRITE_PHASE_POST if post_rev else WRITE_PHASE_PRE)
                journal.set_failure_category(op_id, category)
                if post_rev is not None:
                    landed_host.append(step)
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
            landed_seqs = {s["seq"] for s in landed_host}
            applied.extend(s for s in fresh["steps"]
                           if s["state"] == STEP_FAILED
                           and s.get("post_revision")
                           and s["seq"] in landed_seqs)
            applied.sort(key=lambda s: s["seq"])
            state = _compensate_applied(journal, op_id, applied,
                                        all_failed=len(applied) == 0)
            if state == OP_PENDING:
                # Nothing was written and nothing is ambiguous: no store was
                # touched, so the record is kept OP_PENDING rather than
                # OP_PARTIAL (zero-applied is not "some applied, compensation
                # done"). This does NOT make the op resumable under the same
                # op_id: the step that failed is STEP_FAILED, not pending, and
                # apply_operation refuses any non-pending step, so a same-id
                # retry re-fails immediately. A fresh review and a new
                # operation are required to proceed.
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
        raise ApplyUnknownOperation("unknown operation")
    if op["state"] == OP_RECOVERY_REQUIRED or any(
            s["state"] == STEP_CONFLICT for s in op["steps"]):
        # An ambiguous write is nobody's to roll back from here: restoring the
        # snapshot would clobber bytes we cannot prove are ours, and undoing
        # only the clean steps would half-revert a store whose real state is
        # unknown.
        raise ApplyUndoAmbiguous("operation is ambiguous")
    applied = [s for s in op["steps"] if s["state"] == STEP_APPLIED]
    if not applied:
        raise ApplyNothingToUndo("operation has no applied steps to undo")
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
