# Catalog submission readiness

Status: **the 0.3.2 entry is submitted; the entry in
[NousResearch/hermes-agent#141744](https://github.com/NousResearch/hermes-agent/pull/141744)
is re-pinned to 0.3.2 and awaits maintainer re-review and approval of its Plugin
Catalog CI run.** The original submission addressed the review of plugin commit
`02703e37`. This page tracks the submitted 0.3.2 entry; it is not evidence of
approval or listing.

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

## 0.3.2 readiness and disclosures

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

## Entry (submitted as `plugin-catalog/hermes-model-switcher.yaml`)

```yaml
# Community catalog entry — hermes-model-switcher
#
# Owner-submitted: the PR author (nicolasestrem) owns and maintains the repo below.
# Exact-SHA pin (40-hex, resolves as a COMMIT on the repo). Dashboard-only plugin:
# the whole code surface is a Dashboard extension tab served through the plugin host.
name: hermes-model-switcher
repo: https://github.com/nicolasestrem/hermes-model-switcher
sha: <sha>
description: >-
  Choose models across Hermes profiles from a Dashboard tab: a per-profile
  picker built on host-supplied model data (SDK-authenticated
  GET /api/model/options), a preview of provider/model intent across every
  supported model-selection slot (main, delegation, auxiliary, judge,
  fallback, MoA, cron, plugin and specialist), and one-click apply or revert
  with presets.
  Main-slot preview shows intended provider/model, not the exact persisted
  diff; the exact diff is shown only after host readback. Disclosure — its
  apply route reads and edits the profiles' config.yaml (that is the plugin's
  function) through public seams only: POST /api/model/set for main and
  compatible auxiliary slots, and public hermes_cli.config load/save under the
  target home for other slots; credential presence checks use public
  profile-scoped secret helpers (agent.secret_scope), and profile discovery
  uses public hermes_constants. The plugin makes no direct third-party
  requests and opens no provider caches or pricing registries directly, but
  its browser tab does call the host's /api/model/* routes through the
  Dashboard SDK, and the host options route may itself initiate provider or
  pricing I/O even without an explicit refresh request, so reads are not
  guaranteed cache-only.
  It runs no shell commands or background processes and reports nothing. The
  private undo journal stores a full pre-change snapshot of each config it
  edits, which can contain sensitive fields including embedded credentials.
  Saved presets also persist reviewed change values, potentially including
  inline credentials or credential-bearing base URLs, in presets.json (0600)
  under get_hermes_home()/state/hermes-model-switcher/ (0700).
  The host write route has no expected-revision check: readback surfaces many
  foreign edits as recovery_required, but a concurrent same-key edit can be
  overwritten undetected and full-state deletions retain an unrelated-write
  race, so not every foreign edit is guaranteed to surface.
maintainer: Nico Estrem
tier: community
category: models
docs_url: https://github.com/nicolasestrem/hermes-model-switcher#readme
version: "0.3.2"
# Fresh 0.3.2 captures at the pinned commit, re-taken after the catalog-row
# fix: Dashboard SDK fixture with the
# plugin's real backend and Hermes' own /api/model/* routers over an isolated
# lab home (provenance in docs/images/catalog-captures.json).
screenshots:
  - https://raw.githubusercontent.com/nicolasestrem/hermes-model-switcher/<sha>/docs/images/model-switcher-0.3.2-dashboard.png
  - https://raw.githubusercontent.com/nicolasestrem/hermes-model-switcher/<sha>/docs/images/model-switcher-0.3.2-preview.png
  - https://raw.githubusercontent.com/nicolasestrem/hermes-model-switcher/<sha>/docs/images/model-switcher-0.3.2-operation.png
  - https://raw.githubusercontent.com/nicolasestrem/hermes-model-switcher/<sha>/docs/images/model-switcher-0.3.2-catalog.png
  - https://raw.githubusercontent.com/nicolasestrem/hermes-model-switcher/<sha>/docs/images/model-switcher-0.3.2-light.png
  - https://raw.githubusercontent.com/nicolasestrem/hermes-model-switcher/<sha>/docs/images/model-switcher-0.3.2-narrow.png
known_issues:
  - "Couples at runtime to public Hermes seams that are not stability-guaranteed APIs (the /api/model/* dashboard routes, hermes_cli.config, agent.secret_scope, hermes_constants); a host refactor can break the plugin until it re-pins. Missing upstream seams remain open: a compare-and-save write with an expected-revision check (hermes-agent #142339), a public typed write route for non-main slots (#142340), and a dry-run footprint for exact main-slot previews (#142341)."
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

The live pinned commit is the one in
[NousResearch/hermes-agent#141744](https://github.com/NousResearch/hermes-agent/pull/141744);
do not copy this block's `<sha>` placeholders literally. Re-pins update `sha` and
every `<sha>`-pinned image URL together. Keep `version` aligned with the pinned
manifests, rerun validation and disclose the storage/compatibility limits above
in the PR. Historical screenshots do not verify
the final 0.3.2 tree; the `model-switcher-0.3.2-*` captures were re-taken after the
catalog-row fix (the 0.3.0 catalog capture showed each model id twice).

## Why `.hermes-catalog.json` is NOT in this repository

`.hermes-catalog.json` is an **installer-generated convenience copy**, not a source
artifact. Hermes writes it into an installed plugin directory after a catalog
install; authoritative provenance is the `catalog` block of installer-owned
`.install-metadata.json`. A checkout shipping its own copy is not evidence of a
reviewed catalog install. The file is intentionally absent from source.
