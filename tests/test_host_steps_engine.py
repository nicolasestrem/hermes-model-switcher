"""Host-executor step protocol (engine + journal seam).

Every test drives REAL store files in a throwaway lab directory and simulates
the browser's public host call by editing the store directly between
``apply_operation`` (which hands out the one-time ``host_call``) and
``submit_host_result`` (which reports what the browser observed). The engine
must decide the verdict from the STORE, never from the report:

  host ok                      -> applied, op commits
  host no-write / declined     -> decided failure, known no-write
  host error AFTER a write     -> write recorded, then compensated back
  foreign diff                 -> recovery_required, NO blind compensation
  write that cannot verify     -> recorded, then compensated back
  token mismatch / replay      -> fixed ApplyBlocked, NO state change
  awaiting_host after restart  -> reconcile decides exactly like a host error
  undo of a host step          -> snapshot restored under the revision guard

No inference, no live Hermes home: the journal lives outside any git worktree
(pytest's tmp_path) and every credential is fake.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from model_switcher import engine  # noqa: E402
from model_switcher.adapters import (  # noqa: E402
    JsonConfigStoreAdapter,
    StoreWriteAmbiguous,
    WriteOutcome,
    _set_dotted,
)
from model_switcher.engine import (  # noqa: E402
    ApplyHostResultRejected,
    ApplyHostTokenMismatch,
    ApplyUndoAmbiguous,
    apply_operation,
    plan_operation,
    reconcile,
    submit_host_result,
    undo_operation,
)
from model_switcher.journal import (  # noqa: E402
    OP_AWAITING_HOST,
    OP_COMMITTED,
    OP_COMPENSATED,
    OP_PENDING,
    OP_RECOVERY_REQUIRED,
    OP_UNDONE,
    STEP_APPLIED,
    STEP_AWAITING_HOST,
    STEP_COMPENSATED,
    STEP_CONFLICT,
    STEP_FAILED,
    WRITE_PHASE_AMBIGUOUS,
    WRITE_PHASE_POST,
    WRITE_PHASE_PRE,
    OperationJournal,
    to_public_operation,
)

LAB_DOC = {
    "main": {"provider": "openrouter", "model": "m/old"},
    "auxiliary": {"goal_judge": {"model": "j/old", "threshold": 0.8}},
}
HOST_CHANGES = {"main.provider": "nous", "main.model": "m/new"}


# --------------------------------------------------------------- lab helpers

def _lab(tmp_path, profiles=("default",)):
    """A real lab home: one ``profiles/<name>/config.json`` per profile."""
    home = tmp_path / "lab"
    paths = {}
    for name in profiles:
        d = home / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        path = d / "config.json"
        path.write_text(json.dumps(LAB_DOC, indent=2, sort_keys=True))
        paths[name] = str(path)
    journal = OperationJournal(str(tmp_path / "private" / "journal.db"))
    return paths, journal


def _host_target(path, profile="default", changes=None,
                 readback_keys=("main.provider", "main.model"),
                 allowed=("main",)):
    return {
        "profile": profile, "store_kind": "config", "path": path,
        "changes": dict(changes or HOST_CHANGES),
        "executor": "host",
        "host_call": {
            "method": "POST", "path": "/api/model/set", "query": {},
            "body": {"scope": "main", "provider": "nous", "model": "m/new"},
        },
        "readback_keys": list(readback_keys),
        "allowed_prefixes": list(allowed),
    }


def _backend_target(path, profile="default", changes=None):
    return {"profile": profile, "store_kind": "config", "path": path,
            "changes": dict(changes or HOST_CHANGES)}


def _read(path):
    return json.loads(Path(path).read_text())


def _host_write(path, changes, extra=None):
    """Stand-in for the browser's route: write the store like save_config.

    Serialized exactly as the JSON lab adapter does (indent=2, sorted keys) so
    the on-disk bytes are what the route itself would have produced.
    """
    data = _read(path)
    for dotted, value in changes.items():
        _set_dotted(data, dotted, value)
    for dotted, value in (extra or {}).items():
        _set_dotted(data, dotted, value)
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True))


def _steps(op):
    return [s["state"] for s in op["steps"]]


def _journal_bytes(journal):
    """Every byte the journal can hold (db + WAL + shm), for secret canaries."""
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        p = Path(journal.db_path + suffix)
        if p.exists():
            blob += p.read_bytes()
    return blob


# ------------------------------------------------------- 1. the happy path

def test_host_step_ok_commits_and_never_persists_the_token(tmp_path):
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        # Plan-time persistence: the public route and the verification hints.
        planned = journal.get(op["op_id"])["steps"][0]
        assert planned["executor"] == "host"
        assert planned["host_call"]["path"] == "/api/model/set"
        assert planned["host_readback_keys"] == ["main.provider", "main.model"]
        assert planned["host_allowed_prefixes"] == ["main"]

        awaiting = apply_operation(journal, op["op_id"])
        assert awaiting["state"] == OP_AWAITING_HOST
        assert _steps(awaiting) == [STEP_AWAITING_HOST]
        call = awaiting["host_call"]
        assert call["opId"] == op["op_id"]
        assert call["seq"] == 0
        assert call["method"] == "POST"
        assert call["path"] == "/api/model/set"
        assert call["body"] == {"scope": "main", "provider": "nous",
                                "model": "m/new"}
        token = call["token"]
        assert isinstance(token, str) and len(token) >= 32

        # The engine performed NO write while awaiting the host.
        assert _read(path) == LAB_DOC

        # Only the digest is journalled: the plaintext token exists solely in
        # the returned host_call.
        stored = journal.get(op["op_id"])["steps"][0]
        assert stored["host_token_hash"] == hashlib.sha256(
            token.encode("utf-8")).hexdigest()
        assert stored["host_token_hash"] != token
        blob = _journal_bytes(journal)
        assert token.encode("utf-8") not in blob, \
            "plaintext host token must never reach the journal"

        # The public projection never carries the call or the token.
        pub = json.dumps(to_public_operation(journal.get(op["op_id"])))
        assert "host_call" not in pub and token not in pub

        # The browser's route lands the write; the report says ok.
        _host_write(path, HOST_CHANGES)
        result = submit_host_result(journal, op["op_id"], 0, token, "ok")

        assert result["state"] == OP_COMMITTED
        assert _steps(result) == [STEP_APPLIED]
        assert result["steps"][0]["post_revision"]
        assert _read(path)["main"] == {"provider": "nous", "model": "m/new"}
        # The spent token is retired from the journal.
        assert journal.get(op["op_id"])["steps"][0]["host_token_hash"] is None
    finally:
        journal.close()


# --------------------------------------------- 2. the host wrote nothing

@pytest.mark.parametrize("outcome,category", [
    ("no_write", "host_no_write"),
    ("declined", "host_declined"),
    ("error", "host_error"),
])
def test_host_reporting_no_write_is_a_known_no_write(tmp_path, outcome,
                                                    category):
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        awaiting = apply_operation(journal, op["op_id"])
        token = awaiting["host_call"]["token"]

        result = submit_host_result(journal, op["op_id"], 0, token, outcome)

        # Nothing landed: decided failure, and provably pre-write.
        assert result["state"] == OP_PENDING
        assert result["failure_category"] == category
        assert result["final_state"] == OP_PENDING
        step = result["steps"][0]
        assert step["state"] == STEP_FAILED
        assert step["post_revision"] is None
        assert step["write_phase"] == WRITE_PHASE_PRE
        assert _read(path) == LAB_DOC
    finally:
        journal.close()


def test_host_success_report_without_a_write_is_not_believed(tmp_path):
    """'ok' plus an untouched store is a no-write, not a success."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]

        result = submit_host_result(journal, op["op_id"], 0, token, "ok")

        assert result["state"] == OP_PENDING
        assert result["failure_category"] == "host_no_write"
        assert result["steps"][0]["write_phase"] == WRITE_PHASE_PRE
        assert _read(path) == LAB_DOC
    finally:
        journal.close()


