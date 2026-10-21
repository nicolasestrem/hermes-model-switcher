# Catalog submission readiness

Status: **0.3.1 entry draft; the 0.3.0 entry is under review in
[NousResearch/hermes-agent#141744](https://github.com/NousResearch/hermes-agent/pull/141744).**
That submission addressed the review of plugin commit `02703e37`. This page tracks
the 0.3.1 candidate; it is not evidence that an updated entry has been submitted,
approved or listed.

## Procedure (authoritative)

Per the Hermes developer guide, *Submitting to the Plugin Catalog*
(`website/docs/developer-guide/plugins/catalog-submission.md`, mirrored from
`plugin-catalog/README.md`), a listing is added by a pull request that adds
`plugin-catalog/<name>.yaml` to `NousResearch/hermes-agent`. There is no self-serve
registry. The pinned `sha` is a full 40-character commit; a maintainer reviews that
tree and merges, requests changes or declines.

Local pre-check, against the exact plugin commit to be pinned:

```sh
hermes plugins validate /path/to/hermes-model-switcher --install-deps
```

## 0.3.1 readiness and disclosures

- Runtime integration uses public modules and SDK-authenticated host routes only.
  Require a clean private-import acceptance check on the shipped code, as documented
  in CLAUDE.md and [release readiness](release-readiness.md). A prior validation pass
  is not evidence that the refactored candidate passed.
- Compatibility is capability-based, not a version comparison. Do **not** add a
  `requires_hermes` floor unless the plugin code declares and enforces one. The tested
  reference is **v0.22.3**, commit
  `5b147ab036d6d19131bff8d20701b5001eab844a`; this is a reproducibility pin, not a
  minimum release.
- The plugin needs the public `/api/model/options`, `/api/model/auxiliary`,
  `/api/model/moa` and `/api/model/set` routes. Missing required capabilities leave
  it read-only; missing host routes degrade the affected features to unknown/hidden
  data or unavailable writes, never a private fallback. Main/compatible auxiliary
  assignments use the host write route, including its expensive-model confirmation;
  other slots use public config load/save under the target home.
- The undo journal keeps a copy of **each complete pre-change config**, which can
  include sensitive fields or embedded credentials. State lives only under
  `<hermes home>/state/hermes-model-switcher/`; an unsafe plugin-tree or Git-worktree
  location makes the plugin read-only, with no fallback directory. Do not publish
  journals, sidecars, backups or raw configs.
- Public host writes have no expected-revision parameter. Readback checks semantic
  intent and foreign changes; detected races end in `recovery_required`, not silent
  success or blind rollback. Main preview shows provider/model intent; the exact
  persisted diff appears after readback. These are accepted limits, not a fleet-wide
  transaction or an always-undo promise.

## Entry template (to add as `plugin-catalog/hermes-model-switcher.yaml`)

```yaml
name: hermes-model-switcher
repo: https://github.com/nicolasestrem/hermes-model-switcher
sha: <40-hex commit pinned at submission>   # fill at submission time
description: Choose models across Hermes profiles, preview changes, and reuse your
  setup with presets — one reviewed surface for every supported model-selection slot.
maintainer: Nico Estrem
tier: community
category: models
docs_url: https://github.com/nicolasestrem/hermes-model-switcher#readme
version: "0.3.1"
screenshots:
  - https://raw.githubusercontent.com/nicolasestrem/hermes-model-switcher/<sha>/docs/images/model-switcher-dark-1440.png
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

Update `sha` and every `<sha>`-pinned image URL together. Keep `version` aligned
with the pinned manifests, rerun validation and disclose the storage/compatibility
limits above in the PR. Historical screenshots do not verify the final 0.3.1 tree;
the 0.3.0 captures remain current because the UI is unchanged in 0.3.1.

## Why `.hermes-catalog.json` is NOT in this repository

`.hermes-catalog.json` is an **installer-generated convenience copy**, not a source
artifact. Hermes writes it into an installed plugin directory after a catalog
install; authoritative provenance is the `catalog` block of installer-owned
`.install-metadata.json`. A checkout shipping its own copy is not evidence of a
reviewed catalog install. The file is intentionally absent from source.
