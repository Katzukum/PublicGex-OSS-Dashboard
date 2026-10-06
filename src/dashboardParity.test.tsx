import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import Cockpit from './Cockpit';
import { StrikeMatrix } from './ResearchViews';
import { TraceCharts } from './charts';
import { aggregateStrikes } from './models';
import { chartTime } from './chartConstants';
import { traceViewportSelection } from './traceHeatmap';
import {
  cockpitMetrics,
  matrixRows,
  matrixState,
  traceCandles,
  traceStability,
  zeroGammaLevels,
} from './parityModels';
import type { Dashboard, TraceData, Workspace } from './types';

const dashboard: Dashboard = {
  snapshot: {
    id: 1,
    symbol: 'SPX',
    timestamp: '2026-09-30 13:30:00',
    spot_price: 100,
    total_net_gex: 2e6,
    flip_strike: 99,
    gex_slope: 1e5,
  },
  profile: [
    { strike_price: 100, option_type: 'CALL', gex_value: 1e6, open_interest: 10 },
    { strike_price: 100, option_type: 'PUT', gex_value: -0.6e6, open_interest: 20 },
    { strike_price: 101, option_type: 'CALL', gex_value: 1.6e6, open_interest: 15 },
  ],
  history: [
    { timestamp: '2026-09-30 12:30:00', total_net_gex: 0.5e6 },
    { timestamp: '2026-09-30 13:30:00', total_net_gex: 2e6 },
  ],
  gamma_sweep: { status: 'ok', points: [], zero_crossings: { below: 99.5, above: 102 } },
};

