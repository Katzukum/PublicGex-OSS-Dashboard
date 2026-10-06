import { expect, test } from '@playwright/test';
import { renderedChart, type LiveChartHost } from './chart-helpers';
import type { Positioning } from '../../src/types';

test('unsigned concentration and nullable activity remain separate from the signed proxy and TRACE replay', async ({
  page,
}) => {
  await page.route('**/api/rpc', async (route) => {
    const method = route.request().postDataJSON()?.method;
    if (!['get_decision_workspace', 'get_trace_data'].includes(method)) return route.continue();
    const response = await route.fetch();
    const payload = await response.json();
    const spot =
      method === 'get_trace_data'
        ? payload.result.spot_price
        : payload.result.dashboard.snapshot.spot_price;
    const center = Math.round(spot / 5) * 5;
    const positioning: Positioning = {
      model: 'oi_activity_v1',
      as_of: '2026-10-02 10:44:35',
      dealer_direction: 'unknown',
      status: 'partial',
      warnings: ['The 15 minute activity window is not yet covered.'],
      windows_minutes: [5, 15],
      coverage: {
        observed_minutes: 7,
        max_gap_seconds: 60,
        contracts: 10,
        valid_activity_contracts: 8,
      },
      strikes: Array.from({ length: 5 }, (_, index) => ({
        strike: center + (index - 2) * 5,
        call_oi_gex: (index + 1) * 1e6,
        put_oi_gex: 2e6,
        gross_oi_gex: (index + 3) * 1e6,
        net_oi_proxy: (index - 1) * 1e6,
        call_activity_5m: index ? 1e6 : 0,
        put_activity_5m: index === 2 ? null : 0,
        activity_5m: index === 2 ? null : index ? 1e6 : 0,
        call_activity_15m: null,
        put_activity_15m: null,
        activity_15m: null,
        oi_persistence: 0.8,
      })),
      top_levels: [center - 10, center],
    };
    if (method === 'get_trace_data') payload.result.positioning = positioning;
    else payload.result.dashboard.positioning = positioning;
    await route.fulfill({ response, json: payload });
  });
  await page.goto('/');
  const cockpit = page.locator('[data-view-panel="cockpit"]');
  const panel = cockpit.locator('.positioning-panel');
  await expect(panel.getByText('Dealer direction unknown', { exact: true })).toBeVisible();
  const chart = panel.getByRole('img', {
    name: 'Unsigned OI concentration and recent activity by strike',
  });
  const host = await renderedChart(chart);
  const values = () =>
    host.evaluate((element) =>
      (element as LiveChartHost).__lightweightSeries[0]!.series.data().map((row) =>
        'value' in row ? row.value : null,
      ),
    );
  await expect.poll(values).toEqual([3, 4, 5, 6, 7]);
  await panel.getByLabel('Option side', { exact: true }).selectOption('put');
  await expect.poll(values).toEqual([2, 2, 2, 2, 2]);
  await panel.getByLabel('Positioning measure', { exact: true }).selectOption('5m');
  // LWC omits whitespace from series.data(); the missing strike must remain a gap.
  await expect.poll(values).toEqual([0, 0, 0, 0]);
  expect(
    await host.evaluate((element) => {
      const times = (element as LiveChartHost).__lightweightSeries[0]!.series.data().map(
        (row) => row.time,
      );
      return times.slice(1).map((time, index) => time - times[index]!);
    }),
  ).toEqual([5, 10, 5]);
  await expect(panel.locator('tbody')).toContainText('$0.0M');
  await expect(panel.locator('tbody')).toContainText('Unknown');
  await panel.getByLabel('Positioning measure', { exact: true }).selectOption('15m');
  await expect(panel.getByText('Activity window unavailable', { exact: true })).toBeVisible();
  await panel.getByText('Observation coverage & limitations', { exact: true }).click();
  await expect(
    panel.getByText('The 15 minute activity window is not yet covered.', { exact: true }),
  ).toBeVisible();
  await page.getByRole('button', { name: 'TRACE', exact: true }).click();
  const trace = page.locator('[data-view-panel="trace"]');
  await expect(trace.locator('.positioning-panel')).toContainText(
    'Latest snapshot · independent of replay cursor',
  );
  await trace.getByLabel('Replay cursor', { exact: true }).fill('10');
  await expect(trace.locator('.positioning-panel')).toContainText('As of 2026-10-02 10:44:35');
  const profile = trace.getByRole('img', {
    name: 'Exposure profile at the replay cursor',
    exact: true,
  });
  const profileHost = await renderedChart(profile);
  const span = () =>
    profileHost.evaluate((element) => {
      const chart = (element as LiveChartHost).__lightweightChart;
      const range = chart.priceScale('right').getVisibleRange();
      return range ? range.to - range.from : 0;
    });
  const near = await span();
  await trace.getByLabel('Profile strike window', { exact: true }).selectOption('wide');
  await expect.poll(span).toBeGreaterThan(near);
  await expect(trace.getByLabel('Profile guide levels')).toContainText('Latest spot');
});
