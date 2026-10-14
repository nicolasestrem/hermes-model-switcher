#!/usr/bin/env python3
"""Privacy guard: scan tracked + staged files for private data.

Checks BOTH paths and content of tracked files (git ls-files) and staged
files (git diff --cached --name-only) for:

- paths that look like Hermes configs, exports, journal DBs, snapshots,
  backups, .env files, credentials dumps or session transcripts;
- content that looks like a full Hermes config export, a journalled
  raw-config snapshot, or embedded credentials.

On any finding it prints file path (+ line number, + matched rule name)
and exits 1. Matched VALUES are never printed.

Run:  python3 scripts/privacy_guard.py [--staged-only]
Exit codes: 0 clean, 1 findings, 2 error.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys

# --- path rules -----------------------------------------------------------
PATH_RULES = [
    ("hermes-config", re.compile(r"(^|/)(config\.ya?ml|profile(s)?\.ya?ml)$")),
    ("hermes-export", re.compile(r"(^|/)(export|dump|hermes[-_].*export).*\.(json|ya?ml|txt)$")),
    ("env-file", re.compile(r"(^|/)\.env(\..*)?$")),
    ("journal-db", re.compile(r"\.(db|sqlite3?)(\..*)?$")),
    ("snapshot", re.compile(r"(^|/)(snapshots?|recovery)[-/].*|\.snapshot\.json$")),
    ("backup", re.compile(r"(^|/)backups?/|\.bak$")),
    ("credentials", re.compile(r"credential|secret|token|apikey|api[-_]key", re.I)),
    ("transcript", re.compile(r"transcript|session[-_]?log", re.I)),
]

# --- content rules --------------------------------------------------------
# CREDENTIAL_RULES run on every file (including tests/fixtures).
# STRUCTURAL_RULES look for full config/snapshot *payloads*; they are
# skipped for tests/** because tests deliberately embed synthetic fixtures.
CREDENTIAL_RULES = [
    ("api-key-literal", re.compile(
        r"(sk|pk)-[A-Za-z0-9_-]{20,}\b")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b")),
    ("secret-assignment", re.compile(
        r"\b(api[_-]?key|secret|password|token)\b[\"']?\s*[:=]\s*[\"']?"
        r"(?=[A-Za-z0-9/+_-]{16,}\d)[A-Za-z0-9/+_-]{16,}", re.I)),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]

_STRUCT_PAYLOAD = re.compile(
    r"\"(snapshot|changes)\"\s*:\s*\"\{")   # serialized JSON inside a string
_CONFIG_KEYS = re.compile(r"^(model|profiles|provider|default):", re.M)

CONTENT_RULES = CREDENTIAL_RULES + [
    ("journal-snapshot-dump", _STRUCT_PAYLOAD),
]

# Files allowed to contain these patterns (this script's own rules, tests).
ALLOW_GLOB = [
    "scripts/privacy_guard.py",
    "tests/test_privacy.py",
    ".gitignore",
]


def _allowlisted(path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in ALLOW_GLOB)


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(2)
    return r.stdout


def tracked_files() -> list[str]:
    return [l for l in git("ls-files", "-z").split("\0") if l]


def staged_files() -> list[str]:
    out = set()
    for l in git("diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z").split("\0"):
        if l:
            out.add(l)
    return sorted(out)


def read_bytes(path: str, ref: str | None = None) -> bytes:
    if ref:
        r = subprocess.run(["git", "show", f"{ref}{path}" if ref == ":" else f"{ref}:{path}"],
                           capture_output=True)
        if r.returncode != 0:
            raise OSError("cannot read Git blob")
        return r.stdout
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        raise OSError("cannot read working-tree file") from None


def scan(paths: list[str], blob_ref: str | None = None) -> list[str]:
    findings: list[str] = []
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        for name, rx in PATH_RULES:
            if rx.search(path):
                findings.append(f"{path}: PATH-RULE {name}")
        if _allowlisted(path):
            continue
        try:
            data = read_bytes(path, blob_ref)
        except OSError:
            findings.append(f"{path}: READ-ERROR (content unavailable)")
            continue
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            continue
        # structural: whole-file heuristic for full Hermes config exports
        # (≥3 distinct top-level config keys). Skipped for tests/** —
        # synthetic fixtures there are deliberate.
        if not path.startswith("tests/") and not path.startswith("test_"):
            keys = set(_CONFIG_KEYS.findall(text))
            if len(keys) >= 3:
                findings.append(
                    f"{path}: CONTENT-RULE hermes-config-export (value withheld)")
        for i, line in enumerate(text.splitlines(), 1):
            for name, rx in CONTENT_RULES:
                if rx.search(line):
                    findings.append(
                        f"{path}:{i}: CONTENT-RULE {name} (value withheld)")
    return findings


def main(argv: list[str]) -> int:
    staged_only = "--staged-only" in argv
    staged = staged_files()
    files = staged if staged_only else tracked_files()
    # Scan the index independently: unstaged edits must never hide staged secrets.
    findings = scan(staged if staged_only else files, blob_ref=":")
    if not staged_only:
        findings += scan([path for path in files if os.path.lexists(path)])
    findings = list(dict.fromkeys(findings))
    if findings:
        print("PRIVACY GUARD: private-looking data in tracked files\n"
              "(matched values are intentionally NOT printed):")
        for f in findings:
            print("  " + f)
        print("Refusing. Remove the private data or add an explicit "
              "allowlist entry if it is a false positive.")
        return 2 if any("READ-ERROR" in f for f in findings) else 1
    print(f"privacy guard: clean ({len(files)} files scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