# ----------------------------------- 3. the host errored AFTER writing

def test_host_error_after_a_write_is_compensated_back(tmp_path):
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]

        # The route DID write, then reported an error (e.g. a 500 after save).
        _host_write(path, HOST_CHANGES)
        written_revision = engine._adapter_for_step(
            journal.get(op["op_id"])["steps"][0]).revision()

        result = submit_host_result(journal, op["op_id"], 0, token, "error")

        assert result["state"] == OP_COMPENSATED
        assert result["failure_category"] == "host_error"
        assert result["final_state"] == OP_COMPENSATED
        assert _steps(result) == [STEP_COMPENSATED]
        # The write was OURS: recorded with its post-revision, then reverted.
        assert result["steps"][0]["post_revision"] == written_revision
        assert _read(path) == LAB_DOC, "no unverified write may survive"
    finally:
        journal.close()


# ------------------------------------------- 4. a foreign diff appeared

def test_foreign_diff_needs_recovery_and_compensates_nothing(tmp_path):
    """A host step whose store moved outside its allowed prefixes, while an
    earlier backend step is already applied: recovery_required, and NEITHER
    store is touched (no blind compensation over someone else's bytes)."""
    paths, journal = _lab(tmp_path, profiles=("alpha", "default"))
    try:
        op = plan_operation(journal, [
            _backend_target(paths["alpha"]),
            _host_target(paths["default"]),
        ])
        awaiting = apply_operation(journal, op["op_id"])
        assert awaiting["state"] == OP_AWAITING_HOST
        assert _steps(awaiting) == [STEP_APPLIED, STEP_AWAITING_HOST]
        assert _read(paths["alpha"])["main"]["provider"] == "nous"

        # The route wrote our change AND an unrelated edit landed (another
        # writer / a wider route write): the diff is not ours.
        _host_write(paths["default"], HOST_CHANGES,
                    extra={"auxiliary.threshold_override": 0.5})

        result = submit_host_result(journal, op["op_id"], 1,
                                    awaiting["host_call"]["token"], "ok")

        assert result["state"] == OP_RECOVERY_REQUIRED
        assert result["failure_category"] == "external_edit"
        assert result["final_state"] == OP_RECOVERY_REQUIRED
        assert _steps(result) == [STEP_APPLIED, STEP_CONFLICT]
        # Nothing was compensated: the applied step keeps its write and the
        # foreign edit is preserved for a human to resolve.
        assert _read(paths["alpha"])["main"]["provider"] == "nous"
        assert _read(paths["default"])["auxiliary"]["threshold_override"] == 0.5
    finally:
        journal.close()


