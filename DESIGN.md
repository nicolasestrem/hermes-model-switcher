# Model Switcher design contract

## 1. Atmosphere & Identity
Operational, legible, calm. This is a Hermes Dashboard configuration workspace, not a marketing page. The matrix is the primary workspace, the change rail is secondary. Signature: an explicit selection summary connecting exact profile/slot targets to reviewed changes. Preserve host theme and runtime protections.

The page introduction states the practical benefit in one sentence. Task headings lead with actions: Select your targets, Choose your routing, Preview, Apply. Presets remain a compact reusable-setup strip. The refresh/recovery toolbar separates orientation from editing. Selection counts announce changes politely; model search explains zero matches and how to recover.

## 2. Color
Use host semantic tokens, not a new brand palette: surface --background, text --midground, muted --text-muted-color, border --color-border, focus --color-ring, accent --accent-color. Preserve legacy aliases. Fallbacks: surface #16181d, text #e6e8ee, muted #9aa1ad, border #3a3f48, accent/focus #6f9bff, danger #e5534b, success #3fb950, warning #d29922. Accent is restricted to primary actions, focus and selection, not background floods. Text-backed semantic states; never color alone. Verify AA contrast for actual light/dark host token pairs. Sticky surfaces must resolve opaque colors and refresh with theme changes.

## 3. Typography
Inherit the host sans-serif stack. Technical paths use ui-monospace, SFMono-Regular, Consolas, monospace. Body 14px minimum, secondary text 13px minimum, section titles 16px, page title 22px. Body line-height 1.5. Use weight 400/500/600 and tabular figures for counts. Human-readable labels lead; exact identifiers remain accessible in details. No remote fonts.

## 4. Spacing & Layout
4px base; 4/8/12/16/24 spacing scale. Controls 38px minimum with generous checkbox labels. Wide view: matrix and bounded editor rail (~360px) in one workspace; editor remains before matrix in DOM for action discoverability. Below ~1100px stack a compact editor and matrix; advanced fields use disclosures. At 768/375px controls wrap or stack, profile options are bounded/searchable, no document horizontal overflow. Table owns horizontal scroll and sticky header/profile identity. Do not freeze so many columns that narrow viewports lose route content.

## 5. Components
Profile filter: clearly named 'Profiles shown', show-all reset independent from editing targets. Matrix: select-all matching writable rows, clear selection, selected-profile/slot count and explicit hidden-selection count; read-only reasons visible. Filters use persistent labels and human-readable family/task names; reset affordance and result count. Editor: Choose a model / Set fallback sequence / Reset to default or inherited routing / Turn off fallback; inline explanation and applicability, no silent target exclusion. Exact-model removal is a separate advanced action. Picker retains exact provider ids and manual entry. Preview shows actual before/after and profile/slot refusal identities. Apply gates remain unchanged and explain disabled states. Drawer has summary, close/Escape, focus entry/trap/return. All controls cover hover, focus-visible, active, disabled, loading, empty and error states.

## 6. Motion & Interaction
No decorative motion or smooth-scroll dependency. Optional color transitions <=120ms; disable under prefers-reduced-motion. Preserve selection when filters narrow; display selected targets outside filters and explicitly identify them in preview. Bulk select includes matching writable slots only. Selection/editor changes invalidate review. Filters do not silently add targets. Real requests remain authenticated through the host SDK.

## 7. Depth & Surface
Flat surfaces with 1px borders, 6px radius baseline. No gradients, glass, decorative shadows or dashboard card grid. Use spacing and weight for hierarchy.

## 8. Accessibility Constraints & Accepted Debt
Keyboard-reachable native controls, associated labels and inline descriptions, visible focus, indeterminate bulk checkbox, text status indicators and aria-live action feedback. Dialog drawer manages focus and Escape. Responsive/keyboard/light-dark checks at 1440/768/375px required; no accessibility or performance pass implied by this contract. Runtime coverage is bounded by installed Hermes support; unsupported selectors stay read-only. Preserve preview/apply/readback/undo, stale reviews, exact identities, non-model settings and cost acknowledgment. No live route writes during visual verification; isolate mutation tests.
