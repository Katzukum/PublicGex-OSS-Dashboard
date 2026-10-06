import { describe, expect, it } from 'vitest';
import {
  aggregateStrikes,
  heatmapGrid,
  marketAlert,
  number,
  traceWindow,
  unseenMarketAlerts,
  validateJournalNumbers,
} from './models';
import type { HeatmapRow } from './types';

describe('market display models', () => {
  it('keeps routine refresh events out of actionable notices', () => {
    expect(marketAlert({ type: 'data_refresh', payload: { symbol: 'SPX' } })).toBeUndefined();
    expect(marketAlert({ type: 'MARKET_UPDATE', data: {} })).toBeUndefined();
  });
  it('preserves old and new levels in a magnet-change notice', () => {
    const alert = marketAlert({
      type: 'magnet_change',
      received_at: '2026-09-30T10:00:00',
      payload: { symbol: 'SPX', old_magnet: 6000, new_magnet: 6010 },
    });
    expect(alert?.title).toBe('SPX · Magnet change');
    expect(alert?.message).toBe('SPX magnet moved 6,000 → 6,010.');
  });
  it('deduplicates persisted and live decision alerts and retains dismissals', () => {
    const payload = {
      alert_id: 'alert_17',
      symbol: 'SPX',
      alert_type: 'event',
      severity: 'critical',
      message: 'Blocked event window',
    };
    const persisted = marketAlert(payload)!;
    const live = marketAlert({ type: 'decision_alert', payload })!;
    const seen = new Set<string>();
    expect(unseenMarketAlerts([persisted, live], seen)).toEqual([persisted]);
    expect(persisted.severity).toBe('critical');
    expect(unseenMarketAlerts([persisted], seen)).toEqual([]);
  });
  it('does not fabricate alert text from malformed payloads', () => {
    expect(marketAlert({ type: 'decision_alert', payload: {} })).toBeUndefined();
    expect(
      marketAlert({ type: 'magnet_change', payload: { old_magnet: null, new_magnet: null } }),
    ).toBeUndefined();
  });
  it('keeps unavailable prices distinct from valid zero values', () => {
    expect(number(null)).toBe('—');
    expect(number(undefined)).toBe('—');
    expect(number(Number.NaN)).toBe('—');
    expect(number(0)).toBe('0.00');
  });
  it('aggregates calls and puts in native units and keeps OI by side', () => {
    const result = aggregateStrikes([
      { strike_price: 101, option_type: 'PUT', gex_value: -300, open_interest: 4 },
      { strike_price: 100, option_type: 'CALL', gex_value: 200, open_interest: 3 },
      { strike_price: 100, option_type: 'OPTION_CALL', gex_value: 50, open_interest: 1 },
      { strike_price: 100, option_type: 'PUT', gex_value: -75, open_interest: 2 },
    ]);
    expect(result).toEqual([
      { strike: 100, call: 250, put: -75, net: 175, oi: 6, callOI: 4, putOI: 2 },
      { strike: 101, call: 0, put: -300, net: -300, oi: 4, callOI: 0, putOI: 4 },
    ]);
  });
  it('does not classify unknown option types as puts', () => {
    expect(
      aggregateStrikes([{ strike_price: 100, option_type: 'UNKNOWN', gex_value: 100 }]),
    ).toEqual([]);
  });
  it('ignores nonfinite profile inputs', () => {
    expect(aggregateStrikes([{ strike_price: 100, option_type: 'CALL', gex_value: NaN }])).toEqual(
      [],
    );
  });
  const rows: HeatmapRow[] = [
    { timestamp: '2026-09-30 09:32:00', strike: 101, net_gex: -2e6, modeled_delta_pressure: null },
    { timestamp: '2026-09-30 09:30:00', strike: 100, net_gex: 1e6, modeled_delta_pressure: 0 },
  ];
  it('sorts sparse heatmap axes and preserves missing cells as gaps', () => {
    const result = heatmapGrid(rows, 'net_gex');
    expect(result.times).toEqual(['2026-09-30 09:30:00', '2026-09-30 09:32:00']);
    expect(result.strikes).toEqual([100, 101]);
    expect(result.z).toEqual([
      [1, null],
      [null, -2],
    ]);
    expect(result.maxAbs).toBe(2);
  });
  it('preserves absent modeled pressure instead of fabricating zero', () => {
    expect(heatmapGrid(rows, 'modeled_delta_pressure').z).toEqual([
      [0, null],
      [null, null],
    ]);
  });
  it('uses elapsed minutes for sparse TRACE observations', () => {
    const times = Array.from(
      { length: 61 },
      (_, index) =>
        `2026-09-30 ${String(9 + Math.floor((30 + index * 2) / 60)).padStart(2, '0')}:${String((30 + index * 2) % 60).padStart(2, '0')}:00`,
    );
    expect(traceWindow(times, 60, 60)).toEqual(['2026-09-30 10:30:00', '2026-09-30 11:30:00']);
  });
  it('clamps replay cursor and handles an empty session', () => {
    expect(traceWindow([], 0)).toBeUndefined();
    expect(traceWindow(['2026-09-30 09:30:00', '2026-09-30 09:32:00'], 999, 60)).toEqual([
      '2026-09-30 09:30:00',
      '2026-09-30 09:32:00',
    ]);
  });
  it('rejects invalid manual execution values but accepts an open zero-cost entry', () => {
    expect(validateJournalNumbers(0, 1, null, 0)).toMatch(/positive/);
    expect(validateJournalNumbers(1.5, 1, null, 0)).toMatch(/whole/);
    expect(validateJournalNumbers(1, -1, null, 0)).toMatch(/entry/);
    expect(validateJournalNumbers(1, 1, -1, 0)).toMatch(/Exit/);
    expect(validateJournalNumbers(1, 1, null, -1)).toMatch(/Fees/);
    expect(validateJournalNumbers(1, 0, null, 0)).toBeUndefined();
  });
});