def test_diff_inside_allowed_prefixes_but_none_of_our_values_is_foreign(tmp_path):
    """A change under an allowed prefix that is not our intended write is still
    a foreign diff (the guard is semantic, not just structural)."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]

        # Something wrote a DIFFERENT provider/model: not what we planned.
        _host_write(path, {"main.provider": "openai", "main.model": "gpt-x"})

        result = submit_host_result(journal, op["op_id"], 0, token, "ok")

        assert result["state"] == OP_RECOVERY_REQUIRED
        assert result["failure_category"] == "external_edit"
        assert _read(path)["main"]["provider"] == "openai"
    finally:
        journal.close()


# --------------------------------- 5. a write that cannot be verified

def test_host_write_that_cannot_be_verified_is_rolled_back(tmp_path):
    """The bytes are exactly our intended write, but a semantic key the route
    contract requires is unverifiable: it is recorded and taken back, never
    left as an unverified write."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [
            _host_target(path, readback_keys=("main.provider", "main.model",
                                              "main.reasoning_effort")),
        ])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]

        _host_write(path, HOST_CHANGES)  # canonical bytes -> intended document

        result = submit_host_result(journal, op["op_id"], 0, token, "ok")

        assert result["state"] == OP_COMPENSATED
        assert result["failure_category"] == "write_verification_failed"
        step = result["steps"][0]
        assert step["state"] == STEP_COMPENSATED
        assert step["post_revision"]
        assert _read(path) == LAB_DOC
    finally:
        journal.close()


# ------------------------------------------------- 6. token discipline

def test_token_mismatch_makes_no_state_change(tmp_path):
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        before = journal.get(op["op_id"])

        with pytest.raises(ApplyHostTokenMismatch) as caught:
            submit_host_result(journal, op["op_id"], 0, "not-the-token", "ok")
        assert type(caught.value).category == "host_token_mismatch"
        assert caught.value.public_reason

        after = journal.get(op["op_id"])
        assert after["state"] == OP_AWAITING_HOST
        assert after["steps"][0]["state"] == STEP_AWAITING_HOST
        assert (after["steps"][0]["host_token_hash"]
                == before["steps"][0]["host_token_hash"]), \
            "a rejected submission must not consume the live token"
        assert _read(path) == LAB_DOC

        # ... and the real token still works afterwards.
        _host_write(path, HOST_CHANGES)
        result = submit_host_result(journal, op["op_id"], 0, token, "ok")
        assert result["state"] == OP_COMMITTED
    finally:
        journal.close()


def test_status_without_an_outcome_is_derived(tmp_path):
    """The route reports an HTTP status; the engine derives the outcome
    vocabulary from it (2xx ok, 4xx declined, 5xx error)."""
    paths, journal = _lab(tmp_path, profiles=("ok", "declined", "error"))
    try:
        op_ok = plan_operation(journal, [_host_target(paths["ok"], profile="ok")])
        token_ok = apply_operation(journal, op_ok["op_id"])["host_call"]["token"]
        _host_write(paths["ok"], HOST_CHANGES)
        assert submit_host_result(journal, op_ok["op_id"], 0, token_ok,
                                  status=200)["state"] == OP_COMMITTED

        op_declined = plan_operation(
            journal, [_host_target(paths["declined"], profile="declined")])
        token_declined = apply_operation(
            journal, op_declined["op_id"])["host_call"]["token"]
        out = submit_host_result(journal, op_declined["op_id"], 0,
                                 token_declined, status=409)
        assert out["failure_category"] == "host_declined"
        assert _read(paths["declined"]) == LAB_DOC

        op_error = plan_operation(
            journal, [_host_target(paths["error"], profile="error")])
        token_error = apply_operation(
            journal, op_error["op_id"])["host_call"]["token"]
        out = submit_host_result(journal, op_error["op_id"], 0, token_error,
                                 status=500)
        assert out["failure_category"] == "host_error"
    finally:
        journal.close()


