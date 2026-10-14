# Backlog

Deferred optimizations and known limitations, recorded so they are not rediscovered.
Nothing here contains private host data.

## Performance

- **Cold discovery at scale.** The first state request for 50 synthetic profiles took
  about seven seconds in the lab; subsequent requests were ~1 s. No cache was added,
  because invalidation would have to preserve per-profile and per-runtime freshness.
  Revisit only with a sound invalidation design.
- **Frontend rendering** was measured (2,000 synthetic models render in ~21 ms; 1,000
  slots in ~275 ms) and did not justify an optimization at this sample size.

## Compatibility

- **Private Hermes seams.** `hermes plugins validate` warns under catalog rule 9 that
  the integration imports private core modules (model catalog, profile scope, canonical
  writer, and others). Validation passes, but these can break on later Hermes releases.
  Migrate to supported public equivalents when they exist; do not add new private
  imports casually.
- **Platforms.** Linux is the verification target. macOS and native Windows are
  unverified, and the lab shell helpers are GNU/Linux-only.

## Coverage

- **Slot coverage gaps.** Unknown model-like keys and unsupported plugin selectors stay
  read-only and never receive write access. Extend the versioned registry descriptors as
  Hermes adds surfaces.
- **Retried pre-write failures overwrite their failure category.** Retrying an operation
  that failed at the pre-write revision recheck through the engine's `apply_operation`
  directly records `write_verification_failed` over the accurate `revision_conflict`
  category (`src/model_switcher/engine.py`, the failure category is set from the last
  attempt). Not reachable through the Dashboard API, which always plans a fresh operation;
  relevant only to direct engine callers.

## Build and packaging

- **Committed bundle drift.** `dashboard/dist/*` is committed so a plain `git` install
  has a loadable bundle; keep it rebuilt (via `npm run build`) whenever `ui/src`
  changes so the committed assets match the source.

## Distribution

- **Catalog submission.** Readiness, entry template, and the exact submission procedure
  live in [catalog-submission.md](catalog-submission.md). Status: ready-for-submission,
  not submitted.
