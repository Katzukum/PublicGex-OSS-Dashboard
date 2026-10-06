import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  SeriesAttachedParameter,
} from 'lightweight-charts';
import { COLORS } from './chartConstants';
import { number } from './models';
import type { Theme } from './types';

export interface ChartLevel {
  value: number;
  label: string;
  color: string;
}
export interface ProfilePoint {
  strike: number;
  call: number;
  put: number;
  net?: number;
}
export interface CompassPoint {
  x: number;
  y: number;
}

/** A uniform numeric scaffold makes non-time charts spatially linear in Lightweight Charts. */
export function numericGrid(from: number, to: number, count = 101): number[] {
  if (!Number.isFinite(from) || !Number.isFinite(to)) return [];
  if (from === to) {
    from -= 1;
    to += 1;
  }
  if (from > to) [from, to] = [to, from];
  const size = Math.max(2, Math.floor(count));
  return Array.from({ length: size }, (_, index) => from + ((to - from) * index) / (size - 1));
}

/** Fill absent strikes with whitespace so the native histogram uses true numeric spacing.
 * Very fine irregular lattices use a bounded primitive scaffold instead of allocating millions of bars.
 */
export function strikeLattice(strikes: readonly number[], limit = 25001): number[] | null {
  const values = [...new Set(strikes.filter(Number.isFinite))].sort((a, b) => a - b);
  if (!values.length) return [];
  if (values.length === 1) return [values[0]! - 0.5, values[0]!, values[0]! + 0.5];
  const precision = 1e6;
  const integers = values.map((value) => Math.round(value * precision));
  if (
    integers.some(
      (value, index) => !Number.isSafeInteger(value) || value / precision !== values[index],
    )
  )
    return null;
  const gcd = (a: number, b: number): number => {
    while (b) [a, b] = [b, a % b];
    return a;
  };
  let step = integers[1]! - integers[0]!;
  for (let index = 2; index < integers.length; index++)
    step = gcd(step, integers[index]! - integers[index - 1]!);
  const count = (integers.at(-1)! - integers[0]!) / step + 1;
  if (!Number.isSafeInteger(count) || count > limit) return null;
  return Array.from({ length: count }, (_, index) => (integers[0]! + index * step) / precision);
}

/** Original focusedStrikeWindow: center on all relevant levels and leave space around them. */
export function focusedProfileRange(
  strikes: readonly number[],
  levels: readonly Pick<ChartLevel, 'value'>[],
  spot: number,
): [number, number] | undefined {
  const values = strikes.filter(Number.isFinite);
  if (!values.length || !Number.isFinite(spot) || spot <= 0) return undefined;
  const dataMin = Math.min(...values),
    dataMax = Math.max(...values);
  if (dataMin === dataMax) return [dataMin - 0.5, dataMax + 0.5];
  const relevant = [spot, ...levels.map((level) => level.value).filter(Number.isFinite)];
  const relevantMin = Math.min(...relevant),
    relevantMax = Math.max(...relevant);
  const minHalfWindow = spot * 0.015;
  const halfWindow = Math.max((relevantMax - relevantMin) * 1.4, minHalfWindow);
  const center = (Math.min(relevantMin, spot) + Math.max(relevantMax, spot)) / 2;
  let from = Math.max(dataMin, center - halfWindow),
    to = Math.min(dataMax, center + halfWindow);
  if (to - from < (dataMax - dataMin) * 0.15) {
    const expandedHalf = Math.max((dataMax - dataMin) * 0.075, minHalfWindow);
    from = Math.max(dataMin, center - expandedHalf);
    to = Math.min(dataMax, center + expandedHalf);
  }
  return from < to ? [from, to] : [dataMin, dataMax];
}

