import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Badge, ErrorBoundary, Notice, RequestState } from './components';
import Cockpit from './Cockpit';
import EdgeLab from './EdgeLab';
import { Regime, StrikeMatrix, Trace } from './ResearchViews';
import { OneOff, SettingsView } from './ManagementViews';
import { useRemote } from './hooks';
import { RefreshCountdown } from './RefreshCountdown';
import {
  backendHealth,
  chooseInstrument,
  eventActivity,
  recordActivities,
  statusActivity,
  workspaceActivity,
} from './workspace-shell';
import type { ActivityItem } from './workspace-shell';
import { marketAlert, number, pretty, timeLabel, unseenMarketAlerts } from './models';
import type { MarketAlert } from './models';
import type { Theme, View } from './types';

const navigation: { id: View; name: string; detail: string; icon: string }[] = [
  {
    id: 'cockpit',
    name: 'Cockpit',
    detail: 'The market at a glance',
    icon: 'M3 3h7v7H3z M14 3h7v4h-7z M14 11h7v10h-7z M3 14h7v7H3z',
  },
  {
    id: 'edge',
    name: 'Edge Lab',
    detail: 'Evidence & execution journal',
    icon: 'M4 19h16 M7 15V9 M12 15V4 M17 15v-4',
  },
  {
    id: 'regime',
    name: 'Regime',
    detail: 'Cross-asset market conditions',
    icon: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20 M16 8l-3 5-5 3 3-5z',
  },
  {
    id: 'trace',
    name: 'TRACE',
    detail: 'Intraday positioning replay',
    icon: 'M3 19V5 M3 19h18 M6 15l4-5 4 3 6-8',
  },
  {
    id: 'matrix',
    name: 'Strike Matrix',
    detail: 'Every strike, one snapshot',
    icon: 'M3 4h18v16H3z M3 9h18 M3 14h18 M9 4v16 M15 4v16',
  },
  {
    id: 'oneoff',
    name: 'One-Off',
    detail: 'Independent expiration analysis',
    icon: 'M8 3h8 M10 3v7l-6 9a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2l-6-9V3 M8 15h8',
  },
  {
    id: 'settings',
    name: 'Settings',
    detail: 'Your local workspace',
    icon: 'M4 7h16 M4 17h16 M9 4v6 M15 14v6',
  },
];
function readLocal(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function Icon({ path }: { path: string }) {
  return (
    <svg
      className="app-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={path} />
    </svg>
  );
}

export default function App() {
  const [view, setView] = useState<View>('cockpit'),
    [symbol, setSymbol] = useState(readLocal('publicgex-symbol') || ''),
    [theme, setTheme] = useState<Theme>(
      readLocal('publicgex-theme') === 'light' ? 'light' : 'dark',
    ),
    [revision, setRevision] = useState(0),
    [journalFocus, setJournalFocus] = useState(0),
    [visited, setVisited] = useState<Set<View>>(() => new Set(['cockpit'])),
    [sidebarCollapsed, setSidebarCollapsed] = useState(false),
    [fullscreen, setFullscreen] = useState(false),
    [shellError, setShellError] = useState(''),
    [showActivity, setShowActivity] = useState(true),
    [events, setEvents] = useState<ActivityItem[]>([]),
    [alerts, setAlerts] = useState<MarketAlert[]>([]);
  const currentView = useRef(view);
  const scrollPositions = useRef(new Map<View, number>());
  const themeInitialized = useRef(Boolean(readLocal('publicgex-theme')));
  const navigate = useCallback((next: View) => {
    if (currentView.current === next) return;
    scrollPositions.current.set(currentView.current, window.scrollY);
    currentView.current = next;
    setVisited((previous) => new Set([...previous, next]));
    setView(next);
  }, []);
  useLayoutEffect(() => {
    const frame = requestAnimationFrame(() =>
      window.scrollTo(0, scrollPositions.current.get(view) || 0),
    );
    return () => cancelAnimationFrame(frame);
  }, [view]);
  const seenAlerts = useRef(new Set<string>());
  const processedEventBatch = useRef<unknown>(null);
  const baselineSymbols = useRef(new Set<string>());
  const settings = useRemote('get_settings', []),
    symbols = useRemote('get_symbols', [], { interval: 15000 }),
    runtime = useRemote('get_runtime_info', [], { interval: 5000 }),
    status = useRemote('get_backend_status', [], { interval: 5000 }),
    incoming = useRemote('get_events', [], { interval: 5000 });
  const interval = Math.max(5, settings.data?.refresh_interval ?? 10) * 1000;
  const workspace = useRemote('get_decision_workspace', [symbol], {
    enabled: !!symbol,
    interval: view === 'settings' || view === 'oneoff' ? 0 : interval,
    revision,
  });
  const instruments = useMemo(
    () => [...new Set([...(settings.data?.symbols ?? []), ...(symbols.data ?? [])])],
    [symbols.data, settings.data?.symbols],
  );
  useEffect(() => {
    if (instruments.length && !instruments.includes(symbol))
      setSymbol(chooseInstrument(instruments, symbol, settings.data?.symbols));
  }, [instruments, symbol, settings.data?.symbols]);
  useEffect(() => {
    if (settings.data && !themeInitialized.current) {
      themeInitialized.current = true;
      setTheme(settings.data.theme);
    }
  }, [settings.data]);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem('publicgex-theme', theme);
    } catch {
      /* Storage is optional. */
    }
  }, [theme]);
  useEffect(() => {
    if (symbol)
      try {
        localStorage.setItem('publicgex-symbol', symbol);
      } catch {
        /* Storage is optional. */
      }
  }, [symbol]);
  useEffect(() => {
    if (!incoming.data?.length || processedEventBatch.current === incoming.data) return;
    processedEventBatch.current = incoming.data;
    const enhanced = incoming.data
      .map(eventActivity)
      .filter((entry): entry is ActivityItem => Boolean(entry));
    setEvents((previous) => recordActivities(previous, enhanced));
    if (
      incoming.data.some(
        (event) =>
          event.type === 'MARKET_UPDATE' ||
          (event.type === 'data_refresh' && event.payload?.symbol === symbol),
      )
    ) {
      setRevision((value) => value + 1);
      symbols.reload();
      status.reload();
    }
    const actionable = incoming.data
      .map(marketAlert)
      .filter((alert): alert is MarketAlert => Boolean(alert));
    const freshAlerts = unseenMarketAlerts(actionable, seenAlerts.current);
    if (freshAlerts.length)
      setAlerts((previous) => [...freshAlerts.reverse(), ...previous].slice(0, 4));
  }, [incoming.data, symbol, symbols.reload, status.reload]);
  useEffect(() => {
    if (!workspace.data || workspace.data.symbol !== symbol) return;
    const persisted = (workspace.data.alerts ?? [])
      .map(marketAlert)
      .filter((alert): alert is MarketAlert => Boolean(alert) && alert?.symbol === symbol);
    const freshAlerts = unseenMarketAlerts(persisted, seenAlerts.current);
    // Establish a history baseline once per instrument; old saved decisions are not new notifications.
    if (!baselineSymbols.current.has(symbol)) {
      baselineSymbols.current.add(symbol);
      return;
    }
    if (freshAlerts.length) {
      setAlerts((previous) => [...freshAlerts, ...previous].slice(0, 4));
      setEvents((previous) =>
        recordActivities(
          previous,
          freshAlerts.map((alert) => ({
            key: alert.id,
            title: alert.title,
            message: alert.message,
            timestamp: new Date().toISOString(),
            tone: alert.severity === 'critical' ? 'negative' : 'warning',
          })),
        ),
      );
    }
  }, [workspace.data, symbol]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        event.target instanceof HTMLElement &&
        ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)
      )
        return;
      if (event.altKey && /^[1-7]$/.test(event.key)) {
        event.preventDefault();
        const next = navigation[Number(event.key) - 1];
        if (next) navigate(next.id);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navigate]);
  useEffect(() => {
    if (status.data || status.error)
      setEvents((previous) =>
        recordActivities(previous, [statusActivity(status.data, status.error)]),
      );
  }, [status.data, status.error]);
  useEffect(() => {
    if (workspace.data?.symbol === symbol)
      setEvents((previous) => recordActivities(previous, [workspaceActivity(workspace.data!)]));
  }, [workspace.data, symbol]);
  useEffect(() => {
    const changed = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', changed);
    return () => document.removeEventListener('fullscreenchange', changed);
  }, []);
  const toggleFullscreen = async () => {
    try {
      setShellError('');
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch (error) {
      setShellError(
        error instanceof Error ? error.message : 'Fullscreen is unavailable in this window.',
      );
    }
  };
  const active = navigation.find((item) => item.id === view)!;
  const data = workspace.data?.symbol === symbol ? workspace.data : undefined;
  const demo = runtime.data?.demo ?? false;
  const connected = !!runtime.data && !runtime.error;
  const health = backendHealth(status.data, status.error || runtime.error);
  const selectedAge = data?.as_of
    ? (Date.now() - new Date(data.as_of).getTime()) / 1000
    : undefined;
  const fresh = selectedAge !== undefined && selectedAge <= 180;
  const refresh = () => {
    setRevision((value) => value + 1);
    symbols.reload();
    runtime.reload();
    status.reload();
  };
  const saveSettings = () => {
    settings.reload();
    symbols.reload();
    refresh();
  };
  return (
    <div className={`app-shell${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            navigate('cockpit');
          }}
          aria-label="PublicGex home"
        >
          <span className="brand-mark">
            <i />
            <i />
            <i />
          </span>
          <span>
            PublicGex<small>MARKET WORKSPACE</small>
          </span>
        </a>
        <div className="nav-label">RESEARCH</div>
        <nav aria-label="Main navigation">
          {navigation.map((item, index) => (
            <button
              key={item.id}
              className={view === item.id ? 'active' : ''}
              aria-label={item.name}
              aria-current={view === item.id ? 'page' : undefined}
              onClick={() => navigate(item.id)}
              title={`${item.name} · Alt+${index + 1}`}
            >
              <Icon path={item.icon} />
              <span>{item.name}</span>
              {view === item.id && <i className="nav-active-dot" />}
            </button>
          ))}
        </nav>
        <button
          className="collapse-sidebar"
          onClick={() => setSidebarCollapsed((value) => !value)}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-expanded={!sidebarCollapsed}
        >
          <span aria-hidden="true">{sidebarCollapsed ? '»' : '«'}</span>
          <span className="collapse-label">Collapse</span>
        </button>
        <div className="sidebar-bottom">
          <div className="local-status">
            <i className={`dot ${connected ? 'positive-dot' : 'negative-dot'}`} />
            <span>{connected ? 'Local analytics connected' : 'Connecting to analytics'}</span>
          </div>
          <p>
            Research with context.
            <br />
            Keep the evidence in view.
          </p>
          <span className="version">INDEPENDENT WORKSPACE · 0.1.4</span>
        </div>
      </aside>
      <div className="main-shell">
        <header className="command-bar">
          <div className="breadcrumb">
            Workspace <span>/</span> <strong>{active.name}</strong>
          </div>
          <div className="command-actions">
            <label className="symbol-picker">
              <span>Instrument</span>
              <select
                aria-label="Selected instrument"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
              >
                {!instruments.length && <option value="">No instruments</option>}
                {instruments.map((item) => (
                  <option value={item} key={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
            <div className="header-price">
              <strong>{number(data?.dashboard.snapshot.spot_price)}</strong>
              <span>{symbol || 'Underlying'}</span>
            </div>
            <div className="header-health" data-testid="backend-health" title={health.detail}>
              <span>Data</span>
              <Badge tone={health.tone}>{health.label}</Badge>
            </div>
            <button
              className="icon-button"
              aria-label="Refresh workspace"
              onClick={refresh}
              title="Refresh saved data"
            >
              ↻
            </button>
            <button
              className="icon-button"
              aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
              title="Fullscreen"
              aria-pressed={fullscreen}
              onClick={() => void toggleFullscreen()}
            >
              <Icon path="M8 3H3v5 M16 3h5v5 M21 16v5h-5 M8 21H3v-5" />
            </button>
            <button
              className="icon-button"
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              title="Change appearance"
            >
              {theme === 'dark' ? '☼' : '☾'}
            </button>
            <button
              className={`icon-button ${showActivity ? 'selected' : ''}`}
              aria-label="Toggle activity feed"
              aria-expanded={showActivity}
              aria-controls="workspace-notifications"
              onClick={() => setShowActivity((previous) => !previous)}
              title="Activity feed"
            >
              ≡
            </button>
          </div>
        </header>
        {demo && (
          <div className="demo-banner" data-testid="demo-banner">
            <span className="demo-chip">DEMO</span>
            <span>
              Synthetic market data for exploring the workspace. No live API collection or broker
              actions.
            </span>
          </div>
        )}
        <div className={`workspace-layout${showActivity ? ' notifications-visible' : ''}`}>
          <main>
            <div className="page-title">
              <div>
                <span className="eyebrow">{active.detail}</span>
                <h1>{active.name}</h1>
              </div>
              <div className="page-status">
                <Badge
                  tone={!connected ? 'negative' : demo ? 'warning' : fresh ? 'positive' : 'neutral'}
                >
                  {!connected
                    ? 'OFFLINE'
                    : demo
                      ? 'DEMO DATA'
                      : fresh
                        ? 'FRESH SNAPSHOT'
                        : 'SAVED DATA'}
                </Badge>
                <span>
                  {data?.as_of ? `As of ${timeLabel(data.as_of)}` : 'Waiting for first snapshot'}
                </span>
                <RefreshCountdown
                  interval={interval}
                  updatedAt={workspace.updatedAt}
                  paused={view === 'settings' || view === 'oneoff'}
                />
              </div>
            </div>
            {runtime.error && (
              <Notice tone="error">
                <span>Local analytics disconnected. {runtime.error}</span>
                <button onClick={runtime.reload}>Reconnect</button>
              </Notice>
            )}
            {!demo &&
              runtime.data &&
              !runtime.data.credentials_configured &&
              view !== 'settings' && (
                <Notice tone="info">
                  <span>
                    Start with your own data. Configure your credentials and start the collector in
                    Settings.
                  </span>
                  <button onClick={() => navigate('settings')}>Open Settings</button>
                </Notice>
              )}
            {shellError && (
              <Notice tone="error">
                {shellError}
                <button onClick={() => setShellError('')}>Dismiss</button>
              </Notice>
            )}
            {(view === 'cockpit' || view === 'matrix') && (
              <RequestState {...workspace} hasData={!!data} />
            )}
            {settings.error && view === 'settings' && (
              <Notice tone="error">
                {settings.error}
                <button onClick={settings.reload}>Retry settings</button>
              </Notice>
            )}
            {visited.has('cockpit') && (
              <div data-view-panel="cockpit" hidden={view !== 'cockpit'}>
                <ErrorBoundary>
                  <Cockpit
                    workspace={data}
                    theme={theme}
                    onJournal={() => {
                      navigate('edge');
                      setJournalFocus((value) => value + 1);
                    }}
                  />
                </ErrorBoundary>
              </div>
            )}
            {visited.has('edge') && (
              <div data-view-panel="edge" hidden={view !== 'edge'}>
                <ErrorBoundary>
                  <EdgeLab
                    symbol={symbol}
                    workspace={data}
                    theme={theme}
                    revision={revision}
                    focus={journalFocus}
                    active={view === 'edge'}
                  />
                </ErrorBoundary>
              </div>
            )}
            {visited.has('regime') && (
              <div data-view-panel="regime" hidden={view !== 'regime'}>
                <ErrorBoundary>
                  <Regime
                    theme={theme}
                    revision={revision}
                    interval={view === 'regime' ? interval : 0}
                  />
                </ErrorBoundary>
              </div>
            )}
            {visited.has('trace') && (
              <div data-view-panel="trace" hidden={view !== 'trace'}>
                <ErrorBoundary>
                  <Trace
                    symbol={symbol}
                    theme={theme}
                    revision={revision}
                    interval={view === 'trace' ? interval : 0}
                    active={view === 'trace'}
                  />
                </ErrorBoundary>
              </div>
            )}
            {visited.has('matrix') && (
              <div data-view-panel="matrix" hidden={view !== 'matrix'}>
                <ErrorBoundary>
                  <StrikeMatrix dashboard={data?.dashboard} />
                </ErrorBoundary>
              </div>
            )}
            {visited.has('oneoff') && (
              <div data-view-panel="oneoff" hidden={view !== 'oneoff'}>
                <ErrorBoundary>
                  <OneOff
                    symbol={symbol}
                    theme={theme}
                    revision={revision}
                    demo={demo}
                    active={view === 'oneoff'}
                  />
                </ErrorBoundary>
              </div>
            )}
            {visited.has('settings') && (
              <div data-view-panel="settings" hidden={view !== 'settings'}>
                <ErrorBoundary>
                  <SettingsView
                    settings={settings.data}
                    runtime={runtime.data}
                    onSaved={saveSettings}
                    onRuntime={() => {
                      runtime.reload();
                      status.reload();
                    }}
                    theme={theme}
                    onTheme={setTheme}
                  />
                </ErrorBoundary>
              </div>
            )}
            <footer className="workspace-footer">
              <span>
                <i className="dot accent-dot" />{' '}
                {demo ? 'Synthetic demo dataset' : 'Local SQLite · Public.com market data'}
              </span>
              <span>Data quality measures inputs, not the probability of a trade succeeding.</span>
              <span className="lwc-attribution">
                TradingView Lightweight Charts™ · Copyright (с) 2025 TradingView, Inc.{' '}
                <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">
                  TradingView
                </a>
              </span>
            </footer>
          </main>
          {showActivity && (
            <aside
              id="workspace-notifications"
              className="activity-column"
              aria-label="Notifications"
            >
              <header>
                <div>
                  <span className="eyebrow">LOCAL EVENTS</span>
                  <h2>Notifications</h2>
                </div>
                <button
                  className="icon-button"
                  aria-label="Close activity feed"
                  onClick={() => setShowActivity(false)}
                >
                  ×
                </button>
              </header>
              <div className="activity-health">
                <Badge tone={connected ? 'positive' : 'negative'}>
                  {connected ? 'Connected' : 'Disconnected'}
                </Badge>
                <span>Collector: {runtime.data?.collector?.running ? 'running' : 'stopped'}</span>
                <span>Latest run: {pretty(status.data?.run_status)}</span>
              </div>
              <div className="notification-scroll">
                {alerts.length > 0 && (
                  <section className="actionable-alerts" aria-label="Actionable market alerts">
                    <h3>Market alerts</h3>
                    {alerts.map((alert) => (
                      <Notice
                        key={alert.id}
                        tone={alert.severity === 'critical' ? 'error' : 'warning'}
                      >
                        <div className="actionable-alert-copy">
                          <strong>{alert.title}</strong>
                          <span>{alert.message}</span>
                        </div>
                        <button
                          className="icon-button"
                          aria-label={`Dismiss alert ${alert.title}`}
                          onClick={() =>
                            setAlerts((previous) => previous.filter((item) => item.id !== alert.id))
                          }
                        >
                          ×
                        </button>
                      </Notice>
                    ))}
                  </section>
                )}
                <div className="activity-list">
                  <h3>Activity feed</h3>
                  {events.length ? (
                    events.map((event, index) => (
                      <article
                        key={event.key || `${event.timestamp}:${index}`}
                        data-tone={event.tone}
                      >
                        <span>{timeLabel(event.timestamp)}</span>
                        <strong>{event.title}</strong>
                        <p>{event.message}</p>
                      </article>
                    ))
                  ) : (
                    <p className="muted">
                      No new events in this session. Collector and decision updates will appear
                      here.
                    </p>
                  )}
                </div>
              </div>
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}
