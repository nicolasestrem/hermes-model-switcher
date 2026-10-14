# Usability polish

Public guide to the Model Switcher usability polish. The design contract lives in
[DESIGN.md](../DESIGN.md) (§4 layout, §5 components, §6 interaction) and usage
stays in [README.md](../README.md).

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

- Auxiliary selectors are **derived from the installed runtime**, never frozen:
  built-in tasks come from the installed Hermes auxiliary defaults and
  additional tasks come from plugins registered through the plugin auxiliary
  API.
- Plugin task registration is resolved against **the profile being inventoried**
  — its own context-local Hermes home, not one machine-global registry — so two
  profiles can legitimately show different plugin task rows.
- A **failed discovery is reported, not hidden**: an unconsumable discovery
  surfaces as an inventory warning on the profile so a broken scan can never be
  mistaken for an empty selector set (fail-closed).
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

## Evidence boundary and verification

Observed evidence for this pass is bounded and must not be read more broadly
than it is:

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

Local verification for this change completed with exit code 0:

- `npm run build`
- `env -u PYTHONPATH HMS_PYTHON="$PWD/.venv/bin/python" npm run test -- --workers=1` →
  `65 passed`
- `env -u PYTHONPATH .venv/bin/python -m pytest -o addopts='' -q` →
  `334 passed, 5 warnings`

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
