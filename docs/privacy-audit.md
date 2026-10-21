# Privacy audit

Scope: 0.3.0 public-seams boundaries, with historical findings from the 2026-10-14
audit and PR #10 follow-up. This report is not a guarantee that private data cannot
exist. Historical checks do not establish final-candidate verification.

## State and recovery data

Persistent state lives **only** at
`get_hermes_home()/state/hermes-model-switcher/`. There is no alternate state-dir
setting or fallback. A location resolving inside plugin source or a Git worktree
makes the plugin read-only. Symlink/path and enforceable private-permission checks
also fail closed; an unsafe location is never repaired by silently moving state.

The undo journal copies **every pre-change config**, not just the provider/model
fields being edited. These snapshots may contain sensitive data, including
credentials embedded in an existing config. Presets are not the only retained
local data. Do not attach journals, database sidecars, backups or complete configs
to bug reports or publish them with the plugin.

The journal requires a private 0700 directory and 0600 database before SQLite
opens, with WAL and `synchronous=FULL`; the private parent protects sidecars.
Existing databases receive an idempotent migration for host-step executor,
call/token-hash fields and `awaiting_host` state. Private mode failures refuse
writes. Native Windows ACL behaviour and filesystems without enforceable private
permissions remain unverified and may reject journal creation. Static path checks
do not protect against a local attacker replacing directories concurrently.

Lab replacement/removal preserves plugin trees outside plugin discovery. The
current home-owned journal is separate and is not removed by moving the plugin
tree; legacy plugin backups may also retain private recovery data. Applied model
choices remain in effect. Inspect exact retained paths before deleting them;
deletion permanently removes their recovery evidence.

## Public routes and disclosure

The UI obtains model options/pricing/authentication, auxiliary tasks and MoA
presets through SDK-authenticated public host routes. Catalog transforms do not
read provider caches or registry files directly. Missing or pending pricing stays
unknown, never free. Host data is untrusted display/credential/cost evidence, not
permission to target another profile or expand a reviewed write.

Profile identity and home scope use public `hermes_constants` helpers. Credential
presence checks use `agent.secret_scope.get_secret` under the target home's secret
scope; the plugin does not open another profile's environment file. Otherwise it
uses host options' authentication evidence, with unknown failing closed. A custom
endpoint retains the existing credential policy. The host gates plugin requests;
the plugin does not duplicate that gate through a private backend probe.

Main and compatible auxiliary writes are SDK calls to `/api/model/set`; other
slots use public config load/read-guard/save under the scoped home. Host-call
bodies never include API keys. Main calls leave endpoint resolution to Hermes;
auxiliary endpoint URLs must not carry userinfo or query data. Host
`confirm_message` is rendered as text; explicit approval alone resends
`confirm_expensive_model: true`. Plugin billing approval remains a separate gate.

Public operation views use explicit allowlists: no raw configs, snapshots, local
paths or exception strings. `hostCall` exposes only operation/step identity,
method/path/query/body and the current step's one-time token. The journal stores
the token hashed; tokens for other steps are not exposed. `/host-result` accepts
only step/token, fixed outcome and optional numeric HTTP status, never host
response text. Token mismatch returns a fixed category without changing state.

Scheme-less or malformed configured URLs display `<unparseable URL>` rather than
echoing credentials. Valid public URL previews remove userinfo, query and fragment
and preserve IPv6 brackets. Public provider/model data can still be private to an
installation; sanitized does not mean appropriate for publication.

## Readback and concurrency limits

Main preview describes semantic provider/model intent, not an exact future config
diff. Only post-write readback establishes the persisted diff. The public host
write routes do not accept an expected revision, so a pre-call revision check
cannot prevent every race. Claimed success with no write is `host_no_write`.
Changed configs require semantic readback and a foreign-diff guard covering only
paths the host route is known to write. Foreign changes require
`recovery_required` / `external_edit`, never silent success or blind compensation
over another editor's changes. A race that leaves no distinguishable foreign diff
cannot be ruled out by readback alone.

Abandoned `awaiting_host` steps are reconciled against the store during recovery
and restart. Undo/compensation restores snapshots through the public backend
writer, guarded against the recorded post-write revision; a conflict refuses
automatic restoration. A host HTTP response is not evidence of operation commit.

## Historical findings

These resolutions were inspected in the earlier audit/follow-up. Their previous
passing checks are historical only; rerun relevant regressions on the integrated
0.3.0 candidate.

| Severity | Finding | Resolution |
| --- | --- | --- |
| High | A clean working copy could conceal a staged secret. | Scan index blobs independently, including tracked files deleted only from the working tree; ignore staged deletions. |
| High | README content bypassed credential checks. | Remove the README exemption; reject synthetic canaries without printing their values. |
| Medium | Unreadable files or failed Git blob reads passed as empty content. | Report redacted read errors and exit 2. |
| High | Ignored chmod failures could expose recovery snapshots. | Enforce private directory/database permissions before SQLite connection/schema writes; check symlinks immediately before chmod. |
| Medium | Harness refusal checks depended on the runner's temporary directory. | Redirect native HOME to an explicit sandbox and retain the real reference-source path. |
| Medium | Malformed URLs could echo credentials or raise on invalid ports. | Use redacted URL previews and handle parsing failures without leaking the input. |

The scanner reports rule names and locations, never matched values. Its source
and synthetic privacy fixture exemptions are narrow maintenance boundaries;
structural config checks skip test fixtures. Never store real credentials in
fixtures. Heuristics do not detect every credential format, personal identifier,
image or binary payload. Screenshot privacy needs visual review with synthetic
data.

## Historical repository inspection

The earlier reachable-blob inspection found no pattern matches within the guard's
coverage. That is not proof that all historical data is public. The subsequent
remote identity rewrite covered reachable branches and tags of this repository,
not full erasure: GitHub pull-request refs/caches may retain old metadata until
purged by GitHub support, and pre-rewrite forks, mirrors and local clones remain
outside the project's control. This update does not repeat or broaden that audit.

## Verification required for 0.3.0

Run independently of Hermes:

```sh
python -m unittest discover -s tests -p 'test_privacy*standalone.py' -v
python scripts/privacy_guard.py
```

Use the tested Hermes reference **v0.22.2+canary.20261020T065957Z**, commit
`c5a3c0f8767a72203363d249add14142dbcf3cc7`, for host integration. Capability probes,
not that version label or an install stamp, gate writability.

Final acceptance must include Linux mode checks, unsafe-state refusal without
fallback, existing-journal migration, public DTO/token redaction, real host-step
readback/race recovery and guarded Undo. Run the private-import acceptance check
and require no findings in shipped plugin code. See
[release readiness](release-readiness.md) for the full candidate gate. No new
suite counts, privacy-scan result or live deployment claim is made by this update.
