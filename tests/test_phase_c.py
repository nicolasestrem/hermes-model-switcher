"""Phase C (T4+T5) tests. All state lives in throwaway temp dirs."""
from __future__ import annotations

import json
import os

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from model_switcher.adapters import adapter_for, JsonConfigStoreAdapter
from model_switcher.engine import (
    ApplyBlocked,
    apply_operation,
    plan_operation,
    reconcile,
    undo_operation,
)
from model_switcher.journal import (
    OperationJournal,
    OP_COMMITTED,
    OP_COMPENSATED,
    OP_PENDING,
    OP_RECOVERY_REQUIRED,
    OP_UNDONE,
)
from model_switcher.presets import (
    BillingPolicy,
    COST_METERED,
    COST_SUBSCRIPTION,
    COST_UNKNOWN,
    PresetBlocked,
    ReviewedPreset,
    RouteTarget,
)


@pytest.fixture()
def lab(tmp_path):
    """Throwaway Hermes-lab home with two profiles."""
    home = tmp_path / "hermes-home"
    for p in ("default", "alpha"):
        d = home / "profiles" / p
        d.mkdir(parents=True)
        (d / "config.json").write_text(json.dumps({
            "main": {"provider": "openrouter", "model": "m/old"},
            "auxiliary": {"goal_judge": {"model": "j/old", "threshold": 0.8}},
        }))
        (d / "cron.json").write_text(json.dumps({
            "jobs": [{"id": "nightly", "model": "m/old"}],
            "model": "m/old", "model_provider": "openrouter",
        }))
    journal = OperationJournal(str(tmp_path / "journal.db"))
    return home, journal


def cfg(home, profile):
    return str(home / "profiles" / profile / "config.json")


def cron(home, profile):
    return str(home / "profiles" / profile / "cron.json")


def read(path):
    with open(path) as fh:
        return json.load(fh)


def targets_main(home, profiles):
    return [
        {"profile": p, "store_kind": "config", "path": cfg(home, p),
         "changes": {"main.provider": "nous", "main.model": "m/new"}}
        for p in profiles
    ]


# ---------------------------------------------------------------- T4

def test_same_process_autosave_race(lab):
    """A same-process autosave between journaling and the write changes the
    revision; the recheck must refuse and preserve the autosaved content."""
    home, journal = plan_fixture(lab)
    path = cfg(home, "default")
    op = plan_operation(journal, targets_main(home, ["default"]))
    # simulate an autosave racing us
    data = read(path)
    data["autosave"] = {"stamp": "mid-flight"}
    with open(path, "w") as fh:
        json.dump(data, fh)
    result = apply_operation(journal, op["op_id"])
    # refused before write: main.model untouched, autosave intact
    assert "main" in read(path) and read(path)["main"]["model"] == "m/old"
    assert read(path)["autosave"] == {"stamp": "mid-flight"}
    assert result["steps"][0]["state"] == "failed"  # recheck refused


def plan_fixture(lab):
    return lab


def test_stale_revision_refused(lab):
    home, journal = lab
    path = cfg(home, "default")
    op = plan_operation(journal, targets_main(home, ["default"]))
    # external writer modifies the store after preview
    data = read(path)
    data["external_edit"] = True
    with open(path, "w") as fh:
        json.dump(data, fh)
    result = apply_operation(journal, op["op_id"])
    assert read(path)["external_edit"] is True
    assert read(path)["main"]["model"] == "m/old"
    # nothing wrote; op remains retryable with a fresh plan
    assert result["state"] == OP_PENDING


def test_partial_failure_compensates(lab):
    home, journal = lab
    op = plan_operation(journal, targets_main(home, ["default", "alpha"]))
    result = apply_operation(journal, op["op_id"], fail_after=1)
    assert result["state"] == OP_COMPENSATED
    # both profiles restored to snapshots
    assert read(cfg(home, "default"))["main"]["model"] == "m/old"
    assert read(cfg(home, "alpha"))["main"]["model"] == "m/old"
    states = {s["seq"]: s["state"] for s in result["steps"]}
    assert states[0] == "compensated" and states[1] == "failed"


def test_undo_preserves_later_edit(lab):
    home, journal = lab
    path = cfg(home, "default")
    op = plan_operation(journal, targets_main(home, ["default"]))
    apply_operation(journal, op["op_id"])
    assert read(path)["main"]["model"] == "m/new"
    # a later, unrelated edit lands after the commit
    data = read(path)
    data["user_note"] = "keep me"
    with open(path, "w") as fh:
        json.dump(data, fh)
    undone = undo_operation(journal, op["op_id"])
    # revision no longer matches post-write: conflict, no forced restore
    assert undone["state"] == OP_RECOVERY_REQUIRED
    assert undone["failure_category"] == "external_edit"
    body = read(path)
    assert body["user_note"] == "keep me"
    assert body["main"]["model"] == "m/new"  # later edit preserved


