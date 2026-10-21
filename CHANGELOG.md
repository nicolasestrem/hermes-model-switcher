# Changelog

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

### Verification

Against Hermes `c5a3c0f8`: 743 Python tests and 185 browser tests (mock fixture
and real backend with Hermes' own `/api/model/*` routers) pass with nothing
skipped; `hermes plugins validate` passes with no private-core-import warning.

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
