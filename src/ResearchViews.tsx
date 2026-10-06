import { useEffect, useId, useMemo, useState } from 'react';
import { Badge, Empty, Field, Metric, Notice, Panel, RequestState, Toggle } from './components';
import { COLORS, CompassChart, metricLabels, TraceCharts } from './charts';
import { PositioningPanel } from './PositioningPanel';
import { CategoryChart } from './BasicCharts';
import { ComponentCards, ComponentsTable } from './Cockpit';
import { useRemote } from './hooks';
import { aggregateStrikes, heatmapGrid, millions, number, percent, pretty } from './models';
import type { Dashboard, Theme, TraceMetric } from './types';
import { matrixRows, matrixState, traceStability } from './parityModels';

export function Regime({
  theme,
  revision,
  interval,
}: {
  theme: Theme;
  revision: number;
  interval: number;
}) {
  const remote = useRemote('get_market_overview', [], { revision, interval });
  const data = remote.data;
  const compasses = [
    { name: 'Traders', data: data?.compass_traders ?? data?.compass },
    { name: 'Index basket', data: data?.index_basket ?? data?.compass_whale },
  ];
  return (
    <div className="view-stack">
      <RequestState {...remote} hasData={!!data} />
      <div className="two-col">
        {compasses.map((item) => (
          <Panel
            key={item.name}
            title={item.name}
            eyebrow="Regime compass"
            action={<Badge>{pretty(item.data?.label)}</Badge>}
          >
            <CompassChart compass={item.data} theme={theme} label={item.name} />
            <p className="panel-padding muted">
              {item.data?.strategy || 'Waiting for a valid composite.'}
            </p>
            <p className="source-note">
              Data quality {percent(item.data?.data_quality?.score ?? item.data?.confidence)} ·{' '}
              {item.data?.composition || 'Quality-weighted market observations'}
            </p>
            {!!item.data?.warnings?.length && (
              <p className="source-note warning-text">{item.data.warnings.join(' · ')}</p>
            )}
          </Panel>
        ))}
      </div>
      <Panel title="Effective gamma by instrument" eyebrow="Component contributions">
        {data?.tilt?.length ? (
          <CategoryChart
            theme={theme}
            label="Effective gamma exposure across instruments"
            rows={data.tilt.map((row) => ({
              name: row.symbol,
              value: row.net_gex / 1e6,
              color: row.net_gex >= 0 ? COLORS.positive : COLORS.negative,
            }))}
            yTitle="Effective gamma · $M"
          />
        ) : (
          <Empty>No component tilt is available.</Empty>
        )}
      </Panel>
      <Panel title="Instrument detail" eyebrow="Source observations">
        <ComponentCards components={data?.components ?? []} />
        <ComponentsTable components={data?.components ?? []} />
      </Panel>
    </div>
  );
}

