# Backlog

Deferred optimizations and known limitations, recorded so they are not rediscovered.
Nothing here contains private host data.

## Performance

Measurements below are pre-0.3.0 lab observations recorded before the
public-seams refactor; they are not carried forward as 0.3.0 acceptance (see
[release readiness](release-readiness.md)).

- **Cold discovery at scale.** The first state request for 50 synthetic profiles took
  about seven seconds in the lab; subsequent requests were ~1 s. No cache was added,
  because invalidation would have to preserve per-profile and per-runtime freshness.
  Revisit only with a sound invalidation design.
- **Frontend rendering** was measured (2,000 synthetic models render in ~21 ms; 1,000
  slots in ~275 ms) and did not justify an optimization at this sample size.

## Compatibility

- **Public host contract drift.** Version 0.3.0 uses public modules and authenticated
  `/api/model/*` routes. Keep the private-import gate clean and reverify payload and
  host-write semantics when Hermes changes. Missing public capabilities make the
  plugin read-only; missing catalog evidence stays unknown. The old private-seam
  migration item is resolved by this refactor, not retained as an exemption.
- **Host-write concurrency.** The public assignment route has no expected-revision
  parameter. Readback detects foreign edits and requires recovery rather than blind
  rollback; it is not an atomic compare-and-swap. Avoid concurrent config writers.
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

## Deferred UI

- **Context windows and capability chips** in catalog rows (reasoning/tools/vision):
  the frozen catalog DTO carries prices only; extending it needs a trustworthy
  per-model metadata source first.
- **Fallback-chain chip editor and matrix row menu**: compact affordances sketched
  during the dropdown work and deliberately left out; the chain textarea and the
  preview-gated editor remain the single write path.
- **Price-aware apply policy** (e.g. max $/Mtok per route): catalog prices stay
  informational; the billing gate keeps approving on cost class only.

## Build and packaging

- **Committed bundle drift.** `dashboard/dist/*` is committed so a plain `git` install
  has a loadable bundle; keep it rebuilt (via `npm run build`) whenever `ui/src`
  changes so the committed assets match the source.

## Distribution

- **Catalog submission.** Readiness, the submitted entry, and the exact submission
  procedure live in [catalog-submission.md](catalog-submission.md). The entry in
  [NousResearch/hermes-agent#141744](https://github.com/NousResearch/hermes-agent/pull/141744)
  is re-pinned to 0.3.2 and awaits maintainer re-review.
