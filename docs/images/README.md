# Product screenshots

All eight PNGs in this directory were regenerated on **2026-10-19** from the current
routing editor (merged as PR #13). They document the Model Switcher interface, not
provider execution.

## Fixture renders — the compiled plugin

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

## Actual Hermes Dashboard lab captures

The three `real-*` PNGs are captures from an isolated lab Hermes Dashboard running
two synthetic profiles, default and research. The browser drove Preview, Apply and
guarded Undo through the real host UI: the configuration bytes were unchanged by
Preview, changed by Apply, and restored exactly by Undo, and separate HTTP checks
confirmed authenticated API responses and served-asset hashes. No provider inference
was performed.

- `model-switcher-real-dashboard.png`: the loaded routing matrix and editor.
- `model-switcher-real-preview.png`: a real preview of the synthetic main-model change.
- `model-switcher-real-operation.png`: a committed operation with verified readback.

Operation and revision IDs shown are real lab-generated identifiers, not
fabricated success data. No private data is displayed, and the lab, server and
listening port were checked clean after teardown.
