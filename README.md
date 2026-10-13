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

3b. Frontend host contract — the Dashboard entry is a plain IIFE that hands a
   React component to the host registry, exactly as documented in Hermes'
   *Extending the Dashboard* guide:

   - React comes from `window.__HERMES_PLUGIN_SDK__.React`. React is never
     bundled, and **the Dashboard does not set `globalThis.React` /
     `globalThis.ReactDOM`** — a bundle that destructures them dies on load with
     `Cannot destructure property 'useState' of 'q' as it is undefined`.
   - The entry registers synchronously and must not mount itself:
     `window.__HERMES_PLUGINS__.register("hermes-model-switcher", App)`. The
     registered name must equal `dashboard/manifest.json`'s `name`, and the host
     renders the component inside the tab. There is no ReactDOM on the SDK and no
     host-provided root element.
   - Control styling is plugin-owned: the manifest declares `css: "dist/style.css"`,
     built from `ui/src/styles.css` and shipped beside the JS in both checkout and
     assembled package. Tailwind's host reset removes native button/input/select
     chrome; an unstyled control in the DOM is not a usable action. Scoped styles
     supply borders, padding, focus rings and disabled feedback without changing
     other Dashboard tabs. Fixtures load this same asset under the host reset,
     rather than supplying their own control styles.
   - Theme colours follow the Dashboard's background/midground tokens, with legacy
     aliases supported. Sticky headers reject unsafe alpha values and use an opaque
     theme background instead of a universally dark fallback in light themes.
   - API calls use `window.__HERMES_PLUGIN_SDK__.authedFetch` so the host's
     authentication and profile scope are preserved. Bare `fetch` is only a
     standalone/fixture fallback, not the Dashboard transport.
   - `ui/dist/plugin.js` (the standalone layout) keeps a self-mount fallback for
     hosts that provide `globalThis.React`/`ReactDOM` instead — that is what the
     local Playwright fixture uses.

   Both contracts are covered by `npm test`: `tests/fixture-sdk.html` reproduces
   the real shell (SDK + registry globals, no `globalThis.React`) and loads the
   shipped `dashboard/dist/index.js`; `tests/fixture.html` covers the legacy path.

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

## Using the tab

1. Narrow the matrix to one or several searchable profiles, then choose a family,
   exact subfamily/slot and state. **All profiles** resets the profile scope.
   Select the slot checkboxes in the matrix, then enter the new provider
   and model in the change editor above it. The matrix scrolls internally with
   sticky headers; it does not push the editor below every row.
2. Click **Preview changes** and review the affected routes and side effects.
   Acknowledge unknown-cost routes when required before applying a custom change.
   Editing the route, selection or filters invalidates the review; a late preview
   response cannot revive it. The server also binds reviews to profile identities
   and store revisions, with explicit expiration/stale errors.
   A preview that contained blocked changes is **all-or-nothing**: it cannot be
   applied partially — fix the blocked entries and re-preview. Apply (direct or
   from a cached preview) re-checks every referenced provider's credentials for
   the target profile before any write.
3. Click **Apply** to apply the reviewed change, or enter a preset name and click
   **Save as preset** for reuse. Favorite presets appear above the editor; an empty
   list shows guidance rather than an unexplained blank section.
4. Clicking a favorite preset reviews/previews it; **Apply preset** performs the
   reviewed application. Check the operation drawer's per-profile readback result.
   Undo remains an explicit, guarded recovery action.

Use **Model picker** to search Hermes' provider-scoped catalog, keeping the exact
model ID alongside its provider. IDs are not portable across providers: Nous may
use `deepseek/deepseek-v4.1-flash` while OpenCode Go uses `deepseek-v4.1-flash`.
Refresh requests live discovery, but failed probes and curated/cache fallbacks
remain visibly unproven-fresh. Manual entry stays available for custom routes.

Setting a route on a fallback slot produces an explicit ordered one-entry chain;
the chain editor supports several `provider/model` lines. **Unset** restores
inheritance and **Disable** writes an explicit empty chain. These actions are
distinct, and a malformed/missing chain is rejected instead of silently cleared.
Speech routes bind the selected backend and its native model field together;
MoA preset rows address their aggregator, preserving reference models and policy.

Apply and Undo refresh configured state automatically; **Refresh state** reloads
external edits. **Last observed** stays **unobserved** until actual runtime usage
evidence is available—it is never fabricated from configuration. Saved presets
retain their exact reviewed slots, profiles and billing approval; an apply request
cannot silently broaden that saved policy.

Status and failures are shown beside the action controls as well as announced
through the screen-reader status region. A refused apply preserves the server's
reason, including FastAPI's nested `detail.error` and `detail.reasons`, rather
than displaying only `HTTP 409`. A missing provider connection is a real refusal,
not a successful model switch; connect that provider through Hermes or choose a
connected route. A state-loading authentication failure is separate from a layout
problem: the tab must use the host SDK transport even if the bundle renders.

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
- **Identity-guarded writes.** Apply and Undo refuse when a target profile
  directory was recreated, moved, or restored from a backup after the review:
  its directory identity changed, so the reviewed write is no longer provably
  the same target. The refusal names the profile; re-review the change. This is
  deliberate fail-closed behaviour, not a cached-state error.
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

Verified locally through Hermes (local build/tests and the privacy gate are the
publication checks; no hosted CI/CD was added for this fix):

- Full backend pytest suite (97 tests) incl. real dashboard-plugin discovery
  and module load of the INSTALLED package in a throwaway Hermes home, and an
  install → uninstall round-trip that refuses the live home (fail-closed).
- Playwright suite (17 tests) covers the SDK registration/auth transport contract,
  visible refusal messages and their explicit error severity, header-opacity
  under a transparent host surface token, the error-unwrapping rules (flat
  body, string `reasons`, string `detail`, absent body), and the real FastAPI
  backend (uvicorn) over a
  throwaway lab home: real preview/apply with on-disk config verification and
  guarded undo restoring the exact original files; fake credentials only,
  real file saves, no inference calls; every plugin-api response is asserted
  free of keys, host paths, snapshots and payloads.
- Independent Chromium before/after check against a real Dashboard in an
  isolated lab home: at 1280×577 with 34 matrix rows and scroll position zero,
  **Preview changes** moved from y=1335–1358 to y=417–441. The editor now precedes
  the matrix; the matrix has a 317px scroll viewport for 880px of content.
  Both renders had no console errors. Desktop and narrow mobile lab renders
  were also inspected; a real missing-provider refusal was visible on screen.
  These are lab observations, not evidence of deployment to a live installation.

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
