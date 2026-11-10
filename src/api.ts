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
const status = (value: unknown) => ['ready', 'partial', 'unavailable'].includes(String(value));
const nullableNumeric = (value: unknown) => value === null || numeric(value);
const unsigned = (value: unknown) => numeric(value) && value >= 0;
const ratio = (value: unknown) => value === null || (numeric(value) && value >= 0 && value <= 1);
const strings = (value: unknown) =>
  Array.isArray(value) && value.every((item) => typeof item === 'string');

function envelope(value: unknown, path = 'option envelope'): void {
  object(value, path);
  contract(
    value.model === 'publicgex_envelopes_v1' &&
      ['volume_gamma', 'oi_gamma'].includes(String(value.source)) &&
      status(value.status) &&
      (value.as_of === null || typeof value.as_of === 'string'),
    `${path} model`,
  );
  contract(
    ['zero_gamma', 'major_gamma', 'minor_gamma', 'major_strength', 'minor_strength'].every((key) =>
      nullableNumeric(value[key]),
    ),
    `${path} levels`,
  );
  object(value.coverage, `${path} coverage`);
  const coverage = value.coverage;
  const optionalRange = (item: unknown) => {
    if (item === null) return true;
    if (!isRecord(item)) return false;
    return (
      numeric(item.min) && numeric(item.max) && (item.step === undefined || numeric(item.step))
    );
  };
  object(coverage.excluded_by_reason, `${path} excluded reasons`);
  object(coverage.boundary_flags, `${path} boundary flags`);
  const boundaryFlags = coverage.boundary_flags;
  contract(
    unsigned(coverage.contracts) &&
      Number.isInteger(coverage.contracts) &&
      unsigned(coverage.usable_contracts) &&
      Number.isInteger(coverage.usable_contracts) &&
      Number(coverage.usable_contracts) <= Number(coverage.contracts) &&
      unsigned(coverage.excluded_contracts) &&
      Number.isInteger(coverage.excluded_contracts) &&
      unsigned(coverage.sweep_usable_contracts) &&
      Number.isInteger(coverage.sweep_usable_contracts) &&
      nullableNumeric(coverage.min_strike) &&
      nullableNumeric(coverage.max_strike) &&
      optionalRange(coverage.collected_range) &&
      optionalRange(coverage.modeled_range) &&
      Object.values(coverage.excluded_by_reason).every(
        (count) => unsigned(count) && Number.isInteger(count),
      ) &&
      ['major_gamma', 'minor_gamma', 'zero_gamma'].every(
        (key) => typeof boundaryFlags[key] === 'boolean',
      ),
    `${path} coverage`,
  );
  contract(strings(value.warnings), `${path} warnings`);
}
function envelopes(value: unknown, path = 'option envelopes'): void {
  object(value, path);
  envelope(value.flow, `${path} flow`);
  envelope(value.structural, `${path} structural`);
  object(value.flow, `${path} flow`);
  object(value.structural, `${path} structural`);
  contract(value.flow.source === 'volume_gamma', `${path} flow source`);
  contract(value.structural.source === 'oi_gamma', `${path} structural source`);
}
function pressurePoint(value: unknown, path: string, provisional = false): void {
  object(value, path);
  contract(
    typeof value.timestamp === 'string' &&
      typeof value.available_at === 'string' &&
      (status(value.status) || value.status === 'provisional') &&
      typeof value.usable === 'boolean' &&
      nullableNumeric(value.impulse_shares) &&
      nullableNumeric(value.impulse_dollars) &&
      nullableNumeric(value.cumulative_shares) &&
      nullableNumeric(value.cumulative_dollars) &&
      (provisional || (nullableNumeric(value.ema_fast) && nullableNumeric(value.ema_slow))) &&
      (value.ema_15 === undefined || nullableNumeric(value.ema_15)) &&
      (value.ema_30 === undefined || nullableNumeric(value.ema_30)) &&
      ratio(value.classification_ratio) &&
      (provisional ||
        (nullableNumeric(value.impulse_5m) &&
          nullableNumeric(value.slope_5m) &&
          ['rising', 'falling', 'flat', 'unavailable'].includes(String(value.trend)))),
    path,
  );
  if (value.buckets != null) {
    object(value.buckets, `${path} buckets`);
    for (const bucket of Object.values(value.buckets)) {
      object(bucket, `${path} bucket`);
      contract(
        unsigned(bucket.classified_volume) &&
          numeric(bucket.delta_shares) &&
          numeric(bucket.delta_dollars) &&
          nullableNumeric(bucket.gamma_activity),
        `${path} bucket`,
      );
    }
  }
}
function estimatedPressure(value: unknown, path = 'estimated pressure'): void {
  if (value === null) return;
  object(value, path);
  contract(
    value.model === 'estimated_hedge_pressure_v1' &&
      status(value.status) &&
      (value.session_date === null || typeof value.session_date === 'string') &&
      strings(value.warnings),
    `${path} model`,
  );
  contract(Array.isArray(value.points), `${path} points`);
  object(value.config, `${path} config`);
  contract(
    Array.isArray(value.config.ema_periods) &&
      value.config.ema_periods.length === 2 &&
      value.config.ema_periods.every((period) => unsigned(period) && Number.isInteger(period)),
    `${path} EMA periods`,
  );
  value.points.forEach((point, index) => pressurePoint(point, `${path} point ${index}`));
  if (value.provisional != null)
    pressurePoint(value.provisional, `${path} provisional point`, true);
  if (value.current != null) {
    object(value.current, `${path} current`);
    const current = value.current;
    contract(
      ['cumulative_dollars', 'slope_5m', 'relative_to_ema_fast', 'relative_to_ema_slow'].every(
        (key) => nullableNumeric(current[key]),
      ),
      `${path} current`,
    );
  }
  object(value.coverage, `${path} coverage`);
  contract(
    unsigned(value.coverage.classified_contract_volume) &&
      unsigned(value.coverage.neutral_contract_volume) &&
      unsigned(value.coverage.unclassified_contract_volume) &&
      ratio(value.coverage.classification_ratio) &&
      unsigned(value.coverage.observed_minutes) &&
      unsigned(value.coverage.usable_minutes) &&
      unsigned(value.coverage.valid_intervals) &&
      unsigned(value.coverage.discarded_intervals) &&
      nullableNumeric(value.coverage.max_gap_seconds) &&
      unsigned(value.coverage.missing_clock_count),
    `${path} coverage`,
  );
}
function exhaustionEvent(value: unknown, path: string): void {
  object(value, path);
  contract(
    value.model === 'estimated_option_exhaustion_v1' &&
      typeof value.id === 'string' &&
      typeof value.symbol === 'string' &&
      typeof value.session === 'string' &&
      typeof value.timestamp === 'string' &&
      typeof value.available_at === 'string' &&
      ['CALL', 'PUT'].includes(String(value.side)) &&
      ['BUY', 'SELL'].includes(String(value.action)) &&
      typeof value.label === 'string' &&
      value.estimated === true &&
      numeric(value.price) &&
      unsigned(value.flow_dollars) &&
      ratio(value.flow_percentile) &&
      ratio(value.price_position) &&
      ratio(value.score) &&
      ratio(value.classification_ratio) &&
      (value.hedge_direction === -1 || value.hedge_direction === 1) &&
      value.bar_source === 'collector_spot_observations' &&
      typeof value.config_fingerprint === 'string' &&
      strings(value.evidence) &&
      (value.evidence_flags === undefined ||
        (isRecord(value.evidence_flags) &&
          Object.values(value.evidence_flags).every((flag) => typeof flag === 'boolean'))),
    path,
  );
  object(value.components, `${path} components`);
  const components = value.components;
  contract(
    ['flow_extremity', 'weakening', 'location', 'divergence', 'proximity'].every((key) =>
      ratio(components[key]),
    ),
    `${path} components`,
  );
}
function exhaustion(value: unknown, path = 'exhaustion'): void {
  if (value === null) return;
  object(value, path);
  contract(
    value.model === 'estimated_option_exhaustion_v1' &&
      status(value.status) &&
      strings(value.warnings),
    `${path} model`,
  );
  object(value.coverage, `${path} coverage`);
  if (value.current != null) exhaustionEvent(value.current, `${path} current`);
  if (value.events !== undefined) {
    contract(Array.isArray(value.events), `${path} events`);
    value.events.forEach((event, index) => exhaustionEvent(event, `${path} event ${index}`));
  }
}
function researchConfig(value: unknown, path: string): void {
  object(value, path);
  contract(
    ['volume_gamma', 'oi_gamma'].includes(String(value.envelope_source)) &&
      unsigned(value.pressure_ema_1) &&
      Number.isInteger(value.pressure_ema_1) &&
      unsigned(value.pressure_ema_2) &&
      Number.isInteger(value.pressure_ema_2) &&
      Number(value.pressure_ema_1) < Number(value.pressure_ema_2) &&
      ['low', 'medium', 'high', 'strong'].includes(String(value.exhaustion_sensitivity)) &&
      typeof value.exhaustion_price_filter_enabled === 'boolean' &&
      unsigned(value.exhaustion_period_bars) &&
      Number.isInteger(value.exhaustion_period_bars) &&
      ratio(value.exhaustion_call_above) &&
      ratio(value.exhaustion_put_below) &&
      Number(value.exhaustion_put_below) < Number(value.exhaustion_call_above) &&
      typeof value.observation_timezone === 'string' &&
      value.observation_timezone.length > 0,
    path,
  );
}
function research(value: Record<string, unknown>, path: string): void {
  if ('envelopes' in value && value.envelopes !== undefined)
    envelopes(value.envelopes, `${path} envelopes`);
  if ('estimated_pressure' in value && value.estimated_pressure !== undefined)
    estimatedPressure(value.estimated_pressure, `${path} estimated pressure`);
  if ('exhaustion' in value && value.exhaustion !== undefined)
    exhaustion(value.exhaustion, `${path} exhaustion`);
  if ('research_config' in value && value.research_config !== undefined)
    researchConfig(value.research_config, `${path} research config`);
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
  research(value, 'dashboard');
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
        if (value.option_research !== undefined)
          researchConfig(value.option_research, 'option research settings');
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
        research(value, 'TRACE');
        if (value.envelope_history !== undefined) {
          contract(Array.isArray(value.envelope_history), 'TRACE envelope history');
          value.envelope_history.forEach((item, index) => {
            object(item, `TRACE envelope history ${index}`);
            contract(
              numeric(item.snapshot_id) &&
                typeof item.as_of === 'string' &&
                typeof item.available_at === 'string',
              `TRACE envelope history ${index}`,
            );
            envelopes(item, `TRACE envelope history ${index}`);
          });
        }
        if (value.exhaustion_events !== undefined) {
          contract(Array.isArray(value.exhaustion_events), 'TRACE exhaustion events');
          value.exhaustion_events.forEach((event, index) =>
            exhaustionEvent(event, `TRACE exhaustion event ${index}`),
          );
        }
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
        if (method === 'save_settings' && value.settings) {
          object(value.settings, 'saved settings');
          if (value.settings.option_research !== undefined)
            researchConfig(value.settings.option_research, 'saved option research settings');
        }
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
