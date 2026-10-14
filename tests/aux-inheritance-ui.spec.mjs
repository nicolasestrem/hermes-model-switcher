import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

for (const state of ['inherit', 'unset']) for (const width of [1440, 375]) {
  test(`${state} plugin effective route details remain visible at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
    await page.route('**/tests/mock-api.js', r => r.fulfill({ body: '', contentType: 'application/javascript' }));
    await page.route('**/api/plugins/hermes-model-switcher/state', r => r.fulfill({ json: {
      ok: true, profiles: [{ id: 'default', name: 'Default', writable: true }], presets: [],
      slots: [{ profileId: 'default', slotId: 'auxiliary.follow_base', family: 'auxiliary',
        state, provider: '', model: '', writable: true, selectable: true,
        storagePath: 'auxiliary.follow_base', protocol: 'chat', lastObserved: null,
        effectiveRoute: { provider: 'lablocal', model: 'base-model' },
        metadata: { label: 'Follow base', inheritFrom: state === 'inherit' ? 'compression' : null } }],
    } }));
    await page.goto('/fixture-sdk.html');
    await expect(page.locator('[data-row="default/auxiliary.follow_base"] [data-column="state"]')).toHaveText(state === 'inherit' ? 'Inherited' : 'Unset');
    await expect(page.locator('[data-row="default/auxiliary.follow_base"] [data-column="observed"]')).toHaveText('No recorded usage');
    await page.getByLabel('Details for default auxiliary.follow_base', { exact: true }).click();
    const detail = page.locator('[data-detail="default/auxiliary.follow_base"]');
    await expect(detail).toContainText('effective:');
    await expect(detail).toContainText('lablocal');
    await expect(detail).toContainText('base-model');
    const effective = detail.locator('code').filter({ hasText: 'lablocal' });
    await expect(effective).toBeVisible();
    const rect = await effective.boundingBox();
    expect(rect.x + rect.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    const root = path.join(process.env.TMPDIR, 'model-switcher-aux-review');
    fs.mkdirSync(root, { recursive: true });
    await page.screenshot({ path: path.join(root, `${state === 'inherit' ? 'inherited' : 'default-route'}-${width}.png`), fullPage: true });
    expect(errors).toEqual([]);
  });
}
