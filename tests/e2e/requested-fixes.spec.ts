import { test, expect } from '@playwright/test';
import { renderedChart, type LiveChartHost } from './chart-helpers';

test('TRACE decision overlays accept nullable legacy event labels and remain usable', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/rpc', async (route) => {
    if (route.request().postDataJSON()?.method !== 'get_trace_data') return route.continue();
    const response = await route.fetch();
    const payload = await response.json();
    const times = [
      ...new Set<string>(payload.result.heatmap.map((row: { timestamp: string }) => row.timestamp)),
    ];
    payload.result.overlays = [
      { timestamp: times.at(-3), overlay_type: 'scenario', label: null },
      { timestamp: times.at(-2), overlay_type: 'alert' },
      { timestamp: times.at(-1), overlay_type: 'scenario', label: '  ' },
    ];
    await route.fulfill({ response, json: payload });
  });
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible();
  await page.getByRole('button', { name: 'TRACE', exact: true }).click();
  const trace = page.locator('[data-view-panel="trace"]');
  await expect(trace.getByLabel('Replay cursor', { exact: true })).toBeVisible();
  await trace.getByLabel('Decision overlays').check();
  const heatmap = trace.getByRole('img', { name: 'Net gamma heatmap by strike and session time' });
  await renderedChart(heatmap);
  await expect
    .poll(() =>
      heatmap.locator('.lwc-chart-host').evaluate((element) => {
        const host = element as LiveChartHost;
        return host.__lightweightOverlays?.map((overlay) => overlay.label).slice(-3);
      }),
    )
    .toEqual(['scenario', 'alert', 'scenario']);
  await trace.getByLabel('Replay cursor', { exact: true }).fill('10');
  await expect(trace.getByLabel('Replay cursor', { exact: true })).toHaveValue('10');
  await expect(page.getByText('This view could not render', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Cockpit', exact: true }).click();
  await page.getByRole('button', { name: 'TRACE', exact: true }).click();
  await expect(trace.getByLabel('Decision overlays')).toBeChecked();
  expect(errors).toEqual([]);
});

test('Gamma Sweep follows Gamma landscape and notifications occupy a right column', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible();
  const cockpit = page.locator('[data-view-panel="cockpit"]');
  const landscape = cockpit
    .locator('.panel')
    .filter({
      has: page.getByRole('heading', { name: 'OI-based positioning proxy', exact: true }),
    });
  const sweep = cockpit
    .locator('.panel')
    .filter({ has: page.getByRole('heading', { name: 'Gamma sweep', exact: true }) });
  await expect(landscape).toBeVisible();
  expect(
    await landscape.evaluate(
      (element) => element.nextElementSibling?.querySelector('h2')?.textContent,
    ),
  ).toBe('Gamma sweep');
  await cockpit.getByLabel('Show modeled sweep', { exact: true }).check();
  const notifications = page.getByRole('complementary', { name: 'Notifications', exact: true });
  for (const width of [1500, 1024]) {
    await page.setViewportSize({ width, height: 950 });
    await expect(notifications).toBeVisible();
    await expect
      .poll(async () => {
        const mainBox = (await page.locator('main').boundingBox())!;
        const railBox = (await notifications.boundingBox())!;
        return railBox.x >= mainBox.x + mainBox.width - 1;
      })
      .toBe(true);
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
      )
      .toBe(true);
    const upper = (await landscape.boundingBox())!;
    const lower = (await sweep.boundingBox())!;
    expect(Math.abs(lower.x - upper.x)).toBeLessThan(2);
    expect(lower.y).toBeGreaterThanOrEqual(upper.y + upper.height);
  }
  await page.setViewportSize({ width: 1500, height: 950 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'docs/screenshots/right-notifications.png', fullPage: true });
});
