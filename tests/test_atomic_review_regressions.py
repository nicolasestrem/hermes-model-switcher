"""RED→GREEN regressions for engine/adapter atomic write + delete/readback
correctness (isolated homes only; no live configs, secrets, or deploys).

Findings under test (all in src/model_switcher/{adapters,engine}.py and
hermes_adapter.py — see the module docstrings for the contract):

  A  readback conflated an explicit delete (None = "remove key") with a
     literal null: a correct deletion read back as FAILED, while a present
     literal null read back as a successful deletion. The delete sentinel and
     a literal null must be distinguished, and readback must match apply.
  B  the canonical save's stale-write guard (`expected_revision`) was dead on
     the adapter path: CanonicalConfigStoreAdapter.write never passed it, so
     the revision check happened BEFORE the adapter's write lock and a
     concurrent writer could slip between the engine's recheck and the save.
     The expected revision must be carried into the adapter's save-lock guard
     (public seams only: no Hermes `_CONFIG_LOCK`, no private config helper).
  C  the profile discovered identity was asserted only before entering the
     lock; it must be re-asserted inside the guard, and the adapter must
     accept an explicit identity.
  D  compensation/undo must carry the recorded post-write revision into the
     same lock guard (never a forced restore past an external edit).
  E  public operation results must expose per-step before/after revisions
     safely (no snapshots/paths/changes) via an existing-shape interface.

Every home/journal is a throwaway /tmp dir (conftest mints them outside
~/.hermes). No real provider credentials are read or written.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from model_switcher import engine  # noqa: E402
from model_switcher.adapters import (  # noqa: E402
    DELETE,
    JsonConfigStoreAdapter,
    StoreConflict,
    _get_dotted,
    _MISSING,
    adapter_for,
)
from model_switcher.engine import (  # noqa: E402
    apply_operation,
    plan_operation,
    revision_summary,
    undo_operation,
)
from model_switcher.journal import (  # noqa: E402
    OP_COMMITTED,
    OP_UNDONE,
    OperationJournal,
)

CFG = """\
model:
  provider: openai
  default: gpt-4o-mini
auxiliary:
  goal_judge:
    model: j/old
unrelated:
  keep: yes
