# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Hermes Web Dashboard plugin: reviewed, one-click application of provider/model presets across Hermes profiles, covering every model-selection slot (main, delegation, auxiliary, judge, fallback, MoA, cron, plugin, specialist). Current release: 0.2.0. There is **no inference-probe endpoint** — the plugin never performs inference and never fabricates usage rows.

## Commands

```
python -m pytest tests/                        # backend suite (pytest.ini: testpaths=tests, -q)
python -m pytest tests/test_phase_b.py::name   # single test
npm install && npm run build                   # ui/build.mjs (esbuild) -> ui/dist/plugin.js + dashboard/dist/
npm test                                       # Playwright (tests/playwright.config.mjs)
python scripts/build_package.py                # assemble dist-package/ (needs built UI)
bash scripts/install-local.sh <lab-home>       # install into a LAB Hermes home; refuses the live home
bash scripts/uninstall-local.sh <lab-home>
bash scripts/install_hooks.sh                  # pre-commit/pre-push privacy_guard.py hooks
```

CI lives in `.github/workflows/ci.yml` (on `push`/`pull_request`, `contents: read`): a `backend` job (pytest), a `browser` job (`npm test`), and a `package` job (the declared Python floor, build, privacy guard). The local suites and the privacy gate remain the fast path.

**Both test suites need a real Hermes checkout on `sys.path`.** Set `HERMES_MODEL_SWITCHER_HERMES_REPO` (default `~/.hermes/hermes-agent`); without it every pytest test errors at set-up, because `tests/conftest.py`'s autouse fixture imports `agent.secret_scope`, and the browser suite's `tests/real_app.py` imports `hermes_constants`. CI clones the pinned tested revision (see CONTRIBUTING.md) rather than skipping.

Playwright starts two servers: a mock fixture (`server.mjs`, :8931) and the real FastAPI backend over a throwaway lab home (`real-server.mjs`, :8932), which seeds `tests/real-home.json` (git-ignored). `tests/visual-evidence.spec.mjs` writes under `TMPDIR`.
`HERMES_MODEL_SWITCHER_HERMES_REPO` overrides the Hermes checkout scanned for the registry (default `~/.hermes/hermes-agent`).

## Architecture

Request flow: `dashboard/plugin_api.py` (FastAPI routes: `/state`, `/preview`, `/presets`, `/operations`, `/operations/{id}/undo`) -> `src/model_switcher/engine.py` -> per-store adapters in `adapters.py` -> files in a Hermes home.

- **Root-level modules**: `hermes_adapter.py` is the ONLY place for version-sensitive Hermes imports; it resolves homes/profiles via Hermes APIs, scopes credentials per profile, and makes unsupported Hermes versions (below baseline v0.21.7) read-only. `route_inventory.py` builds the typed slot inventory and deterministic previews (protocol rules: judge slots reject plain chat models, `cron.model` pairs with `cron.model_provider` not `cron.provider`, fallback order and null-vs-[] preserved, invalid MoA removals rejected). The former root `plugin_api.py` was an orphan Phase-A surface and is gone; the mounted router is `dashboard/plugin_api.py`.
- **`src/model_switcher/`**: `engine.py` (journalled apply: journal intent + snapshots first, sequential per-profile revision-check/apply/readback, reverse-order compensation only if revision still matches, else `recovery_required`; repeated op IDs are idempotent; undo is revision-guarded; operation results report `ok=false` with a fixed failure category and `final_state` for compensated/partial/recovery_required — never arbitrary exception text), `adapters.py` (revision = hash of normalized bytes; no adapter => operation blocks), `journal.py` (private SQLite, WAL with `synchronous=FULL` and `busy_timeout`; per-request connections with no pooling because connections are thread-owned), `presets.py` (a corrupted store is quarantined with the original kept, valid entries are salvaged and the repaired set is persisted on read, surfaced under a `presetsCorrupt` warning; a failed rename raises rather than silently succeeding), `registry/` (versioned source-to-slot coverage registry; unknown slots show as coverage gaps and never get write access).
- **`ui/`**: React bundle built to an IIFE using host React via SDK shims; Dashboard loads the prebuilt `dist/index.js`.
- Packaging follows the official dashboard-plugin layout (`dashboard/manifest.json` + `dist/index.js` + `plugin_api.py`; Python modules under `python/`).

## Invariants to preserve

- Writes only in-process via the Dashboard path; hosted-isolation mode stays read-only. Never write config outside adapters; never textually replace YAML.
- Every write target must be a discovered profile's config; refuse anything else before writing.
- Public DTOs (`to_public_operation`/`PublicOperationView`) are allowlists: never expose raw configs, paths, snapshots, payloads, secrets or exception strings.
- The journal DB refuses to live inside any git worktree, symlinked paths, and is created 0600 in a 0700 private parent directory; the SQLite `-wal` and `-shm` sidecars share that same 0600-in-0700 invariant. Snapshots never leave the journal.
- Tests use lab homes and fake credentials only; never touch the live Hermes home or make inference calls, and never fabricate usage rows. There is no inference-probe route to call.
- `scripts/privacy_guard.py` rejects staged/tracked files containing Hermes configs, snapshots or credentials.
