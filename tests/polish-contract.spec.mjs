import { test, expect } from '@playwright/test';

// Explicit UI contract fixtures: no claim of backend saves or inference.
const BASE = '/api/plugins/hermes-model-switcher';
async function scene(page, slots, extraProfiles = []) {
  await page.route('**/tests/mock-api.js', route => route.fulfill({ body: '', contentType: 'application/javascript' }));
  await page.route('**' + BASE + '/state', route => route.fulfill({ json: {
    ok: true, profiles: [{ id: 'default', name: 'Default', writable: true }, ...extraProfiles],
    slots: slots.map(s => ({ profileId: 'default', writable: true, state: 'configured', ...s })), presets: [],
  } }));
  await page.goto('/fixture-sdk.html');
  await expect(page.getByLabel('Change type')).toBeVisible();
}

test('scalar fallback uses model-only intent and implicit OpenRouter provider', async ({ page }) => {
  await scene(page, [{ slotId: 'auxiliary.openrouter_model', family: 'auxiliary', provider: '', model: 'old/model' }]);
  await page.getByLabel('Select default auxiliary.openrouter_model', { exact: true }).check();
  await expect(page.getByLabel('New provider', { exact: true })).toBeDisabled();
  await expect(page.getByTestId('editor-applicability')).toContainText('OpenRouter');
  await page.getByLabel('New model', { exact: true }).fill('vendor/exact-model:free');
  const request = page.waitForRequest(r => r.url().endsWith(BASE + '/preview'));
  await page.route('**' + BASE + '/preview', route => route.fulfill({ json: {
    ok: true, preview: { id: 'fixture-preview', changes: [], blocked: [] },
  } }));
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  expect((await request).postDataJSON().changes[0].to).toEqual({ model: 'vendor/exact-model:free' });
});

test('fallback actions reject mixed non-chain targets instead of dropping them', async ({ page }) => {
  await scene(page, [
    { slotId: 'main', family: 'main', provider: 'openai', model: 'm' },
    { slotId: 'fallback.main_chain', family: 'fallback', provider: '', model: '', chain: [] },
  ]);
  await page.getByLabel('Select default main', { exact: true }).check();
  await page.getByLabel('Select default fallback.main_chain', { exact: true }).check();
  await page.getByLabel('Change type').selectOption('disable');
  await expect(page.getByTestId('editor-applicability')).toContainText('1 of 2');
  await expect(page.getByRole('button', { name: 'Preview changes', exact: true })).toBeDisabled();
  await page.getByLabel('Change type').selectOption('chain');
  await page.getByLabel('Fallback chain entries', { exact: true }).fill('openai/m');
  await expect(page.getByRole('button', { name: 'Preview changes', exact: true })).toBeDisabled();
});

test('metadata, discovery failures, readable tasks and selected vision warning are visible', async ({ page }) => {
  await scene(page, [
    { slotId: 'auxiliary.vision', family: 'auxiliary', provider: 'auto', model: '', note: 'An explicit vision backend sends a text interpretation even when the main model supports vision.' },
    { slotId: 'auxiliary.title_generation', family: 'auxiliary', provider: 'auto', model: '' },
    { slotId: 'auxiliary.custom_task', family: 'auxiliary', provider: 'auto', model: '', metadata: { label: 'Custom task label', description: 'Registered plugin task' } },
  ], [{ id: 'broken', name: 'Broken', writable: false, inventoryWarnings: ['Auxiliary task discovery unavailable.'] }]);
  await expect(page.getByTestId('inventory-warnings')).toContainText('Auxiliary task discovery unavailable');
  await expect(page.getByLabel('Filter by subfamily')).toContainText('Title generation');
  await expect(page.getByLabel('Filter by subfamily')).toContainText('Custom task label');
  await page.getByLabel('Select default auxiliary.vision', { exact: true }).check();
  await expect(page.getByTestId('selected-route-notes')).toContainText('even when');
});
