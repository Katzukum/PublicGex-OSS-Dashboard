import { useCallback, useMemo, useState } from 'react';
import { CandlestickSeries, LineSeries, LineStyle } from 'lightweight-charts';
import type { IChartApiBase, ISeriesApi, SeriesType } from 'lightweight-charts';
import { LightweightChart } from './LightweightChart';
import { ChartLevelLegend } from './ChartLevelLegend';
import { Field } from './components';
import { TrendChart } from './BasicCharts';
import { chartTime, COLORS, metricLabels } from './chartConstants';
import type { ChartBuildResult } from './chartTypes';
import { finite, number } from './models';
import { traceCandles, traceStrikeRange, traceProfileStrikeRange } from './parityModels';
import { HorizontalProfilePrimitive, numericGrid } from './profilePrimitives';
import {
  heatmapColor,
  TraceHeatmapSeries,
  TraceOverlayPrimitive,
  traceHeatmapData,
  traceOverlayData,
  traceReplayRange,
  traceViewportSelection,
} from './traceHeatmap';
import type { HeatmapColumn } from './traceHeatmap';
import type { Theme, TraceData, TraceMetric } from './types';

export interface TraceChartsProps {
  data: TraceData;
  metric: TraceMetric;
  theme: Theme;
  overlays: boolean;
  cursor: number;
  width: number;
  profileMode?: 'latest' | 'cursor';
  showCandles?: boolean;
  showSpot?: boolean;
  resetRevision?: number;
  onViewportChange?: (cursor: number, width: number) => void;
}

