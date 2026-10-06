import { describe, expect, it } from 'vitest';
import {
  backendHealth,
  chooseInstrument,
  eventActivity,
  recordActivities,
  statusActivity,
} from './workspace-shell';

describe('original workspace behavior', () => {
  it('selects the configured first instrument before a hardcoded SPX preference', () => {
    expect(
      chooseInstrument(['IWM', 'NDX', 'QQQ', 'SPX', 'SPY'], '', [
        'SPY',
        'QQQ',
        'IWM',
        'SPX',
        'NDX',
      ]),
    ).toBe('SPY');
    expect(chooseInstrument(['SPX', 'SPY'], 'SPX', ['SPY'])).toBe('SPX');
  });
  it('distinguishes collector progress, missing snapshots, stale data, and failed health', () => {
    expect(backendHealth({ ok: true, run_status: 'running', run_age_seconds: 12 }).label).toBe(
      'Collector running · 12s',
    );
    expect(backendHealth({ ok: true }).label).toBe('No snapshot');
    expect(backendHealth({ ok: true, snapshot_age_seconds: 240 }).tone).toBe('warning');
    expect(backendHealth({ ok: false }).tone).toBe('negative');
  });
  it('records refresh events and coalesces repeat health updates', () => {
    const entry = eventActivity({
      type: 'data_refresh',
      payload: { symbol: 'SPY', snapshot_id: 42 },
    })!;
    expect(entry.message).toContain('#42');
    const health = statusActivity({
      ok: true,
      event_bridge: { status: 'listening', last_event_type: 'data_refresh' },
    });
    expect(health.message).toContain('event bridge data_refresh');
    expect(recordActivities([entry, health], [health])).toHaveLength(2);
    expect(eventActivity({ type: 'unknown' })).toBeUndefined();
  });
});
