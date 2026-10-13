// Model Switcher tab — T6 UI.
// Uses the host Dashboard's global React and theme CSS variables
// (--hms reads Dashboard theme tokens with sane fallbacks).
import React, { useEffect, useMemo, useState } from "shims/react.js";
import { createRoot } from "shims/react-dom-client.js";
import { api, ApiError } from "./api.js";

const t = (v, fb) => `var(--${v}, ${fb})`;

const THEME = {
  accent: t("accent-color", "#6f9bff"),
  bg: t("surface-color", "#16181d"),
  fg: t("text-color", "#e6e8ee"),
  muted: t("text-muted-color", "#9aa1ad"),
  border: t("border-color", "#2c3038"),
  danger: t("danger-color", "#e5534b"),
  ok: t("success-color", "#3fb950"),
  warn: t("warning-color", "#d29922"),
};

const FAMILIES = [
  { id: "main", label: "Main" },
  { id: "delegation", label: "Delegation" },
  { id: "auxiliary", label: "Auxiliary" },
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

function routeText(slot) {
  if (slot.state === "inherit") return "inherit";
  if (slot.state === "unset") return "unset / auto";
  return `${slot.provider}/${slot.model}`;
}

// ---------- API-driven views ----------

function PresetsBar({ presets, onReview, busy }) {
  return (
    <section aria-label="Favorite presets" style={{ marginBottom: 12 }}>
      <h3 style={{ margin: "4px 0" }}>Favorite presets</h3>
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

function Matrix({ profiles, slots, selection, onToggle, onExpand, expanded, filters, setFilters }) {
  const filtered = useMemo(() => {
    const profIds = new Set(filters.profileIds.length ? filters.profileIds : profiles.map((p) => p.id));
    return slots.filter((s) => {
      if (!profIds.has(s.profileId)) return false;
      if (filters.family !== "all" && s.family !== filters.family) return false;
      if (filters.provider && !(s.provider || "").includes(filters.provider)) return false;
      if (filters.model && !(s.model || "").includes(filters.model)) return false;
      if (filters.state !== "all" && s.state !== filters.state) return false;
      return true;
    });
  }, [slots, filters, profiles]);

  const rowKey = (s) => s.profileId + "/" + s.slotId;

  return (
    <section aria-label="Profile model matrix">
      <h3 style={{ margin: "4px 0" }}>Profiles × model slots</h3>
      <div role="group" aria-label="Matrix filters" style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
        <select aria-label="Filter by family" value={filters.family}
          onChange={(e) => setFilters({ ...filters, family: e.target.value })}>
          <option value="all">All families</option>
          {FAMILIES.map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
        </select>
        <input aria-label="Filter by provider" placeholder="provider…" value={filters.provider}
          onChange={(e) => setFilters({ ...filters, provider: e.target.value })} />
        <input aria-label="Filter by model" placeholder="model…" value={filters.model}
          onChange={(e) => setFilters({ ...filters, model: e.target.value })} />
        <select aria-label="Filter by state" value={filters.state}
          onChange={(e) => setFilters({ ...filters, state: e.target.value })}>
          <option value="all">All states</option>
          <option value="configured">Configured</option>
          <option value="inherit">Inherited</option>
          <option value="unset">Unset / auto</option>
        </select>
      </div>
      <table style={{ borderCollapse: "collapse", width: "100%" }}>
        <caption className="visually-hidden">Model slots per profile with configured and last-observed state</caption>
        <thead>
          <tr>
            {["", "Profile", "Slot", "Family", "Configured route", "Last observed"].map((h) => (
              <th key={h} scope="col" style={{ textAlign: "left", borderBottom: `1px solid ${THEME.border}`, padding: 4 }}>{h}</th>
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
    </section>
  );
}

function RoutingDetail({ slot }) {
  return (
    <div>
      <div>Storage: <code>{slot.storagePath || "config.yaml"}</code> · state: <strong>{slot.state}</strong>
        {slot.state === "inherit" && <> (effective: <code>{routeText({ ...slot, state: "configured" })}</code>)</>}
      </div>
      {slot.protocol && <div>Protocol: <code>{slot.protocol}</code></div>}
      {slot.note && <div style={{ color: THEME.warn }}>{slot.note}</div>}
    </div>
  );
}

function ChangeEditor({ selection, slots, onPreview, onApply, preview, previewInvalid, busy }) {
  const [targetProvider, setTargetProvider] = useState("");
  const [targetModel, setTargetModel] = useState("");
  const [removeExact, setRemoveExact] = useState("");
  const [ackUnknownCost, setAckUnknownCost] = useState(false);

  const selectedSlots = slots.filter((s) => selection.has(s.profileId + "/" + s.slotId));
  const hasSelection = selectedSlots.length > 0;  const removal = removeExact.trim();
  const exactMatch = (provider, model) => `${provider}/${model}` === removal;

  const removalTargets = selectedSlots.filter((s) =>
    s.writable && s.state === "configured" && exactMatch(s.provider, s.model));
  const setTargets = selectedSlots.filter((s) => s.writable && targetProvider && targetModel);

  const changes = [
    ...(targetProvider && targetModel
      ? selectedSlots.filter((s) => s.writable).map((s) => ({
          profileId: s.profileId, slotId: s.slotId, action: "set",
          from: { provider: s.provider, model: s.model },
          to: { provider: targetProvider, model: targetModel },
        }))
      : []),
    ...(removal
      ? removalTargets.map((s) => ({
          profileId: s.profileId, slotId: s.slotId, action: "remove",
          from: { provider: s.provider, model: s.model }, to: null,
        }))
      : []),
  ];

  return (
    <section aria-label="Custom change editor" style={{ marginTop: 16, borderTop: `1px solid ${THEME.border}`, paddingTop: 8 }}>
      <h3 style={{ margin: "4px 0" }}>Custom change / exact-model removal</h3>
      {!hasSelection && <p style={{ color: THEME.muted }}>Select slots in the matrix to build a change.</p>}
      <div style={{ display: "grid", gap: 6, maxWidth: 640 }}>
          <label>New provider <input value={targetProvider} onChange={(e) => setTargetProvider(e.target.value)}
            aria-label="New provider" /></label>
          <label>New model <input value={targetModel} onChange={(e) => setTargetModel(e.target.value)}
            aria-label="New model" /></label>
          <label>Exact route to remove (<code>provider/model</code>, matches exact identity only)
            <input value={removeExact} onChange={(e) => setRemoveExact(e.target.value)}
              aria-label="Exact provider/model to remove" placeholder="e.g. openai/gpt-4o" /></label>
          {removal && removalTargets.length === 0 && (
            <div role="note" style={{ color: THEME.warn }}>
              No selected configured slot matches exactly “{removal}”. Nothing will be removed.
            </div>
          )}
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={ackUnknownCost} onChange={(e) => setAckUnknownCost(e.target.checked)} />
            I acknowledge unknown-cost routes are never treated as free
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="hms-preview" disabled={!changes.length || busy}
              onClick={() => onPreview(changes)}>
              Preview changes
            </button>
            <button className="hms-apply" data-testid="apply-custom"
              disabled={!preview || previewInvalid || busy || !ackUnknownCost}
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

function PreviewPanel({ preview }) {
  if (!preview) return null;
  return (
    <section aria-label="Change preview" data-testid="preview-panel"
      style={{ marginTop: 8, border: `1px solid ${THEME.border}`, padding: 8 }}>
      <h4 style={{ margin: "0 0 6px" }}>Preview ({preview.changes.length} changes)</h4>
      <ul>
        {preview.changes.map((c, i) => (
          <li key={i}>
            <strong>{c.profileId}</strong> / {c.slotId}: {c.action === "remove"
              ? `remove ${c.from.provider}/${c.from.model}`
              : `${c.from.provider || "unset"}/${c.from.model || "auto"} → ${c.to.provider}/${c.to.model}`}
            {c.sideEffects && c.sideEffects.length > 0 && (
              <span style={{ color: THEME.warn }}> · side effects: {c.sideEffects.join(", ")}</span>
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

function OperationDrawer({ operation, onUndo, undoGuarded, setUndoGuarded, busy }) {
  if (!operation) return null;
  return (
    <aside aria-label="Operation drawer" data-testid="operation-drawer"
      style={{ position: "fixed", right: 0, top: 0, bottom: 0, width: 380, overflow: "auto",
        background: THEME.bg, borderLeft: `1px solid ${THEME.border}`, padding: 12, zIndex: 50 }}>
      <h3>Operation {operation.id}</h3>
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
        <button className="hms-undo" data-testid="undo-button" disabled={!undoGuarded || busy}
          onClick={onUndo}>Undo operation</button>
      </div>
    </aside>
  );
}

// ---------- App ----------

function App() {
  const [state, setState] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [status, setStatus] = useState("");
  const [selection, setSelection] = useState(new Map()); // key -> slot
  const [expanded, setExpanded] = useState({});
  const [filters, setFilters] = useState({ profileIds: [], family: "all", provider: "", model: "", state: "all" });
  const [reviewedPreset, setReviewPreset] = useState(null);
  const [preview, setPreview] = useState(null);
  const [previewFor, setPreviewFor] = useState(""); // fingerprint of change-set the preview belongs to
  const [operation, setOperation] = useState(null);
  const [undoGuarded, setUndoGuarded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveName, setSaveName] = useState("");
  

  const announce = (msg) => setStatus(msg);

  const load = async () => {
    setBusy(true);
    try {
      const s = await api.loadState();
      setState(s);
      setLoadError(null);
      announce("Model switcher state loaded");
    } catch (e) {
      setState(null);
      setLoadError(e instanceof ApiError ? e.message : String(e));
      announce("Load failed: " + (e instanceof ApiError ? e.message : String(e)));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { load(); }, []);

  const slots = state ? state.slots : [];
  const selectionKey = [...selection.keys()].sort().join("|");

  const runPreview = async (changes, fingerprint) => {
    setBusy(true);
    try {
      const p = await api.preview(changes);
      setPreview(p.preview);
      setPreviewFor(fingerprint);
      // a non-preset preview means the change-set no longer belongs to the
      // reviewed preset — drop its context so apply never pairs them
      if (!fingerprint.startsWith("preset:")) setReviewPreset(null);
      announce(`Preview ready: ${p.preview.changes.length} change(s)`);
    } catch (e) {
      setPreview(null);
      setPreviewFor("");
      announce("Preview failed: " + (e instanceof ApiError ? e.message : String(e)));
    } finally {
      setBusy(false);
    }
  };

  const reviewPreset = async (preset) => {
    setReviewPreset(preset);
    setPreview(null);
    setPreviewFor("");
    const changes = [];
    for (const s of slots) {
      if (!preset.profileIds.includes(s.profileId) || !preset.families.includes(s.family)) continue;
      const target = preset.assignments && preset.assignments[s.family];
      if (!target || !s.writable) continue;
      changes.push({
        profileId: s.profileId, slotId: s.slotId, action: "set",
        from: { provider: s.provider, model: s.model },
        to: { provider: target.provider, model: target.model },
      });
    }
    await runPreview(changes, "preset:" + preset.id + ":" + changes.length);
  };

  const applyPreview = async () => {
    if (!preview) return;
    setBusy(true);
    try {
      const payload = reviewedPreset && previewFor.startsWith("preset:" + reviewedPreset.id + ":")
        ? { presetId: reviewedPreset.id, previewId: preview.id }
        : { previewId: preview.id };
      const op = await api.apply(payload);
      setOperation(op.operation);
      setUndoGuarded(false);
      announce(`Operation ${op.operation.id} finished: ` +
        op.operation.results.map((r) => `${r.profileId} ${r.status}`).join(", "));
    } catch (e) {
      announce("Apply failed: " + (e instanceof ApiError ? e.message : String(e)));
    } finally {
      setBusy(false);
    }
  };

  const saveAsPreset = async () => {
    if (!preview || !saveName.trim()) return;
    setBusy(true);
    try {
      const p = await api.savePreset({ name: saveName.trim(), changes: preview.changes, billingPolicy: "reviewed" });
      announce(`Preset saved: ${p.preset.name}`);
      await load();
    } catch (e) {
      announce("Save preset failed: " + (e instanceof ApiError ? e.message : String(e)));
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!operation || !undoGuarded) return;
    setBusy(true);
    try {
      const op = await api.undo(operation.id);
      setOperation(op.operation);
      setUndoGuarded(false);
      announce("Undo finished: " + op.operation.results.map((r) => `${r.profileId} ${r.status}`).join(", "));
    } catch (e) {
      announce("Undo failed: " + (e instanceof ApiError ? e.message : String(e)));
    } finally {
      setBusy(false);
    }
  };

  const toggle = (key, slot) => {
    const next = new Map(selection);
    if (next.has(key)) next.delete(key); else next.set(key, slot);
    setSelection(next);
    setPreview(null); setPreviewFor(""); // any selection change invalidates a prior preview
  };

  if (loadError) {
    return (
      <div style={{ padding: 12, color: THEME.fg }}>
        <div aria-live="polite" role="status" data-testid="live-status"
          style={{ position: "absolute", left: -9999 }}>{status}</div>
        <div role="alert" data-testid="load-error" style={{ color: THEME.danger, marginBottom: 8 }}>
          Load failed: {loadError}
        </div>
        <button onClick={load}>Retry</button>
      </div>
    );
  }
  if (!state) return <div style={{ padding: 12, color: THEME.fg }} role="status">Loading model switcher…</div>;

  return (
    <div className="hms-root" style={{ color: THEME.fg, padding: 12, fontFamily: "inherit" }}>
      <div aria-live="polite" role="status" data-testid="live-status"
        style={{ position: "absolute", left: -9999 }}>{status}</div>
      <h2>Model Switcher</h2>
      <PresetsBar presets={state.presets} onReview={reviewPreset} busy={busy} />

      {reviewedPreset && (
        <section aria-label="Preset review" data-testid="preset-review"
          style={{ border: `1px solid ${THEME.border}`, padding: 8, marginBottom: 8 }}>
          <h3 style={{ margin: "4px 0" }}>Review preset: {reviewedPreset.name}</h3>
          <p>Targets: {scopeSummary(reviewedPreset)} · billing policy: <strong>{reviewedPreset.billingPolicy}</strong></p>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button className="hms-apply-preset" data-testid="apply-preset"
              disabled={!preview || !previewFor.startsWith("preset:" + reviewedPreset.id + ":") || busy}
              onClick={applyPreview}>Apply preset</button>
            <span style={{ color: THEME.muted }}>{preview ? "reviewed — one click applies exactly this" : "previewing…"}</span>
          </div>
        </section>
      )}

      <Matrix
        profiles={state.profiles} slots={slots} selection={selection} onToggle={toggle}
        expanded={expanded} onExpand={(k) => setExpanded({ ...expanded, [k]: !expanded[k] })}
        filters={filters} setFilters={setFilters}
      />

      <ChangeEditor
        selection={selection} slots={slots}
        onPreview={(changes) => runPreview(changes, "custom:" + selectionKey + ":" + JSON.stringify(changes))}
        onApply={applyPreview}
        preview={preview && previewFor.startsWith("custom:") ? preview : null}
        previewInvalid={false}
        busy={busy}
      />
      <PreviewPanel preview={preview} />

      {previewFor.startsWith("custom:") && preview && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
          <input aria-label="Save as preset name" placeholder="Preset name" value={saveName}
            onChange={(e) => setSaveName(e.target.value)} />
          <button className="hms-save-preset" data-testid="save-preset"
            disabled={!saveName.trim() || busy} onClick={saveAsPreset}>Save as preset</button>
          <button className="hms-apply" data-testid="apply-from-drawer"
            disabled={busy} onClick={applyPreview}>Apply reviewed changes</button>
        </div>
      )}

      <OperationDrawer operation={operation} onUndo={undo} undoGuarded={undoGuarded}
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
