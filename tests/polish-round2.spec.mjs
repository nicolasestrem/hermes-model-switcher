import { test, expect } from '@playwright/test';

async function open(page, theme, width) {
  await page.setViewportSize({ width, height: 1000 });
  await page.route('**/fixture-sdk.html', async route => {
    const response = await route.fetch();
    const tokens = theme === 'light'
      ? '--background:#fafafa;--midground:#151515;--text-muted-color:#595959;--color-border:#777;--color-ring:#0055aa'
      : '--background:#16181d;--midground:#e6e8ee;--text-muted-color:#a5acb8;--color-border:#515965;--color-ring:#6f9bff';
    await route.fulfill({ response, body: (await response.text()).replace('</head>', `<style>:root{${tokens}}body{background:var(--background);color:var(--midground)}</style></head>`) });
  });
  await page.goto('/fixture-sdk.html');
  await expect(page.getByLabel('Select default main', { exact: true })).toBeVisible();
  await expect(page.locator('.hms-workspace')).toHaveCSS('display', width === 1440 ? 'grid' : 'block');
}

test('workflow copy and state guide selection without changing focus order', async ({ page }) => {
  await open(page, 'dark', 1440);
  await page.keyboard.press('Tab');
  const preset = page.getByRole('button', { name: /Fast lane/ });
  await expect(preset).toBeFocused();
  await expect(preset).toContainText('Load preset');
  await expect(page.getByTestId('live-status')).toContainText('state loaded');
  await expect(page.getByTestId('visible-status')).toHaveCount(0);
  await expect(page.getByTestId('selection-summary')).toHaveText('No slots selected');
  const editor = page.getByTestId('change-editor');
  await expect(page.getByTestId('editor-applicability')).toHaveText('Select writable slots in the table to start.');
  await expect(editor).toHaveAttribute('data-empty', 'true');
  await expect(page.getByTestId('catalog-summary')).toHaveText('Catalog not loaded yet');
  await expect(page.getByTestId('removal-advanced').locator('summary')).toHaveText('Advanced: remove a specific route');
  const actions = page.locator('.hms-workspace-tools');
  const note = await actions.locator('.hms-note').boundingBox();
  const refresh = await page.getByTestId('refresh-state').boundingBox();
  expect(note.y).toBeGreaterThanOrEqual(refresh.y + refresh.height);
  const row = page.locator('[data-row="default/main"]');
  const before = await row.evaluate(el => getComputedStyle(el).backgroundColor);
  await page.getByLabel('Select default main', { exact: true }).check();
  await expect(row).toHaveAttribute('data-selected', 'true');
  expect(await row.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe(before);
  expect(await row.locator('[data-column="profile"]').evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe('rgb(22, 24, 29)');
  await expect(editor).toHaveAttribute('data-empty', 'false');
  await expect(page.locator('[data-row="archived/main"]')).toContainText('Read-only');
  await expect(page.getByLabel('Select archived main', { exact: true })).toBeDisabled();
  await expect(page.getByTestId('removal-advanced')).toHaveCSS('margin-top', '8px');
  await expect(page.getByTestId('picker-advanced')).toHaveCSS('margin-top', '8px');
  await expect(page.locator('.hms-cost-ack')).toHaveCSS('margin-top', '16px');
});

for (const theme of ['light', 'dark']) for (const width of [1440, 768, 375]) {
  test(`matrix identifiers stay whole with discoverable owned scroll in ${theme} ${width}`, async ({ page }) => {
    await open(page, theme, width);
    const matrix = page.getByTestId('matrix-scroll');
    for (const column of ['configured', 'observed']) {
      const cell = matrix.locator(`[data-row="default/main"] [data-column="${column}"]`);
      await expect(cell).toHaveCSS('white-space', 'nowrap');
      expect(await cell.evaluate(el => parseFloat(getComputedStyle(el).minWidth))).toBeGreaterThanOrEqual(240);
    }
    await expect(matrix).toHaveCSS('overflow-x', 'scroll');
    // A wide pane may fit all columns; narrow panes must scroll, not wrap IDs.
    if (width < 1440) {
      expect(await matrix.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
      await matrix.focus();
      await page.keyboard.press('ArrowRight');
      await expect.poll(() => matrix.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await expect(matrix.locator('thead th')).toHaveCount(7);
  });
}
