# Technical reference

Implementation and advanced routing details for contributors. The 0.3.0 integration
uses public Hermes modules and authenticated Dashboard routes only.

## Dashboard integration

The Dashboard entry is a plain IIFE that registers synchronously:
`window.__HERMES_PLUGINS__.register("hermes-model-switcher", App)`. The registered
name must equal `dashboard/manifest.json`'s `name`; the host mounts the component.
React comes from `window.__HERMES_PLUGIN_SDK__.React`, not a bundled React or an
assumed `globalThis.React`. Self-mounting is a standalone fixture fallback only.

The manifest declares `css: "dist/style.css"`, built from `ui/src/styles.css` and
shipped beside the JS. Scoped styles restore control borders, padding, focus rings
and disabled feedback under the host's reset. Theme colours follow Dashboard
background/midground tokens with legacy aliases; sticky headers require an opaque
theme background. Fixtures must load these same assets, not substitute styles.

Host requests use the Dashboard SDK's `fetchJSON`; plugin requests use its
authenticated transport (`authedFetch`). Bare `fetch` is not the Dashboard transport.
SDK fixtures check registration without global React, but do not establish live
Dashboard authentication or deployment.

## Public host data

For each discovered profile, the UI obtains these payloads through the SDK, with
at most three concurrent requests:

- `GET /api/model/options?profile=<name>&include_unconfigured=1`: provider/model
  options, authentication and pricing evidence. Explicit catalog refresh adds
  `refresh=1`; refresh is the host's job, not proof of successful live discovery.
- `GET /api/model/auxiliary?profile=<name>`: main/auxiliary routes, inheritance and
  profile-specific plugin tasks.
- `GET /api/model/moa?profile=<name>`: MoA presets. The host also exposes
  `PUT /api/model/moa`; plugin MoA slot edits use the backend config path below.

The root profile uses `profile=default`. Failed or unavailable host reads become
`null` with a non-blocking notice, not an empty successful catalog or proof that no
plugin tasks exist. Missing/malformed auxiliary data hides plugin-registered rows
with a warning; built-in inheritance uses a local exact-shape mirror of the host's
layering rules. MoA preset validation uses host preset names when available, then
configured preset names; without either catalog it refuses the edit.

The UI sends a bounded bundle with state, preview and operation requests:

```json
{"host":{"profiles":{"default":{"options":null,"auxiliary":null,"moa":null}}}}
```

`GET /state` remains config-only; `POST /state {host}` enriches the same response.
Host payloads are display, credential and cost evidence, never write-target
allowlists. Only discovered profiles can be mutation targets.

## Model catalog

**Browse model catalog** is a read-only, provider-grouped view of the host options
payload. `dashboard/model_catalog.py` is a pure transform with no Hermes imports;
it does not read provider caches or a registry from disk. Prices come only from
host pricing fields, never inference calls or recorded spend. Missing or pending
pricing stays unknown ("—"), never zero/free; an explicit free flag is required
for `free`. No fetch timestamp is invented.

Plugin `POST /catalog {profile, provider, options, ...filters}` and `POST /models`
transform that host payload. Their old GET forms return HTTP 410 with
`{"ok":false,"reason":"hostCatalogRequired"}`. Missing/malformed options return
`ok:false` with `hostCatalogUnavailable`, never an empty success. These reads do
not write configuration, presets or the operation journal and remain useful in
read-only mode when host evidence is available.

Provider and model dropdowns share this data; an "Other" entry preserves exact-ID
manual entry. IDs are not portable across providers: Nous may use
`deepseek/deepseek-v4.1-flash` while OpenCode Go uses `deepseek-v4.1-flash`.
A listed model is not proof it is routable. Credential and billing policy are
checked for each target profile, and cost remains unknown without evidence.

## Preview, Apply and recovery

Each journal step declares `executor: "host"` or `"backend"`:

- Main assignments and auxiliary assignments that exactly match the public route's
  semantics use `POST /api/model/set?profile=<name>` through the SDK. Requests
  never carry an API key. Main requests omit `base_url` so Hermes resolves it;
  auxiliary endpoint URLs must not carry userinfo or query data.
- Other slots, including cron, delegation, fallback, judge, speech and unsupported
  auxiliary edit shapes, use public `hermes_cli.config.load_config`,
  `require_readable_config_before_write` and `save_config` in the target profile's
  home scope. The adapter serializes its own writes, rechecks revisions immediately
  before saving and verifies readback; it does not use private host locks/helpers.

Main preview shows semantic intent (`model.provider`, `model.default`), not a
predicted exact canonical config diff. Hermes resolves the final assignment and
may change related fields; the exact persisted diff is known only after readback.
A non-empty provider is required. Explicitly unauthenticated host options refuse
the preview; a manually entered model absent from the catalog produces a warning,
with final validation owned by the host write route.

The host-step protocol is:

1. `POST /operations` journals intent and each pre-change config snapshot, executes
   backend steps in order, then rechecks the revision before a host step. It stores
   a one-time token hash and returns `awaiting_host` with
   `hostCall: {opId, seq, token, method, path, query, body}`.
2. The UI executes that call through SDK `fetchJSON`. A host response with
   `confirm_required: true` opens a dialog displaying `confirm_message` as text.
   Approval resends with `confirm_expensive_model: true`; cancellation reports
   `declined`. This host confirmation is separate from plugin billing approval.
