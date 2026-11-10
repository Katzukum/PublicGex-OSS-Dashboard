import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { validateResult } from './api';
import { EnvelopePanel, envelopePairAt } from './EnvelopePanel';
import { exhaustionEvidence, visibleExhaustionEvents } from './ExhaustionMarkers';
import { PressurePanel, pressurePointsAt } from './PressurePanel';
import type {
  EnvelopeHistoryPoint,
  EnvelopePair,
  EstimatedPressure,
  ExhaustionEvent,
} from './types';

const envelope = (source: 'volume_gamma' | 'oi_gamma', major = 6010) => ({
  model: 'publicgex_envelopes_v1' as const,
  source,
  status: 'partial' as const,
  symbol: 'SPX',
  as_of: '2026-10-06T10:30:00',
  zero_gamma: null,
  major_gamma: major,
  minor_gamma: 5990,
  major_strength: 48_200_000,
  minor_strength: 35_100_000,
  coverage: {
    contracts: 12,
    usable_contracts: 10,
    excluded_contracts: 2,
    excluded_by_reason: { invalid_gamma: 2 },
    sweep_usable_contracts: 10,
    min_strike: 5900,
    max_strike: 6100,
    collected_range: { min: 5900, max: 6100 },
    modeled_range: { min: 5880, max: 6120, step: 1 },
    boundary_flags: { major_gamma: false, minor_gamma: false, zero_gamma: false },
  },
  warnings: ['Boundary limited.'],
});
const envelopes: EnvelopePair = {
  flow: envelope('volume_gamma'),
  structural: envelope('oi_gamma', 6020),
};
const pressure: EstimatedPressure = {
  model: 'estimated_hedge_pressure_v1',
  status: 'partial',
  session_date: '2026-10-06',
  config: { ema_periods: [5, 10] },
  points: [
    {
      timestamp: '2026-10-06T10:31:00',
      available_at: '2026-10-06T10:31:00Z',
      status: 'partial',
      usable: true,
      impulse_shares: 0,
      impulse_dollars: 0,
      cumulative_shares: 0,
      cumulative_dollars: 0,
      ema_fast: null,
      ema_slow: null,
      classification_ratio: 0.75,
      impulse_5m: 0,
      slope_5m: 0,
      trend: 'flat',
    },
  ],
  current: {
    cumulative_dollars: 0,
    slope_5m: 0,
    impulse_5m: 0,
    trend: 'flat',
    relative_to_ema_fast: null,
    relative_to_ema_slow: null,
  },
  coverage: {
    classified_contract_volume: 75,
    neutral_contract_volume: 0,
    unclassified_contract_volume: 25,
    classification_ratio: 0.75,
    observed_minutes: 1,
    usable_minutes: 1,
    valid_intervals: 1,
    discarded_intervals: 0,
    missing_clock_count: 0,
    max_gap_seconds: 60,
  },
  warnings: [],
};
const event: ExhaustionEvent = {
  id: 'event-1',
  model: 'estimated_option_exhaustion_v1',
  symbol: 'SPX',
  session: '2026-10-06',
  timestamp: '2026-10-06T10:35:00',
  available_at: '2026-10-06T10:35:00Z',
  side: 'CALL',
  action: 'BUY',
  label: 'Estimated Call Buy',
  estimated: true,
  price: 6008,
  flow_dollars: 1_000_000,
  flow_percentile: 0.94,
  price_position: 0.87,
  score: 0.78,
  components: { flow_extremity: 0.94, weakening: 1, location: 0.87, divergence: 0, proximity: 1 },
  evidence: ['pressure_turn', 'major_gamma_proximity'],
  evidence_flags: { pressure_turn: true, major_gamma_proximity: true },
  classification_ratio: 0.75,
  envelope: { source: 'volume_gamma', nearest_kind: 'major', nearest_level: 6010 },
  hedge_direction: 1,
  bar_source: 'collector_spot_observations',
  config_fingerprint: 'abc123',
};

function trace(research: Record<string, unknown> = {}) {
  return { symbol: 'SPX', session_date: '2026-10-06', heatmap: [], ...research };
}

