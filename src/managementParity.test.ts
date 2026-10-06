import { describe, expect, it } from 'vitest';
import {
  journalContext,
  journalPrefill,
  parseWeights,
  settingsWithWeights,
} from './managementParity';
import type { Settings, Workspace } from './types';

describe('management parity contracts', () => {
  it('normalizes edited baskets and keeps the legacy index-basket alias synchronized', () => {
    const settings = {
      weights_whale: { SPX: 1 },
      feature_flags: { gamma_sweep_enabled: false },
    } as unknown as Settings;
    const result = settingsWithWeights(
      settings,
      'spy=0.5, QQQ=0.3\nIWM=0.2',
      'SPX=0.45,NDX=0.35,IWM=0.2',
    );
    expect(result.weights).toEqual({ SPY: 0.5, QQQ: 0.3, IWM: 0.2 });
    expect(result.weights_whale).toEqual(result.weights_index_basket);
    expect(result.weights_index_basket).toEqual({ SPX: 0.45, NDX: 0.35, IWM: 0.2 });
    expect(result.feature_flags).toEqual(settings.feature_flags);
    expect(settings.weights_whale).toEqual({ SPX: 1 });
  });
  it.each(['SPY=-1', 'SPY=NaN', 'SPY=', 'SPY=0,QQQ=0', 'spy=1,SPY=2', 'SPY', ''])(
    'rejects invalid basket %s',
    (value) => {
      expect(() => parseWeights(value, 'Basket')).toThrow();
    },
  );
  it('prefills the snapshot session rather than today for a historical cockpit', () => {
    const workspace = { symbol: 'SPX', as_of: '2026-09-25T15:12:00' } as Workspace;
    expect(journalPrefill(workspace, 'SPX', '2026-09-30T10:05')).toEqual({
      session_date: '2026-09-25',
      entry_time: '2026-09-30T10:05',
    });
    expect(journalPrefill(workspace, 'QQQ', '2026-09-30T10:05').session_date).toBe('2026-09-30');
  });
  it('preserves scenario, regime and the first executable idea liquidity in a journal entry', () => {
    const workspace = {
      symbol: 'SPX',
      as_of: '2026-09-25T15:12:00',
      active_scenario: { scenario_id: 'pin-5', scenario_type: 'PIN_MEAN_REVERSION' },
      regime: { traders: { label: 'PINNING' } },
      execution_candidates: {
        ideas: {
          rejected: { status: 'REJECTED', liquidity_grade: 'C' },
          executable: { status: 'EXECUTABLE', liquidity_grade: 'A' },
        },
      },
    } as unknown as Workspace;
    expect(journalContext(workspace, 'SPX', '2026-09-25')).toEqual({
      scenario_id: 'pin-5',
      scenario_type: 'PIN_MEAN_REVERSION',
      regime: 'PINNING',
      liquidity_grade: 'A',
    });
    expect(journalContext(workspace, 'QQQ', '2026-09-25')).toEqual({});
    expect(journalContext(workspace, 'SPX', '2026-09-30')).toEqual({});
  });
});
