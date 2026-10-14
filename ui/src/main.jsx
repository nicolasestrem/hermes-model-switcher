// Model Switcher tab — T6 UI.
// Uses the host Dashboard's global React and theme CSS variables
// (--hms reads Dashboard theme tokens with sane fallbacks).
import React, { useEffect, useMemo, useRef, useState } from "shims/react.js";
import { createRoot } from "shims/react-dom-client.js";
import { api, ApiError } from "./api.js";
import { resolveOpaqueSurface } from "./theme.js";

const t = (v, fb) => `var(--${v}, ${fb})`;

// The Dashboard paints --midground text over --background. --foreground is
// an overlay token and may be fully transparent; do not use it for text.
// Legacy surface/text aliases remain supported for older hosts and fixtures.
const THEME = {
  accent: t("accent-color", "#6f9bff"),
  bg: "var(--background, var(--surface-color, #16181d))",
  fg: "var(--midground, var(--text-color, #e6e8ee))",
  muted: t("text-muted-color", "#9aa1ad"),
  border: t("border-color", "#2c3038"),
  danger: t("danger-color", "#e5534b"),
  ok: t("success-color", "#3fb950"),
  warn: t("warning-color", "#d29922"),
};

// Theme-reactive opaque colour for the sticky matrix header. Never a
// module-scope snapshot: the host can switch theme (class/profile/light-dark)
// without reloading the plugin, which would leave the header on the previous
// surface colour while its text and borders follow the live variables.
function useOpaqueSurface() {
  const [bg, setBg] = useState(() => resolveOpaqueSurface());
  useEffect(() => {
    if (typeof MutationObserver === "undefined") return undefined;
    const sync = () => setBg(resolveOpaqueSurface());
    const obs = new MutationObserver(sync);
    const opts = {
      attributes: true,
      attributeFilter: ["class", "style", "data-theme", "data-color-mode"],
    };
    obs.observe(document.documentElement, opts);
    if (document.body) obs.observe(document.body, opts);
    return () => obs.disconnect();
  }, []);
  return bg;
}

const FAMILIES = [
  { id: "main", label: "Main" },
  { id: "delegation", label: "Delegation" },
  { id: "auxiliary", label: "Auxiliary" },
  { id: "judge", label: "Judges / Reviewers" },
  { id: "cron", label: "Automation (cron)" },
  { id: "moa", label: "Mixture of Agents" },
  { id: "judges", label: "Judges / Reviewers" },
  { id: "fallback", label: "Fallback" },
  { id: "automation", label: "Automation (cron/overrides)" },
  { id: "specialist", label: "Specialist (speech / embeddings)" },
  { id: "plugin", label: "Plugin selectors" },
];

function familyLabel(id) {
  return (FAMILIES.find((f) => f.id === id) || { label: id }).label;
}

function taskLabel(slot) {
  if (slot.metadata?.label) return slot.metadata.label;
  const key = subfamilyOf(slot);
  const labels = { stt: "Speech-to-text", tts: "Text-to-speech", mcp: "MCP",
    moa_reference: "MoA reference fallback", moa_aggregator: "MoA aggregator fallback",
    openrouter_model: "OpenRouter auxiliary fallback", model: "Model",
    embedding: "Embedding (read-only)" };
  return labels[key] || key.replaceAll("_", " ").replace(/^./, c => c.toUpperCase());
}

function canSelectSlot(slot) {
  // Assignment capability is separate from a currently writable model field:
  // Edge TTS can switch backends without inventing tts.edge.model.
  return Boolean(slot.selectable ?? slot.writable);
}

function scopeSummary(preset) {
  return `${preset.profileIds.length} profile${preset.profileIds.length === 1 ? "" : "s"} · ` +
    preset.families.map((f) => (FAMILIES.find((x) => x.id === f) || { label: f }).label).join(", ");
}

// Runtime state vocabulary. The inventory reports `configured` | `inherited` |
// `default`; older payloads/fixtures may send aliases (`inherit`, `unset`,
// `auto`, `disabled`, `off`). Normalize ONCE here so the matrix filter, the row
// text and the picker's route preview can never disagree about a slot's state.
export const STATE_ORDER = ["configured", "inherited", "unset", "auto", "disabled"];
const STATE_LABELS = {
  configured: "Configured",
  inherited: "Inherited",
  unset: "Unset",
  auto: "Auto",
  disabled: "Disabled",
};

export function normalizeState(raw) {
  const s = String(raw == null ? "" : raw).trim().toLowerCase();
  if (s === "configured" || s === "set") return "configured";
  if (s === "inherited" || s === "inherit") return "inherited";
  if (s === "auto" || s === "automatic") return "auto";
  if (s === "disabled" || s === "off") return "disabled";
  if (s === "unset" || s === "default" || s === "none" || s === "") return "unset";
  return s; // unknown values pass through verbatim — never silently recategorised
}

export function stateLabel(raw) {
  const n = normalizeState(raw);
  return STATE_LABELS[n] || n;
}

// A slot's *subfamily* is its identity below the family: the tail segment of a
// dotted slotId ("auxiliary.goal_judge" -> "goal_judge"), else the slotId. The
// matrix filter is hierarchical — choosing a family narrows the subfamily
// options to those that actually exist under it.
export function subfamilyOf(slot) {
  const id = String((slot && slot.slotId) || "");
  const dot = id.indexOf(".");
  return dot < 0 ? id : id.slice(dot + 1);
}

function routeText(slot) {
  const st = normalizeState(slot.state);
  if (st === "inherited") return "inherit";
  if (st === "disabled") return "disabled";
  if (Array.isArray(slot.chain) && slot.chain.length) {
    return slot.chain.map((route) => typeof route === "string" ? route : `${route.provider || "auto"}/${route.model || "auto"}`).join(" → ");
  }
  if (st === "unset" || st === "auto") {
    return slot.provider || slot.model
      ? `${slot.provider || "auto"}/${slot.model || "auto"}`
      : "unset / auto";
  }
  return `${slot.provider || "auto"}/${slot.model || "auto"}`;
}