def test_rejected_outcome_does_not_burn_the_token(tmp_path):
    """A correct token with an unsupported outcome is refused with NO state
    change -- including the token itself. A rejection must never strand the
    step: the browser corrects the report and resubmits the SAME token."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]

        with pytest.raises(ApplyHostResultRejected):
            submit_host_result(journal, op["op_id"], 0, token, "bogus")

        after = journal.get(op["op_id"])
        assert after["state"] == OP_AWAITING_HOST
        assert after["steps"][0]["state"] == STEP_AWAITING_HOST
        assert after["steps"][0]["host_token_hash"], \
            "a rejected observation must not consume the live token"

        _host_write(path, HOST_CHANGES)
        result = submit_host_result(journal, op["op_id"], 0, token, "ok")
        assert result["state"] == OP_COMMITTED
        assert _read(path)["main"]["provider"] == "nous"
    finally:
        journal.close()


def test_rejected_status_does_not_burn_the_token(tmp_path):
    """Same guarantee for a status the engine cannot parse: the refusal is
    side-effect-free, so a corrected retry still applies."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]

        with pytest.raises(ApplyHostResultRejected):
            submit_host_result(journal, op["op_id"], 0, token,
                               status="not-a-status")

        after = journal.get(op["op_id"])
        assert after["state"] == OP_AWAITING_HOST
        assert after["steps"][0]["host_token_hash"]

        _host_write(path, HOST_CHANGES)
        result = submit_host_result(journal, op["op_id"], 0, token, "ok")
        assert result["state"] == OP_COMMITTED
    finally:
        journal.close()


def test_route_expectations_are_journalled_at_plan_time(tmp_path):
    """Expectations are SERVER-derived: the planner persists them with the step
    (so a later reconcile verifies the same write), and the submission API has
    no way to supply or replace them."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path, allowed=("main.",))])
        planned = journal.get(op["op_id"])["steps"][0]
        assert planned["host_readback_keys"] == ["main.provider", "main.model"]
        # A trailing-dot route prefix is journalled as given and cleaned only
        # when matching, so the stored plan mirrors what the planner declared.
        assert planned["host_allowed_prefixes"] == ["main."]

        params = inspect.signature(submit_host_result).parameters
        assert "readback_keys" not in params
        assert "allowed_prefixes" not in params

        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        _host_write(path, HOST_CHANGES)
        result = submit_host_result(journal, op["op_id"], 0, token, "ok")

        assert result["state"] == OP_COMMITTED
        stored = journal.get(op["op_id"])["steps"][0]
        assert stored["host_readback_keys"] == ["main.provider", "main.model"]
        assert stored["host_allowed_prefixes"] == ["main."]
    finally:
        journal.close()


def test_no_journalled_prefixes_allow_only_the_steps_own_changes(tmp_path):
    """Fail-narrow default: with no plan-time allowed_prefixes, the allowlist is
    the step's own changed keys. A host write that also moves a SIBLING key in
    the same top-level section is a foreign diff -> recovery_required."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        target = _host_target(path, readback_keys=())
        target.pop("allowed_prefixes")
        op = plan_operation(journal, [target])
        planned = journal.get(op["op_id"])["steps"][0]
        assert planned["host_allowed_prefixes"] is None
        assert engine._host_expectations(planned) == ([], sorted(HOST_CHANGES))

        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        # Our intended write landed AND a sibling key we never planned moved.
        _host_write(path, HOST_CHANGES,
                    extra={"main.reasoning_effort": "high"})

        result = submit_host_result(journal, op["op_id"], 0, token, "ok")

        assert result["state"] == OP_RECOVERY_REQUIRED
        assert result["failure_category"] == "external_edit"
        assert _steps(result) == [STEP_CONFLICT]
        # Nothing is reverted over an edit that is not ours.
        assert _read(path)["main"]["reasoning_effort"] == "high"
    finally:
        journal.close()


def test_used_token_cannot_be_replayed(tmp_path):
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        _host_write(path, HOST_CHANGES)
        assert submit_host_result(journal, op["op_id"], 0, token,
                                  "ok")["state"] == OP_COMMITTED
        committed = journal.get(op["op_id"])

        with pytest.raises(ApplyHostTokenMismatch):
            submit_host_result(journal, op["op_id"], 0, token, "no_write")

        after = journal.get(op["op_id"])
        assert after["state"] == OP_COMMITTED
        assert after["steps"][0]["state"] == STEP_APPLIED
        assert (after["steps"][0]["post_revision"]
                == committed["steps"][0]["post_revision"])
        assert _read(path)["main"]["provider"] == "nous"
    finally:
        journal.close()


