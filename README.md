# Hermes Model Switcher

**Choose your models without hunting through config files.**

Model Switcher is a community plugin for the Hermes Web Dashboard. It brings the
supported model routes in your profiles into one searchable table. Pick a model,
review the changes, then Apply. Save setups as presets and use guarded Undo when
you need to revert.

Hermes keeps model choices in several places: main models, delegation, auxiliary
tasks, judges/reviewers, fallback chains, Mixture of Agents, cron automation and
specialist routes like speech. Model Switcher puts them together so you can see
what is configured and check a change before writing it.

## What you can do

- Inspect supported routes across profiles in one filterable matrix. Configured
  routes stay separate from recorded usage; an open session may still use its old model.
- See what you could switch to in the model catalog: a bird's-eye, provider-grouped
  view of every model the profile can route to, with list prices per 1M tokens, free
  and no-list-price entries marked distinctly, and which slots already use each model.
- Select individual slots or a matching group, pick a provider and then one of
  its models from plain dropdowns (or choose "Other" for an exact pair), then preview the change.
  Filters change the view; checkboxes choose edit targets.
- Save reviewed choices as named presets and reuse them after a fresh preview.
- Check per-profile results in the operation drawer and use revision-guarded Undo.
  If a write fails, the engine attempts to restore completed steps and flags any
  unresolved conflict as `recovery_required`.

Built for the Hermes Dashboard, using its themes and authenticated transport. MIT licensed.

![Model Switcher showing synthetic Hermes profiles in a dark theme](docs/images/model-switcher-dark-1440.png)

*The compiled plugin in a Dashboard SDK fixture with synthetic lab profiles. This
screenshot demonstrates the interface; it is not evidence of live Dashboard
deployment or provider inference.*

