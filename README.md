# Hermes Model Switcher

**Your profiles. Your models. One clear view.**

Choose models across your Hermes profiles, review the changes, and reuse your setup with presets. Model Switcher is a community plugin for the Hermes Web Dashboard that brings scattered model settings into one searchable workspace.

- **See the whole setup:** main models, auxiliary tasks, delegation, fallbacks, speech, and other supported routes across profiles.
- **Change exactly what you select:** pick individual slots or a matching group, then preview before applying.
- **Save your favorite setups:** reuse reviewed provider/model choices with named presets.
- **Know what happened:** inspect per-profile readback and use guarded Undo when you need to recover.

Built for the Hermes Dashboard, with its themes and authentication. MIT licensed.

![Model Switcher showing synthetic Hermes profiles in a dark theme](docs/images/model-switcher-dark-1440.png)

*The compiled plugin in a Dashboard SDK fixture with synthetic lab profiles. This screenshot demonstrates the interface; it is not evidence of live Dashboard deployment or provider inference.*

## Setup

You need a running Hermes installation with the Web Dashboard and plugin support. The plugin needs a Python version compatible with your Hermes release. Linux is the verification target; macOS and native Windows are unverified. See [release readiness](docs/release-readiness.md) for evidence and remaining checks.

1. Clone or copy this repository into your Hermes plugins directory as `hermes-model-switcher`. Keep the root `plugin.yaml` and `__init__.py` together.
2. Enable **hermes-model-switcher** in Dashboard plugin settings and reload the Dashboard.
3. Open **Model Switcher**.

The repository includes built Dashboard assets. Node.js 18+ is needed only when rebuilding the UI. You can also install the directory produced by `python scripts/build_package.py`; it includes the backend, assets, and documentation.

## Make your first switch

1. **Select targets.** Filter by profile, family, slot, or state. Select the slots you want to change. Filtering changes the view; checkboxes choose edit targets. The selection summary includes selected slots hidden by filters.
2. **Choose routing.** Browse provider models or enter a custom provider/model pair. Model IDs belong to their provider, so keep the pair together.
3. **Preview changes.** Review the affected profiles and side effects. Resolve blocked entries and acknowledge unknown-cost routes when required. Changing the selection or routing requires a fresh preview.
4. **Apply and inspect.** Check each profile's readback in the operation drawer. Save the setup as a preset for next time.

Favorites open a preset preview before application. **Refresh state** reloads external edits. Advanced controls cover inherited/default routing, ordered fallback sequences, and exact-model removal; see [advanced routing](docs/technical-reference.md).

## Limitations

- Writes require an **in-process Dashboard**. Hosted isolation remains read-only; the plugin does not change that setting.
- Changes update saved configuration. Existing chat sessions may keep their previous routing until a new session starts. Configured routing is not proof of inference or recorded usage.
- Each operation validates profiles and revisions. Avoid concurrent CLI or editor changes during Apply or Undo. Multi-profile application uses a journal and compensation; it is not a single atomic transaction across all files.
- Only supported, explicitly selected slots are writable. Unknown model-like keys and unsupported plugin selectors remain coverage gaps.
- Credentials must already be connected through Hermes for each target profile. A missing connection blocks the change.

## Privacy

The plugin does not copy credentials between profiles or rewrite authentication stores, memories, skills, or transcripts. It uses the Dashboard's authenticated transport.

Before writing, it saves private recovery snapshots in a local SQLite journal outside Git worktrees and browser assets. Snapshots can contain sensitive configuration; keep that journal private. Public operation responses expose status and outcomes without raw configurations, snapshots, store paths, or exception strings. Model catalog refresh can contact provider services; a catalog result is not an inference test.

## Recovery

**Undo is explicit and guarded.** It refuses to overwrite conflicting external edits. Failed operations attempt compensation in reverse order; unresolved conflicts report `recovery_required`. After a crash, recovery reconciles recorded intent with actual files. Disabling or uninstalling the plugin does not reverse previous changes. Keep recovery data until you have resolved any outstanding operation.

## Develop and contribute

Use an isolated Hermes lab home for write tests. Do not point test seeding or installation scripts at active profiles.

```sh
npm install
npm run build
python -m pytest tests/
npm test
python scripts/privacy_guard.py
python scripts/build_package.py
```

Install Python test dependencies with `python -m pip install '.[test]'` in a dedicated environment compatible with your Hermes checkout. Runtime integration tests also need that checkout and its dependencies; fixture tests alone do not prove live compatibility.

The package builder refuses an existing destination unless you pass `--replace`; validate the destination before requesting replacement. Install/uninstall helpers are for disposable labs only. Installation refuses an existing plugin unless a third `--replace` argument is supplied. Replacement and uninstall preserve the old directory in a private backup under the lab plugins directory, including recovery data. The helpers always refuse the native live home and its descendants.

Before sharing changes, run the [privacy guard](scripts/privacy_guard.py) and inspect your diff for private data. Optional local Git hooks are installed by `bash scripts/install_hooks.sh`. Report bugs with a minimal reproduction, Hermes version, and sanitized verification output; never attach credentials, full configurations, or recovery journals.

Read the [design contract](DESIGN.md), [usability notes](docs/usability-polish.md), [technical reference](docs/technical-reference.md), and [release findings](docs/release-readiness.md). Changes should preserve preview, apply, readback, and guarded Undo.

## License

[MIT](LICENSE). This is a community project, without a claim of official Hermes endorsement.

Compatibility note: integration uses private Hermes APIs and can require updates for future Hermes releases. Large profile sets increase discovery time: the first request for 50 synthetic profiles took about seven seconds in the lab. See the release-readiness report for measurements and exact verification scope.
