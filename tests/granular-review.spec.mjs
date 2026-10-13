import { test, expect } from '@playwright/test';

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
  await page.getByLabel('New provider', { exact: true }).fill('zhipu');
  await page.getByLabel('New model', { exact: true }).fill('glm-5.3-flash');
  await page.getByLabel('I acknowledge unknown-cost routes').check();
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  await expect(page.getByTestId('preview-panel')).toBeVisible();
  await page.getByLabel('New model', { exact: true }).fill('glm-5.3-flash-changed');
  await expect(page.getByTestId('preview-panel')).toHaveCount(0);
  await expect(page.getByTestId('apply-custom')).toBeDisabled();
  await page.getByLabel('New model', { exact: true }).fill('glm-5.3-flash');
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  await page.getByTestId('apply-custom').click();
  await expect(page.getByTestId('result-research')).toContainText('Applied + read back');
  await expect(page.locator('tr[data-row="research/delegation.model"] [data-column="configured"]')).toContainText('zhipu/glm-5.3-flash');
  await expect(page.locator('tr[data-row="research/delegation.model"] [data-column="observed"]')).toHaveText('unobserved');
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
  await page.getByLabel('Picker provider', { exact: true }).fill('provider-not-configured');
  await page.getByTestId('fetch-models').click();
  await expect(page.getByTestId('picker-error')).toContainText('not available');
  await page.getByLabel('New provider', { exact: true }).fill('nous');
  await page.getByLabel('New model', { exact: true }).fill('deepseek/deepseek-v4.1-flash');
  await expect(page.getByLabel('New model', { exact: true })).toHaveValue('deepseek/deepseek-v4.1-flash');
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
  await page.getByLabel('New provider', { exact: true }).fill('zhipu');
  await page.getByLabel('New model', { exact: true }).fill('glm-5.3-flash');
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  await page.getByLabel('New model', { exact: true }).fill('a-different-route');
  await expect(page.getByRole('button', { name: 'Preview changes', exact: true })).toBeEnabled();
  await expect(page.getByTestId('preview-panel')).toHaveCount(0);
  await expect(page.getByTestId('apply-custom')).toBeDisabled();
});

test('picker contract fixture: search and selection preserve provider-native IDs', async ({ page }) => {
  // Controlled catalog responses test selection spelling; no provider inference is performed.
  await page.route('**/api/plugins/hermes-model-switcher/models?*', route => {
    const provider = new globalThis.URL(route.request().url()).searchParams.get('provider');
    const id = provider === 'nous' ? 'deepseek/deepseek-v4.1-flash' : 'deepseek-v4.1-flash';
    return route.fulfill({ json: { ok: true, provider, models: [{ id, name: id }, { id: 'another-model', name: 'Other' }],
      source: 'contract-fixture', stale: true, fetchedAt: null, error: null } });
  });
  await open(page);
  for (const [provider, id] of [['nous', 'deepseek/deepseek-v4.1-flash'], ['opencode-go', 'deepseek-v4.1-flash']]) {
    await page.getByLabel('Picker provider', { exact: true }).fill(provider);
    await page.getByTestId('fetch-models').click();
    await page.getByLabel('Search models', { exact: true }).fill('deepseek');
    await expect(page.getByTestId('model-option-another-model')).toHaveCount(0);
    await page.getByTestId('model-option-' + id).click();
    await expect(page.getByLabel('New provider', { exact: true })).toHaveValue(provider);
    await expect(page.getByLabel('New model', { exact: true })).toHaveValue(id);
  }
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
