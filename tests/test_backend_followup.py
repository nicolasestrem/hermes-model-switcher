"""Backend follow-up regressions (catalog promotion follow-up).

Two evidenced gaps in the integrated backend (16bba4b):

  F1  ``_preset_store`` salvages the valid records out of a partially corrupt
      ``presets.json`` and quarantines the ORIGINAL, but never rewrites the
      ACTIVE store. A second ``_preset_store()`` read (a later /state or a
      one-click apply) therefore finds no ``presets.json`` and silently returns
      an EMPTY store — the salvaged presets look lost. The cleaned store must be
      persisted after the ORIGINAL is preserved, and a persist failure must stay
      honest (quarantine retained, no false successful save, no silent loss).
  F2  the recovery journal opened with ``PRAGMA synchronous=NORMAL``: in WAL
      mode a committed journal write can be lost on power failure even though
      the engine guarantees intent/snapshot durability before external config
      writes. It must open ``synchronous=FULL`` (a stricter, not weaker, mode).
  F3  the public DTO carried ``failure_category`` straight from the journal;
      a malformed journal holding an arbitrary string would leak it. The
      projection must allowlist only the fixed categories (else null).

All tests run against throwaway /tmp Hermes homes or temp dirs — no live home,
no network, no inference.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from model_switcher.journal import (  # noqa: E402
    OperationJournal,
    to_public_operation,
)

import dashboard.plugin_api as plugin_api  # noqa: E402

HERMES_REPO = str(Path(
    os.environ.get("HERMES_MODEL_SWITCHER_HERMES_REPO",
                   Path.home() / ".hermes" / "hermes-agent")))

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


def _seed_good_preset(tc):
    changes = [{"profileId": "alpha", "slotId": "main", "action": "set",
                "to": {"provider": "lablocal", "model": "lab-7b-instruct"}}]
    r = tc.post("/presets", json={"name": "keep", "changes": changes})
    assert r.status_code == 200, r.text
    return r.json()["preset"]["id"]


def _corrupt_one_record():
    """Append a malformed record to the active store, returning the original
    record list so callers can assert byte-for-byte quarantine retention."""
    path = plugin_api._state_dir() / "presets.json"
    records = json.loads(path.read_text())
    records.append({"preset_id": "p-bad"})  # malformed: no name/targets
    path.write_text(json.dumps(records))
    return path, records


# --------------------------------------------------------------------------- #
# F1 — salvaged presets must persist across reads
# --------------------------------------------------------------------------- #

def test_salvaged_presets_survive_a_second_read(hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    pid = _seed_good_preset(tc)
    path, records = _corrupt_one_record()

    first = tc.get("/state")
    assert first.status_code == 200, first.text
    assert first.json()["presetsCorrupt"] is True
    assert [p["id"] for p in first.json()["presets"]] == [pid]

    # The bug: a second read finds presets.json moved aside and returns [].
    second = tc.get("/state")
    assert second.status_code == 200, second.text
    assert second.json()["presetsCorrupt"] is True
    assert [p["id"] for p in second.json()["presets"]] == [pid]

    # The salvaged store was rewritten to the ACTIVE path (valid record only)…
    assert path.exists()
    assert [r["preset_id"] for r in json.loads(path.read_text())] == [pid]
    # …while the ORIGINAL bytes stayed recoverable in quarantine.
    quarantined = list(plugin_api._state_dir().glob("presets.json.corrupt-*"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_text()) == records


def test_apply_after_a_salvage_read_is_not_unknown_preset(hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    pid = _seed_good_preset(tc)
    _corrupt_one_record()

    # A read salvages + quarantines; the follow-up apply is a SECOND
    # _preset_store() call and must still resolve the salvaged preset.
    assert tc.get("/state").status_code == 200
    r = tc.post("/operations", json={"presetId": pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["operation"]["state"] == "committed", body


def test_salvaged_store_is_visible_to_a_fresh_process(hermes_home):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    pid = _seed_good_preset(tc)
    _corrupt_one_record()
    assert tc.get("/state").status_code == 200  # salvage + quarantine + persist

    script = (
        "import sys, json\n"
        "sys.path.insert(0, sys.argv[1])\n"   # src
        "sys.path.insert(0, sys.argv[2])\n"   # repo root
        "sys.path.insert(0, sys.argv[3])\n"   # hermes-agent reference repo
        "import dashboard.plugin_api as plugin_api\n"
        "store = plugin_api._preset_store()\n"
        "print(json.dumps(sorted(store._presets)))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "src"), str(REPO), HERMES_REPO])
    out = subprocess.run(
        [sys.executable, "-c", script, str(REPO / "src"), str(REPO),
         HERMES_REPO],
        capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [pid]


def test_salvage_persist_failure_is_honest_no_loss_no_false_save(
        hermes_home, monkeypatch):
    _m(hermes_home, "default")
    _m(hermes_home, "alpha")
    tc = _client()
    _seed_good_preset(tc)
    path, records = _corrupt_one_record()

    real_replace = plugin_api.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        # 1st os.replace = quarantine the ORIGINAL (must succeed);
        # 2nd os.replace = persist the cleaned store (simulate a failure).
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("persist refused")
        return real_replace(src, dst)

    monkeypatch.setattr(plugin_api.os, "replace", flaky_replace)
    with pytest.raises(PermissionError):
        plugin_api._preset_store()

    # The ORIGINAL is preserved in quarantine and no false successful save
    # (a rewritten/empty active store) was produced.
    quarantined = list(plugin_api._state_dir().glob("presets.json.corrupt-*"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_text()) == records
    assert not path.exists()


# --------------------------------------------------------------------------- #
# F2 — journal durability mode
# --------------------------------------------------------------------------- #

def test_journal_opens_synchronous_full_with_wal_still_enabled(tmp_path):
    journal = OperationJournal(str(tmp_path / "j.db"))
    try:
        sync = journal._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert sync == 2, f"synchronous must be FULL(2), got {sync!r}"
        mode = journal._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        journal.close()


# --------------------------------------------------------------------------- #
# F3 — failure_category public projection allowlist
# --------------------------------------------------------------------------- #

VALID_CATEGORIES = ("revision_conflict", "store_unreadable",
                    "write_verification_failed", "io_error", "step_failed")


def test_public_projection_drops_unknown_failure_category_canary(tmp_path):
    journal = OperationJournal(str(tmp_path / "j.db"))
    try:
        op = journal.begin({"kind": "canary"})
        op_id = op["op_id"]
        canary = "bogus-category /etc/shadow"
        # Simulate a malformed journal: an arbitrary string written directly
        # into the column (bypassing the engine's fixed-label contract).
        journal._conn.execute(
            "UPDATE operations SET failure_category=? WHERE op_id=?",
            (canary, op_id))
        pub = to_public_operation(journal.get(op_id))
        assert pub["failure_category"] is None
        blob = json.dumps(pub)
        assert canary not in blob and "bogus-category" not in blob
        assert "/etc/shadow" not in blob

        # Every fixed category still projects verbatim.
        for label in VALID_CATEGORIES:
            journal.set_failure_category(op_id, label)
            got = to_public_operation(journal.get(op_id))
            assert got["failure_category"] == label
    finally:
        journal.close()


def test_dto_failure_is_null_for_unknown_category(hermes_home):
    _m(hermes_home, "default")
    tc = _client()
    assert tc.get("/state").status_code == 200
    dto = plugin_api._op_dto({
        "op_id": "op-x", "state": "compensated",
        "created_at": 1.0, "updated_at": 2.0,
        "failure_category": "arbitrary-leak-canary", "steps": []})
    assert dto["failure"] is None
    assert "arbitrary-leak-canary" not in json.dumps(dto)
