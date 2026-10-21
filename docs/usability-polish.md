# Usability polish

Public guide to the Model Switcher usability polish. The design contract lives in
[DESIGN.md](../DESIGN.md) (§4 layout, §5 components, §6 interaction) and usage
stays in [README.md](../README.md).

## Public release refinement

The introduction leads with choosing models across profiles, reviewing changes,
and reusing setups. The matrix is headed **Select your targets**, and the editor
**Choose your routing**. A distinct refresh/recovery toolbar separates workspace
tools from the configuration actions. These headings preserve the existing
editor-first DOM order and wide-screen matrix/rail arrangement.

Selection counts use a polite live announcement. Provider browsing explains
both an empty catalog and a search with no matches, retaining manual exact-ID
entry. Secondary inline text respects the 13 px minimum. Matrix route identifiers
stay whole in the table’s horizontal scroll region; preview entries have clearer
separation, and narrow screens wrap filter fields.
All styles remain scoped to the plugin and use host tokens; no external fonts,
decorative animation, or additional network requests were introduced.

This refinement requires the release verification below against the final
candidate. Earlier recorded results are historical evidence, not a test result
for these source changes.

## Profile view filters vs. bulk selection

- **Profiles shown** filters the matrix *view only*. It never adds or removes
  edit targets. **All profiles** resets the profile scope.
- Edit targets are chosen with the per-row slot checkboxes or the header
  **Select all matching writable slots** control. Bulk select covers **matching writable
  rows only** — it respects the current family / task / provider / model / state
  filters and never touches a read-only row.
- Selection **survives filtering**. When a filter hides a selected row, the
  selection summary still counts it and states how many targets are out of view
  (e.g. `2 slots on 2 profiles selected` plus `1 hidden by current filters`).
- The header select-all is **indeterminate** when only some matching writable
  rows are selected, and checked when all of them are.
- **Clear selection** explicitly removes every edit target, including any that
  are outside the current filters.

## Human action semantics (editor)

Distinct, individually labelled actions — no silent equivalences:

- **Choose a model** assigns a provider and model. On a fallback slot it is an
  explicit **one-entry chain**.
- **Set fallback sequence** accepts several `provider/model` entries in order.
- **Reset to default or inherited routing** removes the override.
- **Turn off fallback** writes an explicit **empty chain** — it does *not*
  disable the main model.
- A malformed or missing chain is **rejected**, never silently cleared.
- An **applicability summary** states what the chosen action will touch instead
  of dropping targets silently (e.g. `1 of 2 fallback chain slots`). When
  selected targets are not compatible with the action, preview is disabled
  rather than a mixed set being quietly reduced.
- **Exact-model removal** is a **separate advanced action**, collapsed inside an
  *Advanced* disclosure until opened, and takes precedence over a model change
  for the affected slots.

## Runtime-derived auxiliary and plugin tasks

- Built-in auxiliary selectors use the public config defaults and a local mirror
  of the host's inheritance rules. Plugin task rows come from SDK-authenticated
  `GET /api/model/auxiliary?profile=...`, not a plugin-side registry import.
- The host payload belongs to **the profile being inventoried**, so two profiles
  can legitimately show different plugin task rows.
- Missing or malformed host auxiliary data hides plugin-registered rows and adds
  an inventory warning; it is never mistaken for a successful empty task list.
- `goal_judge` stays a **judge-owned single row** (it is not duplicated as an
  auxiliary selector). Other auxiliary policy keys and obsolete ignored blocks
  are not task selectors, and a plugin task cannot shadow them. The scalar
  OpenRouter override has its own cross-cutting row, described below.
- Plugin-registered tasks carry their human-readable label/description metadata
  through to the UI. Declared task inheritance follows its base until the
  task is explicitly pinned, without borrowing another route's credentials.

## Vision override

Vision is an **auxiliary task**, not a specialist speech backend. Pinning its
backend while `agent.image_input_mode` is `auto` makes the main agent receive a
**text interpretation** of images even when its main model supports images.
Explicit **native** and **text** image modes are **unchanged**. The slot note
states this side effect; the unrelated `auxiliary.vision.download_timeout` is
preserved and not edited by this selector.

