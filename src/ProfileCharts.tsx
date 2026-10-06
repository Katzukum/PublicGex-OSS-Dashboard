import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AreaSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApiBase,
} from 'lightweight-charts';
import { LightweightChart } from './LightweightChart';
import { ChartLevelLegend } from './ChartLevelLegend';
import { Empty, Field } from './components';
import { COLORS } from './chartConstants';
import type { ChartBuildResult, ChartSeriesInfo } from './chartTypes';
import { aggregateStrikes, finite, number } from './models';
import { zeroGammaLevels } from './parityModels';
import {
  CompassPrimitive,
  HorizontalProfilePrimitive,
  VerticalLevelsPrimitive,
  VerticalProfilePrimitive,
  focusedProfileRange,
  nearestProfileRow,
  numericGrid,
  profileScale,
  sampleNumericCurve,
  strikeLattice,
  type ChartLevel,
  type CompassPoint,
} from './profilePrimitives';
import type { Compass, Dashboard, Theme } from './types';

const NET_COLOR = '#b692ff';
const plainSeries = {
  lastValueVisible: false,
  priceLineVisible: false,
  crosshairMarkerVisible: false,
};
const exposureFormat = {
  type: 'custom' as const,
  formatter: (value: number) => `$${number(value, 2)}M`,
  minMove: 0.001,
};
const strikeFormat = {
  type: 'custom' as const,
  formatter: (value: number) => number(value),
  minMove: 0.01,
};

