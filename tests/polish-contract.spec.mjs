import { test, expect } from '@playwright/test';
import { setProvider, setModel } from "./route-helpers.mjs";

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
  await setModel(page, 'vendor/exact-model:free');
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

test('a multi-change, multi-diff preview renders every keyed row without duplicate-key warnings', async ({ page }) => {
  // Preview rows and their diff rows are keyed by (profile, slot) and d.path
  // instead of array indices. Identity keys must stay unique across a rich
  // preview; a collision would surface as React's duplicate-key warning, so
  // render one and assert the whole list plus a clean console.
  const consoleErrors = [];
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
  await scene(page, [
    { slotId: 'main', family: 'main', provider: 'openai', model: 'a' },
    { slotId: 'auxiliary.vision', family: 'auxiliary', provider: 'auto', model: '' },
  ]);
  await page.route('**' + BASE + '/preview', route => route.fulfill({ json: { ok: true, preview: {
    id: 'prev-keys',
    changes: [
      { profileId: 'default', slotId: 'main', action: 'set',
        from: { provider: 'openai', model: 'a' }, to: { provider: 'zhipu', model: 'glm-5.3-flash' },
        diff: [{ path: 'model.provider', from: 'openai', to: 'zhipu' },
          { path: 'model.default', from: 'a', to: 'glm-5.3-flash' }] },
      { profileId: 'default', slotId: 'auxiliary.vision', action: 'set',
        from: { provider: 'auto', model: '' }, to: { provider: 'openai', model: 'gpt-vision' },
        diff: [{ path: 'auxiliary.vision.provider', from: 'auto', to: 'openai' },
          { path: 'auxiliary.vision.model', from: '', to: 'gpt-vision' }] },
    ],
  } } }));

  await page.getByLabel('Select default main', { exact: true }).check();
  await setProvider(page, 'zhipu');
  await setModel(page, 'glm-5.3-flash');
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();

  const panel = page.getByTestId('preview-panel');
  await expect(panel).toContainText('default / main');
  await expect(panel).toContainText('default / auxiliary.vision');
  // both changes carry a two-row diff list, all keyed by their path
  await expect(page.getByTestId('preview-diff').locator('li')).toHaveCount(4);
  expect(consoleErrors.filter((e) => /same key|unique "key"/i.test(e))).toEqual([]);
});
