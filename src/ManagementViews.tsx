import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api';
import { Badge, Empty, Field, Metric, Notice, Panel, RequestState, Toggle } from './components';
import { ProfileChart, SweepChart } from './charts';
import { StrikeMatrix } from './ResearchViews';
import { useAction, useRemote } from './hooks';
import { aggregateStrikes, localDateTime, millions, number } from './models';
import { formatWeights, savedTime, settingsWithWeights } from './managementParity';
import './managementParity.css';
import type { Dashboard, RuntimeInfo, Settings, Theme } from './types';

export function OneOff({
  theme,
  revision,
  demo,
  active = true,
}: {
  symbol: string;
  theme: Theme;
  revision: number;
  demo: boolean;
  active?: boolean;
}) {
  const [ticker, setTicker] = useState('NVDA'),
    [expiration, setExpiration] = useState(localDateTime().slice(0, 10)),
    [selected, setSelected] = useState<number>(),
    [built, setBuilt] = useState<Dashboard>(),
    [sweep, setSweep] = useState(true);
  const saved = useRemote('get_one_off_profiles', [], { enabled: active, revision });
  const profile = useRemote('get_one_off_profile', [selected ?? 0], {
    enabled: active && selected !== undefined,
    revision,
  });
  const action = useAction();
  const data = built ?? profile.data;
  useEffect(() => {
    if (profile.data) setBuilt(profile.data);
  }, [profile.data]);
  const build = async (event: FormEvent) => {
    event.preventDefault();
    await action.run(async () => {
      if (!ticker.trim() || !expiration)
        throw new Error('Enter an instrument and expiration date.');
      const result = await api('build_one_off_profile', [ticker.trim().toUpperCase(), expiration]);
      if (!result.ok || !result.data)
        throw new Error(result.message || 'The profile could not be built.');
      setSelected(undefined);
      setBuilt(result.data);
      setTicker(result.data.snapshot.symbol);
      setExpiration(result.data.snapshot.expiration_date || expiration);
      saved.reload();
    }, 'Profile saved locally.');
  };
  return (
    <div className="view-stack">
      <Panel title="Build a one-off profile" eyebrow="An independent saved option-chain pull">
        <form onSubmit={(event) => void build(event)} className="toolbar">
          <Field label="One-off symbol">
            <input
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
              required
              maxLength={12}
              placeholder="NVDA"
            />
          </Field>
          <Field label="Expiration date">
            <input
              type="date"
              value={expiration}
              onChange={(e) => setExpiration(e.target.value)}
              required
            />
          </Field>
          <button className="primary" type="submit" disabled={action.busy || demo}>
            {action.busy ? 'Building profile…' : 'Build profile'}
          </button>
          <p className="muted">Saved separately from the live 0DTE feed in one_off_gex_data.db.</p>
        </form>
        {demo && (
          <Notice tone="info">
            Demo mode uses the saved synthetic profile below. Live API pulls are disabled.
          </Notice>
        )}
        {action.error && <Notice tone="error">{action.error}</Notice>}
        {action.message && <Notice tone="success">{action.message}</Notice>}
      </Panel>
      <div className="oneoff-layout">
        <Panel title="Saved profiles" eyebrow={`${saved.data?.length ?? 0} local pulls`}>
          <RequestState {...saved} hasData={!!saved.data} />
          <div className="saved-profiles">
            {saved.data?.map((item) => (
              <button
                className={selected === item.snapshot_id ? 'selected' : ''}
                key={item.snapshot_id}
                onClick={() => {
                  setBuilt(undefined);
                  setSelected(item.snapshot_id);
                  setTicker(item.symbol);
                  setExpiration(item.expiration_date);
                }}
              >
                <div>
                  <strong>{item.symbol}</strong>
                  <span>{item.expiration_date}</span>
                </div>
                <span>{millions(item.total_net_gex)}</span>
                <small>{item.contract_count} contracts</small>
                <small>{savedTime(item.timestamp)}</small>
              </button>
            ))}
          </div>
          {!saved.data?.length && !saved.loading && (
            <Empty title="No saved profiles">
              Your independent profile pulls will appear here.
            </Empty>
          )}
        </Panel>
        <Panel
          title={data ? `${data.snapshot.symbol} option profile` : 'Profile preview'}
          eyebrow={data?.snapshot.expiration_date || 'Select a saved profile'}
          action={data && <Badge>{aggregateStrikes(data.profile).length} strikes</Badge>}
        >
          <RequestState {...profile} hasData={!!data} />
          {data ? (
            <>
              <div className="mini-metrics">
                <Metric label="Spot" value={number(data.snapshot.spot_price)} />
                <Metric label="Net gamma" value={millions(data.snapshot.total_net_gex)} />
                <Metric label="Contracts" value={data.profile.length} />
                <Metric label="Expiration" value={data.snapshot.expiration_date || '—'} />
              </div>
              <ProfileChart dashboard={data} theme={theme} />
              <p className="source-note">
                Saved at {savedTime(data.snapshot.timestamp)}. This independent pull does not
                replace the live instrument snapshot.
              </p>
            </>
          ) : (
            <Empty title="Choose your perspective">
              Open a saved profile or build one for a specific expiration.
            </Empty>
          )}
        </Panel>
      </div>
      {data && (
        <>
          <Panel
            title="One-off gamma sweep"
            action={<Toggle checked={sweep} onChange={setSweep} label="Show sweep" />}
          >
            {sweep ? (
              <SweepChart dashboard={data} theme={theme} />
            ) : (
              <p className="panel-padding muted">
                Model this chain at a range of underlying prices.
              </p>
            )}
          </Panel>
          <StrikeMatrix dashboard={data} />
        </>
      )}
    </div>
  );
}

