# Changelog

## Unreleased

### Documentation

- Accuracy corrections: the technical reference no longer states that final
  0.3.2 verification is recorded in release-readiness.md (that page holds the
  0.3.0/0.3.1 evidence trail only); backlog.md reports the entry's current
  review status; verification.md extends the public-seams
  statement to every release from 0.3.0 onward; CLAUDE.md attributes preset
  quarantine, salvage and `presetsCorrupt` to the plugin API's preset loader in
  `dashboard/plugin_api.py`, not `src/model_switcher/presets.py`; the README and
  image-README captions now state the preview shows semantic provider/model
  intent and that the exact persisted config diff appears only after Apply
  readback.
- catalog-submission.md now carries the real submitted 0.3.2 entry re-pinned in
  NousResearch/hermes-agent#141744 (awaiting maintainer re-review), with `<sha>`
  placeholders for the pinned commit and screenshot URLs.

## 0.3.2

### Catalog captures re-taken

- The six listing captures are re-taken on the fixed UI and renamed
  `model-switcher-0.3.2-*.png`; the 0.3.0 catalog capture showed every model
  id twice (the bug fixed below). The packaged image list, README, image
  README and catalog-submission template point at the new files.

### Catalog rows no longer show the model id twice in a solid bar

- Real provider catalogs (Nous Portal, OpenRouter-style) send `label === id`,
  so every row rendered the id twice; the second copy is a `<code>`, which the
  host dashboard's bare `code { background: var(--selection-bg); color:
  var(--background) }` chip rule turned into a full-width bar (unreadable in
  some themes). The id line now renders only when it adds information, and
  `.hms-root code` neutralises the host chip.
- Why it escaped: fixtures hand-copied part of the host preflight and the mock
  always sent distinct labels. New `tests/host-css-guard.spec.mjs` renders the
  plugin under the REAL host stylesheet from the pinned Hermes checkout, with
  `catalogIdLabels` mock data, plus a screenshot baseline.
- `npm run check:live` (`scripts/live-catalog-check.mjs`) runs the same checks
  against a running dashboard in Chromium and Firefox after install/update;
  `npm run check:design` runs the Impeccable detector over `ui/src/`.

### Host 503 "Restart required" is now named and actionable

- When the running Hermes dashboard process is older than the checkout on disk,
  upstream refuses `/api/model/options` (and `/api/model/set`) with HTTP 503
  and a fixed `Restart required: …` detail (upstream
  `hermes_cli/web_routers/models.py` `_dashboard_code_skew_guard`, introduced by
  commit `f293e7206b`, 2026-08-14, #86207 — the guard predates the plugin's
  Hermes v0.22.3 reference pin, so any pinned host can emit it after a
  `hermes update`). Until now the tab only said
  `Host data unavailable (options — HTTP 503)` and
  `hostCatalogUnavailable`, leaving no hint that a dashboard restart fixes it.
- The UI (ui/src) classifies that host failure into ONE fixed category,
  `hostRestartRequired`, by matching the host detail's fixed prefix — the
  host's own sentence is never displayed, logged, stored, or forwarded. The
  host notice becomes a fixed actionable sentence: "Hermes was updated on disk
  but this dashboard is still running old code. Restart the dashboard
  (`hermes dashboard --port <port>`, or your service restart), then Refresh
  state."
- The backend DTO path carries the category too: `POST /catalog` and
  `POST /models` accept an `optionsFailure` field with the closed vocabulary
  `{"hostRestartRequired"}` (only accepted when no options payload is present;
  anything else is ignored, so arbitrary values are never echoed back), and
  `dashboard/model_catalog.py`'s transform reports it as the DTO `error` token
  instead of `hostCatalogUnavailable`. The UI maps that token back to the same
  fixed sentence. DTO keys are unchanged.
- New tests: `tests/test_host_restart_required.py` (pytest: transform rules,
  route sanitization, no host-text echo) and `tests/restart-required.spec.mjs`
  (browser suite: classification, fixed sentences, privacy).

## 0.3.1

### Upstream reference re-pin (Hermes v0.22.3)

- Re-pin the tested Hermes reference from `v0.22.2+canary.20261020T065957Z`
  (`c5a3c0f8`) to the stable **v0.22.3** release, commit
  `5b147ab036d6d19131bff8d20701b5001eab844a`. Upstream moved to
  `NousResearch/hermes-agent` (the old `NousResearch/hermes` name is gone) and
  shipped two releases past the old pin. No seam the plugin uses changed in a
  breaking way: `hermes_cli.config`, `set_hermes_home_override`, the
  `/api/model/*` routers and their config writer, `plugin_validate_private_imports`
  and the Dashboard mounting path are byte-identical between the pins;
  `hermes_constants` and `agent.secret_scope` gained fail-closed additions only.
  The pin stays a reproducibility reference, not a minimum version.
- The three upstream seam gaps documented in 0.3.0
  ([#142339](https://github.com/NousResearch/hermes-agent/issues/142339),
  [#142340](https://github.com/NousResearch/hermes-agent/issues/142340),
  [#142341](https://github.com/NousResearch/hermes-agent/issues/142341))
  are still open upstream; the documented limits stand unchanged.

### Packaging

- Bound the runtime PyYAML dependency to `>=6.0,<7`, replacing the bare `>=6`
  floor, per the plugin-catalog dependency rule (bare floors receive a change
  request).

### Test harness

- The real host-routes pytest fixture now seeds through `real_app.py --seed`
  with a pytest-private `--home-file` beside its throwaway lab home instead of
  the CLI's default, the shared `tests/real-home.json`. The backend suite
  deletes its lab home in teardown, so the previous default left a dangling
  pointer in the shared file that made three browser specs fail with ENOENT
  when both suites ran in the same tree; the shared file is now written only
  by the browser harness, which seeds it fresh at the start of every run.

### Verification

- Against Hermes v0.22.3 (`5b147ab0`), the release commit passes 777 Python
  tests and 209 browser tests locally (Python 3.12.3, Node 22; backend suite
  then browser suite in the same tree — exactly the previously failing order),
  with the private-import gate returning `[]`, the privacy guard clean, the
  package build assembling, and the committed dashboard bundle matching a
  fresh build. The backend suite also passes against upstream main at
  `3427ff9fd2` (post-v0.22.4-rc) as a forward-compat signal.

## 0.3.0

### Public Hermes integration

- Use public seams only. The UI obtains catalog, authentication and pricing data
  from SDK-authenticated `GET /api/model/options`, profile-specific plugin tasks
  from `/api/model/auxiliary`, and MoA presets from `/api/model/moa`. Catalog code
  only transforms supplied host data; it no longer reads provider caches or
  models.dev files directly.
- Main and compatible auxiliary assignments use public `POST /api/model/set`,
  including the host's separate expensive-model confirmation. Other slots use
  public `hermes_cli.config` load/read-guard/save under the target home's scope.
- Journal host steps as `awaiting_host`; the UI executes an allowlisted call and
  returns a one-time-token result. Readback, not HTTP success, determines whether
  the write committed. Recovery reconciles abandoned steps; Undo/compensation uses
  the revision-guarded public backend writer.
- Replace the version baseline and install-stamp dependency with a capability-based
  runtime check. The tested reference is Hermes **v0.22.2+canary.20261020T065957Z**,
  commit `c5a3c0f8`; it is a reproducibility pin, not a minimum-version floor.
- Remove `plugin_backend_allowed` (the host gates plugin requests) and
  `_profile_env_entries` (credential presence uses public scoped helpers, never
  another profile's environment file).
- Remove `HERMES_MODEL_SWITCHER_STATE_DIR` and the `~/.local/state` fallback.
  State lives only at `<hermes home>/state/hermes-model-switcher/`; a location
  inside the plugin tree or a Git worktree makes the plugin read-only.
- Disclose that the undo journal copies each complete pre-change config, which
  can contain sensitive fields, not just the model selection.

### Accepted limits

- Host write routes have no expected-revision check. Pre-call checks cannot make
  the request atomic; foreign edits detected by readback end in
  `recovery_required`, never silent success or blind overwrite during recovery.
  Readback cannot prove every possible race was observable; avoid concurrent edits.
- Main-slot preview shows provider/model intent. The exact persisted config diff
  is available only after host readback, not predicted with private host helpers.
- Backend-slot saves send changed leaves through `save_config(merge_existing=True)`,
  so concurrent edits to unrelated keys survive. A same-key edit landing between
  the revision check and Hermes' own lock can still be overwritten undetected, and
  deletions need a full-state save. Closing this needs an upstream compare-and-save seam
  ([NousResearch/hermes-agent#142339](https://github.com/NousResearch/hermes-agent/issues/142339)).
  Non-main slots lack a public typed write route
  ([#142340](https://github.com/NousResearch/hermes-agent/issues/142340)), and main has no
  dry-run footprint for exact previews
  ([#142341](https://github.com/NousResearch/hermes-agent/issues/142341)).
- When a host save normalizes legacy raw config aliases, Undo cannot recreate them
  exactly and stops at `recovery_required` instead of claiming a restore.
- A config holding a value the JSON journal cannot restore exactly (for example an
  unquoted YAML date) is refused before anything is written, with a plain reason.
- A typed main model id that is not in the host catalog may be saved by Hermes
  under a different spelling. The preview warns about this; if it happens, the
  operation stops at `recovery_required` and the host's write is left in place.

### Release hardening

- Preset preflight refusals name routes by profile and slot only, never by their
  values, so inline credentials or credential-bearing endpoints in a stored route
  cannot reach a 409 response. `presets.json` is written `0600`, and an existing
  wider file is tightened on load.
- Auxiliary tasks that Hermes' assignment route does not accept are written through
  the public backend writer instead of a host call that would be refused.
- Main assignments journal the host's canonical provider id (aliases and case
  resolved from the supplied catalog), so a correct host write is not mistaken for
  a foreign edit; an unresolvable provider is refused with a fixed reason.
- Recovery resumes a compensation interrupted by a crash when the store still
  holds our recorded write; one unreadable store fails only its own operation
  instead of every operation route; a step whose write phase is ambiguous is never
  reported as a known no-write.
- The local installer checks every load-bearing package file, no longer creates a
  misleading in-plugin `state/` directory, and prints the enable step; uninstall
  reports where state really lives. PyYAML is declared as a runtime dependency,
  build tools moved to `devDependencies`, and CI assembles the package.
- UI: dark-theme checkbox styling, route ids that never break mid-token, muted
  informational notes, correct singular/plural counts, persistent polite live
  regions, and gate reasons linked to the controls they disable.

### Verification

Against Hermes `c5a3c0f8`, the release commit passes 777 Python tests and 209
browser tests locally (Python 3.13; mock fixture and real backend with Hermes' own
`/api/model/*` routers) with nothing skipped; CI repeats them on Python 3.11/3.13
and Node 22/24. The host `private_import_findings` gate returned `[]` against
the reference checkout. A locally run `hermes plugins validate` (installed CLI,
over the repository tree and an assembled package) exited 0 in the pre-release
packaging audit; that is a locally observed result, not an admission receipt,
and no CI workflow runs the validator.

### Model catalog and native dropdowns

- Add a collapsible, provider-grouped catalog with per-1M-token list prices,
  free/included/unknown badges, "Configured in" slots and preview-gated "Use".
  Missing or pending pricing stays unknown, never free or `$0.00`; prices remain
  informational and plugin billing approval is unchanged.
- Provider/model editors and matrix filters use native `<select>` controls.
  Authenticated providers come first; an explicit "Other" entry keeps custom
  endpoints and exact model IDs available. Remove the hand-rolled ARIA combobox.
- Plugin `POST /catalog` and `POST /models` transform the supplied host options.
  Their old GET variants return 410 `hostCatalogRequired`; missing/malformed host
  options return a fixed unavailable result, not empty success. Refresh goes
  through the public host options route.

### Security

- Fix `py/stack-trace-exposure` in the preview endpoint: a removal that cannot
  bind returns a fixed class-level reason, and a failing route seam reports its
  exception type only. Exception text must never reach a response.
- Fix `actions/unpinned-tag` in `claude.yml`: pin `anthropics/claude-code-action`
  to the `v1` commit instead of the moving tag.

## 0.2.0 — 2026-10-14

### Removed

- The `/probe` HTTP route is gone. It previously always refused and never performed an inference probe, so removing it loses no supported inference capability.

### Catalog/publication pass

- Add catalog-grade `plugin.yaml` metadata (author, MIT license, homepage, tags) following the supported manifest schema; set version `0.2.0` consistently across `plugin.yaml`, `dashboard/manifest.json`, `pyproject.toml`, `package.json`, and `package-lock.json`.
- Rewrite the README around what/why, the official `hermes plugins install`/`update` commands, a feature overview, an FAQ, and explicit limits (configured ≠ live). Add `CONTRIBUTING.md` with real dependency/runtime setup and the portable vs real-runtime test split.
- Add `.github/workflows/ci.yml` (`push`/`pull_request`, `contents: read`) running pytest, the browser suite, and the declared Python-floor package build against the pinned tested Hermes revision; document that both suites require a real Hermes checkout and are not green when it is absent.
- Record catalog-submission readiness (ready-for-submission, not submitted) and explain why the installer-generated `.hermes-catalog.json` is intentionally not committed. Note the remaining catalog rule 9 private-import warning.
- Describe the removed `/probe` route honestly: there is no inference-probe capability.
- Add `docs/backlog.md`; scope the privacy audit's history statement around the remote-identity cleanup (no full-erasure claim).
- Reflect the backend review changes in the docs: no fleet-wide transaction or always-undo promise; compensated/partial/recovery_required results report `ok=false`; preset corruption quarantines the original and salvages valid entries with a `presetsCorrupt` warning.

## Pre-0.2.0 notes

- Rewrite the README around profile selection, reviewed model changes, and reusable presets.
- Separate advanced routing and Dashboard integration details into a technical reference.
- Clarify community status, platform verification boundaries, credential handling, and guarded recovery.
- Include documentation and the design contract in the assembled plugin package.
- Validate package inputs before replacement and refuse unsafe output paths; require `--replace` for an existing package directory.
- Correct lab helper live-home checks, reject redirected plugin destinations, and preserve prior installations/recovery data during explicit replacement or uninstall.
- Scan README and staged Git blobs, fail closed on unreadable files, require private journal permissions before SQLite opens, and sanitize malformed public URLs.
- Refine Dashboard hierarchy, spacing, mobile scrolling, empty states, and accessible selection feedback; include synthetic-data screenshots.
- Fix Windows file-URL handling in build/fixture scripts and make isolation tests independent of the host temporary directory.
- Verify 402 Python and 69 browser tests against the recorded Linux Hermes revision; document private-API compatibility warnings and measured inventory scaling.
- Address PR #10: block recursive package destinations, preserve URL review visibility, scan deleted files from Git index, order journal symlink checks before chmod, and move lab backups outside plugin discovery with flexible --replace parsing.
- Package documentation only from an explicit approved-file manifest; exclude untracked and ignored local notes, exports, and raw captures.
- Reverify the latest PR #10 candidate on Linux: 413 tests pass, including the explicit documentation manifest and symlink cases.
