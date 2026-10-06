import { useCallback, useMemo, useState } from 'react';
import { HistogramSeries, LineSeries, type IChartApiBase } from 'lightweight-charts';
import { Badge, Empty, Field, Panel } from './components';
import { LightweightChart } from './LightweightChart';
import { finite, millions, number, percent } from './models';
import { numericGrid, strikeLattice, VerticalProfilePrimitive } from './profilePrimitives';
import type { ChartBuildResult } from './chartTypes';
import type { Positioning, PositioningStrike, Theme } from './types';
import './positioning.css';

export type PositioningMeasure = 'oi' | '5m' | '15m';
export type PositioningSide = 'combined' | 'call' | 'put';
const measures = {
  oi: 'OI concentration',
  '5m': 'Recent activity · 5 minutes',
  '15m': 'Recent activity · 15 minutes',
};
const sides = { combined: 'Combined', call: 'Calls', put: 'Puts' };
const colors = { combined: '#9cafff', call: '#59b8dc', put: '#d4a3f1' };

/** Null stays unknown; observed zero is a valid activity measurement. */
export function positioningValue(
  row: PositioningStrike,
  measure: PositioningMeasure,
  side: PositioningSide,
): number | null {
  if (measure === 'oi') return side === 'combined' ? row.gross_oi_gex : row[`${side}_oi_gex`];
  return side === 'combined' ? row[`activity_${measure}`] : row[`${side}_activity_${measure}`];
}

export function persistentPositioningLevels(data: Positioning): PositioningStrike[] {
  const byStrike = new Map(data.strikes.map((row) => [row.strike, row]));
  return [...new Set(data.top_levels)].flatMap((strike) => {
    const row = byStrike.get(strike);
    return row ? [row] : [];
  });
}

function amount(value: number | null) {
  return value === null ? 'Unknown' : millions(value);
}