![The same interface following the host's light theme](docs/images/model-switcher-light-1440.png)

*The plugin follows the host Dashboard's theme; the light theme is shown here.*

![Model catalog with list prices per 1M tokens](docs/images/model-switcher-catalog-1440.png)

*The provider-grouped catalog in the Dashboard SDK fixture with synthetic lab profiles.
List prices come from Hermes' pricing caches, not from provider probes or recorded spend.*

![Model catalog on a narrow screen](docs/images/model-switcher-catalog-375.png)

*The same catalog on a narrow screen; a fixture render of the compiled plugin, not a
live Dashboard deployment or provider execution.*

![The routing matrix on a narrow screen](docs/images/model-switcher-dark-375.png)

*On a narrow screen the matrix scrolls horizontally inside its own region.*

### In the real Hermes Dashboard

The same plugin, running in an isolated lab with two synthetic profiles (default
and research) and synthetic routes:

![Routing matrix in the real Hermes Dashboard](docs/images/model-switcher-real-dashboard.png)

*Choose the slots you want to change; filters do not select edit targets.*

![A real preview of one synthetic main-model change](docs/images/model-switcher-real-preview.png)

*Review the provider/model diff before any configuration is written.*

![A real committed operation with verified readback](docs/images/model-switcher-real-operation.png)

*Applied and read back. The lab's subsequent Undo restored the original configuration
bytes exactly. These captures show configuration changes, not provider inference.*

## Setup

You need a running Hermes installation with the Web Dashboard and plugin support, and
a Python version compatible with your Hermes release. This plugin declares Python
`>=3.10`; the Hermes runtime it integrates with requires Python `>=3.11`. Linux is the
verification target; macOS and native Windows are unverified. See
[release readiness](docs/release-readiness.md) for evidence and remaining checks.

### From this repository

> This plugin is **not yet in the curated Hermes plugin catalog**, so the bare name
> `hermes plugins install hermes-model-switcher` does not resolve. Use the
> `owner/repo` form below until a catalog entry is merged.

```sh
# Install from the repository (pin an exact commit with --ref <40-char sha> if you want)
hermes plugins install nicolasestrem/hermes-model-switcher

# Enable it when you are ready
hermes plugins enable hermes-model-switcher

# Confirm what is installed
hermes plugins list

# Pull later changes (when the installed source tracks the repository)
hermes plugins update hermes-model-switcher
```

Run `hermes plugins install --help` for the full set of options (`--ref`,
`--enable`/`--no-enable`, `--force`, dependency-consent flags). These are the official
Hermes plugin commands; the plugin does not ship a self-updater.

**Pinned installs do not move with `update`.** Hermes refuses that command rather
than silently replacing your chosen revision. To move a pin, rerun `hermes plugins
install` with the same repository, `--force`, and `--ref` followed by the new commit's
full 40-character SHA. The guarded lab check verified that same-source reinstalls
preserve untracked local state.

The Dashboard bundle is already built and committed. If the installer reports that
build-time Node dependencies were skipped, you do not need to install them to use
these shipped assets; Node.js is needed when rebuilding the UI from source.

### From the built package

```sh
python scripts/build_package.py        # assembles dist-package/ (needs a built UI)
```

`dist-package/` contains the backend, the prebuilt Dashboard assets, and the
documentation, and can be pointed at a Hermes plugin directory. The builder refuses
an existing destination unless you pass `--replace`.

### Manual

Clone or copy this repository into your Hermes plugins directory as
`hermes-model-switcher`, keeping the root `plugin.yaml` and `__init__.py` together,
then enable **hermes-model-switcher** in Dashboard plugin settings and reload the
Dashboard. The repository ships the built Dashboard assets, so Node.js is needed only
when you rebuild the UI yourself.

## Make your first switch

1. **Select your targets.** Narrow the matrix with the **Profiles shown** selector
   and the **Family**, **Task**, **Provider**, **Model**, and **State** filters, then
   select the slots you want to change. Filtering changes the view only; checkboxes
   choose edit targets. The selection summary includes selected slots hidden by filters.
2. **Choose your routing.** Pick a provider, then a model from its dropdown (each
   entry shows its list price), open **Browse model catalog** to compare options, or
   choose **Other** to enter an exact provider/model pair for custom routes. Model IDs belong to their provider,
   so keep the pair together.
3. **Preview changes.** Review the affected profiles and side effects. Resolve blocked
   entries and acknowledge unknown-cost routes when required. Changing the selection
   or routing requires a fresh preview.
4. **Apply and inspect.** Check each profile's readback in the operation drawer, then
   save the setup as a preset for next time.

Favorites open a preset preview before application. **Refresh state** reloads external
edits. Advanced controls cover inherited/default routing, ordered fallback sequences,
and exact-model removal; see [advanced routing](docs/technical-reference.md).

## FAQ

- **Does switching a model here start using it right away?** No. Changes update saved
  configuration; an already-running session keeps its previous routing until a new
  session starts. Configured routing is **not** proof of inference or of recorded
  usage.
- **Does it manage or copy credentials?** No. Credentials stay in Hermes and are read
  through its authenticated routes. If a target profile has no connected credential,
  the change is blocked rather than routed around.
- **What if the model list looks stale or a provider is missing?** Model catalog
  refresh can contact provider services; a catalog result is a listing, not an
  inference test. Missing providers usually mean a missing credential on that profile.
- **Where do the catalog prices come from?** List prices per 1M tokens come from the
  installed Hermes pricing caches and official pricing snapshots — not from provider
  probes, inference, or your recorded spend. They can lag behind provider pricing
  pages; a missing price is shown as unknown, never as free or `$0.00`.
- **Someone edited a config while I was reviewing — what happens?** Each operation
  validates profiles and revisions. Avoid concurrent CLI or editor edits during Apply
  or Undo, and use **Refresh state** to reload external changes.
- **Can I undo anything?** Undo is explicit, per-operation, and revision-guarded: it
  refuses to overwrite conflicting external edits. Disabling or uninstalling the plugin
  does **not** reverse changes already applied.
- **Where does recovery data live?** In a private local SQLite journal outside Git
  worktrees and browser assets, created `0600` inside a `0700` directory. Keep it
  private — snapshots can contain sensitive configuration.
- **What if my saved presets get corrupted?** The original preset store is quarantined
  (kept, not deleted) and valid entries are salvaged with a visible `presetsCorrupt`
  warning, so a damaged file is not silently swallowed and is not silently overwritten.
- **Does it work on a remote / hosted-isolation Dashboard?** Writes require an
  in-process Dashboard. Hosted isolation stays read-only; the plugin does not change
  that setting.
- **Why are some slots read-only?** Unknown model-like keys and unsupported plugin
  selectors remain coverage gaps and never receive write access.

## Limitations

- **Configured ≠ live.** Applying a change writes configuration. It is not evidence
  that a provider accepted it, that inference happened, or that a running session
  adopted it.
- **Not a single global transaction.** Multi-profile application uses a journal and
  compensation; it is not one atomic transaction across all files.
- **Explicit selection only.** Only supported, explicitly selected slots are writable;
  unknown or unsupported slots remain coverage gaps.
- **Prerequisite: connected credentials.** A missing connection blocks the change.
- **Prices are informational.** Catalog list prices can be stale or absent; they are
  not spend estimates, not cost approvals, and unknown cost is never treated as free.
- **Scale.** Large profile sets increase first-request (cold discovery) time; 50
  synthetic profiles took about seven seconds in the lab. See
  [release readiness](docs/release-readiness.md) and
  [backlog](docs/backlog.md).
- **Platform scope.** Linux is verified; macOS and native Windows are unverified.
- **Private Hermes APIs.** Integration uses private Hermes seams and may need updates
  for future Hermes releases.

## Privacy

The plugin does not copy credentials between profiles or rewrite authentication stores,
memories, skills, or transcripts. It uses the Dashboard's authenticated transport.
Before writing, it saves private recovery snapshots in the local journal described
above; public operation responses expose status and outcomes without raw
configurations, snapshots, store paths, or exception strings. Read the
[privacy audit](docs/privacy-audit.md) for scope and limits.

## Recovery

**There is no fleet-wide transaction and no always-undo promise.** Applying across
several profiles is a journalled sequence, not one atomic transaction. When a step
fails, the engine compensates in reverse order only while each profile's revision still
matches; a conflict stops compensation and the operation reports `recovery_required`. A
compensated, partial, or `recovery_required` outcome is reported as `ok=false` with a
fixed failure category and the operation's final state — a success is never claimed for
a fleet that did not fully apply.

**Undo is explicit and per-operation.** It refuses to overwrite conflicting external
edits. After a crash, recovery reconciles recorded intent with the actual files. Keep
recovery data until any outstanding operation is resolved; disabling or uninstalling the
plugin does not reverse changes already applied.

## Develop and contribute

See
[CONTRIBUTING.md](CONTRIBUTING.md)
for dependency setup, the portable vs real-runtime test split, and the CI workflow. In
short:

```sh
python -m pip install -e '.[test]'
npm ci && npm run build
python -m pytest tests/
npm test
python scripts/privacy_guard.py
```

Read the [design contract](DESIGN.md), [usability notes](docs/usability-polish.md),
[technical reference](docs/technical-reference.md),
[verification scope](docs/verification.md), and
[release findings](docs/release-readiness.md). Changes should preserve preview, apply,
readback, and guarded Undo.

## License

[MIT](LICENSE). This is a community project and does not claim official Hermes
endorsement or catalog listing.