describe('source-derived dashboard parity', () => {
  it('renders decision overlays when saved events have null, missing or blank labels', () => {
    const trace: TraceData = {
      symbol: 'SPX',
      timestamp: '2026-09-30 13:30:00',
      session_date: '2026-09-30',
      spot_price: 100,
      heatmap: [{ timestamp: '2026-09-30 13:30:00', strike: 100, net_gex: 2e6 }],
      overlays: [
        { timestamp: '2026-09-30 13:30:00', overlay_type: 'scenario', label: null },
        { timestamp: '2026-09-30 13:30:00', overlay_type: 'alert' },
        { timestamp: '2026-09-30 13:30:00', overlay_type: 'scenario', label: '  ' },
      ],
    };
    expect(() =>
      renderToStaticMarkup(
        <TraceCharts data={trace} metric="net_gex" theme="dark" overlays cursor={0} width={60} />,
      ),
    ).not.toThrow();
  });
  it('preserves the original cockpit exposure calculations', () => {
    expect(cockpitMetrics(dashboard)).toEqual({
      call: 2.6e6,
      put: -0.6e6,
      local: 2e6,
      change: 1.5e6,
    });
    expect(zeroGammaLevels(dashboard.gamma_sweep, 99)).toEqual([
      { label: 'Zero gamma below', value: 99.5 },
      { label: 'Zero gamma above', value: 102 },
    ]);
    expect(zeroGammaLevels(undefined, null)).toEqual([]);
  });
  it('shows the missing original cockpit metrics, basket signal and candidate geometry', () => {
    const workspace: Workspace = {
      schema_version: 2,
      symbol: 'SPX',
      snapshot_id: 1,
      dashboard,
      decision: { score: 0.42, bias: 'CALL', target: null, invalidation: null },
      regime: { index_basket: { label: 'GRIND_UP', strategy: 'Positive gamma basket' } },
      execution_candidates: {
        ideas: {
          butterfly: {
            status: 'EXECUTABLE',
            side: 'CALL',
            lower: 95,
            center: 100,
            upper: 105,
            lower_breakeven: 97,
            upper_breakeven: 103,
            estimated_debit: 2,
            max_profit: 3,
            estimated_debit_dollars: 200,
          },
          debit_spread: { status: 'MODELED_ONLY', breakeven: 102, target: 104 },
        },
      },
    };
    const html = renderToStaticMarkup(
      <Cockpit workspace={workspace} theme="dark" onJournal={() => {}} />,
    );
    for (const label of [
      'Bias score',
      '42%',
      'Index Gamma Basket',
      'Positive gamma basket',
      'Total Call GEX',
      'Total Put GEX',
      'Gamma Exposure',
      'GEX Change',
      'Zero Gamma',
      'Gamma Slope',
      'Breakeven tent',
      'Modeled debit risk',
      '97.00',
      '103.00',
    ])
      expect(html).toContain(label);
  });
  it('filters only rows that satisfy both original tiny-position conditions', () => {
    const rows = aggregateStrikes([
      { strike_price: 95, option_type: 'CALL', gex_value: 99999, open_interest: 9 },
      { strike_price: 100, option_type: 'CALL', gex_value: 100000, open_interest: 0 },
      { strike_price: 105, option_type: 'PUT', gex_value: -1, open_interest: 10 },
    ]);
    expect(matrixRows(rows).map((row) => row.strike)).toEqual([100, 105]);
    expect(matrixRows(rows, true)).toHaveLength(3);
    expect(matrixState(rows[1]!, 100000)).toBe('MAGNET');
    expect(matrixState(rows[2]!, 100000)).toBe('VOLATILITY');
  });
  it('restores visible dealer state, total OI, and low-activity control in Strike Matrix', () => {
    const html = renderToStaticMarkup(<StrikeMatrix dashboard={dashboard} />);
    for (const label of [
      'OI proxy sign',
      'MAGNET',
      'STABILITY',
      'Positive OI proxy',
      'Total OI',
      'Include low-activity strikes',
      'Jump to spot',
    ])
      expect(html).toContain(label);
  });
  it('reproduces original minute OHLC including previous-close opening price', () => {
    const candles = traceCandles(
      [
        { timestamp: '2026-09-30 13:30:01', spot_price: 100 },
        { timestamp: '2026-09-30 13:30:20', spot_price: 102 },
        { timestamp: '2026-09-30 13:31:20', spot_price: 99 },
      ],
      ['2026-09-30 13:30:00', '2026-09-30 13:31:00', '2026-09-30 13:32:00'],
    );
    expect(candles.map(({ open, close, low, high }) => ({ open, close, low, high }))).toEqual([
      { open: 100, close: 102, low: 100, high: 102 },
      { open: 102, close: 99, low: 99, high: 102 },
      { open: 99, close: 99, low: 99, high: 99 },
    ]);
  });
  it('keeps native candles aligned with timezone-free backend wall time', () => {
    expect(chartTime('2026-09-30 13:30:00')).toBe(Date.UTC(2026, 8, 30, 13, 30) / 1000);
  });
  it('synchronizes sparse time-axis zoom to the correct timeline observation', () => {
    const times = ['2026-09-30 13:00:00', '2026-09-30 13:10:00', '2026-09-30 13:30:00'].map(
      chartTime,
    );
    expect(
      traceViewportSelection(times, {
        from: chartTime('2026-09-30 13:02:00'),
        to: chartTime('2026-09-30 13:20:00'),
      }),
    ).toEqual({ cursor: 1, width: 18 });
    expect(traceViewportSelection([], { from: NaN, to: NaN })).toBeUndefined();
  });
  it('preserves TRACE stability as gamma concentration rather than probability', () => {
    const trace: TraceData = {
      symbol: 'SPX',
      timestamp: '2026-09-30 13:30:00',
      session_date: '2026-09-30',
      spot_price: 100,
      total_net_gex: 40,
      heatmap: [],
      latest_profile: [
        { strike: 100, net_gex: 70 },
        { strike: 101, net_gex: -30 },
      ],
    };
    expect(traceStability(trace)).toBe(40);
    expect(traceStability({ ...trace, latest_profile: [] })).toBeNull();
  });
});