def test_undo_clean(lab):
    home, journal = lab
    op = plan_operation(journal, targets_main(home, ["default"]))
    apply_operation(journal, op["op_id"])
    undone = undo_operation(journal, op["op_id"])
    assert undone["state"] == OP_UNDONE
    assert read(cfg(home, "default")) == read(cfg(home, "alpha"))


def test_timeout_idempotency(lab):
    """Retry with the same operation ID never duplicates writes."""
    home, journal = lab
    path = cfg(home, "default")
    op = plan_operation(journal, targets_main(home, ["default"]),
                        op_id="op-timeout-1")
    # apply, then a "timeout" retry with the same op_id arrives
    result = apply_operation(journal, op["op_id"])
    assert result["state"] == OP_COMMITTED
    rev_after_apply = adapter_for("config", path).revision()
    retry = apply_operation(journal, "op-timeout-1")
    assert retry["op_id"] == "op-timeout-1"
    assert retry["state"] == OP_COMMITTED
    assert adapter_for("config", path).revision() == rev_after_apply
    body = read(path)
    assert body["main"]["model"] == "m/new"  # written exactly once


def test_crash_reconciliation(lab):
    home, journal = lab
    path = cfg(home, "default")
    op = plan_operation(journal, targets_main(home, ["default"]))
    # crash right after the write, before the journal recorded commit
    adapter = adapter_for("config", path)
    adapter.apply_changes({"main.provider": "nous", "main.model": "m/new"})
    # pretend the journal missed the commit
    result = reconcile(journal, op["op_id"])
    assert result["state"] == OP_COMMITTED
    assert read(path)["main"]["model"] == "m/new"

    # ambiguous case: file changed again after the (uncommitted) write
    op2 = plan_operation(journal, targets_main(home, ["alpha"]),
                         op_id="op-crash-2")
    adapter2 = adapter_for("config", cfg(home, "alpha"))
    adapter2.apply_changes({"main.provider": "nous", "main.model": "m/new"})
    data = read(cfg(home, "alpha"))
    data["post_crash_edit"] = True
    with open(cfg(home, "alpha"), "w") as fh:
        json.dump(data, fh)
    result2 = reconcile(journal, op2["op_id"])
    assert result2["state"] == OP_RECOVERY_REQUIRED
    assert read(cfg(home, "alpha"))["post_crash_edit"] is True


def test_cross_store_partial_failure(lab):
    """Failure on a later store compensates earlier stores (reverse order),
    across config and cron stores of the same profile."""
    home, journal = lab
    targets = [
        {"profile": "default", "store_kind": "config",
         "path": cfg(home, "default"),
         "changes": {"main.model": "m/new"}},
        {"profile": "default", "store_kind": "cron",
         "path": cron(home, "default"),
         "changes": {"model": "m/new", "model_provider": "nous"}},
        {"profile": "alpha", "store_kind": "config",
         "path": cfg(home, "alpha"),
         "changes": {"main.model": "m/new"}},
    ]
    op = plan_operation(journal, targets)
    result = apply_operation(journal, op["op_id"], fail_after=2)
    assert result["state"] == OP_COMPENSATED
    assert read(cfg(home, "default"))["main"]["model"] == "m/old"
    assert read(cron(home, "default"))["model"] == "m/old"
    assert read(cron(home, "default"))["model_provider"] == "openrouter"
    assert read(cfg(home, "alpha"))["main"]["model"] == "m/old"
    states = {s["seq"]: s["state"] for s in result["steps"]}
    assert states == {0: "compensated", 1: "compensated", 2: "failed"}


def test_no_adapter_blocks_before_writes(lab, tmp_path):
    home, journal = lab
    unknown = [{"profile": "default", "store_kind": "mystery",
                "path": str(tmp_path / "x"), "changes": {"a.b": 1}}]
    with pytest.raises(ApplyBlocked):
        plan_operation(journal, unknown)
    assert not os.path.exists(tmp_path / "x")
    assert journal.unfinished_operations() == []


def test_malformed_store_blocks(lab):
    home, journal = lab
    path = cfg(home, "default")
    with open(path, "w") as fh:
        fh.write("{not json")
    with pytest.raises(ApplyBlocked):
        plan_operation(journal, targets_main(home, ["default"]))
    assert "{not json" in open(path).read()


# ---------------------------------------------------------------- T5