export function TraceCharts({
  data,
  metric,
  theme,
  overlays,
  cursor,
  width,
  profileMode = 'latest',
  showCandles = true,
  showSpot = true,
  resetRevision = 0,
  onViewportChange,
}: TraceChartsProps) {
  const [profileWindow, setProfileWindow] = useState('near');
  const surface = useMemo(() => traceHeatmapData(data.heatmap, metric), [data.heatmap, metric]);
  const times = useMemo(() => surface.columns.map((column) => column.time), [surface]);
  const timestamps = useMemo(
    () => [...new Set(data.heatmap.map((row) => row.timestamp))].sort(),
    [data.heatmap],
  );
  const candles = useMemo(
    () => traceCandles(data.spot_ticks ?? [], timestamps),
    [data.spot_ticks, timestamps],
  );
  const strikeRange = useMemo(() => traceStrikeRange(data), [data]);
  const modeled = metric.startsWith('modeled_');
  const units =
    metric === 'modeled_charm_pressure' ? 'M shares/day' : modeled ? 'M shares' : 'M USD';
  const annotations = useMemo(
    () => (overlays ? traceOverlayData(data.overlays) : []),
    [overlays, data.overlays],
  );
  const levels = useMemo(
    () =>
      [
        { value: data.spot_price, label: 'Latest spot', color: COLORS.amber },
        { value: data.flip_strike, label: 'Flip', color: COLORS.negative },
      ].filter(
        (level): level is { value: number; label: string; color: string } =>
          finite(level.value) && level.value > 0,
      ),
    [data.spot_price, data.flip_strike],
  );
  const range = useMemo(() => traceReplayRange(times, cursor, width), [times, cursor, width]);
  const identity = `${data.symbol}:${data.session_date}:${resetRevision}`;

  const buildHeatmap = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      const autoscaleInfoProvider = () =>
        strikeRange ? { priceRange: { minValue: strikeRange[0], maxValue: strikeRange[1] } } : null;
      const heatmap = chart.addCustomSeries(new TraceHeatmapSeries(surface.maxAbs, theme), {
        priceLineVisible: false,
        lastValueVisible: false,
        autoscaleInfoProvider,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
      });
      heatmap.setData(surface.columns);
      const series: ChartBuildResult['series'] = [
        {
          series: heatmap as unknown as ISeriesApi<SeriesType, number>,
          name: metricLabels[metric],
          color: COLORS.positive,
        },
      ];
      chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.02, bottom: 0.02 } });
      if (showCandles && candles.length) {
        const candleSeries = chart.addSeries(CandlestickSeries, {
          upColor: theme === 'dark' ? '#e4edf8' : '#ffffff',
          downColor: theme === 'dark' ? '#152133' : '#4b5b6d',
          borderColor: theme === 'dark' ? '#aabbd0' : '#33455c',
          wickColor: theme === 'dark' ? '#d9e5f4' : '#33455c',
          priceLineVisible: false,
          lastValueVisible: false,
          autoscaleInfoProvider,
        });
        candleSeries.setData(
          candles.map((candle) => ({
            time: chartTime(candle.timestamp),
            open: candle.open,
            high: candle.high,
            low: candle.low,
            close: candle.close,
          })),
        );
        series.push({
          series: candleSeries,
          name: 'Price candles',
          color: theme === 'dark' ? '#e4edf8' : '#4b5b6d',
        });
      }
      if (showSpot && data.spot_path?.length) {
        const spot = chart.addSeries(LineSeries, {
          color: COLORS.accent,
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
          autoscaleInfoProvider,
        });
        // Provider retries can repeat timestamps; Lightweight Charts requires unique ascending times.
        const points = new Map(
          data.spot_path
            .filter((row) => finite(row.spot_price) && finite(chartTime(row.timestamp)))
            .map((row) => [chartTime(row.timestamp), row.spot_price]),
        );
        spot.setData(
          [...points].sort(([a], [b]) => a - b).map(([time, value]) => ({ time, value })),
        );
        series.push({ series: spot, name: 'Spot path', color: COLORS.accent });
      }
      for (const level of levels)
        heatmap.createPriceLine({
          price: level.value,
          title: level.label,
          color: level.color,
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: true,
        });
      const primitive = new TraceOverlayPrimitive(annotations, times, theme);
      heatmap.attachPrimitive(primitive);
      return {
        series,
        overlays: primitive.annotations,
        cleanup: () => heatmap.detachPrimitive(primitive),
        tooltip: (event) => {
          if (!event.point) return [];
          const column = event.seriesData.get(heatmap) as HeatmapColumn | undefined;
          const strike = heatmap.coordinateToPrice(event.point.y);
          const cell = column?.cells?.find(
            (item) => strike !== null && strike >= item.low && strike <= item.high,
          );
          const output =
            cell && cell.value !== null
              ? [
                  `Strike ${number(cell.strike)}`,
                  `${metricLabels[metric]} ${number(cell.value, 3)} ${units}`,
                ]
              : [];
          for (const item of series.slice(1)) {
            const observation = event.seriesData.get(item.series);
            if (observation && 'open' in observation)
              output.push(
                `Open ${number(observation.open)} · High ${number(observation.high)}`,
                `Low ${number(observation.low)} · Close ${number(observation.close)}`,
              );
            else if (observation && 'value' in observation && finite(observation.value))
              output.push(`${item.name} ${number(observation.value)}`);
          }
          return output;
        },
      };
    },
    [
      surface,
      theme,
      metric,
      strikeRange,
      showCandles,
      candles,
      showSpot,
      data.spot_path,
      levels,
      annotations,
      times,
      units,
    ],
  );

  const selectedTime =
    profileMode === 'latest'
      ? times.at(-1)
      : times[Math.max(0, Math.min(cursor < 0 ? times.length - 1 : cursor, times.length - 1))];
  const profile = useMemo(() => {
    const source =
      profileMode === 'latest' && !modeled && data.latest_profile?.length
        ? data.latest_profile
        : data.heatmap.filter((row) => chartTime(row.timestamp) === selectedTime);
    return source
      .filter((row) => finite(row.strike) && finite(row[metric]))
      .map((row) => {
        const value = row[metric]! / 1e6;
        return { strike: row.strike, call: Math.max(0, value), put: Math.min(0, value) };
      })
      .sort((a, b) => a.strike - b.strike);
  }, [profileMode, modeled, data.latest_profile, data.heatmap, selectedTime, metric]);
  const profileStrikeRange = useMemo(
    () =>
      traceProfileStrikeRange(
        profile.map((row) => row.strike),
        data.spot_price,
        profileWindow === 'all' ? null : profileWindow === 'wide' ? 0.015 : 0.0075,
      ),
    [profile, data.spot_price, profileWindow],
  );
  const visibleProfile = profile.filter(
    (row) =>
      !profileStrikeRange ||
      (row.strike >= profileStrikeRange[0] && row.strike <= profileStrikeRange[1]),
  );
  const profileMax =
    Math.max(0.001, ...visibleProfile.flatMap((row) => [row.call, -row.put])) * 1.1;
  const profileRange: [number, number] = [-profileMax, profileMax];
  const buildProfile = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      chart
        .priceScale('right')
        .applyOptions({ autoScale: true, scaleMargins: { top: 0.03, bottom: 0.03 } });
      const domain = numericGrid(-profileMax, profileMax);
      const scaffold = chart.addSeries(LineSeries, {
        color: 'transparent',
        lineVisible: false,
        pointMarkersVisible: false,
        crosshairMarkerVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        autoscaleInfoProvider: () =>
          profileStrikeRange
            ? { priceRange: { minValue: profileStrikeRange[0], maxValue: profileStrikeRange[1] } }
            : null,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
      });
      scaffold.setData(domain.map((time) => ({ time, value: data.spot_price })));
      const primitive = new HorizontalProfilePrimitive(profile, {
        domain,
        sideMax: profileMax,
        levels,
        theme,
      });
      scaffold.attachPrimitive(primitive);
      return {
        series: [{ series: scaffold, name: metricLabels[metric], color: COLORS.accent }],
        cleanup: () => scaffold.detachPrimitive(primitive),
        tooltip: (event) => {
          if (!event.point || !profile.length) return [];
          const strike = scaffold.coordinateToPrice(event.point.y);
          if (strike === null) return [];
          const row = profile.reduce((closest, item) =>
            Math.abs(item.strike - strike) < Math.abs(closest.strike - strike) ? item : closest,
          );
          return [
            `Strike ${number(row.strike)}`,
            `${metricLabels[metric]} ${number(row.call + row.put, 3)} ${units}`,
          ];
        },
      };
    },
    [profileMax, profileStrikeRange, data.spot_price, profile, levels, theme, metric, units],
  );

  const onRangeChange = useCallback(
    (visible: { from: number; to: number }) => {
      const selection = traceViewportSelection(times, visible);
      if (selection && (selection.cursor !== cursor || selection.width !== width))
        onViewportChange?.(selection.cursor, selection.width);
    },
    [times, cursor, width, onViewportChange],
  );
  const selectedLabel =
    selectedTime === undefined
      ? 'selected time'
      : new Date(selectedTime * 1000).toISOString().slice(11, 16);
  return (
    <>
      <LightweightChart
        label={`${metricLabels[metric]} heatmap by strike and session time`}
        theme={theme}
        height={460}
        axis="time"
        build={buildHeatmap}
        revision={identity}
        range={range}
        rangeKey={`${identity}:${cursor}:${width}:${cursor < 0 ? times.at(-1) : ''}`}
        onRangeChange={onRangeChange}
        xTitle="Session time"
        yTitle="Strike / underlying price"
      />
      <div
        aria-label={`${metricLabels[metric]} symmetric color scale`}
        className="source-note"
        style={{ display: 'flex', alignItems: 'center', gap: 10 }}
      >
        <span>{number(-surface.maxAbs, 3)}</span>
        <span
          style={{
            display: 'inline-block',
            width: 160,
            height: 9,
            background: `linear-gradient(to right, ${heatmapColor(-surface.maxAbs, surface.maxAbs, theme)}, ${heatmapColor(0, surface.maxAbs, theme)}, ${heatmapColor(surface.maxAbs, surface.maxAbs, theme)})`,
          }}
        />
        <span>
          {number(surface.maxAbs, 3)} {units} · zero centered
        </span>
      </div>
      <div className="two-col trace-profile-row">
        <section>
          <h3 className="chart-subtitle">
            {profileMode === 'latest' ? 'Latest snapshot' : 'Replay cursor'} · exposure at{' '}
            {selectedLabel}
          </h3>
          <div className="profile-range-control">
            <Field label="Profile strike window">
              <select
                value={profileWindow}
                onChange={(event) => setProfileWindow(event.target.value)}
              >
                <option value="near">Near spot · ±0.75%</option>
                <option value="wide">Wider · ±1.5%</option>
                <option value="all">All strikes</option>
              </select>
            </Field>
          </div>
          <ChartLevelLegend levels={levels} />
          <LightweightChart
            label="Exposure profile at the replay cursor"
            theme={theme}
            height={440}
            axis="number"
            build={buildProfile}
            revision={identity}
            range={profileRange}
            rangeKey={`${identity}:${profileWindow}`}
            xTitle={units}
            yTitle="Strike"
            xFormat={(value) => number(value, 2)}
            legend={false}
          />
        </section>
        <section>
          <h3 className="chart-subtitle">Session net gamma · {data.session_date}</h3>
          <TrendChart history={data.history ?? []} theme={theme} revision={identity} />
        </section>
      </div>
      {modeled && (
        <p className="source-note">
          Modeled sensitivity surface; not observed dealer transactions. Null cells remain gaps.
          Values shown in{' '}
          {metric === 'modeled_charm_pressure'
            ? 'millions of shares per day'
            : 'millions of shares'}
          .
        </p>
      )}
    </>
  );
}
