import type { TraceMetric } from './types';
export const COLORS = {
  positive: '#39c7a0',
  negative: '#f07987',
  accent: '#89a8ff',
  amber: '#e7bc76',
  spot: '#dbe6f7',
};
export const metricLabels: Record<TraceMetric, string> = {
  net_gex: 'Net gamma',
  call_gex: 'Call gamma',
  put_gex: 'Put gamma',
  modeled_delta_pressure: 'Modeled delta',
  modeled_charm_pressure: 'Modeled charm',
};
/** Preserve the backend's session wall clock regardless of the machine timezone. */
export function chartTime(value: string): number {
  return Date.parse(`${value.replace(' ', 'T').replace(/(?:Z|[+-]\d{2}:?\d{2})$/, '')}Z`) / 1000;
}