export function ProfileChart({
  dashboard,
  theme,
  target,
  invalidation,
}: {
  dashboard: Dashboard;
  theme: Theme;
  target?: number | null;
  invalidation?: number | null;
}) {
  const rows = useMemo(
    () =>
      aggregateStrikes(dashboard.profile).map((row) => ({
        ...row,
        call: row.call / 1e6,
        put: row.put / 1e6,
        net: row.net / 1e6,
      })),
    [dashboard.profile],
  );
  const [orientation, setOrientation] = useState<'vertical' | 'horizontal'>('vertical');
  const [manualRange, setManualRange] = useState<[number, number] | null>();
  const [focusRevision, setFocusRevision] = useState(0);
  const clickedStrike = useRef<number | undefined>(undefined);
  const strikeViewport = useRef<{ from: number; to: number } | null>(null);
  const viewportRevision = useRef('');
  const symbol = dashboard.snapshot.symbol,
    spot = dashboard.snapshot.spot_price;
  const min = rows[0]?.strike,
    max = rows.at(-1)?.strike;
  useEffect(() => {
    setManualRange(undefined);
    strikeViewport.current = null;
    clickedStrike.current = undefined;
  }, [symbol]);
  const levels = useMemo<ChartLevel[]>(
    () =>
      [
        { value: spot, label: 'Spot', color: COLORS.accent },
        ...zeroGammaLevels(dashboard.gamma_sweep, dashboard.snapshot.flip_strike).map((level) => ({
          ...level,
          color: COLORS.amber,
        })),
        { value: target, label: 'Target', color: COLORS.positive },
        { value: invalidation, label: 'Invalidation', color: COLORS.negative },
      ].filter((level): level is ChartLevel => finite(level.value)),
    [spot, dashboard.gamma_sweep, dashboard.snapshot.flip_strike, target, invalidation],
  );
  const defaultRange = useMemo(
    () =>
      focusedProfileRange(
        rows.map((row) => row.strike),
        levels,
        spot,
      ),
    [rows, spot, levels],
  );
  const range: [number, number] | undefined =
    manualRange === null
      ? finite(min) && finite(max)
        ? [min - (min === max ? 0.5 : 0), max + (min === max ? 0.5 : 0)]
        : undefined
      : (manualRange ?? defaultRange);
  const { sideMax, netMax } = useMemo(() => profileScale(rows, range), [rows, range]);
  const resetViewport = (value: [number, number] | null | undefined) => {
    setManualRange(value);
    strikeViewport.current = null;
    setFocusRevision((previous) => previous + 1);
  };
  const zoomAround = () => {
    const requested = clickedStrike.current ?? spot;
    const center = Math.max(min ?? requested, Math.min(max ?? requested, requested));
    const steps = rows
      .slice(1)
      .map((row, index) => row.strike - rows[index]!.strike)
      .filter((step) => step > 0)
      .sort((a, b) => a - b);
    const step = steps[Math.floor(steps.length / 2)] ?? 1;
    const from = Math.max(min ?? center - step * 3, center - step * 3);
    const to = Math.min(max ?? center + step * 3, center + step * 3);
    resetViewport(from < to ? [from, to] : [center - 0.5, center + 0.5]);
  };
  const build = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      if (orientation === 'vertical') {
        chart.applyOptions({
          leftPriceScale: {
            visible: true,
            autoScale: true,
            scaleMargins: { top: 0.12, bottom: 0.08 },
          },
          rightPriceScale: {
            visible: true,
            autoScale: true,
            scaleMargins: { top: 0.12, bottom: 0.08 },
          },
        });
        const sideOptions = {
          ...plainSeries,
          priceScaleId: 'right',
          priceFormat: exposureFormat,
          base: 0,
          autoscaleInfoProvider: () => ({ priceRange: { minValue: -sideMax, maxValue: sideMax } }),
        };
        const call = chart.addSeries(HistogramSeries, {
          ...sideOptions,
          color: COLORS.positive,
          title: 'Call GEX',
        });
        const put = chart.addSeries(HistogramSeries, {
          ...sideOptions,
          color: COLORS.negative,
          title: 'Put GEX',
        });
        const net = chart.addSeries(LineSeries, {
          ...plainSeries,
          title: 'Net GEX',
          color: NET_COLOR,
          lineWidth: 2,
          priceScaleId: 'left',
          priceFormat: exposureFormat,
          autoscaleInfoProvider: () => ({ priceRange: { minValue: -netMax, maxValue: netMax } }),
        });
        const lattice = strikeLattice(rows.map((row) => row.strike));
        const domain = lattice ?? numericGrid(min ?? 0, max ?? 1, 401);
        if (lattice) {
          const byStrike = new Map(rows.map((row) => [row.strike, row]));
          call.setData(
            domain.map((time) => {
              const row = byStrike.get(time);
              return row ? { time, value: row.call } : { time };
            }),
          );
          put.setData(
            domain.map((time) => {
              const row = byStrike.get(time);
              return row ? { time, value: row.put } : { time };
            }),
          );
          net.setData(rows.map((row) => ({ time: row.strike, value: row.net })));
        } else {
          // Preserve exact strike positions without allocating a huge lattice for nonstandard strikes.
          call.applyOptions({ color: 'rgba(0,0,0,0)' });
          put.applyOptions({ color: 'rgba(0,0,0,0)' });
          net.applyOptions({ lineVisible: false });
          const scaffold = domain.map((time) => ({ time, value: 0 }));
          call.setData(scaffold);
          put.setData(scaffold);
          net.setData(scaffold);
          call.attachPrimitive(new VerticalProfilePrimitive(rows, 'call', domain));
          put.attachPrimitive(new VerticalProfilePrimitive(rows, 'put', domain));
          net.attachPrimitive(new VerticalProfilePrimitive(rows, 'net', domain));
        }
        net.createPriceLine({
          price: 0,
          color: theme === 'dark' ? '#7287a0' : '#95a4b5',
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: false,
        });
        net.attachPrimitive(new VerticalLevelsPrimitive(levels, domain, theme));
        return {
          overlays: levels,
          series: [
            { series: call, name: 'Call GEX · right $M', color: COLORS.positive },
            { series: put, name: 'Put GEX · right $M', color: COLORS.negative },
            { series: net, name: 'Net GEX · left $M', color: NET_COLOR },
          ],
          tooltip: (event) => {
            const row = finite(event.time) ? nearestProfileRow(rows, event.time) : undefined;
            return row
              ? [
                  `Strike ${number(row.strike)}`,
                  `Call GEX $${number(row.call, 3)}M`,
                  `Put GEX $${number(row.put, 3)}M`,
                  `Net GEX $${number(row.net, 3)}M`,
                ]
              : [];
          },
        };
      }
      chart.applyOptions({
        leftPriceScale: { visible: false },
        rightPriceScale: {
          visible: true,
          autoScale: true,
          scaleMargins: { top: 0.12, bottom: 0.06 },
        },
      });
      const domain = numericGrid(-sideMax, sideMax),
        initialRange = range ?? [min ?? 0, max ?? 1];
      const mid = (initialRange[0] + initialRange[1]) / 2;
      const scaffoldData = domain.map((time) => ({ time, value: mid }));
      const makeScaffold = () => {
        const series = chart.addSeries(LineSeries, {
          ...plainSeries,
          color: 'rgba(0,0,0,0)',
          lineVisible: false,
          priceFormat: strikeFormat,
          autoscaleInfoProvider: () => ({
            priceRange: { minValue: initialRange[0], maxValue: initialRange[1] },
          }),
        });
        series.setData(scaffoldData);
        return series;
      };
      const call = makeScaffold(),
        put = makeScaffold(),
        net = makeScaffold(),
        markers = makeScaffold();
      const options = { domain, sideMax, netMax, theme };
      call.attachPrimitive(
        new HorizontalProfilePrimitive(
          rows.map((row) => ({ strike: row.strike, call: row.call, put: 0 })),
          options,
        ),
      );
      put.attachPrimitive(
        new HorizontalProfilePrimitive(
          rows.map((row) => ({ strike: row.strike, call: 0, put: row.put })),
          options,
        ),
      );
      net.attachPrimitive(
        new HorizontalProfilePrimitive(
          rows.map((row) => ({ strike: row.strike, call: 0, put: 0, net: row.net })),
          options,
        ),
      );
      markers.attachPrimitive(new HorizontalProfilePrimitive([], { ...options, levels }));
      const nextRevision = `${symbol}:${orientation}:${focusRevision}`;
      const saved = viewportRevision.current === nextRevision ? strikeViewport.current : null;
      chart
        .priceScale('right')
        .setVisibleRange(saved ?? { from: initialRange[0], to: initialRange[1] });
      viewportRevision.current = nextRevision;
      const series: ChartSeriesInfo[] = [
        { series: call, name: 'Call GEX', color: COLORS.positive },
        { series: put, name: 'Put GEX', color: COLORS.negative },
        { series: net, name: 'Net GEX · top scale', color: NET_COLOR },
        { series: markers, name: 'Strike levels', color: COLORS.accent, hidden: true },
      ];
      return {
        series,
        overlays: levels,
        cleanup: () => {
          strikeViewport.current = chart.priceScale('right').getVisibleRange();
        },
        tooltip: (event) => {
          const strike = event.point ? call.coordinateToPrice(event.point.y) : null;
          const row = strike === null ? undefined : nearestProfileRow(rows, strike);
          return row
            ? [
                `Strike ${number(row.strike)}`,
                `Call GEX $${number(row.call, 3)}M`,
                `Put GEX $${number(row.put, 3)}M`,
                `Net GEX $${number(row.net, 3)}M`,
              ]
            : [];
        },
      };
    },
    [rows, orientation, sideMax, netMax, levels, theme, range, min, max, symbol, focusRevision],
  );
  if (!rows.length) return <Empty>No option profile is stored for this snapshot.</Empty>;
  return (
    <>
      <div className="parity-chart-tools">
        <Field label="Profile orientation">
          <select
            value={orientation}
            onChange={(event) => {
              setOrientation(event.target.value as typeof orientation);
              resetViewport(undefined);
            }}
          >
            <option value="vertical">Strike on X · original</option>
            <option value="horizontal">Strike on Y</option>
          </select>
        </Field>
        <button onClick={() => resetViewport(undefined)}>Focus spot &amp; levels</button>
        <button onClick={() => resetViewport(null)}>All strikes</button>
        <span className="muted">Net and call/put use separate exposure axes</span>
      </div>
      {orientation === 'horizontal' && <ChartLevelLegend levels={levels} />}
      <LightweightChart
        theme={theme}
        label="Gamma by strike: call, put, and net exposure with spot and scenario levels"
        height={430}
        axis="number"
        revision={`${symbol}:${orientation}`}
        build={build}
        range={orientation === 'vertical' ? range : [-sideMax, sideMax]}
        rangeKey={`${symbol}:${orientation}:${focusRevision}`}
        xFormat={(value) => number(value)}
        xTitle={orientation === 'vertical' ? 'Strike' : 'Call / put GEX · $M'}
        yTitle={orientation === 'vertical' ? 'Net GEX · left $M / Call & put · right $M' : 'Strike'}
        onRangeChange={
          orientation === 'vertical'
            ? (next) =>
                setManualRange((previous) =>
                  previous &&
                  Math.abs(previous[0] - next.from) < 1e-8 &&
                  Math.abs(previous[1] - next.to) < 1e-8
                    ? previous
                    : [next.from, next.to],
                )
            : undefined
        }
        onPointClick={(x, y) => {
          const strike = orientation === 'vertical' ? x : y;
          if (finite(strike)) clickedStrike.current = strike;
        }}
        onDoubleClick={zoomAround}
      />
    </>
  );
}

