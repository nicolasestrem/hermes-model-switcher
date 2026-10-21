import { test, expect } from '@playwright/test';
import { setProvider, setModel } from "./route-helpers.mjs";

const URL = 'http://localhost:8932/fixture-real.html';
const BASE = 'http://localhost:8932/api/plugins/hermes-model-switcher';
const rows = (page) => page.locator('tr[data-row]');
async function open(page) {
  await page.goto(URL);
  await expect(page.getByTestId('live-status')).toContainText('state loaded');
}

test('real API: one, two and searchable profiles restrict actual rows', async ({ page }) => {
  await open(page);
  await page.getByLabel('Profile research', { exact: true }).check();
  await expect(page.locator('tr[data-row^="default/"]')).toHaveCount(0);
  expect(await rows(page).count()).toBeGreaterThan(0);
  await page.getByLabel('Profile default', { exact: true }).check();
  expect(await page.locator('tr[data-row^="default/"]').count()).toBeGreaterThan(0);
  expect(await page.locator('tr[data-row^="research/"]').count()).toBeGreaterThan(0);
  await page.getByLabel('Search profiles').fill('research');
  await expect(page.getByLabel('Profile default', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Profile research', { exact: true })).toBeChecked();
});

test('real API: family, exact subfamily and inherited/unset states match DTO rows', async ({ page, request }) => {
  const dto = await (await request.get(BASE + '/state')).json();
  await open(page);
  await page.getByLabel('Filter by family').selectOption('delegation');
  await page.getByLabel('Filter by subfamily').selectOption('fallback_chain');
  await expect(rows(page)).toHaveCount(dto.slots.filter(s => s.family === 'delegation' && s.slotId.endsWith('fallback_chain')).length);
  await page.getByLabel('Filter by family').selectOption('all');
  for (const [option, aliases] of [['inherited', ['inherit', 'inherited']], ['unset', ['unset', 'default']]]) {
    await page.getByLabel('Filter by state').selectOption(option);
    await expect(rows(page)).toHaveCount(dto.slots.filter(s => aliases.includes(s.state)).length);
  }
});

test('real API: edits invalidate review and refresh after mutation reveals configured state', async ({ page }) => {
  await open(page);
  await page.getByLabel('Profile research', { exact: true }).check();
  await page.getByLabel('Select research delegation.model', { exact: true }).check();
  await setProvider(page, 'zhipu');
  await setModel(page, 'glm-5.3-flash');
  await page.getByLabel('I acknowledge unknown-cost routes').check();
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  await expect(page.getByTestId('preview-panel')).toBeVisible();
  await setModel(page, 'glm-5.3-flash-changed');
  await expect(page.getByTestId('preview-panel')).toHaveCount(0);
  await expect(page.getByTestId('apply-custom')).toBeDisabled();
  await setModel(page, 'glm-5.3-flash');
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  await page.getByTestId('apply-custom').click();
  await expect(page.getByTestId('result-research')).toContainText('Applied + read back');
  await expect(page.locator('tr[data-row="research/delegation.model"] [data-column="configured"]')).toContainText('zhipu · glm-5.3-flash');
  await expect(page.locator('tr[data-row="research/delegation.model"] [data-column="observed"]')).toHaveText('No recorded usage');
  await expect(page.getByTestId('preview-panel')).toHaveCount(0);
  await page.getByTestId('undo-guard').check();
  await page.getByTestId('undo-button').click();
  await expect(page.getByTestId('result-research')).toContainText('Restored');
  await expect(page.locator('tr[data-row="research/delegation.model"] [data-column="configured"]')).toContainText('inherit');
  await expect(page.getByTestId('undo-button')).toBeDisabled();
});

test('real API: disabled chain preview and configured disabled state survive readback', async ({ page }) => {
  await open(page);
  await page.getByLabel('Profile research', { exact: true }).check();
  await page.getByLabel('Select research delegation.fallback_chain', { exact: true }).check();
  await page.getByLabel('Change type').selectOption('disable');
  await page.getByLabel('I acknowledge unknown-cost routes').check();
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  await expect(page.getByTestId('preview-panel')).toContainText('explicit empty list');
  await page.getByTestId('apply-custom').click();
  await expect(page.getByTestId('result-research')).toContainText('Applied + read back');
  await expect(page.locator('tr[data-row="research/delegation.fallback_chain"] [data-column="configured"]')).toHaveText('disabled');
  await page.getByTestId('undo-guard').check();
  await page.getByTestId('undo-button').click();
  await expect(page.getByTestId('result-research')).toContainText('Restored');
});

test('real API: unavailable catalog is visible and manual exact ID stays usable', async ({ page }) => {
  await open(page);
  // A provider the catalog does not list falls back to /models: its refusal
  // surfaces inline, and exact manual entry remains.
  await setProvider(page, 'provider-not-configured');
  await expect(page.getByTestId('model-error')).toContainText('not available');
  await setProvider(page, 'nous');
  await setModel(page, 'deepseek/deepseek-v4.1-flash');
  await expect(page.getByTestId('route-summary')).toHaveText('Route: nous · deepseek/deepseek-v4.1-flash');
});

test('real API: a late preview response cannot revive an edited review', async ({ page }) => {
  await open(page);
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (String(args[0]).endsWith('/preview')) await new Promise(resolve => setTimeout(resolve, 700));
      return response;
    };
  });
  await page.getByLabel('Select research delegation.model', { exact: true }).check();
  await setProvider(page, 'zhipu');
  await setModel(page, 'glm-5.3-flash');
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  await setModel(page, 'a-different-route');
  await expect(page.getByRole('button', { name: 'Preview changes', exact: true })).toBeEnabled();
  await expect(page.getByTestId('preview-panel')).toHaveCount(0);
  await expect(page.getByTestId('apply-custom')).toBeDisabled();
});

