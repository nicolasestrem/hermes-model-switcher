// Shared helpers for the native provider/model dropdowns in the change editor.
// Ids listed in the loaded catalog are chosen from the <select>; anything else
// goes through the explicit "Other" entry and its exact-id text field, which is
// how custom endpoints and uncatalogued routes are entered in the real UI.
export const OTHER = "__other__";
export const providerSelect = (page) => page.getByLabel("New provider", { exact: true });
export const modelSelect = (page) => page.getByLabel("New model", { exact: true });
export const providerExact = (page) => page.getByLabel("Exact provider slug", { exact: true });
export const modelExact = (page) => page.getByLabel("Exact model id", { exact: true });

async function listed(select, id) {
  return (await select.locator("option").evaluateAll(
    (opts, want) => opts.some((o) => o.value === want && !o.disabled), id));
}

// The provider catalog loads lazily when the dropdown is hovered or focused;
// wait for that load to settle so a listed id is picked from the list.
export async function ensureCatalog(page) {
  const select = providerSelect(page);
  await select.focus();
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-testid="provider-select"]');
    return el && el.options.length && el.options[0].textContent !== "Loading providers…";
  });
}

export async function setProvider(page, id, { exact = false } = {}) {
  const select = providerSelect(page);
  if (!(await select.isDisabled())) await ensureCatalog(page);
  if (!exact && await listed(select, id)) {
    await select.selectOption(id);
    return;
  }
  await select.selectOption(OTHER);
  await providerExact(page).fill(id);
  // Model lookup for a typed slug waits until the field is left.
  await providerExact(page).blur();
}

export async function setModel(page, id, { exact = false } = {}) {
  const select = modelSelect(page);
  if (!exact && await listed(select, id)) {
    await select.selectOption(id);
    return;
  }
  if (!(await modelExact(page).count())) await select.selectOption(OTHER);
  await modelExact(page).fill(id);
}

export async function setRoute(page, provider, model, opts = {}) {
  await setProvider(page, provider, opts);
  await setModel(page, model, opts);
}
