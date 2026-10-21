"""Backend reliability batch regressions (catalog-promotion plan B1, B3–B6).

One test (or a small cluster) per owned fix, all against throwaway /tmp Hermes
homes or temp dirs — no live home, no inference, no network.

  B1  tests/real-server.mjs resolves its interpreter portably
      (HMS_PYTHON > <repo>/.venv/bin/python > python3) and only starts the
      server when it is the process entry point.
  B3  the operation DTO carries a safe ``failure`` category and ``final_state``,
      the response ``ok`` reflects committed vs compensated/partial/
      recovery_required, and no raw exception text ever leaves the backend.
  B4  two targets that share a (profile, store, path) but disagree on a dotted
      key are rejected; identical duplicates still coalesce.
  B5  the journal opens with WAL + a bounded busy timeout, closes its
      connection when init fails, and tolerates a second-process writer.
  B6  a single corrupt preset record is quarantined while the valid records are
      salvaged, and a failed quarantine never silently discards data.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from model_switcher.adapters import StoreConflict, StoreUnreadable  # noqa: E402
from model_switcher.engine import (  # noqa: E402
    ApplyBlocked,
    StepFailure,
    _failure_category,
    apply_operation,
    plan_operation,
)
from model_switcher.journal import (  # noqa: E402
    OP_COMMITTED,
    OP_COMPENSATED,
    OperationJournal,
)

import dashboard.plugin_api as plugin_api  # noqa: E402
from host_protocol_helpers import (  # noqa: E402
    finish_host_main_call, finish_host_main_calls,
)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

BASE_CONFIG = """\
model:
  provider: openai
  default: gpt-4o-mini
providers:
  lablocal:
    base_url: http://127.0.0.1:8080/v1
