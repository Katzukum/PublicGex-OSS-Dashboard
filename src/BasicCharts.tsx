import { useCallback, useMemo } from 'react';
import {
  BaselineSeries,
  HistogramSeries,
  LineSeries,
  type AutoscaleInfo,
  type IChartApiBase,
} from 'lightweight-charts';
import { LightweightChart } from './LightweightChart';
import { COLORS, chartTime } from './chartConstants';
import type { ChartBuildResult, ChartSeriesInfo } from './chartTypes';
import { Empty } from './components';
import { finite, number } from './models';
import type { HistoryRow, Theme } from './types';

export function TrendChart({
  history,
  theme,
  revision = 'trend',
}: {
  history: HistoryRow[];
  theme: Theme;
  revision?: string;
}) {
  const observations = useMemo(
    () =>
      [...new Map(history.map((row) => [chartTime(row.timestamp), row]))]
        .filter(([time]) => Number.isFinite(time))
        .sort((a, b) => a[0] - b[0]),
    [history],
  );
  const build = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      const hasSpot = observations.some(([, row]) => finite(row.spot_price));
      chart.priceScale('left').applyOptions({ visible: hasSpot });
      const gamma = chart.addSeries(BaselineSeries, {
        baseValue: { type: 'price', price: 0 },
        topLineColor: COLORS.accent,
        bottomLineColor: COLORS.accent,
        topFillColor1: '#89a8ff24',
        topFillColor2: '#89a8ff08',
        bottomFillColor1: '#89a8ff08',
        bottomFillColor2: '#89a8ff24',
        lineWidth: 2,
        priceScaleId: hasSpot ? 'left' : 'right',
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: {
          type: 'custom',
          formatter: (value: number) => `$${number(value)}M`,
          minMove: 0.01,
        },
      });
      gamma.setData(
        observations.map(([time, row]) =>
          finite(row.total_net_gex) ? { time, value: row.total_net_gex / 1e6 } : { time },
        ),
      );
      const series: ChartSeriesInfo[] = [
        { series: gamma, name: 'Net gamma', color: COLORS.accent },
      ];
      if (hasSpot) {
        const spot = chart.addSeries(LineSeries, {
          color: COLORS.amber,
          lineWidth: 2,
          priceScaleId: 'right',
          priceLineVisible: false,
          lastValueVisible: false,
        });
        spot.setData(
          observations.map(([time, row]) =>
            finite(row.spot_price) ? { time, value: row.spot_price } : { time },
          ),
        );
        series.push({ series: spot, name: 'Spot', color: COLORS.amber });
      }
      return {
        series,
        tooltip: (event) => {
          const row = observations.find(([time]) => time === event.time)?.[1];
          return row
            ? [
                row.timestamp,
                `Net gamma: $${number(row.total_net_gex / 1e6)}M`,
                ...(finite(row.spot_price) ? [`Spot: ${number(row.spot_price)}`] : []),
              ]
            : [];
        },
      };
    },
    [observations],
  );
  return observations.length ? (
    <LightweightChart
      theme={theme}
      label="Historical net gamma and spot price"
      height={270}
      axis="time"
      revision={revision}
      build={build}
      xTitle="Session time"
      yTitle="Net gamma · $M / spot"
    />
  ) : (
    <Empty>No historical snapshots yet.</Empty>
  );
}

export interface CategoryRow {
  name: string;
  value: number;
  color?: string;
}
export function CategoryChart({
  rows,
  label,
  theme,
  height = 300,
  yTitle,
}: {
  rows: CategoryRow[];
  label: string;
  theme: Theme;
  height?: number;
  yTitle: string;
}) {
  const valid = useMemo(() => rows.filter((row) => finite(row.value)), [rows]);
  const build = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      const bars = chart.addSeries(HistogramSeries, {
        color: COLORS.accent,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        autoscaleInfoProvider: (original: () => AutoscaleInfo | null) => {
          const info = original();
          return info?.priceRange
            ? {
                ...info,
                priceRange: {
                  minValue: Math.min(0, info.priceRange.minValue),
                  maxValue: Math.max(0, info.priceRange.maxValue),
                },
              }
            : null;
        },
      });
      bars.setData(
        valid.map((row, index) => ({
          time: index + 1,
          value: row.value,
          color: row.color ?? COLORS.accent,
        })),
      );
      return {
        series: [{ series: bars, name: yTitle, color: COLORS.accent }],
        tooltip: (event) => {
          const row = valid[Number(event.time) - 1];
          return row ? [row.name, `${yTitle}: ${number(row.value)}`] : [];
        },
      };
    },
    [valid, yTitle],
  );
  return (
    <LightweightChart
      label={label}
      theme={theme}
      height={height}
      build={build}
      legend={false}
      xFormat={(value) => valid[Math.round(value) - 1]?.name ?? ''}
      yTitle={yTitle}
    />
  );
}

export interface DistributionBin {
  low: number;
  high: number;
  center: number;
  count: number;
}
export function distributionBins(values: number[]): DistributionBin[] {
  const usable = values.filter(finite);
  if (!usable.length) return [];
  const min = Math.min(...usable),
    max = Math.max(...usable);
  if (min === max) return [{ low: min - 0.5, high: min + 0.5, center: min, count: usable.length }];
  const count = Math.min(24, Math.max(4, Math.ceil(Math.sqrt(usable.length))));
  const step = (max - min) / count;
  const bins = Array.from({ length: count }, (_, index) => ({
    low: min + index * step,
    high: min + (index + 1) * step,
    center: min + (index + 0.5) * step,
    count: 0,
  }));
  for (const value of usable) bins[Math.min(count - 1, Math.floor((value - min) / step))]!.count++;
  return bins;
}
export function DistributionChart({ values, theme }: { values: number[]; theme: Theme }) {
  const bins = useMemo(() => distributionBins(values), [values]);
  const build = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      const series = chart.addSeries(HistogramSeries, {
        color: COLORS.accent,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: 'price', precision: 0, minMove: 1 },
      });
      series.setData(bins.map((bin) => ({ time: bin.center, value: bin.count })));
      return {
        series: [{ series, name: 'Independent opportunities', color: COLORS.accent }],
        tooltip: (event) => {
          const bin = bins.find((item) => item.center === event.time);
          return bin
            ? [`${number(bin.low)} to ${number(bin.high)} points`, `${bin.count} opportunities`]
            : [];
        },
      };
    },
    [bins],
  );
  return (
    <LightweightChart
      theme={theme}
      label="Distribution of after-cost opportunity results"
      height={300}
      build={build}
      legend={false}
      xTitle="After-cost points"
      yTitle="Independent opportunities"
    />
  );
}
