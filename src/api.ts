import { invoke } from '@tauri-apps/api/core';
import type { ApiMap } from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly kind: 'transport' | 'contract' | 'backend' = 'backend',
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function contract(condition: unknown, path: string): asserts condition {
  if (!condition)
    throw new ApiError(
      `The analytics service returned an invalid ${path}. Refresh or restart the service.`,
      'contract',
    );
}
function object(value: unknown, path: string): asserts value is Record<string, unknown> {
  contract(isRecord(value), path);
}
function numeric(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}
function rows(
  value: unknown,
  path: string,
  check: (row: Record<string, unknown>) => boolean,
): void {
  contract(Array.isArray(value), path);
  for (const row of value) {
    object(row, path);
    contract(check(row), path);
  }
}
function positioning(value: unknown): void {
  if (value == null) return;
  object(value, 'positioning');
  contract(
    value.model === 'oi_activity_v1' &&
      value.dealer_direction === 'unknown' &&
      typeof value.as_of === 'string' &&
      ['ready', 'partial', 'unavailable'].includes(String(value.status)),
    'positioning model',
  );
  contract(
    Array.isArray(value.warnings) && value.warnings.every((item) => typeof item === 'string'),
    'positioning warnings',
  );
  contract(
    Array.isArray(value.windows_minutes) &&
      value.windows_minutes.length === 2 &&
      value.windows_minutes[0] === 5 &&
      value.windows_minutes[1] === 15,
    'positioning windows',
  );
  object(value.coverage, 'positioning coverage');
  const unsigned = (item: unknown) => numeric(item) && item >= 0;
  const optionalUnsigned = (item: unknown) => item === null || unsigned(item);
  contract(
    unsigned(value.coverage.observed_minutes) &&
      optionalUnsigned(value.coverage.max_gap_seconds) &&
      unsigned(value.coverage.contracts) &&
      Number.isInteger(value.coverage.contracts) &&
      unsigned(value.coverage.valid_activity_contracts) &&
      Number.isInteger(value.coverage.valid_activity_contracts) &&
      Number(value.coverage.valid_activity_contracts) <= Number(value.coverage.contracts),
    'positioning coverage',
  );
  rows(
    value.strikes,
    'positioning strikes',
    (row) =>
      numeric(row.strike) &&
      numeric(row.net_oi_proxy) &&
      ['call_oi_gex', 'put_oi_gex', 'gross_oi_gex'].every((key) => unsigned(row[key])) &&
      [
        'call_activity_5m',
        'put_activity_5m',
        'activity_5m',
        'call_activity_15m',
        'put_activity_15m',
        'activity_15m',
      ].every((key) => optionalUnsigned(row[key])) &&
      (row.oi_persistence === null ||
        (unsigned(row.oi_persistence) && Number(row.oi_persistence) <= 1)),
  );
  contract(
    Array.isArray(value.top_levels) && value.top_levels.every(numeric),
    'positioning levels',
  );
}
function dashboard(value: unknown): void {
  object(value, 'dashboard');
  object(value.snapshot, 'snapshot');
  contract(
    typeof value.snapshot.symbol === 'string' &&
      numeric(value.snapshot.spot_price) &&
      numeric(value.snapshot.total_net_gex),
    'snapshot values',
  );
  rows(
    value.profile,
    'option profile',
    (r) => numeric(r.strike_price) && numeric(r.gex_value) && typeof r.option_type === 'string',
  );
  contract(Array.isArray(value.history), 'history');
  positioning(value.positioning);
}