def test_submission_to_a_non_host_step_is_refused(tmp_path):
    """A completed backend step has no host token, so any submission is a fixed
    refusal and the committed record is untouched."""
    paths, journal = _lab(tmp_path)
    try:
        op = plan_operation(journal, [_backend_target(paths["default"])])
        apply_operation(journal, op["op_id"])
        with pytest.raises(ApplyHostTokenMismatch):
            submit_host_result(journal, op["op_id"], 0, "", "ok")
        assert journal.get(op["op_id"])["state"] == OP_COMMITTED
    finally:
        journal.close()


def test_unknown_outcome_is_rejected_without_a_state_change(tmp_path):
    paths, journal = _lab(tmp_path)
    try:
        op = plan_operation(journal, [_host_target(paths["default"])])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        with pytest.raises(ApplyHostResultRejected):
            submit_host_result(journal, op["op_id"], 0, token, "maybe")
        assert journal.get(op["op_id"])["state"] == OP_AWAITING_HOST
        # An unknown step number is refused the same way.
        with pytest.raises(ApplyHostResultRejected):
            submit_host_result(journal, op["op_id"], 7, token, "ok")
        assert journal.get(op["op_id"])["state"] == OP_AWAITING_HOST
    finally:
        journal.close()


def test_unreadable_store_after_the_call_needs_recovery(tmp_path):
    """A store that moved and cannot be parsed is undecidable: recovery, never
    a guess (and never a blind compensation)."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        Path(path).write_text("{not json")

        result = submit_host_result(journal, op["op_id"], 0, token, "ok")

        assert result["state"] == OP_RECOVERY_REQUIRED
        assert result["failure_category"] == "store_unreadable"
        assert _steps(result) == [STEP_CONFLICT]
        assert Path(path).read_text() == "{not json"
    finally:
        journal.close()


def test_a_second_concurrent_submission_finds_the_token_gone(tmp_path):
    """The token is consumed atomically, so a racing second submission can
    never evaluate the same store twice."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
        # Another request got there first.
        assert journal.consume_host_token(op["op_id"], 0, presented) is True
        assert journal.consume_host_token(op["op_id"], 0, presented) is False

        _host_write(path, HOST_CHANGES)
        with pytest.raises(ApplyHostTokenMismatch):
            submit_host_result(journal, op["op_id"], 0, token, "ok")
        state = journal.get(op["op_id"])
        assert state["state"] == OP_AWAITING_HOST
        assert _read(path)["main"]["provider"] == "nous"  # untouched by us
    finally:
        journal.close()


