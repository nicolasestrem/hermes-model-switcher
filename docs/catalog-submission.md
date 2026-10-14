# Catalog submission readiness

Status: **ready-for-submission — NOT submitted.** No entry has been opened against
`NousResearch/hermes-agent`; recording readiness is not a claim of review, approval,
or listing.

## Procedure (authoritative)

Per the Hermes developer guide, *Submitting to the Plugin Catalog*
(`website/docs/developer-guide/plugins/catalog-submission.md`, mirrored from
`plugin-catalog/README.md`), a listing is added by a **pull request that adds one file**,
`plugin-catalog/<name>.yaml`, to `NousResearch/hermes-agent`. There is no self-serve
registry. The pinned `sha` is a full 40-character commit; a maintainer reviews the pinned
tree and merges, requests changes, or declines.

Local pre-check (the same check catalog CI runs), against a checkout at the commit to be
pinned:

```sh
hermes plugins validate /path/to/hermes-model-switcher --install-deps
```

## Local readiness evidence

- `hermes plugins validate .` → **Validation passed** (manifest, loadable entry point,
  capability probe, security scan `safe`, no core override).
- One warning remains, from catalog rule 9: **private core imports** in
  `dashboard/model_catalog.py`, `dashboard/plugin_api.py`, and `hermes_adapter.py`.
  Validation passes, but a reviewer reads these; they are the main reason the plugin is
  not yet a clean submission. See [release readiness](release-readiness.md).
- Rule 14 (`requires_hermes` truthful): the adapter's inspected baseline is Hermes
  `v0.21.7` (`hermes_adapter.py`, `SUPPORTED_BASELINE_VERSION`); the tested release
  checkout is `v0.22.0+canary.20261014T070054Z`. A `requires_hermes: ">=0.21.7"` floor
  is consistent with both; confirm before submitting.

## Entry template (to add as `plugin-catalog/hermes-model-switcher.yaml`)

```yaml
name: hermes-model-switcher
repo: https://github.com/nicolasestrem/hermes-model-switcher
sha: <40-hex commit pinned at submission>   # fill at submission time
description: Choose models across Hermes profiles, preview changes, and reuse your
  setup with presets — one reviewed surface for every model-selection slot.
maintainer: nicolasestrem
tier: community
category: models
requires_hermes: ">=0.21.7"
docs_url: https://github.com/nicolasestrem/hermes-model-switcher#readme
version: "0.2.0"
screenshots:
  - https://raw.githubusercontent.com/nicolasestrem/hermes-model-switcher/<sha>/docs/images/model-switcher-dark-1440.png
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

Update `sha` and every `<sha>`-pinned image URL in the same PR that pins the commit.

## Why `.hermes-catalog.json` is NOT in this repository

`.hermes-catalog.json` is an **installer-generated convenience copy**, not a source
artifact. The Hermes CLI writes it into an *installed* plugin directory after a catalog
install, and its authoritative provenance is the `catalog` block of the installer-owned
`.install-metadata.json` (`hermes_cli/plugins_cmd_catalog.py`). A checkout that ships its
own `.hermes-catalog.json` is explicitly treated as **not** a reviewed catalog install.
Committing one would therefore be misleading at best; the file is intentionally absent
from source.