export function SettingsView({
  settings,
  runtime,
  onSaved,
  onRuntime,
  theme,
  onTheme,
}: {
  settings: Settings | undefined;
  runtime: RuntimeInfo | undefined;
  onSaved: () => void;
  onRuntime: () => void;
  theme: Theme;
  onTheme: (theme: Theme) => void;
}) {
  const [draft, setDraft] = useState<Settings | undefined>(settings),
    [symbols, setSymbols] = useState(settings?.symbols.join(', ') || ''),
    [traderWeights, setTraderWeights] = useState(formatWeights(settings?.weights)),
    [basketWeights, setBasketWeights] = useState(
      formatWeights(settings?.weights_index_basket ?? settings?.weights_whale),
    ),
    [port, setPort] = useState(settings?.ninjatrader_port ?? 5010);
  const action = useAction();
  const serviceAction = useAction();
  useEffect(() => {
    if (settings) {
      setDraft(settings);
      setSymbols(settings.symbols.join(', '));
      setTraderWeights(formatWeights(settings.weights));
      setBasketWeights(formatWeights(settings.weights_index_basket ?? settings.weights_whale));
      setPort(settings.ninjatrader_port ?? 5010);
    }
  }, [settings]);
  const update = <K extends keyof Settings>(key: K, value: Settings[K]) =>
    setDraft((previous) => (previous ? { ...previous, [key]: value } : previous));
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!draft) return;
    await action.run(async () => {
      const list = [
        ...new Set(
          symbols
            .split(',')
            .map((s) => s.trim().toUpperCase())
            .filter(Boolean),
        ),
      ];
      if (!list.length) throw new Error('Add at least one instrument.');
      if (draft.min_poll_interval_seconds > draft.max_poll_interval_seconds)
        throw new Error('Minimum polling interval must not exceed the maximum.');
      const maximumRisk = draft.maximum_risk_dollars ?? 500;
      const fees = draft.fees_per_contract ?? 1.25;
      if (!Number.isFinite(maximumRisk) || maximumRisk <= 0)
        throw new Error('Maximum risk must be greater than zero.');
      if (!Number.isFinite(fees) || fees < 0)
        throw new Error('Fees per contract must be zero or greater.');
      const result = await api('save_settings', [
        settingsWithWeights(
          {
            ...draft,
            symbols: list,
            theme,
            maximum_risk_dollars: maximumRisk,
            fees_per_contract: fees,
            ninjatrader_port: port,
          },
          traderWeights,
          basketWeights,
        ),
      ]);
      if (!result.ok) throw new Error(result.message || 'Settings were rejected.');
      onSaved();
    }, 'Settings saved.');
  };
  const toggleService = async (service: 'collector' | 'ninjatrader', running: boolean) => {
    await serviceAction.run(
      async () => {
        if (service === 'collector') {
          const result = await api(running ? 'stop_collector' : 'start_collector', []);
          if (!result.ok)
            throw new Error(result.message || 'The collector could not change state.');
        } else if (running) {
          const result = await api('stop_ninjatrader', []);
          if (!result.ok) throw new Error(result.message || 'The broadcaster could not stop.');
        } else {
          if (!Number.isInteger(port) || port < 1024 || port > 65535)
            throw new Error('Choose a port from 1024 to 65535.');
          if (port === 5005)
            throw new Error(
              'Port 5005 is the original collector event port. NinjaTrader uses 5010.',
            );
          const result = await api('start_ninjatrader', [port]);
          if (!result.ok) throw new Error(result.message || 'The broadcaster could not start.');
        }
        onRuntime();
      },
      `${service === 'collector' ? 'Collector' : 'NinjaTrader broadcaster'} ${running ? 'stopped' : 'started'}.`,
    );
  };
  const collectorRunning = runtime?.collector?.running ?? false,
    ninjaRunning = runtime?.ninjatrader?.running ?? false;
  return (
    <div className="view-stack">
      <Panel
        title="Local runtime"
        eyebrow="Independent PublicGex installation"
        action={
          <Badge tone={runtime?.demo ? 'warning' : 'accent'}>
            {runtime?.demo ? 'DEMO' : 'LOCAL'}
          </Badge>
        }
      >
        <div className="runtime-grid">
          <div>
            <h3>Your data stays in this installation</h3>
            <p className="muted">
              This app uses its own settings, database, and credentials. Existing projects are not
              read or modified.
            </p>
            <dl className="path-list">
              <div>
                <dt>Data directory</dt>
                <dd>{runtime?.data_dir || 'Connecting…'}</dd>
              </div>
              <div>
                <dt>Credentials file</dt>
                <dd>
                  {runtime?.credentials_path || 'The analytics service will provide the path.'}
                </dd>
              </div>
            </dl>
            <Notice tone={runtime?.credentials_configured ? 'success' : 'info'}>
              {runtime?.demo
                ? 'Synthetic demonstration data. Collector and broker data calls are disabled.'
                : runtime?.credentials_configured
                  ? 'Credentials are configured. Live launches start the collector automatically unless disabled below.'
                  : 'Add your own PUBLIC_API_KEY and PUBLIC_ACCOUNT_ID to the credentials file above, then start the collector. Credentials are read locally and are never displayed here.'}
            </Notice>
          </div>
          <div className="service-cards">
            <div className="service-card">
              <div className="split">
                <h3>Market data collector</h3>
                <Badge tone={collectorRunning ? 'positive' : 'neutral'}>
                  {collectorRunning ? 'Running' : 'Stopped'}
                </Badge>
              </div>
              <p className="muted">Reads option chains and saves local snapshots.</p>
              {runtime?.startup_results?.collector && (
                <p className="source-note" role="status">
                  {runtime.startup_results.collector.message}
                </p>
              )}
              <button
                disabled={serviceAction.busy || runtime?.demo || !runtime}
                onClick={() => void toggleService('collector', collectorRunning)}
              >
                {collectorRunning ? 'Stop collector' : 'Start collector'}
              </button>
            </div>
            <div className="service-card">
              <div className="split">
                <h3>NinjaTrader broadcaster</h3>
                <Badge tone={ninjaRunning ? 'positive' : 'neutral'}>
                  {ninjaRunning ? 'Running' : 'Stopped'}
                </Badge>
              </div>
              <div className="toolbar">
                <Field label="NinjaTrader port">
                  <input
                    type="number"
                    min="1024"
                    max="65535"
                    value={port}
                    disabled={ninjaRunning}
                    onChange={(e) => setPort(Number(e.target.value))}
                  />
                </Field>
                <button
                  disabled={serviceAction.busy || runtime?.demo || !runtime}
                  onClick={() => void toggleService('ninjatrader', ninjaRunning)}
                >
                  {ninjaRunning ? 'Stop broadcaster' : 'Start broadcaster'}
                </button>
              </div>
              <p className="source-note">Local research levels only; no order routing.</p>
              {runtime?.startup_results?.ninjatrader && (
                <p className="source-note" role="status">
                  {runtime.startup_results.ninjatrader.message}
                </p>
              )}
            </div>
            {serviceAction.error && <Notice tone="error">{serviceAction.error}</Notice>}
            {serviceAction.message && <Notice tone="success">{serviceAction.message}</Notice>}
          </div>
        </div>
      </Panel>
      <Panel title="Workspace preferences" eyebrow="Display & collection">
        {draft ? (
          <form onSubmit={(event) => void save(event)} className="settings-form">
            <div className="toolbar" aria-label="Automatic startup">
              <Toggle
                label="Start collector when app opens"
                checked={draft.auto_start_collector ?? true}
                onChange={(value) => update('auto_start_collector', value)}
              />
              <Toggle
                label="Start NinjaTrader when app opens"
                checked={draft.auto_start_ninjatrader ?? true}
                onChange={(value) => update('auto_start_ninjatrader', value)}
              />
            </div>
            <p className="source-note">
              Saved startup preferences and the NinjaTrader port apply on the next live launch. Demo
              mode keeps both integrations off.
            </p>
            <div className="form-grid">
              <Field label="Instruments" hint="Comma-separated tickers">
                <input value={symbols} required onChange={(e) => setSymbols(e.target.value)} />
              </Field>
              <Field label="Appearance">
                <select value={theme} onChange={(e) => onTheme(e.target.value as Theme)}>
                  <option value="dark">Dark</option>
                  <option value="light">Light</option>
                </select>
              </Field>
              <Field label="UI refresh interval (seconds)">
                <input
                  type="number"
                  min="5"
                  value={draft.refresh_interval}
                  required
                  onChange={(e) => update('refresh_interval', Number(e.target.value))}
                />
              </Field>
              <Field label="API requests per second">
                <input
                  type="number"
                  min="0.1"
                  step="0.1"
                  value={draft.api_rate_limit_per_second}
                  required
                  onChange={(e) => update('api_rate_limit_per_second', Number(e.target.value))}
                />
              </Field>
              <Field label="API rate utilization" hint="Fraction from 0.1 to 1.0">
                <input
                  type="number"
                  min="0.1"
                  max="1"
                  step="0.05"
                  value={draft.api_rate_limit_utilization}
                  required
                  onChange={(e) => update('api_rate_limit_utilization', Number(e.target.value))}
                />
              </Field>
              <Field label="Minimum poll interval (seconds)">
                <input
                  type="number"
                  min="1"
                  value={draft.min_poll_interval_seconds}
                  required
                  onChange={(e) => update('min_poll_interval_seconds', Number(e.target.value))}
                />
              </Field>
              <Field label="Maximum poll interval (seconds)">
                <input
                  type="number"
                  min="1"
                  value={draft.max_poll_interval_seconds}
                  required
                  onChange={(e) => update('max_poll_interval_seconds', Number(e.target.value))}
                />
              </Field>
              <Field label="Raw data retention (days)">
                <input
                  type="number"
                  min="1"
                  value={draft.raw_retention_days}
                  required
                  onChange={(e) => update('raw_retention_days', Number(e.target.value))}
                />
              </Field>
            </div>
            <details className="management-advanced">
              <summary>Execution &amp; regime settings</summary>
              <div className="form-grid">
                <Field
                  label="Maximum risk per idea ($)"
                  hint="Used to size executable option candidates"
                >
                  <input
                    type="number"
                    min="0.01"
                    step="0.01"
                    required
                    value={draft.maximum_risk_dollars ?? 500}
                    onChange={(e) => update('maximum_risk_dollars', Number(e.target.value))}
                  />
                </Field>
                <Field label="Fees per contract ($)" hint="Included in candidate risk estimates">
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    required
                    value={draft.fees_per_contract ?? 1.25}
                    onChange={(e) => update('fees_per_contract', Number(e.target.value))}
                  />
                </Field>
                <Field
                  label="Trader basket weights"
                  hint="SYMBOL=weight, separated by commas. Weights need not total 1."
                >
                  <textarea
                    rows={2}
                    value={traderWeights}
                    required
                    onChange={(e) => setTraderWeights(e.target.value)}
                    placeholder="SPY=0.5, QQQ=0.3, IWM=0.2"
                  />
                </Field>
                <Field
                  label="Index basket weights"
                  hint="Used by the index basket compass; add these symbols to Instruments to collect them."
                >
                  <textarea
                    rows={2}
                    value={basketWeights}
                    required
                    onChange={(e) => setBasketWeights(e.target.value)}
                    placeholder="SPX=0.45, NDX=0.35, IWM=0.2"
                  />
                </Field>
              </div>
            </details>
            <div className="form-footer">
              <span className="muted">Changes apply to this installation only.</span>
              <button className="primary" type="submit" disabled={action.busy}>
                {action.busy ? 'Saving…' : 'Save settings'}
              </button>
            </div>
            {action.error && <Notice tone="error">{action.error}</Notice>}
            {action.message && <Notice tone="success">{action.message}</Notice>}
          </form>
        ) : (
          <Empty title="Settings unavailable">
            Connect to the local analytics service to load preferences.
          </Empty>
        )}
      </Panel>
    </div>
  );
}