3. The UI posts `/operations/{id}/host-result` with `seq`, `token` and outcome
   `ok`, `declined` or `error`, plus numeric HTTP status if available. It never
   forwards host response text. A wrong token yields HTTP 409
   `host_token_mismatch` without changing state.
4. The backend rereads the config. No revision change after claimed success is
   `host_no_write`, not success. Changed configs require semantic readback and a
   foreign-diff check: only paths the selected host route is known to write may
   differ. An unrelated edit yields `recovery_required` / `external_edit`, never
   silent success or blind snapshot restoration over that edit. Other failed or
   declined steps enter the guarded compensation path.
5. Recovery and restart reconciliation evaluate abandoned `awaiting_host` steps
   against the actual store. Undo/compensation of either executor restores the
   saved config through the public backend writer, guarded by the recorded
   post-write revision. A conflicting edit stops automatic restoration.

**Concurrency limit:** the host write routes do not accept an expected revision.
The plugin's pre-call check cannot make the SDK request atomic with that check.
Post-write readback detects foreign edits and requires recovery when they cannot
be attributed safely; it cannot prove that every concurrent edit was observable.
A successful HTTP response alone never establishes a committed operation.

Public operation DTOs are allowlists: no raw configs, snapshots, local paths or
exception strings. Only the current host step's one-time token is exposed.
See [privacy audit](privacy-audit.md) for snapshot sensitivity and storage limits.

## Advanced routing

**Choose a model** assigns a provider and model. On a fallback slot it creates an
explicit one-entry chain. **Set fallback sequence** accepts ordered
`provider/model` lines. **Reset to default or inherited routing** removes an
override; **Turn off fallback** writes an explicit empty chain, not a disabled
main model. Missing/malformed chains are refused rather than silently cleared.
Exact-model removal is a separate advanced action. Speech routes bind the backend
and its native model field together; MoA rows address their aggregator while
preserving reference models and policy.

Vision is an auxiliary task, not a speech backend. Pinning it while
`agent.image_input_mode` is `auto` gives the main agent a text interpretation even
if its model supports images. Explicit native/text modes are unchanged. The
separate OpenRouter fallback override affects auxiliary auto-routing across tasks.

Unset speech backends follow runtime defaults (local STT / Edge TTS in the tested
runtime). Edge TTS has no configurable model field; the row can still explicitly
switch to a supported model-bearing backend. No editable model key does not mean
that every field on that row is uneditable.

Apply/Undo refresh configured state; **Refresh state** reloads external edits.
**Last observed** remains **No recorded usage** without actual usage evidence.
Presets retain their reviewed slots, profiles and billing approval; applying one
cannot silently broaden its policy. Host assignments apply to new sessions, not
hot-swaps of an in-flight chat.

Failures appear beside the controls and in the screen-reader status region.
Sanitized plugin refusals preserve actionable nested `detail.error` /
`detail.reasons`; host response text is not forwarded into the operation journal.
A missing provider connection is a refusal, not a successful model switch.

## Runtime and configuration

`probe_runtime()` is capability-based: required public config, home/profile and
secret-scope seams must import and be callable. The best-effort package version is
informational only; neither a version string nor an install stamp gates writes.
Missing capabilities degrade to read-only. Hosted isolation remains read-only;
the host gates plugin requests, not a duplicate plugin-side backend gate.

The tested reference is Hermes **v0.22.3**, commit
`5b147ab036d6d19131bff8d20701b5001eab844a`. This is a reproducibility pin, not a
minimum or exclusive supported version. Final 0.3.2 verification is recorded
separately in [release readiness](release-readiness.md).

Profiles use public `hermes_constants` identity/home helpers. Config and secret
access enter `set_hermes_home_override` and `agent.secret_scope` together; the
plugin does not open another profile's environment file. Configured key-variable
names are checked for presence through scoped `get_secret`; otherwise credential
evidence comes from the host options' authentication flags, with unknown failing
closed. A configured custom endpoint retains its existing credential policy.

`HERMES_MODEL_SWITCHER_HERMES_REPO` selects the read-only reference source checkout
for source scans and tests (default `~/.hermes/hermes-agent`).
`HERMES_MODEL_SWITCHER_PLUGINS_DIR` exposes coverage gaps, not write access.

One test-only helper, `registry/scanner.py`'s `load_default_config`, loads
`hermes_cli/config_defaults.py` by file path; it is called only by tests, is
inert at runtime, and is invisible to the AST-based private-import gate, so
"public seams only" is a statement about runtime imports, not an absence of
this test-only file-path coupling.

Persistent state lives **only** at
`get_hermes_home()/state/hermes-model-switcher/`. There is no alternate state-dir
setting or fallback. If this location resolves inside plugin source or a Git
worktree, or cannot meet privacy checks, the plugin is read-only.

## Disposable lab helpers

`install-local.sh` and `uninstall-local.sh` require GNU/Linux, Bash and GNU
`readlink -m`; native Windows and macOS are not supported. They refuse the native
live home and its ancestors/descendants. `--replace` is accepted anywhere in the
install arguments.

Replacement/removal preserve complete plugin trees under the lab home's
`.model-switcher-backup.*/plugin` and `.model-switcher-removed.*/plugin`, outside
plugin discovery. The current journal remains separately in the home-owned state
directory; moving a plugin tree does not move or erase that journal. Old plugin
trees may also contain sensitive legacy recovery data. Applied model choices stay
in effect. Inspect exact paths before manually deleting retained data; backups
are never pruned automatically, and deleting journals removes recovery evidence.
