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
  test(`secondary text contrast is AA on actual surfaces in ${theme} ${width}`, async ({ page }) => {
    await open(page, theme, width);
    const readings = [];
    const measure = async state => {
      const samples = await page.locator('.hms-root').evaluate(root => {
        const ctx = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
        const rgba = color => {
          ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1);
          return [...ctx.getImageData(0, 0, 1, 1).data].map((v, i) => i === 3 ? v / 255 : v);
        };
        const over = (a, b) => {
          const alpha = a[3] + b[3] * (1 - a[3]);
          return !alpha ? [0, 0, 0, 0] : [0, 1, 2].map(i => (a[i] * a[3] + b[i] * b[3] * (1 - a[3])) / alpha).concat(alpha);
        };
        const lum = c => c.slice(0, 3).map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
        const probe = document.createElement('span');
        probe.style.color = 'var(--text-muted-color)'; root.appendChild(probe);
        const muted = getComputedStyle(probe).color; probe.remove();
        return [...root.querySelectorAll('*')].filter(el => el.getClientRects().length && el.getBoundingClientRect().right > 0 && !el.closest(':disabled,[aria-disabled="true"],[hidden]') && [...el.childNodes].some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim()) && getComputedStyle(el).color === muted).map(el => {
          let text = [0, 0, 0, 0], blank = [0, 0, 0, 0];
          for (let n = el; n; n = n.parentElement) {
            const css = getComputedStyle(n), bg = rgba(css.backgroundColor);
            text = over(text, bg); blank = over(blank, bg);
            if (n === el) text = over(rgba(css.color), text);
            text[3] *= Number(css.opacity); blank[3] *= Number(css.opacity);
          }
          const a = lum(over(text, [255, 255, 255, 1])), b = lum(over(blank, [255, 255, 255, 1]));
          return { text: el.textContent.trim().slice(0, 100), ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05) };
        });
      });
      expect(samples.length).toBeGreaterThan(0);
      for (const sample of samples) expect(sample.ratio, `${state}: ${sample.text}`).toBeGreaterThanOrEqual(4.5);
      readings.push({ state, count: samples.length, min: Math.min(...samples.map(s => s.ratio)) });
    };
    await measure('initial');
    await page.getByLabel('Select default main', { exact: true }).check();
    await measure('selected');
    await page.getByTestId('catalog-toggle').click();
    await expect(page.getByTestId('catalog-table')).toBeVisible();
    await measure('catalog');
    console.log(JSON.stringify({ theme, width, secondaryContrast: readings }));
  });

  test(`matrix identifiers stay whole with discoverable owned scroll in ${theme} ${width}`, async ({ page }) => {
    await open(page, theme, width);
    const matrix = page.getByTestId('matrix-scroll');
    for (const column of ['configured', 'observed']) {
      const cell = matrix.locator(`[data-row="default/main"] [data-column="${column}"]`);
      await expect(cell).toHaveCSS('white-space', 'nowrap');
      expect(await cell.evaluate(el => parseFloat(getComputedStyle(el).minWidth))).toBeGreaterThanOrEqual(240);
    }
    for (const [column, min] of [['slot', 160], ['family', 176], ['state', 112]]) {
      const cell = column === 'slot'
        ? matrix.locator('[data-row="default/main"] td').nth(2)
        : matrix.locator(`[data-row="default/main"] [data-column="${column}"]`);
      expect(await cell.evaluate(el => parseFloat(getComputedStyle(el).minWidth))).toBeGreaterThanOrEqual(min);
    }
    const labelLines = await matrix.locator('[data-row="default/main"] .hms-expand').evaluate(el => {
      const range = document.createRange(); range.selectNodeContents(el);
      return range.getClientRects().length;
    });
    expect(labelLines).toBe(1);
    const profileHeader = await matrix.locator('thead th').nth(1).boundingBox();
    const slotHeader = await matrix.locator('thead th').nth(2).boundingBox();
    expect(profileHeader.x + profileHeader.width).toBeLessThanOrEqual(slotHeader.x);
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