/** Resample the stored piecewise-linear model; missing endpoint values remain gaps, never zero. */
export function sampleNumericCurve<T>(
  points: readonly T[],
  domain: readonly number[],
  getX: (point: T) => number,
  getY: (point: T) => number | null | undefined,
): ({ time: number; value: number } | { time: number })[] {
  const sorted = points
    .filter((point) => Number.isFinite(getX(point)))
    .slice()
    .sort((a, b) => getX(a) - getX(b));
  let index = 0;
  return domain.map((time) => {
    if (!sorted.length || time < getX(sorted[0]!) || time > getX(sorted.at(-1)!)) return { time };
    while (index < sorted.length - 1 && getX(sorted[index + 1]!) <= time) index++;
    const left = sorted[index]!,
      leftY = getY(left);
    if (getX(left) === time)
      return typeof leftY === 'number' && Number.isFinite(leftY)
        ? { time, value: leftY }
        : { time };
    const right = sorted[index + 1];
    if (!right) return { time };
    const rightY = getY(right),
      span = getX(right) - getX(left);
    if (
      typeof leftY !== 'number' ||
      !Number.isFinite(leftY) ||
      typeof rightY !== 'number' ||
      !Number.isFinite(rightY) ||
      span <= 0
    )
      return { time };
    return { time, value: leftY + ((rightY - leftY) * (time - getX(left))) / span };
  });
}

/** Interpolate levels between observed x values instead of snapping them to a neighboring strike. */
export function numericCoordinate(
  chart: IChartApiBase<number>,
  value: number,
  domain: readonly number[],
): number | null {
  if (!domain.length || !Number.isFinite(value)) return null;
  const scale = chart.timeScale();
  const exact = scale.timeToCoordinate(value);
  if (exact !== null) return exact;
  let lo = 0,
    hi = domain.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (domain[mid]! < value) lo = mid + 1;
    else hi = mid;
  }
  const index = Math.max(1, Math.min(domain.length - 1, lo));
  const left = domain[index - 1],
    right = domain[index];
  if (left === undefined || right === undefined || left === right) return null;
  const x0 = scale.timeToCoordinate(left),
    x1 = scale.timeToCoordinate(right);
  return x0 === null || x1 === null ? null : x0 + ((value - left) / (right - left)) * (x1 - x0);
}

export function profileScale(rows: readonly ProfilePoint[], range?: readonly [number, number]) {
  const visible = rows.filter(
    (row) => !range || (row.strike >= range[0] && row.strike <= range[1]),
  );
  return {
    sideMax:
      Math.max(0.001, ...visible.flatMap((row) => [Math.abs(row.call), Math.abs(row.put)])) * 1.15,
    netMax: Math.max(0.001, ...visible.map((row) => Math.abs(row.net ?? 0))) * 1.15,
  };
}

export function nearestProfileRow(
  rows: readonly ProfilePoint[],
  strike: number,
): ProfilePoint | undefined {
  if (!Number.isFinite(strike)) return undefined;
  return rows.reduce<ProfilePoint | undefined>(
    (nearest, row) =>
      !nearest || Math.abs(row.strike - strike) < Math.abs(nearest.strike - strike) ? row : nearest,
    undefined,
  );
}

abstract class CanvasPrimitive implements ISeriesPrimitive<number> {
  protected attachment: SeriesAttachedParameter<number> | undefined;
  private readonly renderer: IPrimitivePaneRenderer = {
    draw: (target) =>
      target.useMediaCoordinateSpace(({ context, mediaSize }) => {
        context.save();
        context.beginPath();
        context.rect(0, 0, mediaSize.width, mediaSize.height);
        context.clip();
        this.draw(context, mediaSize.width, mediaSize.height);
        context.restore();
      }),
  };
  private readonly views: IPrimitivePaneView[] = [
    { zOrder: () => 'normal', renderer: () => this.renderer },
  ];
  attached(parameters: SeriesAttachedParameter<number>) {
    this.attachment = parameters;
    parameters.requestUpdate();
  }
  detached() {
    this.attachment = undefined;
  }
  paneViews() {
    return this.views;
  }
  protected abstract draw(context: CanvasRenderingContext2D, width: number, height: number): void;
}