// Diff values are arbitrary storage values: scalars, lists (chains) and route
// dicts. `String(value)` would print "[object Object]" for the object shapes,
// hiding exactly the before/after the review must show.
export function formatDiffValue(value) {
  if (value === undefined) return "unset";
  if (value === null) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// ---------- API-driven views ----------

function PresetsBar({ presets, onReview, busy }) {
  return (
    <section aria-label="Favorite presets" style={{ marginBottom: 12 }}>
      <h3 style={{ margin: "4px 0" }}>Favorite presets</h3>
      {presets.length === 0 && (
        <p data-testid="presets-empty" style={{ color: THEME.muted, margin: "4px 0" }}>
          Keep a setup you love. Select slots, preview your changes, then save a preset to reuse them.
        </p>
      )}
      <div role="list" style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {presets.map((p) => (
          <button
            key={p.id}
            className="hms-preset"
            data-preset={p.id}
            disabled={busy}
            onClick={() => onReview(p)}
            title={`Billing policy: ${p.billingPolicy}`}
          >
            <strong>{p.name}</strong>
            <span style={{ color: THEME.muted, marginLeft: 8, fontSize: 13 }}>
              {scopeSummary(p)}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

function ProfileFilter({ profiles, value, onChange }) {
  // Searchable multi-select. `value` is the list of explicitly selected profile
  // ids; an EMPTY list means "all profiles" (the un-narrowed default), so an
  // existing unselected profile is never silently dropped from the matrix.
  const [query, setQuery] = useState("");
  const visible = profiles.filter(
    (p) => !query || (p.name || p.id).toLowerCase().includes(query.toLowerCase()),
  );
  const toggle = (id) => {
    const next = value.includes(id) ? value.filter((x) => x !== id) : [...value, id];
    onChange(next);
  };
  return (
    <div data-testid="profile-filter"
      style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-start" }}>
      <button type="button" aria-pressed={!value.length} onClick={() => onChange([])}>
        All profiles
      </button>
      <input aria-label="Search profiles" placeholder="search profiles…" value={query}
        onChange={(e) => setQuery(e.target.value)} style={{ minWidth: 0 }} />
      <div role="group" aria-label="Profiles" data-testid="profile-options"
        style={{ display: "flex", gap: 8, flexWrap: "wrap", alignSelf: "center", maxHeight: 112, overflow: "auto", width: "100%" }}>
        {visible.map((p) => (
          <label key={p.id} style={{ display: "inline-flex", gap: 4 }}>
            <input type="checkbox"
              aria-label={`Profile ${p.name || p.id}`}
              data-profile-option={p.id}
              checked={value.includes(p.id)}
              onChange={() => toggle(p.id)} />
            {p.name || p.id}
          </label>
        ))}
        {visible.length === 0 && <span style={{ color: THEME.muted }}>No profiles match.</span>}
      </div>
    </div>
  );
}

// The matrix column headers, in order. One extra leading column holds the row
// selection checkbox, so the real column count is these + 1. Detail and
// empty-state rows span ALL columns via COLUMN_COUNT — hard-coding the number
// silently drifted from the header set once already.
const MATRIX_COLUMNS = ["Profile", "Slot", "Family", "State", "Configured route", "Last observed"];
const COLUMN_COUNT = MATRIX_COLUMNS.length + 1; // + selection checkbox column

function Matrix({ profiles, slots, selection, onToggle, onToggleAll, onClearSelection, onExpand, expanded, filters, setFilters }) {
  // Resolved per render-source and refreshed on host theme changes, never a
  // module-scope snapshot (see useOpaqueSurface).
  const stickyBg = useOpaqueSurface();
  const scoped = useMemo(() => {
    const profIds = new Set(filters.profileIds.length ? filters.profileIds : profiles.map((p) => p.id));
    return slots.filter((s) => profIds.has(s.profileId));
  }, [slots, filters.profileIds, profiles]);

  // Hierarchical: the subfamily options are only those that actually exist
  // under the currently selected family (and profiles), so the second dropdown
  // can never offer a subfamily that would empty the table.
  const subfamilies = useMemo(() => {
    const set = new Set();
    for (const s of scoped) {
      if (filters.family !== "all" && s.family !== filters.family) continue;
      set.add(subfamilyOf(s));
    }
    return [...set].sort();
  }, [scoped, filters.family]);

  const filtered = useMemo(() => {
    return scoped.filter((s) => {
      if (filters.family !== "all" && s.family !== filters.family) return false;
      if (filters.subfamily !== "all" && subfamilyOf(s) !== filters.subfamily) return false;
      if (filters.provider && !(s.provider || "").includes(filters.provider)) return false;
      if (filters.model && !(s.model || "").includes(filters.model)) return false;
      if (filters.state !== "all" && normalizeState(s.state) !== filters.state) return false;
      return true;
    });
  }, [scoped, filters]);

  const setFilter = (patch) => setFilters({ ...filters, ...patch });

  const rowKey = (s) => s.profileId + "/" + s.slotId;
  const writableInView = filtered.filter(canSelectSlot);
  const selectedInView = writableInView.filter((s) => selection.has(rowKey(s)));
  const masterChecked = writableInView.length > 0 && selectedInView.length === writableInView.length;
  const masterIndeterminate = !masterChecked && selectedInView.length > 0;
  const masterRef = (el) => { if (el) el.indeterminate = masterIndeterminate; };
  // Bulk select-all only ever targets the WRITABLE rows matching the current
  // filters — a read-only row is never added, even from the header control.
  const onMasterChange = () => {
    const next = new Map(selection);
    if (masterChecked) for (const s of writableInView) next.delete(rowKey(s));
    else for (const s of writableInView) next.set(rowKey(s), s);
    onToggleAll(next);
  };
  // Counts describe actual edit targets, never read-only profiles in the view.
  const selCount = selection.size;
  const selProfileCount = new Set([...selection.values()].map((s) => s.profileId)).size;
  const inViewKeys = new Set(filtered.map(rowKey));
  const hiddenCount = [...selection.keys()].filter((k) => !inViewKeys.has(k)).length;
  const summaryText = `${selCount} slot${selCount === 1 ? "" : "s"} on ` +
    `${selProfileCount} profile${selProfileCount === 1 ? "" : "s"} selected` +
    (hiddenCount > 0 ? ` · ${hiddenCount} hidden by current filters` : "");

  return (
    <section aria-label="Profile model matrix">
      <h3 style={{ margin: "4px 0" }}>Select your targets</h3>
      <p className="hms-section-description">Choose the profile slots you want to change. Filters help you find them; selected targets stay selected.</p>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "baseline", margin: "4px 0" }}>
        <p data-testid="selection-summary" aria-live="polite" style={{ margin: 0 }}>{summaryText}</p>
        <button type="button" data-testid="clear-selection" disabled={selection.size === 0}
          title="Deselect every selected target, including ones hidden by the current filters"
          onClick={() => onClearSelection()}>Clear selection</button>
      </div>
      <div role="group" aria-label="Matrix filters"
        style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
        <div className="hms-profile-scope"><strong>Profiles shown</strong>
        <ProfileFilter profiles={profiles} value={filters.profileIds}
          onChange={(profileIds) => setFilter({ profileIds, subfamily: "all" })} />
        <span className="hms-note">{filters.profileIds.length || profiles.length} of {profiles.length} profiles shown. This filters the view; it does not select changes.</span>
        </div>
        <label className="hms-filter">Family<select aria-label="Filter by family" value={filters.family}
          onChange={(e) => setFilter({ family: e.target.value, subfamily: "all" })}>
          <option value="all">All families</option>
          {FAMILIES.filter((family) => slots.some((slot) => slot.family === family.id)).map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
        </select></label>
        <label className="hms-filter">Task<select aria-label="Filter by subfamily" data-testid="filter-subfamily" value={filters.subfamily}
          onChange={(e) => setFilter({ subfamily: e.target.value })}>
          <option value="all">All tasks</option>
          {subfamilies.map((sf) => <option key={sf} value={sf}>{taskLabel(scoped.find(s => subfamilyOf(s) === sf) || { slotId: sf })}</option>)}
        </select></label>
        <label className="hms-filter">Provider<input aria-label="Filter by provider" placeholder="provider…" value={filters.provider}
          onChange={(e) => setFilter({ provider: e.target.value })} /></label>
        <label className="hms-filter">Model<input aria-label="Filter by model" placeholder="model…" value={filters.model}
          onChange={(e) => setFilter({ model: e.target.value })} /></label>
        <label className="hms-filter">State<select aria-label="Filter by state" value={filters.state}
          onChange={(e) => setFilter({ state: e.target.value })}>
          <option value="all">All states</option>
          {STATE_ORDER.map((s) => <option key={s} value={s}>{STATE_LABELS[s]}</option>)}
        </select></label>
        <button type="button" onClick={() => setFilter({ profileIds: [], family: "all", subfamily: "all", provider: "", model: "", state: "all" })}>Reset filters</button>
      </div>
      <p style={{ color: THEME.muted, fontSize: 13, margin: "4px 0" }}>{filtered.length} matching slots · {filtered.filter(s => !canSelectSlot(s)).length} read-only. Scroll horizontally to inspect routes on narrow screens.</p>
      <div data-testid="matrix-scroll" role="region" aria-label="Scrollable model slots" tabIndex={0} style={{ maxHeight: "70vh", overflow: "auto" }}>
      <table style={{ borderCollapse: "collapse", width: "100%" }}>
        {/* Screen-reader-only caption. The hidden styles are INLINE: the host
            Dashboard does not define `.visually-hidden`, so a class-based hide
            renders the caption as a stray visible line above the table (measured
            on the real host: position static, 961x23 at 1280px wide). */}
        <caption className="visually-hidden"
          style={{ position: "absolute", left: -9999, width: 1, height: 1, overflow: "hidden" }}>
          Model slots per profile with configured and last-observed state
        </caption>
        <thead>
          <tr>
            <th scope="col" style={{
              textAlign: "left", borderBottom: `1px solid ${THEME.border}`, padding: 4,
              position: "sticky", top: 0, left: 0, background: stickyBg, zIndex: 3, width: 32, minWidth: 32,
            }}>
              <input ref={masterRef} type="checkbox" data-testid="select-all"
                aria-label="Select all matching writable slots" checked={masterChecked}
                disabled={!writableInView.length}
                onChange={onMasterChange} />
            </th>
            {MATRIX_COLUMNS.map((h) => (
              <th key={h} scope="col" style={{
                textAlign: "left", borderBottom: `1px solid ${THEME.border}`, padding: 4,
                position: "sticky", top: 0, left: h === "Profile" ? 40 : undefined,
                background: stickyBg, zIndex: h === "Profile" ? 3 : 2,
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {filtered.map((s) => {
            const key = rowKey(s);
            const checked = selection.has(key);
            return (
              <React.Fragment key={key}>
                <tr data-row={key} data-selected={checked}
                  style={{ borderBottom: `1px solid ${THEME.border}` }}>
                  <td data-column="select" style={{ position: "sticky", left: 0, background: stickyBg, zIndex: 1, minWidth: 32 }}>
                    <input
                      type="checkbox"
                      aria-label={`Select ${s.profileId} ${s.slotId}`}
                      checked={checked}
                      disabled={!canSelectSlot(s)}
                      onChange={() => onToggle(key, s)}
                    />
                  </td>
                  <td data-column="profile" data-profile-id={s.profileId} style={{ position: "sticky", left: 40, background: stickyBg, zIndex: 1 }}>
                    {profiles.find((p) => p.id === s.profileId)?.name || s.profileId}
                    {s.requiresModelBackend && (
                      <span className="hms-note" data-testid="backend-switch-note"> Current backend has no model field; choose a model-bearing backend to switch.</span>
                    )}
                    {!canSelectSlot(s) && (
                      <span className="hms-note" data-testid="readonly-reason">
                        {" "}{s.note ? s.note : "Profile is read-only — routing can not be changed on this profile."}
                      </span>
                    )}
                  </td>
                  <td><button className="hms-expand" title={s.slotId} aria-label={`Details for ${s.profileId} ${s.slotId}`} aria-expanded={!!expanded[key]} onClick={() => onExpand(key)}>{taskLabel(s)}</button></td>
                  <td data-column="family" data-family-id={s.family}>{familyLabel(s.family)}</td>
                  <td data-column="state">
                    <span className="hms-badge" data-state={normalizeState(s.state)}>{stateLabel(s.state)}</span>
                  </td>
                  <td data-column="configured">{routeText(s)}</td>
                  <td style={{ color: THEME.muted }} data-column="observed" data-provider={s.provider}
                    data-model={s.model}>
                    {s.lastObserved ? `${s.lastObserved.provider}/${s.lastObserved.model}` : "No recorded usage"}
                  </td>
                </tr>
                {expanded[key] && (
                  <tr data-detail={key}>
                    <td colSpan={COLUMN_COUNT} style={{ padding: "6px 12px", background: THEME.bg }}>
                      <RoutingDetail slot={s} />
                    </td>
                  </tr>
                )}
              </React.Fragment>
            );
          })}
          {filtered.length === 0 && (
            <tr><td colSpan={COLUMN_COUNT} style={{ color: THEME.muted, padding: 16 }}>No slots match these filters. Reset filters to see every profile slot.</td></tr>
          )}
        </tbody>
      </table>
      </div>
    </section>
  );
}

function RoutingDetail({ slot }) {
  const st = normalizeState(slot.state);
  const effective = slot.effectiveRoute;
  const hasDefaultRoute = effective && (
    (typeof effective.model === "string" && effective.model.trim()) ||
    (typeof effective.provider === "string" && effective.provider.trim() && effective.provider.toLowerCase() !== "auto")
  );
  return (
    <div>
      <div>Storage: <code>{slot.storagePath || "config.yaml"}</code> · state: <strong>{stateLabel(slot.state)}</strong>
      </div>
      {effective && (st === "inherited" || (st === "unset" && hasDefaultRoute)) && (
        <div data-testid="inherited-effective-route" style={{ maxWidth: "min(640px, calc(100vw - 72px))" }}>
          {st === "inherited" ? "Inherited route" : "Runtime default"} — effective:
          <code style={{ display: "block", whiteSpace: "normal", overflowWrap: "anywhere" }}>
            {routeText({ ...effective, state: "configured" })}
          </code>
        </div>
      )}
      {slot.protocol && <div>Protocol: <code>{slot.protocol}</code></div>}
      {slot.note && <div style={{ color: THEME.warn }}>{slot.note}</div>}
    </div>
  );
}

// Parse the chain editor textarea. Each non-empty line is "provider/model".
// A line without a "/" (or with an empty side) is INVALID — never coerced into
// an apparently valid entry (a bare string is not a route).
export function parseChain(text) {
  const entries = [];
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const idx = line.indexOf("/");
    if (idx <= 0 || idx === line.length - 1) { entries.push({ invalid: line }); continue; }
    const provider = line.slice(0, idx).trim();
    const model = line.slice(idx + 1).trim();
    if (!provider || !model) { entries.push({ invalid: line }); continue; }
    entries.push({ provider, model });
  }
  return entries;
}

export function chainInvalid(entries) {
  return entries.some((e) => e.invalid !== undefined);
}

function ChangeEditor({
  selection, slots, editor, setEditor, onPreview, onApply, preview, previewInvalid,
  busy, picker, onFetchModels,
}) {
  const selectedSlots = slots.filter((s) => selection.has(s.profileId + "/" + s.slotId));
  const hasSelection = selectedSlots.length > 0;
  const removal = editor.removeExact.trim();
  const exactMatch = (provider, model) => `${provider}/${model}` === removal;
  const removalTargets = selectedSlots.filter(
    (s) => s.writable && normalizeState(s.state) === "configured" && exactMatch(s.provider, s.model),
  );
  const set = (patch) => setEditor({ ...editor, ...patch });
  const chainEntries = parseChain(editor.chainText);
  const chainBad = editor.changeType === "chain" && chainInvalid(chainEntries);
  const writableSelected = selectedSlots.filter(canSelectSlot);
  // Applicability is stated in words: a change type that cannot reach every
  // selected target names the excluded ones instead of silently dropping them
  // from the review.
  const disableApplicable = writableSelected.filter(isChainSlot);
  const disableExcluded = writableSelected.length - disableApplicable.length;
  const scalarSelected = writableSelected.filter(isScalarSlot);
  const scalarOnly = writableSelected.length > 0 && scalarSelected.length === writableSelected.length;
  const mixedScalar = editor.changeType === "set" && scalarSelected.length > 0 && !scalarOnly && editor.provider.trim() !== "openrouter";
  const incompatibleChain = ["chain", "disable"].includes(editor.changeType) && disableExcluded > 0;
  const assignmentOnly = writableSelected.filter(s => s.requiresModelBackend);
  const assignmentMismatch = assignmentOnly.length > 0 && editor.changeType !== "set";
  const applicability = (() => {
    if (!writableSelected.length) {
      return "No writable slot selected — select rows in the matrix to build a change.";
    }
    if (assignmentOnly.length > 0) {
      return `${assignmentOnly.length} selected speech slot(s) have no current model field. Choose a model and an explicit model-bearing backend to switch; resetting, removing or disabling an imaginary model field is not supported.`;
    }
    if (editor.changeType === "set") {
      if (scalarOnly) return "OpenRouter auxiliary fallback — enter the exact model ID only. OpenRouter is implicit; this setting affects the auxiliary auto-chain across tasks.";
      if (mixedScalar) return "The OpenRouter auxiliary fallback can only be combined with routes to OpenRouter. Choose OpenRouter or select these targets separately.";
      return `Applies to ${writableSelected.length} of ${writableSelected.length} selected slots — sets provider and model. Enter New provider and New model, then Preview changes.`;
    }
    if (editor.changeType === "chain") {
      return `Applies to ${disableApplicable.length} of ${writableSelected.length} selected slots — fallback chain slots only; tried in order when earlier routes fail.` + (disableExcluded ? " Deselect non-chain targets to continue; no targets will be silently skipped." : "");
    }
    if (editor.changeType === "unset") {
      return `Applies to ${writableSelected.length} of ${writableSelected.length} selected slots — removes the explicit override. Each slot returns to its runtime default or inherited routing.`;
    }
    // disable: only fallback-chain slots can receive to:[]
    return `Applies to ${disableApplicable.length} of ${writableSelected.length} selected slots — fallback chain slots only.` +
      (disableExcluded > 0
        ? " Deselect non-chain targets to continue; no targets will be silently skipped."
        : "");
  })();

  return (
    <section aria-label="Custom change editor" data-testid="change-editor"
      style={{ marginTop: 0 }}>
      <h3 style={{ margin: "4px 0" }}>Choose your routing</h3>
      <p className="hms-section-description">Set the route, preview the exact changes, then apply when you’re ready.</p>
      {!hasSelection && <p style={{ color: THEME.muted }}>Select slots in the matrix to build a change.</p>}
      <p data-testid="editor-applicability" style={{ color: THEME.muted, margin: "0 0 4px" }}>{applicability}</p>
      {selectedSlots.some(s => s.note || s.metadata?.description) && <ul data-testid="selected-route-notes" className="hms-note">
        {[...new Set(selectedSlots.map(s => s.note || s.metadata?.description).filter(Boolean))].map(note => <li key={note}>{note}</li>)}
      </ul>}
      {selectedSlots.some(s => String(s.protocol).replaceAll("-", "_") === "typed_decision") &&
        <p className="hms-note">These targets require a compatible typed-decision route. Preview validates compatibility; a plain chat model is not interchangeable.</p>}
      <div style={{ display: "grid", gap: 6, maxWidth: "min(640px, 100%)" }}>
        <label>Change type
          <select data-testid="change-type" aria-label="Change type" value={editor.changeType}
            onChange={(e) => set({ changeType: e.target.value })}>
            <option value="set">Choose a model</option>
            <option value="chain">Set fallback sequence</option>
            <option value="unset">Reset to default or inherited routing</option>
            <option value="disable">Turn off fallback</option>
          </select></label>

        {editor.changeType === "set" && (
          <>
            <label>New provider <input value={scalarOnly ? "openrouter" : editor.provider} disabled={scalarOnly}
              onChange={(e) => set({ provider: e.target.value })} aria-label="New provider" /></label>
            <label>New model <input value={editor.model}
              onChange={(e) => set({ model: e.target.value })} aria-label="New model" /></label>
          </>
        )}

        {editor.changeType === "chain" && (
          <>
            <label>Fallback chain entries (one <code>provider/model</code> per line, order preserved)
              <textarea aria-label="Fallback chain entries" data-testid="chain-input"
                value={editor.chainText} rows={3}
                onChange={(e) => set({ chainText: e.target.value })} /></label>
            {chainBad && (
              <div role="alert" data-testid="chain-invalid" style={{ color: THEME.danger }}>
                Each chain line must be <code>provider/model</code>. Fix or remove the malformed line to preview.
              </div>
            )}
          </>
        )}

        {(editor.changeType === "unset" || editor.changeType === "disable") && (
          <p data-testid="typed-intent-note" style={{ color: THEME.muted, margin: 0 }}>
            {editor.changeType === "unset"
              ? "Remove the override, not the feature. Runtime defaults or inherited routing apply."
              : "Disable only this fallback sequence. The main model remains enabled."}
          </p>
        )}

        {/* Advanced actions are collapsed until requested. */}
        <details data-testid="removal-advanced">
          <summary style={{ cursor: "pointer" }}>Advanced — remove an exact provider/model</summary>
          <label>Exact route to remove (<code>provider/model</code>, matches exact identity only)
            <input value={editor.removeExact} onChange={(e) => set({ removeExact: e.target.value })}
              aria-label="Exact provider/model to remove" placeholder="e.g. openai/gpt-4o" /></label>
          {removal && removalTargets.length === 0 && (
            <div role="note" style={{ color: THEME.warn }}>
              No selected configured slot matches exactly “{removal}”. Nothing will be removed.
            </div>
          )}
          {removalTargets.length > 0 && <p role="note">Remove this exact route from {removalTargets.length} selected configured slot(s). Removal takes precedence over a model change for these targets.</p>}
        </details>

        {/* Provider-native model picker: exact ids from the installed catalog,
            never a guessed list. Manual entry stays available above. */}
        <details data-testid="picker-advanced"><summary>Browse provider models</summary>
        <fieldset data-testid="model-picker-controls"
          style={{ border: `1px solid ${THEME.border}`, borderRadius: 6, padding: 8 }}>
          <legend>Model picker (exact catalog ids)</legend>
          <label style={{ display: "block" }}>Provider
            <input value={editor.pickerProvider} onChange={(e) => set({ pickerProvider: e.target.value })}
              style={{ display: "block", marginTop: 4 }} aria-label="Picker provider" placeholder="nous / opencode-go / …" /></label>
          <label style={{ display: "block" }}>Profile scope (optional)
            <input value={editor.pickerProfileId} onChange={(e) => set({ pickerProfileId: e.target.value })}
              style={{ display: "block", marginTop: 4, marginBottom: 6 }} aria-label="Picker profile" placeholder="default" /></label>
          <button data-testid="fetch-models" disabled={busy || picker.loading || !editor.pickerProvider.trim()}
            onClick={() => onFetchModels(false)}>Fetch models</button>
          <button data-testid="fetch-models-refresh" disabled={busy || picker.loading || !editor.pickerProvider.trim()}
            onClick={() => onFetchModels(true)}>Refresh catalog</button>
          {picker.error && (
            <div role="alert" data-testid="picker-error" style={{ color: THEME.danger }}>{picker.error}</div>
          )}
          {picker.loading && <p role="status">Loading model catalog…</p>}
          {picker.data && (
            <div data-testid="model-picker" style={{ marginTop: 6 }}>
              <div data-testid="picker-freshness" style={{ color: THEME.muted, fontSize: 13 }}>
                provider <code>{picker.data.provider}</code> · source {picker.data.source || "unknown"} ·
                {" "}fetched {picker.data.fetchedAt || "unverified"}
                {picker.data.stale ? <> · <strong style={{ color: THEME.warn }}>stale</strong></> : null}
              </div>
              {picker.data.error && (
                <div role="note" data-testid="picker-note" style={{ color: THEME.warn }}>{picker.data.error}</div>
              )}
              <label>Search models <input aria-label="Search models" value={editor.modelSearch || ""}
                onChange={(e) => set({ modelSearch: e.target.value })} /></label>
              <ul style={{ listStyle: "none", padding: 0, maxHeight: 180, overflow: "auto" }}>
                {(picker.data.models || []).filter((model) => `${model.name} ${model.id}`.toLowerCase().includes((editor.modelSearch || "").toLowerCase())).map((m) => (
                  <li key={m.id}>
                    <button data-testid={`model-option-${m.id}`} data-model-id={m.id}
                      onClick={() => set({ provider: picker.data.provider, model: m.id, changeType: "set" })}>
                      {m.name || m.id}
                    </button>
                  </li>
                ))}
                {(picker.data.models || []).length === 0 && (
                  <li style={{ color: THEME.muted }}>This provider returned no models. You can still enter an exact model ID above.</li>
                )}
                {(picker.data.models || []).length > 0 && !(picker.data.models || []).some((model) => `${model.name} ${model.id}`.toLowerCase().includes((editor.modelSearch || "").toLowerCase())) && (
                  <li role="status" style={{ color: THEME.muted }}>No models match your search. Try a shorter name or clear the search.</li>
                )}
              </ul>
            </div>
          )}
        </fieldset>
        </details>

        <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={editor.ackUnknownCost}
            onChange={(e) => set({ ackUnknownCost: e.target.checked })} />
          I acknowledge unknown-cost routes are never treated as free
        </label>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="hms-preview" disabled={!writableSelected.length || busy || chainBad || incompatibleChain || mixedScalar || assignmentMismatch || !buildCustomChanges(editor, selectedSlots).length}
            onClick={() => onPreview()}>
            Preview changes
          </button>
          <button className="hms-apply" data-testid="apply-custom"
            disabled={!preview || previewInvalid || busy || !editor.ackUnknownCost}
            onClick={onApply}
            title={preview ? undefined : "Preview is required before apply"}>
            Apply
          </button>
          <span style={{ color: THEME.muted, alignSelf: "center" }}>
            {busy ? "Request in progress…" : preview
              ? previewInvalid ? "Preview stale — preview your changes again" : `${preview.changes.length} ${preview.changes.length === 1 ? "change" : "changes"} reviewed`
              : !hasSelection ? "Select writable targets first" : incompatibleChain || mixedScalar || assignmentMismatch ? "Resolve the selection mismatch first" : "Preview required"}
          </span>
          {preview && !editor.ackUnknownCost && <span className="hms-note">Acknowledge the cost policy before applying.</span>}
        </div>
      </div>
    </section>
  );
}

function changeHeadline(c) {
  const from = c.from ? `${c.from.provider || "default"}/${c.from.model || "automatic"}` : "unset";
  if (c.action === "remove") return `remove ${from}`;
  const to = c.to;
  if (to === null) return `reset to default / inherited routing — was ${from}`;
  if (Array.isArray(to)) return `turn off fallback — was ${from}`;
  if (to && Array.isArray(to.chain)) {
    if (!to.chain.length) return "disable chain (explicit empty list)";
    const chain = to.chain.map((e) => `${e.provider}/${e.model}`).join(" → ");
    return `set chain [${chain}]`;
  }
  if (to && Object.hasOwn(to, "chain") && to.chain === null) return "unset chain (inherit)";
  return `${from} → ${to ? `${to.provider || (c.slotId === "auxiliary.openrouter_model" ? "openrouter" : "default")}/${to.model || "automatic"}` : "?"}`;
}

// Stable React key for a preview row. Index keys re-mount every list item on
// insert/reorder, discarding focus and state; a change's identity is its
// (profile, slot) pair. The index is only a last-resort fallback when that
// identity is absent from a malformed payload.
function changeKey(c, i) {
  if (c && c.profileId && c.slotId) return `${c.profileId}/${c.slotId}`;
  return "change-" + i;
}

function PreviewPanel({ preview }) {
  if (!preview) return null;
  return (
    <section aria-label="Change preview" data-testid="preview-panel"
      style={{ marginTop: 8, border: `1px solid ${THEME.border}`, padding: 8 }}>
      <h3 style={{ margin: "0 0 6px" }}>Preview ({preview.changes.length} {preview.changes.length === 1 ? "change" : "changes"})</h3>
      <ul>
        {preview.changes.map((c, i) => (
          <li key={changeKey(c, i)}>
            <strong>{c.profileId}</strong> / {c.slotId}: {changeHeadline(c)}
            {c.sideEffects && c.sideEffects.length > 0 && (
              <span style={{ color: THEME.warn }}> · side effects: {c.sideEffects.join(", ")}</span>
            )}
            {/* The engine's per-change typed storage diff (relative config paths
                only). Shown verbatim: the user reviews the ACTUAL writes, not a
                re-derived summary. */}
            {c.diff && c.diff.length > 0 && (
              <ul data-testid="preview-diff" style={{ margin: "2px 0 0", paddingLeft: 18, color: THEME.muted }}>
                {c.diff.map((d, j) => (
                  <li key={d.path || "diff-" + j} data-diff-path={d.path}>
                    <code>{d.path}</code>: {formatDiffValue(d.from)} → {formatDiffValue(d.to)}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
      {preview.blocked && preview.blocked.length > 0 && (
        <div role="alert" style={{ color: THEME.danger }}>
          Blocked: {preview.blocked.map((b) => `${b.profileId}${b.slotId ? "/" + b.slotId : ""}: ${b.reason}`).join("; ")}
        </div>
      )}
    </section>
  );
}

const RESULT_LABEL = {
  applied: { text: "Applied + read back", color: THEME.ok },
  // ``restored``/``compensated`` mean "the previous revision is back". That is a
  // SUCCESS only when the user asked for an undo (operation state ``undone``);
  // after a FAILED APPLY the identical status is a rollback the user never
  // requested, so it is relabelled below and never painted green.
  restored: { text: "Restored (undo verified)", color: THEME.ok },
  compensated: { text: "Restored (undo verified)", color: THEME.ok },
  failed: { text: "FAILED — not verified", color: THEME.danger },
  conflict: { text: "Conflict — recovery required", color: THEME.warn },
  // A known no-write conflict (F1): the step was deliberately left untouched.
  not_written: { text: "Not changed — review again", color: THEME.warn },
};

// Same status, opposite meaning: an apply that rolled back mid-operation.
const ROLLBACK_LABEL = { text: "Rolled back after failed apply", color: THEME.warn };
const COMPENSATED_STATUSES = ["restored", "compensated"];

// The backend sends a SAFE failure category, never an exception string. Render
// descriptive copy where there is a clean mapping; the enum stays on
// ``data-failure-category`` for traceability but is not the user-facing text.
const FAILURE_COPY = {
  write_verification_failed: "Write verification failed — the change did not stick",
  revision_conflict: "Revision conflict — a profile changed after review",
  store_unreadable: "Stored configuration could not be read",
  blocked: "Apply blocked before any write",
  io_error: "Storage error while writing the change",
  step_failed: "A step failed before it could be verified",
};
const STATE_COPY = {
  compensated: "rolled back after a failed apply",
  partial: "partially rolled back after a failed apply",
  recovery_required: "recovery required",
  pending: "pending — not written",
  undone: "undone",
  committed: "committed",
};

// Only a scalar is displayable: an object would render as "[object Object]" and
// the safe failure category / state strings are always scalars.
function scalarText(v) {
  if (typeof v === "string") return v.trim() || null;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return null;
}

function OperationDrawer({ open, operation, onUndo, onClose, undoGuarded, setUndoGuarded, busy }) {
  const ref = useRef(null);
  React.useEffect(() => {
    if (open && ref.current) ref.current.focus();
  }, [open]);
  // Focus trap (dialog pattern): document-level CAPTURE listener so the trap
  // holds even if focus escaped the drawer (e.g. browser-default Tab wrap).
  // Escape closes; Tab/Shift+Tab cycle inside the drawer only.
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      const el = ref.current;
      if (!el) return;
      if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
      if (e.key !== "Tab") return;
      const focusables = Array.from(el.querySelectorAll("button, input, select, textarea, a[href], [tabindex]:not([tabindex=\"-1\"])"))
        .filter((n) => !n.disabled && n.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (!active || !el.contains(active)) {
        e.preventDefault();
        first.focus();
        return;
      }
      if ((!e.shiftKey && active === last) || (e.shiftKey && active === first)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);
  if (!open || !operation) return null;
  // Consume the ACTUAL operation DTO. Status is per-profile (`results[].status`)
  // and the overall outcome is `operation.state` / `operation.final_state`; the
  // backend exposes a SAFE failure category (never an exception string). The
  // drawer must never infer success from anything else — in particular it no
  // longer depends on a per-result ``readback`` field, which only ever existed
  // in the test mock and never in a real response.
  const results = Array.isArray(operation.results) ? operation.results : [];
  const finalState = scalarText(operation.final_state) || scalarText(operation.state) || null;
  // ``undone`` is the ONLY state in which a compensated/restored step is a
  // verified user-requested undo. Every other non-committed state is a failed
  // apply whose rollback must NOT be dressed as success.
  const undoVerified = finalState === "undone";
  const appliedCount = results.filter((r) => r.status === "applied").length;
  // Any step that is not a verified ``applied`` needs attention — UNLESS it is a
  // compensated/restored step of a user-requested undo (state ``undone``). A
  // fully-compensated failed apply therefore reads "0 applied · N need
  // attention" instead of the old, dangerously clean "0 · 0"; an unrecognised
  // future status is counted too rather than silently ignored.
  const attentionCount = results.filter((r) =>
    r.status !== "applied" &&
    !(COMPENSATED_STATUSES.includes(r.status) && undoVerified)
  ).length;
  const failure = scalarText(operation.failure) || results.map((r) => scalarText(r.failure)).find(Boolean) || null;
  const failureCopy = failure ? FAILURE_COPY[failure] || failure : null;
  // A failed apply may carry NO failure category (the rollback itself is the
  // outcome); the terminal state still has to be on screen, so the state line
  // is driven by the state, not by the presence of ``failure``.
  const showStateLine = !failure && !undoVerified && Boolean(finalState) && finalState !== "committed";
  return (
    <aside ref={ref} tabIndex={-1} role="dialog" aria-modal="true"
      aria-label="Operation drawer" data-testid="operation-drawer"
      style={{ position: "fixed", right: 0, top: 0, bottom: 0, width: "min(380px, 100vw)", boxSizing: "border-box", overflow: "auto",
        background: THEME.bg, borderLeft: `1px solid ${THEME.border}`, padding: 12, zIndex: 50, outline: "none" }}>
      <h3>Operation results</h3>
      <p data-testid="operation-summary" data-attention={attentionCount > 0 ? "true" : "false"}
        style={{ color: attentionCount > 0 ? THEME.warn : THEME.muted }}>
        {appliedCount} profiles applied · {attentionCount} need attention
      </p>
      <p className="hms-note">Reference: {operation.id}</p>
      <button type="button" onClick={onClose} aria-label="Close operation drawer">Close</button>
      <p style={{ color: THEME.muted }}>Configured ≠ live: existing sessions may keep their old route.</p>
      {failure && (
        <p data-testid="operation-failure" role="alert" data-failure-category={failure}
          style={{ color: THEME.danger }}>
          {failureCopy}{finalState ? ` · final state: ${finalState}` : ""}
        </p>
      )}
      {showStateLine && (
        <p data-testid="operation-state" data-state={finalState} style={{ color: THEME.warn }}>
          Apply did not commit — {STATE_COPY[finalState] || finalState}
        </p>
      )}
      {undoVerified && (
        <p data-testid="operation-state" data-state="undone" style={{ color: THEME.ok }}>
          Undo verified — previous revisions restored
        </p>
      )}
      <ul style={{ listStyle: "none", padding: 0 }}>
        {results.map((r) => {
          const isCompensated = COMPENSATED_STATUSES.includes(r.status);
          const label = isCompensated && !undoVerified
            ? ROLLBACK_LABEL
            : RESULT_LABEL[r.status] || { text: r.status, color: THEME.fg };
          return (
            <li key={r.profileId} data-testid={`result-${r.profileId}`}
              style={{ borderBottom: `1px solid ${THEME.border}`, padding: "6px 0" }}>
              <strong>{r.profileId}</strong>{" "}
              <span role="status" style={{ color: label.color }} data-status={r.status}>
                {label.text}
              </span>
              <div style={{ fontSize: 13, color: THEME.muted }}>
                rev {r.revisionBefore?.slice(0, 8) || "?"} → {r.revisionAfter?.slice(0, 8) || "?"}
              </div>
            </li>
          );
        })}
      </ul>
      <div style={{ marginTop: 12 }}>
        <label style={{ display: "block", marginBottom: 6 }}>
          <input type="checkbox" checked={undoGuarded} data-testid="undo-guard"
            onChange={(e) => setUndoGuarded(e.target.checked)} />
          {" "}I understand undo restores previous revisions only where they still match
        </label>
        <button className="hms-undo" data-testid="undo-button" disabled={!undoGuarded || busy || operation.undoable === false}
          onClick={onUndo}>Undo operation</button>
      </div>
    </aside>
  );
}

// ---------- App ----------

function mkChange(slot, to, action) {
  return {
    profileId: slot.profileId, slotId: slot.slotId, action: action || "set",
    from: { provider: slot.provider, model: slot.model },
    to: action === "remove" ? null : to,
  };
}

// Typed `to` payload: {provider,model} | {chain:[{provider,model}]} | null
// (inherit) | [] (disabled). A malformed chain yields NO changes.
export function buildCustomChanges(editor, selectedSlots) {
  const writable = selectedSlots.filter(canSelectSlot);
  const removal = editor.removeExact.trim();
  const removalTargets = selectedSlots.filter((s) =>
    s.writable && normalizeState(s.state) === "configured" &&
    `${s.provider}/${s.model}` === removal);
  // A slot can never carry BOTH a set and an exact-removal in one review: the
  // engine coalesces same-slot changes (later wins) and would silently apply
  // only one. The removal owns the slot; the set skips it (server-side
  // duplicate rejection backs this up for API/preset callers).
  const removalKeys = new Set(removalTargets.map((s) => s.profileId + "/" + s.slotId));
  const settable = writable.filter((s) => !removalKeys.has(s.profileId + "/" + s.slotId));
  const type = editor.changeType;
  if (type !== "set" && settable.some(s => s.requiresModelBackend)) return [];
  if (["chain", "disable"].includes(type) && settable.some(s => !isChainSlot(s))) return [];
  let setPart = [];
  if (type === "chain") {
    const entries = parseChain(editor.chainText);
    if (entries.length && !chainInvalid(entries)) {
      setPart = settable.map((s) => mkChange(s,
        { chain: entries.map((e) => ({ provider: e.provider, model: e.model })) }));
    }
  } else if (type === "unset") {
    setPart = settable.map((s) => isChainSlot(s) ? mkChange(s, { chain: null }) : mkChange(s, null, "remove"));
  } else if (type === "disable") {
    setPart = settable.filter(isChainSlot).map((s) => mkChange(s, { chain: [] }));
  } else if (editor.model.trim() && (editor.provider.trim() || settable.every(isScalarSlot))) {
    if (settable.some(isScalarSlot) && settable.some(s => !isScalarSlot(s)) && editor.provider.trim() !== "openrouter") return [];
    const route = { provider: editor.provider.trim(), model: editor.model.trim() };
    setPart = settable.map((s) => mkChange(s, isScalarSlot(s) ? { model: route.model } : isChainSlot(s) ? { chain: [route] } : route));
  }
  const removalPart = removal ? removalTargets.map((s) => mkChange(s, null, "remove")) : [];
  return [...setPart, ...removalPart];
}

function isChainSlot(slot) {
  return /fallback_(chain|providers)$|main_chain$/.test(slot.slotId);
}

function isScalarSlot(slot) {
  return slot.slotId === "auxiliary.openrouter_model";
}

// Preset scope: exact reviewed slot intents in `preset.changes` when the planned
// API supplies them; the legacy family-scoped shape is a fallback only. Never
// auto-expanded beyond the preset's OWN reviewed set.
export function buildPresetChanges(preset, slots) {
  if (Array.isArray(preset.changes) && preset.changes.length) {
    return preset.changes.map((c) => ({ ...c }));
  }
  const out = [];
  const profIds = preset.profileIds || [];
  const families = preset.families || [];
  for (const s of slots) {
    if (!profIds.includes(s.profileId) || !families.includes(s.family)) continue;
    const target = preset.assignments && preset.assignments[s.family];
    if (!target || !canSelectSlot(s)) continue;
    out.push(mkChange(s, { provider: target.provider, model: target.model }));
  }
  return out;
}

const EMPTY_EDITOR = {
  changeType: "set", provider: "", model: "", chainText: "",
  removeExact: "", ackUnknownCost: false, pickerProvider: "", pickerProfileId: "",
};

function App() {
  const [state, setState] = useState(null);
  const [loadError, setLoadError] = useState(null);
  // Measure the actual host pane, not the browser including its sidebar.
  const rootRef = useRef(null);
  const [wideViewport, setWideViewport] = useState(false);
  React.useEffect(() => {
    if (!rootRef.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => setWideViewport(entry.contentRect.width >= 1100));
    observer.observe(rootRef.current);
    return () => observer.disconnect();
  }, [Boolean(state)]);
  // {text, kind}. Severity is explicit, never inferred from the message text:
  // a success line that merely contains the word "failed" must stay neutral.
  const [status, setStatus] = useState({ text: "", kind: "info" });
  const [selection, setSelection] = useState(new Map()); // key -> slot
  const [expanded, setExpanded] = useState({});
  const [filters, setFilters] = useState({ profileIds: [], family: "all", subfamily: "all", provider: "", model: "", state: "all" });
  const [reviewedPreset, setReviewPreset] = useState(null);
  const [preview, setPreview] = useState(null);
  const [previewFor, setPreviewFor] = useState(""); // fingerprint of change-set the preview belongs to
  const [operation, setOperation] = useState(null);
  const [showOperation, setShowOperation] = useState(false);
  const [undoGuarded, setUndoGuarded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [editor, setEditor] = useState(EMPTY_EDITOR);
  const [picker, setPicker] = useState({ data: null, error: null, loading: false });
  const reviewSeq = useRef(0);
  const pickerSeq = useRef(0);
  const loadSeq = useRef(0);

  const announce = (msg, kind = "info") => setStatus({ text: msg, kind });
  // Keep the preset action mounted for focus return, but consume its preview.
  // Applying or undoing must never leave an old review ready to apply again.
  const refreshSilently = async () => {
    reviewSeq.current += 1;
    setPreview(null);
    setPreviewFor("");
    const seq = ++loadSeq.current;
    try {
      const s = await api.loadState();
      if (seq !== loadSeq.current) return;
      setState(s);
      setLoadError(null);
      setSelection((old) => new Map(s.slots.filter((slot) =>
        old.has(slot.profileId + "/" + slot.slotId)).map((slot) =>
        [slot.profileId + "/" + slot.slotId, slot])));
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setState(null);
      setLoadError(e instanceof ApiError ? e.message : String(e));
      announce("Load failed: " + (e instanceof ApiError ? e.message : String(e)), "error");
    }
  };
  const invalidateReviews = () => {
    reviewSeq.current += 1;
    setPreview(null);
    setPreviewFor("");
    setReviewPreset(null);
    // Cancel any review that is still in flight: its response is superseded
    // and will (correctly) leave the busy flag alone, so clearing it here is
    // what keeps "busy" meaning "a review request is actually pending".
    setBusy(false);
  };
  const editEditor = (next) => {
    const { ackUnknownCost: oldAck, ...oldIntent } = editor;
    const { ackUnknownCost: newAck, ...newIntent } = next;
    if (JSON.stringify(oldIntent) !== JSON.stringify(newIntent)) invalidateReviews();
    if (next.pickerProvider !== editor.pickerProvider || next.pickerProfileId !== editor.pickerProfileId) {
      pickerSeq.current += 1;
      setPicker({ data: null, error: null, loading: false });
    }
    setEditor(next);
  };

  const load = async ({ silent = false } = {}) => {
    const seq = ++loadSeq.current;
    invalidateReviews();
    if (!silent) setBusy(true);
    try {
      const s = await api.loadState();
      if (seq !== loadSeq.current) return;
      setState(s);
      setLoadError(null);
      setSelection((old) => new Map(s.slots.filter((slot) =>
        old.has(slot.profileId + "/" + slot.slotId)).map((slot) =>
        [slot.profileId + "/" + slot.slotId, slot])));
      if (!silent) announce("Model switcher state loaded");
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setState(null);
      setLoadError(e instanceof ApiError ? e.message : String(e));
      announce("Load failed: " + (e instanceof ApiError ? e.message : String(e)), "error");
    } finally {
      if (!silent && seq === loadSeq.current) setBusy(false);
    }
  };
  useEffect(() => { load(); }, []);

  const slots = state ? state.slots : [];
  const selectedSlots = slots.filter((slot) => selection.has(slot.profileId + "/" + slot.slotId));
  const customChanges = buildCustomChanges(editor, selectedSlots);
  const customFingerprint = "custom:" + JSON.stringify(customChanges);
  const activePreview = preview && (previewFor === customFingerprint ||
    (reviewedPreset && previewFor === "preset:" + reviewedPreset.id)) ? preview : null;
  const cleanReview = activePreview && activePreview.changes.length > 0 &&
    !(activePreview.blocked && activePreview.blocked.length);
  const canApply = Boolean(cleanReview && !busy && (reviewedPreset || editor.ackUnknownCost));

  const runPreview = async (changes, fingerprint) => {
    if (!changes.length) return;
    const seq = ++reviewSeq.current;
    setPreview(null);
    setPreviewFor("");
    setBusy(true);
    try {
      const p = await api.preview(changes);
      if (seq !== reviewSeq.current) return;
      setPreview(p.preview);
      setPreviewFor(fingerprint);
      // a non-preset preview means the change-set no longer belongs to the
      // reviewed preset — drop its context so apply never pairs them
      if (!fingerprint.startsWith("preset:")) setReviewPreset(null);
      announce(`Preview ready: ${p.preview.changes.length} change(s)`);
    } catch (e) {
      if (seq !== reviewSeq.current) return;
      setPreview(null);
      setPreviewFor("");
      announce("Preview failed: " + (e instanceof ApiError ? e.message : String(e)), "error");
    } finally {
      // A SUPERSEDED response must not clear the busy flag while a newer
      // review is still in flight (the lifecycle stays honest; the gates
      // would still prevent a wrong write, but Apply would flicker enabled).
      if (seq === reviewSeq.current) setBusy(false);
    }
  };

  const reviewPreset = async (preset) => {
    invalidateReviews();
    setReviewPreset(preset);
    setPreview(null);
    setPreviewFor("");
    await runPreview(buildPresetChanges(preset, slots), "preset:" + preset.id);
  };

  const fetchModels = async (refresh) => {
    const seq = ++pickerSeq.current;
    setPicker({ data: null, error: null, loading: true });
    try {
      const data = await api.getModels({ provider: editor.pickerProvider,
        profileId: editor.pickerProfileId || (filters.profileIds.length === 1 ? filters.profileIds[0] : "default"), refresh });
      if (seq === pickerSeq.current) setPicker({ data, error: null, loading: false });
    } catch (e) {
      // Mirror the load path's safety: a non-Error rejection has no `.message`,
      // so `e.message` would render the literal "undefined" in the picker alert.
      if (seq === pickerSeq.current) {
        setPicker({ data: null, error: e instanceof ApiError ? e.message : String(e), loading: false });
      }
    }
  };

  const applyPreview = async () => {
    if (!canApply) return;
    setBusy(true);
    try {
      const payload = reviewedPreset
        ? { presetId: reviewedPreset.id, previewId: activePreview.id, allowUnknownCost: editor.ackUnknownCost }
        : { previewId: activePreview.id, allowUnknownCost: editor.ackUnknownCost };
      const op = await api.apply(payload);
      setOperation(op.operation);
      openDrawer();
      setUndoGuarded(false);
      await refreshSilently();
      // ok === true only for committed/undone. A non-success apply still carries
      // the operation DTO (per-profile statuses + safe failure category +
      // final_state), so render the drawer either way; the announcement stays
      // neutral (the per-profile statuses carry the severity) and never claims a
      // success it did not get.
      const statuses = op.operation.results.map((r) => `${r.profileId} ${r.status}`).join(", ");
      const outcome = op.ok === true ? "" : ` · ${op.operation.final_state || "incomplete"}` +
        (op.operation.failure ? ` (${op.operation.failure})` : "");
      announce(`Operation ${op.operation.id} finished: ${statuses}${outcome}`);
    } catch (e) {
      announce("Apply failed: " + (e instanceof ApiError ? e.message : String(e)), "error");
    } finally {
      setBusy(false);
    }
  };

  const saveAsPreset = async () => {
    if (!cleanReview || !saveName.trim()) return;
    setBusy(true);
    try {
      const p = await api.savePreset({ name: saveName.trim(), changes: activePreview.changes,
        allowUnknownCost: editor.ackUnknownCost, billingPolicy: "reviewed" });
      announce(`Preset saved: ${p.preset.name}`);
      await load({ silent: true });
    } catch (e) {
      announce("Save preset failed: " + (e instanceof ApiError ? e.message : String(e)), "error");
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!operation || operation.undoable === false || !undoGuarded || busy) return;
    setBusy(true);
    try {
      const op = await api.undo(operation.id);
      setOperation(op.operation);
      setUndoGuarded(false);
      await refreshSilently();
      // A conflicted undo returns ok:false WITH the operation DTO
      // (recovery_required); render it and say so plainly instead of "Undo
      // failed" for an outcome the user can actually see in the drawer.
      const statuses = op.operation.results.map((r) => `${r.profileId} ${r.status}`).join(", ");
      const outcome = op.ok === true ? "" : ` · ${op.operation.final_state || "incomplete"}` +
        (op.operation.failure ? ` (${op.operation.failure})` : "");
      announce(`Undo finished: ${statuses}${outcome}`);
    } catch (e) {
      announce("Undo failed: " + (e instanceof ApiError ? e.message : String(e)), "error");
    } finally {
      setBusy(false);
    }
  };

  const toggle = (key, slot) => {
    const next = new Map(selection);
    if (next.has(key)) next.delete(key); else next.set(key, slot);
    setSelection(next);
    invalidateReviews();
  };
  const toggleAll = (next) => {
    setSelection(next);
    invalidateReviews();
  };
  const clearSelection = () => {
    setSelection(new Map());
    invalidateReviews();
  };
  // Drawer focus management: remember who opened it so Escape/Close can hand
  // focus back (dialog pattern), and reuse the same entry point for undo.
  const drawerOpener = useRef(null);
  const openDrawer = () => {
    drawerOpener.current = document.activeElement;
    setShowOperation(true);
  };
  const closeDrawer = () => {
    setShowOperation(false);
    const el = drawerOpener.current;
    drawerOpener.current = null;
    const target = el?.isConnected && !el.disabled ? el : rootRef.current?.querySelector('[data-testid="refresh-state"]');
    target?.focus();
  };

  if (loadError) {
    return (
      <div className="hms-root" style={{ padding: 12, color: THEME.fg }}>
        <div aria-live="polite" role="status" data-testid="live-status"
          style={{ position: "absolute", left: -9999 }}>{status.text}</div>
        <div role="alert" data-testid="load-error"
          style={{ color: THEME.danger, marginBottom: 8 }}>
          Load failed: {loadError}
        </div>
        <button onClick={() => load()}>Retry</button>
      </div>
    );
  }
  if (!state) return (
    <div className="hms-root" style={{ padding: 12, color: THEME.fg }} role="status">
      Loading model switcher…
    </div>
  );

  return (
    <div ref={rootRef} className="hms-root" style={{ color: THEME.fg, padding: 12, fontFamily: "inherit" }}>
      <div aria-live="polite" role="status" data-testid="live-status"
        style={{ position: "absolute", left: -9999 }}>{status.text}</div>
      <header className="hms-page-header">
        <h2>Model Switcher</h2>
        <p>Choose models across your Hermes profiles. Review every change. Reuse your setup with presets.</p>
      </header>
      {state.presetsCorrupt && <p role="alert" style={{ color: THEME.warn }}>
        A damaged preset store was saved aside for recovery. Its presets are not loaded; recover them before replacing saved presets.
      </p>}
      {state.profiles.some(p => p.inventoryWarnings?.length) && <div role="alert" data-testid="inventory-warnings" className="hms-note">
        <strong>Some runtime selectors could not be discovered.</strong>
        <ul>{state.profiles.flatMap(p => (p.inventoryWarnings || []).map((warning, i) => <li key={`${p.id}/${i}`}>{p.name || p.id}: {warning}</li>))}</ul>
      </div>}
      <PresetsBar presets={state.presets} onReview={reviewPreset} busy={busy} />
      <div className="hms-workspace-tools">
        <button data-testid="refresh-state" disabled={busy} onClick={() => load()}>Refresh state</button>
        {operation && !showOperation && <button onClick={openDrawer}>Show last operation</button>}
        <span className="hms-note">Configuration changes apply after review. Existing sessions may keep their current model.</span>
      </div>

      {reviewedPreset && (
        <section aria-label="Preset review" data-testid="preset-review"
          style={{ border: `1px solid ${THEME.border}`, padding: 8, marginBottom: 8 }}>
          <h3 style={{ margin: "4px 0" }}>Review preset: {reviewedPreset.name}</h3>
          <p>Targets: {scopeSummary(reviewedPreset)} · billing policy: <strong>{reviewedPreset.billingPolicy}</strong></p>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button className="hms-apply-preset" data-testid="apply-preset"
              disabled={!canApply || previewFor !== "preset:" + reviewedPreset.id}
              onClick={applyPreview}>Apply preset</button>
            <span style={{ color: THEME.muted }}>{activePreview ? "reviewed — one click applies exactly this" : busy ? "previewing…" : "Review this preset again before applying"}</span>
          </div>
        </section>
      )}

      {/* Wide view: matrix + bounded editor rail in one workspace; below the
          breakpoint it stacks (rail above matrix). The rail stays BEFORE the
          matrix in the DOM for action discoverability (DESIGN §4). */}
      <div className={`hms-workspace${wideViewport ? " hms-wide" : ""}`}>
        {/* Action surface: the only click targets the user needs sit above the
            fold, BEFORE the (potentially ~130-row) profile matrix, so the table
            can never push the flow controls off-screen. */}
        <div data-testid="action-surface">
        {/* The aria-live region above is screen-reader only. Every action here
            reports through it — so a refused apply (409 from the canonical
            seam) or a failed preview used to be silent on screen: the button
            just did nothing. Mirror the same message visibly, next to the
            buttons that trigger it. */}
        {status.text && (
          <p data-testid="visible-status"
            style={{
              margin: "0 0 8px", fontSize: 13,
              color: status.kind === "error" ? THEME.danger : THEME.muted,
            }}>
            {status.text}
          </p>
        )}
        <ChangeEditor
          selection={selection} slots={slots}
          editor={editor} setEditor={editEditor} picker={picker} onFetchModels={fetchModels}
          onPreview={() => runPreview(customChanges, customFingerprint)}
          onApply={applyPreview}
          preview={activePreview && previewFor.startsWith("custom:") ? activePreview : null}
          previewInvalid={!cleanReview}
          busy={busy}
        />
        <PreviewPanel preview={activePreview} />

        {previewFor.startsWith("custom:") && activePreview && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
            <input aria-label="Save as preset name" placeholder="Preset name" value={saveName}
              onChange={(e) => setSaveName(e.target.value)}
              style={{ minWidth: 0 }} />

            <button className="hms-save-preset" data-testid="save-preset"
              disabled={!saveName.trim() || !cleanReview || busy} onClick={saveAsPreset}>Save as preset</button>

          </div>
        )}
      </div>

      <Matrix
        profiles={state.profiles} slots={slots} selection={selection} onToggle={toggle}
        onToggleAll={toggleAll} onClearSelection={clearSelection}
        expanded={expanded} onExpand={(k) => setExpanded({ ...expanded, [k]: !expanded[k] })}
        filters={filters} setFilters={(next) => {
          // Filters change the VIEW only: selection survives (hidden targets
          // stay selected and are counted), but a stale review is invalidated.
          invalidateReviews();
          setFilters(next);
        }}
      />
      </div>

      <OperationDrawer open={showOperation} operation={operation} onClose={closeDrawer} onUndo={undo} undoGuarded={undoGuarded}
        setUndoGuarded={setUndoGuarded} busy={busy} />
    </div>
  );
}

export function mount(el) {
  createRoot(el).render(<App />);
}

// --- host wiring: the Dashboard contract first, legacy self-mount second -----
//
// Dashboard (docs: extending-the-dashboard, "Load lifecycle"): the host exposes
// window.__HERMES_PLUGIN_SDK__ / window.__HERMES_PLUGINS__, appends this file with
// a plain <script>, and expects the IIFE to synchronously register a component —
// the HOST mounts it inside the tab. A dashboard plugin must never create its own
// root: there is no ReactDOM on the SDK and no host-provided root element.
// Registering at module scope is inside the host's 2s registration window.
//
// Standalone/legacy host (no registry present — e.g. this repo's Playwright
// harness, which sets globalThis.React/ReactDOM): self-mount into our own element.
export const PLUGIN_NAME = "hermes-model-switcher";

const HOST = typeof window !== "undefined" ? window.__HERMES_PLUGINS__ : undefined;

if (HOST && typeof HOST.register === "function") {
  HOST.register(PLUGIN_NAME, App);
} else {
  function autoMount() {
    const el = document.querySelector('[data-plugin-root="hermes-model-switcher"]');
    if (el) mount(el);
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", autoMount);
    else autoMount();
  }
}