"""


def _client():
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="")
    return TestClient(app)


def _m(cp, name, config=BASE_CONFIG):
    home = cp.path
    pdir = home if name == "default" else home / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(config)
    (pdir / ".env").write_text("FAKE_LAB_PROVIDER_KEY=lab-only\n")
    return pdir / "config.yaml"


def _two_profile_lab(tmp_path):
    """One journal + two distinct-store targets (default, alpha)."""
    journal = OperationJournal(str(tmp_path / "journal.db"))
    targets = []
    for name in ("default", "alpha"):
        d = tmp_path / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        p = d / "config.json"
        p.write_text(json.dumps(
            {"main": {"provider": "openrouter", "model": "m/old"}}))
        targets.append({"profile": name, "store_kind": "config",
                        "path": str(p), "changes": {"main.model": "m/new"}})
    return journal, targets


# --------------------------------------------------------------------------- #
# B1 — portable interpreter resolution
# --------------------------------------------------------------------------- #

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_real_server_resolves_python_portably(tmp_path):
    real_server = REPO / "tests" / "real-server.mjs"
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("")  # a local venv that must win over python3
    runner = tmp_path / "probe.mjs"
    runner.write_text(
        'import { pathToFileURL } from "node:url";\n'
        'const [,, mod, repo] = process.argv;\n'
        'const { resolvePython } = await import(pathToFileURL(mod).href);\n'
        'console.log(JSON.stringify([\n'
        '  resolvePython({ HMS_PYTHON: "/opt/py" }, "/nonexistent"),\n'
        '  resolvePython({}, repo),\n'
        '  resolvePython({}, "/nonexistent-repo"),\n'
        ']));\n'
    )
    out = subprocess.run([NODE, str(runner), str(real_server), str(tmp_path)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    # Importing the harness must not bind ports or spawn uvicorn; if it did the
    # process would hang (busy port / await) rather than print and exit.
    assert json.loads(out.stdout) == [
        "/opt/py", str(venv_bin / "python"), "python3"]


# --------------------------------------------------------------------------- #
# B3 — operation failure contract
# --------------------------------------------------------------------------- #

def test_failure_categories_are_fixed_labels():
    assert _failure_category(StoreConflict("x")) == "revision_conflict"
    assert _failure_category(StoreUnreadable("x")) == "store_unreadable"
    assert _failure_category(StepFailure("x")) == "write_verification_failed"
    assert _failure_category(OSError("x")) == "io_error"
    assert _failure_category(ValueError("raw message must not leak")) == "step_failed"


def test_failed_apply_persists_safe_category_not_raw_message(tmp_path):
    journal, targets = _two_profile_lab(tmp_path)
    try:
        op = plan_operation(journal, targets)
        result = apply_operation(journal, op["op_id"], fail_after=1)
        assert result["state"] == OP_COMPENSATED
        assert result["failure_category"] == "write_verification_failed"
        assert "failure" not in result  # the raw-message key is gone
        stored = journal.get(op["op_id"])
        assert stored["failure_category"] == "write_verification_failed"
        assert stored["state"] == OP_COMPENSATED
    finally:
        journal.close()


def test_op_ok_matrix_reflects_outcome():
    assert plugin_api._op_ok({"state": OP_COMMITTED}) is True
    assert plugin_api._op_ok({"state": "undone"}) is True
    for state in ("compensated", "partial", "recovery_required", "pending"):
        assert plugin_api._op_ok({"state": state}) is False
    assert plugin_api._op_ok(None) is False


def _fleet_preset(tc):
    changes = [{"profileId": p, "slotId": "main", "action": "set",
                "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}
               for p in ("default", "alpha")]
    r = tc.post("/presets", json={"name": "fleet", "changes": changes})
    assert r.status_code == 200, r.text
    return r.json()["preset"]["id"]


def test_apply_success_reports_ok_true_committed(hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    pid = _fleet_preset(tc)
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200, r.text
    r = finish_host_main_calls(tc, r, hermes_home.path)
    body = r.json()
    assert body["ok"] is True, body
    op = body["operation"]
    assert op["state"] == "committed" and op["final_state"] == "committed"
    assert op["failure"] is None
    assert set(op) == {"id", "state", "results", "undoable",
                       "final_state", "failure"}
    for res in op["results"]:
        assert set(res) == {"profileId", "status", "seq", "store",
                            "revisionBefore", "revisionAfter"}


@pytest.mark.parametrize("outcome, category", [
    ("error", "host_error"),
    ("ok", "host_no_write"),
])
def test_apply_failure_reports_ok_false_with_safe_category(hermes_home,
                                                           outcome, category):
    # The first host write succeeds; the second host call fails without writing.
    # Both explicit failure and false-success/no-write must compensate the first.
    private_marker = "synthetic-private-snapshot-value"
    config = BASE_CONFIG + f"unrelated:\n  private_note: {private_marker}\n"
    paths = {p: _m(hermes_home, p, config=config) for p in ("default", "alpha")}
    before = {p: yaml.safe_load(path.read_text()) for p, path in paths.items()}
    tc = _client()
    pid = _fleet_preset(tc)
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200, r.text
    assert r.json()["hostCall"]["seq"] == 0
    assert r.json()["hostCall"]["query"]["profile"] == "default"
    assert {p: yaml.safe_load(path.read_text()) for p, path in paths.items()} == before
    r = finish_host_main_call(tc, r, hermes_home.path)
    assert r.json()["operation"]["state"] == "awaiting_host", r.text
    assert yaml.safe_load(paths["default"].read_text())["model"]["default"] == \
        "lab-7b-instruct"
    assert yaml.safe_load(paths["alpha"].read_text()) == before["alpha"]
    call = r.json()["hostCall"]
    assert call["seq"] == 1 and call["query"]["profile"] == "alpha"
    r = tc.post(f"/operations/{call['opId']}/host-result", json={
        "seq": call["seq"], "token": call["token"], "outcome": outcome,
        "status": 503 if outcome == "error" else 200,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False, body
    op = body["operation"]
    assert op["state"] == "compensated", op
    assert op["final_state"] == "compensated", op
    assert op["failure"] == category, op
    assert {p: yaml.safe_load(path.read_text()) for p, path in paths.items()} == before
    persisted = tc.get(f"/operations/{op['id']}")
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["operation"] == op
    # No raw message, snapshot, payload, path or expired token may escape.
    assert "hostCall" not in body
    for response in (r, persisted):
        assert private_marker not in response.text
        assert str(hermes_home.path) not in response.text
        assert call["token"] not in response.text
    assert not ({"error", "snapshot", "payload", "path", "changes"} & set(op))
    assert set(op) == {"id", "state", "results", "undoable",
                       "final_state", "failure"}
    assert [res["seq"] for res in op["results"]] == [0, 1]
    for res in op["results"]:
        assert set(res) == {"profileId", "status", "seq", "store",
                            "revisionBefore", "revisionAfter"}


# --------------------------------------------------------------------------- #
# B4 — conflicting merged target keys
# --------------------------------------------------------------------------- #

def test_conflicting_merged_target_keys_are_rejected(tmp_path):
    journal, targets = _two_profile_lab(tmp_path)
    try:
        shared = [dict(targets[0], changes={"main.model": "a"}),
                  dict(targets[0], changes={"main.model": "b"})]
        with pytest.raises(ApplyBlocked):
            plan_operation(journal, shared)
        assert journal.unfinished_operations() == []
    finally:
        journal.close()


def test_identical_duplicate_merged_target_keys_are_allowed(tmp_path):
    journal, targets = _two_profile_lab(tmp_path)
    try:
        same = [dict(targets[0], changes={"main.provider": "nous"}),
                dict(targets[0], changes={"main.provider": "nous"})]
        op = plan_operation(journal, same)
        assert len(op["steps"]) == 1  # coalesced, no conflict
        assert apply_operation(journal, op["op_id"])["state"] == OP_COMMITTED
    finally:
        journal.close()


# --------------------------------------------------------------------------- #
# B5 — journal lifetime / contention safety
# --------------------------------------------------------------------------- #

def test_journal_enables_wal_and_busy_timeout(tmp_path):
    journal = OperationJournal(str(tmp_path / "j.db"))
    try:
        mode = journal._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        busy = journal._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy >= 1000
    finally:
        journal.close()


def test_journal_closes_connection_when_init_fails(tmp_path, monkeypatch):
    import model_switcher.journal as jmod
    real_connect = jmod.sqlite3.connect
    made = []

    class _Boom:
        def __init__(self, real):
            self._real = real
            self.closed = False

        def __getattr__(self, name):
            return getattr(self._real, name)

        def executescript(self, *a, **k):
            raise sqlite3.OperationalError("boom during schema init")

        def close(self):
            self.closed = True
            self._real.close()

    def fake_connect(*a, **k):
        conn = _Boom(real_connect(*a, **k))
        made.append(conn)
        return conn

    monkeypatch.setattr(jmod.sqlite3, "connect", fake_connect)
    with pytest.raises(sqlite3.OperationalError):
        OperationJournal(str(tmp_path / "p" / "j.db"))
    assert made and made[0].closed is True


def test_two_process_lock_contention_is_tolerated(tmp_path):
    db = tmp_path / "shared.db"
    marker = tmp_path / "child.ready"
    journal = OperationJournal(str(db))
    script = (
        "import sys, pathlib\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from model_switcher.journal import OperationJournal\n"
        "pathlib.Path(sys.argv[3]).write_text('ready')\n"
        "j = OperationJournal(sys.argv[2])\n"
        "try:\n"
        "    j.begin({'kind': 'contender'})\n"
        "    print('ok')\n"
        "finally:\n"
        "    j.close()\n"
    )
    journal.begin({"kind": "holder"})
    proc = None
    try:
        journal._conn.execute("BEGIN IMMEDIATE")
        journal._conn.execute("UPDATE operations SET updated_at=updated_at")
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(REPO / "src"), str(db),
             str(marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        deadline = time.time() + 20
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.4)  # let the child reach the guarded write
        journal._conn.execute("COMMIT")
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        assert "ok" in out
        assert "database is locked" not in err + out
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        try:
            journal._conn.execute("ROLLBACK")
        except Exception:
            pass
        journal.close()


# --------------------------------------------------------------------------- #
# B6 — preset salvage + honest quarantine
# --------------------------------------------------------------------------- #

def _seed_good_preset(tc):
    changes = [{"profileId": "alpha", "slotId": "main", "action": "set",
                "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]
    r = tc.post("/presets", json={"name": "keep", "changes": changes})
    assert r.status_code == 200, r.text
    return r.json()["preset"]["id"]


def test_single_corrupt_record_is_quarantined_and_valid_presets_salvaged(
        hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    pid = _seed_good_preset(tc)
    path = plugin_api._state_dir() / "presets.json"
    records = json.loads(path.read_text())
    assert len(records) == 1
    records.append({"preset_id": "p-bad"})  # malformed: no name/targets
    path.write_text(json.dumps(records))

    r = tc.get("/state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["presetsCorrupt"] is True
    # the valid preset survived; only the corrupt record was dropped
    assert [p["id"] for p in body["presets"]] == [pid]
    quarantined = list(plugin_api._state_dir().glob("presets.json.corrupt-*"))
    assert len(quarantined) == 1
    # the ORIGINAL bytes (both records) are preserved for an operator
    assert json.loads(quarantined[0].read_text()) == records


def test_salvage_quarantine_failure_is_honest_no_silent_loss(hermes_home,
                                                             monkeypatch):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    _seed_good_preset(tc)
    path = plugin_api._state_dir() / "presets.json"
    records = json.loads(path.read_text())
    records.append({"preset_id": "p-bad"})
    path.write_text(json.dumps(records))

    def refuse(*_a, **_k):
        raise PermissionError("test quarantine refusal")

    monkeypatch.setattr(plugin_api.os, "replace", refuse)
    with pytest.raises(PermissionError):
        plugin_api._preset_store()
    # a failed rename must not overwrite the original -> nothing is lost
    assert json.loads(path.read_text()) == records