"""


def _journal() -> OperationJournal:
    d = tempfile.mkdtemp(prefix="msw-jr-", dir="/tmp")
    return OperationJournal(os.path.join(d, "ops.db"))


def _lab_json(tmp_path):
    path = tmp_path / "profiles" / "default" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "main": {"provider": "openrouter", "model": "m/old"},
        "auxiliary": {"goal_judge": {"model": "j/old"}},
    }))
    return path


# ---------------------------------------------------------------------------
# A — delete sentinel vs literal null, matching readback
# ---------------------------------------------------------------------------


def test_readback_verifies_explicit_delete_sentinel_as_absent(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"a": {"b": 1}}))
    a = JsonConfigStoreAdapter(str(path))

    assert a.readback({"a.b": 1}) is True

    # Explicit delete sentinel: the key must be ABSENT afterwards...
    a.apply_changes({"a.b": DELETE})
    assert _get_dotted(a.read(), "a.b") is _MISSING
    assert a.readback({"a.b": DELETE}) is True

    # ...and the legacy None alias behaves identically (backwards compatible
    # for the API's `{path: None}` removal contract).
    path.write_text(json.dumps({"a": {"b": 2}}))
    a.apply_changes({"a.b": None})
    assert _get_dotted(a.read(), "a.b") is _MISSING
    assert a.readback({"a.b": None}) is True


def test_readback_does_not_accept_present_literal_null_as_a_delete(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"a": {"b": None}}))
    a = JsonConfigStoreAdapter(str(path))

    # The key is PRESENT (with a literal null). A delete readback must fail:
    # "removed" and "still null" are different states. (Pre-fix this returned
    # True — the exact inversion of the correct-delete case.)
    assert a.readback({"a.b": None}) is False
    assert a.readback({"a.b": DELETE}) is False
    # And a value readback of the literal null still succeeds.
    assert a.readback({"a.b": None, "a.c": None}) is False  # a.c absent -> False


def test_engine_commits_a_removal_change(tmp_path):
    path = _lab_json(tmp_path)
    journal = _journal()
    try:
        op = plan_operation(journal, [{
            "profile": "default", "store_kind": "config", "path": str(path),
            "changes": {"main.model": None},
        }])
        res = apply_operation(journal, op["op_id"])
        assert res["state"] == OP_COMMITTED, res
        data = json.loads(path.read_text())
        assert data["main"] == {"provider": "openrouter"}  # model removed
        assert data["auxiliary"] == {"goal_judge": {"model": "j/old"}}
    finally:
        journal.close()


# ---------------------------------------------------------------------------
# B/C/D — expected revision + discovered identity carried into the lock guard
# ---------------------------------------------------------------------------


def _spy_canonical(monkeypatch):
    """Record every canonical-seam call's (profile, expected_revision)."""
    import hermes_adapter as ha
    calls = []
    orig = ha.HermesAdapter.apply_canonical_save

    def spy(self, profile, updated, identity=None, expected_revision=None):
        # the adapter carries the revision through the revision scope (so the
        # seam signature stays stable); observe it the way the seam does.
        eff = expected_revision if expected_revision is not None \
            else ha._EXPECTED_REVISION.get()
        calls.append({"profile": profile, "expected_revision": eff,
                      "identity": identity})
        return orig(self, profile, updated, identity=identity,
                    expected_revision=eff)

    monkeypatch.setattr(ha.HermesAdapter, "apply_canonical_save", spy)
    return calls


def test_canonical_apply_carries_expected_revision_into_lock(hermes_home, monkeypatch):
    home = hermes_home.path
    (home / "config.yaml").write_text(CFG)
    calls = _spy_canonical(monkeypatch)
    journal = _journal()
    try:
        op = plan_operation(journal, [{
            "profile": "default", "store_kind": "canonical_config",
            "path": str(home / "config.yaml"),
            "changes": {"model.default": "lab-7b-instruct"},
        }])
        pre_revision = op["steps"][0]["pre_revision"]
        assert pre_revision and pre_revision != "absent"

        res = apply_operation(journal, op["op_id"])
        assert res["state"] == OP_COMMITTED, res
        # The engine's planned pre-revision was carried into the save's guard.
        assert calls, "canonical seam never invoked"
        assert calls[-1]["expected_revision"] == pre_revision, calls
        assert yaml.safe_load((home / "config.yaml").read_text())[
            "model"]["default"] == "lab-7b-instruct"
    finally:
        journal.close()


def test_canonical_save_refuses_a_stale_expected_revision(hermes_home):
    home = hermes_home.path
    (home / "config.yaml").write_text(CFG)
    a = adapter_for("canonical_config", str(home / "config.yaml"), "default")
    stale = a.revision()

    # an external writer intervenes AFTER the revision was taken
    (home / "config.yaml").write_text(CFG + "\nexternal:\n  edit: yes\n")

    with pytest.raises(StoreConflict):
        a.write({"model": {"provider": "openai", "default": "m-new"}},
                expected_revision=stale)

    # nothing was clobbered
    assert yaml.safe_load((home / "config.yaml").read_text()).get("external") == {
        "edit": True}

    # with the CURRENT revision the guard admits the save
    a.write({"model": {"provider": "openai", "default": "m-new"}},
            expected_revision=a.revision())
    assert yaml.safe_load((home / "config.yaml").read_text())[
        "model"]["default"] == "m-new"


