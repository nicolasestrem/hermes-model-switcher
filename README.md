# hermes-model-switcher

A Hermes Web Dashboard plugin for reviewed, one-click application of
provider/model presets across profiles — including every auxiliary, delegate,
judge, fallback, MoA, automation, plugin and specialist model-selection slot.

MIT licensed. No Hermes fork, no credential copying, no direct config writing
outside the supported in-process Dashboard path.

## Setup

1. Requirements: a running Hermes installation (baseline tested:
   `590af66a58c2b3eb3c5edd1dcfefa09c625ae18a`, v0.21.7+18.g590af66) with the
   Web Dashboard enabled, Python 3.11+, Node.js 18+ (build only).
2. Copy or clone this repository into your Hermes plugins directory, or point
   the Dashboard's plugin discovery at it.
3. Build the UI bundle (the Dashboard loads the prebuilt asset — Node is not
   needed at runtime):

   ```
   npm install
   npm run build      # produces ui/dist/plugin.js
   ```

4. Enable `hermes-model-switcher` in the Dashboard plugin settings and reload.
   The backend mounts under `/api/plugins/hermes-model-switcher/`.
5. Python-side tests (no Hermes writes; the registry scans a Hermes checkout
   read-only):

   ```
   python -m pytest tests/
   ```

Environment: `HERMES_MODEL_SWITCHER_HERMES_REPO` overrides the Hermes checkout
used for registry scans (default `/home/hermes/.hermes/hermes-agent`).

## Limitations

- **In-process Dashboard writes only.** Hosted-isolation mode is read-only
  until a compatible coordinated writer is proven; the plugin never changes
  your isolation setting automatically.
- **Journalled application with compensation, not fleet-wide atomicity.**
  Same-process Dashboard writes share Hermes locks; external CLI/editor
  writers do not. An exclusive-writer requirement applies during Apply/Undo.
- **Configured ≠ observed.** The plugin reports what was persisted and read
  back. Existing chat sessions keep their old route until they start anew;
  live switching of in-flight sessions is out of scope.
- **Unknown slots block completeness.** Selectors not in the versioned
  registry appear as visible coverage gaps and never gain write access
  automatically. Plugin authors may register typed descriptors.
- **Protected by default:** identity, skills, memories, transcripts, auth
  stores, schedules/delivery, judge thresholds, compression thresholds and
  any slot not explicitly selected. Code constants (e.g. OMH shipped model
  chains) are immutable and never patched.
- Historical usage, transcripts and `.env`/auth stores are never rewritten.

## Recovery

- Every apply operation journals an operation ID plus a private per-store
  snapshot (outside the repository and outside browser-served assets) before
  the first write.
- On failure the rollout stops and completed writes are compensated in
  reverse order, only when the current revision still matches the recorded
  post-write state. A conflict becomes `recovery_required` — never a forced
  restore.
- After a crash or timeout, retry/undo first reconciles persisted intent
  against the actual files; ambiguous commit state is reported, not guessed.
- Repeating an operation ID returns the existing operation instead of
  duplicating writes.
- Undo is an explicit, revision-guarded action. Removing or disabling the
  plugin does not silently undo already-applied model choices; use the
  operation drawer's guarded Undo or restore from the private snapshots.

## License

MIT — see [LICENSE](LICENSE).