export function SweepChart({ dashboard, theme }: { dashboard: Dashboard; theme: Theme }) {
  const sweep = dashboard.gamma_sweep;
  const points = useMemo(
    () =>
      (sweep?.points ?? [])
        .filter((point) => finite(point.spot) && finite(point.net_gex))
        .slice()
        .sort((a, b) => a.spot - b.spot),
    [sweep?.points],
  );
  const crossings = useMemo(
    () =>
      [
        ...new Set(
          (
            sweep?.zero_crossings?.all ?? [
              sweep?.zero_crossings?.below,
              sweep?.zero_crossings?.above,
            ]
          ).filter(finite),
        ),
      ].sort((a, b) => a - b),
    [sweep?.zero_crossings],
  );
  const build = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      chart.applyOptions({
        leftPriceScale: { visible: true, autoScale: true },
        rightPriceScale: { visible: true, autoScale: true },
      });
      const gamma = chart.addSeries(AreaSeries, {
        ...plainSeries,
        title: 'Modeled net gamma',
        lineColor: COLORS.accent,
        topColor: '#89a8ff22',
        bottomColor: '#89a8ff02',
        lineWidth: 2,
        priceScaleId: 'left',
        priceFormat: exposureFormat,
      });
      const hedge = chart.addSeries(LineSeries, {
        ...plainSeries,
        title: 'Cumulative hedge demand',
        color: COLORS.amber,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        priceScaleId: 'right',
        priceFormat: {
          type: 'custom',
          formatter: (value: number) => `${number(value, 2)}M`,
          minMove: 0.001,
        },
      });
      const domain = numericGrid(
        points[0]?.spot ?? 0,
        points.at(-1)?.spot ?? 1,
        Math.min(2001, Math.max(401, points.length * 4 + 1)),
      );
      gamma.setData(
        sampleNumericCurve(
          points,
          domain,
          (point) => point.spot,
          (point) => point.net_gex / 1e6,
        ),
      );
      hedge.setData(
        sampleNumericCurve(
          points,
          domain,
          (point) => point.spot,
          (point) => (finite(point.hedge_shares) ? point.hedge_shares / 1e6 : null),
        ),
      );
      gamma.createPriceLine({
        price: 0,
        color: theme === 'dark' ? '#7287a0' : '#95a4b5',
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: false,
      });
      const levels = [
        { value: dashboard.snapshot.spot_price, label: 'Spot', color: COLORS.amber },
        ...crossings.map((value) => ({ value, label: 'Zero gamma', color: COLORS.positive })),
      ];
      gamma.attachPrimitive(new VerticalLevelsPrimitive(levels, domain, theme));
      return {
        overlays: levels,
        series: [
          { series: gamma, name: 'Modeled net gamma · left $M', color: COLORS.accent },
          { series: hedge, name: 'Cumulative hedge demand · right M shares', color: COLORS.amber },
        ],
        tooltip: (event) => {
          const point = finite(event.time)
            ? points.reduce<(typeof points)[number] | undefined>(
                (nearest, row) =>
                  !nearest ||
                  Math.abs(row.spot - event.time!) < Math.abs(nearest.spot - event.time!)
                    ? row
                    : nearest,
                undefined,
              )
            : undefined;
          return point
            ? [
                `Hypothetical spot ${number(point.spot)}`,
                `Modeled net gamma $${number(point.net_gex / 1e6, 3)}M`,
                `Cumulative hedge demand ${finite(point.hedge_shares) ? `${number(point.hedge_shares / 1e6, 3)}M shares` : 'unavailable'}`,
              ]
            : [];
        },
      };
    },
    [points, crossings, dashboard.snapshot.spot_price, theme],
  );
  if (sweep?.status !== 'ok' || !points.length)
    return (
      <Empty title="Sweep unavailable">
        {sweep?.reason ||
          'A gamma sweep requires a usable option chain. This chart is modeled, not an observed trade flow.'}
      </Empty>
    );
  return (
    <>
      <LightweightChart
        theme={theme}
        label="Modeled gamma sweep and cumulative hedge demand across underlying prices"
        height={320}
        revision={dashboard.snapshot.symbol}
        build={build}
        xFormat={(value) => number(value)}
        xTitle="Hypothetical underlying price"
        yTitle="Net gamma · left $M / Hedge demand · right M shares"
      />
      <p className="source-note">
        Zero-gamma crossings:{' '}
        {crossings.length
          ? crossings.map((value) => number(value)).join(' / ')
          : 'none in the modeled range'}
        . Current spot: {number(dashboard.snapshot.spot_price)}. Model:{' '}
        {sweep.model || 'stored option sensitivities'}. Hedge demand is cumulative relative to
        current spot. This is modeled sensitivity, not observed trade flow.
      </p>
    </>
  );
}

