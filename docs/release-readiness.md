# Release readiness

This report records findings and verification for public release preparation. A built package or fixture screenshot does not establish authenticated Dashboard compatibility or provider inference. No publication, deployment, or merge is included.

## Findings

| Severity | Finding and evidence | Resolution | Verification |
| --- | --- | --- | --- |
| Medium | `scripts/build_package.py` deleted its output before checking required assets; an invalid `--out` could target the source checkout. | Check all inputs first, reject source ancestors and symlink paths, and require explicit replacement. | Package regression tests; see raw verification output. |
| High | Lab helpers compared their destination to the user home rather than `~/.hermes`, and recursively deleted installations/recovery data. | Correct live-root containment, reject symlinked destinations, require explicit replacement, and preserve private backups on replacement/removal. | Linux shell regression tests pending remote verification. |
| Low | The package README linked to docs and DESIGN.md that were absent from `COPY_FILES`. | Ship Markdown documentation and DESIGN.md with the package. | Package regression test checks local README links. |
| Low | README contained old test counts and runtime snapshots presented as current verification. | Replace with capability-focused copy and explicit verification boundaries. | Documentation review. |

## Verification boundaries

Linux with the user's existing Hermes checkout is the intended runtime target. Record the exact revision, Python environment, command output, and exit codes in the final verification handoff. macOS, native Windows, hosted-isolation writes, provider inference, and live switching of in-flight sessions are unverified.

Screenshots must show the real compiled plugin with synthetic isolated profile data. Sanitized captures of the compiled plugin in the Dashboard SDK fixture are included in `docs/images/`, with synthetic lab profiles; see that directory's README for provenance. These captures do not establish authenticated live deployment.

## Required release evidence

- Python and browser suites, frontend build, package assembly, plugin validation, and privacy scans pass against the final candidate.
- Real isolated backend Apply/readback and byte-exact guarded Undo pass.
- Light and dark layouts at 1440, 768, and 375px, keyboard navigation, long identifiers, loading/error states, and browser errors are checked.
- A fresh lab installation contains working assets and documentation links.
- Any serious unresolved finding or blocked material check remains explicitly listed; readiness is not inferred from partial checks.

## Verified candidate — 2026-10-14

Linux reference: Hermes `v0.22.0+canary.20261014T070054Z`, Python 3.13.15, FastAPI 0.133.1, pytest 9.1.1, Node 26.8.1. The active Dashboard uses a separate bundled Python 3.14.7; tests used the existing checkout environment. The reference checkout remained clean. The installed plugin was inspected read-only in an authenticated Dashboard; the candidate was not deployed there.

Raw command result lines, with checked exit codes:

```text
402 passed, 2 warnings in 30.53s
FINAL_PYTHON_EXIT=0
69 passed (31.3s)
FINAL_BROWSER_EXIT=0
BUILD_EXIT=0
PACKAGE_EXIT=0
INSTALL_EXIT=0
Validation passed.
VALIDATE_EXIT=0
UNINSTALL_EXIT=0
privacy guard: clean (109 files scanned)
PRIVACY_EXIT=0
```

The Python warnings originate in Hermes manifest parsing. Tests include real isolated Apply/readback/byte-exact Undo and six light/dark viewport combinations. No inference was performed. The original baseline passed 378 Python tests with one environment-dependent harness assertion, now fixed with an explicit sandbox native home. Its browser suite passed 69 tests after building fixture assets and setting TMPDIR.

### Remaining compatibility risk

Hermes validation warns about private core imports (catalog rule 9), including model catalog, profile scope and canonical writer seams. Validation passes, but these integrations can break on later releases. Official catalog eligibility is not established. Public API migration requires supported upstream equivalents; no substitute writer was introduced. Hosted isolation remains read-only. See [privacy audit](privacy-audit.md) for scanner, journal and URL-sanitization findings and fixes.

### Measured performance

Three real isolated state requests per profile count, milliseconds:

| Profiles | First request | Subsequent requests | Median |
| --- | --- | --- | --- |
| 2 | 540.51 | 11.25, 10.84 | 11.25 |
| 10 | 1179.63 | 63.60, 57.59 | 63.60 |
| 50 | 7201.91 | 1014.94, 947.32 | 1014.94 |

First requests after adding profiles include cold discovery. Fifty profiles expose a scalability limit. No additional cache was added because invalidation must preserve profile/runtime freshness.

Local Chromium SDK fixture navigation through rendered rows plus two animation frames, three fresh pages per size: 40 slots median 80ms; 200 slots 111ms; 1,000 slots 275ms. A synthetic 2,000-model catalog rendered in 21ms and search to one result took 9ms (single samples). These measurements do not establish provider discovery latency. No frontend optimization was justified by this sample.

## PR #10 review follow-up

Fixed recursive output containment and canonical output parent aliases, journal symlink-check ordering, nonblank redacted URL previews, Git-index scanning for deleted working files, flexible replacement flags, and backup placement outside plugin discovery. Shell files are pinned to LF. Permission failures intentionally remain fail-closed on every platform; native Windows runtime remains unverified. The README passes the privacy guard, committed bundles support fresh checkout package tests, and package versions remain 0.1.0 with Unreleased changelog entries.

Remote Linux raw verification:

```text
412 passed, 2 warnings in 33.55s
TEST_EXIT=0
```

Local privacy verification:

```text
Ran 13 tests in 1.623s
OK (skipped=1)
privacy guard: clean (110 files scanned)
```

Linux suite covers the symlink and permission checks skipped locally. Browser behavior was unchanged by this follow-up; previous 69-browser-test evidence remains scoped to the original candidate.
