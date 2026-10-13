"""Privacy safeguards tests: journal isolation, private modes, safe DTO,
and the privacy guard scan. All fixtures are synthetic — no real Hermes
configs or credentials are used anywhere in this file.
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from model_switcher.journal import (  # noqa: E402
    OperationJournal, PublicOperationView, is_inside_git_worktree,
    to_public_operation,
)


# --- journal placement ------------------------------------------------------

def test_refuses_journal_inside_this_git_worktree():
    # This repo IS a git worktree; any path inside it must be refused,
    # including inside git-ignored directories like __pycache__.
    with pytest.raises(ValueError, match="worktree"):
        OperationJournal(str(REPO / "__pycache__" / "j.db"))


def test_refuses_journal_in_ignored_dir_of_any_worktree(tmp_path):
    # A separate throwaway git worktree (main checkout flavor).
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(ValueError):
        OperationJournal(str(repo / "ignored" / "j.db"))


def test_is_inside_git_worktree_unit():
    assert is_inside_git_worktree(str(REPO / "a" / "b.db"))
    assert not is_inside_git_worktree("/tmp/nonexistent-xyz/j.db")


def test_refuses_symlink_db(tmp_path):
    real = tmp_path / "real.db"
    real.write_bytes(b"")
    link = tmp_path / "link.db"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        OperationJournal(str(link))


def test_refuses_symlink_parent(tmp_path):
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    link_dir = tmp_path / "journals"
    link_dir.symlink_to(real_dir)
    with pytest.raises(ValueError, match="symlink"):
        OperationJournal(str(link_dir / "j.db"))


# --- private permissions ----------------------------------------------------

def test_private_db_and_dir_permissions(tmp_path):
    db = tmp_path / "private" / "j.db"
    j = OperationJournal(str(db))
    j.close()
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(db.parent).st_mode) == 0o700


def test_private_snapshot_data_stays_local(tmp_path):
    """Recovery snapshot content must persist (exact undo) but only in the
    private DB — never outside it."""
    db = tmp_path / "p" / "j.db"
    j = OperationJournal(str(db))
    j.begin({"kind": "test"})
    snap = {"raw": "model:\n  provider: syn\n  default: m1\n"}
    j.add_step("op-x", 0, "profA", "store", "/tmp/store.yaml",
               {"k": "v"}, snap, "rev1")
    j.close()
    assert b"provider: syn" in db.read_bytes()
    assert not any(p.is_file() and b"provider: syn" in p.read_bytes()
                   for p in tmp_path.rglob("*") if p != db)


# --- safe public DTO --------------------------------------------------------

def _op_with_secrets():
    return {
        "op_id": "op-abc123",
        "state": "committed",
        "created_at": 1.0,
        "updated_at": 2.0,
        "payload": {"raw_config": "model:\n  provider: syn\n  api_key: sk-SYNTHETIC-0000000000000000"},
        "steps": [{
            "seq": 0, "profile": "profA", "store_kind": "store",
            "path": "/lab-home/.hermes/profiles/profA/config.yaml",
            "state": "applied",
            "changes": {"default": "m2"},
            "snapshot": {"raw": "model:\n  api_key: sk-SYNTHETIC-0000000000000001"},
            "pre_revision": "r1", "post_revision": "r2",
            "error": "Exception: boom with sk-SYNTHETIC-0000000000000002",
        }],
    }


def test_public_dto_allowlist_only_status_and_slot_outcome():
    pub = to_public_operation(_op_with_secrets())
    assert set(pub) == {"op_id", "state", "created_at", "updated_at", "steps"}
    step = pub["steps"][0]
    assert set(step) == {"seq", "profile", "store_kind", "state",
                         "pre_revision", "post_revision"}
    blob = json.dumps(pub)
    for forbidden in ("payload", "path", "changes", "snapshot", "error",
                      "raw_config", "api_key", "/lab-home"):
        assert forbidden not in blob


def test_public_view_roundtrip(tmp_path):
    db = tmp_path / "j.db"
    j = OperationJournal(str(db))
    op = j.begin({"kind": "test", "raw": "model:\n  api_key: sk-SYNTH-x"})
    j.add_step(op["op_id"], 0, "profA", "store", "/tmp/s.yaml",
               {"k": "v"}, {"raw": "model:\n  api_key: sk-SYNTH-y"}, "r1")
    view = PublicOperationView(j)
    pub = view.get(op["op_id"])
    assert pub["state"] == "applying"
    assert pub["steps"][0]["profile"] == "profA"
    blob = json.dumps(pub)
    assert "sk-SYNTH" not in blob and "/tmp/s.yaml" not in blob
    j.close()


# --- privacy guard script ---------------------------------------------------

GUARD = REPO / "scripts" / "privacy_guard.py"

# Synthetic canaries — look real, are not real.
CANARY_CONTENT_RULES = [
    ("api-key-literal", "".join(map(chr, [115, 107, 45, 83, 89, 78, 84, 72, 45, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97]))),
    ("bearer-token", "".join(map(chr, [65, 117, 116, 104, 111, 114, 105, 122, 97, 116, 105, 111, 110, 58, 32, 66, 101, 97, 114, 101, 114, 32, 83, 89, 78, 84, 72, 46, 116, 111, 107, 101, 110, 46, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97]))),
    ("secret-assignment", "".join(map(chr, [97, 112, 105, 95, 107, 101, 121, 32, 61, 32, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 97, 49, 83, 89, 78, 84, 72]))),
    ("aws-access-key", "".join(map(chr, [65, 75, 73, 65, 73, 79, 83, 70, 79, 68, 78, 78, 55, 83, 89, 78, 84, 72, 48, 49]))),
]
CANARY_PATH_RULES = [
    ("env-file", ".env"),
    ("journal-db", "profiles.db"),
    ("credentials", "credentials-dump.json"),
    ("hermes-config", "config.yaml"),
    ("transcript", "session-transcript-2026-10-13.md"),
]


def test_guard_rejects_canary_content_without_printing_values():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pg", GUARD)
    pg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pg)
    for rule, canary in CANARY_CONTENT_RULES:
        assert not pg._allowlisted(f"fixture-{rule}.txt")
        out = io.StringIO()
        with redirect_stdout(out):
            findings = pg.scan([f"fixture-{rule}.txt"])  # path clean
        # content scan needs a file on disk:
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            f = Path(d) / "sample.txt"
            f.write_text(f"some line\n{canary}\n")
            findings = pg.scan([str(f)])
        joined = "\n".join(out.getvalue())
        assert any(f"CONTENT-RULE {rule}" in x for x in findings), rule
        assert canary not in findings and canary not in joined


def test_guard_rejects_canary_paths():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pg", GUARD)
    pg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pg)
    for rule, path in CANARY_PATH_RULES:
        findings = pg.scan([f"some/dir/{path}"])
        assert any(f"PATH-RULE {rule}" in x for x in findings), rule


def test_guard_allows_own_sources():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pg", GUARD)
    pg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pg)
    # content rules would fire on the script itself; allowlist covers it
    assert pg._allowlisted("scripts/privacy_guard.py")
    assert pg._allowlisted("tests/test_privacy.py")


def test_worktree_repo_is_clean():
    """The real gate: run the guard against the actual repo tree."""
    r = subprocess.run([sys.executable, str(GUARD)], cwd=REPO,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
