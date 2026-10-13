import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

const out = path.join(process.env.TMPDIR, 'model-switcher-visual');
const files = ['ui/src/main.jsx', 'ui/src/api.js', 'ui/src/styles.css', 'dashboard/dist/index.js'];
const digest = crypto.createHash('sha256');
for (const file of files) digest.update(fs.readFileSync(file));
const lineage = { repo: process.cwd(), commit: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
  surfaceDigest: digest.digest('hex'), sourceFiles: files, captureBoundary: 'Compiled UI in Dashboard SDK fixture with real FastAPI/canonical save against isolated home; no production mutation' };

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`observed UI captures and interactive layout at ${viewport.width}px`, async ({ page }) => {
    fs.mkdirSync(out, { recursive: true });
    await page.setViewportSize(viewport);
    const consoleErrors = [], failedRequests = [], captures = [];
    page.on('pageerror', error => consoleErrors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
    page.on('requestfailed', request => failedRequests.push({ url: request.url(), error: request.failure()?.errorText }));
    await page.route('**/tests/mock-api.js', route => route.fulfill({ body: '', contentType: 'application/javascript' }));
    await page.route('**/api/plugins/**', async route => {
      const url = new URL(route.request().url());
      url.host = 'localhost:8932';
      const response = await route.fetch({ url: url.toString() });
      await route.fulfill({ response });
    });
    async function capture(state) {
      const filename = path.join(out, `${viewport.width}-${state}.png`);
      if (state === 'operation') await page.evaluate(() => scrollTo(0, 0));
      await page.screenshot({ path: filename, fullPage: state !== 'operation' });
      const bounds = await page.evaluate(() => ({ width: innerWidth, documentWidth: document.documentElement.scrollWidth }));
      captures.push({ state, file: filename, viewport, bounds });
      expect(bounds.documentWidth, `${state} horizontal overflow`).toBeLessThanOrEqual(viewport.width);
    }
    await page.goto('/fixture-sdk.html');
    await expect(page.getByLabel('New provider', { exact: true })).toBeVisible();
    await capture('idle');
    await page.getByLabel('Profile research', { exact: true }).check();
    await expect(page.locator('tr[data-row^="default/"]')).toHaveCount(0);
    await capture('profile-filter');
    if (viewport.width < 600) {
      const table = page.getByTestId('matrix-scroll');
      await table.focus();
      await expect(table).toBeFocused();
      await page.keyboard.press('ArrowRight');
      await expect.poll(() => table.evaluate(node => node.scrollLeft)).toBeGreaterThan(0);
      await table.evaluate(node => { node.scrollLeft = 0; });
    }
    await page.getByRole('button', { name: '1 selected · show all' }).click();
    await page.getByLabel('Select default main', { exact: true }).check();
    await page.getByLabel('New provider', { exact: true }).fill('zhipu');
    await page.getByLabel('New model', { exact: true }).fill('glm-5.3-flash');
    await page.getByLabel('I acknowledge unknown-cost routes').check();
    await page.getByRole('button', { name: 'Preview changes', exact: true }).click();
    await expect(page.getByTestId('preview-panel')).toBeVisible();
    await capture('preview');
    await page.getByTestId('apply-custom').click();
    await expect(page.getByTestId('operation-drawer')).toBeVisible();
    const drawer = await page.getByTestId('operation-drawer').boundingBox();
    expect(drawer.x).toBeGreaterThanOrEqual(0);
    expect(drawer.x + drawer.width).toBeLessThanOrEqual(viewport.width);
    await page.getByRole('button', { name: 'Close operation drawer' }).focus();
    await expect(page.getByRole('button', { name: 'Close operation drawer' })).toBeFocused();
    await capture('operation');
    await page.keyboard.press('Enter');
    await expect(page.getByTestId('operation-drawer')).toHaveCount(0);
    await page.getByRole('button', { name: 'Show last operation' }).click();
    await page.getByTestId('undo-guard').check();
    await page.getByTestId('undo-button').click();
    await expect(page.getByTestId('result-default')).toContainText('Restored');
    await expect(page.getByTestId('live-status')).toContainText('Undo finished');
    await page.unrouteAll({ behavior: 'wait' });
    fs.writeFileSync(path.join(out, `${viewport.width}-manifest.json`), JSON.stringify({
      lineage, viewport, captures, consoleErrors, failedRequests, observedAt: new Date().toISOString(),
      journeys: ['profile filter and reset', 'review and apply', 'keyboard close and reopen', 'guarded undo'] }, null, 2));
    expect(consoleErrors).toEqual([]);
    expect(failedRequests).toEqual([]);
  });
}