def test_canonical_revision_domain_matches_the_save_guard(hermes_home):
    """The adapter's revision() must be expressed in the SAME domain the
    Hermes canonical save guard compares under the lock — otherwise the
    carried expected revision could never match (and the guard is dead)."""
    home = hermes_home.path
    (home / "config.yaml").write_text(CFG)
    a = adapter_for("canonical_config", str(home / "config.yaml"), "default")
    import hermes_adapter as ha
    expected = ha._rev(ha.HermesAdapter().load_raw_config("default"))
    assert a.revision() == expected


def test_canonical_accepts_and_rechecks_profile_identity(hermes_home):
    home = hermes_home.path
    (home / "config.yaml").write_text(CFG)
    import hermes_adapter as ha

    # A tampered identity (fingerprint no longer matches the discovered one)
    # must be refused, not silently written.
    bogus = ha.ProfileIdentity(name="default", path=str(home),
                               fingerprint="deadbeefdeadbeef")
    a = adapter_for("canonical_config", str(home / "config.yaml"), "default",
                    identity=bogus)
    with pytest.raises(StoreConflict, match="identity changed"):
        a.write({"model": {"provider": "openai", "default": "m-new"}})

    # A freshly discovered identity is accepted.
    good = ha.discover_profiles()["default"]
    a2 = adapter_for("canonical_config", str(home / "config.yaml"), "default",
                     identity=good)
    a2.write({"model": {"provider": "openai", "default": "m-new"}})
    assert yaml.safe_load((home / "config.yaml").read_text())[
        "model"]["default"] == "m-new"


def test_canonical_undo_carries_recorded_post_revision(hermes_home, monkeypatch):
    home = hermes_home.path
    (home / "config.yaml").write_text(CFG)
    pre_doc = yaml.safe_load((home / "config.yaml").read_text())
    calls = _spy_canonical(monkeypatch)
    journal = _journal()
    try:
        op = plan_operation(journal, [{
            "profile": "default", "store_kind": "canonical_config",
            "path": str(home / "config.yaml"),
            "changes": {"model.default": "lab-7b-instruct"},
        }])
        res = apply_operation(journal, op["op_id"])
        assert res["state"] == OP_COMMITTED, res
        post_revision = res["steps"][0]["post_revision"]

        n = len(calls)
        undone = undo_operation(journal, op["op_id"])
        assert undone["state"] == OP_UNDONE, undone
        # the restore went through the canonical seam carrying the recorded
        # post-write revision (so an intervening external edit is preserved,
        # never clobbered)
        assert calls[n:], "undo did not invoke the canonical seam"
        assert calls[-1]["expected_revision"] == post_revision, calls
        assert yaml.safe_load((home / "config.yaml").read_text()) == pre_doc
    finally:
        journal.close()


def test_canonical_undo_refuses_after_external_edit(hermes_home):
    """A later external edit must survive undo: the revision guard turns the
    step into a conflict instead of a forced restore."""
    home = hermes_home.path
    (home / "config.yaml").write_text(CFG)
    journal = _journal()
    try:
        op = plan_operation(journal, [{
            "profile": "default", "store_kind": "canonical_config",
            "path": str(home / "config.yaml"),
            "changes": {"model.default": "lab-7b-instruct"},
        }])
        assert apply_operation(journal, op["op_id"])["state"] == OP_COMMITTED

        # external writer edits the file after our commit
        (home / "config.yaml").write_text(CFG + "\nexternal:\n  edit: yes\n")

        undone = undo_operation(journal, op["op_id"])
        assert undone["state"] == "recovery_required", undone
        after = yaml.safe_load((home / "config.yaml").read_text())
        assert after.get("external") == {"edit": True}  # external edit preserved
    finally:
        journal.close()


# ---------------------------------------------------------------------------
# E — safe public before/after revision interface
# ---------------------------------------------------------------------------


