import { describe, expect, it } from 'vitest';
import { chartTime } from './chartConstants';
import { distributionBins } from './BasicCharts';
import { retainChartRange } from './chartViewport';
describe('Lightweight Charts data conversion', () => {
  it('preserves session wall-clock timestamps independently of local DST', () => {
    expect(chartTime('2026-09-30 13:30:00')).toBe(Date.UTC(2026, 8, 30, 13, 30) / 1000);
    expect(chartTime('2026-09-30T13:30:00-04:00')).toBe(chartTime('2026-09-30 13:30:00'));
  });
  it('bins every finite opportunity once including both endpoints', () => {
    const input = [-4, -2, 0, 0, 1, 5, NaN, Infinity];
    const bins = distributionBins(input);
    expect(bins.reduce((total, bin) => total + bin.count, 0)).toBe(6);
    expect(bins[0]?.low).toBe(-4);
    expect(bins.at(-1)?.high).toBe(5);
    expect(bins[0]?.count).toBeGreaterThan(0);
    expect(bins.at(-1)?.count).toBeGreaterThan(0);
    expect(distributionBins([2, 2])[0]).toEqual({ low: 1.5, high: 2.5, center: 2, count: 2 });
    expect(distributionBins([])).toEqual([]);
  });
  it('retains fractional zoom and blank future space across refreshes', () => {
    const range = { from: 0.25, to: 3.75 };
    expect(retainChartRange(range, [10, 20, 30], [10, 20, 30])).toEqual(range);
    expect(retainChartRange(range, [10, 20, 30], [20, 30, 40, 50])).toEqual({
      from: -0.75,
      to: 2.75,
    });
  });
  it('anchors numeric zoom to the same exposure values when the domain grows', () => {
    expect(retainChartRange({ from: 0.5, to: 1.5 }, [-10, 0, 10], [-20, 0, 20])).toEqual({
      from: 0.75,
      to: 1.25,
    });
  });
});