export function CompassChart({
  compass,
  theme,
  label,
}: {
  compass: Compass | undefined;
  theme: Theme;
  label: string;
}) {
  const [trail, setTrail] = useState<CompassPoint[]>([]);
  useEffect(() => {
    if (!finite(compass?.x_score) || !finite(compass?.y_score)) return;
    const point = { x: compass.x_score, y: compass.y_score };
    setTrail((previous) => {
      const last = previous.at(-1);
      return !last || Math.hypot((point.x - last.x) * 35, (point.y - last.y) * 35) > 0.5
        ? [...previous, point].slice(-6)
        : previous;
    });
  }, [compass?.x_score, compass?.y_score]);
  const build = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      const domain = numericGrid(-1, 1, 81);
      chart.applyOptions({
        leftPriceScale: { visible: false },
        rightPriceScale: {
          visible: true,
          autoScale: true,
          scaleMargins: { top: 0.03, bottom: 0.03 },
        },
      });
      const position = chart.addSeries(LineSeries, {
        ...plainSeries,
        color: 'rgba(0,0,0,0)',
        lineVisible: false,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        autoscaleInfoProvider: () => ({ priceRange: { minValue: -1, maxValue: 1 } }),
      });
      position.setData(domain.map((time) => ({ time, value: 0 })));
      const current =
        finite(compass?.x_score) && finite(compass?.y_score)
          ? { x: compass.x_score, y: compass.y_score }
          : undefined;
      const last = trail.at(-1);
      const points = current
        ? last?.x === current.x && last?.y === current.y
          ? trail
          : [...trail, current].slice(-6)
        : trail;
      position.attachPrimitive(new CompassPrimitive(points, domain, theme));
      return {
        series: [{ series: position, name: label, color: COLORS.accent }],
        tooltip: () =>
          current
            ? [
                label,
                `Gamma imbalance ${number(current.x, 3)}`,
                `Spot / flip ${number(current.y, 3)}`,
                prettyRegime(compass?.label),
              ]
            : [],
      };
    },
    [compass?.x_score, compass?.y_score, compass?.label, trail, label, theme],
  );
  if (!compass || !finite(compass.x_score) || !finite(compass.y_score))
    return <Empty title="Regime unavailable">More market observations are needed.</Empty>;
  return (
    <LightweightChart
      theme={theme}
      label={`${label}: normalized gamma imbalance versus spot and flip`}
      height={330}
      build={build}
      revision={label}
      range={[-1, 1]}
      rangeKey={label}
      legend={false}
      xFormat={(value) => number(value, 2)}
      xTitle="Short gamma ← imbalance → Long gamma"
      yTitle="Spot below / above flip"
    />
  );
}

function prettyRegime(value: string | undefined) {
  return value?.replaceAll('_', ' ') || 'Current regime';
}