def test_prewrite_revision_mismatch_blocks_before_the_call(tmp_path):
    """The revision recheck before handing the call out takes the ordinary
    PRE-WRITE conflict path: known no-write, external edit preserved."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        # An autosave lands between planning and apply.
        bumped = _read(path)
        bumped["autosave"] = {"stamp": "mid-flight"}
        Path(path).write_text(json.dumps(bumped, indent=2, sort_keys=True))

        result = apply_operation(journal, op["op_id"])

        assert "host_call" not in result
        assert result["state"] == OP_PENDING
        assert result["failure_category"] == "revision_conflict"
        step = result["steps"][0]
        assert step["state"] == STEP_FAILED
        assert step["write_phase"] == WRITE_PHASE_PRE
        assert _read(path)["autosave"] == {"stamp": "mid-flight"}
    finally:
        journal.close()


# ------------------------------- 7. mixed backend + host + backend op

class SabotagedWriteAdapter(JsonConfigStoreAdapter):
    """Writes something to disk that diverges from the intended changes, so its
    post-write readback must fail (the write DID land)."""

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


def test_mixed_op_compensates_backend_and_host_steps_on_mid_failure(
        tmp_path, monkeypatch):
    """backend p1 -> host p2 -> backend p3, where p3 fails AFTER writing.

    The host step's write and p1's write must both be taken back in reverse
    order, together with p3's own landed-but-unverified write.
    """
    paths, journal = _lab(tmp_path, profiles=("p1", "p2", "p3"))
    sabotaged = str(paths["p3"])
    # Capture the real factory BEFORE patching the module global the engine uses.
    real_adapter_for = engine.adapter_for

    def _adapter_for(kind, path, profile=None):
        if path == sabotaged:
            return SabotagedWriteAdapter(path)
        return real_adapter_for(kind, path, profile)

    monkeypatch.setattr(engine, "adapter_for", _adapter_for)
    try:
        op = plan_operation(journal, [
            _backend_target(paths["p1"]),
            _host_target(paths["p2"], profile="p2"),
            _backend_target(paths["p3"], profile="p3"),
        ])
        awaiting = apply_operation(journal, op["op_id"])
        assert awaiting["state"] == OP_AWAITING_HOST
        assert _steps(awaiting) == [STEP_APPLIED, STEP_AWAITING_HOST,
                                    "pending"]
        assert _read(paths["p1"])["main"]["provider"] == "nous"

        # The host step lands; then the trailing step writes and fails its
        # readback.
        _host_write(paths["p2"], HOST_CHANGES)

        result = submit_host_result(journal, op["op_id"], 1,
                                    awaiting["host_call"]["token"], "ok")

        assert result["state"] == OP_COMPENSATED
        assert result["failure_category"] == "write_verification_failed"
        assert _steps(result) == [STEP_COMPENSATED, STEP_COMPENSATED,
                                  STEP_COMPENSATED]
        # Reverse-order compensation restored ALL THREE stores, including the
        # one the browser's route wrote and the sabotaged one.
        assert _read(paths["p1"]) == LAB_DOC
        assert _read(paths["p2"]) == LAB_DOC
        assert _read(paths["p3"]) == LAB_DOC
    finally:
        journal.close()


def test_mixed_op_stops_at_the_second_host_step(tmp_path):
    """A host step followed by another host step: the second call is handed
    out only after the first result is accepted."""
    paths, journal = _lab(tmp_path, profiles=("p1", "p2"))
    try:
        op = plan_operation(journal, [
            _host_target(paths["p1"], profile="p1"),
            _host_target(paths["p2"], profile="p2"),
        ])
        first = apply_operation(journal, op["op_id"])
        assert first["host_call"]["seq"] == 0

        _host_write(paths["p1"], HOST_CHANGES)
        second = submit_host_result(journal, op["op_id"], 0,
                                    first["host_call"]["token"], "ok")
        assert second["state"] == OP_AWAITING_HOST
        assert second["host_call"]["seq"] == 1
        assert second["host_call"]["token"] != first["host_call"]["token"]
        assert _steps(second) == [STEP_APPLIED, STEP_AWAITING_HOST]

        _host_write(paths["p2"], HOST_CHANGES)
        done = submit_host_result(journal, op["op_id"], 1,
                                  second["host_call"]["token"], "ok")
        assert done["state"] == OP_COMMITTED
        assert _steps(done) == [STEP_APPLIED, STEP_APPLIED]
        assert _read(paths["p1"])["main"]["provider"] == "nous"
        assert _read(paths["p2"])["main"]["provider"] == "nous"
    finally:
        journal.close()


# ------------------------------- 8. awaiting_host after a restart

def test_reconcile_handles_awaiting_host_like_a_host_error(tmp_path):
    """Two operations are left awaiting a host result, then the process
    "restarts". reconcile() decides each from the store: the one whose write
    landed is compensated back; the untouched one is a known no-write."""
    paths, journal = _lab(tmp_path, profiles=("written", "untouched"))
    written_path = paths["written"]
    untouched_path = paths["untouched"]
    try:
        op_written = plan_operation(
            journal, [_host_target(written_path, profile="written")])
        op_untouched = plan_operation(
            journal, [_host_target(untouched_path, profile="untouched")])
        apply_operation(journal, op_written["op_id"])
        apply_operation(journal, op_untouched["op_id"])

        # The browser's call landed for one of them, and its response was lost.
        _host_write(written_path, HOST_CHANGES)

        # Restart: a fresh journal handle on the same private DB.
        journal.close()
        journal = OperationJournal(journal.db_path)

        assert {op["op_id"] for op in journal.unfinished_operations()} == {
            op_written["op_id"], op_untouched["op_id"]}

        replayed = reconcile(journal, op_written["op_id"])
        assert replayed["state"] == OP_COMPENSATED
        assert replayed["failure_category"] == "host_error"
        assert replayed["steps"][0]["state"] == STEP_COMPENSATED
        assert replayed["steps"][0]["post_revision"]
        assert replayed["steps"][0]["write_phase"] == WRITE_PHASE_POST
        assert _read(written_path) == LAB_DOC, \
            "an unconfirmed host write is compensated back"
        assert journal.get(op_written["op_id"])["steps"][0][
            "host_token_hash"] is None

        untouched = reconcile(journal, op_untouched["op_id"])
        assert untouched["state"] == OP_PENDING
        assert untouched["failure_category"] == "host_error"
        assert untouched["steps"][0]["state"] == STEP_FAILED
        assert untouched["steps"][0]["post_revision"] is None
        assert untouched["steps"][0]["write_phase"] == WRITE_PHASE_PRE
        assert _read(untouched_path) == LAB_DOC

        # Nothing is left awaiting a host result: the written op is closed,
        # and the untouched one is a known no-write record (pre-existing
        # "pending", never re-drivable under the same op_id).
        still_open = {op["op_id"] for op in journal.unfinished_operations()}
        assert still_open == {op_untouched["op_id"]}
        assert all(step["state"] != STEP_AWAITING_HOST
                   for op in journal.unfinished_operations()
                   for step in op["steps"])
    finally:
        journal.close()


def test_reconcile_awaiting_host_with_a_foreign_edit_needs_recovery(tmp_path):
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        apply_operation(journal, op["op_id"])
        _host_write(path, HOST_CHANGES, extra={"plugins": {"x": 1}})

        result = reconcile(journal, op["op_id"])

        assert result["state"] == OP_RECOVERY_REQUIRED
        assert result["failure_category"] == "external_edit"
        assert _steps(result) == [STEP_CONFLICT]
        assert _read(path)["plugins"] == {"x": 1}, "never a blind restore"
    finally:
        journal.close()


def test_reapply_while_awaiting_reissues_a_fresh_token(tmp_path):
    """Retrying apply while the store is untouched re-issues the call (the old
    token dies with it); it never re-runs or double-writes."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        first = apply_operation(journal, op["op_id"])
        second = apply_operation(journal, op["op_id"])
        assert second["state"] == OP_AWAITING_HOST
        assert second["host_call"]["token"] != first["host_call"]["token"]
        assert _read(path) == LAB_DOC

        with pytest.raises(ApplyHostTokenMismatch):
            submit_host_result(journal, op["op_id"], 0,
                               first["host_call"]["token"], "ok")

        _host_write(path, HOST_CHANGES)
        result = submit_host_result(journal, op["op_id"], 0,
                                    second["host_call"]["token"], "ok")
        assert result["state"] == OP_COMMITTED
    finally:
        journal.close()