// With no catalog rows, every provider is entered exactly and its model list
// comes from POST /models — the fallback path these contract fixtures pin.
// /catalog and /models are POST-only now (a GET answers 410), so the patterns
// must not require a query string.
const emptyCatalog = (page) => page.route('**/api/plugins/hermes-model-switcher/catalog', route =>
  route.fulfill({ json: { ok: true, profileId: 'default', providers: [], cost: { pending: false } } }));

test('picker contract fixture: selection preserves provider-native IDs', async ({ page }) => {
  // Controlled catalog responses test selection spelling; no provider inference is performed.
  await emptyCatalog(page);
  await page.route('**/api/plugins/hermes-model-switcher/models', route => {
    const provider = (route.request().postDataJSON() || {}).provider;
    const id = provider === 'nous' ? 'deepseek/deepseek-v4.1-flash' : 'deepseek-v4.1-flash';
    return route.fulfill({ json: { ok: true, provider, models: [{ id, name: id }, { id: 'another-model', name: 'Other' }],
      source: 'contract-fixture', stale: true, fetchedAt: null, error: null } });
  });
  await open(page);
  const model = page.getByLabel('New model', { exact: true });
  for (const [provider, id] of [['nous', 'deepseek/deepseek-v4.1-flash'], ['opencode-go', 'deepseek-v4.1-flash']]) {
    await setProvider(page, provider);
    await expect(model.locator(`option[value="${id}"]`)).toHaveCount(1);
    await expect(model.locator('option[value="another-model"]')).toHaveCount(1);
    await model.selectOption(id);
    await expect(page.getByLabel('Exact provider slug', { exact: true })).toHaveValue(provider);
    await expect(model).toHaveValue(id);
    await expect(page.getByTestId('route-summary')).toHaveText(`Route: ${provider} · ${id}`);
  }
});

test('picker note: an ok:true catalog carrying a warning still surfaces it', async ({ page }) => {
  // Reconciling the audit: the picker note is NOT dead code. The real /models
  // returns ok:true WITH a non-null error when a provider row resolves but
  // reports no models (or carries a warning), so this state is reachable and
  // must render — prove it rather than deleting the branch.
  await emptyCatalog(page);
  await page.route('**/api/plugins/hermes-model-switcher/models', route =>
    route.fulfill({ json: { ok: true, provider: 'nous', models: [], fetchedAt: null,
      source: 'curated', stale: true, error: "provider 'nous' reported no models" } }));
  await open(page);
  // The note renders an ok:true /models warning without converting it into a
  // hard error (and without hiding the exact-ID path).
  await setProvider(page, 'nous');
  await expect(page.getByTestId('model-note')).toContainText('reported no models');
  await expect(page.getByTestId('model-error')).toHaveCount(0);
  await expect(page.getByLabel('New model', { exact: true }).locator('option[value="__other__"]')).toHaveCount(1);
});

test('real API: object-shaped diff values render as JSON, never [object Object]', async ({ page }) => {
  await open(page);
  await page.getByLabel('Select research delegation.fallback_chain', { exact: true }).check();
  await page.getByLabel('Change type').selectOption('chain');
  await page.getByLabel('Fallback chain entries', { exact: true }).fill('nous/hermes-4');
  await page.getByLabel('I acknowledge unknown-cost routes').check();
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  const diff = page.getByTestId('preview-diff');
  await expect(diff).toBeVisible();
  await expect(diff).toContainText('{"provider":"nous","model":"hermes-4"}');
  await expect(diff).not.toContainText('[object Object]');
});

test('real API: one slot carries either a set or a removal — never both', async ({ page }) => {
  // The engine coalesces same-slot changes (later wins); the editor must not
  // emit both intents for one slot. The removal owns the slot here.
  await open(page);
  await page.getByLabel('Select default main', { exact: true }).check();
  await setProvider(page, 'zhipu');
  await setModel(page, 'glm-5.3-flash');
  await page.getByTestId('removal-advanced').locator('summary').click();
  await page.getByLabel('Exact provider/model to remove', { exact: true })
    .fill('anthropic/claude-sonnet-4-6');
  await page.getByLabel('I acknowledge unknown-cost routes').check();
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  const panel = page.getByTestId('preview-panel');
  await expect(panel).toContainText('Preview (1 change)');
  await expect(page.getByTestId('preview-diff')).toContainText('model.default');
  await expect(panel).not.toContainText('zhipu');
});
