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
  { id: "fallback", label: "Fallback / MoA" },
  { id: "automation", label: "Automation (cron/overrides)" },
  { id: "specialist", label: "Specialist (vision/embeddings/speech)" },
  { id: "plugin", label: "Plugin selectors" },
];

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

// ---------- API-driven views ----------

function PresetsBar({ presets, onReview, busy }) {
  return (
    <section aria-label="Favorite presets" style={{ marginBottom: 12 }}>
      <h3 style={{ margin: "4px 0" }}>Favorite presets</h3>
      {presets.length === 0 && (
        <p data-testid="presets-empty" style={{ color: THEME.muted, margin: "4px 0" }}>
          No presets saved yet — select slots below, preview a change, then save it as a preset.
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
            <span style={{ color: THEME.muted, marginLeft: 8, fontSize: "0.85em" }}>
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
        {value.length ? `${value.length} selected · show all` : "All profiles"}
      </button>
      <input aria-label="Search profiles" placeholder="search profiles…" value={query}
        onChange={(e) => setQuery(e.target.value)} style={{ minWidth: 0 }} />
      <div role="group" aria-label="Profiles" data-testid="profile-options"
        style={{ display: "flex", gap: 8, flexWrap: "wrap", alignSelf: "center" }}>
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

function Matrix({ profiles, slots, selection, onToggle, onExpand, expanded, filters, setFilters }) {
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

  const rowKey = (s) => s.profileId + "/" + s.slotId;

  const setFilter = (patch) => setFilters({ ...filters, ...patch });

  return (
    <section aria-label="Profile model matrix">
      <h3 style={{ margin: "4px 0" }}>Profiles × model slots</h3>
      <div role="group" aria-label="Matrix filters"
        style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
        <ProfileFilter profiles={profiles} value={filters.profileIds}
          onChange={(profileIds) => setFilter({ profileIds, subfamily: "all" })} />
        <select aria-label="Filter by family" value={filters.family}
          onChange={(e) => setFilter({ family: e.target.value, subfamily: "all" })}>
          <option value="all">All families</option>
          {FAMILIES.filter((family) => slots.some((slot) => slot.family === family.id)).map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
        </select>
        <select aria-label="Filter by subfamily" data-testid="filter-subfamily" value={filters.subfamily}
          onChange={(e) => setFilter({ subfamily: e.target.value })}>
          <option value="all">All subfamilies</option>
          {subfamilies.map((sf) => <option key={sf} value={sf}>{sf}</option>)}
        </select>
        <input aria-label="Filter by provider" placeholder="provider…" value={filters.provider}
          onChange={(e) => setFilter({ provider: e.target.value })} />
        <input aria-label="Filter by model" placeholder="model…" value={filters.model}
          onChange={(e) => setFilter({ model: e.target.value })} />
        <select aria-label="Filter by state" value={filters.state}
          onChange={(e) => setFilter({ state: e.target.value })}>
          <option value="all">All states</option>
          {STATE_ORDER.map((s) => <option key={s} value={s}>{STATE_LABELS[s]}</option>)}
        </select>
      </div>
      <p style={{ color: THEME.muted, fontSize: "0.85em", margin: "4px 0" }}>Scroll the table horizontally to inspect routes on narrow screens.</p>
      <div data-testid="matrix-scroll" role="region" aria-label="Scrollable model slots" tabIndex={0} style={{ maxHeight: "55vh", overflow: "auto" }}>
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
            {["", "Profile", "Slot", "Family", "Configured route", "Last observed"].map((h) => (
              <th key={h} scope="col" style={{
                textAlign: "left", borderBottom: `1px solid ${THEME.border}`, padding: 4,
                position: "sticky", top: 0, background: stickyBg, zIndex: 1,
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
                <tr data-row={key} style={{ borderBottom: `1px solid ${THEME.border}` }}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${s.profileId} ${s.slotId}`}
                      checked={checked}
                      disabled={!s.writable}
                      onChange={() => onToggle(key, s)}
                    />
                  </td>
                  <td>{profiles.find((p) => p.id === s.profileId)?.name || s.profileId}
                    {!s.writable && <em style={{ color: THEME.muted }}> (read-only)</em>}
                  </td>
                  <td><button className="hms-expand" aria-expanded={!!expanded[key]} onClick={() => onExpand(key)}>{s.slotId}</button></td>
                  <td>{s.family}</td>
                  <td data-column="configured">{routeText(s)}</td>
                  <td style={{ color: THEME.muted }} data-column="observed">
                    {s.lastObserved ? `${s.lastObserved.provider}/${s.lastObserved.model}` : "unobserved"}
                  </td>
                </tr>
                {expanded[key] && (
                  <tr data-detail={key}>
                    <td colSpan={6} style={{ padding: "6px 12px", background: THEME.bg }}>
                      <RoutingDetail slot={s} />
                    </td>
                  </tr>
                )}
              </React.Fragment>
            );
          })}
          {filtered.length === 0 && (
            <tr><td colSpan={6} style={{ color: THEME.muted, padding: 8 }}>No slots match the current filters.</td></tr>
          )}
        </tbody>
      </table>
      </div>
    </section>
  );
}

function RoutingDetail({ slot }) {
  const st = normalizeState(slot.state);
  return (
    <div>
      <div>Storage: <code>{slot.storagePath || "config.yaml"}</code> · state: <strong>{stateLabel(slot.state)}</strong>
        {st === "inherited" && slot.effectiveRoute && <> (effective: <code>{routeText({ ...slot.effectiveRoute, state: "configured" })}</code>)</>}
      </div>
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
  const writableSelected = selectedSlots.filter((s) => s.writable);

  return (
    <section aria-label="Custom change editor" data-testid="change-editor"
      style={{ marginTop: 16, borderTop: `1px solid ${THEME.border}`, paddingTop: 8 }}>
      <h3 style={{ margin: "4px 0" }}>Custom change / exact-model removal</h3>
      {!hasSelection && <p style={{ color: THEME.muted }}>Select slots in the matrix to build a change.</p>}
      <div style={{ display: "grid", gap: 6, maxWidth: 640 }}>
        <label>Change type
          <select aria-label="Change type" value={editor.changeType}
            onChange={(e) => set({ changeType: e.target.value })}>
            <option value="set">Set route (provider + model)</option>
            <option value="chain">Set typed chain</option>
            <option value="unset">Unset (inherit)</option>
            <option value="disable">Disable (empty chain)</option>
          </select></label>

        {editor.changeType === "set" && (
          <>
            <label>New provider <input value={editor.provider}
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
              ? "Sends to:null — the slot inherits its parent (never a disabled chain)."
              : "Sends to:[] — the slot's chain is explicitly disabled (distinct from inherit)."}
          </p>
        )}

        <label>Exact route to remove (<code>provider/model</code>, matches exact identity only)
          <input value={editor.removeExact} onChange={(e) => set({ removeExact: e.target.value })}
            aria-label="Exact provider/model to remove" placeholder="e.g. openai/gpt-4o" /></label>
        {removal && removalTargets.length === 0 && (
          <div role="note" style={{ color: THEME.warn }}>
            No selected configured slot matches exactly “{removal}”. Nothing will be removed.
          </div>
        )}

        {/* Provider-native model picker: exact ids from the installed catalog,
            never a guessed list. Manual entry stays available above. */}
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
              <div data-testid="picker-freshness" style={{ color: THEME.muted, fontSize: "0.85em" }}>
                provider <code>{picker.data.provider}</code> · source {picker.data.source || "unknown"} ·
                {" "}fetched {picker.data.fetchedAt || "?"}
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
                  <li style={{ color: THEME.muted }}>No models reported.</li>
                )}
              </ul>
            </div>
          )}
        </fieldset>

        <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={editor.ackUnknownCost}
            onChange={(e) => set({ ackUnknownCost: e.target.checked })} />
          I acknowledge unknown-cost routes are never treated as free
        </label>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="hms-preview" disabled={!writableSelected.length || busy || chainBad || !buildCustomChanges(editor, selectedSlots).length}
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
            {preview
              ? previewInvalid ? "Preview stale — re-run preview" : `${preview.changes.length} change(s) reviewed`
              : "Preview required"}
          </span>
        </div>
      </div>
    </section>
  );
}

function changeHeadline(c) {
  if (c.action === "remove") return `remove ${c.from ? `${c.from.provider}/${c.from.model}` : ""}`;
  const to = c.to;
  if (to === null) return `unset (inherit) — was ${c.from ? `${c.from.provider}/${c.from.model}` : "unset"}`;
  if (Array.isArray(to)) return `disable chain (to:[]) — was ${c.from ? `${c.from.provider}/${c.from.model}` : "unset"}`;
  if (to && Array.isArray(to.chain)) {
    if (!to.chain.length) return "disable chain (explicit empty list)";
    const chain = to.chain.map((e) => `${e.provider}/${e.model}`).join(" → ");
    return `set chain [${chain}]`;
  }
  if (to && Object.hasOwn(to, "chain") && to.chain === null) return "unset chain (inherit)";
  const from = c.from ? `${c.from.provider || "unset"}/${c.from.model || "auto"}` : "unset";
  return `${from} → ${to ? `${to.provider}/${to.model}` : "?"}`;
}

function PreviewPanel({ preview }) {
  if (!preview) return null;
  return (
    <section aria-label="Change preview" data-testid="preview-panel"
      style={{ marginTop: 8, border: `1px solid ${THEME.border}`, padding: 8 }}>
      <h3 style={{ margin: "0 0 6px" }}>Preview ({preview.changes.length} {preview.changes.length === 1 ? "change" : "changes"})</h3>
      <ul>
        {preview.changes.map((c, i) => (
          <li key={i}>
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
                  <li key={j} data-diff-path={d.path}>
                    <code>{d.path}</code>: {String(d.from)} → {String(d.to)}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
      {preview.blocked && preview.blocked.length > 0 && (
        <div role="alert" style={{ color: THEME.danger }}>
          Blocked: {preview.blocked.map((b) => `${b.profileId}: ${b.reason}`).join("; ")}
        </div>
      )}
    </section>
  );
}

const RESULT_LABEL = {
  applied: { text: "Applied + read back", color: THEME.ok },
  restored: { text: "Restored (undo verified)", color: THEME.ok },
  compensated: { text: "Restored (undo verified)", color: THEME.ok },
  failed: { text: "FAILED — not verified", color: THEME.danger },
  conflict: { text: "Conflict — recovery required", color: THEME.warn },
};

function OperationDrawer({ operation, onUndo, onClose, undoGuarded, setUndoGuarded, busy }) {
  if (!operation) return null;
  return (
    <aside aria-label="Operation drawer" data-testid="operation-drawer"
      style={{ position: "fixed", right: 0, top: 0, bottom: 0, width: "min(380px, 100vw)", boxSizing: "border-box", overflow: "auto",
        background: THEME.bg, borderLeft: `1px solid ${THEME.border}`, padding: 12, zIndex: 50 }}>
      <h3>Operation {operation.id}</h3>
      <button type="button" onClick={onClose} aria-label="Close operation drawer">Close</button>
      <p style={{ color: THEME.muted }}>Configured ≠ live: existing sessions may keep their old route.</p>
      <ul style={{ listStyle: "none", padding: 0 }}>
        {operation.results.map((r) => {
          const label = RESULT_LABEL[r.status] || { text: r.status, color: THEME.fg };
          return (
            <li key={r.profileId} data-testid={`result-${r.profileId}`}
              style={{ borderBottom: `1px solid ${THEME.border}`, padding: "6px 0" }}>
              <strong>{r.profileId}</strong>{" "}
              <span role="status" style={{ color: label.color }} data-status={r.status}>
                {r.readback && r.readback.ok === false
                  ? "FAILED — readback not verified"
                  : label.text}
              </span>
              <div style={{ fontSize: "0.85em", color: THEME.muted }}>
                rev {r.revisionBefore?.slice(0, 8) || "?"} → {r.revisionAfter?.slice(0, 8) || "?"}
                {r.error && <span style={{ color: THEME.danger }}> · {r.error}</span>}
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
  const writable = selectedSlots.filter((s) => s.writable);
  const removal = editor.removeExact.trim();
  const removalTargets = selectedSlots.filter((s) =>
    s.writable && normalizeState(s.state) === "configured" &&
    `${s.provider}/${s.model}` === removal);
  const type = editor.changeType;
  let setPart = [];
  if (type === "chain") {
    const entries = parseChain(editor.chainText);
    if (entries.length && !chainInvalid(entries)) {
      setPart = writable.map((s) => mkChange(s,
        { chain: entries.map((e) => ({ provider: e.provider, model: e.model })) }));
    }
  } else if (type === "unset") {
    setPart = writable.map((s) => isChainSlot(s) ? mkChange(s, { chain: null }) : mkChange(s, null, "remove"));
  } else if (type === "disable") {
    setPart = writable.filter(isChainSlot).map((s) => mkChange(s, { chain: [] }));
  } else if (editor.provider && editor.model) {
    const route = { provider: editor.provider, model: editor.model };
    setPart = writable.map((s) => mkChange(s, isChainSlot(s) ? { chain: [route] } : route));
  }
  const removalPart = removal ? removalTargets.map((s) => mkChange(s, null, "remove")) : [];
  return [...setPart, ...removalPart];
}

function isChainSlot(slot) {
  return /fallback_(chain|providers)$|main_chain$/.test(slot.slotId);
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
    if (!target || !s.writable) continue;
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
  const invalidateReviews = () => {
    reviewSeq.current += 1;
    setPreview(null);
    setPreviewFor("");
    setReviewPreset(null);
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
      setBusy(false);
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
      if (seq === pickerSeq.current) setPicker({ data: null, error: e.message, loading: false });
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
      setShowOperation(true);
      setUndoGuarded(false);
      await load({ silent: true });
      announce(`Operation ${op.operation.id} finished: ` +
        op.operation.results.map((r) => `${r.profileId} ${r.status}`).join(", "));
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
      setShowOperation(true);
      setUndoGuarded(false);
      await load({ silent: true });
      announce("Undo finished: " + op.operation.results.map((r) => `${r.profileId} ${r.status}`).join(", "));
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

  if (loadError) {
    return (
      <div className="hms-root" style={{ padding: 12, color: THEME.fg }}>
        <div aria-live="polite" role="status" data-testid="live-status"
          style={{ position: "absolute", left: -9999 }}>{status.text}</div>
        <div role="alert" data-testid="load-error"
          style={{ color: THEME.danger, marginBottom: 8 }}>
          Load failed: {loadError}
        </div>
        <button onClick={load}>Retry</button>
      </div>
    );
  }
  if (!state) return (
    <div className="hms-root" style={{ padding: 12, color: THEME.fg }} role="status">
      Loading model switcher…
    </div>
  );

  return (
    <div className="hms-root" style={{ color: THEME.fg, padding: 12, fontFamily: "inherit" }}>
      <div aria-live="polite" role="status" data-testid="live-status"
        style={{ position: "absolute", left: -9999 }}>{status.text}</div>
      <h2>Model Switcher</h2>
      {state.presetsCorrupt && <p role="alert" style={{ color: THEME.warn }}>
        A damaged preset store was saved aside for recovery. Its presets are not loaded; recover them before replacing saved presets.
      </p>}
      <PresetsBar presets={state.presets} onReview={reviewPreset} busy={busy} />
      <button data-testid="refresh-state" disabled={busy} onClick={() => load()}>Refresh state</button>
      {operation && !showOperation && <button onClick={() => setShowOperation(true)}>Show last operation</button>}

      {reviewedPreset && (
        <section aria-label="Preset review" data-testid="preset-review"
          style={{ border: `1px solid ${THEME.border}`, padding: 8, marginBottom: 8 }}>
          <h3 style={{ margin: "4px 0" }}>Review preset: {reviewedPreset.name}</h3>
          <p>Targets: {scopeSummary(reviewedPreset)} · billing policy: <strong>{reviewedPreset.billingPolicy}</strong></p>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button className="hms-apply-preset" data-testid="apply-preset"
              disabled={!canApply || previewFor !== "preset:" + reviewedPreset.id}
              onClick={applyPreview}>Apply preset</button>
            <span style={{ color: THEME.muted }}>{preview ? "reviewed — one click applies exactly this" : "previewing…"}</span>
          </div>
        </section>
      )}

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
              margin: "0 0 8px", fontSize: "0.9em",
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
        expanded={expanded} onExpand={(k) => setExpanded({ ...expanded, [k]: !expanded[k] })}
        filters={filters} setFilters={(next) => {
          invalidateReviews();
          setSelection(new Map());
          setFilters(next);
        }}
      />

      <OperationDrawer operation={showOperation ? operation : null} onClose={() => setShowOperation(false)} onUndo={undo} undoGuarded={undoGuarded}
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
