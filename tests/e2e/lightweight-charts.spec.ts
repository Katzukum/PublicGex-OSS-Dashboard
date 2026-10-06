import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import {
  chartState,
  hoverChart,
  profileLabel,
  renderedChart,
  sweepLabel,
  zoomChart,
  type LiveChartHost,
} from './chart-helpers';

test('native chart families render their data and TRACE retains wall-clock timestamps', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible();
  const cockpit = page.locator('[data-view-panel="cockpit"]');
  await expect(cockpit.getByLabel('Show modeled sweep', { exact: true })).toBeChecked();
  const profile = cockpit.getByRole('img', { name: profileLabel, exact: true });
  await renderedChart(profile);
  const profileData = await chartState(profile);
  expect(
    profileData.series.filter((series) => series.type === 'Histogram').length,
  ).toBeGreaterThanOrEqual(2);
  expect(profileData.series.some((series) => series.type === 'Line' && series.samples > 10)).toBe(
    true,
  );
  const sweep = cockpit.getByRole('img', { name: sweepLabel, exact: true });
  await renderedChart(sweep);
  const sweepData = await chartState(sweep);
  expect(sweepData.series.find((series) => series.name.startsWith('Modeled net gamma'))?.axis).toBe(
    'left',
  );
  expect(
    sweepData.series.find((series) => series.name.startsWith('Cumulative hedge demand'))?.axis,
  ).toBe('right');
  const trend = cockpit.getByRole('img', {
    name: 'Historical net gamma and spot price',
    exact: true,
  });
  await renderedChart(trend);
  expect((await chartState(trend)).range?.from).toBeGreaterThan(1_000_000_000);
  await page.getByRole('button', { name: 'Regime', exact: true }).click();
  const compasses = page.locator('[data-view-panel="regime"] .lwc-chart-shell');
  await expect.poll(() => compasses.count()).toBeGreaterThanOrEqual(2);
  for (const chart of await compasses.all()) await renderedChart(chart);
  // The seeded completed opportunity history belongs to SPX and NDX.
  await page.getByLabel('Selected instrument', { exact: true }).selectOption('SPX');
  await page.getByRole('button', { name: 'Edge Lab', exact: true }).click();
  const evidence = page.locator('[data-view-panel="edge"]');
  await renderedChart(
    evidence.getByRole('img', {
      name: 'Distribution of after-cost opportunity results',
      exact: true,
    }),
  );
  await renderedChart(
    evidence.getByRole('img', { name: 'Historical outcome categories', exact: true }),
  );
  const response = page.waitForResponse(
    (result) =>
      result.url().endsWith('/api/rpc') &&
      result.request().postDataJSON()?.method === 'get_trace_data',
  );
  await page.getByRole('button', { name: 'TRACE', exact: true }).click();
  const payload = (await (await response).json()).result;
  const trace = page.locator('[data-view-panel="trace"]');
  const heatmap = trace.getByRole('img', {
    name: 'Net gamma heatmap by strike and session time',
    exact: true,
  });
  const host = await renderedChart(heatmap);
  const heatmapState = await host.evaluate((element) => {
    const host = element as LiveChartHost;
    const custom = host.__lightweightSeries.find(({ series }) => series.seriesType() === 'Custom')!;
    const data = custom.series.data() as { time: number; cells?: { value: number | null }[] }[];
    return {
      times: data.map((row) => row.time),
      cells: data.reduce((sum, row) => sum + (row.cells?.length ?? 0), 0),
      hasCandles: host.__lightweightSeries.some(
        ({ series }) => series.seriesType() === 'Candlestick' && series.data().length > 0,
      ),
    };
  });
  expect(heatmapState.cells).toBeGreaterThan(50);
  expect(heatmapState.hasCandles).toBe(true);
  expect(
    heatmapState.times.every(
      (time, index, values) => Number.isFinite(time) && (index === 0 || time > values[index - 1]!),
    ),
  ).toBe(true);
  const backendTimes = new Set(
    payload.heatmap.map(
      (row: { timestamp: string }) =>
        Date.parse(`${row.timestamp.replace(' ', 'T').replace(/(?:Z|[+-]\d{2}:?\d{2})$/, '')}Z`) /
        1000,
    ),
  );
  expect(heatmapState.times.every((time) => backendTimes.has(time))).toBe(true);
  await trace.getByLabel('Strike profile', { exact: true }).selectOption('cursor');
  await renderedChart(
    trace.getByRole('img', { name: 'Exposure profile at the replay cursor', exact: true }),
  );
  await expect(page.locator('.js-plotly-plot')).toHaveCount(0);
});

