# Privacy audit

Audit date: 2026-10-14. This report records inspected behavior and verification limits; it is not a guarantee that private data cannot exist.

| Severity | Finding and reproduction | Fix | Verification |
| --- | --- | --- | --- |
| High | Stage a secret-bearing file, then replace its working copy with clean content. The scanner previously read only the clean working copy. | Scan index blobs independently; staged-only reads the index. Ignore staged deletions. | Standalone temporary-repository regressions pass. |
| High | Credentials in README.md bypassed all content checks. | Remove the README exemption. | Synthetic README canary rejected without printing its value. |
| Medium | Missing/unreadable files and failed Git blob reads became empty content and passed. | Record redacted read errors and exit 2. | Missing file and Git read regressions pass. |
| High | Journal chmod failures were ignored, potentially allowing recovery snapshots under broader Linux permissions. | Require private parent/database modes before SQLite opens; create the database with 0600. Failure occurs before connection/schema writes. | Both injected permission-denial cases pass; POSIX mode assertion requires Linux. |
| Medium (test reliability) | Harness refusal test assumed pytest temporary paths lived beneath the native Hermes root; ordinary /tmp runners correctly booted and failed the assertion. | Build an explicit sandbox native HOME with a nested .hermes target and pass the real reference source path. Both boot tests redirect native HOME. | Python compilation passes; remote real-resolver tests required. |
| Medium | Malformed base URLs could echo unsanitized credentials after URL parsing failed, or raise while reading an invalid port. | Omit malformed URLs; keep valid URLs with userinfo/query/fragment removed and preserve IPv6 brackets. | Actual sanitizer source regressions pass; host router integration requires remote suite. |

## Reviewed boundaries

The operation journal deliberately retains full snapshots for exact recovery. Public operation views project only operation status and step identity/revisions; snapshot content, changes, paths, and raw errors remain local. The journal refuses Git worktrees and symlink path components. A private parent protects SQLite sidecars; operating-system ACL behavior on native Windows remains unverified. Local attackers able to replace directories concurrently are outside the static path checks' protection.

The scanner reports rule names and locations, never matched values. The scanner itself and the existing synthetic privacy fixture remain explicitly exempt; structural config checks skip test fixtures. These exemptions are narrow maintenance boundaries, not a reason to store real credentials in tests. The scanner uses heuristic patterns and does not detect every credential format, personal identifier, image, or binary payload. Screenshot privacy must be verified visually with synthetic data.

## History inspection

Inspected 225 unique reachable Git blobs across all refs before implementation using the guard's path and content patterns, respecting its explicit source/fixture exemptions. Zero pattern findings. No private historical content was confirmed. This does not establish that all historical data is public; heuristic coverage and binary-image limitations still apply. No history was rewritten.

## Verification

Run independently of Hermes:

```sh
python -m unittest discover -s tests -p 'test_privacy*standalone.py' -v
python scripts/privacy_guard.py
```

Local result: 11 standalone tests ran; 10 passed, one POSIX-only permission check skipped on Windows. Scanner returned exit 0 for 97 tracked files at inspection time. Remote Linux mode checks and full host integration must be included in final acceptance. Raw command output is available in the implementation verification record.

### PR 10 review follow-up

Journal directories are checked for symlinks immediately before permission changes. Permission errors intentionally fail closed on every platform; POSIX mode verification adds a further check on Linux. Native Windows and mounts without enforceable private permissions remain unverified and may reject journal creation.

Scheme-less or malformed configured URLs display `<unparseable URL>` in public DTOs, preserving the distinction between a configured endpoint and a cleared field without echoing credentials. The privacy guard scans every tracked index blob plus existing working-tree files; an unstaged deletion cannot hide indexed credentials or cause a spurious working-tree read error.
