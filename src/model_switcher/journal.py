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

STEP_PENDING = "pending"
STEP_APPLIED = "applied"
STEP_FAILED = "failed"
STEP_COMPENSATED = "compensated"
STEP_CONFLICT = "conflict"

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
            conn.execute("PRAGMA synchronous=NORMAL")
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
            # intact.
            step_columns = {row[1] for row in conn.execute("PRAGMA table_info(steps)")}
            if "identity" not in step_columns:
                conn.execute("ALTER TABLE steps ADD COLUMN identity TEXT")
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
                 pre_revision: str, identity: Optional[Dict[str, str]] = None) -> None:
        self._conn.execute(
            "INSERT INTO steps (op_id, seq, profile, store_kind, path, state,"
            " changes, snapshot, pre_revision, identity) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (op_id, seq, profile, store_kind, path, STEP_PENDING,
             json.dumps(changes, sort_keys=True),
             json.dumps(snapshot, sort_keys=True) if snapshot is not None else None,
             pre_revision, json.dumps(identity) if identity is not None else None),
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
        op["steps"] = steps
        return op

    def unfinished_operations(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT op_id FROM operations WHERE state IN (?,?,?)",
            (OP_PENDING, OP_APPLYING, OP_PARTIAL)).fetchall()
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


def to_public_operation(op: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Project an internal journal operation into a safe public DTO.

    Allowlist-only: anything not explicitly listed below (payload, step
    paths, changes, snapshots, error strings) is dropped.
    """
    if op is None:
        return None
    pub: Dict[str, Any] = {k: op[k] for k in _PUBLIC_OP_FIELDS if k in op}
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