## Scalar OpenRouter override

`auxiliary.openrouter_model` is a **cross-cutting scalar**, not a per-task
route: it affects auxiliary auto-routing across tasks (consumed where the
runtime builds auxiliary auto-chains). It is stored as a **bare model string**
with an **implicit OpenRouter provider**; an edit that also supplies a provider
is rejected. A non-empty value is treated as routing through OpenRouter for the
credential gate, so it cannot vacuously pass without satisfying the target
profile's OpenRouter provider gate. The existing declared-custom-endpoint rule
still applies.

## Speech defaults and read-only slots

- **Unset** speech backends follow the **runtime defaults** (local STT / Edge
  TTS in the tested runtime); the selector does not invent an explicit backend.
- Speech edits bind the selected backend and its **native model field** together
  and write the matching provider companion.
- A backend that carries **no configurable model field** (the current **Edge
  TTS** default) exposes **no editable model key**; the row states that reason
  instead of inventing a key. The row still supports an **explicit switch to a
  supported model-bearing backend**, which then binds that backend's **native
  model field**. This is scoped to the model slot — it is **not** a claim that
  every writable field on the row is uneditable.

## Responsive rail / layout and drawer keyboard

- **Wide view**: matrix plus a **bounded editor rail** (≈360 px, never more than
  380 px) side by side, one workspace. The editor precedes the matrix in the
  **DOM** for action discoverability even when rendered to the right.
- **Narrow view**: the workspace **stacks** (editor above matrix), controls wrap
  or stack, and there is no document horizontal overflow. The matrix scrolls
  internally with a sticky row identity; the scroll region is keyboard
  focusable and arrow-scrollable.
- **Operation drawer**: focus **enters** the drawer on open, is **trapped**
  (Tab / Shift+Tab cycle inside), **Escape closes** it, and focus **returns** to
  the opening control — or to **Refresh state** when the opener is disabled.

## Public host-seam state matrix

This is the implementation/verification matrix, not a visual-QA verdict. Mock
keyboard, request and state assertions exercise the compiled bundle. Screenshot
review and final integration belong to the integrating owner; no live host write
or real-provider pricing claim is implied here.

| Surface | State | 1440 px | 768 px | 375 px |
| --- | --- | --- | --- | --- |
| Host data notice | loading | Muted polite live line; matrix usable | Same, wrapping | Same, wrapping; no blocked selectors |
| Host data notice | partial | Missing part named; valid parts retained | Same | Same; no horizontal overflow |
| Host data notice | unavailable / older-host 404 | Warning line; config-only state retained | Same | Same |
| Apply progress | pending | Ordered journal-step list beside Apply | Stacked list | Wrapped labels, vertical flow |
| Apply progress | awaiting host | Running step named; subsequent steps pending | Same | Same |
| Apply progress | confirm | Running step says confirm; one modal owns focus | Same | Same; viewport gutters |
| Apply progress | done | Backend readback statuses, never inferred host success | Same | Same |
| Apply progress | failed / rolled back | Danger failure or warning rollback text; consumed review | Same | Same |
| Apply progress | recovery_required / interrupted | Fixed error, recovery control; further mutations blocked | Same | Same; control stays reachable |
| Host confirmation | open / confirm | Neutral title, verbatim text, Confirm/Cancel | Maximum width 520 px | 16 px gutters; vertical scroll |
| Host confirmation | keyboard / Escape | Initial Confirm focus, Tab cycle; Escape declines | Same | Same; reduced-motion unchanged |
| Host confirmation | closed | Return to Apply; final summary drawer then returns to Apply on close | Same | Same |
| Host apply error | non-2xx or ok:false | Inline danger message + polite live announcement; numeric status only reported | Same | Same |
| Catalog refresh | loading / completed | Explicit refresh=1 options refetch; refresh-requested provenance | Same | Same; existing controls wrap |
| Catalog provenance | no fetch timestamp | “from Hermes picker cache”; no invented time or default stale warning | Same | Same |
| Model prices | priced / free / unknown / pending | Verbatim host displays; unknown says price not published, sorts last | Same | Table owns horizontal scroll |
| Provider evidence | unauthenticated / provider error | Existing sign-in/error row, never labelled free | Same | Wrapped notice |

