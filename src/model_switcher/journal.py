"""Durable SQLite operation journal: intent, steps, revisions, state.

The journal is the recovery source of truth. Operations are recorded
BEFORE the first write and updated after each step, so a crash or
timeout leaves enough persisted intent to reconcile actual files.
"""
from __future__ import annotations

import json
import os
import sqlite3
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


class OperationJournal:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
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
                 pre_revision: str) -> None:
        self._conn.execute(
            "INSERT INTO steps (op_id, seq, profile, store_kind, path, state,"
            " changes, snapshot, pre_revision) VALUES (?,?,?,?,?,?,?,?,?)",
            (op_id, seq, profile, store_kind, path, STEP_PENDING,
             json.dumps(changes, sort_keys=True),
             json.dumps(snapshot, sort_keys=True) if snapshot is not None else None,
             pre_revision),
        )
        self._touch(op_id, OP_APPLYING)

    def _touch(self, op_id: str, state: str) -> None:
        self._conn.execute(
            "UPDATE operations SET state=?, updated_at=? WHERE op_id=?",
            (state, time.time(), op_id))

    def set_step_state(self, op_id: str, seq: int, state: str,
                       post_revision: Optional[str] = None) -> None:
        self._conn.execute(
            "UPDATE steps SET state=?, post_revision=? WHERE op_id=? AND seq=?",
            (state, post_revision, op_id, seq))
        self._touch(op_id, OP_APPLYING)

    def finish(self, op_id: str, state: str) -> None:
        self._touch(op_id, state)

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
