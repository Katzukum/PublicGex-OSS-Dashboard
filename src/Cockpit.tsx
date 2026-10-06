import { useState } from 'react';
import { Badge, Empty, Metric, Notice, Panel, Toggle } from './components';
import { ProfileChart, SweepChart, TrendChart } from './charts';
import { finite, millions, money, number, percent, pretty } from './models';
import type { Candidate, Component, Theme, Workspace } from './types';
import { cockpitMetrics, zeroGammaLevels } from './parityModels';
import './parity.css';
import { PositioningPanel } from './PositioningPanel';

function ageLabel(age: number | null | undefined) {
  return finite(age)
    ? age < 60
      ? `${number(age, 0)}s old`
      : age < 3600
        ? `${number(age / 60, 0)}m old`
        : `${number(age / 3600, 1)}h old`
    : 'Age unavailable';
}

export function ComponentCards({ components }: { components: Component[] }) {
  return (
    <div className="parity-pressure-grid">
      {components.map((item) => {
        const score = item.data_quality?.score ?? item.confidence;
        const pressure = item.distance_pct ?? 0;
        const width = Math.min((Math.abs(pressure) / 3) * 50, 50);
        return (
          <article className="parity-pressure-card" key={item.symbol}>
            <div className="split">
              <h3>{item.symbol}</h3>
              <Badge tone={finite(score) && score >= 0.65 ? 'positive' : 'warning'}>
                Quality {percent(score)}
              </Badge>
            </div>
            <div className="mini-metrics">
              <Metric
                label="Flip distance"
                value={finite(item.distance_pct) ? `${number(item.distance_pct, 1)}%` : '—'}
                tone={pressure >= 0 ? 'positive-text' : 'negative-text'}
              />
              <Metric label="Acceleration" value={`${millions(item.acceleration)} / pt`} />
            </div>
            <div
              className="parity-pressure-track"
              aria-label={`${item.symbol} flip distance ${number(item.distance_pct, 1)} percent`}
            >
              <i
                style={
                  pressure >= 0
                    ? { left: '50%', width: `${width}%`, background: 'var(--green)' }
                    : { right: '50%', width: `${width}%`, background: 'var(--red)' }
                }
              />
            </div>
            <div className="parity-pressure-scale">
              <span>−3%</span>
              <span>0</span>
              <span>+3%</span>
            </div>
            <p className="source-note">
              Spot {number(item.spot)} · Flip {number(item.flip_strike)}
              <br />
              {ageLabel(item.age_seconds ?? item.data_quality?.age_seconds)} ·{' '}
              {pretty(item.flip_quality)} flip
              <br />
              Gamma imbalance {percent(item.gex_imbalance)}
            </p>
            <p className="source-note warning-text">
              {[
                ...new Set([...(item.warnings ?? []), ...(item.data_quality?.warnings ?? [])]),
              ].join(' · ') || 'No source warnings'}
            </p>
          </article>
        );
      })}
    </div>
  );
}