No decorative animation is introduced. New surfaces disable transitions and
animation under prefers-reduced-motion. Native selects, first-Tab target,
matrix columns, exact provider/model IDs and plugin success-envelope rules stay
unchanged. A journal result is a step, not a profile aggregate: multiple results
for the same profile have unique React keys, and counts labelled “profiles” use
unique profile IDs. Known no-write revision conflicts remain reviewable;
ambiguous interrupted writes never replay a host instruction automatically.

## Preserved gates

The polish does not relax the review pipeline:

- Editing the route, the selection, or the filters **invalidates the review**
  (the preview panel clears and Apply is disabled); a late preview response
  cannot revive it.
- Preview remains **all-or-nothing**: a preview containing blocked changes
  cannot be applied partially — fix the blocked entries and re-preview.
- **Apply** re-checks every referenced provider's credentials for the target
  profile before any write.
- **Readback** per profile is reported in the operation drawer; **Undo** remains
  an explicit, revision-guarded recovery action. Apply/Undo refresh configured
  state automatically; **Last observed** stays **No recorded usage** until real
  runtime usage evidence exists and is never fabricated from configuration.

## Impeccable scoped polish — observed fixture evidence

This refinement preserves the host-native design contract, native selects, editor-first DOM, pinned first Tab target, seven matrix columns, exact DTOs and every preview/apply/readback/undo guard. It restores 14px matrix body / 13px secondary text, modern host-token precedence, readable semantic fallbacks on both prescribed host themes, 16px root gutters, two-column narrow filters and compact tablet picks. It removes duplicate no-selection prose, both thick side borders, nested advanced-disclosure chrome, metric-card boxes and smooth scrolling. Empty-state copy is concrete; unavailable Apply no longer receives enabled hover/primary emphasis.

Evidence is in the profile scratch `impeccable-polish/` folder, not committed screenshots:

| Evidence | Observed result |
|---|---|
| Independent critique | Before 26/40; after 27/40. Before specificity qualitative; after 7/10. Remaining issues below. |
| Static detector `detect --json ui/src` | 2 side-tab warnings → `[]`; final exit 0 |
| Rendered detector `detect --json http://localhost:8951/fixture.html` | edge-gutter + nested-card warnings → `[]`; final exit 0 |
| SDK host-reset detector | `[]`, exit 0 |
| State matrix | Before and after: 96 unique state/theme/viewport cells, 300 PNGs each; 1440×900 / 768×1024 / 375×812, light + dark |
| States | Initial, selected matrix, editor, preview, host loading/partial/unavailable, host-step apply progress, confirm, host error, recovery required, catalog + priced/free/unknown/unauthenticated rows |
| Enabled-text contrast samples, light / dark | Foreground 17.495 / 14.496; muted 6.711 / 7.774; danger 6.411 / 6.876; success 6.523 / 9.948; warning 6.473 / 10.170 |
| Build | `npm run build`, exit 0 |
| Mock specs | `env -u PYTHONPATH HMS_FXT_PORT=8951 node_modules/.bin/playwright test --config=/home/hermes/.hermes/profiles/brian/cache/scratch/ps-ui-mock.config.mjs --workers=2`, exit 0; **138 passed, 0 failed** |
| Stylesheet contract | `ui/src/styles.css`, `dashboard/dist/style.css`, `ui/dist/style.css` byte-equal; scoped-style spec green |

The new regression tests assert actual host-rule colors, 14/13px type, sampled warning-text contrast in both themes, compact two-column filters, flat disclosure/notices/counts and no smooth-scroll dependency. Existing behavior assertions were not weakened. Build outputs were generated only with the build command.

The first rendered detector needed a local Chromium executable override; failed attempts are preserved rather than counted as clean scans. Successful final scans used installed Chromium1248. No browser download or live configuration writes occurred. Initial Assessment A used the ordinary fixture; the controlled before/after state matrix uses the same SDK fixture. Its provisional recovery score was revised after exercising failures, so the one-point overall improvement is not an accessibility or quality certification.

