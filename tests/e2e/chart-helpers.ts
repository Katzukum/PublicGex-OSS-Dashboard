import { expect, type Locator, type Page } from '@playwright/test';
import type { IChartApiBase, ISeriesApi, SeriesType } from 'lightweight-charts';

export interface LiveChartHost extends HTMLElement {
  __lightweightChart: IChartApiBase<number>;
  __lightweightSeries: {
    series: ISeriesApi<SeriesType, number>;
    name: string;
    color: string;
    hidden?: boolean;
  }[];
  __lightweightOverlays?: {
    time?: number;
    value?: number;
    label: string;
    overlay_type?: string;
    color: string;
  }[];
}

export const profileLabel =
  'Gamma by strike: call, put, and net exposure with spot and scenario levels';
export const sweepLabel =
  'Modeled gamma sweep and cumulative hedge demand across underlying prices';
export const visibleChart = (page: Page) =>
  page.locator('[data-view-panel]:visible .lwc-chart-shell').first();

/** Inspect the real chart API and its raster output, not just a mounted wrapper. */
export async function renderedChart(shell: Locator) {
  await expect(shell).toHaveAttribute('data-chart-engine', 'lightweight-charts');
  const host = shell.locator('.lwc-chart-host[data-chart-ready="true"]');
  await expect(host).toBeVisible({ timeout: 60000 });
  await expect
    .poll(
      () =>
        host.evaluate((element) => {
          const host = element as LiveChartHost;
          const api = host.__lightweightChart;
          if (!api || !host.__lightweightSeries?.length) return false;
          const canvas = api.takeScreenshot();
          const context = canvas.getContext('2d');
          if (!context || canvas.width < 200 || canvas.height < 120) return false;
          const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
          let colored = 0;
          for (let i = 0; i < pixels.length; i += 16) {
            const r = pixels[i]!,
              g = pixels[i + 1]!,
              b = pixels[i + 2]!;
            if (
              pixels[i + 3]! > 0 &&
              Math.max(r, g, b) - Math.min(r, g, b) > 50 &&
              Math.max(r, g, b) > 90
            )
              colored++;
          }
          return (
            host.__lightweightSeries.some(({ series }) => series.data().length > 0) && colored > 8
          );
        }),
      { timeout: 30000 },
    )
    .toBe(true);
  return host;
}

export async function chartState(shell: Locator) {
  return shell.locator('.lwc-chart-host').evaluate((element) => {
    const host = element as LiveChartHost;
    return {
      range: host.__lightweightChart.timeScale().getVisibleRange(),
      logicalRange: host.__lightweightChart.timeScale().getVisibleLogicalRange(),
      series: host.__lightweightSeries.map(({ series, name, hidden }) => ({
        name,
        hidden: hidden ?? false,
        type: series.seriesType(),
        samples: series.data().length,
        axis: series.options().priceScaleId,
        lines: series
          .priceLines()
          .map((line) => ({ price: line.options().price, title: line.options().title })),
      })),
    };
  });
}

export async function hoverChart(page: Page, shell: Locator) {
  const host = await renderedChart(shell);
  await host.scrollIntoViewIfNeeded();
  const point = await host.evaluate((element) => {
    const host = element as LiveChartHost;
    const box = host.getBoundingClientRect();
    const item = host.__lightweightSeries.find(
      ({ hidden, series }) => !hidden && series.data().length > 1,
    )!;
    const datum = item.series.data()[Math.floor(item.series.data().length / 2)]!;
    const value =
      'value' in datum ? Number(datum.value) : 'close' in datum ? Number(datum.close) : 0;
    return {
      x:
        box.x + (host.__lightweightChart.timeScale().timeToCoordinate(datum.time) ?? box.width / 2),
      y: box.y + (item.series.priceToCoordinate(value) ?? box.height / 2),
    };
  });
  await page.mouse.move(point.x, point.y, { steps: 8 });
  await expect(shell.locator('.chart-tooltip')).toBeVisible();
  await expect(shell.locator('.chart-tooltip')).not.toBeEmpty();
}

export async function zoomChart(page: Page, shell: Locator) {
  const host = await renderedChart(shell);
  await host.scrollIntoViewIfNeeded();
  const box = (await host.boundingBox())!;
  const before = (await chartState(shell)).logicalRange!;
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
  await page.mouse.wheel(0, -550);
  await expect
    .poll(async () => {
      const range = (await chartState(shell)).logicalRange;
      return range ? range.to - range.from : Infinity;
    })
    .toBeLessThan(before.to - before.from);
  return (await chartState(shell)).range;
}