export function ComponentsTable({ components }: { components: Component[] }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Instrument</th>
            <th>Spot</th>
            <th>Flip</th>
            <th>Flip distance</th>
            <th>Gamma imbalance</th>
            <th>Data quality</th>
            <th>Age</th>
            <th>Flip quality</th>
            <th>Acceleration</th>
            <th>Warnings</th>
          </tr>
        </thead>
        <tbody>
          {components.map((item) => (
            <tr key={item.symbol}>
              <td className="instrument">{item.symbol}</td>
              <td>{number(item.spot)}</td>
              <td>{number(item.flip_strike)}</td>
              <td className={(item.distance_pct ?? 0) >= 0 ? 'positive-text' : 'negative-text'}>
                {number(item.distance_pct, 2)}%
              </td>
              <td>{percent(item.gex_imbalance)}</td>
              <td>
                <span title={(item.warnings ?? item.data_quality?.warnings ?? []).join('; ')}>
                  {percent(item.data_quality?.score ?? item.confidence)}
                </span>
              </td>
              <td>{ageLabel(item.age_seconds ?? item.data_quality?.age_seconds)}</td>
              <td>{pretty(item.flip_quality)}</td>
              <td>{millions(item.acceleration)} / pt</td>
              <td>
                {[
                  ...new Set([...(item.warnings ?? []), ...(item.data_quality?.warnings ?? [])]),
                ].join(' · ') || 'Clean'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!components.length && <Empty>No cross-asset observations are available.</Empty>}
    </div>
  );
}
function CandidateCard({ name, idea }: { name: string; idea: Candidate | undefined }) {
  const ready = idea?.status === 'EXECUTABLE';
  return (
    <div className="candidate">
      <div className="split">
        <h3>{name}</h3>
        <Badge tone={ready ? 'positive' : 'warning'}>{pretty(idea?.status)}</Badge>
      </div>
      <p className="candidate-strikes">
        {idea?.side && <span>{idea.side} · </span>}
        {name === 'Butterfly'
          ? [idea?.lower, idea?.center, idea?.upper]
              .map((n) => number(n, Number.isInteger(n) ? 0 : 2))
              .join(' / ')
          : [idea?.long_strike, idea?.short_strike]
              .map((n) => number(n, Number.isInteger(n) ? 0 : 2))
              .join(' / ')}
      </p>
      <div className="mini-metrics">
        <Metric label="Modeled debit" value={number(idea?.estimated_debit)} detail="points" />
        <Metric label="Max loss" value={money(idea?.max_loss_dollars)} />
        <Metric label="Max reward" value={money(idea?.max_reward_dollars)} />
      </div>
      <dl className="definition-list">
        <div>
          <dt>Modeled maximum reward</dt>
          <dd>{number(idea?.max_profit)} pts</dd>
        </div>
        <div>
          <dt>Modeled debit risk</dt>
          <dd>{money(idea?.estimated_debit_dollars)}</dd>
        </div>
        {name === 'Butterfly' ? (
          <div>
            <dt>Breakeven tent</dt>
            <dd>
              {number(idea?.lower_breakeven)} – {number(idea?.upper_breakeven)}
            </dd>
          </div>
        ) : (
          <>
            <div>
              <dt>Breakeven</dt>
              <dd>{number(idea?.breakeven)}</dd>
            </div>
            <div>
              <dt>Target</dt>
              <dd>{number(idea?.target)}</dd>
            </div>
          </>
        )}
      </dl>
      {idea?.quote && (
        <dl className="definition-list">
          <div>
            <dt>Bid / mid / ask</dt>
            <dd>
              {[idea.quote.bid, idea.quote.mid, idea.quote.ask].map((n) => number(n)).join(' / ')}
            </dd>
          </div>
          <div>
            <dt>Quote age / liquidity</dt>
            <dd>
              {finite(idea.quote.age_seconds) ? `${number(idea.quote.age_seconds, 0)}s` : 'Unknown'}{' '}
              · {idea.liquidity_grade || 'Ungraded'}
            </dd>
          </div>
          <div>
            <dt>Settlement / close</dt>
            <dd>
              {idea.settlement_type || 'Unknown'} · {idea.time_to_close || 'Unknown'}
            </dd>
          </div>
          {ready && (
            <div>
              <dt>Risk-based contracts</dt>
              <dd>{idea.contracts ?? '—'}</dd>
            </div>
          )}
        </dl>
      )}
      <p className="muted">
        {idea?.reason || idea?.rationale || 'No quoted candidate is available for this snapshot.'}
      </p>
    </div>
  );
}
export default function Cockpit({
  workspace,
  theme,
  onJournal,
}: {
  workspace: Workspace | undefined;
  theme: Theme;
  onJournal: () => void;
}) {
  const [sweep, setSweep] = useState(true);
  if (!workspace)
    return (
      <Panel>
        <Empty title="Your market workspace is ready">
          Start collection in Settings to capture your first snapshot, or select an instrument with
          saved data.
        </Empty>
      </Panel>
    );
  const {
    dashboard,
    decision,
    data_quality: quality,
    active_scenario: scenario,
    market_context: context,
  } = workspace;
  const snap = dashboard.snapshot;
  const bias = decision?.bias || 'WAIT';
  const direction = bias === 'CALL' ? 'positive' : bias === 'PUT' ? 'negative' : 'warning';
  const metrics = cockpitMetrics(dashboard);
  const zeroLevels = zeroGammaLevels(dashboard.gamma_sweep, decision?.flip ?? snap.flip_strike);
  const basket = workspace.regime?.index_basket;
  const marketScore = decision?.market_vote?.score ?? 0,
    dealerScore = decision?.dealer_vote?.score ?? 0,
    liquidityScore = decision?.liquidity_vote?.score ?? 0;
  const signalCards = [
    {
      title: 'Market Signal',
      label: marketScore > 0.2 ? 'CALL' : marketScore < -0.2 ? 'PUT' : 'WAIT',
      badge: marketScore > 0.2 ? 'Supportive' : marketScore < -0.2 ? 'Active' : 'Neutral',
      detail: decision?.market_vote?.detail,
      score: marketScore,
    },
    {
      title: 'OI positioning proxy',
      label:
        dealerScore > 0.2
          ? 'Positive proxy'
          : dealerScore < -0.2
            ? 'Negative proxy'
            : 'Mixed proxy',
      badge:
        Math.abs(dealerScore) > 0.55
          ? 'High Risk'
          : Math.abs(dealerScore) > 0.2
            ? 'Active'
            : 'Neutral',
      detail: 'Call-positive / put-negative assumption; dealer direction is unknown.',
      score: dealerScore,
    },
    {
      title: 'Gamma Liquidity',
      label:
        liquidityScore > 0.2 ? 'Upside Open' : liquidityScore < -0.2 ? 'Downside Open' : 'Balanced',
      badge:
        Math.abs(liquidityScore) > 0.45
          ? 'Fragile'
          : Math.abs(liquidityScore) > 0.2
            ? 'Active'
            : 'Neutral',
      detail: decision?.liquidity_vote?.detail,
      score: liquidityScore,
    },
    {
      title: 'Index Gamma Basket',
      label: pretty(basket?.label),
      badge: `Quality ${percent(basket?.data_quality?.score ?? basket?.confidence)}`,
      detail: basket?.strategy || 'No index-basket composite.',
      score: basket?.y_score ?? 0,
    },
  ];
  return (
    <div className="view-stack">
      <div className="metrics-strip">
        <Metric label="Underlying spot" value={number(snap.spot_price)} detail={snap.symbol} />
        <Metric
          label="Net OI gamma proxy"
          value={millions(snap.total_net_gex)}
          detail="per 1% underlying move"
          tone={snap.total_net_gex >= 0 ? 'positive-text' : 'negative-text'}
        />
        <Metric
          label="Gamma flip"
          value={number(decision?.flip ?? snap.flip_strike)}
          detail="estimated level"
        />
        <Metric
          label="Data quality"
          value={percent(quality?.score)}
          detail={`${pretty(quality?.label)} · not win probability`}
        />
      </div>
      <div className="parity-signal-grid">
        {signalCards.map((card) => (
          <article className="panel parity-signal-card" key={card.title}>
            <div className="split">
              <span>{card.title}</span>
              <Badge>{card.badge}</Badge>
            </div>
            <strong
              className={
                card.score > 0.2
                  ? 'positive-text'
                  : card.score < -0.2
                    ? 'negative-text'
                    : 'warning-text'
              }
            >
              {card.label}
            </strong>
            <p>{card.detail || 'No observation available.'}</p>
          </article>
        ))}
      </div>
      <PositioningPanel
        data={dashboard.positioning}
        theme={theme}
        symbol={snap.symbol}
        spot={snap.spot_price}
      />
      <div className="cockpit-grid">
        <div className="cockpit-landscape-column">
          <Panel
            title="OI-based positioning proxy"
            eyebrow="Call-positive / put-negative assumption"
            action={
              <Badge tone="accent">
                {snap.symbol} · {snap.expiration_date || '0DTE'}
              </Badge>
            }
          >
            <ProfileChart
              dashboard={dashboard}
              theme={theme}
              target={decision?.target}
              invalidation={decision?.invalidation}
            />
            <div className="chart-foot">
              <span>
                <i className="dot positive-dot" />
                Call gamma
              </span>
              <span>
                <i className="dot negative-dot" />
                Put gamma
              </span>
              <span>
                <i className="dot accent-dot" />
                Net gamma by strike
              </span>
              <span>Scroll to zoom · chart controls restore focus</span>
            </div>
            <div className="parity-metric-grid">
              <Metric label="Total Call GEX" value={millions(metrics.call)} />
              <Metric label="Total Put GEX" value={millions(metrics.put)} />
              <Metric label="Net GEX" value={millions(snap.total_net_gex)} />
              <Metric
                label="Gamma Exposure proxy"
                value={metrics.local >= 0 ? 'Positive' : 'Negative'}
                detail="within 2% of spot"
              />
              <Metric
                label="GEX Change"
                value={millions(metrics.change)}
                detail="first to last saved history point"
              />
              <Metric
                label="Zero Gamma"
                value={
                  zeroLevels.length
                    ? zeroLevels
                        .map(
                          (item) =>
                            `${item.label.includes('below') ? 'B ' : item.label.includes('above') ? 'A ' : ''}${number(item.value)}`,
                        )
                        .join(' / ')
                    : '—'
                }
              />
              <Metric label="Gamma Slope" value={`${millions(snap.gex_slope)} / pt`} />
            </div>
          </Panel>
          <Panel
            title="Gamma sweep"
            eyebrow="Sensitivity analysis"
            action={<Toggle checked={sweep} onChange={setSweep} label="Show modeled sweep" />}
          >
            {sweep ? (
              <>
                <SweepChart dashboard={dashboard} theme={theme} />
                <p className="source-note">
                  A modeled repricing of the existing chain across hypothetical spot prices.
                </p>
              </>
            ) : (
              <p className="muted panel-padding">
                Explore how gamma exposure changes as the underlying price moves.
              </p>
            )}
          </Panel>
        </div>
        <Panel
          title="Current scenario"
          eyebrow="Snapshot-consistent decision"
          className="scenario-panel"
        >
          <div className="scenario-label">
            <Badge tone={direction}>{bias === 'WAIT' ? 'WAIT' : `${bias} BIAS`}</Badge>
            <span className="muted">{pretty(scenario?.scenario_type)}</span>
          </div>
          <p className="scenario-copy">{decision?.context || 'Waiting for eligible conditions.'}</p>
          <div className="scenario-levels">
            <Metric
              label="Bias score"
              value={percent(decision?.score)}
              detail="directional score, not probability"
            />
            <Metric label="Target" value={number(decision?.target)} />
            <Metric label="Invalidation" value={number(decision?.invalidation)} />
          </div>
          <div className="votes">
            {[
              ['Market', decision?.market_vote],
              ['Dealer', decision?.dealer_vote],
              ['Liquidity', decision?.liquidity_vote],
            ].map(([label, vote]) => {
              const item = typeof vote === 'object' ? vote : undefined;
              return (
                <div key={String(label)}>
                  <span>{String(label)}</span>
                  <strong
                    className={
                      (item?.score ?? 0) > 0.2
                        ? 'positive-text'
                        : (item?.score ?? 0) < -0.2
                          ? 'negative-text'
                          : 'muted'
                    }
                  >
                    {(item?.score ?? 0) > 0.2
                      ? 'Supportive'
                      : (item?.score ?? 0) < -0.2
                        ? 'Defensive'
                        : 'Neutral'}
                  </strong>
                  <small>{item?.detail || 'No observation'}</small>
                </div>
              );
            })}
          </div>
          <button className="primary full" onClick={onJournal}>
            Journal this scenario <span>↗</span>
          </button>
          <p className="source-note">Research context only. Candidate prices are read-only.</p>
        </Panel>
      </div>
      {(quality?.warnings?.length ?? 0) > 0 && (
        <Notice tone="warning">Data quality: {quality?.warnings?.join(' · ')}</Notice>
      )}
      <Panel title="Market context" eyebrow="The conditions around the setup">
        <div className="context-grid">
          <Metric label="Event risk" value={pretty(context?.event_risk?.state)} />
          <Metric label="Implied move" value={number(context?.implied_move)} detail="points" />
          <Metric label="Range consumed" value={percent(context?.range_consumed)} />
          <Metric
            label="15m realized volatility"
            value={percent(context?.realized_volatility_15m, 2)}
          />
          <Metric label="Cross-asset state" value={pretty(context?.cross_asset_state)} />
        </div>
        {!!context?.warnings?.length && (
          <p className="source-note">{context.warnings.join(' · ')}</p>
        )}
      </Panel>
      <div className="two-col">
        <Panel title="Historical edge" eyebrow="Independent observed outcomes">
          <div className="horizon-grid">
            {[15, 30, 60].map((horizon) => {
              const item = workspace.historical_edge?.horizons?.find(
                (row) => row.horizon_minutes === horizon,
              );
              return (
                <div className="horizon-card" key={horizon}>
                  <span>{horizon} minutes</span>
                  <strong>{percent(item?.win_rate)}</strong>
                  <small>historical win rate · n={item?.sample_size ?? 0}</small>
                  <dl>
                    <div>
                      <dt>Median move</dt>
                      <dd>{number(item?.median_move_points, 1)}</dd>
                    </div>
                    <div>
                      <dt>MFE / MAE</dt>
                      <dd>
                        {number(item?.median_favorable_points, 1)} /{' '}
                        {number(item?.median_adverse_points, 1)}
                      </dd>
                    </div>
                  </dl>
                </div>
              );
            })}
          </div>
          <p className="source-note">
            Historical evidence is separate from data quality. No outcome is guaranteed.
          </p>
        </Panel>
        <Panel title="Net gamma trend" eyebrow="Recent saved snapshots">
          <TrendChart history={dashboard.history} theme={theme} revision={snap.symbol} />
        </Panel>
      </div>
      <Panel title="Quoted structures" eyebrow="Read-only execution candidates">
        <div className="two-col">
          <CandidateCard name="Butterfly" idea={workspace.execution_candidates?.ideas?.butterfly} />
          <CandidateCard
            name="Debit spread"
            idea={workspace.execution_candidates?.ideas?.debit_spread}
          />
        </div>
      </Panel>
      <Panel title="Market breadth" eyebrow="Cross-asset components">
        <ComponentCards components={workspace.components ?? []} />
        <ComponentsTable components={workspace.components ?? []} />
      </Panel>
    </div>
  );
}
