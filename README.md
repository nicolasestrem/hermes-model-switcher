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
2. Install it into your Hermes plugins directory — either copy/clone this
   repository there, or copy the assembled package
   (`python scripts/build_package.py` → `dist-package/hermes-model-switcher/`).
   Either layout must leave `plugin.yaml` beside a root `__init__.py`: a
   manifest with no `__init__.py`, `desktop/plugin.js` or `plugin.json` next to
   it is rejected by `hermes plugins validate` and fails to load.
3. The Dashboard loads the prebuilt bundle, so Node is a build-time dependency
   only. `dashboard/dist/index.js` is committed for that reason — a clone or a
   plain copy installs straight from the repo. Rebuild it after changing the
   UI:

   ```
   npm install
   npm run build      # produces dashboard/dist/index.js and ui/dist/plugin.js
   ```

4. Enable `hermes-model-switcher` in the Dashboard plugin settings and reload.
   The backend mounts under `/api/plugins/hermes-model-switcher/`.
4b. Local (never-live) install of the assembled package into a LAB Hermes
   home for isolated verification:

   ```
   npm run build && python scripts/build_package.py   # assemble dist-package/
   bash scripts/install-local.sh /tmp/your-lab-home   # refuses the live home
   bash scripts/uninstall-local.sh /tmp/your-lab-home
   ```

5. Python-side tests (no Hermes writes; the registry scans a Hermes checkout
   read-only):

   ```
   python -m pytest tests/
   ```

Environment: `HERMES_MODEL_SWITCHER_HERMES_REPO` overrides the Hermes checkout
used for registry scans (default `~/.hermes/hermes-agent`, resolved relative
to the running user's home — never a baked-in path).

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

## Privacy & local data handling

- **The operation journal (SQLite) is private by construction.** It refuses
  to be created inside ANY git worktree (including git-ignored directories —
  ignore rules alone give no guarantee), refuses symlinked DB paths or
  symlinked parent directories, and is created with `0600` permissions in a
  `0700` parent directory.
- **Recovery snapshots stay local.** Private per-store snapshots (the only
  source for exact undo) live solely in the private journal DB — they are
  never exported, logged, or returned by the public API.
- **Public operations use a safe DTO.** `to_public_operation` /
  `PublicOperationView` allowlist only operation status and per-step slot
  outcomes (state, profile, store kind, revisions). Raw configs, store
  paths, payloads, changes, snapshots and exception strings never cross the
  boundary.
- **Local privacy gate (no CI/CD).** `scripts/privacy_guard.py` scans
  tracked and staged files by path AND content for full Hermes
  configs/exports/snapshots and credentials, rejecting findings without
  printing values. Install local hooks with:

  ```sh
  bash scripts/install_hooks.sh
  ```

  This installs `pre-commit` (staged files) and `pre-push` (full tree)
  hooks into the repo's git hooks dir. `.gitignore` additionally blocks
  `*.db`, `*.sqlite*`, `journals/`, `snapshots/`, `backups/`, `.env`,
  credentials dumps and session transcripts — but the hooks, not ignore
  rules, are the actual enforcement.

## Verified scope & known gaps

Verified locally (no CI/CD exists in this repo; the local privacy gate and
this suite are the enforcement):

- Full backend pytest suite (96 tests) incl. real dashboard-plugin discovery
  and module load of the INSTALLED package in a throwaway Hermes home, and an
  install → uninstall round-trip that refuses the live home (fail-closed).
- Playwright suite (8 tests) drives the real FastAPI backend (uvicorn) over a
  throwaway lab home: real preview/apply with on-disk config verification and
  guarded undo restoring the exact original files; fake credentials only,
  real file saves, no inference calls; every plugin-api response is asserted
  free of keys, host paths, snapshots and payloads.

Known gaps (not yet verified — do not assume them):

- **No live Hermes runtime probe.** All evidence comes from lab homes; a real
  `session_model_usage` probe (PLAN T7) has never been executed.
- **No CI.** PLAN T8's "CI passes" criterion is satisfied locally only.
- **Family coverage.** The reviewed registry covers main, delegation,
  auxiliary, judge/reviewer, fallback, MoA, cron/automation, specialist
  (STT/TTS/embedding) and two reviewed plugin selectors (jev-judge,
  jev-skill-router) for the baseline Hermes checkout. Slots from OTHER
  installed plugins (e.g. hermes-adaptive-effort's config-schema model
  fields) are NOT reviewed and appear as coverage gaps by design — point
  `HERMES_MODEL_SWITCHER_PLUGINS_DIR` at them to surface the gap.
- **Hosted-isolation mode remains read-only**; coordinated-writer feasibility
  is unproven.

## License

MIT — see [LICENSE](LICENSE).