export function Trace({
  symbol,
  theme,
  revision,
  interval,
  active = true,
}: {
  symbol: string;
  theme: Theme;
  revision: number;
  interval: number;
  active?: boolean;
}) {
  const [date, setDate] = useState(''),
    [metric, setMetric] = useState<TraceMetric>('net_gex'),
    [overlays, setOverlays] = useState(false),
    [cursor, setCursor] = useState(-1),
    [width, setWidth] = useState(60),
    [playing, setPlaying] = useState(false),
    [showCandles, setShowCandles] = useState(true),
    [showSpot, setShowSpot] = useState(true),
    [profileMode, setProfileMode] = useState<'latest' | 'cursor'>('latest'),
    [resetRevision, setResetRevision] = useState(0);
  const dates = useRemote('get_trace_dates', [symbol, 30], {
    enabled: active && !!symbol,
    revision,
  });
  const remote = useRemote('get_trace_data', [symbol, 390, date || null], {
    enabled: active && !!symbol,
    revision,
    interval: date ? 0 : interval,
  });
  const data = remote.data;
  const grid = useMemo(() => heatmapGrid(data?.heatmap ?? [], metric), [data?.heatmap, metric]);
  const current =
    cursor < 0
      ? Math.max(0, grid.times.length - 1)
      : Math.min(cursor, Math.max(0, grid.times.length - 1));
  useEffect(() => {
    setDate('');
    setCursor(-1);
    setPlaying(false);
  }, [symbol]);
  useEffect(() => {
    setCursor(-1);
    setPlaying(false);
  }, [date]);
  useEffect(() => {
    if (!active) return;
    if (!playing) return;
    const timer = setInterval(() => {
      setCursor((previous) => {
        const next = (previous < 0 ? 0 : previous) + 1;
        if (next >= grid.times.length - 1) {
          setPlaying(false);
          return Math.max(0, grid.times.length - 1);
        }
        return next;
      });
    }, 500);
    return () => clearInterval(timer);
  }, [playing, grid.times.length, active]);
  return (
    <div className="view-stack">
      <Panel
        title="Session replay"
        eyebrow="TRACE"
        action={<Badge tone="accent">{symbol || 'No instrument'}</Badge>}
      >
        <div className="toolbar">
          <Field label="TRACE session">
            <select value={date} onChange={(event) => setDate(event.target.value)}>
              <option value="">Latest session</option>
              {dates.data?.map((item) => (
                <option value={item} key={item}>
                  {item}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Exposure metric">
            <select
              value={metric}
              onChange={(event) => setMetric(event.target.value as TraceMetric)}
            >
              {Object.entries(metricLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Replay window">
            <select value={width} onChange={(e) => setWidth(Number(e.target.value))}>
              <option value="30">30 minutes</option>
              <option value="60">60 minutes</option>
              <option value="120">120 minutes</option>
              <option value="480">Full session</option>
              {![30, 60, 120, 480].includes(width) && (
                <option value={width}>{width} minutes · chart zoom</option>
              )}
            </select>
          </Field>
          <Toggle checked={overlays} onChange={setOverlays} label="Decision overlays" />
          <Toggle checked={showCandles} onChange={setShowCandles} label="Price candles" />
          <Toggle checked={showSpot} onChange={setShowSpot} label="Spot path" />
          <Field label="Strike profile">
            <select
              value={profileMode}
              onChange={(event) => setProfileMode(event.target.value as typeof profileMode)}
            >
              <option value="latest">Latest snapshot · original</option>
              <option value="cursor">At replay cursor</option>
            </select>
          </Field>
          <button
            onClick={() => {
              setCursor(-1);
              setWidth(480);
              setPlaying(false);
              setResetRevision((value) => value + 1);
            }}
          >
            Fit session
          </button>
        </div>
        <RequestState {...remote} hasData={!!data} />
        {dates.error && <Notice tone="warning">Session list: {dates.error}</Notice>}
        {data && (
          <>
            <div className="trace-meta">
              <Metric label="Session" value={data.session_date} />
              <Metric label="Spot" value={number(data.spot_price)} />
              <Metric label="Net GEX" value={millions(data.total_net_gex)} />
              <Metric
                label="Stability"
                value={traceStability(data) === null ? '—' : `${traceStability(data)}%`}
                detail="absolute net / gross strike GEX"
              />
              <Metric label="Heatmap cells" value={number(data.heatmap.length, 0)} />
              <Metric label="Updated" value={data.timestamp.slice(11, 19)} />
              <Metric
                label="Observations / strikes"
                value={`${grid.times.length} / ${grid.strikes.length}`}
              />
              <Metric
                label="Modeled charm coverage"
                value={percent(data.modeled_pressure_coverage?.ratio)}
                detail={
                  data.modeled_pressure_coverage
                    ? `${data.modeled_pressure_coverage.matched_contracts ?? 0} / ${data.modeled_pressure_coverage.total_contracts ?? 0} contracts`
                    : 'No modeled coverage reported'
                }
              />
            </div>
            {metric.startsWith('modeled_') && (
              <Notice tone={data.modeled_pressure_coverage?.warning ? 'warning' : 'info'}>
                {data.modeled_pressure_coverage?.warning ||
                  'Modeled pressure reflects option sensitivities, not actual dealer flow.'}
              </Notice>
            )}
            <PositioningPanel
              data={data.positioning}
              theme={theme}
              symbol={data.symbol}
              spot={data.spot_price}
              latestOnly
            />
            <p className="source-note">
              Signed GEX views use an OI-based positioning proxy with a call-positive / put-negative
              assumption. Dealer direction is unknown.
            </p>
            {grid.times.length ? (
              <>
                <TraceCharts
                  data={data}
                  metric={metric}
                  theme={theme}
                  overlays={overlays}
                  cursor={cursor}
                  width={width}
                  showCandles={showCandles}
                  showSpot={showSpot}
                  profileMode={profileMode}
                  resetRevision={resetRevision}
                  onViewportChange={(nextCursor, nextWidth) => {
                    setPlaying(false);
                    setCursor(nextCursor);
                    setWidth(nextWidth);
                  }}
                />
                <div className="scrubber">
                  <span className="parity-timeline-bound">{grid.times[0]?.slice(11, 16)}</span>
                  <button
                    aria-label={playing ? 'Pause replay' : 'Play replay'}
                    onClick={() => {
                      if (!playing && current >= grid.times.length - 1) setCursor(0);
                      setPlaying(!playing);
                    }}
                  >
                    {playing ? 'Ⅱ Pause' : '▶ Replay'}
                  </button>
                  <input
                    aria-label="Replay cursor"
                    type="range"
                    min={0}
                    max={Math.max(0, grid.times.length - 1)}
                    value={current}
                    onChange={(event) => {
                      setPlaying(false);
                      setCursor(Number(event.target.value));
                    }}
                  />
                  <output>{grid.times[current]?.slice(11, 16) || '—'}</output>
                  <span className="parity-timeline-bound">{grid.times.at(-1)?.slice(11, 16)}</span>
                  <button
                    onClick={() => {
                      setCursor(-1);
                      setPlaying(false);
                    }}
                  >
                    Latest
                  </button>
                </div>
              </>
            ) : (
              <Empty title="No intraday cells">
                This session has no option profile data available for replay.
              </Empty>
            )}
          </>
        )}
      </Panel>
    </div>
  );
}

export function StrikeMatrix({ dashboard }: { dashboard: Dashboard | undefined }) {
  const matrixId = useId();
  const [query, setQuery] = useState(''),
    [sort, setSort] = useState<'strike' | 'net' | 'oi'>('strike'),
    [showAll, setShowAll] = useState(false);
  const allRows = useMemo(() => aggregateStrikes(dashboard?.profile ?? []), [dashboard?.profile]);
  const maxNet = Math.max(...allRows.map((row) => Math.abs(row.net)), 0),
    maxSide = Math.max(...allRows.flatMap((row) => [Math.abs(row.call), Math.abs(row.put)]), 1);
  const rows = useMemo(() => {
    const result = matrixRows(allRows, showAll).filter(
      (row) => !query || String(row.strike).includes(query),
    );
    return result.sort((a, b) =>
      sort === 'net'
        ? Math.abs(b.net) - Math.abs(a.net)
        : sort === 'oi'
          ? b.oi - a.oi
          : a.strike - b.strike,
    );
  }, [allRows, query, sort, showAll]);
  const exportRows = () => {
    const csv = [
      'Strike,Net Gamma,Call Gamma,Put Gamma,Call OI,Put OI',
      ...rows.map((row) =>
        [row.strike, row.net, row.call, row.put, row.callOI, row.putOI].join(','),
      ),
    ].join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `PublicGex-${dashboard?.snapshot.symbol || 'profile'}-strikes.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <Panel
      title="Strike Matrix"
      eyebrow="One snapshot · all strikes"
      action={
        <div className="row-actions">
          <button
            disabled={!rows.length}
            onClick={() => {
              const spot = dashboard?.snapshot.spot_price;
              if (spot === undefined) return;
              const nearest = rows.reduce(
                (best, row) =>
                  Math.abs(row.strike - spot) < Math.abs(best.strike - spot) ? row : best,
                rows[0]!,
              );
              document
                .getElementById(`${matrixId}-strike-${nearest.strike}`)
                ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }}
          >
            Jump to spot
          </button>
          <button disabled={!rows.length} onClick={exportRows}>
            Export CSV ↓
          </button>
        </div>
      }
    >
      <div className="toolbar">
        <Field label="Filter strike">
          <input
            type="search"
            placeholder="Search strike…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </Field>
        <Field label="Sort strikes">
          <select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)}>
            <option value="strike">Strike ascending</option>
            <option value="net">Largest gamma exposure</option>
            <option value="oi">Largest open interest</option>
          </select>
        </Field>
        <span className="muted">
          {rows.length} strikes · spot {number(dashboard?.snapshot.spot_price)}
        </span>
        <Toggle checked={showAll} onChange={setShowAll} label="Include low-activity strikes" />
      </div>
      <div className="table-scroll tall">
        <table>
          <thead>
            <tr>
              <th>Strike</th>
              <th>OI proxy sign</th>
              <th>Net gamma</th>
              <th>Call gamma</th>
              <th>Put gamma</th>
              <th>Call OI</th>
              <th>Put OI</th>
              <th>Total OI</th>
              <th>Positioning</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                id={`${matrixId}-strike-${row.strike}`}
                key={row.strike}
                className={
                  dashboard &&
                  Math.abs(row.strike - dashboard.snapshot.spot_price) <
                    dashboard.snapshot.spot_price * 0.001
                    ? 'near-spot'
                    : ''
                }
              >
                <td className="instrument">
                  {number(row.strike)}
                  <span className="parity-state-detail">
                    {dashboard &&
                    Math.abs(row.strike - dashboard.snapshot.spot_price) /
                      dashboard.snapshot.spot_price <
                      0.001
                      ? 'ATM'
                      : dashboard && row.strike < dashboard.snapshot.spot_price
                        ? 'ITM'
                        : 'OTM'}
                  </span>
                </td>
                <td>
                  <Badge
                    tone={
                      matrixState(row, maxNet) === 'MAGNET'
                        ? 'accent'
                        : row.net > 0
                          ? 'positive'
                          : 'warning'
                    }
                  >
                    {matrixState(row, maxNet)}
                  </Badge>
                  <span className="parity-state-detail">
                    {row.net > 0
                      ? 'Positive OI proxy'
                      : row.net < 0
                        ? 'Negative OI proxy'
                        : 'Zero OI proxy'}
                  </span>
                </td>
                <td className={row.net >= 0 ? 'positive-text' : 'negative-text'}>
                  {millions(row.net)}
                </td>
                <td>
                  {millions(row.call)}
                  <span className="parity-side-bar">
                    <i
                      style={{
                        width: `${(Math.abs(row.call) / maxSide) * 100}%`,
                        background: COLORS.positive,
                      }}
                    />
                  </span>
                </td>
                <td>
                  {millions(row.put)}
                  <span className="parity-side-bar">
                    <i
                      style={{
                        width: `${(Math.abs(row.put) / maxSide) * 100}%`,
                        background: COLORS.negative,
                      }}
                    />
                  </span>
                </td>
                <td>{number(row.callOI, 0)}</td>
                <td>{number(row.putOI, 0)}</td>
                <td>{number(row.oi, 0)}</td>
                <td>
                  <div className="exposure-bar">
                    <i
                      style={{
                        width: `${Math.min(100, (Math.abs(row.net) / Math.max(maxNet, 1)) * 100)}%`,
                        background: row.net >= 0 ? COLORS.positive : COLORS.negative,
                      }}
                    />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!rows.length && (
        <Empty title="No matching strikes">
          Load an instrument with option data, or clear the strike filter.
        </Empty>
      )}
      <p className="source-note">
        Exposure in dollars per 1% underlying move. Highlighted rows are within 0.1% of spot. The
        original activity filter hides a strike only when total OI is below 10 and absolute net GEX
        is below $100,000. MAGNET identifies the largest absolute net exposure in the complete
        profile.
      </p>
    </Panel>
  );
}
