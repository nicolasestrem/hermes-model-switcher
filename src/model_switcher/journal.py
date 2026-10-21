"""Durable SQLite operation journal: intent, steps, revisions, state.

The journal is the recovery source of truth. Operations are recorded
BEFORE the first write and updated after each step, so a crash or
timeout leaves enough persisted intent to reconcile actual files.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import uuid
from typing import Any, Dict, List, Optional

# Operation / step states
OP_PENDING = "pending"            # journalled, no writes yet
OP_APPLYING = "applying"
OP_COMMITTED = "committed"
OP_COMPENSATED = "compensated"
OP_PARTIAL = "partial"            # some applied, compensation done
OP_RECOVERY_REQUIRED = "recovery_required"
OP_UNDONE = "undone"
# The engine reached a host-executor step and handed the call to the caller.
# Nothing is written by the backend while this state is held; the operation
# resumes through submit_host_result() or, after a lost result/restart,
# through reconcile() -- which treats the step exactly like a host ERROR
# (i.e. the write may or may not have landed and is decided from the store).
OP_AWAITING_HOST = "awaiting_host"

STEP_PENDING = "pending"
STEP_APPLIED = "applied"
STEP_FAILED = "failed"
STEP_COMPENSATED = "compensated"
STEP_CONFLICT = "conflict"
STEP_AWAITING_HOST = "awaiting_host"

# Backend steps write through store adapters. Host steps give the browser a
# public route call; the engine verifies the resulting store without credentials.
CALL_BACKEND = "backend"
CALL_HOST = "host"

# Evidenced write PHASE of a failed step. Durable (survives a restart) and
# internal only: it is deliberately NOT in the public step projection.
#   WRITE_PHASE_PRE       -- the step failed BEFORE any write attempt
#                            (revision recheck / fresh read). The store was
#                            provably never touched by this step, so a later
#                            revision change is an external edit, NOT evidence
#                            of our write. The step stays retryable.
#   WRITE_PHASE_POST      -- the write landed (a post_revision is recorded).
#   WRITE_PHASE_AMBIGUOUS -- a write may or may not have landed; ownership is
#                            undecidable. Fail closed to recovery_required.
# Only WRITE_PHASE_PRE is safe to treat as a known no-write.
WRITE_PHASE_PRE = "pre_write"
WRITE_PHASE_POST = "post_write"
WRITE_PHASE_AMBIGUOUS = "ambiguous"

# Bounded wait for a competing writer (a dashboard request thread, the
# crash-recovery path, a CLI in another process). Combined with WAL this keeps
# concurrent opens from failing with "database is locked".
_BUSY_TIMEOUT_S = 5.0


def is_inside_git_worktree(path: str) -> bool:
    """True if `path` (or any parent up to /) contains a `.git` entry.

    Covers both checkouts (`.git` directory) and linked worktrees
    (`.git` file pointing at a common dir), so journal files can never be
    persisted inside ANY git worktree — including git-ignored subdirs,
    since ignores give no guarantee against accidental `git add -f`.
    """
    p = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


class OperationJournal:
    def __init__(self, db_path: str):
        raw = os.path.abspath(db_path)
        if os.path.islink(raw):
            raise ValueError(
                "journal db_path must not be a symlink: %r" % db_path)
        # No component of the path (the db itself or ANY ancestor) may be a
        # symlink: a link such as /outside/link/private/j.db would otherwise
        # bypass both the direct islink checks and the worktree guard below,
        # resolving into an unvetted location after makedirs created the
        # non-existent tail.
        probe = raw
        while True:
            if os.path.islink(probe):
                raise ValueError(
                    "journal db path component must not be a symlink: %r"
                    % probe)
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        if is_inside_git_worktree(raw):
            raise ValueError(
                "refusing to store operation journal inside a git worktree "
                "(%r); journal DBs may contain private recovery snapshots "
                "and must stay in a private directory" % db_path)
        # Re-check containment on the fully resolved path: mounts or link
        # races aside, realpath can only differ where a symlink smuggled the
        # location into a git worktree.
        resolved = os.path.realpath(raw)
        if is_inside_git_worktree(resolved):
            raise ValueError(
                "journal path resolves into a git worktree (%r): refusing "
                "to store the operation journal there" % db_path)
        db_dir = os.path.dirname(raw) or "."
        os.makedirs(db_dir, mode=0o700, exist_ok=True)
        # Private DB and parent: group/other get no access. Private
        # recovery snapshots stay local — they are the only source for
        # exact undo, but they must never be readable beyond the user.
        if os.path.islink(db_dir):
            raise ValueError("journal parent directory must not be a symlink")
        os.chmod(db_dir, stat.S_IRWXU)
        if os.name == "posix" and stat.S_IMODE(os.stat(db_dir).st_mode) != 0o700:
            raise PermissionError("journal directory permissions must be private")
        # Create privately before SQLite opens the file, so permissive umasks
        # cannot expose the initial database or its recovery snapshots.
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(raw, flags, 0o600)
        try:
            os.chmod(raw, stat.S_IRUSR | stat.S_IWUSR)
            if os.name == "posix" and stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise PermissionError("journal database permissions must be private")
        finally:
            os.close(fd)
        self.db_path = raw
        # This connection is owned by THIS journal and is closed on any init
        # error, so a failed open never leaks an fd or an open handle into the
        # state dir. WAL + a bounded busy timeout keep concurrent opens (a
        # dashboard request thread, the crash-recovery path, a CLI in another
        # process) from failing with "database is locked".
        conn = sqlite3.connect(raw, isolation_level=None,
                               timeout=_BUSY_TIMEOUT_S)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=%d" % int(_BUSY_TIMEOUT_S * 1000))
            # FULL, not NORMAL: the engine commits journal intent/snapshots
            # BEFORE any external config write, so the journal is the recovery
            # source of truth. NORMAL WAL may drop a committed journal write on
            # power failure, silently weakening that guarantee; FULL syncs the
            # WAL on every commit. Correctness over throughput for the journal.
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    op_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    op_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    profile TEXT NOT NULL,
                    store_kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    changes TEXT NOT NULL,
                    snapshot TEXT,
                    pre_revision TEXT,
                    post_revision TEXT,
                    PRIMARY KEY (op_id, seq),
                    FOREIGN KEY (op_id) REFERENCES operations(op_id)
                );
                """
            )
            # Additive migrations; existing private recovery snapshots stay
            # intact. Every migration is idempotent (a column-existence check),
            # so an OLD-schema journal keeps working after an upgrade instead of
            # failing on the first write.
            step_columns = {row[1] for row in conn.execute("PRAGMA table_info(steps)")}
            if "identity" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN identity TEXT")
            # Durable, internal write-phase marker for failed steps: lets
            # reconcile tell a provably-pre-write failure (known no-write)
            # from a write that may have landed, across a restart.
            if "write_phase" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN write_phase TEXT")
            # Host-executor steps: who writes ('host'|'backend'), the PUBLIC
            # host call the browser must perform (method/path/query/body --
            # internal, never projected publicly), the per-route semantic
            # readback keys and allowed key prefixes used to verify what
            # actually landed, and the SHA-256 of the one-time submission
            # token. The PLAINTEXT token is never persisted anywhere.
            if "executor" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN executor TEXT")
            if "host_call" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN host_call TEXT")
            if "host_token_hash" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN host_token_hash TEXT")
            if "host_readback_keys" not in step_columns:
                conn.execute(
                    "ALTER TABLE steps ADD COLUMN host_readback_keys TEXT")
            if "host_allowed_prefixes" not in step_columns:
                conn.execute(
                    "ALTER TABLE steps ADD COLUMN host_allowed_prefixes TEXT")
            op_columns = {row[1] for row in conn.execute("PRAGMA table_info(operations)")}
            if "failure_category" not in op_columns:
                conn.execute(
                    "ALTER TABLE operations ADD COLUMN failure_category TEXT")
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def begin(self, payload: Dict[str, Any],
              op_id: Optional[str] = None) -> Dict[str, Any]:
        """Journal the intent. If op_id already exists, return the existing
        operation (idempotency: retries never duplicate writes)."""
        if op_id is not None:
            existing = self.get(op_id)
            if existing is not None:
                return existing  # type: ignore[return-value]
        op_id = op_id or f"op-{uuid.uuid4().hex[:12]}"
        now = time.time()
        self._conn.execute(
            "INSERT INTO operations (op_id, state, created_at, updated_at, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (op_id, OP_PENDING, now, now, json.dumps(payload, sort_keys=True)),
        )
        return self.get(op_id)

    def add_step(self, op_id: str, seq: int, profile: str, store_kind: str,
                 path: str, changes: Dict[str, Any],
                 snapshot: Optional[Dict[str, Any]],
                 pre_revision: str, identity: Optional[Dict[str, str]] = None,
                 executor: Optional[str] = None,
                 host_call: Optional[Dict[str, Any]] = None,
                 readback_keys: Optional[List[str]] = None,
                 allowed_prefixes: Optional[List[str]] = None) -> None:
        """Record one step. ``executor``/``host_call``/``readback_keys``/
        ``allowed_prefixes`` are the host-executor seam: ``host_call`` is the
        PUBLIC route the browser must call (method/path/query/body), and the
        readback keys + allowed prefixes are what the engine later uses to
        decide whether whatever landed is THIS step's write. They are
        persisted so a restart can still verify the call (reconcile).
        """
        self._conn.execute(
            "INSERT INTO steps (op_id, seq, profile, store_kind, path, state,"
            " changes, snapshot, pre_revision, identity, executor, host_call,"
            " host_readback_keys, host_allowed_prefixes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, seq, profile, store_kind, path, STEP_PENDING,
             json.dumps(changes, sort_keys=True),
             json.dumps(snapshot, sort_keys=True) if snapshot is not None else None,
             pre_revision, json.dumps(identity) if identity is not None else None,
             executor or CALL_BACKEND,
             json.dumps(host_call, sort_keys=True) if host_call is not None else None,
             json.dumps(list(readback_keys)) if readback_keys is not None else None,
             json.dumps(list(allowed_prefixes)) if allowed_prefixes is not None else None),
        )
        self._touch(op_id, OP_APPLYING)

    def _touch(self, op_id: str, state: str) -> None:
        self._conn.execute(
            "UPDATE operations SET state=?, updated_at=? WHERE op_id=?",
            (state, time.time(), op_id))

    def set_step_state(self, op_id: str, seq: int, state: str,
                       post_revision: Optional[str] = None) -> None:
        # A None post_revision must not clobber a revision recorded earlier
        # (e.g. a written-but-failed step keeps its post-write revision
        # through compensation for the recovery trail).
        if post_revision is None:
            self._conn.execute(
                "UPDATE steps SET state=? WHERE op_id=? AND seq=?",
                (state, op_id, seq))
        else:
            self._conn.execute(
                "UPDATE steps SET state=?, post_revision=?"
                " WHERE op_id=? AND seq=?",
                (state, post_revision, op_id, seq))
        self._touch(op_id, OP_APPLYING)

    def set_step_phase(self, op_id: str, seq: int, phase: str) -> None:
        """Record the EVIDENCED write phase of a failed step.

        ``WRITE_PHASE_PRE`` means the step failed before any write attempt, so
        the store was provably untouched by it; a later revision change is an
        external edit and reconcile must not treat it as an ambiguous write.
        Durable (a plain column) so the classification survives a restart. It
        is internal: never added to the public step projection.
        """
        self._conn.execute(
            "UPDATE steps SET write_phase=? WHERE op_id=? AND seq=?",
            (phase, op_id, seq))
        self._touch(op_id, OP_APPLYING)

    def set_awaiting_host(self, op_id: str, seq: int, token_hash: str) -> None:
        """Hand a host step to the caller: the step and the operation both move
        to ``awaiting_host`` and only the token HASH is persisted.

        The plaintext token exists solely in the engine's return value, so a
        journal read (or a leaked DB) can never be replayed against a route.
        """
        self._conn.execute(
            "UPDATE steps SET state=?, host_token_hash=? WHERE op_id=? AND seq=?",
            (STEP_AWAITING_HOST, token_hash, op_id, seq))
        self._touch(op_id, OP_AWAITING_HOST)

    def clear_host_token(self, op_id: str, seq: int) -> None:
        """Retire a host token. Called the moment a result is accepted or the
        step is decided, so a used token can never be replayed."""
        self._conn.execute(
            "UPDATE steps SET host_token_hash=NULL WHERE op_id=? AND seq=?",
            (op_id, seq))

    def consume_host_token(self, op_id: str, seq: int, token_hash: str) -> bool:
        """Atomically retire a host token for a submission.

        True iff THIS call consumed it: the conditional UPDATE means two
        simultaneous submissions (double-click, retried request) can never both
        be accepted, and the loser sees exactly the replay path (no state
        change) instead of a second evaluation of the same store.
        """
        cur = self._conn.execute(
            "UPDATE steps SET host_token_hash=NULL"
            " WHERE op_id=? AND seq=? AND host_token_hash=?", (op_id, seq, token_hash))
        return cur.rowcount == 1

    def set_host_expectations(self, op_id: str, seq: int,
                              readback_keys: Optional[List[str]] = None,
                              allowed_prefixes: Optional[List[str]] = None) -> None:
        """Persist the semantic readback keys / allowed key prefixes for a host
        step when the planner did not know them. Stored (not just used) so
        reconcile can still verify the write after a restart."""
        if readback_keys is not None:
            self._conn.execute(
                "UPDATE steps SET host_readback_keys=? WHERE op_id=? AND seq=?",
                (json.dumps(list(readback_keys)), op_id, seq))
        if allowed_prefixes is not None:
            self._conn.execute(
                "UPDATE steps SET host_allowed_prefixes=? WHERE op_id=? AND seq=?",
                (json.dumps(list(allowed_prefixes)), op_id, seq))

    def finish(self, op_id: str, state: str) -> None:
        self._touch(op_id, state)

    def set_failure_category(self, op_id: str, category: str) -> None:
        """Record a FIXED, non-sensitive failure category.

        Callers must pass a bounded label (engine._failure_category), never a
        raw exception message — the message may embed config bytes or
        credentials and must not be persisted or exposed.
        """
        self._conn.execute(
            "UPDATE operations SET failure_category=?, updated_at=?"
            " WHERE op_id=?",
            (category, time.time(), op_id))

    def get(self, op_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
        if row is None:
            return None
        steps = [dict(s) for s in self._conn.execute(
            "SELECT * FROM steps WHERE op_id=? ORDER BY seq", (op_id,))]
        op = dict(row)
        op["payload"] = json.loads(op["payload"])
        for s in steps:
            s["identity"] = json.loads(s["identity"]) if s.get("identity") else None
            s["changes"] = json.loads(s["changes"])
            s["snapshot"] = (json.loads(s["snapshot"])
                             if s["snapshot"] is not None else None)
            # Host-executor columns. Internal only: never in the public DTO.
            s["host_call"] = (json.loads(s["host_call"])
                              if s.get("host_call") else None)
            s["host_readback_keys"] = (json.loads(s["host_readback_keys"])
                                       if s.get("host_readback_keys") else None)
            s["host_allowed_prefixes"] = (json.loads(s["host_allowed_prefixes"])
                                          if s.get("host_allowed_prefixes") else None)
        op["steps"] = steps
        return op

    def unfinished_operations(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT op_id FROM operations WHERE state IN (?,?,?,?)",
            (OP_PENDING, OP_APPLYING, OP_PARTIAL, OP_AWAITING_HOST)).fetchall()
        return [op for op in (self.get(r["op_id"]) for r in rows) if op]

    def close(self) -> None:
        self._conn.close()


# Public DTO allowlist: only operation status and slot outcomes. The full
# raw config, store paths, payload, recovery snapshots, changes and any
# exception strings NEVER leave the journal through this projection.
_PUBLIC_OP_FIELDS = ("op_id", "state", "created_at", "updated_at",
                     "failure_category")
_PUBLIC_STEP_FIELDS = ("seq", "profile", "store_kind", "state",
                       "pre_revision", "post_revision")

# Fixed, non-sensitive failure categories the engine may persist and project.
# This is the SINGLE vocabulary: engine._failure_category() may only return a
# member, and journal.to_public_operation() only projects a member. Anything
# else in the column (a malformed or foreign journal written outside the
# engine's contract) projects as null.
FAILURE_CATEGORIES = frozenset({
    "revision_conflict", "store_unreadable", "write_verification_failed",
    "io_error", "step_failed",
    # host-executor seam
    "host_no_write",          # the host performed no write (revision unmoved)
    "host_declined",          # the host refused the call (e.g. 4xx)
    "host_error",             # the host reported an error (write may have landed)
    "host_token_mismatch",    # submission token missing/wrong/replayed
    "external_edit",          # the store moved outside this step's write
})

_PUBLIC_FAILURE_CATEGORIES = FAILURE_CATEGORIES



def to_public_operation(op: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Project an internal journal operation into a safe public DTO.

    Allowlist-only: anything not explicitly listed below (payload, step
    paths, changes, snapshots, error strings) is dropped. ``failure_category``
    is additionally validated against the fixed category allowlist — an
    unknown value (e.g. from a malformed journal) projects as ``None``.
    """
    if op is None:
        return None
    pub: Dict[str, Any] = {k: op[k] for k in _PUBLIC_OP_FIELDS if k in op}
    if "failure_category" in pub:
        cat = pub["failure_category"]
        pub["failure_category"] = (
            cat if isinstance(cat, str) and cat in _PUBLIC_FAILURE_CATEGORIES
            else None)
    pub["steps"] = [
        {k: s[k] for k in _PUBLIC_STEP_FIELDS if k in s}
        for s in op.get("steps", [])
    ]
    return pub


class PublicOperationView:
    """Read-only public accessor over a journal: get/list by allowlisted DTO."""

    def __init__(self, journal: "OperationJournal"):
        self._journal = journal

    def get(self, op_id: str) -> Optional[Dict[str, Any]]:
        return to_public_operation(self._journal.get(op_id))

    def unfinished(self) -> List[Dict[str, Any]]:
        return [pub for pub in (to_public_operation(op)
                                for op in self._journal.unfinished_operations())
                if pub is not None]

    def list_operations(self) -> List[Dict[str, Any]]:
        rows = self._journal._conn.execute(
            "SELECT op_id FROM operations ORDER BY created_at").fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            pub = to_public_operation(self._journal.get(r["op_id"]))
            if pub is not None:
                out.append(pub)
        return out
