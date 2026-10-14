"""Regression: the base guarded recheck is a PRE-WRITE refusal.

The engine's ``_apply_step`` rechecks the store revision before writing, then
calls ``adapter.apply_changes_guarded``. For a store WITHOUT its own write lock
the base implementation performs a SECOND revision recheck (adapters.py) before
delegating to ``apply_changes``. A concurrent writer can land in the window
BETWEEN those two checks -- the classic check-then-save race at the adapter
seam.

That second refusal is still provably PRE-WRITE: the base guard raises before
``apply_changes`` (and thus ``write``) is ever reached, so nothing of ours
touched the store. Before the fix the base guard raised a plain ``StoreConflict``
with no ``write_phase``, so the engine's ``getattr(exc, 'write_phase', None)``
defaulted to ``WRITE_PHASE_AMBIGUOUS`` and ``reconcile`` wrongly escalated a
known-no-write step to ``recovery_required``.

This drives the race with a REAL adapter subclass that lands an ACTUAL external
edit (not a faked result) after the engine's first check and before the base
guard's check, then calls the genuine ``super().apply_changes_guarded``. The
monkeypatched ``adapter_for`` only injects the timing; the real base guard still
runs and still decides.
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

from model_switcher.adapters import (  # noqa: E402
    JsonConfigStoreAdapter,
    StoreConflict,
    StorePreWriteConflict,
    adapter_for,
)
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
)


@pytest.fixture()
def lab():
    """Throwaway lab OUTSIDE any git worktree (journal refuses worktrees).

    ``tempfile.mkdtemp`` respects ``TMPDIR`` (Brian's scratch), which lives
    outside the repo, so nothing is written under version control.
    """
    root = Path(tempfile.mkdtemp(prefix="prewrite-lab-"))
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


class _TwoStageRaceAdapter(JsonConfigStoreAdapter):
    """Land an external edit in the window between the engine's pre-write
    recheck and the base guard's recheck, then run the REAL base guard.

    The base guard's refusal is therefore a genuine pre-write decision; the
    external edit is an actual on-disk write, never a faked exception.
    """

    def apply_changes_guarded(self, changes, expected_revision=None):
        # the interloper lands AFTER the engine rechecked, BEFORE our guard
        disk = self.read() or {}
        disk.setdefault("main", {})["model"] = "m/external"
        super().write(disk)  # real external edit on disk
        # now the genuine base guard rechecks the revision and must refuse
        return super().apply_changes_guarded(
            changes, expected_revision=expected_revision)


def test_guarded_prewrite_conflict_is_known_no_write(lab, monkeypatch):
    import model_switcher.engine as engine

    journal = OperationJournal(str(lab / "journal.db"))
    try:
        monkeypatch.setattr(
            engine, "adapter_for",
            lambda kind, p, profile=None, identity=None: _TwoStageRaceAdapter(p))
        targets = _targets(lab, ["default"])
        op = plan_operation(journal, targets)
        res = apply_operation(journal, op["op_id"])

        # the apply failed at the guarded recheck -- nothing committed
        assert res["state"] == OP_PENDING, res
        step = res["steps"][0]
        assert step["state"] == "failed"
        assert step["post_revision"] is None
        # the fix: the base guard's pre-write refusal is classified pre-write,
        # NOT ambiguous
        assert step["write_phase"] == WRITE_PHASE_PRE
        assert step["write_phase"] != WRITE_PHASE_AMBIGUOUS
        # our requested model was never written; the external edit is intact
        assert _model(lab, "default") == "m/external"
        assert _model(lab, "default") != "m/new"

        # reconcile must NOT false-alarm to recovery_required
        rec = reconcile(journal, op["op_id"])
        assert rec["state"] == OP_PENDING, rec
        assert [s["state"] for s in rec["steps"]] == ["failed"]
        assert rec["state"] != OP_RECOVERY_REQUIRED
        # and the external edit survives reconcile untouched
        assert _model(lab, "default") == "m/external"
    finally:
        journal.close()


def test_canonical_locked_prewrite_guard_is_known_no_write(hermes_home):
    """The CANONICAL save's locked pre-save guard is also a known no-write.

    ``HermesAdapter.apply_canonical_save`` raises its ``StoreConflict`` ONLY at
    the fresh-revision guard, BEFORE ``config_mod.save_config`` — i.e. inside
    the lock, with nothing written. ``CanonicalConfigStoreAdapter.write`` must
    translate that into the pre-write-tagged ``StorePreWriteConflict`` (so the
    engine records a known no-write step), NOT the untagged ``StoreConflict``
    the engine would classify as ambiguous and escalate to recovery_required.

    The refusal is decided by the REAL guard: an actual on-disk external edit
    lands after the revision is taken, and the real ``apply_canonical_save``
    re-reads under its lock and refuses.
    """
    import yaml

    home = hermes_home.path
    a = adapter_for("canonical_config", str(home / "config.yaml"), "default")
    stale = a.revision()

    # a real external writer lands AFTER the revision was taken, so the locked
    # pre-save guard (the genuine guard, not a fake) must refuse.
    (home / "config.yaml").write_text(
        "model:\n  provider: openai\n  default: gpt-4o-mini\n"
        "external:\n  edit: yes\n")

    with pytest.raises(StorePreWriteConflict) as exc:
        a.write({"model": {"provider": "openai", "default": "m-new"}},
                expected_revision=stale)

    # tagged pre-write (known no-write), never the post-write ambiguity that
    # must keep failing closed to recovery_required
    assert exc.value.write_phase == WRITE_PHASE_PRE
    assert exc.value.write_phase != WRITE_PHASE_AMBIGUOUS
    # still an ordinary StoreConflict subclass so the engine's existing catch
    # and the conservative contract are preserved
    assert isinstance(exc.value, StoreConflict)

    # nothing of ours landed; the external edit is intact
    after = yaml.safe_load((home / "config.yaml").read_text())
    assert after.get("external") == {"edit": True}
    assert after["model"]["default"] == "gpt-4o-mini"
