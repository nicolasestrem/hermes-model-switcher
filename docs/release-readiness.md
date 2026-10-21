# Release readiness

This report records the 0.3.0 public-seams contract and release verification
boundaries. A built package, fixture screenshot or earlier green suite does not
establish final-candidate compatibility. No publication, deployment or merge is
included.

## Runtime reference

The tested Hermes reference is **v0.22.3**, commit
`5b147ab036d6d19131bff8d20701b5001eab844a`. See CONTRIBUTING.md for checkout and
suite setup. This pin makes tests reproducible; it is not a version gate.
`probe_runtime()` requires callable public config, home/profile and secret-scope
capabilities. Package version is informational, and no install stamp is required.
Missing capabilities and hosted isolation remain read-only.

The 0.3.1 reference re-pin was verified fresh: the backend and browser suites,
the private-import gate, the privacy guard and the package build all ran against
`5b147ab0` on the release commit, with the raw results recorded in the
CHANGELOG. Final commands, environment, exit codes and unresolved failures
still belong in the release handoff; no new suite result is asserted here.

## 0.3.0 integration boundaries

- Catalog, authentication and pricing evidence come from authenticated SDK reads
  of `/api/model/options`; auxiliary inheritance/plugin tasks and MoA presets
  come from `/api/model/auxiliary` and `/api/model/moa`. The plugin transforms
  supplied host payloads without private catalog imports or disk-cache readers.
  Missing payloads degrade truthfully, not to empty success or invented free cost.
- Main and compatible auxiliary assignments use public `/api/model/set` host
  steps. Other slots use public `hermes_cli.config` load/read-guard/save under
  scoped public home and secret helpers. Request gating belongs to the host.
- Operations persist `awaiting_host`, issue one-time host-call tokens, and verify
  the config after `/host-result`. Expensive-model confirmation is displayed as
  text and only explicit approval resends `confirm_expensive_model: true`.
- Main preview is semantic provider/model intent; an exact persisted diff is
  available only after host readback. Host routes have no expected-revision
  parameter: a pre-call revision check cannot prevent an intervening write.
  Foreign changes found at readback require `recovery_required` / `external_edit`,
  not silent success or blind rollback. Readback is not a global concurrency lock.
- Undo/compensation restores snapshots through the public backend writer and
  refuses revision conflicts. The journal copies every pre-change config and
  may therefore retain sensitive data even when the edited fields are harmless.
- State lives only at `get_hermes_home()/state/hermes-model-switcher/`. An unsafe
  source/worktree location or failed privacy check makes the plugin read-only;
  there is no alternate state location or fallback.

## Required final-candidate evidence

- Run backend and browser suites, frontend build, package assembly and plugin
  validation against the exact candidate and reference checkout. Run the host's
  `private_import_findings` acceptance check against shipped plugin code and
  require `[]`; a generic validation pass alone is insufficient.
- Exercise real isolated main/auxiliary host writes, SDK expensive-confirmation
  approval/cancellation, and backend slot writes with persisted readback.
- Exercise no-write and error responses, bad tokens, abandoned `awaiting_host`
  recovery, restart reconciliation, foreign edits and revision-guarded Undo.
  A host HTTP success alone must not mark an operation committed.
- Verify missing/malformed options, auxiliary and MoA payloads, unknown pricing,
  profile-scoped credential checks, capability failures and unsafe state paths.
- Check journal schema migration, private database/sidecar modes and public DTO
  redaction. Run the privacy guard against final sources and packaged files.
- Check light/dark layouts at 1440, 768 and 375px, keyboard navigation, long IDs,
  loading/error states and browser errors. Exercise the compiled plugin through
  the Dashboard SDK, not only a permissive standalone fixture.
- Install a fresh lab package and check its declared assets and documentation
  links. Retain serious unresolved findings and blocked checks explicitly.

Use isolated lab homes and fake credentials only; never inference or live-profile
writes. macOS, native Windows, hosted-isolation writes, provider inference and
hot-switching in-flight sessions remain unverified. Host assignments target new
sessions. Sanitized captures in `docs/images/` are fixture evidence with synthetic
profiles; they are not evidence of final 0.3.0 assets or live deployment.

## Historical findings and evidence

Earlier release preparation and PR #10 addressed these issues. These are
historical resolutions, not a claim that the refactored candidate has been rerun:

| Severity | Finding | Resolution retained for regression checks |
| --- | --- | --- |
| Medium | Package output could be removed before asset validation. | Validate inputs first, reject source ancestors/symlink aliases, and require explicit replacement. |
| High | Lab helper containment and deletion could endanger installations/recovery data. | Refuse live-root containment and symlink destinations; preserve replacement/removal backups outside plugin discovery. |
| Low | Packaged README links pointed at omitted documentation. | Ship only the explicit `DOC_FILES` manifest and validate every approved file before replacement. |
| Low | Old suite totals and runtime snapshots implied current verification. | Remove obsolete green totals; require candidate-specific receipts. |
| High | Scanner and journal privacy gaps could conceal staged credentials or weaken snapshot permissions. | Scan index blobs independently, fail closed on read/permission errors and check symlinks before permission changes. See [privacy audit](privacy-audit.md). |

Earlier Python/browser results, package/lab checks and performance measurements
applied to the pre-refactor candidate only and are not carried forward as 0.3.0
acceptance. The old private-import compatibility warning is superseded by the
public-seams acceptance gate, not by an exemption or a claim of catalog approval.
Official catalog eligibility still requires the venue's review.

The documentation publication boundary remains an exact allowlist: a local or
ignored file under `docs/` does not become publishable just by existing. Update
`DOC_FILES` deliberately for new public files; never package private notes,
configuration exports or raw screenshots by directory recursion.
