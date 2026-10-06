import { useEffect, useRef, useState } from 'react';
import {
  ColorType,
  CrosshairMode,
  createChart,
  createOptionsChart,
  type IChartApiBase,
  type MouseEventParams,
} from 'lightweight-charts';
import type { ChartBuildResult, ChartSeriesInfo, LightweightChartProps } from './chartTypes';
import { number } from './models';
import { retainChartRange } from './chartViewport';
import './lightweight-charts.css';

type ChartElement = HTMLDivElement & {
  __lightweightChart?: IChartApiBase<number>;
  __lightweightSeries?: ChartSeriesInfo[];
  __lightweightOverlays?: readonly unknown[];
};

const domainOf = (series: ChartSeriesInfo[]) =>
  [...new Set(series.flatMap((item) => item.series.data().map((point) => Number(point.time))))]
    .filter(Number.isFinite)
    .sort((a, b) => a - b);

/** One live chart per mounted view; updates never discard the user's viewport. */
export function LightweightChart(props: LightweightChartProps) {
  const {
    label,
    theme,
    height = 330,
    axis = 'number',
    revision = 'stable',
    build,
    range,
    rangeKey,
    xFormat,
    xTitle,
    yTitle,
    legend = true,
  } = props;
  const host = useRef<ChartElement>(null);
  const chart = useRef<IChartApiBase<number> | null>(null);
  const result = useRef<ChartBuildResult | null>(null);
  const latest = useRef(props);
  latest.current = props;
  const updating = useRef(false);
  const interactionUntil = useRef(0);
  const applied = useRef('');
  const lastRange = useRef('');
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [seriesNames, setSeriesNames] = useState<ChartSeriesInfo[]>([]);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; lines: string[] }>();
  const formatX = (value: number) =>
    latest.current.xFormat?.(value) ??
    (axis === 'time' ? new Date(value * 1000).toISOString().slice(11, 19) : number(value));
  const notifyRange = () => {
    const visible = chart.current?.timeScale().getVisibleRange();
    if (!visible) return;
    if (host.current) host.current.dataset.visibleRange = JSON.stringify(visible);
    const key = JSON.stringify(visible);
    if (
      !updating.current &&
      performance.now() < interactionUntil.current &&
      key !== lastRange.current
    ) {
      lastRange.current = key;
      latest.current.onRangeChange?.(visible);
    }
  };
  useEffect(() => {
    const target = host.current;
    if (!target) return;
    // The time implementation accepts UTC epoch seconds; the common API uses
    // number so option prices and timestamp values share the same host lifecycle.
    const instance =
      axis === 'time'
        ? (createChart(target, {
            width: Math.max(target.clientWidth, 300),
            height,
          }) as unknown as IChartApiBase<number>)
        : createOptionsChart(target, { width: Math.max(target.clientWidth, 300), height });
    chart.current = instance;
    target.__lightweightChart = instance;
    applied.current = '';
    if (axis === 'number') {
      const behavior = instance.horzBehaviour();
      behavior.formatHorzItem = (value) => formatX(Number(value));
      behavior.formatTickmark = (mark) => formatX(Number(mark.time));
      behavior.shouldResetTickmarkLabels = () => true;
    }
    const moving = (event: MouseEventParams<number>) => {
      if (updating.current || !event.point || event.time === undefined) {
        setTooltip(undefined);
        return;
      }
      const current = result.current;
      const lines = current?.tooltip?.(event) ?? [
        formatX(event.time),
        ...(current?.series ?? [])
          .filter((item) => !item.hidden)
          .flatMap((item) => {
            const point = event.seriesData.get(item.series);
            if (!point) return [];
            if ('value' in point && typeof point.value === 'number')
              return [`${item.name}: ${number(point.value)}`];
            if ('close' in point)
              return [
                `${item.name}: O ${number(point.open)} · H ${number(point.high)} · L ${number(point.low)} · C ${number(point.close)}`,
              ];
            return [];
          }),
      ];
      setTooltip({
        x: Math.min(event.point.x + 14, Math.max(8, target.clientWidth - 245)),
        y: Math.max(6, event.point.y - 75),
        lines,
      });
    };
    const clicked = (event: MouseEventParams<number>) => {
      if (event.time === undefined) return;
      const primary = result.current?.series[0]?.series;
      latest.current.onPointClick?.(
        event.time,
        event.point && primary
          ? (primary.coordinateToPrice(event.point.y) ?? undefined)
          : undefined,
      );
    };
    const doubleClicked = () => latest.current.onDoubleClick?.();
    instance.subscribeCrosshairMove(moving);
    instance.subscribeClick(clicked);
    instance.subscribeDblClick(doubleClicked);
    instance.timeScale().subscribeVisibleTimeRangeChange(notifyRange);
    const observer = new ResizeObserver(() => {
      if (target.clientWidth > 0 && target.clientHeight > 0)
        instance.resize(target.clientWidth, latest.current.height ?? 330);
    });
    observer.observe(target);
    return () => {
      observer.disconnect();
      updating.current = true;
      result.current?.cleanup?.();
      result.current = null;
      instance.unsubscribeCrosshairMove(moving);
      instance.unsubscribeClick(clicked);
      instance.unsubscribeDblClick(doubleClicked);
      instance.timeScale().unsubscribeVisibleTimeRangeChange(notifyRange);
      instance.remove();
      chart.current = null;
      delete target.__lightweightChart;
      delete target.__lightweightSeries;
      delete target.__lightweightOverlays;
      updating.current = false;
    };
  }, [axis]);

  const viewKey = JSON.stringify([revision, rangeKey ?? range]);
  useEffect(() => {
    const instance = chart.current,
      target = host.current;
    if (!instance || !target) return;
    let frame = 0;
    const previousRange = instance.timeScale().getVisibleLogicalRange();
    const previousDomain = domainOf(result.current?.series ?? []);
    const keepViewport = applied.current === viewKey;
    const manualPrices = (['left', 'right'] as const).map((id) => {
      const scale = instance.priceScale(id);
      return { id, range: !scale.options().autoScale ? scale.getVisibleRange() : null };
    });
    updating.current = true;
    try {
      const foreground = theme === 'dark' ? '#a6b5ca' : '#52647a';
      const background = theme === 'dark' ? '#131e2c' : '#ffffff';
      const grid = theme === 'dark' ? '#253346' : '#e2e8ee';
      instance.applyOptions({
        height,
        layout: {
          background: { type: ColorType.Solid, color: background },
          textColor: foreground,
          fontSize: 11,
          fontFamily: 'Segoe UI, sans-serif',
          attributionLogo: true,
        },
        grid: { vertLines: { color: grid }, horzLines: { color: grid } },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { visible: true, borderColor: grid, minimumWidth: 58 },
        leftPriceScale: { visible: false, borderColor: grid, minimumWidth: 58 },
        timeScale: {
          borderColor: grid,
          timeVisible: axis === 'time',
          secondsVisible: false,
          rightOffset: 0,
          minBarSpacing: 0.2,
          fixLeftEdge: false,
          fixRightEdge: false,
        },
        handleScroll: {
          mouseWheel: true,
          pressedMouseMove: true,
          horzTouchDrag: true,
          vertTouchDrag: false,
        },
        handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
        localization: axis === 'time' ? { timeFormatter: (value: number) => formatX(value) } : {},
      });
      result.current?.cleanup?.();
      for (const item of result.current?.series ?? []) instance.removeSeries(item.series);
      result.current = null;
      const next = build(instance);
      result.current = next;
      target.__lightweightSeries = next.series;
      target.__lightweightOverlays = next.overlays ?? [];
      setSeriesNames(next.series);
      if (!keepViewport) {
        if (range && range[0] < range[1])
          instance.timeScale().setVisibleRange({ from: range[0], to: range[1] });
        else instance.timeScale().fitContent();
      } else if (previousRange) {
        instance
          .timeScale()
          .setVisibleLogicalRange(
            retainChartRange(previousRange, previousDomain, domainOf(next.series)),
          );
        for (const entry of manualPrices)
          if (entry.range) instance.priceScale(entry.id).setVisibleRange(entry.range);
      }
      applied.current = viewKey;
      setError('');
      frame = requestAnimationFrame(() => {
        if (!target.isConnected || chart.current !== instance) return;
        updating.current = false;
        target.dataset.chartReady = 'true';
        notifyRange();
        setReady(true);
      });
    } catch (cause) {
      updating.current = false;
      result.current?.cleanup?.();
      result.current = null;
      // A builder can throw after adding a series; recover without leaving stale layers.
      for (const pane of instance.panes())
        for (const series of pane.getSeries()) instance.removeSeries(series);
      target.__lightweightSeries = [];
      target.__lightweightOverlays = [];
      applied.current = '';
      setSeriesNames([]);
      setReady(false);
      setError(cause instanceof Error ? cause.message : 'Chart could not render');
      target.dataset.chartReady = 'false';
    }
    return () => cancelAnimationFrame(frame);
  }, [axis, build, theme, height, viewKey, xFormat]);

  const interact = () => {
    interactionUntil.current = performance.now() + 1500;
  };
  const zoom = (factor: number) => {
    const scale = chart.current?.timeScale(),
      visible = scale?.getVisibleLogicalRange();
    if (!scale || !visible) return;
    interact();
    const middle = (visible.from + visible.to) / 2,
      half = ((visible.to - visible.from) * factor) / 2;
    scale.setVisibleLogicalRange({ from: middle - half, to: middle + half });
  };
  const reset = () => {
    if (!chart.current) return;
    interact();
    chart.current.priceScale('left').setAutoScale(true);
    chart.current.priceScale('right').setAutoScale(true);
    chart.current.timeScale().fitContent();
    notifyRange();
  };
  const exportChart = () => {
    const canvas = chart.current?.takeScreenshot();
    if (!canvas) return;
    const link = document.createElement('a');
    link.href = canvas.toDataURL('image/png');
    link.download = 'PublicGex-chart.png';
    link.click();
  };
  return (
    <div
      className="lwc-chart-shell"
      data-testid="chart"
      data-chart-engine="lightweight-charts"
      role="img"
      aria-label={label}
    >
      <div className="lwc-chart-toolbar">
        <span className="lwc-axis-title">{yTitle}</span>
        <div className="lwc-chart-actions">
          <button type="button" aria-label="Zoom chart in" onClick={() => zoom(0.7)}>
            +
          </button>
          <button type="button" aria-label="Zoom chart out" onClick={() => zoom(1.4)}>
            −
          </button>
          <button type="button" onClick={reset}>
            Reset chart
          </button>
          <button type="button" onClick={exportChart}>
            Export chart PNG
          </button>
        </div>
      </div>
      {legend && (
        <div className="chart-legend">
          {seriesNames
            .filter((item) => !item.hidden)
            .map((item, index) => (
              <span key={`${item.name}:${index}`}>
                <i style={{ background: item.color }} />
                {item.name}
              </span>
            ))}
        </div>
      )}
      <div
        className="lwc-chart-frame"
        onPointerDownCapture={interact}
        onWheelCapture={interact}
        onPointerMoveCapture={(event) => {
          if (event.buttons) interact();
        }}
      >
        <div ref={host} className="lwc-chart-host" style={{ height, width: '100%' }} />
        {!ready && !error && <span className="chart-loading">Preparing chart…</span>}
        {tooltip && (
          <div className="chart-tooltip" style={{ left: tooltip.x, top: tooltip.y }}>
            {tooltip.lines.map((line, index) => (
              <div key={index}>{line}</div>
            ))}
          </div>
        )}
      </div>
      {xTitle && <div className="lwc-x-title">{xTitle}</div>}
      {error && (
        <div className="notice error" role="alert">
          Chart could not render: {error}
        </div>
      )}
    </div>
  );
}