### Deliberately retained / pin conflicts

- Editor-first DOM and first Tab on the preset remain pinned: stacked target/editor travel is improved, not eliminated. No new workflow jump/reordering was introduced.
- Consequential Apply still appears before exact preview spatially; review gates were preserved rather than moved under a polish label.
- Repeated-profile result rows need richer target identity only if actual DTOs provide it; no fields were invented. Recovery guidance needs separately scoped, guard-aware copy.
- The critique's generic four-item cognitive-load advice does not justify dropping any of the pinned seven matrix columns.
- Host fonts, surfaces, explicit semantic token overrides and legacy fallbacks remain authoritative. No new palette/font/motion/portal was introduced.

Reports: `CRITIQUE.md`, `assessment-a-after.md`, `assessment-b-after.md`, `FINAL.md`; raw `before-static.json`, `before-rendered-browser-path.json`, `after-static.json`, `after-rendered.json`, `after-rendered-sdk.json`; both state manifests and PNGs. The scratch evidence is fixture-only, not a live-host, screen-reader, zoom/reflow, performance, or accessibility PASS.


## Impeccable round 2 — concrete defect closure

The second restrained pass keeps exact configured/observed route IDs on one line,
with 240px route cells and native table-owned horizontal scrolling. Slot, Family
and State also have readable minimum widths: a fresh review caught short labels
being squeezed after the first nowrap change; that regression was fixed before
final capture. The pinned seven columns and sticky profile identity remain.

The header advisory now owns a line below its actions. The preset's existing
button says **Load preset**, without adding a focus target. The zero summary is
**No slots selected**; the routine loaded status remains only in the polite
screen-reader live region. The empty editor leads with **Select writable slots
in the table to start.**, while its fields are visually subordinate, not disabled
or reordered. Checked rows have restrained selection fill, including sticky
identity cells; read-only rows state **Read-only** plus their actual reason.
Advanced/catalog/acknowledgement groups have 16/24px separation. The original
cost acknowledgement and every safety gate are unchanged; **Required before
Apply.** explains when it is needed.

Only wording-pinned assertions changed in existing specs: zero selection and
Read-only capitalization in `usability-polish.spec.mjs`, plus catalog profile
wording in `dropdowns.spec.mjs`. The read-only reason assertion was strengthened.
The new `polish-round2.spec.mjs` adds 13 cases for wording/focus/selection, owned
scrolling and readable target labels, and composited muted-text contrast.

Final bounded evidence is under profile scratch `impeccable-polish/round2/`:

- `npm run build`: exit 0. Source, shipped and fixture CSS are byte-equal.
- Exact mock-only command above: **151 passed, 0 failed**, exit 0.
- Static detector and both rendered fixture detectors: **[]**, exit 0.
- Fresh capture: **96 unique state/theme/viewport cells, 300 PNGs**, no missing
  files or recorded page errors; includes preview, progress, confirmation, host
  error, recovery-required and catalog in both themes at 1440/768/375px.
- Enabled secondary text matching the muted host token was measured on actual
  composited initial/selected/catalog surfaces, not judged by eye: light minimum
  **6.711:1 initially / 5.544:1 selected**, dark **7.774:1 / 6.450:1**. All exceed
  AA 4.5:1; no unnecessary replacement of host muted colors was introduced.
- Honest critique: Round1 **27/40**, final Round2 **27/40**, specificity **7/10**.
  Local defects are fixed; broader stacked-layout, review-position and recovery
  guidance debt remains. Detector cleanliness is not a design/accessibility pass.

`round2/FINAL.md` lists per-defect before/after screenshots, every copy change,
exact commands and receipts. `round2/CRITIQUE.md` retains the independent interim
regression finding and final reinspection addendum. No live configuration,
provider network, source-order change or weakened behavior assertion is implied.

## Earlier evidence boundary and verification

The following evidence is historical, from the pre-0.3.0 usability pass. It does
not verify the public-seams refactor or its host-step protocol:

- **Compiled UI in an SDK / host-reset fixture** (theme-light and theme-dark at
  1440/768/375 px, plus the interactive layout captures at desktop and mobile
  widths): these verify the compiled plugin under the host CSS reset with
  explicit UI fixture data. They are **not** a live Dashboard render.
- **Actual isolated FastAPI backend** (uvicorn over a throwaway lab home) with a
  **canonical save** on disk and **byte-exact Undo** restoring the original
  files; fake credentials only, no inference calls, and every plugin-API
  response asserted free of keys, host paths, snapshots and payloads.
- **Not verified here**: an authenticated live Hermes Dashboard was **not**
  exercised, and fixture checks do not establish deployment or merge status.
  Hosted-isolation mode remains read-only.

Historical local verification for that usability pass completed with exit code 0:

- `npm run build`
- `env -u PYTHONPATH HMS_PYTHON="$PWD/.venv/bin/python" npm run test -- --workers=1` →
  `69 passed`
- `env -u PYTHONPATH .venv/bin/python -m pytest -o addopts='' -q` →
  `379 passed, 1 warning`

## Tests

- `tests/usability-polish.spec.mjs` — filters vs. bulk selection, hidden-target
  counting, indeterminate select-all, readable labels, read-only reasons,
  editor action labels / applicability, removal disclosure, layout and drawer
  keyboard.
- `tests/polish-contract.spec.mjs` — scalar OpenRouter model-only intent,
  fallback actions rejecting mixed targets, discovery warnings, readable plugin
  task labels, vision warning on selection.
- `tests/polish-visual.spec.mjs` — light/dark × 1440/768/375 px layout, sticky
  identity, keyboard scroll, contrast, screenshots and per-run manifest.
- `tests/visual-evidence.spec.mjs` — observed captures against the real FastAPI
  backend with canonical save and guarded Undo.
- `tests/test_usability_inventory.py` — runtime-derived auxiliary and plugin
  slots, scalar vs. selector behaviour, vision note, speech defaults and
  read-only backends.
- `tests/test_review_batch_regressions.py` — vision auto-mode note, OpenRouter
  scalar credential reference/gating, discovery-failure warnings.
- `tests/test_browser_harness_isolation.py` — the real browser harness refuses
  discovery outside its declared lab; the `--seed` CLI refuses a target nested
  under (or equal to) the native home **before any write** and seeds only a
  provably isolated root without overwriting existing custom-home profile data;
  `--print-profiles` constructs no router and writes nothing.
- `tests/test_pr9_backend_reviews.py` — speech assignment capabilities, scoped
  warnings, concurrent inventory isolation, sanitized fallback warnings,
  profile-specific task registries and inheritance cycles.
- `tests/test_pr9_profile_route_scope.py` — profile task scope agrees across
  State, Preview, Apply and exact removal, with real isolated save/undo; legacy
  read-only descriptors cannot acquire assignment capability accidentally.
- `tests/tts-assignment.spec.mjs` — Edge TTS selection, explicit backend/model
  intent, refusal of imaginary-field resets, and rendered desktop/mobile switch
  guidance.
- `tests/test_pr9_speech_validation.py` — blank, non-string and untrimmed route
  values are rejected before submitted-backend lookup; valid identifiers remain
  byte-exact and supported model-only updates keep their established semantics.
- `tests/test_pr9_speech_api_refusals.py` — both HTTP Preview and direct Apply
  refuse malformed assignments and model-less reset/remove/chain attempts,
  without changing the profile's configuration bytes.
- `tests/test_pr9_aux_review_regressions.py` — scalar model-string validation,
  declared inheritance, pinned defaults, missing bases and bounded cycles.
- `tests/test_pr9_aux_api_regressions.py` — refused scalar mutations preserve
  config bytes; valid assignment/removal and guarded Undo work through the real
  HTTP API; inherited DTOs keep configured and effective values separate.
- `tests/aux-inheritance-ui.spec.mjs` — inherited and known default routes are
  readable after expanding details at desktop/mobile widths, without labeling
  configuration resolution as recorded usage.