/** Exact scalar positioning for unusually fine strike lattices, still on the native LWC canvas. */
export class VerticalProfilePrimitive extends CanvasPrimitive {
  constructor(
    readonly rows: readonly ProfilePoint[],
    private readonly kind: 'call' | 'put' | 'net',
    private readonly domain: number[],
    private readonly overrideColor?: string,
  ) {
    super();
  }
  protected draw(ctx: CanvasRenderingContext2D, width: number, _height: number) {
    if (!this.attachment) return;
    const { chart, series } = this.attachment;
    const positions = this.rows.map((row) => ({
      row,
      x: numericCoordinate(chart, row.strike, this.domain),
    }));
    const distances = positions
      .slice(1)
      .flatMap((point, index) =>
        point.x !== null && positions[index]!.x !== null ? [point.x - positions[index]!.x!] : [],
      )
      .filter((value) => value > 0);
    const barWidth = Math.max(
      1,
      Math.min(18, (distances.length ? Math.min(...distances) : 15) * 0.8),
    );
    const zero = series.priceToCoordinate(0);
    if (zero === null) return;
    const color =
      this.overrideColor ??
      (this.kind === 'call' ? COLORS.positive : this.kind === 'put' ? COLORS.negative : '#b692ff');
    ctx.fillStyle = color;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    for (const { row, x } of positions) {
      if (x === null) continue;
      const value = row[this.kind];
      if (value === undefined) continue;
      const y = series.priceToCoordinate(value);
      if (y === null) continue;
      if (this.kind === 'net') {
        if (started) ctx.lineTo(x, y);
        else {
          ctx.moveTo(x, y);
          started = true;
        }
      } else if (x >= -barWidth && x <= width + barWidth && value !== 0)
        ctx.fillRect(
          x - barWidth / 2,
          Math.min(y, zero),
          barWidth,
          Math.max(1, Math.abs(y - zero)),
        );
    }
    if (this.kind === 'net') ctx.stroke();
  }
}

