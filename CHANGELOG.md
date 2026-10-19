# Changelog

## Unreleased

### Native route dropdowns

- Replace the provider/model comboboxes with native `<select>` dropdowns. Provider
  entries read "Label (slug)", authenticated providers first; providers needing
  sign-in are listed but disabled. Model entries show the id, name and catalog
  price per 1M tokens; a missing price reads "price not published", never free.
- An explicit "Other — type an exact…" entry reveals an exact-id field, so custom
  endpoints and uncatalogued routes remain first-class.
- Model rows come from the cached `/catalog`; `GET /models` is used only for a
  provider the catalog does not list. The catalog still loads lazily, on first
  hover/focus of the provider dropdown.
- Matrix provider/model filters are native dropdowns of values present in the
  matrix, with exact matching; the model filter narrows to the chosen provider.
- The editor's per-provider freshness list collapses into "Catalog details" under
  a one-line summary (providers · models · stale · scope) with Refresh.
- Remove the hand-rolled ARIA combobox and its styles; browser specs drive the
  selects through `tests/route-helpers.mjs`.

### Model catalog and dropdowns

- Add a read-only `GET /catalog` route aggregating every provider's models with
  per-1M-token list prices (picker pricing caches, then the official-pricing
  snapshot, then the local models.dev registry), behind an import-probed seam in
  `hermes_adapter`; seam drift degrades to "unknown price" instead of failing.
  `/models` is unchanged.
- Replace blind free-text provider/model entry with searchable ARIA comboboxes at
  the point of editing and in the matrix filters; exact-ID typing stays
  first-class for custom routes.
- Add a collapsible bird's-eye model catalog: provider-grouped table with
  input/output prices, free/included/unknown badges, a "Configured in" slot
  cross-reference, a summary strip, and a preview-gated "Use" prefill.
- Cost honesty throughout: unknown is never rendered as free or `$0.00`, no price
  timestamps are invented, and the catalog stays informational — billing approval
  is unchanged and re-derived server-side.
- New backend tests (catalog pricing and catalog API) and browser specs
  (`dropdowns.spec.mjs`, `models-catalog.spec.mjs`, `real-catalog.spec.mjs`).

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

## Earlier unreleased notes

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