describe('option research frontend contract', () => {
  it('validates nested research settings while preserving old settings payloads', () => {
    const base = { symbols: ['SPX'], refresh_interval: 10 };
    expect(validateResult('get_settings', base).option_research).toBeUndefined();
    const optionResearch = {
      envelope_source: 'volume_gamma',
      pressure_ema_1: 9,
      pressure_ema_2: 21,
      exhaustion_sensitivity: 'high',
      exhaustion_price_filter_enabled: true,
      exhaustion_period_bars: 5,
      exhaustion_call_above: 0.7,
      exhaustion_put_below: 0.3,
      observation_timezone: 'America/New_York',
    };
    expect(
      validateResult('get_settings', { ...base, option_research: optionResearch }).option_research
        ?.envelope_source,
    ).toBe('volume_gamma');
    expect(() =>
      validateResult('get_settings', {
        ...base,
        option_research: { ...optionResearch, exhaustion_call_above: 0.2 },
      }),
    ).toThrow(/option research settings/);
  });

  it('keeps old payloads compatible and validates new envelope fields when present', () => {
    expect(validateResult('get_trace_data', trace()).envelopes).toBeUndefined();
    expect(validateResult('get_trace_data', trace({ envelopes })).envelopes?.flow.major_gamma).toBe(
      6010,
    );
    expect(() =>
      validateResult(
        'get_trace_data',
        trace({ envelopes: { ...envelopes, flow: { ...envelopes.flow, zero_gamma: '6000' } } }),
      ),
    ).toThrow(/envelopes flow levels/);
  });

  it('accepts configured EMA aliases without fixed 15/30 fields and rejects corrupt ratios', () => {
    expect(
      validateResult('get_trace_data', trace({ estimated_pressure: pressure })).estimated_pressure
        ?.points[0]?.cumulative_dollars,
    ).toBe(0);
    expect(() =>
      validateResult(
        'get_trace_data',
        trace({
          estimated_pressure: {
            ...pressure,
            coverage: { ...pressure.coverage, classification_ratio: 1.1 },
          },
        }),
      ),
    ).toThrow(/estimated pressure coverage/);
  });

  it('selects only envelope states available at the replay cursor', () => {
    const history: EnvelopeHistoryPoint[] = [
      {
        snapshot_id: 1,
        as_of: '2026-10-06T10:30:00',
        available_at: '2026-10-06T10:30:00Z',
        ...envelopes,
      },
      {
        snapshot_id: 2,
        as_of: '2026-10-06T10:40:00',
        available_at: '2026-10-06T10:40:00Z',
        flow: envelope('volume_gamma', 6050),
        structural: envelope('oi_gamma', 6060),
      },
    ];
    expect(
      envelopePairAt(envelopes, history, Date.parse('2026-10-06T10:35:00Z') / 1000)?.flow
        .major_gamma,
    ).toBe(6010);
    expect(
      envelopePairAt(envelopes, history, Date.parse('2026-10-06T10:45:00Z') / 1000)?.flow
        .major_gamma,
    ).toBe(6050);
  });

  it('labels sources, preserves null levels, and distinguishes measured zero pressure', () => {
    const envelopeHtml = renderToStaticMarkup(
      <EnvelopePanel envelopes={envelopes} source="volume_gamma" />,
    );
    expect(envelopeHtml).toContain('Flow proxy');
    expect(envelopeHtml).toContain('Zero Gamma');
    expect(envelopeHtml).toContain('—');
    const pressureHtml = renderToStaticMarkup(<PressurePanel data={pressure} theme="dark" />);
    expect(pressureHtml).toContain('EMA 5');
    expect(pressureHtml).toContain('$0');
    expect(pressureHtml).toContain('Flat');
    expect(pressureHtml).toContain('dealer/customer identity is not observed');
  });

  it('clips pressure points and exhaustion markers at availability time with evidence text', () => {
    const before = Date.parse('2026-10-06T10:30:00Z') / 1000;
    const after = Date.parse('2026-10-06T10:36:00Z') / 1000;
    expect(pressurePointsAt(pressure, before)).toEqual([]);
    expect(pressurePointsAt(pressure, after)).toHaveLength(1);
    expect(visibleExhaustionEvents([event], before)).toEqual([]);
    expect(visibleExhaustionEvents([event], after)).toEqual([event]);
    expect(exhaustionEvidence(event).join(' ')).toContain('pressure turn');
    expect(exhaustionEvidence(event).join(' ')).toContain('Nearest Envelope major 6010');
  });
});
