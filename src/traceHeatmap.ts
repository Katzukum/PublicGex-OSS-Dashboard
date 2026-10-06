import { customSeriesDefaultOptions } from 'lightweight-charts';
import type {
  CustomData,
  CustomSeriesOptions,
  CustomSeriesWhitespaceData,
  IChartApiBase,
  ICustomSeriesPaneRenderer,
  ICustomSeriesPaneView,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  PaneRendererCustomData,
  PriceToCoordinateConverter,
  SeriesAttachedParameter,
} from 'lightweight-charts';
import type { CanvasRenderingTarget2D } from 'fancy-canvas';
import { chartTime, COLORS } from './chartConstants';
import { finite } from './models';
import type { HeatmapRow, Theme, TraceData, TraceMetric } from './types';

export interface HeatmapCell {
  strike: number;
  low: number;
  high: number;
  value: number | null;
}
export interface HeatmapColumn extends CustomData<number> {
  cells: HeatmapCell[];
  low: number;
  high: number;
}

/** One native time point per observation, containing its full vertical strike surface. */
export function traceHeatmapData(rows: HeatmapRow[], metric: TraceMetric) {
  const valid = rows.filter((row) => finite(row.strike) && finite(chartTime(row.timestamp)));
  const strikes = [...new Set(valid.map((row) => row.strike))].sort((a, b) => a - b);
  const bounds = new Map(
    strikes.map((strike, index) => {
      const previous = strikes[index - 1],
        next = strikes[index + 1];
      const lowerGap =
        previous === undefined ? (next === undefined ? 1 : next - strike) : strike - previous;
      const upperGap = next === undefined ? lowerGap : next - strike;
      return [strike, { low: strike - lowerGap / 2, high: strike + upperGap / 2 }];
    }),
  );
  const grouped = new Map<number, Map<number, HeatmapCell>>();
  let maxAbs = 0.001;
  for (const row of valid) {
    const time = chartTime(row.timestamp),
      raw = row[metric];
    const value = finite(raw) ? raw / 1e6 : null;
    if (value !== null) maxAbs = Math.max(maxAbs, Math.abs(value));
    const cells = grouped.get(time) ?? new Map<number, HeatmapCell>();
    cells.set(row.strike, { strike: row.strike, ...bounds.get(row.strike)!, value });
    grouped.set(time, cells);
  }
  const columns: HeatmapColumn[] = [...grouped]
    .sort(([a], [b]) => a - b)
    .map(([time, items]) => {
      const cells = [...items.values()].sort((a, b) => a.strike - b.strike);
      return { time, cells, low: cells[0]!.low, high: cells.at(-1)!.high };
    });
  return { columns, maxAbs };
}

/** A symmetric color scale keeps positive-only and negative-only metrics correctly signed. */
export function heatmapColor(value: number, maxAbs: number, theme: Theme) {
  const neutral = theme === 'dark' ? [23, 34, 49] : [241, 244, 248];
  const end = value < 0 ? [187, 80, 108] : [57, 199, 160];
  const ratio = Math.min(1, Math.abs(value) / Math.max(maxAbs, 0.001));
  return `rgb(${neutral.map((component, index) => Math.round(component + (end[index]! - component) * ratio)).join(', ')})`;
}

export class TraceHeatmapSeries
  implements ICustomSeriesPaneView<number, HeatmapColumn>, ICustomSeriesPaneRenderer
{
  private data?: PaneRendererCustomData<number, HeatmapColumn>;
  constructor(
    private maxAbs: number,
    private theme: Theme,
  ) {}
  renderer() {
    return this;
  }
  update(data: PaneRendererCustomData<number, HeatmapColumn>) {
    this.data = data;
  }
  defaultOptions(): CustomSeriesOptions {
    return { ...customSeriesDefaultOptions, color: COLORS.accent };
  }
  isWhitespace(
    data: HeatmapColumn | CustomSeriesWhitespaceData<number>,
  ): data is CustomSeriesWhitespaceData<number> {
    return !('cells' in data) || !Array.isArray(data.cells) || !data.cells.length;
  }
  priceValueBuilder(row: HeatmapColumn) {
    return [row.low, row.high, (row.low + row.high) / 2];
  }
  draw(target: CanvasRenderingTarget2D, priceToCoordinate: PriceToCoordinateConverter) {
    const data = this.data;
    if (!data?.visibleRange) return;
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      context.save();
      context.beginPath();
      context.rect(0, 0, mediaSize.width, mediaSize.height);
      context.clip();
      const halfWidth = (data.barSpacing * data.conflationFactor) / 2;
      for (
        let index = Math.max(0, Math.floor(data.visibleRange!.from));
        index < Math.min(data.bars.length, Math.ceil(data.visibleRange!.to));
        index++
      ) {
        const bar = data.bars[index]!;
        for (const cell of bar.originalData.cells) {
          if (cell.value === null) continue;
          const y1 = priceToCoordinate(cell.high),
            y2 = priceToCoordinate(cell.low);
          if (y1 === null || y2 === null) continue;
          context.fillStyle = heatmapColor(cell.value, this.maxAbs, this.theme);
          context.fillRect(
            bar.x - halfWidth,
            Math.min(y1, y2),
            Math.max(1, halfWidth * 2 + 0.5),
            Math.max(1, Math.abs(y2 - y1)),
          );
        }
      }
      context.restore();
    });
  }
  destroy() {
    this.data = undefined;
  }
}

