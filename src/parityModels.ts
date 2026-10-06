import { aggregateStrikes, finite } from './models';
import type { StrikeRow } from './models';
import type { Dashboard, GammaSweep, TraceData } from './types';

export function zeroGammaLevels(sweep: GammaSweep | undefined, fallback?: number | null) {
  const levels = [
    { label: 'Zero gamma below', value: sweep?.zero_crossings?.below },
    { label: 'Zero gamma above', value: sweep?.zero_crossings?.above },
  ].filter((item): item is { label: string; value: number } => finite(item.value));
  return levels.length
    ? levels
    : finite(fallback) && fallback > 0
      ? [{ label: 'Estimated flip', value: fallback }]
      : [];
}

export function cockpitMetrics(dashboard: Dashboard) {
  const rows = aggregateStrikes(dashboard.profile),
    spot = dashboard.snapshot.spot_price;
  const call = rows.reduce((sum, row) => sum + row.call, 0),
    put = rows.reduce((sum, row) => sum + row.put, 0);
  const local = rows
    .filter((row) => spot > 0 && Math.abs(row.strike - spot) / spot <= 0.02)
    .reduce((sum, row) => sum + row.net, 0);
  const first = dashboard.history[0],
    last = dashboard.history.at(-1);
  return {
    call,
    put,
    local,
    change: first
      ? (last?.total_net_gex ?? dashboard.snapshot.total_net_gex) - first.total_net_gex
      : null,
  };
}

/** The original matrix hides a strike only when BOTH open interest and exposure are tiny. */
export function matrixRows(rows: StrikeRow[], showAll = false) {
  return rows.filter((row) => showAll || row.oi >= 10 || Math.abs(row.net) >= 100000);
}
export function matrixState(row: StrikeRow, maxAbs: number) {
  return Math.abs(row.net) === maxAbs ? 'MAGNET' : row.net > 0 ? 'STABILITY' : 'VOLATILITY';
}

export interface PriceCandle {
  timestamp: string;
  open: number;
  close: number;
  low: number;
  high: number;
}
export function traceCandles(
  ticks: { timestamp: string; spot_price: number }[],
  buckets: string[],
): PriceCandle[] {
  const grouped = new Map<string, number[]>();
  for (const tick of ticks) {
    if (!finite(tick.spot_price)) continue;
    const key = tick.timestamp.replace('T', ' ').slice(0, 16);
    grouped.set(key, [...(grouped.get(key) ?? []), tick.spot_price]);
  }
  let previous: number | undefined;
  const result: PriceCandle[] = [];
  for (const timestamp of buckets) {
    const values = grouped.get(timestamp.replace('T', ' ').slice(0, 16)) ?? [];
    if (!values.length && previous === undefined) continue;
    const open = previous ?? values[0]!,
      close = values.at(-1) ?? open;
    result.push({
      timestamp,
      open,
      close,
      low: Math.min(open, close, ...values),
      high: Math.max(open, close, ...values),
    });
    previous = close;
  }
  return result;
}
export function traceStability(data: TraceData): number | null {
  const gross = (data.latest_profile ?? []).reduce((sum, row) => sum + Math.abs(row.net_gex), 0);
  return gross > 0 && finite(data.total_net_gex)
    ? Math.min(99, Math.max(1, Math.round((Math.abs(data.total_net_gex) / gross) * 100)))
    : null;
}
export function traceStrikeRange(data: TraceData): [number, number] | undefined {
  const strikes = [...new Set(data.heatmap.map((row) => row.strike))].sort((a, b) => a - b);
  if (!strikes.length) return;
  const gaps = strikes
    .slice(1)
    .map((value, index) => value - strikes[index]!)
    .filter((value) => value > 0)
    .sort((a, b) => a - b);
  const step = gaps[Math.floor(gaps.length / 2)] ?? 1;
  const spot = data.spot_price;
  return spot > 0
    ? [
        Math.max(strikes[0]! - step / 2, Math.floor((spot * 0.985) / step) * step),
        Math.min(strikes.at(-1)! + step / 2, Math.ceil((spot * 1.015) / step) * step),
      ]
    : [strikes[0]! - step / 2, strikes.at(-1)! + step / 2];
}

/** Profile zoom is independent from the history heatmap and retains exact strike spacing. */
export function traceProfileStrikeRange(
  strikes: readonly number[],
  spot: number,
  halfWidth: number | null = 0.0075,
): [number, number] | undefined {
  const sorted = [...new Set(strikes.filter(Number.isFinite))].sort((a, b) => a - b);
  if (!sorted.length) return undefined;
  const gaps = sorted
    .slice(1)
    .map((strike, index) => strike - sorted[index]!)
    .sort((a, b) => a - b);
  const step = gaps[Math.floor(gaps.length / 2)] ?? 5;
  const low = sorted[0]! - step / 2,
    high = sorted.at(-1)! + step / 2;
  if (halfWidth === null || !Number.isFinite(spot) || spot <= 0) return [low, high];
  const from = Math.max(low, Math.floor((spot * (1 - halfWidth)) / step) * step);
  const to = Math.min(high, Math.ceil((spot * (1 + halfWidth)) / step) * step);
  return from < to ? [from, to] : [low, high];
}
