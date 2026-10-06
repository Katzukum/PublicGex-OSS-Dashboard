import { test, expect, type Page, type Locator } from '@playwright/test';
import {
  chartState,
  renderedChart,
  zoomChart,
  sweepLabel,
  profileLabel,
  type LiveChartHost,
} from './chart-helpers';

const navigate = (page: Page, name: string) =>
  page.getByRole('button', { name, exact: true }).click();
async function open(page: Page) {
  await page.goto('/');
  await expect(page.getByLabel('Selected instrument')).toHaveValue('SPY', { timeout: 60000 });
  await expect(page.getByLabel('Selected instrument').locator('option')).toHaveText([
    'SPY',
    'QQQ',
    'IWM',
    'SPX',
    'NDX',
  ]);
  await renderedChart(page.locator('[data-view-panel="cockpit"] .lwc-chart-shell').first());
}
async function verifySweep(chart: Locator) {
  await renderedChart(chart);
  const details = await chartState(chart);
  const gamma = details.series.find((series) => series.name.startsWith('Modeled net gamma'))!;
  const hedge = details.series.find((series) => series.name.startsWith('Cumulative hedge demand'))!;
  expect(gamma.samples).toBeGreaterThan(10);
  expect(hedge.samples).toBeGreaterThan(10);
  expect(gamma.axis).not.toBe(hedge.axis);
  expect(new Set([gamma.axis, hedge.axis])).toEqual(new Set(['left', 'right']));
  expect(gamma.type).toBe('Area');
  expect(hedge.type).toBe('Line');
  expect(gamma.lines.some((line) => line.price === 0)).toBe(true);
  const guides = await chart
    .locator('.lwc-chart-host')
    .evaluate((element) => (element as LiveChartHost).__lightweightOverlays ?? []);
  expect(guides.some((guide) => guide.label === 'Spot' && Number.isFinite(guide.value))).toBe(true);
  expect(guides.some((guide) => guide.label === 'Zero gamma' && Number.isFinite(guide.value))).toBe(
    true,
  );
}

test('Gamma Sweep in both views and chart zoom survive tab switches', async ({ page }) => {
  await open(page);
  const cockpit = page.locator('[data-view-panel="cockpit"]');
  await cockpit.getByLabel('Show modeled sweep', { exact: true }).check();
  await verifySweep(cockpit.getByRole('img', { name: sweepLabel }));
  await cockpit.getByLabel('Profile orientation').selectOption('horizontal');
  const profile = cockpit.getByRole('img', {
    name: profileLabel,
  });
  const zoom = await zoomChart(page, profile);
  const range = async () => (await chartState(profile)).range;
  await page.evaluate(() => window.scrollTo(0, 550));
  const scroll = await page.evaluate(() => window.scrollY);
  await navigate(page, 'One-Off');
  const oneoff = page.locator('[data-view-panel="oneoff"]');
  await oneoff.locator('.saved-profiles button').first().click();
  await expect(oneoff.getByLabel('Show sweep', { exact: true })).toBeChecked();
  await verifySweep(oneoff.getByRole('img', { name: sweepLabel }));
  await navigate(page, 'Cockpit');
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(scroll);
  await expect(cockpit.getByLabel('Show modeled sweep')).toBeChecked();
  await expect(cockpit.getByLabel('Profile orientation')).toHaveValue('horizontal');
  await expect.poll(range).toEqual(zoom);
  await verifySweep(cockpit.getByRole('img', { name: sweepLabel }));
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'docs/screenshots/gamma-sweep.png', fullPage: true });
  await navigate(page, 'One-Off');
  await expect(oneoff.getByLabel('Show sweep', { exact: true })).toBeChecked();
  await verifySweep(oneoff.getByRole('img', { name: sweepLabel }));
});

test('TRACE retains replay position, window, metric and display controls between tabs', async ({
  page,
}) => {
  await open(page);
  await navigate(page, 'TRACE');
  const trace = page.locator('[data-view-panel="trace"]');
  const cursor = trace.getByLabel('Replay cursor', { exact: true });
  await expect(cursor).toBeVisible({ timeout: 30000 });
  await trace.getByLabel('Exposure metric').selectOption('call_gex');
  await trace.getByLabel('Replay window').selectOption('30');
  await trace.getByLabel('Decision overlays').check();
  await trace.getByLabel('Price candles').uncheck();
  await trace.getByLabel('Spot path').uncheck();
  await trace.getByLabel('Strike profile', { exact: true }).selectOption('cursor');
  await cursor.fill('10');
  await navigate(page, 'Regime');
  await expect(page.getByRole('heading', { name: 'Traders', exact: true })).toBeVisible();
  await navigate(page, 'TRACE');
  await expect(cursor).toHaveValue('10');
  await expect(trace.getByLabel('Exposure metric')).toHaveValue('call_gex');
  await expect(trace.getByLabel('Replay window')).toHaveValue('30');
  await expect(trace.getByLabel('Decision overlays')).toBeChecked();
  await expect(trace.getByLabel('Price candles')).not.toBeChecked();
  await expect(trace.getByLabel('Spot path')).not.toBeChecked();
  await expect(trace.getByLabel('Strike profile', { exact: true })).toHaveValue('cursor');
  await renderedChart(
    trace.getByRole('img', { name: 'Call gamma heatmap by strike and session time' }),
  );
});

test('matrix filters and unsaved settings persist, and sidebar and fullscreen controls work', async ({
  page,
}) => {
  await open(page);
  await navigate(page, 'Strike Matrix');
  const matrix = page.locator('[data-view-panel="matrix"]');
  await matrix.getByLabel('Filter strike').fill('6');
  await matrix.getByLabel('Sort strikes').selectOption('oi');
  await matrix.getByLabel('Include low-activity strikes').check();
  await navigate(page, 'Settings');
  const settings = page.locator('[data-view-panel="settings"]');
  await expect(settings.getByLabel('Start collector when app opens')).toBeChecked();
  await expect(settings.getByLabel('Start NinjaTrader when app opens')).toBeChecked();
  await settings.getByLabel('Instruments', { exact: true }).fill('SPY, QQQ, IWM, SPX, NDX, NVDA');
  await settings.getByText('Execution & regime settings', { exact: true }).click();
  await settings.getByLabel('Maximum risk per idea ($)', { exact: true }).fill('999');
  await navigate(page, 'Strike Matrix');
  await expect(matrix.getByLabel('Filter strike')).toHaveValue('6');
  await expect(matrix.getByLabel('Sort strikes')).toHaveValue('oi');
  await expect(matrix.getByLabel('Include low-activity strikes')).toBeChecked();
  await navigate(page, 'Settings');
  await expect(settings.getByLabel('Instruments', { exact: true })).toHaveValue(
    'SPY, QQQ, IWM, SPX, NDX, NVDA',
  );
  await expect(settings.getByLabel('Maximum risk per idea ($)', { exact: true })).toBeVisible();
  await expect(settings.getByLabel('Maximum risk per idea ($)', { exact: true })).toHaveValue(
    '999',
  );
  await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Expand sidebar', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Fullscreen', exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(true);
  await page.getByRole('button', { name: 'Exit fullscreen', exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(false);
});