/** Runtime checks guard data entering the typed application, including Python/JSON boundaries. */
export function validateResult<K extends keyof ApiMap>(
  method: K,
  value: unknown,
): ApiMap[K]['result'] {
  if (isRecord(value) && typeof value.error === 'string' && value.error)
    throw new ApiError(value.error);
  if (method === 'get_symbols' || method === 'get_trace_dates') {
    contract(Array.isArray(value) && value.every((v) => typeof v === 'string'), method);
  } else if (method === 'delete_journal_entry') contract(typeof value === 'boolean', method);
  else if (method === 'get_events') {
    rows(
      value,
      method,
      (r) =>
        typeof r.type === 'string' ||
        typeof r.message === 'string' ||
        typeof r.alert_type === 'string',
    );
  } else if (method === 'get_journal_entries') {
    rows(
      value,
      method,
      (r) => numeric(r.id) && typeof r.symbol === 'string' && numeric(r.contracts),
    );
  } else if (method === 'get_one_off_profiles') {
    rows(value, method, (r) => numeric(r.snapshot_id) && typeof r.symbol === 'string');
  } else {
    object(value, method);
    switch (method) {
      case 'get_settings':
        contract(
          Array.isArray(value.symbols) &&
            value.symbols.every((s) => typeof s === 'string') &&
            numeric(value.refresh_interval),
          'settings',
        );
        break;
      case 'get_backend_status':
        contract(typeof value.ok === 'boolean', 'backend status');
        break;
      case 'get_runtime_info':
        contract(
          typeof value.mode === 'string' && typeof value.data_dir === 'string',
          'runtime information',
        );
        break;
      case 'get_decision_workspace':
        contract(typeof value.symbol === 'string' && numeric(value.snapshot_id), 'workspace');
        dashboard(value.dashboard);
        if (value.decision) {
          object(value.decision, 'decision');
          for (const key of ['target', 'invalidation', 'flip'])
            contract(
              value.decision[key] == null || numeric(value.decision[key]),
              `decision ${key}`,
            );
        }
        break;
      case 'get_one_off_profile':
        dashboard(value);
        break;
      case 'get_market_overview':
        rows(value.components, 'market components', (r) => typeof r.symbol === 'string');
        break;
      case 'get_trace_data':
        contract(
          typeof value.symbol === 'string' && typeof value.session_date === 'string',
          'TRACE session',
        );
        rows(
          value.heatmap,
          'TRACE heatmap',
          (r) => typeof r.timestamp === 'string' && numeric(r.strike) && numeric(r.net_gex),
        );
        positioning(value.positioning);
        break;
      case 'get_edge_lab':
        object(value.stats, 'evidence statistics');
        contract(
          typeof value.status === 'string' && Array.isArray(value.expectancy_distribution),
          'historical edge',
        );
        rows(value.opportunities, 'opportunities', (r) => typeof r.session_date === 'string');
        break;
      case 'get_weekly_journal_review':
        rows(
          value.groups,
          'weekly review',
          (r) => typeof r.dimension === 'string' && numeric(r.pnl) && numeric(r.trades),
        );
        break;
      case 'create_journal_entry':
      case 'update_journal_entry':
        contract(numeric(value.id) && typeof value.symbol === 'string', 'journal entry');
        break;
      default:
        contract(typeof value.ok === 'boolean', 'operation result');
        if (value.ok === false)
          throw new ApiError(String(value.message || 'The operation was rejected.'));
        if (method === 'build_one_off_profile' && value.data) dashboard(value.data);
    }
  }
  return value as ApiMap[K]['result'];
}

export async function api<K extends keyof ApiMap>(
  method: K,
  args: ApiMap[K]['args'],
  signal?: AbortSignal,
): Promise<ApiMap[K]['result']> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) controller.abort();
  const timeout = method === 'build_one_off_profile' ? 180000 : 20000;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timedOut = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(
        new ApiError(
          'The local analytics service did not respond. Retry or restart the app.',
          'transport',
        ),
      );
    }, timeout);
  });
  try {
    const request = async (): Promise<unknown> => {
      if ('__TAURI_INTERNALS__' in window)
        return invoke<unknown>('backend_request', { method, args });
      const response = await fetch('/api/rpc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ method, args }),
        signal: controller.signal,
      });
      const envelope: unknown = await response.json().catch(() => null);
      if (!response.ok && !isRecord(envelope))
        throw new ApiError(`Analytics service unavailable (${response.status}).`, 'transport');
      object(envelope, 'RPC envelope');
      if (envelope.error)
        throw new ApiError(
          typeof envelope.error === 'string'
            ? envelope.error
            : 'The analytics service rejected this request.',
        );
      if (!response.ok)
        throw new ApiError(`Analytics service unavailable (${response.status}).`, 'transport');
      contract('result' in envelope, 'RPC result');
      return envelope.result;
    };
    return validateResult(method, await Promise.race([request(), timedOut]));
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (signal?.aborted) throw error;
    throw new ApiError(
      error instanceof Error ? error.message : 'The local analytics service is disconnected.',
      'transport',
    );
  } finally {
    if (timer) clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}
