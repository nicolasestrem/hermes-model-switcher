# Product screenshots

## Current: 0.3.2 catalog captures

The six `model-switcher-0.3.2-*.png` images are the current captures of the 0.3.2
interface, taken on **2026-10-21** at a **1280x800** viewport (the last one at
**390x844**), device scale factor 1, as viewport screenshots rather than full-page
captures.

Boundary: *Dashboard SDK fixture with the plugin's real backend and Hermes' own
`/api/model/*` routers over an isolated lab home.* Concretely, the compiled plugin
runs inside this repository's Dashboard SDK fixture and talks to its real FastAPI
backend, which performs real preview/apply/readback saves against a throwaway lab
home under `/tmp`. Hermes' own `/api/model/*` routers serve the host evidence. This
is **not** a live Dashboard deployment, not provider execution and not a production
mutation. The lab holds two synthetic profiles, `default` and `research`.

- `model-switcher-0.3.2-dashboard.png`: idle workspace in the dark host theme — the
  profile filter, routing matrix and change editor before a target is selected.
- `model-switcher-0.3.2-preview.png`: a reviewed `default/main` change from
  `anthropic/claude-sonnet-4-6` to `zhipu/glm-5.3-flash`, showing the semantic
  provider/model intent before Apply; the exact persisted config diff appears
  only after Apply readback.
- `model-switcher-0.3.2-operation.png`: the operation drawer after Apply, showing
  `Applied + read back`, the operation reference, the revision transition and the
  guarded Undo control.
- `model-switcher-0.3.2-catalog.png`: the provider-grouped model catalog with the
  input/output price columns and a per-row status. The pinned offline lab host
  publishes no pricing fields, so every row reads "price not published" — unknown
  is never treated as free. A host that supplies pricing fills the same columns.
- `model-switcher-0.3.2-light.png`: the same idle workspace under the light host
  tokens.
- `model-switcher-0.3.2-narrow.png`: 390x844, where the editor stacks above the
  matrix and the matrix scrolls horizontally inside its own region.

`catalog-captures.json` records the capture boundary, the viewport, and a SHA-256 per
file, plus the surface digest computed exactly as `tests/visual-evidence.spec.mjs`
does: SHA-256 over `ui/src/main.jsx`, `ui/src/api.js`, `ui/src/styles.css` and
`dashboard/dist/index.js`, in that order. `observedAt` changes on every run, and so do
the lab-generated operation reference and revision hashes in the operation capture
(and therefore that file's digest); the other five files are byte-stable.

The captures are opt-in so the normal suite never rewrites tracked images:

```sh
HMS_WRITE_CATALOG_CAPTURES=1 \
HERMES_MODEL_SWITCHER_HERMES_REPO=/path/to/hermes-agent \
HMS_PYTHON="$PWD/.venv/bin/python" \
  npx playwright test --config=tests/playwright.config.mjs tests/catalog-captures.spec.mjs
```

Without `HMS_WRITE_CATALOG_CAPTURES=1` the spec reports six skipped tests. When it
does run it also asserts that no console error or failed request occurred and that
the document has no horizontal overflow at the captured width.

No private profile data, credentials, host addresses, file paths or ports appear in
these images: the plugin's public DTOs carry profile ids, slot ids, routes, operation
references and revision hashes only.

## Historical: pre-0.3.0 captures

The eight remaining PNGs were captured on **2026-10-19** at commit `02703e3`
(pre-0.3.0), from the routing editor as merged in PR #13, and are carried unchanged.
They are kept for comparison and describe the older interface; they are not current
UI evidence.

### Fixture renders — the compiled plugin

The five theme, catalog and viewport PNGs are authentic renders of the compiled
plugin in this repository's Dashboard SDK fixture, driven with synthetic Default,
Research and Archived profiles. They are real captures of the plugin UI — not a
live Dashboard deployment and not provider execution. Recorded usage is
deliberately absent.

- `model-switcher-dark-1440.png`: desktop, dark host theme.
- `model-switcher-light-1440.png`: desktop, light host theme.
- `model-switcher-dark-375.png`: narrow screen, dark host theme; the matrix scrolls
  horizontally inside its own region.
- `model-switcher-catalog-1440.png`: the bird's-eye model catalog, desktop.
- `model-switcher-catalog-375.png`: the model catalog on a narrow screen.

No private profile data, credentials, host addresses, file paths, or real
operation results are displayed in these fixture shots.

### Isolated lab Hermes Dashboard captures

The three `real-*` PNGs are captures from an isolated lab Hermes Dashboard running
two synthetic profiles, default and research. The browser drove Preview, Apply and
guarded Undo through the real host UI: the configuration bytes were unchanged by
Preview, changed by Apply, and restored exactly by Undo, and separate HTTP checks
confirmed authenticated API responses and served-asset hashes. No provider inference
was performed.

- `model-switcher-real-dashboard.png`: the loaded routing matrix and editor.
- `model-switcher-real-preview.png`: a preview of the synthetic main-model change.
- `model-switcher-real-operation.png`: a committed operation with verified readback.

Operation and revision IDs shown are real lab-generated identifiers, not
fabricated success data. No private data is displayed, and the lab, server and
listening port were checked clean after teardown.
