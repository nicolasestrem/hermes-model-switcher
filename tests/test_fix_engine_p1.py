"""Regression tests for the three Codex P1 findings against engine.py /
journal.py. One test per finding; written RED first, then made GREEN.

All state lives in throwaway temp dirs; single-step multi-targets and exact privacy layouts are covered by the existing suites — pytest tmp_path here resolves under the system temp root, outside any git worktree.
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from model_switcher import engine
from model_switcher.adapters import (
    JsonConfigStoreAdapter,
    WriteOutcome,
    _set_dotted,
)
from model_switcher.engine import apply_operation, plan_operation
from model_switcher.journal import (
    OP_COMMITTED,
    OP_COMPENSATED,
    OperationJournal,
)


def _lab(tmp_path):
    """One default profile with a small JSON config store + journal."""
    d = tmp_path / "profiles" / "default"
    d.mkdir(parents=True)
    path = d / "config.json"
    path.write_text(json.dumps({
        "main": {"provider": "openrouter", "model": "m/old"},
        "auxiliary": {"goal_judge": {"model": "j/old"}},
    }))
    journal = OperationJournal(str(tmp_path / "journal.db"))
    return str(path), journal


# ------------------------------------------------ Finding 1: readback gap

class SabotagedWriteAdapter(JsonConfigStoreAdapter):
    """Writes *something* to disk but diverges from the intended changes,
    so the post-write readback must fail. With the pre-fix engine the
    unverified write survived (step recorded failed, never compensated)."""

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


def test_readback_failure_is_compensated_from_snapshot(tmp_path, monkeypatch):
    path, journal = _lab(tmp_path)
    original = json.load(open(path))
    monkeypatch.setattr(
        engine, "adapter_for",
        lambda kind, p, profile=None: SabotagedWriteAdapter(p))

    op = plan_operation(journal, [{
        "profile": "default", "store_kind": "config", "path": path,
        "changes": {"main.model": "m/new"},
    }])
    result = apply_operation(journal, op["op_id"])

    # No unverified write survives: the store is exactly the pre-op snapshot.
    assert json.load(open(path)) == original
    assert result["state"] == OP_COMPENSATED
    step = result["steps"][0]
    assert step["state"] == "compensated"
    assert step["post_revision"]  # recorded for the compensation guard


# ------------------------------- Finding 2: same-store multi-slot preset

def test_multi_slot_preset_against_one_store_commits(tmp_path):
    path, journal = _lab(tmp_path)
    targets = [
        {"profile": "default", "store_kind": "config", "path": path,
         "changes": {"main.provider": "nous"}},
        {"profile": "default", "store_kind": "config", "path": path,
         "changes": {"main.model": "m/new"}},
    ]
    op = plan_operation(journal, targets)

    # Coalesced into ONE journalled step for the (profile, store) target.
    assert len(op["steps"]) == 1
    result = apply_operation(journal, op["op_id"])
    assert result["state"] == OP_COMMITTED
    data = json.load(open(path))
    assert data["main"]["provider"] == "nous"
    assert data["main"]["model"] == "m/new"
    assert data["auxiliary"] == {"goal_judge": {"model": "j/old"}}


# ------------------------------------------------ Finding 3: symlink ancestor

def test_journal_refuses_symlinked_ancestor(tmp_path):
    """A symlink deeper than the immediate parent must be refused too:
    outside/symlink/private/j.db previously bypassed both checks because
    `private` did not exist when the symlink checks ran."""
    real = tmp_path / "outside"
    real.mkdir()
    link = tmp_path / "symlink"
    link.symlink_to(real)

    # db_dir does not exist yet (created by makedirs) -> bypassed pre-fix.
    with pytest.raises(ValueError):
        OperationJournal(str(link / "private" / "j.db"))
    # and a mid-path ancestor under the link
    (link / "sub").mkdir()
    with pytest.raises(ValueError):
        OperationJournal(str(link / "sub" / "j.db"))