# ------------------------------------------------- 9. undo of a host step

def test_undo_of_a_host_step_restores_the_snapshot(tmp_path):
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        _host_write(path, HOST_CHANGES)
        assert submit_host_result(journal, op["op_id"], 0, token,
                                  "ok")["state"] == OP_COMMITTED

        undone = undo_operation(journal, op["op_id"])

        assert undone["state"] == OP_UNDONE
        assert _steps(undone) == [STEP_COMPENSATED]
        assert _read(path) == LAB_DOC
    finally:
        journal.close()


def test_undo_of_a_host_step_never_clobbers_a_later_edit(tmp_path):
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        _host_write(path, HOST_CHANGES)
        submit_host_result(journal, op["op_id"], 0, token, "ok")

        # A later external edit lands after the host write.
        _host_write(path, {"main.provider": "later-writer"})
        undone = undo_operation(journal, op["op_id"])

        assert undone["state"] == OP_RECOVERY_REQUIRED
        assert _read(path)["main"]["provider"] == "later-writer"
    finally:
        journal.close()


# ------------------- 9b. an ambiguous save (StoreWriteAmbiguous)

class AmbiguousSaveAdapter(JsonConfigStoreAdapter):
    """A save lands, then the strict readback shows a document that is not ours
    (a concurrent writer replaced the file). Carries NO post_revision."""

    def apply_changes_guarded(self, changes, expected_revision=None):
        disk = self.read() or {}
        disk["main"] = {"interloper": True}
        self.write(disk)  # the save really happened
        raise StoreWriteAmbiguous(
            "readback does not match the intended document")


def test_ambiguous_backend_write_requires_recovery_and_refuses_undo(
        tmp_path, monkeypatch):
    """A StoreWriteAmbiguous step is durably ambiguous: recovery_required with
    the fixed external_edit category, no post_revision, no compensation write
    anywhere (not even for the clean step before it), no retry, refused undo."""
    paths, journal = _lab(tmp_path, profiles=("clean", "ambiguous"))
    ambiguous_path = str(paths["ambiguous"])
    real_adapter_for = engine.adapter_for

    def _adapter_for(kind, path, profile=None):
        if path == ambiguous_path:
            return AmbiguousSaveAdapter(path)
        return real_adapter_for(kind, path, profile)

    monkeypatch.setattr(engine, "adapter_for", _adapter_for)
    try:
        op = plan_operation(journal, [
            _backend_target(paths["clean"], profile="clean"),
            _backend_target(paths["ambiguous"], profile="ambiguous"),
        ])
        result = apply_operation(journal, op["op_id"])

        assert result["state"] == OP_RECOVERY_REQUIRED
        assert result["failure_category"] == "external_edit"
        assert result["final_state"] == OP_RECOVERY_REQUIRED
        assert _steps(result) == [STEP_APPLIED, STEP_CONFLICT]
        step = result["steps"][1]
        assert step["post_revision"] is None, "never adopt a post revision"
        assert step["write_phase"] == WRITE_PHASE_AMBIGUOUS

        # NO compensation write: the interloper's document stands, and the
        # clean step before it is not rolled back either.
        assert _read(paths["ambiguous"])["main"] == {"interloper": True}
        assert _read(paths["clean"])["main"]["provider"] == "nous"

        # Never retried: a same-id apply returns the record, writes nothing.
        again = apply_operation(journal, op["op_id"])
        assert again["state"] == OP_RECOVERY_REQUIRED
        assert _read(paths["ambiguous"])["main"] == {"interloper": True}

        # Undo is refused, and refusing changes nothing.
        with pytest.raises(ApplyUndoAmbiguous) as caught:
            undo_operation(journal, op["op_id"])
        assert caught.value.public_reason
        after = journal.get(op["op_id"])
        assert after["state"] == OP_RECOVERY_REQUIRED
        assert _steps(after) == [STEP_APPLIED, STEP_CONFLICT]
        assert _read(paths["clean"])["main"]["provider"] == "nous"

        # reconcile keeps the ambiguous operation where a human can see it.
        assert reconcile(journal, op["op_id"])["state"] == OP_RECOVERY_REQUIRED
        assert op["op_id"] not in {o["op_id"]
                                   for o in journal.unfinished_operations()}
    finally:
        journal.close()


