import type { HeatmapRow, NullableNumber, ProfileRow, TraceMetric } from './types';
export function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}
export function number(value: NullableNumber | undefined, decimals = 2): string {
  return finite(value)
    ? value.toLocaleString(undefined, {
        maximumFractionDigits: decimals,
        minimumFractionDigits: decimals,
      })
    : '—';
}
export function money(value: NullableNumber | undefined, decimals = 0): string {
  return finite(value) ? `${value < 0 ? '−' : ''}$${number(Math.abs(value), decimals)}` : '—';
}
export function millions(value: NullableNumber | undefined): string {
  return finite(value) ? `${value < 0 ? '−' : ''}$${number(Math.abs(value) / 1e6, 1)}M` : '—';
}
export function percent(value: NullableNumber | undefined, decimals = 0): string {
  return finite(value) ? `${number(value * 100, decimals)}%` : '—';
}
export function pretty(value: string | undefined | null): string {
  return value
    ? value
        .replaceAll('_', ' ')
        .toLowerCase()
        .replace(/\b\w/g, (s) => s.toUpperCase())
    : 'Unavailable';
}
export function localDateTime(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
export function timeLabel(value: string | undefined | null): string {
  return value ? value.replace('T', ' ').slice(11, 19) : '—';
}
export interface StrikeRow {
  strike: number;
  call: number;
  put: number;
  net: number;
  oi: number;
  callOI: number;
  putOI: number;
}
export function aggregateStrikes(profile: ProfileRow[]): StrikeRow[] {
  const map = new Map<number, StrikeRow>();
  for (const item of profile) {
    if (!finite(item.strike_price) || !finite(item.gex_value)) continue;
    const row = map.get(item.strike_price) ?? {
      strike: item.strike_price,
      call: 0,
      put: 0,
      net: 0,
      oi: 0,
      callOI: 0,
      putOI: 0,
    };
    const oi = finite(item.open_interest) ? item.open_interest : 0;
    if (item.option_type.toUpperCase().includes('CALL')) {
      row.call += item.gex_value;
      row.callOI += oi;
    } else if (item.option_type.toUpperCase().includes('PUT')) {
      row.put += item.gex_value;
      row.putOI += oi;
    } else continue;
    row.net += item.gex_value;
    row.oi += oi;
    map.set(row.strike, row);
  }
  return [...map.values()].sort((a, b) => a.strike - b.strike);
}
export function heatmapGrid(rows: HeatmapRow[], metric: TraceMetric) {
  const times = [...new Set(rows.map((row) => row.timestamp))].sort();
  const strikes = [...new Set(rows.map((row) => row.strike))].sort((a, b) => a - b);
  const ti = new Map(times.map((value, index) => [value, index])),
    si = new Map(strikes.map((value, index) => [value, index]));
  const z: (number | null)[][] = strikes.map(() => times.map(() => null));
  let maxAbs = 0;
  for (const row of rows) {
    const value = row[metric];
    if (!finite(value)) continue;
    const y = si.get(row.strike),
      x = ti.get(row.timestamp);
    if (y === undefined || x === undefined) continue;
    const scaled = value / 1e6;
    z[y]![x] = scaled;
    maxAbs = Math.max(maxAbs, Math.abs(scaled));
  }
  return { times, strikes, z, maxAbs: Math.max(maxAbs, 0.001) };
}
export function traceWindow(
  times: string[],
  cursor: number,
  width = 60,
): [string, string] | undefined {
  if (!times.length) return;
  const end = Math.max(0, Math.min(times.length - 1, cursor));
  const endTime = times[end]!;
  const stamp = new Date(endTime).getTime();
  if (!Number.isFinite(stamp)) return [times[0]!, endTime];
  const cutoff = stamp - Math.max(1, width) * 60000;
  const start = times.findIndex((value) => new Date(value).getTime() >= cutoff);
  return [times[Math.max(0, Math.min(start, end))]!, endTime];
}
export function validateJournalNumbers(
  contracts: number,
  entry: number,
  exit: number | null,
  fees: number,
): string | undefined {
  if (!Number.isInteger(contracts) || contracts <= 0)
    return 'Contracts must be a positive whole number.';
  if (!finite(entry) || entry < 0) return 'Enter a nonnegative entry price.';
  if (exit !== null && (!finite(exit) || exit < 0))
    return 'Exit price must be nonnegative or blank.';
  if (!finite(fees) || fees < 0) return 'Fees must be nonnegative.';
  return;
}

export interface MarketAlert {
  id: string;
  symbol: string;
  title: string;
  message: string;
  severity: 'info' | 'warning' | 'critical';
}

/** Normalize live events and persisted decisions without turning refresh activity into alerts. */
export function marketAlert(value: unknown): MarketAlert | undefined {
  const record = (item: unknown): item is Record<string, unknown> =>
    typeof item === 'object' && item !== null && !Array.isArray(item);
  if (!record(value)) return;
  const source = record(value.payload) ? value.payload : value;
  const kind = value.type;
  if (
    kind !== 'magnet_change' &&
    kind !== 'decision_alert' &&
    !(kind == null && typeof source.alert_type === 'string')
  )
    return;
  const symbol = typeof source.symbol === 'string' ? source.symbol : '';
  const label =
    kind === 'magnet_change'
      ? 'Magnet change'
      : pretty(typeof source.alert_type === 'string' ? source.alert_type : 'Decision alert');
  let message = typeof source.message === 'string' ? source.message : '';
  if (kind === 'magnet_change' && finite(source.old_magnet) && finite(source.new_magnet)) {
    message = `${symbol || 'Instrument'} magnet moved ${number(source.old_magnet, 0)} → ${number(source.new_magnet, 0)}.`;
  }
  if (!message) return;
  const persistedId = typeof source.alert_id === 'string' ? source.alert_id : undefined;
  const timestamp = source.created_at ?? value.received_at ?? value.timestamp ?? '';
  const id = persistedId ?? JSON.stringify([kind, symbol, label, message, timestamp]);
  return {
    id,
    symbol,
    title: symbol ? `${symbol} · ${label}` : label,
    message,
    severity:
      source.severity === 'critical'
        ? 'critical'
        : source.severity === 'warning'
          ? 'warning'
          : 'info',
  };
}

/** Seen IDs outlive dismissals so polling and duplicated bridge delivery never replay a notice. */
export function unseenMarketAlerts(alerts: MarketAlert[], seen: Set<string>): MarketAlert[] {
  const result: MarketAlert[] = [];
  for (const alert of alerts) {
    if (seen.has(alert.id)) continue;
    seen.add(alert.id);
    result.push(alert);
  }
  return result;
}
