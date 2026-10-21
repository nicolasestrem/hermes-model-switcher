// Model Switcher tab — T6 UI.
// Uses the host Dashboard's global React and theme CSS variables
// (--hms reads Dashboard theme tokens with sane fallbacks).
import React, { useEffect, useMemo, useRef, useState } from "shims/react.js";
import { createRoot } from "shims/react-dom-client.js";
import { api, ApiError, hostFailureText, hostStatusOf, loadHostBundle, validateHostCall } from "./api.js";
import { resolveOpaqueSurface } from "./theme.js";

const t = (v, fb) => `var(--${v}, ${fb})`;

// The Dashboard paints --midground text over --background. --foreground is
// an overlay token and may be fully transparent; do not use it for text.
// Legacy surface/text aliases remain supported for older hosts and fixtures.
const THEME = {
  accent: "var(--color-ring, var(--accent-color, #6f9bff))",
  bg: "var(--background, var(--surface-color, #16181d))",
  fg: "var(--midground, var(--text-color, #e6e8ee))",
  muted: t("text-muted-color", "#9aa1ad"),
  border: "var(--color-border, var(--border-color, #2c3038))",
  danger: "var(--danger-color, var(--hms-danger-text, #e5534b))",
  ok: "var(--success-color, var(--hms-success-text, #3fb950))",
  warn: "var(--warning-color, var(--hms-warning-text, #d29922))",
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

// ---------- Native route dropdowns ----------
// Provider and model are plain <select>s: one click shows every choice, the
// browser handles keyboard and screen-reader behaviour, and nothing pops
// outside the plugin root. Exact hand-typed ids (custom endpoints, routes the
// catalog does not list) stay possible through an explicit "Other" entry that
// reveals a labelled text field.
const OTHER_VALUE = "__other__";

function routeOptionText(text, max = 72) {
  const v = String(text || "");
  return v.length > max ? v.slice(0, max - 1) + "…" : v;
}

// Price hint for one model from the aggregate /catalog row. Provider display
// strings are rendered verbatim, including a real "$0.00" or "free" for one
// token direction — that is a zero rate for that direction, not a free model.
// Only an absent/null display is unknown: a dash plus an explicit
// screen-reader sentence. Only pricingStatus === "free" earns the badge.
const CATALOG_UNKNOWN_TITLE = "no list price in the installed catalog; never treated as free";
const CATALOG_PENDING_TEXT = "prices still loading";

function priceHint(catalogModel) {
  const unknown = {
    text: "—",
    kind: "unknown",
    title: CATALOG_UNKNOWN_TITLE,
  };

  if (!catalogModel) return unknown;
  const status = catalogModel.pricingStatus;
  if (status === "free") return { text: "free", kind: "free", title: "free route" };
  if (status === "included") {
    return { text: "included in subscription", kind: "included", title: "included in subscription" };
  }
  if (status === "priced" && catalogModel.cost) {
    const out = catalogModel.cost.display?.output;
    if (out) {
      return { text: `→ ${out} out`, kind: "priced", title: `${out} per 1M output tokens` };
    }
    const inp = catalogModel.cost.display?.input;
    if (inp) {
      return { text: `→ ${inp} in`, kind: "priced", title: `${inp} per 1M input tokens` };
    }
  }
  return unknown;
}

function freshnessText(value) {
  if (!value) return "from Hermes picker cache";
  return String(value);
}

// ---------- Model catalog browser ----------
// A read-only, full-width bird's-eye view of the SAME aggregate /catalog DTO
// the editor dropdowns consume. Collapsed until asked for; opening it never
// touches selection, editor state, or a pending review. Prices are rendered
// verbatim from ``cost.display`` (already formatted per 1M tokens) and an
// unknown price is never coerced to "$0.00" or a free badge.
const CATALOG_SR_ONLY = {
  position: "absolute", left: -9999, width: 1, height: 1, overflow: "hidden",
};
const CATALOG_PAGE_SIZE = 50;
const CATALOG_FLAT_PAGE_SIZE = 100;

function catalogModelsOf(provider) {
  return Array.isArray(provider && provider.models) ? provider.models : [];
}

function catalogPriceValue(model) {
  const v = model && model.cost ? model.cost.input : null;
  // A provider-stated 0 is a real (cheapest) rate, not unknown; only an
  // absent or non-numeric input has no comparable numeric price.
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
}

function catalogDisplay(model, field) {
  const value = model && model.cost && model.cost.display ? model.cost.display[field] : null;
  // Provider display strings are provider-stated data: render them verbatim,
  // including "$0.00" (a real zero rate for this direction) and "free" in one
  // direction. Only an absent/null display is unknown — absent data is never
  // coerced to a fabricated zero here.
  return value ? value : null;
}

function catalogCheapestInput(models) {
  const priced = models.filter((m) => m.pricingStatus === "priced" && catalogPriceValue(m) !== null);
  if (!priced.length) return null;
  priced.sort((a, b) => catalogPriceValue(a) - catalogPriceValue(b));
  const cheapest = priced[0];
  return catalogDisplay(cheapest, "input") || `$${catalogPriceValue(cheapest).toFixed(2)}`;
}

function catalogPriceRange(models) {
  const priced = models.filter((m) => m.pricingStatus === "priced" && catalogPriceValue(m) !== null);
  if (!priced.length) return null;
  const sorted = [...priced].sort((a, b) => catalogPriceValue(a) - catalogPriceValue(b));
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  const lo = catalogDisplay(first, "input") || `$${catalogPriceValue(first).toFixed(2)}`;
  const hi = catalogDisplay(last, "input") || `$${catalogPriceValue(last).toFixed(2)}`;
  return lo === hi ? lo : `${lo}–${hi}`;
}

// Price cells are deliberately status-first: free/included/unknown come from
// the catalog's own classification, never from a numeric guess. A present
// display string — including "$0.00" or "free" for one direction — is rendered
// verbatim; only a missing display for a priced row is unknown, never zero.
function CatalogPriceCell({ model, field, pending }) {
  if (pending) return <span className="hms-catalog-pending">{CATALOG_PENDING_TEXT}</span>;
  const status = model.pricingStatus;
  if (status === "free") return <span className="hms-badge hms-badge-free">free</span>;
  if (status === "included") return <span className="hms-catalog-included">included in subscription</span>;
  const display = catalogDisplay(model, field);
  if (display) return <span>{display}</span>;
  return (
    <span className="hms-catalog-unknown" title={CATALOG_UNKNOWN_TITLE}
      aria-label={CATALOG_UNKNOWN_TITLE}>price not published</span>
  );
}

// Price sort puts free routes at zero (they are the cheapest), priced rows by
// their numeric input price, and every unknown/included row LAST in BOTH
// directions — an absent price must never look like the cheapest option.
function catalogSortRows(rows, sort) {
  const nameOf = (row) => String(row.model.label || row.model.id || "").toLowerCase();
  const providerOf = (row) => String(row.provider.label || row.provider.id || "").toLowerCase();
  const priceOf = (model) => {
    if (model.pricingStatus === "free") return 0;
    return catalogPriceValue(model);
  };
  return [...rows].sort((a, b) => {
    if (sort === "name") {
      return nameOf(a).localeCompare(nameOf(b)) || String(a.model.id).localeCompare(String(b.model.id));
    }
    if (sort === "provider") {
      return providerOf(a).localeCompare(providerOf(b)) ||
        String(a.provider.id).localeCompare(String(b.provider.id)) ||
        nameOf(a).localeCompare(nameOf(b));
    }
    const av = priceOf(a.model);
    const bv = priceOf(b.model);
    if (av === null && bv === null) return nameOf(a).localeCompare(nameOf(b));
    if (av === null) return 1; // unknown always last
    if (bv === null) return -1;
    return sort === "price-desc" ? bv - av : av - bv;
  });
}

function catalogStatusText(status) {
  return ["priced", "free", "included", "unknown"].includes(status) ? status : "unknown";
}

function catalogConfiguredText(routeIndex, providerId, modelId) {
  const matches = routeIndex.get(providerId + "\u0000" + modelId) || [];
  if (!matches.length) {
    return { text: "—", title: "No loaded profile slot routes to this provider/model." };
  }
  const profiles = [...new Set(matches.map((s) => s.profileId))];
  return {
    text: `${matches.length} slot${matches.length === 1 ? "" : "s"} · ${profiles.join(", ")}`,
    title: matches.map((s) => `${s.profileId}/${s.slotId}`).join(", "),
  };
}

function CatalogRow({ provider, model, pending, grouped, routeIndex, onUse, profileId }) {
  const configured = catalogConfiguredText(routeIndex, provider.id, model.id);
  const blocked = provider.authenticated === false;
  const useId = `catalog-use-${provider.id}-${model.id}`;
  return (
    <tr data-model-row data-provider-id={provider.id} data-model-id={model.id}
      data-pricing-status={catalogStatusText(model.pricingStatus)}>
      <td data-column="model">
        <span className="hms-catalog-model-label">{model.label || model.id}</span>{" "}
        <code className="hms-catalog-model-id">{model.id}</code>
        {!grouped && <span className="hms-catalog-provider-tag">{provider.id}</span>}
      </td>
      <td className="hms-catalog-price" data-column="input">
        <CatalogPriceCell model={model} field="input" pending={pending} />
      </td>
      <td className="hms-catalog-price" data-column="output">
        <CatalogPriceCell model={model} field="output" pending={pending} />
      </td>
      <td data-column="status">{catalogStatusText(model.pricingStatus)}</td>
      <td data-column="configured" title={configured.title}>{configured.text}</td>
      <td data-column="use">
        <button type="button" data-testid={useId} disabled={blocked}
          title={blocked ? `Sign-in needed for ${provider.id}; this profile cannot route there.` : `Use ${provider.id}/${model.id}`}
          onClick={() => onUse(provider, model)}>Use</button>
        {blocked && (
          <span className="hms-catalog-use-reason">
            Sign-in needed — not authenticated in profile “{profileId}”.
          </span>
        )}
      </td>
    </tr>
  );
}

function ModelCatalogView({ catalog, profileId, slots, onRefresh, onRetry, onCollapse, onUse }) {
  const stickyBg = useOpaqueSurface();
  const searchRef = useRef(null);
  const [query, setQuery] = useState("");
  const [freeOnly, setFreeOnly] = useState(false);
  const [pricedOnly, setPricedOnly] = useState(false);
  const [grouped, setGrouped] = useState(true);
  const [sort, setSort] = useState("price-asc");
  const [expandedGroups, setExpandedGroups] = useState(() => new Set());
  const [showAllFlat, setShowAllFlat] = useState(false);

  // '/' focuses the catalog search from anywhere on the page, except while the
  // user is typing in a field (or editing content). Mounted only while open.
  useEffect(() => {
    const onKey = (event) => {
      if (event.key !== "/" || event.altKey || event.ctrlKey || event.metaKey) return;
      const el = event.target;
      const tag = el && el.tagName ? el.tagName.toUpperCase() : "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (el && el.isContentEditable)) return;
      event.preventDefault();
      searchRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const data = catalog && catalog.data && catalog.profileId === profileId ? catalog.data : null;
  const providers = data && Array.isArray(data.providers) ? data.providers : [];
  const loading = !data && Boolean(catalog && catalog.loading);
  const error = catalog && catalog.profileId === profileId ? catalog.error : null;
  const pending = Boolean(data && data.cost && data.cost.pending);
  const unavailable = new Set(
    data && data.cost && Array.isArray(data.cost.unavailableProviders)
      ? data.cost.unavailableProviders
      : [],
  );

  const stats = useMemo(() => {
    const models = providers.flatMap((p) => catalogModelsOf(p));
    const priced = models.filter((m) => m.pricingStatus === "priced").length;
    const free = models.filter((m) => m.pricingStatus === "free").length;
    const unknown = models.filter((m) => m.pricingStatus === "unknown").length;
    const included = models.filter((m) => m.pricingStatus === "included").length;
    const pricedProviders = providers.filter((p) =>
      catalogModelsOf(p).some((m) => m.pricingStatus === "priced")).length;
    return {
      providers: providers.length,
      models: models.length,
      priced,
      free,
      unknown,
      included,
      pricedProviders,
      cheapest: catalogCheapestInput(models),
      stale: providers.filter((p) => p.stale).length,
      withFetchTime: providers.filter((p) => Boolean(p.fetchedAt)).length,
      pending,
    };
  }, [data, pending]);

  // One pass over the loaded /state slots: provider+model exact identity is
  // the join key. 9k-model catalogs stay responsive because rows only look up
  // a prebuilt map instead of rescanning every slot.
  const routeIndex = useMemo(() => {
    const index = new Map();
    for (const slot of slots || []) {
      if (!slot || !slot.provider || !slot.model) continue;
      const key = slot.provider + "\u0000" + slot.model;
      if (!index.has(key)) index.set(key, []);
      index.get(key).push(slot);
    }
    return index;
  }, [slots]);

  const q = query.trim().toLowerCase();
  const matches = (provider, model) => {
    if (freeOnly && model.pricingStatus !== "free") return false;
    if (pricedOnly && model.pricingStatus !== "priced") return false;
    if (!q) return true;
    return [provider.id, provider.label, model.id, model.label]
      .some((value) => String(value || "").toLowerCase().includes(q));
  };

  const rowsByProvider = useMemo(() => providers.map((provider) => ({
    provider,
    rows: catalogSortRows(
      catalogModelsOf(provider).filter((m) => matches(provider, m))
        .map((m) => ({ provider, model: m })),
      sort,
    ),
  })), [providers, q, freeOnly, pricedOnly, sort]);

  const narrowing = Boolean(q) || freeOnly || pricedOnly;
  const orderedGroups = sort === "provider"
    ? [...rowsByProvider].sort((a, b) =>
      String(a.provider.label || a.provider.id).localeCompare(String(b.provider.label || b.provider.id)) ||
      String(a.provider.id).localeCompare(String(b.provider.id)))
    : rowsByProvider;
  const groups = narrowing ? orderedGroups.filter((g) => g.rows.length > 0) : orderedGroups;

  const flatAll = useMemo(() => {
    if (grouped) return [];
    return catalogSortRows(
      providers.flatMap((provider) => catalogModelsOf(provider)
        .filter((m) => matches(provider, m))
        .map((m) => ({ provider, model: m }))),
      sort,
    );
  }, [grouped, providers, q, freeOnly, pricedOnly, sort]);
  const flatShown = showAllFlat ? flatAll : flatAll.slice(0, CATALOG_FLAT_PAGE_SIZE);

  const toggleGroup = (id) => {
    setExpandedGroups((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const clearNarrowing = () => {
    setQuery("");
    setFreeOnly(false);
    setPricedOnly(false);
  };
  const toggleFree = () => {
    setFreeOnly((v) => !v);
    setPricedOnly(false);
  };
  const togglePriced = () => {
    setPricedOnly((v) => !v);
    setFreeOnly(false);
  };

  const noticeProviders = providers.filter((p) => p.warning || unavailable.has(p.id));

  const theadStyle = {
    textAlign: "left", borderBottom: `1px solid ${THEME.border}`, padding: 4,
    position: "sticky", top: 0, background: stickyBg, zIndex: 2,
  };

  return (
    <div className="hms-catalog-view" data-testid="catalog-view">
      <header className="hms-catalog-header">
        <div className="hms-catalog-heading">
          <h3 id="hms-catalog-title">Model catalog — what this profile can route to</h3>
          {data ? (
            <p className="hms-catalog-provenance" data-testid="catalog-provenance">
              List prices per 1M tokens from the installed Hermes catalog ·{" "}
              {stats.priced} of {stats.models} models priced
              {pending ? ` · ${CATALOG_PENDING_TEXT}` : ""}
              {stats.unknown ? ` · ${stats.unknown} with no list price — never treated as free` : ""}
            </p>
          ) : (
            <p className="hms-catalog-provenance">
              Reading the installed Hermes catalog for profile <code>{profileId}</code>…
            </p>
          )}
          <p className="hms-note hms-catalog-readonly">
            Profile scope: <code>{profileId}</code> · Read-only: browsing this catalog never
            changes configuration or costs.
          </p>
        </div>
        <div className="hms-catalog-actions">
          <button type="button" data-testid="catalog-view-refresh"
            aria-label="Refresh catalog for this profile"
            disabled={Boolean(catalog && catalog.loading)}
            onClick={() => onRefresh?.()}>
            {catalog && catalog.loading ? "Refreshing…" : "Refresh catalog"}
          </button>
          <button type="button" data-testid="catalog-collapse" onClick={onCollapse}>
            Collapse
          </button>
        </div>
      </header>

      {data && (
        <details className="hms-catalog-freshness" data-testid="catalog-freshness-details">
          <summary>
            Catalog freshness by provider — {catalog.refreshed ? "refresh requested · " : ""}
            {stats.withFetchTime ? `${stats.withFetchTime} with a fetch time · ${stats.stale} of ${stats.providers} marked stale` : "from Hermes picker cache"}
          </summary>
          {stats.withFetchTime > 0 ? (
            <ul>
              {providers.filter((p) => p.fetchedAt).map((p) => (
                <li key={p.id} data-provider-freshness={p.id}>
                  <strong>{p.label || p.id}</strong>: {p.fetchedAt}
                  {p.stale ? " · stale" : " · fresh"}
                </li>
              ))}
            </ul>
          ) : (
            <p className="hms-note">
              From Hermes picker cache. No provider reported a fetch timestamp;
              no freshness time is invented.
            </p>
          )}
        </details>
      )}

      {data && providers.length > 0 && (
        <>
          <dl className="hms-catalog-summary" data-testid="catalog-summary-strip"
            aria-label="Model catalog summary">
            <div data-tile="providers">
              <dt>Providers</dt>
              <dd>{stats.providers} <span>({stats.pricedProviders} priced)</span></dd>
            </div>
            <div data-tile="models">
              <dt>Models</dt>
              <dd>{stats.models}
                <span>({stats.priced} priced · {stats.free} free)</span>
              </dd>
            </div>
            {stats.cheapest && (
              <div data-tile="cheapest">
                <dt>Cheapest input</dt>
                <dd>{stats.cheapest}/Mtok<span>per 1M tokens</span></dd>
              </div>
            )}
            <div data-tile="unknown">
              <dt>No list price</dt>
              <dd>{stats.unknown} <span>unknown — never treated as free</span></dd>
            </div>
          </dl>

          {pending && (
            <p className="hms-catalog-pending-note" data-testid="catalog-pending" role="note">
              Prices are still loading for this catalog. No route is treated as free while
              cost data is pending.
            </p>
          )}

          {!grouped && noticeProviders.length > 0 && (
            <div className="hms-catalog-notices" data-testid="catalog-provider-notices"
              role="group" aria-label="Provider catalog notices">
              {providers.filter((p) => p.warning).map((p) => (
                <p key={"warning-" + p.id} role="note" data-provider-warning={p.id}>
                  <strong>{p.label || p.id}</strong>: {p.warning}
                </p>
              ))}
              {providers.filter((p) => unavailable.has(p.id)).map((p) => (
                <p key={"unavailable-" + p.id} role="note" data-unavailable-provider={p.id}>
                  <strong>{p.label || p.id}</strong>: prices unavailable — no price is shown,
                  and nothing is treated as free.
                </p>
              ))}
            </div>
          )}

          <div className="hms-catalog-controls" role="group" aria-label="Catalog filters">
            <label className="hms-catalog-search">Search catalog
              <input ref={searchRef} type="search" data-testid="catalog-search"
                aria-label="Search model catalog"
                placeholder="provider, model id, or model name…"
                value={query} onChange={(e) => setQuery(e.target.value)} />
            </label>
            <div className="hms-catalog-toggles">
              <button type="button" data-testid="catalog-free-only" aria-pressed={freeOnly}
                onClick={toggleFree}>Free only</button>
              <button type="button" data-testid="catalog-priced-only" aria-pressed={pricedOnly}
                onClick={togglePriced}>Priced only</button>
              <button type="button" data-testid="catalog-group-toggle" aria-pressed={grouped}
                onClick={() => setGrouped((v) => !v)}>Group by provider</button>
            </div>
            <label className="hms-catalog-sort">Sort
              <select data-testid="catalog-sort" aria-label="Sort model catalog"
                value={sort} onChange={(e) => setSort(e.target.value)}>
                <option value="price-asc">Price ↑</option>
                <option value="price-desc">Price ↓</option>
                <option value="name">Name</option>
                <option value="provider">Provider</option>
              </select>
            </label>
          </div>

          {groups.length === 0 || (!grouped && flatAll.length === 0) ? (
            <div className="hms-catalog-panel" data-testid="catalog-view-empty-matches" role="status">
              <span>
                {q
                  ? `No models or providers match “${query.trim()}”.`
                  : "No models match these filters."}{" "}
                Clear the search and filters to see all {stats.models} models.
              </span>
              <button type="button" data-testid="catalog-clear-filters" onClick={clearNarrowing}>
                Clear search and filters
              </button>
            </div>
          ) : (
            <div className="hms-catalog-scroll" data-testid="catalog-scroll" role="region"
              aria-label="Scrollable model catalog" tabIndex={0}>
              <table className="hms-catalog-table" data-testid="catalog-table">
                <caption style={CATALOG_SR_ONLY}>
                  Model catalog: providers, per-million-token prices, and configuration status
                </caption>
                <thead>
                  <tr>
                    <th scope="col" style={theadStyle}
                      aria-sort={sort === "name" ? "ascending" : "none"}>Model</th>
                    <th scope="col" style={{ ...theadStyle, textAlign: "right" }}
                      aria-label="Input price, US dollars per million tokens"
                      aria-sort={sort === "price-asc" ? "ascending" : sort === "price-desc" ? "descending" : "none"}>
                      Input $/1M
                    </th>
                    <th scope="col" style={{ ...theadStyle, textAlign: "right" }}
                      aria-label="Output price, US dollars per million tokens"
                      aria-sort="none">Output $/1M</th>
                    <th scope="col" style={theadStyle}>Status</th>
                    <th scope="col" style={theadStyle}>Configured in</th>
                    <th scope="col" style={theadStyle}>Action</th>
                  </tr>
                </thead>
                {grouped ? groups.map(({ provider, rows }) => {
                  const models = catalogModelsOf(provider);
                  const range = catalogPriceRange(models);
                  const expanded = expandedGroups.has(provider.id);
                  const shownRows = expanded ? rows : rows.slice(0, CATALOG_PAGE_SIZE);
                  return (
                    <tbody key={provider.id} data-provider-group={provider.id}>
                      <tr className="hms-catalog-group">
                        <th scope="colgroup" colSpan={6}>
                          <span className="hms-catalog-group-head">
                            <strong>{provider.label || provider.id}</strong>
                            <code>{provider.id}</code>
                            <span>{provider.modelCount ?? models.length} models</span>
                            {range && <span className="hms-catalog-range">range {range}</span>}
                            <span className="hms-catalog-group-status"
                              data-auth={provider.authenticated === false ? "no" : "yes"}>
                              {provider.authenticated === false
                                ? "not authenticated — sign-in needed"
                                : "authenticated"}
                              {provider.configured === false ? " · not configured in this profile" : ""}
                            </span>
                            {provider.stale && provider.fetchedAt && (
                              <span className="hms-catalog-stale">
                                stale snapshot{provider.fetchedAt ? ` · fetched ${provider.fetchedAt}` : ""}
                              </span>
                            )}
                          </span>
                          {provider.warning && (
                            <span className="hms-catalog-warning" role="note"
                              data-provider-warning={provider.id}>{provider.warning}</span>
                          )}
                          {unavailable.has(provider.id) && (
                            <span className="hms-catalog-unavailable" role="note"
                              data-unavailable-provider={provider.id}>
                              Prices unavailable for this provider — no price is shown, and
                              nothing is treated as free.
                            </span>
                          )}
                        </th>
                      </tr>
                      {shownRows.map(({ provider: rowProvider, model }) => (
                        <CatalogRow key={rowProvider.id + "/" + model.id}
                          provider={rowProvider} model={model} pending={pending}
                          grouped routeIndex={routeIndex} onUse={onUse} profileId={profileId} />
                      ))}
                      {rows.length === 0 && (
                        <tr data-empty-provider={provider.id}>
                          <td colSpan={6} className="hms-catalog-no-models">
                            No models published for this provider. This is not evidence of
                            free access.
                          </td>
                        </tr>
                      )}
                      {rows.length > CATALOG_PAGE_SIZE && (
                        <tr data-cap-row={provider.id}>
                          <td colSpan={6}>
                            <button type="button" data-testid={`catalog-show-all-${provider.id}`}
                              aria-expanded={expanded} onClick={() => toggleGroup(provider.id)}>
                              {expanded ? "Show fewer" : `Show all ${rows.length}`}
                            </button>
                          </td>
                        </tr>
                      )}
                    </tbody>
                  );
                }) : (
                  <tbody>
                    {flatShown.map(({ provider, model }) => (
                      <CatalogRow key={provider.id + "/" + model.id}
                        provider={provider} model={model} pending={pending}
                        grouped={false} routeIndex={routeIndex} onUse={onUse} profileId={profileId} />
                    ))}
                    {flatAll.length > CATALOG_FLAT_PAGE_SIZE && (
                      <tr data-cap-row="flat">
                        <td colSpan={6}>
                          <button type="button" data-testid="catalog-show-all-flat"
                            aria-expanded={showAllFlat} onClick={() => setShowAllFlat((v) => !v)}>
                            {showAllFlat ? "Show fewer" : `Show all ${flatAll.length}`}
                          </button>
                        </td>
                      </tr>
                    )}
                  </tbody>
                )}
              </table>
            </div>
          )}
        </>
      )}

      {!data && loading && (
        <div className="hms-catalog-panel" role="status" data-testid="catalog-view-loading">
          Loading model catalog for profile <code>{profileId}</code>…
        </div>
      )}
      {!data && !loading && error && (
        <div className="hms-catalog-panel" data-kind="error" role="alert"
          data-testid="catalog-view-error">
          <span><strong>Catalog failed to load:</strong> {error}</span>
          <button type="button" data-testid="catalog-view-retry" onClick={() => onRetry?.()}>
            Retry
          </button>
        </div>
      )}
      {!data && !loading && !error && (
        <div className="hms-catalog-panel" role="status" data-testid="catalog-view-idle">
          <span>The catalog has not been loaded yet.</span>
          <button type="button" data-testid="catalog-view-load" onClick={() => onRetry?.()}>
            Load catalog
          </button>
        </div>
      )}
      {data && error && (
        <div className="hms-catalog-panel" data-kind="error" role="alert"
          data-testid="catalog-view-error">
          <span><strong>Catalog refresh failed:</strong> {error} — showing the last loaded catalog.</span>
          <button type="button" data-testid="catalog-view-retry" onClick={() => onRetry?.()}>
            Retry
          </button>
        </div>
      )}
      {data && providers.length === 0 && (
        <div className="hms-catalog-panel" role="status" data-testid="catalog-view-empty">
          <span>
            This profile reports no providers in its catalog. Nothing here is treated as
            free; check the profile’s provider configuration.
          </span>
        </div>
      )}
    </div>
  );
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
          Select slots, preview your changes, then save a preset to reuse this setup.
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
        {visible.length === 0 && <span style={{ color: THEME.muted }}>No profiles match. Change the search to see other profiles.</span>}
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
      if (filters.provider && (s.provider || "") !== filters.provider) return false;
      if (filters.model && (s.model || "") !== filters.model) return false;
      if (filters.state !== "all" && normalizeState(s.state) !== filters.state) return false;
      return true;
    });
  }, [scoped, filters]);

  const setFilter = (patch) => setFilters({ ...filters, ...patch });

  // Filter dropdowns are strictly local: their options are the distinct
  // values already present in the loaded state. No network request is made.
  const providerFilterValues = useMemo(() => {
    const values = new Set();
    for (const s of slots) if (s.provider) values.add(s.provider);
    return [...values].sort((a, b) => a.localeCompare(b));
  }, [slots]);
  const modelFilterValues = useMemo(() => {
    const values = new Set();
    for (const s of slots) {
      if (!s.model) continue;
      if (filters.provider && s.provider !== filters.provider) continue;
      values.add(s.model);
    }
    if (filters.model) values.add(filters.model);
    return [...values].sort((a, b) => a.localeCompare(b));
  }, [slots, filters.provider, filters.model]);

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
      <p className="hms-section-description">Select profile slots to change. Filters narrow the view; selected targets stay selected.</p>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center", margin: "8px 0" }}>
        <p data-testid="selection-summary" aria-live="polite" style={{ margin: 0 }}>{summaryText}</p>
        <button type="button" data-testid="clear-selection" disabled={selection.size === 0}
          title="Deselect every selected target, including ones hidden by the current filters"
          onClick={() => onClearSelection()}>Clear selection</button>
      </div>
      <div role="group" aria-label="Matrix filters"
        className="hms-matrix-filters">
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
        <label className="hms-filter">Provider<select aria-label="Filter by provider"
          data-testid="filter-provider" value={filters.provider}
          onChange={(e) => setFilter({ provider: e.target.value, model: "" })}>
          <option value="">All providers</option>
          {providerFilterValues.map((v) => <option key={v} value={v}>{v}</option>)}
        </select></label>
        <label className="hms-filter">Model<select aria-label="Filter by model"
          data-testid="filter-model" value={filters.model}
          onChange={(e) => setFilter({ model: e.target.value })}>
          <option value="">All models</option>
          {modelFilterValues.map((v) => <option key={v} value={v}>{routeOptionText(v, 48)}</option>)}
        </select></label>
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
  busy, host, catalog, catalogProfileId, onEnsureCatalog, onRefreshCatalog, onRetryCatalog, announce,
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

  // ---- catalog-backed provider / model dropdowns ------------------------
  // The aggregate /catalog is shared app state, fetched lazily (once per
  // profile scope, then cached) the moment the provider dropdown is hovered,
  // touched or focused — before the native list opens — or on Refresh. Model rows come from the
  // catalog entry for the chosen provider; GET /models is the fallback only
  // for a provider the catalog does not list (a hand-typed slug).
  // An OpenRouter-only scalar selection pins the provider; its models still list.
  const scalarPinned = writableSelected.length > 0 && writableSelected.every(isScalarSlot);
  const modelProvider = scalarPinned ? "openrouter" : editor.provider.trim();
  const providerList = useMemo(
    () => ((catalog.data && Array.isArray(catalog.data.providers)) ? catalog.data.providers : []),
    [catalog.data],
  );
  const catalogModelCount = providerList.reduce((n, p) => n + (p.models || []).length, 0);
  const staleProviderCount = providerList.filter((p) => p.stale).length;
  const catalogProvider = providerList.find((p) => p.id === modelProvider) || null;

  const [providerOther, setProviderOther] = useState(false);
  // While an exact provider slug is being typed, partial slugs ("ope", "open")
  // must not each trigger a GET /models; the fallback fetch waits for blur.
  const [providerTyping, setProviderTyping] = useState(false);
  const [modelOther, setModelOther] = useState(false);
  const [modelState, setModelState] = useState({ key: null, data: null, error: null, loading: false });
  const modelSeq = useRef(0);
  const modelKey = `${catalogProfileId}\u0000${modelProvider}`;
  const fallbackModels = modelState.key === modelKey ? modelState.data : null;
  const modelError = modelState.key === modelKey ? modelState.error : null;
  const needsModelFetch = Boolean(modelProvider && !providerTyping && catalog.data && !catalog.loading &&
    !(catalogProvider && (catalogProvider.models || []).length));
  const modelLoading = needsModelFetch && !fallbackModels && !modelError;


  const fetchModels = async (provider, { retry = false } = {}) => {
    const key = `${catalogProfileId}\u0000${provider}`;
    if (!retry && modelState.key === key && (modelState.loading || modelState.data || modelState.error)) return;
    const seq = ++modelSeq.current;
    setModelState({ key, data: null, error: null, loading: true });
    try {
      const data = await Promise.race([
        api.getModels({ provider, profileId: catalogProfileId, host }),
        new Promise((_, reject) => setTimeout(
          () => reject(new Error("Model list timed out — retry, or choose Other to type an exact model id.")),
          4000,
        )),
      ]);
      if (seq !== modelSeq.current) return;
      setModelState({ key, data, error: null, loading: false });
    } catch (err) {
      if (seq !== modelSeq.current) return;
      const message = err && typeof err.message === "string" && err.message
        ? err.message : "model list request failed";
      setModelState({ key, data: null, error: message, loading: false });
    }
  };
  // A pinned provider (OpenRouter-only scalar selection) disables the
  // provider dropdown, so its lazy-load handlers can never fire: load the
  // catalog as soon as the pin applies so the model dropdown still lists.
  useEffect(() => {
    if (!scalarPinned || editor.changeType !== "set" || catalog.loading) return;
    const sameScope = catalog.profileId === catalogProfileId;
    if (!sameScope || (!catalog.data && !catalog.error)) onEnsureCatalog?.();
  }, [scalarPinned, editor.changeType, catalogProfileId, catalog.profileId, catalog.data, catalog.loading, catalog.error]);

  useEffect(() => {
    if (!needsModelFetch) return undefined;
    const timer = setTimeout(() => fetchModels(modelProvider), 250);
    return () => clearTimeout(timer);
  }, [needsModelFetch, modelKey]);

  const sortedProviders = useMemo(() => [...providerList].sort((x, y) => {
    const ax = x.authenticated === false ? 1 : 0;
    const ay = y.authenticated === false ? 1 : 0;
    if (ax !== ay) return ax - ay;
    return String(x.label || x.id).localeCompare(String(y.label || y.id));
  }), [providerList]);

  const catalogModelMap = useMemo(() => {
    const map = new Map();
    for (const p of providerList) for (const m of p.models || []) map.set(`${p.id}\u0000${m.id}`, m);
    return map;
  }, [providerList]);

  const modelRows = useMemo(() => {
    if (catalogProvider && (catalogProvider.models || []).length) {
      return catalogProvider.models.map((m) => ({ id: m.id, name: m.label || m.name || "" }));
    }
    return (fallbackModels?.models || []).map((m) => ({ id: m.id, name: m.name || "" }));
  }, [catalogProvider, fallbackModels]);

  const providerInList = sortedProviders.some((p) => p.id === modelProvider);
  const modelInList = modelRows.some((m) => m.id === editor.model);
  const showProviderOther = providerOther || (Boolean(modelProvider) && !providerInList && Boolean(catalog.data));
  // An unmounted exact field fires no blur: never leave the fetch gate shut.
  useEffect(() => {
    if (providerTyping && (!showProviderOther || scalarPinned || editor.changeType !== "set")) setProviderTyping(false);
  });
  const showModelOther = modelOther || (Boolean(editor.model) && !modelInList && !modelLoading && Boolean(modelProvider));

  const modelOptionText = (m) => {
    const hint = priceHint(catalogModelMap.get(`${modelProvider}\u0000${m.id}`));
    const price = hint.kind === "unknown" ? "price not published"
      : hint.kind === "priced" ? hint.text.replace(/^→\s*/, "").replace(/ (in|out)$/, "/1M $1") : hint.text;
    const name = m.name && m.name !== m.id ? ` (${m.name})` : "";
    return routeOptionText(`${m.id}${name} — ${price}`, 90);
  };

  const chooseProvider = (value) => {
    if (value === OTHER_VALUE) {
      setProviderOther(true);
      set({ provider: "", model: "" });
      return;
    }
    setProviderOther(false);
    setProviderTyping(false);
    setModelOther(false);
    const p = providerList.find((x) => x.id === value);
    const known = p ? (p.models || []) : [];
    const keepModel = editor.model && known.some((m) => m.id === editor.model);
    set({ provider: value, model: keepModel ? editor.model : "" });
    if (value) announce?.(`Provider set to ${value}${known.length ? ` — ${known.length} models` : ""}`);
  };

  const chooseModel = (value) => {
    if (value === OTHER_VALUE) {
      setModelOther(true);
      set({ model: "" });
      return;
    }
    setModelOther(false);
    set({ model: value, changeType: "set" });
    if (value) announce?.(`Route set to ${modelProvider}/${value}`);
  };

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
      if (scalarOnly) return "OpenRouter auxiliary fallback — choose the model only. OpenRouter is implicit; this setting affects the auxiliary auto-chain across tasks.";
      if (mixedScalar) return "The OpenRouter auxiliary fallback can only be combined with routes to OpenRouter. Choose OpenRouter or select these targets separately.";
      return `Applies to ${writableSelected.length} of ${writableSelected.length} selected slots — sets provider and model. Choose both, then Preview changes.`;
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
      <p className="hms-section-description">Preview changes before applying.</p>
      <p data-testid="editor-applicability" style={{ color: THEME.muted, margin: "0 0 4px" }}>{applicability}</p>
      {selectedSlots.some(s => s.note || s.metadata?.description) && <ul data-testid="selected-route-notes" className="hms-note">
        {[...new Set(selectedSlots.map(s => s.note || s.metadata?.description).filter(Boolean))].map(note => <li key={note}>{note}</li>)}
      </ul>}
      {selectedSlots.some(s => String(s.protocol).replaceAll("-", "_") === "typed_decision") &&
        <p className="hms-note">These targets require a compatible typed-decision route. Preview validates compatibility; a plain chat model is not interchangeable.</p>}
      <div className="hms-editor-fields">
        <label>Change type
          <select data-testid="change-type" aria-label="Change type" value={editor.changeType}
            onChange={(e) => set({ changeType: e.target.value })}>
            <option value="set">Choose a model</option>
            <option value="chain">Set fallback sequence</option>
            <option value="unset">Reset to default or inherited routing</option>
            <option value="disable">Turn off fallback</option>
          </select></label>

        {editor.changeType === "set" && (
          <div className="hms-route-picks" data-testid="route-picks">
            <label className="hms-route-field">Provider
              <select aria-label="New provider" data-testid="provider-select"
                value={scalarOnly ? "openrouter" : (showProviderOther ? OTHER_VALUE : modelProvider)}
                disabled={scalarOnly}
                onFocus={() => onEnsureCatalog?.()}
                onPointerEnter={() => onEnsureCatalog?.()}
                onMouseDown={() => onEnsureCatalog?.()}
                onTouchStart={() => onEnsureCatalog?.()}
                onChange={(e) => chooseProvider(e.target.value)}>
                <option value="">
                  {catalog.loading && !catalog.data ? "Loading providers…" : "Choose a provider…"}
                </option>
                {scalarOnly && <option value="openrouter">openrouter</option>}
                {!scalarOnly && modelProvider && !providerInList && !showProviderOther && (
                  <option value={modelProvider}>{modelProvider}</option>
                )}
                {sortedProviders.map((p) => (
                  <option key={p.id} value={p.id} data-testid={`provider-option-${p.id}`}
                    disabled={p.authenticated === false}
                    title={p.authenticated === false ? (p.warning || "this provider is not authenticated in this profile") : undefined}>
                    {routeOptionText(p.label && p.label !== p.id ? `${p.label} (${p.id})` : p.id)}
                    {p.authenticated === false ? " — sign-in needed" : ""}
                  </option>
                ))}
                <option value={OTHER_VALUE}>Other — type an exact provider slug…</option>
              </select>
            </label>
            {showProviderOther && !scalarOnly && (
              <label className="hms-route-field hms-route-exact">Exact provider slug
                <input data-testid="provider-exact" value={editor.provider}
                  onFocus={() => setProviderTyping(true)}
                  onBlur={() => setProviderTyping(false)}
                  placeholder="e.g. nous, opencode-go, custom:my-endpoint"
                  onChange={(e) => set({ provider: e.target.value })} />
              </label>
            )}
            {catalog.error && (
              <div className="hms-route-status" role="alert" data-testid="catalog-error">
                Provider list unavailable: {catalog.error}{" "}
                <button type="button" data-testid="catalog-retry" onClick={() => onRetryCatalog?.()}>Retry</button>
              </div>
            )}

            <label className="hms-route-field">Model
              <select aria-label="New model" data-testid="model-select"
                value={showModelOther ? OTHER_VALUE : editor.model}
                disabled={!modelProvider && !scalarOnly}
                onFocus={() => onEnsureCatalog?.()}
                onPointerEnter={() => onEnsureCatalog?.()}
                onChange={(e) => chooseModel(e.target.value)}>
                <option value="">
                  {!modelProvider && !scalarOnly ? "Choose a provider first"
                    : providerTyping ? "Finish the provider slug to list its models"
                    : modelLoading ? "Loading models…"
                    : modelRows.length ? `Choose a model (${modelRows.length})…` : "No listed models — choose Other"}
                </option>
                {modelRows.map((m) => (
                  <option key={m.id} value={m.id} data-testid={`model-option-${m.id}`}>{modelOptionText(m)}</option>
                ))}
                <option value={OTHER_VALUE}>Other — type an exact model id…</option>
              </select>
            </label>
            {showModelOther && (
              <label className="hms-route-field hms-route-exact">Exact model id
                <input data-testid="model-exact" value={editor.model}
                  placeholder="provider-native model id"
                  onChange={(e) => set({ model: e.target.value })} />
              </label>
            )}
            {modelError && (
              <div className="hms-route-status" role="alert" data-testid="model-error">
                {modelError}{" "}
                <button type="button" data-testid="model-retry"
                  onClick={() => fetchModels(modelProvider, { retry: true })}>Retry</button>
              </div>
            )}
            {!modelError && fallbackModels?.error && (
              <div className="hms-route-status" role="note" data-testid="model-note">{fallbackModels.error}</div>
            )}
            {modelProvider && editor.model && (
              <p className="hms-route-summary" data-testid="route-summary">
                Route: <code>{modelProvider}/{editor.model}</code>
              </p>
            )}
          </div>
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

        {/* One compact catalog line; per-provider freshness stays collapsed.
            Only an explicit click sends refresh=1. */}
        <div className="hms-catalog-tools" data-testid="picker-advanced">
          <div className="hms-catalog-actions">
            <span className="hms-note" data-testid="catalog-summary">
              {catalog.data
                ? `${providerList.length} providers · ${catalogModelCount} models` +
                  (catalog.data.cost?.pending ? " · cost pending" : "") +
                  (providerList.some((p) => p.fetchedAt)
                    ? (staleProviderCount ? ` · ${staleProviderCount} stale` : " · fresh")
                    : ` · ${catalog.refreshed ? "refresh requested" : "from Hermes picker cache"}`)
                : catalog.loading ? "Loading catalog…" : "Catalog not loaded"}
              {" · scope "}<code>{catalogProfileId}</code>
            </span>
            <button type="button" data-testid="refresh-catalog"
              disabled={busy || catalog.loading}
              onClick={() => onRefreshCatalog?.()}>
              {catalog.loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          {catalog.data && providerList.length > 0 && (
            <details className="hms-catalog-freshness" data-testid="catalog-freshness">
              <summary>Catalog details</summary>
              <ul className="hms-freshness-list" aria-label="Provider catalog freshness">
                {providerList.map((p) => (
                  <li key={p.id} data-provider-freshness={p.id}>
                    <code>{p.id}</code> — {freshnessText(p.fetchedAt)}
                    {p.stale && p.fetchedAt ? <strong> · stale</strong> : null}
                    {p.warning ? <span> · {p.warning}</span> : null}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>

        <label className="hms-cost-ack">
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
            aria-disabled={!preview || previewInvalid || busy || !editor.ackUnknownCost}
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
      className="hms-preview-panel">
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

// Per-step labels for the journal's REAL step vocabulary — pending | applied |
// failed | compensated | conflict (journal.py). A ``not_written`` status only
// ever existed in the test mock; no backend path can emit it, so it is not
// mapped here — an unrecognised status renders the neutral default below.
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
};

// Same status, opposite meaning: an apply that rolled back mid-operation.
const ROLLBACK_LABEL = { text: "Rolled back after failed apply", color: THEME.warn };
// A compensated/restored step seen with NO recorded client action context (e.g.
// an operation rehydrated from a GET): a user-requested undo and a rollback
// after a failed apply are indistinguishable here, so we never invent "apply" —
// a neutral, non-success label is the only truthful render.
const UNKNOWN_RESTORE_LABEL = { text: "Restored — action context unknown", color: THEME.warn };
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
  // Host-owned steps: the browser performed the host route, the backend
  // classified the result by re-reading the store. The host's own response
  // text never reaches this map — only these fixed categories do.
  host_no_write: "The host reported success but wrote nothing — not applied",
  host_declined: "Host write declined at the model confirmation",
  host_error: "The host refused the write",
  host_token_mismatch: "Host step out of sequence — nothing was changed",
  external_edit: "The host route was changed by something else during apply",
};
const STATE_COPY = {
  compensated: "rolled back after a failed apply",
  partial: "partially rolled back after a failed apply",
  recovery_required: "recovery required",
  // ``pending`` is the known no-write terminal state (the step never touched
  // the store; the op stays retryable) — stated as a no-change fact, never a
  // success. This is the only op-level home for "not changed".
  pending: "Not changed — review again",
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

function OperationDrawer({ open, operation, onUndo, onClose, undoGuarded, setUndoGuarded, busy, action }) {
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
  // ``action`` is LOCAL UI context recorded at the client mutation that produced
  // this DTO (``apply`` vs ``undo``). The DTO alone cannot distinguish a
  // user-requested undo from a rollback after a failed apply — both surface as
  // per-step ``compensated``/``restored``. Trust the recorded action, and fall
  // back to the operation's own ``undone`` state when no local context exists
  // (e.g. an operation rehydrated from a GET).
  const undoContext = action === "undo" || undoVerified;
  // In an UNDO context every compensated/restored step is a verified restore of
  // THAT profile — even when the whole operation is incomplete (another profile
  // hit a conflict). Per-profile bytes are verified independently of the op.
  const isRestored = (status) => COMPENSATED_STATUSES.includes(status) && undoContext;
  const profileCount = (rows) => new Set(rows.map((r) => r.profileId)).size;
  const appliedCount = profileCount(results.filter((r) => r.status === "applied"));
  const restoredCount = profileCount(results.filter((r) => isRestored(r.status)));
  const conflictCount = profileCount(results.filter((r) => r.status === "conflict"));
  // Any step that is not a verified ``applied`` needs attention — UNLESS it is a
  // compensated/restored step of a user-requested undo. A fully-compensated
  // failed apply therefore reads "0 applied · N need attention" instead of the
  // old, dangerously clean "0 · 0"; a partial undo counts only the conflicted
  // profile (the restored one is genuinely back); an unrecognised future status
  // is counted too rather than silently ignored.
  const attentionCount = profileCount(results.filter((r) =>
    r.status !== "applied" && !isRestored(r.status)
  ));
  const category = scalarText(operation.failure) || results.map((r) => scalarText(r.failure)).find(Boolean) || null;
  const failure = category && Object.hasOwn(FAILURE_COPY, category) ? category : category ? "unknown_failure" : null;
  const failureCopy = failure ? FAILURE_COPY[failure] || "Operation could not be verified — check recovery" : null;
  // A failed apply may carry NO failure category (the rollback itself is the
  // outcome); the terminal state still has to be on screen, so the state line
  // is driven by the state, not by the presence of ``failure``. ``pending`` is
  // the one terminal state that keeps the line even alongside a safe category:
  // the step never wrote, so "not changed — review again" is the truthful
  // headline next to the category.
  const showStateLine = !undoVerified && Boolean(finalState) && finalState !== "committed" &&
    (!failure || finalState === "pending");
  return (
    <aside ref={ref} tabIndex={-1} role="dialog" aria-modal="true"
      aria-label="Operation drawer" data-testid="operation-drawer"
      style={{ position: "fixed", right: 0, top: 0, bottom: 0, width: "min(380px, 100vw)", boxSizing: "border-box", overflow: "auto",
        background: THEME.bg, borderLeft: `1px solid ${THEME.border}`, padding: 12, zIndex: 50, outline: "none" }}>
      <h3>Operation results</h3>
      <p data-testid="operation-summary" data-attention={attentionCount > 0 ? "true" : "false"}
        data-restored={restoredCount} data-conflict={conflictCount}
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
          {undoContext ? "Undo incomplete" : "Apply did not commit"} — {STATE_COPY[finalState] || finalState}
        </p>
      )}
      {undoVerified && (
        <p data-testid="operation-state" data-state="undone" style={{ color: THEME.ok }}>
          Undo verified — previous revisions restored
        </p>
      )}
      <ul style={{ listStyle: "none", padding: 0 }}>
        {results.map((r, index) => {
          const isCompensated = COMPENSATED_STATUSES.includes(r.status);
          const label = isCompensated
            ? (undoContext
                ? RESULT_LABEL[r.status]   // verified restore of this profile (green)
                : action === "apply"
                  ? ROLLBACK_LABEL         // rollback after a failed apply (warn)
                  : UNKNOWN_RESTORE_LABEL) // no context: never invent "apply"
            : RESULT_LABEL[r.status] || { text: r.status, color: THEME.fg };
          return (
            <li key={`${r.profileId}-${index}`} data-testid={`result-${r.profileId}`}
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
  removeExact: "", ackUnknownCost: false,
};

// ---------- host step surfacing ----------

// The host's own cost guard answers a main/aux assignment with
// `{confirm_required: true, confirm_message}`. That message is backend text:
// it is rendered as TEXT (React escapes it), never as HTML, and the dialog
// offers exactly two actions. Cancel resolves the step as `declined` — the
// plugin backend compensates; the UI never guesses what "no" means.
function ConfirmDialog({ ask, onConfirm, onCancel }) {
  const confirmRef = useRef(null);
  const cancelRef = useRef(null);
  useEffect(() => {
    if (!ask) return undefined;
    confirmRef.current?.focus();
    const onKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onCancel(); }
      if (event.key === "Tab") {
        event.preventDefault();
        (document.activeElement === confirmRef.current ? cancelRef : confirmRef).current?.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [ask]);
  if (!ask) return null;
  return (
    <div className="hms-confirm-backdrop">
      <div role="dialog" aria-modal="true" aria-labelledby="hms-confirm-title"
        aria-describedby="hms-confirm-message" data-testid="confirm-dialog" className="hms-confirm">
        <h3 id="hms-confirm-title">Hermes asks you to confirm this model</h3>
        <p id="hms-confirm-message" data-testid="confirm-message">
          {String(ask.message || "Review this model assignment before continuing.")}
        </p>
        <div className="hms-confirm-actions">
          <button ref={cancelRef} type="button" data-testid="confirm-cancel" onClick={onCancel}>Cancel</button>
          <button type="button" ref={confirmRef} data-testid="confirm-accept"
            className="hms-apply" onClick={onConfirm}>Confirm</button>
        </div>
      </div>
    </div>
  );
}

const PROGRESS_LABEL = {
  applied: "done", ok: "done", pending: "pending", awaiting_host: "awaiting host",
  confirm: "confirm", failed: "failed", compensated: "rolled back", conflict: "recovery required",
  recovery_required: "recovery required",
};

// Per-step apply progress. A host step is a round trip of its own (and may wait
// on the user for a cost confirmation), so "request in progress…" alone would
// hide where the operation actually is. Entries come from the envelope's
// per-profile results plus the live host call it is currently waiting on.
function ApplyProgress({ progress }) {
  if (!progress || !progress.entries || !progress.entries.length) return null;
  return (
    <div data-testid="apply-progress" role="status" aria-live="polite"
      className="hms-apply-progress">
      <strong style={{ display: "block", marginBottom: 4 }}>
        Apply progress{progress.note ? ` — ${progress.note}` : ""}
      </strong>
      <ol style={{ margin: 0, paddingLeft: 18 }}>
        {progress.entries.map((entry) => (
          <li key={entry.key} data-testid={`apply-step-${entry.key}`}
            data-step-status={entry.status}>
            {entry.label} — {PROGRESS_LABEL[entry.status] || "pending"}
          </li>
        ))}
      </ol>
    </div>
  );
}

// Results are journal steps (several may share a profile). The host sequence
// is zero-based and indexes the same step list, including backend-only steps.
function progressEntries(operation, hostCall, profileNameOf) {
  const entries = ((operation && Array.isArray(operation.results)) ? operation.results : [])
    .map((result, index) => ({
      key: `step-${index}`,
      label: `${profileNameOf(result.profileId)} (${result.profileId}) · step ${index + 1}`,
      status: result.status,
    }));
  if (hostCall) {
    const profile = (hostCall.query && hostCall.query.profile) || "";
    const route = String(hostCall.path || "").replace(/^\/api\/model\//, "");
    const running = {
      key: `host-${hostCall.seq}`,
      label: `host ${route || "write"}${profile ? ` → ${profile}` : ""} · step ${hostCall.seq + 1}`,
      status: "awaiting_host",
    };
    if (entries[hostCall.seq]) entries[hostCall.seq] = running;
    else entries.push(running);
  }
  return entries;
}

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
  // Local UI-only context for the operation DTO on screen: which client mutation
  // produced it. Never sent to the backend; the DTO itself cannot tell a
  // user-requested undo from a rollback after a failed apply.
  const [opAction, setOpAction] = useState(null);
  const [showOperation, setShowOperation] = useState(false);
  const [undoGuarded, setUndoGuarded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [editor, setEditor] = useState(EMPTY_EDITOR);
  // Lazy, shared aggregate catalog state. One fetch per profileId; only an
  // explicit Refresh sends refresh=1. Never fetched on mount.
  const [catalog, setCatalog] = useState({ profileId: null, data: null, error: null, loading: false });
  // Bird's-eye catalog section is collapsed until the user asks for it. The
  // section is a sibling of the workspace, so toggling it can never unmount
  // or reset selection/editor/preview state.
  const [catalogOpen, setCatalogOpen] = useState(false);
  // Host data: Hermes' own model routes (/api/model/options|auxiliary|moa) per
  // profile. Loaded after the config-only /state paint, never blocking it; a
  // profile whose route is missing keeps `null` parts plus one notice line.
  const [host, setHost] = useState({ profiles: {} });
  const [hostNotice, setHostNotice] = useState({ text: "Loading host model data…", kind: "loading" });
  const mutationLock = useRef(false);
  const [recoveryNeeded, setRecoveryNeeded] = useState(false);
  const applyOpener = useRef(null);
  // Per-step apply progress (host steps included) and the pending host cost
  // confirmation. `confirmResolve` holds the promise resolver for the loop.
  const [progress, setProgress] = useState(null);
  const [confirmAsk, setConfirmAsk] = useState(null);
  const hostRef = useRef({ profiles: {} });
  const confirmResolve = useRef(null);
  const reviewSeq = useRef(0);
  const catalogSeq = useRef(0);
  const catalogCache = useRef(new Map());
  const loadSeq = useRef(0);
  const hostSeq = useRef(0);

  const announce = (msg, kind = "info") => setStatus({ text: msg, kind });
  const profileNameOf = (profileId) => {
    const found = ((state && state.profiles) || []).find((p) => p.id === profileId);
    return (found && (found.name || found.id)) || profileId || "profile";
  };
  const adoptHost = (bundle) => {
    hostRef.current = bundle || { profiles: {} };
    setHost(hostRef.current);
  };
  // Host bundle for `profileIds`, refreshed in place. Never throws: an
  // unreachable route becomes a null part and a fixed-category notice.
  const refreshHost = async (profileIds, { refresh = false, seq = null } = {}) => {
    const mine = ++hostSeq.current;
    setHostNotice({ text: "Loading host model data…", kind: "loading" });
    const { host: bundle, failures } = await loadHostBundle(profileIds, { refresh });
    if (mine !== hostSeq.current || (seq !== null && seq !== loadSeq.current)) return null;
    adoptHost(bundle);
    const available = Object.values(bundle.profiles).some((parts) => Object.values(parts).some(Boolean));
    setHostNotice({ text: hostFailureText(failures), kind: available ? "partial" : "unavailable" });
    // Upgrade the config-only paint with the host-derived rows. Best effort:
    // a backend that predates POST /state keeps the GET DTO.
    try {
      const enriched = await api.loadState(bundle);
      if (!enriched || !Array.isArray(enriched.slots)) return null;
      if (mine !== hostSeq.current || (seq !== null && seq !== loadSeq.current)) return null;
      setState(enriched);
      setSelection((old) => new Map(enriched.slots.filter((slot) =>
        old.has(slot.profileId + "/" + slot.slotId)).map((slot) =>
        [slot.profileId + "/" + slot.slotId, slot])));
    } catch (e) {
      setHostNotice({ text: "Host enrichment unavailable. Configuration-only state is shown.", kind: "partial" });
    }
    return bundle;
  };
  // Host parts for one profile, fetched on demand (catalog / model lists) when
  // the mount-time load has not reached it yet, or refetched on refresh.
  const ensureHostFor = async (profileId, { refresh = false } = {}) => {
    const pid = profileId || "";
    const current = hostRef.current && hostRef.current.profiles ? hostRef.current.profiles[pid] : null;
    if (current && !refresh) return current;
    const { host: bundle, failures } = await loadHostBundle([pid], { refresh });
    const merged = { profiles: { ...(hostRef.current.profiles || {}), ...bundle.profiles } };
    adoptHost(merged);
    if (failures.length) setHostNotice({ text: hostFailureText(failures), kind: "partial" });
    return merged.profiles[pid];
  };
  // Resolve the pending host cost confirmation. `true` = Confirm, `false` =
  // Cancel; the loop reports `declined` and the backend compensates.
  const askConfirm = (message) => new Promise((resolve) => {
    confirmResolve.current = resolve;
    setConfirmAsk({ message: typeof message === "string" ? message : "" });
  });
  const answerConfirm = (accepted) => {
    const resolve = confirmResolve.current;
    confirmResolve.current = null;
    setConfirmAsk(null);
    applyOpener.current?.focus();
    if (resolve) resolve(Boolean(accepted));
  };
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
      // Host data is refreshed out of band: the paint above is complete on its
      // own, and a slow or missing host route must never stall a reload.
      refreshHost((s.profiles || []).map((p) => p.id), { seq });
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
    if (!mutationLock.current) setBusy(false);
  };
  const editEditor = (next) => {
    const { ackUnknownCost: oldAck, ...oldIntent } = editor;
    const { ackUnknownCost: newAck, ...newIntent } = next;
    if (JSON.stringify(oldIntent) !== JSON.stringify(newIntent)) invalidateReviews();
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
      // Non-blocking host half: the tab is already usable with config-only
      // data while these land.
      refreshHost((s.profiles || []).map((p) => p.id), { seq });
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
  // Scope for /catalog and /models: the single checked profile, else default
  // (same rule the retired picker used). The freshness line states the scope.
  const catalogProfileId = filters.profileIds.length === 1 ? filters.profileIds[0] : "default";
  const selectedSlots = slots.filter((slot) => selection.has(slot.profileId + "/" + slot.slotId));
  const customChanges = buildCustomChanges(editor, selectedSlots);
  const customFingerprint = "custom:" + JSON.stringify(customChanges);
  const activePreview = preview && (previewFor === customFingerprint ||
    (reviewedPreset && previewFor === "preset:" + reviewedPreset.id)) ? preview : null;
  const cleanReview = activePreview && activePreview.changes.length > 0 &&
    !(activePreview.blocked && activePreview.blocked.length);
  const canApply = Boolean(cleanReview && !busy && !recoveryNeeded && (reviewedPreset || editor.ackUnknownCost));

  const runPreview = async (changes, fingerprint) => {
    if (!changes.length || mutationLock.current || recoveryNeeded) return;
    const seq = ++reviewSeq.current;
    setPreview(null);
    setPreviewFor("");
    setBusy(true);
    try {
      const p = await api.preview(changes, hostRef.current);
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
    if (mutationLock.current || recoveryNeeded) return;
    invalidateReviews();
    setReviewPreset(preset);
    setPreview(null);
    setPreviewFor("");
    await runPreview(buildPresetChanges(preset, slots), "preset:" + preset.id);
  };

  const loadCatalog = async ({ refresh = false, force = false } = {}) => {
    const pid = catalogProfileId;
    const cached = catalogCache.current.get(pid);
    if (!refresh && !force && cached) {
      setCatalog({ profileId: pid, data: cached, error: null, loading: false });
      return;
    }
    if (catalog.loading && catalog.profileId === pid) return;
    const seq = ++catalogSeq.current;
    setCatalog((prev) => ({
      profileId: pid,
      data: prev.profileId === pid ? prev.data : null,
      error: null,
      loading: true,
    }));
    try {
      // The catalog is a pure transform of the host's options payload, so the
      // host part for this profile is fetched (or refetched with refresh=1 on
      // an explicit Refresh) BEFORE the plugin route is asked. A missing host
      // route leaves `options: null`: the backend then answers its truthful
      // hostCatalogUnavailable DTO instead of an invented catalog.
      await ensureHostFor(pid, { refresh });
      const data = await api.getCatalog({ profileId: pid, refresh, host: hostRef.current });
      if (seq !== catalogSeq.current) return;
      catalogCache.current.set(pid, data);
      setCatalog({ profileId: pid, data, error: null, loading: false, refreshed: refresh });
      const providers = Array.isArray(data.providers) ? data.providers : [];
      const models = providers.reduce((n, p) => n + (p.models || []).length, 0);
      const priced = providers.reduce((n, p) =>
        n + (p.models || []).filter((m) => ["priced", "free", "included"].includes(m.pricingStatus)).length, 0);
      announce(`${models} models available, ${priced} priced`);
    } catch (e) {
      if (seq !== catalogSeq.current) return;
      setCatalog((prev) => ({
        profileId: pid,
        data: prev.profileId === pid ? prev.data : null,
        error: e instanceof ApiError ? e.message : String(e),
        loading: false,
      }));
    }
  };

  // The trigger is the ONLY thing that opens the section; it shares the
  // editor's lazy cache, so opening after the picker loaded costs zero extra
  // requests. A profile-scope change while open fetches that profile once.
  const toggleCatalog = () => {
    if (!catalogOpen) loadCatalog();
    setCatalogOpen((open) => !open);
  };
  useEffect(() => {
    if (!catalogOpen) return;
    if (catalog.profileId !== catalogProfileId && !catalog.loading) loadCatalog();
  }, [catalogOpen, catalogProfileId, catalog.profileId, catalog.loading]);

  // Row action: exactly the editor's existing model-option behavior, then
  // bring the rail into view. Never touches acknowledgment or selection.
  const useCatalogRoute = (provider, model) => {
    if (provider.authenticated === false) return;
    editEditor({ ...editor, changeType: "set", provider: provider.id, model: model.id });
    announce(`Route set to ${provider.id}/${model.id}`);
    const rail = rootRef.current?.querySelector('[data-testid="change-editor"]');
    const reduce = typeof window !== "undefined" && typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    rail?.scrollIntoView?.({ behavior: reduce ? "auto" : "smooth", block: "start" });
    rail?.querySelector('[data-testid="provider-select"]')?.focus({ preventScroll: true });
  };

  const catalogTriggerCount = catalog.data && catalog.profileId === catalogProfileId
    ? (Array.isArray(catalog.data.providers) ? catalog.data.providers : [])
      .reduce((n, p) => n + (Array.isArray(p.models) ? p.models.length : 0), 0)
    : null;

  // One host step of an operation: perform the host route the backend asked
  // for, honour its cost guard, and report the outcome. `status` is the numeric
  // HTTP status only — the host's response text is never forwarded anywhere;
  // the backend turns the number into one of its fixed failure categories.
  const runHostCall = async (call) => {
    let res;
    try {
      res = await api.hostCall(call);
    } catch (e) {
      return { outcome: "error", status: hostStatusOf(e) };
    }
    if (res?.ok === false && res.confirm_required === true && typeof res.confirm_message === "string") {
      setProgress((prev) => (prev ? { ...prev, note: "waiting for your confirmation",
        entries: prev.entries.map((entry) => entry.key === `host-${call.seq}` ? { ...entry, status: "confirm" } : entry) } : prev));
      const accepted = await askConfirm(res.confirm_message);
      // Cancel is a real answer, not an error: the backend compensates and
      // reports its own fixed category for the declined step.
      if (!accepted) return { outcome: "declined" };
      try {
        res = await api.hostCall(call, { confirmExpensiveModel: true });
      } catch (e) {
        return { outcome: "error", status: hostStatusOf(e) };
      }
    }
    if (res && res.ok === true) return { outcome: "ok" };
    // A 2xx that is not ok: the host refused without an error status. Report
    // the numeric status; the backend still owns the classification.
    return { outcome: "error", status: 200 };
  };

  const applyPreview = async () => {
    if (!canApply || mutationLock.current) return;
    mutationLock.current = true;
    applyOpener.current = document.activeElement;
    setBusy(true);
    setProgress({ entries: [{ key: "request", label: "Operation", status: "pending" }], note: "requesting" });
    const payload = reviewedPreset
      ? { presetId: reviewedPreset.id, previewId: activePreview.id, allowUnknownCost: editor.ackUnknownCost }
      : { previewId: activePreview.id, allowUnknownCost: editor.ackUnknownCost };
    // Consume at dispatch, not only on success: a lost response may hide a write.
    reviewSeq.current += 1;
    setPreview(null);
    setPreviewFor("");
    let operationId = null;
    try {
      let op = await api.apply({ ...payload, host: hostRef.current });
      operationId = op.operation.id;
      setOperation(op.operation);
      setOpAction("apply");
      const seen = new Set();
      let lastSeq = -1;
      // Bulk operations may contain arbitrarily many valid steps. Detect a
      // repeated or out-of-order instruction before issuing a second write.
      while (op.hostCall) {
        const call = validateHostCall(op.hostCall, operationId);
        if (seen.has(call.token) || call.seq <= lastSeq) throw new ApiError("Invalid host step");
        seen.add(call.token);
        lastSeq = call.seq;
        setProgress({ entries: progressEntries(op.operation, call, profileNameOf), note: "host step in progress" });
        const result = await runHostCall(call);
        op = await api.hostResult(operationId, {
          seq: call.seq, token: call.token, outcome: result.outcome, status: result.status,
        });
        setOperation(op.operation);
      }
      const terminal = ["committed", "undone", "partial", "compensated", "recovery_required", "failed"];
      const finalState = op.operation.final_state || op.operation.state;
      const noWriteConflict = finalState === "pending" && op.operation.failure === "revision_conflict" &&
        op.operation.results.every((r) => ["failed", "pending"].includes(r.status));
      if (!terminal.includes(finalState) && !noWriteConflict) throw new ApiError("Operation is unfinished");
      setRecoveryNeeded(finalState === "recovery_required");
      setProgress({ entries: progressEntries(op.operation, null, profileNameOf),
        note: op.ok === true ? "finished" : finalState === "recovery_required" ? "recovery required" : "finished with issues" });
      setUndoGuarded(false);
      await refreshSilently();
      openDrawer(applyOpener.current);
      const statuses = op.operation.results.map((r) => `${r.profileId} ${r.status}`).join(", ");
      const outcome = op.ok === true ? "" : ` · ${finalState}`;
      announce(`Operation ${operationId} finished: ${statuses}${outcome}`, op.ok === true ? "info" : "error");
    } catch (e) {
      // Never retry a host write after a lost continuation response. Read the
      // exact operation and reconcile the journal before another mutation.
      if (operationId) {
        setRecoveryNeeded(true);
        let reconciled = null;
        try {
          let recovered = await api.getOperation(operationId);
          if (["pending", "awaiting_host"].includes(recovered.operation.state)) recovered = await api.recover(operationId);
          setOperation(recovered.operation);
          reconciled = recovered.operation;
          setRecoveryNeeded(["pending", "awaiting_host", "recovery_required"].includes(recovered.operation.state));
          await refreshSilently();
          openDrawer(applyOpener.current);
        } catch {
          // Preserve the last known operation and block subsequent mutations.
          setRecoveryNeeded(true);
        }
        setProgress(reconciled ? { entries: progressEntries(reconciled, null, profileNameOf), note: "interrupted · " + reconciled.state }
          : { entries: [{ key: "recovery", label: `Operation ${operationId}`, status: "recovery_required" }], note: "check operation before continuing" });
        announce(reconciled ? "Apply interrupted. Operation readback: " + reconciled.state
          : "Apply interrupted. Check or recover the last operation before continuing.", "error");
      } else {
        setProgress({ entries: [{ key: "request", label: "Operation request", status: "failed" }], note: "failed" });
        announce("Apply failed: " + (e instanceof ApiError ? e.message : "Request failed"), "error");
      }
    } finally {
      mutationLock.current = false;
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
    if (!operation || operation.undoable === false || !undoGuarded || busy || mutationLock.current) return;
    mutationLock.current = true;
    setBusy(true);
    try {
      const op = await api.undo(operation.id);
      setOperation(op.operation);
      // Tag the undo action even on ok:false (HTTP 200 with the operation DTO):
      // the drawer must read the per-profile outcome as an undo, not an apply.
      setOpAction("undo");
      setUndoGuarded(false);
      await refreshSilently();
      setRecoveryNeeded(op.operation.state === "recovery_required");
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
      mutationLock.current = false;
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
  const openDrawer = (opener = document.activeElement) => {
    drawerOpener.current = opener;
    setShowOperation(true);
  };
  const closeDrawer = () => {
    setShowOperation(false);
    setProgress(null);
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
    <div ref={rootRef} className="hms-root" style={{ color: THEME.fg, fontFamily: "inherit" }}>
      <div aria-live="polite" role="status" data-testid="live-status"
        style={{ position: "absolute", left: -9999 }}>{status.text}</div>
      <header className="hms-page-header">
        <h2>Model Switcher</h2>
        <p>Choose models across Hermes profiles. Preview changes, then apply or save a preset.</p>
      </header>
      {state.presetsCorrupt && <p role="alert" style={{ color: THEME.warn }}>
        A damaged preset store was saved aside for recovery. Its presets are not loaded; recover them before replacing saved presets.
      </p>}
      {state.profiles.some(p => p.inventoryWarnings?.length) && <div role="alert" data-testid="inventory-warnings" className="hms-note">
        <strong>Some runtime selectors could not be discovered.</strong>
        <ul>{state.profiles.flatMap(p => (p.inventoryWarnings || []).map((warning, i) => <li key={`${p.id}/${i}`}>{p.name || p.id}: {warning}</li>))}</ul>
      </div>}
      {/* Host half: Hermes' own model routes. A route that is missing or
          unreachable is stated once, in fixed words, and never blocks the tab
          — config-only rows stay usable and the backend degrades truthfully. */}
      {hostNotice.text && <p role="status" aria-live="polite" data-testid="host-notice"
        data-state={hostNotice.kind} className="hms-note hms-host-notice">{hostNotice.text}</p>}
      <PresetsBar presets={state.presets} onReview={reviewPreset} busy={busy} />
      <div className="hms-workspace-tools">
        <button data-testid="refresh-state" disabled={busy} onClick={() => load()}>Refresh state</button>
        {operation && !showOperation && <button onClick={() => openDrawer()}>Show last operation</button>}
        <button type="button" data-testid="catalog-toggle" aria-expanded={catalogOpen}
          aria-controls="hms-model-catalog" onClick={toggleCatalog}>
          {catalogTriggerCount === null
            ? "Browse model catalog"
            : `Browse model catalog · ${catalogTriggerCount} model${catalogTriggerCount === 1 ? "" : "s"}`}
        </button>
        <span className="hms-note">Configuration changes apply after review. Existing sessions may keep their current model.</span>
      </div>

      {/* Bird's-eye catalog: full width, directly below the tools row and
          ABOVE the workspace grid. The section placeholder always exists so
          aria-controls is valid; its content mounts only when opened and the
          workspace is never remounted by the toggle. */}
      <section id="hms-model-catalog" className="hms-catalog" data-testid="catalog-section"
        aria-label="Model catalog" hidden={!catalogOpen}>
        {catalogOpen && (
          <ModelCatalogView catalog={catalog} profileId={catalogProfileId} slots={slots}
            onRefresh={() => loadCatalog({ refresh: true })}
            onRetry={() => loadCatalog({ force: true })}
            onCollapse={() => setCatalogOpen(false)}
            onUse={useCatalogRoute} />
        )}
      </section>

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
          <p data-testid="visible-status" data-kind={status.kind}
            style={{
              margin: "0 0 8px", fontSize: 13,
              color: status.kind === "error" ? THEME.danger : THEME.muted,
            }}>
            {status.text}
          </p>
        )}
        <ApplyProgress progress={progress} />
        {recoveryNeeded && operation && <button type="button" disabled={busy}
          data-testid="recover-operation" onClick={async () => {
            if (mutationLock.current) return;
            mutationLock.current = true;
            setBusy(true);
            try {
              const result = await api.recover(operation.id);
              setOperation(result.operation);
              setRecoveryNeeded(["pending", "awaiting_host", "recovery_required"].includes(result.operation.state));
              announce("Operation recovery checked: " + result.operation.state);
              await refreshSilently();
            } catch { announce("Operation recovery unavailable. Try again before applying changes.", "error"); }
            finally { mutationLock.current = false; setBusy(false); }
          }}>Recover last operation</button>}
        <ChangeEditor
          selection={selection} slots={slots}
          editor={editor} setEditor={editEditor}
          host={host}
          catalog={catalog} catalogProfileId={catalogProfileId}
          onEnsureCatalog={() => loadCatalog()}
          onRefreshCatalog={() => loadCatalog({ refresh: true })}
          onRetryCatalog={() => loadCatalog({ force: true })}
          announce={announce}
          onPreview={() => runPreview(customChanges, customFingerprint)}
          onApply={applyPreview}
          preview={activePreview && previewFor.startsWith("custom:") ? activePreview : null}
          previewInvalid={!cleanReview || recoveryNeeded}
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

      <OperationDrawer open={showOperation && !confirmAsk} operation={operation} onClose={closeDrawer} onUndo={undo} undoGuarded={undoGuarded}
        setUndoGuarded={setUndoGuarded} busy={busy} action={opAction} />
      <ConfirmDialog ask={confirmAsk} onConfirm={() => answerConfirm(true)} onCancel={() => answerConfirm(false)} />
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