export interface TraceOverlay {
  time: number;
  label: string;
  overlay_type: string;
  color: string;
}
export function traceOverlayData(overlays: TraceData['overlays']): TraceOverlay[] {
  return (overlays ?? [])
    .slice(-20)
    .map((event) => ({
      time: chartTime(event.timestamp),
      label: (event.label?.trim() || event.overlay_type?.trim() || 'Decision event').replaceAll(
        '_',
        ' ',
      ),
      overlay_type: event.overlay_type || 'decision',
      color: event.overlay_type === 'alert' ? COLORS.negative : COLORS.accent,
    }))
    .filter((event) => finite(event.time));
}

/** Events may occur between observation minutes; interpolate instead of losing their annotations. */
function timeCoordinate(chart: IChartApiBase<number>, time: number, times: number[]) {
  const scale = chart.timeScale();
  const exact = scale.timeToCoordinate(time);
  if (exact !== null) return exact;
  if (times.length < 2) return null;
  let right = times.findIndex((value) => value > time);
  if (right < 0) right = times.length - 1;
  if (right === 0) right = 1;
  const left = right - 1,
    x1 = scale.timeToCoordinate(times[left]!),
    x2 = scale.timeToCoordinate(times[right]!);
  if (x1 === null || x2 === null) return null;
  return x1 + ((time - times[left]!) / (times[right]! - times[left]!)) * (x2 - x1);
}

export class TraceOverlayPrimitive implements ISeriesPrimitive<number>, IPrimitivePaneRenderer {
  private chart?: IChartApiBase<number>;
  private views: IPrimitivePaneView[] = [{ zOrder: () => 'top', renderer: () => this }];
  constructor(
    readonly annotations: TraceOverlay[],
    private times: number[],
    private theme: Theme,
  ) {}
  attached({ chart }: SeriesAttachedParameter<number>) {
    this.chart = chart;
  }
  detached() {
    this.chart = undefined;
  }
  paneViews() {
    return this.views;
  }
  draw(target: CanvasRenderingTarget2D) {
    const chart = this.chart;
    if (!chart) return;
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      context.save();
      context.beginPath();
      context.rect(0, 0, mediaSize.width, mediaSize.height);
      context.clip();
      for (const event of this.annotations) {
        const x = timeCoordinate(chart, event.time, this.times);
        if (x === null || x < 0 || x > mediaSize.width) continue;
        context.strokeStyle = event.color;
        context.lineWidth = 1;
        context.setLineDash([2, 4]);
        context.beginPath();
        context.moveTo(x, 0);
        context.lineTo(x, mediaSize.height);
        context.stroke();
        context.save();
        context.translate(x + 3, 9);
        context.rotate(Math.PI / 2);
        context.font = '10px sans-serif';
        const label = event.label.slice(0, 80),
          size = context.measureText(label).width;
        context.fillStyle = this.theme === 'dark' ? '#111824e8' : '#ffffffe8';
        context.fillRect(-2, -10, size + 4, 13);
        context.fillStyle = event.color;
        context.fillText(label, 0, 0);
        context.restore();
      }
      context.restore();
    });
  }
}

export function traceReplayRange(
  times: number[],
  cursor: number,
  minutes: number,
): [number, number] | undefined {
  if (!times.length) return;
  const index = cursor < 0 ? times.length - 1 : Math.max(0, Math.min(times.length - 1, cursor));
  const to = times[index]!,
    from = Math.max(times[0]!, to - Math.max(1, minutes) * 60);
  return from === to ? [from - 30, to + 30] : [from, to];
}

export function traceViewportSelection(times: number[], range: { from: number; to: number }) {
  if (!times.length || !finite(range.from) || !finite(range.to)) return;
  let cursor = 0;
  for (let index = 0; index < times.length && times[index]! <= range.to; index++) cursor = index;
  return { cursor, width: Math.max(1, Math.round(Math.abs(range.to - range.from) / 60)) };
}
