# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Hermes Web Dashboard plugin: reviewed application of provider/model presets across Hermes profiles, covering every supported model-selection slot (main, delegation, auxiliary, judge, fallback, MoA, cron, plugin, specialist). Current release: 0.3.2. There is **no inference-probe endpoint** — the plugin never performs inference and never fabricates usage rows.

## Commands

```sh
python -m pytest tests/                        # backend suite (pytest.ini: testpaths=tests, -q)
python -m pytest tests/test_phase_b.py::name    # single test
npm ci && npm run build                        # ui/build.mjs (esbuild) -> ui/dist/plugin.js + dashboard/dist/
npm test                                      # Playwright (tests/playwright.config.mjs)
python scripts/build_package.py                # assemble dist-package/ (needs built UI)
bash scripts/install-local.sh <lab-home>       # install into a LAB Hermes home; refuses the live home
bash scripts/uninstall-local.sh <lab-home>
python scripts/privacy_guard.py                # tracked working-tree files and index blobs
bash scripts/install_hooks.sh                  # optional pre-commit/pre-push privacy hooks
```

CI lives in `.github/workflows/ci.yml` (on `push`/`pull_request`, `contents: read`): `backend`, `browser`, and `package` jobs. The package job checks the declared Python floor and committed bundle freshness. Local suites and the privacy gate remain the fast path.

**Both test suites need a real Hermes checkout on `sys.path`.** Set `HERMES_MODEL_SWITCHER_HERMES_REPO` (default `~/.hermes/hermes-agent`); without it pytest errors at setup and the real browser backend cannot start. CI clones tested commit `5b147ab036d6d19131bff8d20701b5001eab844a` (**v0.22.3**); see CONTRIBUTING.md. A bare checkout is enough: no runtime bootstrap or install stamp is required. `probe_runtime()` checks callable public capabilities; package version is informational, not a write gate.

Playwright starts a mock fixture (`server.mjs`, :8931) and a real FastAPI backend over a throwaway lab home (`real-server.mjs`, :8932). The latter mounts the real Hermes `/api/model/*` routers and seeds `tests/real-home.json` (git-ignored). `tests/visual-evidence.spec.mjs` writes under `TMPDIR`.

## Architecture

- **Public Hermes boundary:** `hermes_adapter.py` contains only public config, home/profile and secret-scope seams. It scopes homes and credentials together and uses public `hermes_cli.config` load/read-guard/save for backend writes. It holds its own lock, checks revisions immediately before saving and verifies readback; it never reaches private host locks/helpers. `route_inventory.py` uses public defaults, a local inheritance mirror and supplied host auxiliary/MoA data. Unknown slots remain coverage gaps.
- **Host reads in `ui/`:** React uses the Dashboard SDK transport to fetch `/api/model/options?profile=...&include_unconfigured=1`, `/api/model/auxiliary?profile=...` and `/api/model/moa?profile=...` for each discovered profile (concurrency at most three). It sends the bounded `host.profiles` bundle with state, preview and operation requests. Catalog refresh adds `refresh=1`; the host owns refresh. Missing host data produces notices and truthful unknown/hidden rows, not empty success. `dashboard/model_catalog.py` only transforms supplied options; no Hermes imports or disk-cache readers.
- **Plugin API:** `dashboard/plugin_api.py` mounts `/state`, `/preview`, `/catalog`, `/models`, `/presets`, `/operations` and operation recovery/undo routes. `GET /state` is config-only; `POST /state` enriches it with host evidence. Catalog/model transforms are POST routes; their old GET forms return 410 `hostCatalogRequired`. Host evidence never authorizes write targets.
- **Host-step protocol:** `src/model_switcher/engine.py` journals intent and each pre-change config snapshot before execution. Steps declare `executor: "host"` or `"backend"`. Main and compatible auxiliary assignments pause at `awaiting_host` with an allowlisted `hostCall` for SDK `POST /api/model/set`. The current step receives a one-time token (only its hash is stored). If Hermes requests expensive-model confirmation, the UI displays the message as text and resends with `confirm_expensive_model: true` only after approval; cancellation reports `declined`. `/operations/{id}/host-result` accepts sequence, token, fixed outcome and optional numeric status, never host response text. Readback, semantic checks and a foreign-diff guard determine success; wrong tokens fail without changing state. Recovery/restart reconciles abandoned host steps. Other slots execute through the public backend writer. Undo/compensation restores snapshots through that same writer, guarded by post-write revisions.
- **Concurrency and preview limits:** the host write route has no expected-revision parameter. A pre-call check is not atomic with the host write; a foreign edit found at readback ends in `recovery_required` / `external_edit`, never silent success or blind restoration. Readback cannot prove every possible race was observable. Main preview shows provider/model intent; the exact persisted config diff appears only after readback.
- **Storage:** `journal.py` uses private SQLite, WAL, `synchronous=FULL`, `busy_timeout` and per-request connections. The plugin API's preset loader in `dashboard/plugin_api.py` quarantines corruption, salvages valid entries and reports `presetsCorrupt`; failed quarantine is not silently ignored. `registry/` holds versioned source-to-slot coverage descriptors.
- **Packaging:** React builds to an IIFE using SDK React; the host mounts the registered component. The official layout is `dashboard/manifest.json`, `dist/index.js`, CSS and `plugin_api.py`, with backend modules under `python/` in assembled packages. Source changes require rebuilt committed `dashboard/dist` assets.

## Invariants to preserve

- Writes only through the in-process Dashboard path; hosted isolation stays read-only. Configuration writes belong to either the selected public host route or backend adapters, never ad-hoc YAML replacement.
- Every write target must be a discovered profile's config; refuse anything else before writing. Host payloads are evidence, not a target allowlist.
- No private Hermes imports or private attribute access in runtime files. Do not hide coupling with dynamic imports. Test-only harnesses may import host internals. Run the private-import gate below against shipped code and require `[]`.
- State lives **only** at `get_hermes_home()/state/hermes-model-switcher/`. If this resolves inside the plugin tree or any Git worktree, or fails privacy checks, the plugin is read-only. No environment override or alternate-directory fallback.
- The journal copies each **complete pre-change config**, which may contain sensitive data. Refuse symlinked paths and require 0600 database/sidecars in a 0700 parent before opening SQLite. Snapshots never leave the journal or enter public responses.
- Public operation DTOs are allowlists: no raw configs, snapshots, local paths, secrets or exception text. Only the current host call's permitted fields/token are exposed; no API keys in its body. Operation failure categories are fixed; compensated/partial/recovery-required results report `ok=false` and `final_state`.
- Never compensate over a newer unrelated edit. No fleet-wide transaction or always-undo promise; an HTTP success alone is not a committed operation.
- Tests use lab homes and fake credentials only; never touch the live Hermes home, perform inference or fabricate usage rows. `scripts/privacy_guard.py` rejects staged/tracked private data.

Private-import gate (after exporting the reference-checkout variable above):

```sh
python -c 'import os, sys; from pathlib import Path; sys.path.insert(0, os.environ["HERMES_MODEL_SWITCHER_HERMES_REPO"]); from hermes_cli.plugin_validate_private_imports import private_import_findings; findings = [h for h in private_import_findings(Path(".")) if not h.startswith((".venv", "node_modules", "dist-package"))]; print(findings); sys.exit(bool(findings))'
```
