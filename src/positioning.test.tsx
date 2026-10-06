import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { validateResult } from './api';
import {
  PositioningPanel,
  persistentPositioningLevels,
  positioningValue,
} from './PositioningPanel';
import { traceProfileStrikeRange } from './parityModels';
import type { Positioning, PositioningStrike } from './types';

const strike: PositioningStrike = {
  strike: 7750,
  call_oi_gex: 8e9,
  put_oi_gex: 2e9,
  gross_oi_gex: 10e9,
  net_oi_proxy: 6e9,
  call_activity_5m: 0,
  put_activity_5m: 2e6,
  activity_5m: 2e6,
  call_activity_15m: null,
  put_activity_15m: null,
  activity_15m: null,
  oi_persistence: 0.8,
};
const model: Positioning = {
  model: 'oi_activity_v1',
  as_of: '2026-10-02 10:44:35',
  dealer_direction: 'unknown',
  status: 'partial',
  warnings: ['The 15 minute activity window is not yet covered.'],
  windows_minutes: [5, 15],
  coverage: { observed_minutes: 7, max_gap_seconds: 60, contracts: 4, valid_activity_contracts: 2 },
  strikes: [strike],
  top_levels: [7750],
};
function trace(positioning?: unknown) {
  return { symbol: 'SPX', session_date: '2026-10-02', heatmap: [], positioning };
}

describe('positioning observations', () => {
  it('keeps both OI sides unsigned and distinguishes unknown activity from observed zero', () => {
    expect(positioningValue(strike, 'oi', 'put')).toBe(2e9);
    expect(positioningValue(strike, 'oi', 'combined')).toBe(10e9);
    expect(positioningValue(strike, '5m', 'call')).toBe(0);
    expect(positioningValue(strike, '15m', 'combined')).toBeNull();
  });
  it('preserves server level ranking rather than reranking by signed exposure', () => {
    const data = {
      ...model,
      strikes: [{ ...strike, strike: 7760 }, strike],
      top_levels: [7750, 7760, 7750],
    };
    expect(persistentPositioningLevels(data).map((row) => row.strike)).toEqual([7750, 7760]);
  });
  it('accepts optional legacy payloads, nullable windows, and observed zero at the RPC boundary', () => {
    expect(validateResult('get_trace_data', trace()).positioning).toBeUndefined();
    expect(
      validateResult('get_trace_data', trace(model)).positioning?.strikes[0]?.call_activity_5m,
    ).toBe(0);
    expect(
      validateResult('get_trace_data', trace(model)).positioning?.strikes[0]?.activity_15m,
    ).toBeNull();
    expect(
      validateResult(
        'get_trace_data',
        trace({ ...model, status: 'unavailable', strikes: [], top_levels: [] }),
      ).positioning?.status,
    ).toBe('unavailable');
  });
  it('rejects invented dealer direction, negative unsigned values, and missing nullable activity fields', () => {
    expect(() =>
      validateResult('get_trace_data', trace({ ...model, dealer_direction: 'long' })),
    ).toThrow(/positioning model/);
    expect(() =>
      validateResult(
        'get_trace_data',
        trace({ ...model, strikes: [{ ...strike, put_oi_gex: -2e9 }] }),
      ),
    ).toThrow(/positioning strikes/);
    expect(() =>
      validateResult(
        'get_trace_data',
        trace({ ...model, strikes: [{ ...strike, activity_5m: undefined }] }),
      ),
    ).toThrow(/positioning strikes/);
  });
  it('validates positioning on the dashboard boundary too', () => {
    const data = {
      snapshot: { symbol: 'SPX', spot_price: 7748, total_net_gex: 6e9 },
      profile: [],
      history: [],
      positioning: { ...model, coverage: { ...model.coverage, valid_activity_contracts: 5 } },
    };
    expect(() => validateResult('get_one_off_profile', data)).toThrow(/positioning coverage/);
  });
  it('makes absent observations explicit and labels TRACE data as latest, independent of replay', () => {
    const absent = renderToStaticMarkup(
      <PositioningPanel theme="dark" symbol="SPX" spot={7748} latestOnly />,
    );
    expect(absent).toContain('Positioning observations unavailable');
    expect(absent).toContain('Latest snapshot · independent of replay cursor');
    expect(absent).toContain('Dealer direction unknown');
    const html = renderToStaticMarkup(
      <PositioningPanel
        data={{ ...model, strikes: [{ ...strike, activity_5m: 0 }] }}
        theme="dark"
        symbol="SPX"
        spot={7748}
      />,
    );
    expect(html).toContain('$0.0M');
    expect(html).toContain('Unknown');
    expect(html).toContain('80%');
    expect(html).toContain(model.warnings[0]);
  });
  it('focuses the profile without adding chart-library margins to the strike window', () => {
    const strikes = Array.from({ length: 101 }, (_, index) => 7500 + index * 5);
    const near = traceProfileStrikeRange(strikes, 7748.03)!;
    const wide = traceProfileStrikeRange(strikes, 7748.03, 0.015)!;
    expect(near).toEqual([7685, 7810]);
    expect(wide).toEqual([7630, 7865]);
    expect(traceProfileStrikeRange(strikes, 7748, null)).toEqual([7497.5, 8002.5]);
    expect(traceProfileStrikeRange([], 7748)).toBeUndefined();
  });
});