test('chart crosshair, wheel zoom, drag pan, reset, and PNG export use the live chart', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible();
  const cockpit = page.locator('[data-view-panel="cockpit"]');
  const profile = cockpit.getByRole('img', { name: profileLabel, exact: true });
  await hoverChart(page, profile);
  await expect(profile.locator('.chart-legend')).toContainText('Call');
  await zoomChart(page, profile);
  const beforePan = (await chartState(profile)).logicalRange!;
  const box = (await profile.locator('.lwc-chart-host').boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.65, box.y + box.height * 0.45);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.35, box.y + box.height * 0.45, { steps: 12 });
  await page.mouse.up();
  await expect
    .poll(async () =>
      Math.abs(((await chartState(profile)).logicalRange?.from ?? beforePan.from) - beforePan.from),
    )
    .toBeGreaterThan(0.01);
  await profile.getByRole('button', { name: 'Reset chart', exact: true }).click();
  await expect
    .poll(async () => {
      const range = (await chartState(profile)).logicalRange!;
      return range.to - range.from;
    })
    .toBeGreaterThan(beforePan.to - beforePan.from);
  const download = page.waitForEvent('download');
  await profile.getByRole('button', { name: 'Export chart PNG', exact: true }).click();
  const png = await download;
  expect(png.suggestedFilename()).toMatch(/\.png$/i);
  const bytes = await readFile((await png.path())!);
  expect([...bytes.subarray(0, 8)]).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
  expect(bytes.length).toBeGreaterThan(2000);
  await hoverChart(page, cockpit.getByRole('img', { name: sweepLabel, exact: true }));
});

test('fractional native zoom survives theme changes, tab switches, and refresh', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible();
  const cockpit = page.locator('[data-view-panel="cockpit"]');
  await cockpit.getByLabel('Profile orientation', { exact: true }).selectOption('horizontal');
  const profile = cockpit.getByRole('img', { name: profileLabel, exact: true });
  const host = await renderedChart(profile);
  await host.evaluate((element) =>
    (element as LiveChartHost).__lightweightChart
      .timeScale()
      .setVisibleLogicalRange({ from: 5.25, to: 20.75 }),
  );
  const roundedRange = async () => {
    const range = (await chartState(profile)).logicalRange!;
    return { from: Number(range.from.toFixed(6)), to: Number(range.to.toFixed(6)) };
  };
  await expect.poll(roundedRange).toEqual({ from: 5.25, to: 20.75 });
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.getByRole('button', { name: 'Cockpit', exact: true }).click();
  await expect(cockpit.getByLabel('Profile orientation', { exact: true })).toHaveValue(
    'horizontal',
  );
  await expect.poll(roundedRange).toEqual({ from: 5.25, to: 20.75 });
  const refreshed = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/rpc') &&
      response.request().postDataJSON()?.method === 'get_decision_workspace',
  );
  await page.getByRole('button', { name: 'Refresh workspace', exact: true }).click();
  await (await refreshed).finished();
  await expect.poll(roundedRange).toEqual({ from: 5.25, to: 20.75 });
  await page.getByRole('button', { name: 'TRACE', exact: true }).click();
  await page.getByRole('button', { name: 'Cockpit', exact: true }).click();
  await expect.poll(roundedRange).toEqual({ from: 5.25, to: 20.75 });
});
