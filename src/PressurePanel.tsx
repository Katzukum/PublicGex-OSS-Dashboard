import { useCallback, useMemo } from 'react';
import { LineSeries, LineStyle, type IChartApiBase } from 'lightweight-charts';
import { COLORS } from './chartConstants';
import type { ChartBuildResult } from './chartTypes';
import { Empty, Metric, Notice, Panel } from './components';
import { LightweightChart } from './LightweightChart';
import { finite, money, number, percent, pretty } from './models';
import type { EstimatedPressure, PressurePoint, Theme } from './types';
import './pressure.css';

const pressureTime = (value: string) => {
  const parsed = Date.parse(`${value.replace(' ', 'T').replace(/(?:Z|[+-]\d{2}:?\d{2})$/, '')}Z`);
  return parsed / 1000;
};
const availableBy = (point: PressurePoint, asOf?: number) =>
  asOf === undefined ||
  // TRACE coordinates are stored local wall-clock labels; canonical UTC
  // availability is enforced by the backend before points reach this payload.
  pressureTime(point.timestamp) <= asOf;

export function pressurePointsAt(data: EstimatedPressure | null | undefined, asOf?: number) {
  return (data?.points ?? []).filter((point) => availableBy(point, asOf));
}

export function PressurePanel({
  data,
  theme,
  asOf,
  range,
  rangeKey,
  onRangeChange,
  title = 'Estimated Hedge Pressure',
}: {
  data?: EstimatedPressure | null;
  theme: Theme;
  asOf?: number;
  range?: [number, number];
  rangeKey?: string | number;
  onRangeChange?: (range: { from: number; to: number }) => void;
  title?: string;
}) {
  const points = useMemo(() => pressurePointsAt(data, asOf), [data, asOf]);
  const latest = points.at(-1);
  const [fastPeriod, slowPeriod] = data?.config?.ema_periods ?? [9, 21];
  const direction =
    asOf === undefined ? (data?.current?.trend ?? data?.current?.direction) : latest?.trend;
  const relativeToFast =
    finite(latest?.cumulative_dollars) && finite(latest?.ema_fast)
      ? latest.cumulative_dollars - latest.ema_fast
      : asOf === undefined
        ? data?.current?.relative_to_ema_fast
        : null;
  const build = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
      const cumulative = chart.addSeries(LineSeries, {
        color: COLORS.accent,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: 'custom', formatter: (value: number) => money(value), minMove: 1 },
      });
      const emaFast = chart.addSeries(LineSeries, {
        color: COLORS.positive,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      const emaSlow = chart.addSeries(LineSeries, {
        color: COLORS.amber,
        lineWidth: 2,
        lineStyle: LineStyle.Dotted,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      const seriesData = (key: 'cumulative_dollars' | 'ema_fast' | 'ema_slow') =>
        points.map((point) => {
          const time = pressureTime(point.timestamp);
          const value = point[key];
          return value == null ? { time } : { time, value };
        });
      cumulative.setData(seriesData('cumulative_dollars'));
      emaFast.setData(seriesData('ema_fast'));
      emaSlow.setData(seriesData('ema_slow'));
      return {
        series: [
          { series: cumulative, name: 'Observed classified pressure', color: COLORS.accent },
          { series: emaFast, name: `EMA ${fastPeriod}`, color: COLORS.positive },
          { series: emaSlow, name: `EMA ${slowPeriod}`, color: COLORS.amber },
        ],
        tooltip: (event) => {
          if (event.time === undefined) return [];
          const point = points.find((item) => pressureTime(item.timestamp) === event.time);
          return point
            ? [
                new Date(event.time * 1000).toISOString().slice(11, 16),
                `Pressure ${money(point.cumulative_dollars)}`,
                `EMA ${fastPeriod} ${money(point.ema_fast)} · EMA ${slowPeriod} ${money(point.ema_slow)}`,
                `Classification ${percent(point.classification_ratio)}`,
                point.status === 'partial' ? 'Partial minute coverage' : pretty(point.status),
              ]
            : [];
        },
      };
    },
    [points, fastPeriod, slowPeriod],
  );
  const content = !data ? (
    <Empty title="Estimated pressure unavailable">
      This payload does not include classified option-volume observations.
    </Empty>
  ) : data.status === 'unavailable' || !points.length ? (
    <>
      <Empty title="Estimated pressure unavailable">
        More completed observations and fresh option quotes are required.
      </Empty>
      {!!data.warnings.length && <Notice tone="warning">{data.warnings.join(' · ')}</Notice>}
    </>
  ) : (
    <>
      <LightweightChart
        label={`${title}: observed classified cumulative dollars with EMA ${fastPeriod} and EMA ${slowPeriod}`}
        theme={theme}
        height={300}
        axis="time"
        build={build}
        revision={`${data.session_date ?? 'unknown'}:${data.as_of ?? ''}`}
        range={range}
        rangeKey={rangeKey ?? data.session_date ?? 'unknown'}
        onRangeChange={onRangeChange}
        xTitle="Session time"
        yTitle="Observed classified pressure · USD"
      />
      <div className="pressure-summary">
        <Metric
          label="Direction"
          value={pretty(
            direction ??
              (latest?.slope_5m == null
                ? 'unavailable'
                : latest.slope_5m > 0
                  ? 'rising'
                  : latest.slope_5m < 0
                    ? 'falling'
                    : 'flat'),
          )}
        />
        <Metric label={`vs EMA ${fastPeriod}`} value={money(relativeToFast)} />
        <Metric label="5m impulse" value={money(latest?.impulse_5m ?? data.current?.impulse_5m)} />
        <Metric label="Coverage" value={percent(data.coverage.classification_ratio)} />
      </div>
      {!!data.warnings.length && <Notice tone="warning">{data.warnings.join(' · ')}</Notice>}
    </>
  );
  return (
    <Panel
      title={title}
      eyebrow="Option volume + quote inference"
      action={
        data && (
          <span className={`badge ${data.status === 'ready' ? 'positive' : 'warning'}`}>
            {pretty(data.status)}
          </span>
        )
      }
      className="pressure-panel"
    >
      {content}
      <p className="source-note">
        Estimated from option volume + quotes; dealer/customer identity is not observed. A
        snapshot's last trade cannot classify every trade between polls.
      </p>
    </Panel>
  );
}
