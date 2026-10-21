# Contributing

Thanks for helping improve Model Switcher — a community Hermes Web Dashboard plugin.

## Invariants to preserve

- Writes happen only in-process through the Dashboard path; hosted isolation stays
  read-only. Never write configuration outside the adapters, and never textually
  replace YAML.
- Every write target must be a discovered profile's config; refuse anything else
  before writing.
- Public operation views are allowlists: never expose raw configs, paths, snapshots,
  stored payloads, secrets, or exception strings. The only request body a view
  carries is the allowlisted `hostCall.body` the UI forwards to the host route.
- Keep preview, apply, per-profile readback, and revision-guarded Undo working
  together. Do not weaken the journal's fail-closed privacy behaviour.
- Tests use lab homes and fake credentials only. Never touch a live Hermes home and
  never perform inference.

## Development environment

- **Python:** the plugin declares `requires-python >=3.10`. The Hermes runtime it
  integrates with requires Python `>=3.11`, so the backend suite runs on 3.11+
  (CI uses 3.11 and 3.13; the declared 3.10 floor is covered by the package-build
  job).
- **Node.js:** `>=18` is the effective floor (esbuild's declared engine). CI runs
  Node 22 and 24. There is no `engines` field in `package.json`.

```sh
python -m pip install -e '.[test]'        # pytest + fastapi/uvicorn/httpx/yaml/...
npm ci
npm run build                             # esbuild -> ui/dist/plugin.js + dashboard/dist/{index.js,style.css}
npx playwright install --with-deps chromium
```

## Tests: portable vs real-runtime

Two suites:

- **Backend (pytest):** `python -m pytest tests/`
- **Browser (Playwright):** `npm test`

**Both require a real Hermes checkout.** `tests/conftest.py` puts the checkout on
`sys.path` and an autouse fixture imports `agent.secret_scope`; `tests/real_app.py`
— the browser suite's real backend — imports `hermes_constants` and `hermes_cli`. With
no checkout every pytest test errors during set-up and the browser suite cannot start
its real backend. That is a hard failure, **not** a skip you may treat as a pass.

Point the suites at a checkout with:

```sh
export HERMES_MODEL_SWITCHER_HERMES_REPO=/path/to/hermes-agent
```

Both `conftest.py` and `real_app.py` honour this variable (default
`~/.hermes/hermes-agent`).

The verified revision is Hermes **v0.22.3**, commit
`5b147ab036d6d19131bff8d20701b5001eab844a`. Reproduce it from the public repository:

```sh
# Pin the exact commit: it is immutable (the v0.22.3 tag is annotated onto it).
git init /tmp/hermes-agent
git -C /tmp/hermes-agent remote add origin https://github.com/NousResearch/hermes-agent.git
git -C /tmp/hermes-agent fetch --depth 1 origin 5b147ab036d6d19131bff8d20701b5001eab844a
git -C /tmp/hermes-agent checkout -q FETCH_HEAD
git -C /tmp/hermes-agent rev-parse HEAD   # 5b147ab036d6d19131bff8d20701b5001eab844a
```

A bare clone is enough: **there is no runtime bootstrap and no install stamp to
write.** `hermes_adapter.probe_runtime()` gates on capability, not on a version
string — it requires every public seam this plugin uses (`hermes_cli.config`'s
load/save/read-guard helpers, the `hermes_constants` home and named-profile
helpers, and `agent.secret_scope`) to import and be callable, and reports
`supported=True` when they do. `hermes_cli.__version__` is never consulted, so a
checkout without `install-stamp.json` (installer-generated and git-ignored) is
fully writable, a future Hermes that keeps these seams stays writable without a
plugin release, and a Hermes missing a seam degrades to read-only instead of
failing in the middle of a write.

A plain checkout is enough: the plugin's own `.[test]` dependencies run the whole
suite once the checkout is on the path — you do not install Hermes's own dependency
set for these tests.

The browser suite spawns a Python interpreter for `tests/real_app.py`, using
`HMS_PYTHON` when set and otherwise `python`. In CI `python` comes from
`actions/setup-python`; locally, point it at an interpreter with the `.[test]` deps:

```sh
HMS_PYTHON="$(python -c 'import sys; print(sys.executable)')" npm test
```

Always use an isolated lab home; never aim seeding or install scripts at live
profiles. The visual-evidence spec writes under `TMPDIR`, so keep it pointed at a
writable directory.

### Host-stylesheet guard

`tests/host-css-guard.spec.mjs` renders the catalog under the host dashboard's
real stylesheet, served by `tests/server.mjs` from
`$HERMES_MODEL_SWITCHER_HERMES_REPO/hermes_cli/web_dist/assets/index-*.css`.
`web_dist` is git-ignored upstream, so build it once in the pinned checkout
(`cd <checkout>/web && npm ci && npm run build`); without it the route answers
500 and the spec fails rather than passing on fixture CSS. CI does the same.

Its screenshot comparison is opt-in (`HMS_SCREENSHOT=1`): the
`catalog-host-css-linux.png` baseline depends on the local font stack and on the
pinned stylesheet, so regenerate it (`--update-snapshots`) whenever the Hermes pin
moves. The DOM and colour assertions run everywhere.

After installing into a real dashboard, `npm run check:live` runs the same checks
against it read-only in Chromium and Firefox (`HMS_LIVE_URL`, optional
`HMS_LIVE_PROFILE` selected through the plugin's own profile filter, and
`HMS_LIVE_COOKIE` when the dashboard needs a login).

## Privacy gate

```sh
python scripts/privacy_guard.py
```

It scans staged and tracked files and fails closed on credentials, configs, snapshots,
and related private data. Optional local Git hooks: `bash scripts/install_hooks.sh`
(a repository-only helper; the packaged distribution ships no hook installer).

## Packaging

In the source repository:

```sh
python scripts/build_package.py
```

An assembled package ships only `scripts/privacy_guard.py` under `scripts/`;
`scripts/build_package.py` and the `.github/workflows/ci.yml` described below are
repository-only and are not present in a packaged distribution. Only files named
in the explicit `DOC_FILES` manifest are packaged; a local or ignored
file under `docs/` does not become publishable just by existing. `scripts/build_package.py`
refuses an existing destination unless you pass `--replace`.

## CI

`.github/workflows/ci.yml` runs on `push` and `pull_request` with `contents: read`
only. Three jobs:

- **backend** — `pip install -e '.[test]'` then `python -m pytest tests/`, on Python
  3.11 and 3.13, after cloning the pinned Hermes checkout above. No install-stamp
  or bootstrap step: gating is capability-based, so a bare clone runs the suite.
- **browser** — `npm ci`, `npm run build`, `npx playwright install --with-deps chromium`,
  then bare `npm test`, on Node 22 and 24, with the pinned checkout available.
- **package** — the declared Python 3.10 floor: install, UI build, `compileall`, and
  the privacy guard.

CI is Linux-only; macOS and native Windows remain unverified.

## Reporting bugs

Include a minimal reproduction, your Hermes version, and sanitized command output.
Never attach credentials, complete configurations, or recovery journals.
