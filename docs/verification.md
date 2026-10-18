# Verification scope

This page separates local checks from CI, real host behavior and deployment.
No production configuration, inference call or live plugin installation is used by these checks.

## Observed integrated checks

Candidate `feat/model-catalog` (release-candidate tree; commit id recorded in the
release PR) was checked on Linux, Python 3.13 and Node 26:

```text
512 passed, 1 skipped, 0 failed (513 tests)
124 passed (59.6s)
```

Package assembly (`scripts/build_package.py --replace`) and the privacy guard
(`scripts/privacy_guard.py`, clean) ran on the same tree.

The single local skip is the explicitly gated clean-checkout bootstrap E2E test.
It was exercised separately with its gate enabled (`32 passed, 5 warnings in 9.27s`
including bootstrap and adjacent write-path tests); CI enables it too. These are
revision-bounded historical receipts, not promises that a later checkout passes.

Commands: `python -m pytest -o addopts='' -q` and `npm test`. The browser command needs no `HMS_PYTHON` override: its entry point and seed reader share a resolver (explicit override, local virtual environment, then `python3`). Note `pytest.ini` already sets `addopts = -q`; adding another `-q` on the command line suppresses the final summary line — use `-o addopts='' -q`, or count the progress rows.

The backend suite exercises journal privacy, commit/compensation/undo, malformed URLs, conflicting targets, two-process SQLite contention, persistent preset salvage across fresh processes, and public failure-category allowlisting. The browser suite includes real-router configuration writes and byte-exact undo plus SDK-contract, error presentation, theme opacity, keyboard and narrow-layout checks. UI-only fixtures are not evidence of a deployed Dashboard or provider inference.

## Real Hermes Dashboard

Final acceptance uses the actual Hermes Dashboard in a throwaway home with synthetic configuration, not the SDK fixture. It checks the runtime's home/profile/plugin discovery resolvers before launching, authenticated API responses, served bundle hashes, rendered state, preview without writes, Apply, and byte-exact Undo. Port/process/lab cleanup is a separate required check. Results are recorded for an exact candidate commit; a baseline result is not automatically evidence for a later revision.

## Public CI

`.github/workflows/ci.yml` runs Linux backend tests on Python 3.11/3.13, browser tests on Node 22/24, and Python 3.10 source compilation/privacy checks. Runtime-dependent suites fetch an immutable Hermes source revision and refuse a revision mismatch. CI uses synthetic lab data and no provider credentials; it publishes no raw configs, journals, recovery snapshots or lab logs.

A workflow existing in this repository is not evidence that a GitHub runner passed it. Use the PR's checks for the current commit and inspect any failure before treating that revision as verified.

## Boundaries

- No inference benchmark or live-session model switch is claimed.
- Linux is the exercised platform; macOS and native Windows remain unverified.
- Private Hermes imports remain a documented compatibility risk; see [catalog submission](catalog-submission.md).
- A ready PR is not a merged release, official endorsement, curated catalog listing or live deployment.
