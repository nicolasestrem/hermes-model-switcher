"""Reviewed identity/revision must survive the plan/apply/undo boundary."""
import json
import pytest

from hermes_adapter import discover_profiles
from model_switcher.journal import OperationJournal
from model_switcher import engine


def test_recreated_profile_same_path_is_a_different_identity(hermes_home):
    folder = hermes_home.make_profile("alpha")
    original = discover_profiles()["alpha"]
    folder.rename(folder.with_name("alpha-backup"))
    hermes_home.make_profile("alpha")
    assert discover_profiles()["alpha"].fingerprint != original.fingerprint


def test_plan_records_identity_privately(hermes_home):
    folder = hermes_home.make_profile("alpha")
    journal = OperationJournal(str(hermes_home.path / "private" / "operations.db"))
    try:
        op = engine.plan_operation(journal, [{"profile": "alpha", "store_kind": "canonical_config",
            "path": str(folder / "config.yaml"), "changes": {"delegation.model": "model-new"}}])
        assert op["steps"][0]["identity"]["fingerprint"] == discover_profiles()["alpha"].fingerprint
        from model_switcher.journal import to_public_operation
        assert "identity" not in json.dumps(to_public_operation(op))
    finally:
        journal.close()


def test_profile_replacement_between_plan_and_apply_refuses_write(hermes_home):
    folder = hermes_home.make_profile("alpha")
    journal = OperationJournal(str(hermes_home.path / "private" / "operations.db"))
    try:
        op = engine.plan_operation(journal, [{"profile": "alpha", "store_kind": "canonical_config",
            "path": str(folder / "config.yaml"), "changes": {"delegation.model": "model-new"}}])
        folder.rename(folder.with_name("alpha-backup"))
        replacement = hermes_home.make_profile("alpha")
        before = (replacement / "config.yaml").read_bytes()
        try:
            result = engine.apply_operation(journal, op["op_id"])
            assert result["state"] != "committed"
        except (engine.ApplyBlocked, RuntimeError):
            pass
        assert (replacement / "config.yaml").read_bytes() == before
    finally:
        journal.close()


def test_expected_review_revision_cannot_be_rebased_at_plan(hermes_home):
    folder = hermes_home.make_profile("alpha")
    journal = OperationJournal(str(hermes_home.path / "private" / "operations.db"))
    try:
        with pytest.raises(engine.ApplyBlocked, match="revision"):
            engine.plan_operation(journal, [{"profile": "alpha", "store_kind": "canonical_config",
                "path": str(folder / "config.yaml"), "expected_revision": "reviewed-old",
                "changes": {"delegation.model": "model-new"}}])
        assert journal.unfinished_operations() == []
    finally:
        journal.close()


def test_snapshot_and_revision_cannot_describe_different_documents(hermes_home, monkeypatch):
    folder = hermes_home.make_profile("alpha")
    journal = OperationJournal(str(hermes_home.path / "private" / "operations.db"))
    original = engine.snapshot_adapter
    def race(adapter):
        snapshot = original(adapter)
        with open(adapter.path, "a") as stream:
            stream.write("external_setting: changed\n")
        return snapshot
    monkeypatch.setattr(engine, "snapshot_adapter", race)
    try:
        with pytest.raises(engine.ApplyBlocked, match="revision"):
            engine.plan_operation(journal, [{"profile": "alpha", "store_kind": "canonical_config",
                "path": str(folder / "config.yaml"), "changes": {"delegation.model": "model-new"}}])
        assert journal.unfinished_operations() == []
    finally:
        journal.close()
