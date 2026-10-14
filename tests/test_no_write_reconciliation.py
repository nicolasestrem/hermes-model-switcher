"""Round-1 F1 regression: a PRE-WRITE step failure is a known no-write state.

An apply that fails BEFORE any write -- a revision recheck conflict, or an
unreadable fresh read -- provably never touched the store. That fact must be
recorded durably, so a later ``reconcile()`` (triggered by ``GET
/operations/{id}``, the recover endpoint, or the first journal use after a
restart) must NOT escalate it to ``recovery_required`` just because the store
later changed for an unrelated reason. Before the fix the failed step carried
no post-revision, so reconcile could not tell "never wrote" from "wrote but
lost ownership" and falsely claimed writes may need repair.

The control matters: a genuinely AMBIGUOUS write (a write that may have
landed, e.g. a concurrent writer detected after our save) must still fail
closed to ``recovery_required`` -- the fix must not declare every failed step
safe.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from model_switcher.adapters import JsonConfigStoreAdapter  # noqa: E402
from model_switcher.engine import (  # noqa: E402
    apply_operation,
    plan_operation,
    reconcile,
)
from model_switcher.journal import (  # noqa: E402
    OP_PENDING,
    OP_RECOVERY_REQUIRED,
    WRITE_PHASE_AMBIGUOUS,
    WRITE_PHASE_PRE,
    OperationJournal,
    to_public_operation,
)


@pytest.fixture()
def lab():
    """Throwaway lab OUTSIDE any git worktree (journal refuses worktrees)."""
    root = Path(tempfile.mkdtemp(prefix="nowrite-lab-", dir="/tmp"))
    yield root
    import shutil
    shutil.rmtree(root, ignore_errors=True)


def _targets(root, names):
    targets = []
    for name in names:
        d = root / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        p = d / "config.json"
        p.write_text(json.dumps(
            {"main": {"provider": "openrouter", "model": "m/old"}}))
        targets.append({"profile": name, "store_kind": "config",
                        "path": str(p), "changes": {"main.model": "m/new"}})
    return targets


def _cfg(root, name):
    return root / "profiles" / name / "config.json"


def _model(root, name):
    return json.loads(_cfg(root, name).read_text())["main"]["model"]


def _edit(root, name, model):
    """Unrelated external edit: rewrites the file so its revision changes."""
    data = json.loads(_cfg(root, name).read_text())
    data["main"]["model"] = model
    _cfg(root, name).write_text(json.dumps(data))


def _plan_failed_prewrite(root, journal):
    """Plan two profiles, land an external edit, then apply -> step 0 fails
    at the pre-write revision recheck. Returns (op, op_id)."""
    targets = _targets(root, ["default", "alpha"])
    op = plan_operation(journal, targets)
    _edit(root, "default", "m/interloper")
    res = apply_operation(journal, op["op_id"])
    return op, res


# --------------------------------------------------------------------------- #
# F1 -- a pre-write failure is durably known-no-write
# --------------------------------------------------------------------------- #

def test_prewrite_failure_is_marked_known_no_write(lab):
    journal = OperationJournal(str(lab / "journal.db"))
    try:
        _op, res = _plan_failed_prewrite(lab, journal)
        assert res["state"] == OP_PENDING
        assert [s["state"] for s in res["steps"]] == ["failed", "pending"]
        step = res["steps"][0]
        # nothing was written, so no post-revision, and the durable phase
        # marker says so
        assert step["post_revision"] is None
        assert step["write_phase"] == WRITE_PHASE_PRE
    finally:
        journal.close()


def test_reconcile_after_later_edit_does_not_false_alarm(lab):
    """The round-1 F1 repro: apply fails pre-write, then an unrelated later
    edit lands, then reconcile() must NOT claim recovery is required."""
    journal = OperationJournal(str(lab / "journal.db"))
    try:
        op, _res = _plan_failed_prewrite(lab, journal)
        # a SECOND, unrelated external edit -- nothing of ours is on disk
        _edit(lab, "default", "m/later")
        rec = reconcile(journal, op["op_id"])
        assert rec["state"] == OP_PENDING, rec
        assert [s["state"] for s in rec["steps"]] == ["failed", "pending"]
        # the external edit is preserved untouched
        assert _model(lab, "default") == "m/later"
    finally:
        journal.close()


def test_reconcile_without_later_edit_stays_no_write(lab):
    """Control: even with no second edit, reconcile of a pre-write failure
    stays known-no-write (not a false recovery alarm)."""
    journal = OperationJournal(str(lab / "journal.db"))
    try:
        op, _res = _plan_failed_prewrite(lab, journal)
        rec = reconcile(journal, op["op_id"])
        assert rec["state"] == OP_PENDING, rec
        assert [s["state"] for s in rec["steps"]] == ["failed", "pending"]
    finally:
        journal.close()


def test_known_no_write_is_durable_across_fresh_journal_read(lab):
    """A fresh process reopening the journal must still classify the step as
    known-no-write (the marker lives on disk, not in memory)."""
    db = str(lab / "journal.db")
    journal = OperationJournal(db)
    op, _res = _plan_failed_prewrite(lab, journal)
    op_id = op["op_id"]
    journal.close()

    # fresh instance: reads the committed row back from disk
    reopened = OperationJournal(db)
    try:
        stored = reopened.get(op_id)
        assert stored["steps"][0]["write_phase"] == WRITE_PHASE_PRE
        assert stored["steps"][0]["post_revision"] is None
        _edit(lab, "default", "m/later-after-restart")
        rec = reconcile(reopened, op_id)
        assert rec["state"] == OP_PENDING, rec
        assert [s["state"] for s in rec["steps"]] == ["failed", "pending"]
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# Control -- an AMBIGUOUS write must still fail closed
# --------------------------------------------------------------------------- #

class _RacingWriteAdapter(JsonConfigStoreAdapter):
    """A concurrent writer replaces the file immediately AFTER our save but
    BEFORE any revision capture -> the write may have landed, ownership is
    undecidable."""

    def write(self, data, expected_revision=None):
        super().write(data)
        disk = self.read() or {}
        disk["external"] = {"edit": True}
        super().write(disk)  # interloper lands between save and capture
        return None


def test_ambiguous_write_still_requires_recovery(lab, monkeypatch):
    import model_switcher.engine as engine

    journal = OperationJournal(str(lab / "journal.db"))
    try:
        monkeypatch.setattr(
            engine, "adapter_for",
            lambda kind, p, profile=None, identity=None: _RacingWriteAdapter(p))
        targets = _targets(lab, ["default"])
        op = plan_operation(journal, targets)
        res = apply_operation(journal, op["op_id"])
        assert res["state"] != "committed"
        step = res["steps"][0]
        assert step["state"] == "failed"
        assert step["post_revision"] is None
        # genuinely ambiguous -- NOT classified as known-no-write
        assert step["write_phase"] == WRITE_PHASE_AMBIGUOUS
        rec = reconcile(journal, op["op_id"])
        assert rec["state"] == OP_RECOVERY_REQUIRED, rec
        assert rec["steps"][0]["state"] == "conflict"
    finally:
        journal.close()


# --------------------------------------------------------------------------- #
# Retry stays gated on a fresh, valid revision (never a forced apply)
# --------------------------------------------------------------------------- #

def test_retry_does_not_force_write_and_fresh_plan_succeeds(lab):
    journal = OperationJournal(str(lab / "journal.db"))
    try:
        op, _res = _plan_failed_prewrite(lab, journal)
        # retrying the SAME operation must not force the stale write
        retry = apply_operation(journal, op["op_id"])
        assert retry["state"] == OP_PENDING, retry
        assert _model(lab, "default") == "m/interloper"  # untouched
        # a FRESH preview (new plan) re-reads current revisions and succeeds
        op2 = plan_operation(journal, _targets(lab, ["default", "alpha"]))
        res2 = apply_operation(journal, op2["op_id"])
        assert res2["state"] == "committed", res2
        assert _model(lab, "default") == "m/new"
        assert _model(lab, "alpha") == "m/new"
    finally:
        journal.close()


# --------------------------------------------------------------------------- #
# The marker is internal -- it must not leak through the public projection
# --------------------------------------------------------------------------- #

def test_public_projection_does_not_leak_the_phase_marker(lab):
    journal = OperationJournal(str(lab / "journal.db"))
    try:
        op, _res = _plan_failed_prewrite(lab, journal)
        pub = to_public_operation(journal.get(op["op_id"]))
        allowed = {"seq", "profile", "store_kind", "state",
                   "pre_revision", "post_revision"}
        assert set(pub["steps"][0]) <= allowed, pub["steps"][0]
        assert "write_phase" not in pub["steps"][0]
        blob = json.dumps(pub)
        for forbidden in ("snapshot", "changes", "interloper", "path",
                          "write_phase"):
            assert forbidden not in blob, forbidden
    finally:
        journal.close()
