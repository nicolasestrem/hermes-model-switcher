import { test, expect } from '@playwright/test';
import { setProvider, setModel, mockHostReads } from "./route-helpers.mjs";
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const BASE = '/api/plugins/hermes-model-switcher';

async function openAssignmentFixture(page) {
  await mockHostReads(page);
    await page.route('**/tests/mock-api.js', r => r.fulfill({ body: '', contentType: 'application/javascript' }));
  await page.route('**' + BASE + '/catalog', r => r.fulfill({ json: {
    ok: true, profileId: 'default', providers: [], cost: { pending: false, unavailableProviders: [] }, error: null,
  } }));
  await page.route('**' + BASE + '/models', r => r.fulfill({ json: {
    ok: true, providers: [], models: [], provider: r.request().postDataJSON()?.provider || "",
    fetchedAt: null, source: "fixture", stale: true, error: null,
  } }));
  await page.route('**' + BASE + '/state', r => r.fulfill({ json: {
    ok: true, profiles: [{ id: 'default', name: 'Default', writable: true }], presets: [],
    slots: [
      { profileId: 'default', slotId: 'specialist.tts', family: 'specialist', provider: 'edge', model: '', state: 'unset',
        writable: false, selectable: true, requiresModelBackend: true, note: 'Edge uses voices, not a configurable model. Choose a model-bearing backend to switch.' },
      { profileId: 'default', slotId: 'specialist.embedding', family: 'specialist', provider: '', model: '', state: 'unset',
        writable: false, selectable: false, note: 'No runtime-configurable embedding field.' },
    ],
  } }));
  await page.goto('/fixture-sdk.html');
  await expect(page.getByLabel('Change type')).toBeVisible();
}

test('Edge TTS is selectable for an explicit model-bearing backend assignment', async ({ page }) => {
  await openAssignmentFixture(page);
  await expect(page.getByLabel('Select default specialist.tts', { exact: true })).toBeEnabled();
  await page.getByTestId('select-all').check();
  await expect(page.getByLabel('Select default specialist.tts', { exact: true })).toBeChecked();
  await expect(page.getByLabel('Select default specialist.embedding', { exact: true })).not.toBeChecked();
  await expect(page.getByTestId('selection-summary')).toContainText('1 slot on 1 profile selected');
  await expect(page.getByTestId('editor-applicability')).toContainText('model-bearing backend');
  await setProvider(page, 'openai');
  await setModel(page, 'gpt-4o-mini-tts');
  await page.route('**' + BASE + '/preview', r => r.fulfill({ json: { ok: true, preview: { id: 'fixture-only-preview', changes: [], blocked: [] } } }));
  const request = page.waitForRequest(r => r.url().endsWith(BASE + '/preview'));
  await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
  expect((await request).postDataJSON().changes[0]).toMatchObject({
    profileId: 'default', slotId: 'specialist.tts', action: 'set',
    to: { provider: 'openai', model: 'gpt-4o-mini-tts' },
  });
});

test('assignment-only TTS cannot reset or remove an imaginary Edge model field', async ({ page }) => {
  await openAssignmentFixture(page);
  await page.getByLabel('Select default specialist.tts', { exact: true }).check();
  for (const action of ['unset', 'disable', 'chain']) {
    await page.getByLabel('Change type').selectOption(action);
    await expect(page.getByTestId('editor-applicability')).toContainText('Choose a model');
    await expect(page.getByRole('button', { name: 'Preview changes', exact: true })).toBeDisabled();
  }
  await page.getByLabel('Change type').selectOption('set');
  await page.getByTestId('removal-advanced').locator('summary').click();
  await page.getByLabel('Exact provider/model to remove').fill('edge/');
  await expect(page.getByRole('button', { name: 'Preview changes', exact: true })).toBeDisabled();
});

for (const viewport of [{ width: 1440, height: 900, theme: 'dark' }, { width: 375, height: 844, theme: 'light' }]) {
  test(`rendered TTS backend-switch interaction ${viewport.width}px ${viewport.theme}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const errors = [], failedRequests = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
    page.on('requestfailed', req => failedRequests.push({ url: req.url(), error: req.failure()?.errorText }));
    await page.route('**/fixture-sdk.html', async route => {
      const response = await route.fetch();
      const tokens = viewport.theme === 'light'
        ? '--background:#fafafa;--midground:#151515;--text-muted-color:#595959;--color-border:#777;--color-ring:#0055aa'
        : '--background:#16181d;--midground:#e6e8ee;--text-muted-color:#a5acb8;--color-border:#515965;--color-ring:#6f9bff';
      const html = (await response.text()).replace('</head>', `<style>:root{${tokens}}body{background:var(--background);color:var(--midground)}</style></head>`);
      await route.fulfill({ response, body: html });
    });
    await openAssignmentFixture(page);
    await page.getByLabel('Select default specialist.tts', { exact: true }).check();
    await expect(page.getByTestId('backend-switch-note')).toContainText('choose a model-bearing backend');
    await expect(page.getByTestId('editor-applicability')).toContainText('model-bearing backend');
    await page.getByLabel('Change type').selectOption('unset');
    await expect(page.getByRole('button', { name: 'Preview changes', exact: true })).toBeDisabled();
    await page.getByLabel('Change type').selectOption('set');
    await setProvider(page, 'openai');
    await setModel(page, 'gpt-4o-mini-tts');
    await expect(page.getByRole('button', { name: 'Preview changes', exact: true })).toBeEnabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
    const root = path.join(process.env.TMPDIR, 'model-switcher-review-round1');
    fs.mkdirSync(root, { recursive: true });
    const image = path.join(root, `tts-${viewport.width}-${viewport.theme}.png`);
    await page.screenshot({ path: image, fullPage: true });
    const files = ['ui/src/main.jsx', 'ui/src/api.js', 'ui/src/styles.css', 'dashboard/dist/index.js', 'dashboard/dist/style.css'];
    const digest = crypto.createHash('sha256');
    for (const file of files) digest.update(fs.readFileSync(file));
    fs.writeFileSync(path.join(root, `tts-${viewport.width}-${viewport.theme}.json`), JSON.stringify({
      boundary: 'Rendered compiled plugin in isolated SDK/host-reset fixture with explicit assignment DTO, not live Dashboard/backend save evidence',
      viewport, surfaceDigest: digest.digest('hex'), files, image, errors, failedRequests,
      observedAt: new Date().toISOString(), interactions: ['select Edge TTS', 'reject imaginary model reset', 'enable explicit OpenAI model assignment'],
    }, null, 2));
    expect(errors).toEqual([]);
    expect(failedRequests).toEqual([]);
  });
}