def make_preset(home):
    return ReviewedPreset(
        preset_id="p-fast",
        name="Fast fleet switch",
        billing_policy=BillingPolicy(approved_classes=[COST_METERED]),
        targets=[
            RouteTarget("default", "config", cfg(home, "default"),
                        {"main.model": "m/new"}, COST_METERED),
            RouteTarget("alpha", "config", cfg(home, "alpha"),
                        {"main.model": "m/new"}, COST_METERED),
        ],
        reviewed_profile_ids=["default", "alpha"],
    )


def test_one_click_saved_preset(lab):
    home, journal = lab
    store = PresetStoreHolder()
    store.save(make_preset(home))
    # one click: no confirmation modal — preflight then apply
    preset = store.get("p-fast")
    assert preset.preflight(
        credentials={"default": True, "alpha": True},
        live_cost_classes={}) == []
    targets = store.apply("p-fast")
    op = plan_operation(journal, targets, op_id="preset-apply-1")
    result = apply_operation(journal, op["op_id"])
    assert result["state"] == OP_COMMITTED
    assert read(cfg(home, "default"))["main"]["model"] == "m/new"
    assert read(cfg(home, "alpha"))["main"]["model"] == "m/new"


class PresetStoreHolder:
    from model_switcher.presets import PresetStore
    _store = PresetStore()

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_new_profiles_do_not_join(lab):
    home, _ = lab
    store = PresetStoreHolder()
    store.save(make_preset(home))
    reasons = store.get("p-fast").preflight(
        credentials={"default": True, "alpha": True},
        live_cost_classes={},
        discovered_profiles=["default", "alpha", "beta"],
    )
    assert any("beta" in r for r in reasons)
    # beta is not written to: preset targets stay frozen
    assert all(t["profile"] != "beta" for t in store.apply("p-fast"))


def test_missing_credentials_block_before_writes(lab):
    home, journal = lab
    store = PresetStoreHolder()
    store.save(make_preset(home))
    before = {p: open(cfg(home, p)).read()
              for p in ("default", "alpha")}
    reasons = store.get("p-fast").preflight(
        credentials={"default": True, "alpha": False},
        live_cost_classes={},
    )
    assert reasons and "alpha" in reasons[0]
    # preflight refusal performs no writes
    assert {p: open(cfg(home, p)).read() for p in ("default", "alpha")} == before
    assert journal.unfinished_operations() == []


def test_unknown_cost_not_free(lab):
    home, _ = lab
    store = PresetStoreHolder()
    preset = make_preset(home)
    # live catalog now reports unknown cost for the route
    store.save(preset)
    key = preset.targets[0].changes
    reasons = store.get("p-fast").preflight(
        credentials={"default": True, "alpha": True},
        live_cost_classes={
            "default:config:main.model=m/new": COST_UNKNOWN,
        },
    )
    assert any("unknown cost" in r for r in reasons)
    # even explicit unknown approval requires the allow_unknown flag; absent
    # here, the block stands regardless of approved_classes
    assert not any(r == "" for r in reasons)


def test_unapproved_cost_change_blocks(lab):
    home, _ = lab
    store = PresetStoreHolder()
    store.save(make_preset(home))  # reviewed as metered, policy metered only
    reasons = store.get("p-fast").preflight(
        credentials={"default": True, "alpha": True},
        live_cost_classes={
            "default:config:main.model=m/new": COST_SUBSCRIPTION,
        },
    )
    assert any("cost changed after review" in r for r in reasons)
    # and a cost class outside the policy even without change
    preset = make_preset(home)
    from dataclasses import replace
    p2 = replace(preset, targets=[replace(
        preset.targets[0], cost_class=COST_SUBSCRIPTION)])
    store.save(p2)
    reasons2 = store.get("p-fast").preflight(
        credentials={"default": True, "alpha": True},
        live_cost_classes={},
    )
    assert any("not in the preset's approved billing policy" in r
               for r in reasons2)


def test_warning_lookup_failure_preserves_cost_gate(lab):
    """Catalog lookup failure (missing live class) must not mark a
    metered route as approved-free; reviewed class is used and stays
    subject to the billing policy — an unknown/absent lookup never
    yields 'free'."""
    home, _ = lab
    store = PresetStoreHolder()
    preset = make_preset(home)
    from dataclasses import replace
    p2 = replace(preset, targets=[replace(
        preset.targets[0], cost_class=COST_UNKNOWN)])
    store.save(p2)
    # live lookup fails entirely (empty live map) -> reviewed unknown stands
    reasons = store.get("p-fast").preflight(
        credentials={"default": True, "alpha": True},
        live_cost_classes={},
    )
    assert any("unknown cost" in r for r in reasons)
