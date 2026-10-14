# Technical reference

Implementation and advanced routing details for contributors.

## Dashboard integration

3b. Frontend host contract — the Dashboard entry is a plain IIFE that hands a
   React component to the host registry, exactly as documented in Hermes'
   *Extending the Dashboard* guide:

   - React comes from `window.__HERMES_PLUGIN_SDK__.React`. React is never
     bundled, and **the Dashboard does not set `globalThis.React` /
     `globalThis.ReactDOM`** — a bundle that destructures them dies on load with
     `Cannot destructure property 'useState' of 'q' as it is undefined`.
   - The entry registers synchronously and must not mount itself:
     `window.__HERMES_PLUGINS__.register("hermes-model-switcher", App)`. The
     registered name must equal `dashboard/manifest.json`'s `name`, and the host
     renders the component inside the tab. There is no ReactDOM on the SDK and no
     host-provided root element.
   - Control styling is plugin-owned: the manifest declares `css: "dist/style.css"`,
     built from `ui/src/styles.css` and shipped beside the JS in both checkout and
     assembled package. Tailwind's host reset removes native button/input/select
     chrome; an unstyled control in the DOM is not a usable action. Scoped styles
     supply borders, padding, focus rings and disabled feedback without changing
     other Dashboard tabs. Fixtures load this same asset under the host reset,
     rather than supplying their own control styles.
   - Theme colours follow the Dashboard's background/midground tokens, with legacy
     aliases supported. Sticky headers reject unsafe alpha values and use an opaque
     theme background instead of a universally dark fallback in light themes.
   - API calls use `window.__HERMES_PLUGIN_SDK__.authedFetch` so the host's
     authentication and profile scope are preserved. Bare `fetch` is only a
     standalone/fixture fallback, not the Dashboard transport.
   - `ui/dist/plugin.js` (the standalone layout) keeps a self-mount fallback for
     hosts that provide `globalThis.React`/`ReactDOM` instead — that is what the
     local Playwright fixture uses.

   Both contracts are covered by `npm test`: `tests/fixture-sdk.html` reproduces
   the real shell (SDK + registry globals, no `globalThis.React`) and loads the
   shipped `dashboard/dist/index.js`; `tests/fixture.html` covers the legacy path.


## Advanced routing

Expand **Browse provider models** to search Hermes' provider-scoped catalog, keeping the exact
model ID alongside its provider. IDs are not portable across providers: Nous may
use `deepseek/deepseek-v4.1-flash` while OpenCode Go uses `deepseek-v4.1-flash`.
Refresh requests live discovery, but failed probes and curated/cache fallbacks
remain visibly unproven-fresh. Manual entry stays available for custom routes.

**Choose a model** assigns a provider and model. On a fallback slot, that is an
explicit one-entry chain. **Set fallback sequence** supports several
`provider/model` lines in order. **Reset to default or inherited routing** removes
the override; **Turn off fallback** writes an explicit empty chain, not a disabled
main model. These actions are distinct, and a malformed/missing chain is rejected
instead of silently cleared. Exact-model removal is a separate advanced action.
Speech routes bind the selected backend and its native model field together;
MoA preset rows address their aggregator, preserving reference models and policy.

Auxiliary tasks are derived from the installed Hermes defaults and registered
plugin tasks. Plugin task registration is resolved against **each profile's own
Hermes home** (a context-local Hermes instance, not one machine-global
registry), so two profiles can legitimately show different plugin task rows.
Vision is an auxiliary task, not a specialist speech backend.
Pinning its backend while `agent.image_input_mode` is `auto` makes the main agent
receive a text interpretation even if its main model supports images. Explicit
native/text image modes are unchanged. The separate OpenRouter fallback override
affects auxiliary auto-routing across tasks; it is not a per-task route.
Unset speech backends follow the runtime defaults (local STT / Edge TTS in the
tested runtime). The current Edge TTS backend carries **no configurable model
field**, so its model slot shows no editable key — but the row still supports an
**explicit switch to a supported model-bearing backend**, which then binds that
backend's native model field. "No model key today" is scoped to that model slot;
it is not a claim that every writable field on the row is uneditable.

Apply and Undo refresh configured state automatically; **Refresh state** reloads
external edits. **Last observed** shows **No recorded usage** until actual runtime usage
evidence is available—it is never fabricated from configuration. Saved presets
retain their exact reviewed slots, profiles and billing approval; an apply request
cannot silently broaden that saved policy.

Status and failures are shown beside the action controls as well as announced
through the screen-reader status region. A refused apply preserves the server's
reason, including FastAPI's nested `detail.error` and `detail.reasons`, rather
than displaying only `HTTP 409`. A missing provider connection is a real refusal,
not a successful model switch; connect that provider through Hermes or choose a
connected route. A state-loading authentication failure is separate from a layout
problem: the tab must use the host SDK transport even if the bundle renders.


## Configuration

`HERMES_MODEL_SWITCHER_HERMES_REPO` selects the read-only Hermes source checkout for registry scans (default `~/.hermes/hermes-agent`). `HERMES_MODEL_SWITCHER_PLUGINS_DIR` can expose plugin coverage gaps; it does not grant write access.