# ------------------- 9c. the same rule for a host foreign diff

def test_host_foreign_diff_also_refuses_undo(tmp_path):
    """A host step that saw a foreign diff leaves the op ambiguous too: undo
    must not restore snapshots over bytes we cannot prove are ours."""
    paths, journal = _lab(tmp_path)
    path = paths["default"]
    try:
        op = plan_operation(journal, [_host_target(path)])
        token = apply_operation(journal, op["op_id"])["host_call"]["token"]
        _host_write(path, HOST_CHANGES, extra={"plugins": {"x": 1}})
        assert submit_host_result(journal, op["op_id"], 0, token,
                                  "ok")["state"] == OP_RECOVERY_REQUIRED

        with pytest.raises(ApplyUndoAmbiguous):
            undo_operation(journal, op["op_id"])
        assert _read(path)["plugins"] == {"x": 1}
    finally:
        journal.close()


# ------------------------------------------- 10. old-schema migration

_LEGACY_OPERATIONS = """
CREATE TABLE operations (
    op_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    payload TEXT NOT NULL
);
"""
_LEGACY_STEPS = """
CREATE TABLE steps (
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


def _legacy_journal(db_path):
    """A journal exactly as an older release created it (no host columns)."""
    db_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_LEGACY_OPERATIONS)
        conn.executescript(_LEGACY_STEPS)
        conn.execute("INSERT INTO operations VALUES"
                     " ('op-legacy', 'compensated', 1.0, 2.0, '{}')")
        conn.execute(
            "INSERT INTO steps VALUES ('op-legacy', 0, 'default', 'config',"
            " '/lab/old.json', 'compensated', ?, NULL, 'rev-a', 'rev-b')",
            (json.dumps({"main.model": "legacy/new"}, sort_keys=True),))
        conn.commit()
    finally:
        conn.close()


def test_journal_migrates_an_old_schema_database(tmp_path):
    db = tmp_path / "legacy" / "journal.db"
    _legacy_journal(db)
    journal = OperationJournal(str(db))
    try:
        columns = {row[1] for row in
                   journal._conn.execute("PRAGMA table_info(steps)")}
        assert {"identity", "write_phase", "executor", "host_call",
                "host_token_hash", "host_readback_keys",
                "host_allowed_prefixes"} <= columns
        op_columns = {row[1] for row in
                      journal._conn.execute("PRAGMA table_info(operations)")}
        assert "failure_category" in op_columns

        # Durability + privacy invariants survive the migration.
        assert journal._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() \
            == "wal"
        assert journal._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert journal._conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700

        # The legacy record is intact and still reads as a BACKEND step.
        legacy = journal.get("op-legacy")
        assert legacy["steps"][0]["changes"] == {"main.model": "legacy/new"}
        assert legacy["steps"][0]["executor"] is None
        assert legacy["steps"][0]["host_call"] is None
        assert legacy["steps"][0]["post_revision"] == "rev-b"

        # ... and a host operation works end to end on the migrated DB.
        lab = tmp_path / "lab" / "profiles" / "default"
        lab.mkdir(parents=True)
        path = lab / "config.json"
        path.write_text(json.dumps(LAB_DOC, indent=2, sort_keys=True))
        op = plan_operation(journal, [_host_target(str(path))])
        awaiting = apply_operation(journal, op["op_id"])
        assert awaiting["state"] == OP_AWAITING_HOST
        _host_write(str(path), HOST_CHANGES)
        result = submit_host_result(journal, op["op_id"], 0,
                                    awaiting["host_call"]["token"], "ok")
        assert result["state"] == OP_COMMITTED
        assert _read(str(path))["main"]["provider"] == "nous"
    finally:
        journal.close()