function caption(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  color: string,
  theme: Theme,
  align: CanvasTextAlign = 'left',
) {
  ctx.font = '10px Inter, Segoe UI, sans-serif';
  ctx.textBaseline = 'middle';
  ctx.textAlign = align;
  const width = ctx.measureText(text).width;
  const left = align === 'right' ? x - width : align === 'center' ? x - width / 2 : x;
  ctx.fillStyle = theme === 'dark' ? '#131c29e8' : '#ffffffe8';
  ctx.fillRect(left - 4, y - 8, width + 8, 16);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

export class VerticalLevelsPrimitive extends CanvasPrimitive {
  constructor(
    readonly levels: ChartLevel[],
    private readonly domain: number[],
    private readonly theme: Theme,
  ) {
    super();
  }
  protected draw(ctx: CanvasRenderingContext2D, width: number, height: number) {
    if (!this.attachment) return;
    this.levels.forEach((level, index) => {
      const x = numericCoordinate(this.attachment!.chart, level.value, this.domain);
      if (x === null || x < 0 || x > width) return;
      ctx.strokeStyle = level.color;
      ctx.lineWidth = 1;
      ctx.setLineDash(level.label === 'Spot' ? [] : [3, 4]);
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
      ctx.stroke();
      ctx.setLineDash([]);
      const text = `${level.label} ${number(level.value)}`;
      ctx.font = '10px Inter, Segoe UI, sans-serif';
      const labelWidth = ctx.measureText(text).width;
      const labelX = Math.max(5, Math.min(x + 5, width - labelWidth - 5));
      caption(ctx, text, labelX, 13 + (index % 3) * 18, level.color, this.theme);
    });
  }
}

export interface HorizontalProfileOptions {
  domain: number[];
  sideMax: number;
  netMax?: number;
  levels?: ChartLevel[];
  theme: Theme;
}

/** Horizontal exposures and a separately scaled net curve drawn on the chart's native canvas. */
export class HorizontalProfilePrimitive extends CanvasPrimitive {
  constructor(
    readonly rows: ProfilePoint[],
    readonly options: HorizontalProfileOptions,
  ) {
    super();
  }
  protected draw(ctx: CanvasRenderingContext2D, width: number, height: number) {
    const attached = this.attachment;
    if (!attached) return;
    const { chart, series } = attached;
    const x = (value: number) => numericCoordinate(chart, value, this.options.domain);
    const zero = x(0);
    if (zero === null) return;
    const positions = this.rows.map((row) => ({ row, y: series.priceToCoordinate(row.strike) }));
    const distances = positions
      .slice(1)
      .flatMap((item, index) =>
        item.y !== null && positions[index]!.y !== null
          ? [Math.abs(item.y - positions[index]!.y!)]
          : [],
      )
      .filter((value) => value > 0);
    const barHeight = Math.max(
      1,
      Math.min(28, (distances.length ? Math.min(...distances) : 15) * 0.8),
    );
    ctx.strokeStyle = this.options.theme === 'dark' ? '#657990' : '#b3bfcb';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(zero, 0);
    ctx.lineTo(zero, height);
    ctx.stroke();
    for (const { row, y } of positions) {
      if (y === null || y < -barHeight || y > height + barHeight) continue;
      for (const [value, color] of [
        [row.call, COLORS.positive],
        [row.put, COLORS.negative],
      ] as const) {
        const end = x(value);
        if (end === null || value === 0) continue;
        ctx.fillStyle = color;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(
          Math.min(zero, end),
          y - barHeight / 2,
          Math.max(1, Math.abs(end - zero)),
          barHeight,
        );
      }
    }
    ctx.globalAlpha = 1;
    if (this.options.netMax && this.rows.some((row) => row.net !== undefined)) {
      ctx.beginPath();
      let started = false;
      for (const { row, y } of positions) {
        if (y === null || row.net === undefined) continue;
        const end = x((row.net / this.options.netMax) * this.options.sideMax);
        if (end === null) continue;
        if (!started) {
          ctx.moveTo(end, y);
          started = true;
        } else ctx.lineTo(end, y);
      }
      ctx.strokeStyle = '#b692ff';
      ctx.lineWidth = 2.4;
      ctx.stroke();
      for (const fraction of [-1, -0.5, 0, 0.5, 1]) {
        const end = x(fraction * this.options.sideMax);
        if (end === null || end < 0 || end > width) continue;
        caption(
          ctx,
          number(fraction * this.options.netMax, 2),
          Math.max(27, Math.min(width - 27, end)),
          12,
          '#b692ff',
          this.options.theme,
          'center',
        );
      }
      caption(
        ctx,
        'Net GEX · $M (top scale)',
        width / 2,
        29,
        '#b692ff',
        this.options.theme,
        'center',
      );
    }
    for (const level of this.options.levels ?? []) {
      const y = series.priceToCoordinate(level.value);
      if (y === null || y < 0 || y > height) continue;
      ctx.strokeStyle = level.color;
      ctx.lineWidth = 1;
      ctx.setLineDash(level.label === 'Spot' ? [] : [3, 4]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
      ctx.setLineDash([]);
      // Labels live in the adjacent HTML legend, outside the exposure bars.
    }
  }
}

export class CompassPrimitive extends CanvasPrimitive {
  constructor(
    readonly points: CompassPoint[],
    private readonly domain: number[],
    private readonly theme: Theme,
  ) {
    super();
  }
  protected draw(ctx: CanvasRenderingContext2D, width: number, height: number) {
    if (!this.attachment) return;
    const { chart, series } = this.attachment;
    const x = (value: number) => numericCoordinate(chart, value, this.domain);
    const y = (value: number) => series.priceToCoordinate(value);
    const zeroX = x(0),
      zeroY = y(0);
    if (zeroX === null || zeroY === null) return;
    ctx.fillStyle = '#39c7a010';
    ctx.fillRect(zeroX, 0, width - zeroX, zeroY);
    ctx.fillStyle = '#f0798710';
    ctx.fillRect(0, zeroY, zeroX, height - zeroY);
    ctx.strokeStyle = this.theme === 'dark' ? '#7287a0' : '#95a4b5';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.moveTo(zeroX, 0);
    ctx.lineTo(zeroX, height);
    ctx.moveTo(0, zeroY);
    ctx.lineTo(width, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);
    const textColor = this.theme === 'dark' ? '#dbe6f7' : '#223248';
    const labels = [
      { text: 'MELT UP', x: 8, y: 16, color: COLORS.amber, align: 'left' },
      { text: 'GRIND UP', x: width - 8, y: 16, color: COLORS.positive, align: 'right' },
      { text: 'CRASH / FLUSH', x: 8, y: height - 15, color: COLORS.negative, align: 'left' },
      { text: 'SUPPORT / CHOP', x: width - 8, y: height - 15, color: COLORS.amber, align: 'right' },
    ] as const;
    labels.forEach((label) =>
      caption(ctx, label.text, label.x, label.y, label.color, this.theme, label.align),
    );
    this.points.forEach((point, index) => {
      const px = x(point.x),
        py = y(point.y);
      if (px === null || py === null) return;
      const current = index === this.points.length - 1;
      ctx.globalAlpha = current ? 1 : 0.2 + index * 0.1;
      ctx.fillStyle = COLORS.accent;
      ctx.beginPath();
      ctx.arc(px, py, current ? 10 : 4 + index * 0.6, 0, Math.PI * 2);
      ctx.fill();
      if (current) {
        ctx.strokeStyle = textColor;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    });
    ctx.globalAlpha = 1;
  }
}