export function PositioningPanel({
  data,
  theme,
  symbol,
  spot,
  latestOnly = false,
}: {
  data?: Positioning | null;
  theme: Theme;
  symbol: string;
  spot: number;
  latestOnly?: boolean;
}) {
  const [measure, setMeasure] = useState<PositioningMeasure>('oi');
  const [side, setSide] = useState<PositioningSide>('combined');
  const [allStrikes, setAllStrikes] = useState(false);
  const rows = useMemo(
    () => (data?.strikes ?? []).slice().sort((a, b) => a.strike - b.strike),
    [data],
  );
  const values = useMemo(
    () => rows.map((row) => ({ row, value: positioningValue(row, measure, side) })),
    [rows, measure, side],
  );
  const observed = values.filter(({ value }) => finite(value)).length;
  const range = useMemo<[number, number] | undefined>(() => {
    if (!rows.length) return undefined;
    const low = rows[0]!.strike,
      high = rows.at(-1)!.strike;
    if (low === high) return [low - 5, high + 5];
    const focused: [number, number] = [Math.max(low, spot * 0.99), Math.min(high, spot * 1.01)];
    return allStrikes || !finite(spot) || spot <= 0 || focused[0] >= focused[1]
      ? [low, high]
      : focused;
  }, [rows, spot, allStrikes]);
  const build = useCallback(
    (chart: IChartApiBase<number>): ChartBuildResult => {
      chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.12, bottom: 0.02 } });
      const series = chart.addSeries(HistogramSeries, {
        color: colors[side],
        base: 0,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: {
          type: 'custom',
          formatter: (value: number) => `$${number(value, 2)}M`,
          minMove: 0.001,
        },
      });
      const lattice = strikeLattice(rows.map((row) => row.strike));
      const byStrike = new Map(values.map((item) => [item.row.strike, item.value]));
      let cleanup: (() => void) | undefined;
      const resultSeries: ChartBuildResult['series'] = [
        { series, name: `${sides[side]} · ${measures[measure]}`, color: colors[side] },
      ];
      if (lattice) {
        series.setData(
          lattice.map((time) => {
            const value = byStrike.get(time);
            return finite(value) ? { time, value: value / 1e6 } : { time };
          }),
        );
      } else {
        const domain = numericGrid(rows[0]?.strike ?? 0, rows.at(-1)?.strike ?? 1, 401);
        const scaffold = chart.addSeries(LineSeries, {
          color: 'transparent',
          lineVisible: false,
          pointMarkersVisible: false,
          crosshairMarkerVisible: false,
          priceLineVisible: false,
          lastValueVisible: false,
          autoscaleInfoProvider: () => ({
            priceRange: {
              minValue: 0,
              maxValue: Math.max(0.001, ...values.map(({ value }) => (value ?? 0) / 1e6)),
            },
          }),
        });
        scaffold.setData(domain.map((time) => ({ time, value: 0 })));
        const primitive = new VerticalProfilePrimitive(
          values.flatMap(({ row, value }) =>
            finite(value) ? [{ strike: row.strike, call: value / 1e6, put: 0 }] : [],
          ),
          'call',
          domain,
          colors[side],
        );
        scaffold.attachPrimitive(primitive);
        resultSeries.push({
          series: scaffold,
          name: 'Strike coordinates',
          color: colors[side],
          hidden: true,
        });
        cleanup = () => scaffold.detachPrimitive(primitive);
      }
      return {
        series: resultSeries,
        cleanup,
        tooltip: (event) => {
          if (!finite(event.time) || !rows.length) return [];
          const strike = event.time;
          const row = rows.reduce((nearest, item) =>
            Math.abs(item.strike - strike) < Math.abs(nearest.strike - strike) ? item : nearest,
          );
          return [
            `Strike ${number(row.strike)}`,
            `${measures[measure]} · ${sides[side]}: ${amount(positioningValue(row, measure, side))}`,
            'Unsigned · dealer direction unknown',
          ];
        },
      };
    },
    [rows, values, side, measure],
  );

  return (
    <Panel
      title="OI concentration & recent activity"
      eyebrow={
        latestOnly
          ? 'Latest snapshot · independent of replay cursor'
          : 'Observed positioning inputs'
      }
      action={<Badge>Dealer direction unknown</Badge>}
      className="positioning-panel"
    >
      <p className="source-note">
        Activity is traded volume, not dealer inventory. These research measures do not change
        scenario targets.
      </p>
      {!data || data.status === 'unavailable' || !rows.length ? (
        <Empty title="Positioning observations unavailable">
          This snapshot does not contain enough stored observations for the concentration and
          activity view.
        </Empty>
      ) : (
        <>
          <div className="positioning-meta">
            <span>
              {symbol} · As of {data.as_of}
            </span>
            <Badge tone={data.status === 'partial' ? 'warning' : 'neutral'}>
              {data.status === 'partial' ? 'Partial observations' : 'Observations available'}
            </Badge>
          </div>
          <div className="positioning-controls">
            <Field label="Positioning measure">
              <select
                value={measure}
                onChange={(event) => setMeasure(event.target.value as PositioningMeasure)}
              >
                <option value="oi">OI concentration</option>
                <option value="5m">Recent activity · 5 minutes</option>
                <option value="15m">Recent activity · 15 minutes</option>
              </select>
            </Field>
            <Field label="Option side">
              <select
                value={side}
                onChange={(event) => setSide(event.target.value as PositioningSide)}
              >
                <option value="combined">Combined calls + puts</option>
                <option value="call">Calls</option>
                <option value="put">Puts</option>
              </select>
            </Field>
            <button onClick={() => setAllStrikes((current) => !current)}>
              {allStrikes ? 'Focus near spot' : 'Show all strikes'}
            </button>
          </div>
          <p className="source-note">
            {measure === 'oi'
              ? 'Gamma-weighted open interest; both sides are unsigned.'
              : `${observed} of ${rows.length} strikes have ${measure} activity observations. Missing values remain unknown; measured zero means no observed activity.`}
          </p>
          <div className="positioning-content">
            <div>
              {observed ? (
                <LightweightChart
                  label="Unsigned OI concentration and recent activity by strike"
                  theme={theme}
                  height={340}
                  axis="number"
                  build={build}
                  revision={`${symbol}:${measure}:${side}:${allStrikes}`}
                  range={range}
                  rangeKey={`${symbol}:${measure}:${side}:${allStrikes}`}
                  xTitle="Strike"
                  yTitle="Gamma-weighted amount · $M per 1% move"
                />
              ) : (
                <Empty title="Activity window unavailable">
                  More valid volume observations are needed for this window and option side.
                </Empty>
              )}
            </div>
            <div className="positioning-levels">
              <h3>Persistent concentration levels</h3>
              <p className="source-note">
                Ranked from OI concentration and its observed persistence, not a directional
                forecast.
              </p>
              {data.top_levels.length ? (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Strike</th>
                        <th>Gross OI</th>
                        <th>5m activity</th>
                        <th>15m activity</th>
                        <th>OI persistence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {persistentPositioningLevels(data).map((row) => (
                        <tr key={row.strike}>
                          <td>{number(row.strike)}</td>
                          <td>{amount(row.gross_oi_gex)}</td>
                          <td>{amount(row.activity_5m)}</td>
                          <td>{amount(row.activity_15m)}</td>
                          <td>
                            {row.oi_persistence === null ? 'Unknown' : percent(row.oi_persistence)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="source-note">No persistent levels have been identified yet.</p>
              )}
            </div>
          </div>
        </>
      )}
      {data && (
        <details className="positioning-coverage">
          <summary>Observation coverage & limitations</summary>
          <p>
            {number(data.coverage.observed_minutes, 1)} minutes observed ·{' '}
            {data.coverage.valid_activity_contracts} / {data.coverage.contracts} contracts with
            valid 15m activity · longest gap{' '}
            {data.coverage.max_gap_seconds === null
              ? 'unknown'
              : `${number(data.coverage.max_gap_seconds, 0)}s`}
            .
          </p>
          <p>
            Volume may open or close positions and can turn over the same contracts. OI persistence
            measures repeated concentration, not confidence in dealer direction.
          </p>
          {!!data.warnings.length && (
            <ul>
              {data.warnings.map((warning, index) => (
                <li key={`${index}:${warning}`}>{warning}</li>
              ))}
            </ul>
          )}
        </details>
      )}
    </Panel>
  );
}
