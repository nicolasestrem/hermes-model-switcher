import { test, expect } from '@playwright/test';
import { mockHostReads } from './route-helpers.mjs';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

const out = path.join(process.env.TMPDIR, 'model-switcher-polish');
const files = ['ui/src/main.jsx', 'ui/src/api.js', 'ui/src/styles.css', 'dashboard/dist/index.js', 'dashboard/dist/style.css'];
const sourceDigest = crypto.createHash('sha256');
for (const file of files) sourceDigest.update(fs.readFileSync(file));
const lineage = { repository: process.cwd(), branch: execFileSync('git', ['branch', '--show-current'], { encoding: 'utf8' }).trim(),
  baseRevision: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
  workingTreeSurfaceDigest: sourceDigest.digest('hex'), files };
for (const theme of ['dark', 'light']) {
  for (const width of [1440, 768, 375]) {
    test(`polish layout ${theme} ${width}px with long names and keyboard scroll`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      const errors = [];
      const consoleErrors = [], failedRequests = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
      page.on('requestfailed', req => failedRequests.push({ url: req.url(), error: req.failure()?.errorText }));
      await page.route('**/fixture-sdk.html', async route => {
        const response = await route.fetch();
        const colors = theme === 'light'
          ? '--background:#fafafa;--midground:#151515;--text-muted-color:#595959;--color-border:#777;--color-ring:#0055aa;--surface-color:#fafafa;--text-color:#151515'
          : '--background:#16181d;--midground:#e6e8ee;--text-muted-color:#a5acb8;--color-border:#515965;--color-ring:#6f9bff;--surface-color:#16181d;--text-color:#e6e8ee';
        const html = (await response.text()).replace('</head>', `<style>:root{${colors}}body{background:var(--background);color:var(--midground)}</style></head>`);
        await route.fulfill({ response, body: html });
      });
      await mockHostReads(page);
    await page.route('**/tests/mock-api.js', route => route.fulfill({ body: '', contentType: 'application/javascript' }));
      const profiles = ['default', 'research', 'advisor-active-runtime-resolution-long-profile-name'].map(id => ({ id, name: id, writable: true }));
      const slots = profiles.flatMap(p => [
        { profileId: p.id, slotId: 'main', family: 'main', provider: 'openai', model: 'provider-native/exact-model-identifier', state: 'configured', writable: true, lastObserved: null },
        { profileId: p.id, slotId: 'auxiliary.vision', family: 'auxiliary', provider: '', model: '', state: 'inherit', writable: true, lastObserved: null, note: 'Pinning vision in auto mode sends a text interpretation even when the main model supports images.' },
        { profileId: p.id, slotId: 'specialist.embedding', family: 'specialist', provider: '', model: '', state: 'unset', writable: false, lastObserved: null, note: 'No runtime-configurable embedding model field.' },
      ]);
      await page.route('**/api/plugins/hermes-model-switcher/state', route => route.fulfill({ json: { ok: true, profiles, slots, presets: [] } }));
      await page.goto('/fixture-sdk.html');
      await expect(page.getByLabel('New provider', { exact: true })).toBeVisible();
      await expect(page.getByTestId('removal-advanced')).not.toHaveAttribute('open', '');
      await expect(page.getByTestId('picker-advanced')).not.toHaveAttribute('open', '');
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
      // hms-wide is applied by a ResizeObserver after mount: assert with a
      // retrying expectation instead of a one-shot computed-style read.
      await expect(page.locator('.hms-workspace')).toHaveCSS('display', width === 1440 ? 'grid' : 'block');
      await expect(page.locator('[data-column="profile"]').first()).toHaveCSS('position', 'sticky');
      const region = page.getByTestId('matrix-scroll');
      if (width === 375) {
        await region.focus();
        await page.keyboard.press('ArrowRight');
        await expect.poll(() => region.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
        await region.evaluate(el => { el.scrollLeft = 0; });
      }
      // Computed input text contrast uses the actual rendered CSS colors.
      const contrast = await page.getByLabel('New provider', { exact: true }).evaluate(el => {
        const css = getComputedStyle(el);
        const luminance = color => {
          const rgb = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => v / 255);
          const linear = rgb.map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
          return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
        };
        const fg = luminance(css.color), bg = luminance(css.backgroundColor);
        return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
      });
      expect(contrast).toBeGreaterThanOrEqual(4.5);
      fs.mkdirSync(out, { recursive: true });
      await page.screenshot({ path: path.join(out, `${theme}-${width}.png`), fullPage: true });
      fs.writeFileSync(path.join(out, `${theme}-${width}.json`), JSON.stringify({
        boundary: 'Compiled plugin in SDK/host-reset fixture, explicit UI-only data; not live Dashboard or provider execution',
        lineage, observedAt: new Date().toISOString(), width, theme, inputTextContrast: contrast, pageErrors: errors, consoleErrors, failedRequests,
      }, null, 2));
      expect(errors).toEqual([]);
      expect(consoleErrors).toEqual([]);
      expect(failedRequests).toEqual([]);
    });
  }
}
