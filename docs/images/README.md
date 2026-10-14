# Product screenshots

The three theme/viewport PNGs show the compiled plugin in the repository's Dashboard SDK fixture,
with synthetic Default, Research and Archived profiles. They are authentic
renders of the plugin UI, not a live Dashboard deployment or provider execution.
Recorded usage is deliberately absent. The preview selects two main-model slots;
Apply remains disabled until the cost acknowledgment is checked.

- `model-switcher-dark-1440.png`: desktop, dark host theme.
- `model-switcher-light-1440.png`: desktop, light host theme.
- `model-switcher-dark-375.png`: narrow screen, dark host theme; the matrix scrolls
  horizontally inside its own region.

Captured during the 2026-10-14 release-preparation pass after building the current
candidate. No private profile data, credentials, host addresses, file paths,
or real operation results are displayed.

## Actual Hermes Dashboard lab captures

Captured from candidate `188add0` in the real Hermes Dashboard, using an isolated
synthetic default profile:

- `model-switcher-real-dashboard.png`: the loaded routing matrix and editor.
- `model-switcher-real-preview.png`: a real preview of the synthetic main-model change.
- `model-switcher-real-operation.png`: a committed operation with verified readback.

The browser drove Preview, Apply and guarded Undo through the real host UI; the
configuration bytes were unchanged by Preview, changed by Apply, and restored
exactly by Undo. Separate HTTP checks confirmed authenticated API responses and
served-asset hashes. No provider inference was performed. Operation/revision IDs
are real lab-generated identifiers, not fabricated success data. The lab, server
and listening port were checked clean after teardown.