def test_revision_summary_exposes_before_after_without_private_fields(tmp_path):
    path = _lab_json(tmp_path)
    journal = _journal()
    try:
        op = plan_operation(journal, [{
            "profile": "default", "store_kind": "config", "path": str(path),
            "changes": {"main.model": "m/new"},
        }])
        res = apply_operation(journal, op["op_id"])
        assert res["state"] == OP_COMMITTED, res

        summary = revision_summary(res)
        assert summary == [{
            "profile": "default", "store": "config",
            "revision_before": res["steps"][0]["pre_revision"],
            "revision_after": res["steps"][0]["post_revision"],
        }], summary
        blob = json.dumps(summary)
        for forbidden in ("snapshot", "path", "changes", "provider", "openrouter"):
            assert forbidden not in blob, forbidden
    finally:
        journal.close()


# ---------------------------------------------------------------------------
# P0 fault injection — a concurrent write must never be adopted as ours
# ---------------------------------------------------------------------------


class _RacingWriteAdapter(JsonConfigStoreAdapter):
    """Deterministic race: a concurrent writer replaces the file immediately
    AFTER our save but BEFORE any revision capture."""

    def write(self, data, expected_revision=None):
        super().write(data)
        disk = self.read() or {}          # read() returns the parsed mapping
        disk["external"] = {"edit": True}
        super().write(disk)  # interloper lands between save and capture
        return None


def test_racing_write_is_never_adopted_and_fails_closed(tmp_path, monkeypatch):
    from model_switcher.engine import OP_RECOVERY_REQUIRED, reconcile

    path = _lab_json(tmp_path)
    journal = _journal()
    try:
        monkeypatch.setattr(
            engine, "adapter_for",
            lambda kind, p, profile=None, identity=None: _RacingWriteAdapter(p))
        op = plan_operation(journal, [{
            "profile": "default", "store_kind": "config", "path": str(path),
            "changes": {"main.model": "m/new"},
        }])
        res = apply_operation(journal, op["op_id"])
        # NOT a false success: the interloper's revision is never adopted
        assert res["state"] != OP_COMMITTED, res
        assert not res["steps"][0].get("post_revision")
        assert json.loads(path.read_text())["external"] == {"edit": True}

        # reconcile must flag the ambiguous (changed-but-unowned) write
        rec = reconcile(journal, op["op_id"])
        assert rec["state"] == OP_RECOVERY_REQUIRED, rec
        assert json.loads(path.read_text())["external"] == {"edit": True}
    finally:
        journal.close()


def test_canonical_lock_guard_rejects_an_intervening_edit(hermes_home, monkeypatch):
    """The guard must run INSIDE the canonical lock: an edit that lands after
    the engine's recheck but before the save is refused under the lock."""
    import hermes_adapter as ha

    home = hermes_home.path
    (home / "config.yaml").write_text(CFG)
    orig = ha.HermesAdapter.apply_canonical_save
    fired = {"done": False}

    def spy(self, profile, updated, identity=None, expected_revision=None):
        if not fired["done"]:
            fired["done"] = True
            # concurrent writer lands AFTER the engine pre-check, BEFORE the
            # lock guard reads its freshness, i.e. inside the guarded window
            (home / "config.yaml").write_text(CFG + "\nexternal:\n  edit: yes\n")
        return orig(self, profile, updated, identity=identity,
                    expected_revision=expected_revision)

    monkeypatch.setattr(ha.HermesAdapter, "apply_canonical_save", spy)
    journal = _journal()
    try:
        op = plan_operation(journal, [{
            "profile": "default", "store_kind": "canonical_config",
            "path": str(home / "config.yaml"),
            "changes": {"model.default": "lab-7b-instruct"},
        }])
        res = apply_operation(journal, op["op_id"])
        assert res["state"] != OP_COMMITTED, res
        after = yaml.safe_load((home / "config.yaml").read_text())
        assert after.get("external") == {"edit": True}   # preserved, not clobbered
        assert after["model"]["default"] == "gpt-4o-mini"  # our save refused
    finally:
        journal.close()
