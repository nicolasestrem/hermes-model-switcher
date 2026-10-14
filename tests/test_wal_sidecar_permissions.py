"""Journal WAL sidecar permissions.

The SQLite ``-wal``/``-shm`` sidecars the journal's WAL connection creates
must stay 0600, inside the journal's 0700 private parent — the same
invariant the database file itself is held to. This uses a real
``OperationJournal`` (real SQLite WAL) under pytest's ``tmp_path``, which
lives outside any git worktree (the journal refuses to live inside one).
No inference, no lab home, no live Hermes state.
"""
from __future__ import annotations

import stat

from model_switcher.journal import OperationJournal


def test_wal_sidecar_permissions(tmp_path):
    db = tmp_path / "private" / "journal.db"
    journal = OperationJournal(str(db))
    try:
        # A real journal write opens the WAL and materialises -wal/-shm.
        journal.begin({"op": "permission-probe"})

        parent = db.parent
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700, \
            "journal parent dir must be 0700"
        for suffix in ("", "-wal", "-shm"):
            sidecar = db.with_name(db.name + suffix)
            assert sidecar.exists(), f"missing {sidecar.name}"
            mode = stat.S_IMODE(sidecar.stat().st_mode)
            assert mode == 0o600, f"{sidecar.name} mode {oct(mode)} != 0o600"
    finally:
        journal.close()
