import type { BackendEvent, BackendStatus, Workspace } from './types';
import { marketAlert, pretty } from './models';

export interface ActivityItem {
  key?: string;
  title: string;
  message: string;
  timestamp: string;
  tone: 'positive' | 'negative' | 'warning' | 'neutral';
}

export function ageLabel(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return 'n/a';
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function backendHealth(status?: BackendStatus, error?: string) {
  if (error || status?.ok === false)
    return {
      label: 'Backend unavailable',
      detail: error || status?.run_message || 'Status unavailable',
      tone: 'negative' as const,
    };
  if (!status)
    return {
      label: 'Checking backend',
      detail: 'Waiting for the local service',
      tone: 'neutral' as const,
    };
  if (status.run_status?.toLowerCase() === 'running')
    return {
      label: `Collector running · ${ageLabel(status.run_age_seconds)}`,
      detail: status.run_message || 'Collecting current option data',
      tone: 'positive' as const,
    };
  if (status.snapshot_age_seconds == null)
    return {
      label: 'No snapshot',
      detail: status.run_message || 'Start the collector to collect market data',
      tone: 'neutral' as const,
    };
  const stale = status.snapshot_age_seconds > 180;
  return {
    label: `${stale ? 'Stale' : 'Fresh'} · ${ageLabel(status.snapshot_age_seconds)}`,
    detail: status.run_message || 'Latest saved market snapshot',
    tone: stale ? ('warning' as const) : ('positive' as const),
  };
}

export function statusActivity(status?: BackendStatus, error?: string): ActivityItem {
  const health = backendHealth(status, error);
  const bridge = status?.event_bridge;
  const bridgeLabel =
    bridge?.status === 'listening'
      ? `event bridge ${bridge.last_event_type || 'listening'}`
      : 'event bridge stopped';
  return {
    key: 'backend-health',
    title: health.label,
    message: `${health.detail} · ${bridgeLabel}`,
    timestamp: new Date().toISOString(),
    tone: health.tone,
  };
}

export function workspaceActivity(workspace: Workspace): ActivityItem {
  return {
    key: `workspace:${workspace.symbol}`,
    title: `${workspace.symbol} cockpit refreshed`,
    message: `Snapshot #${workspace.snapshot_id} · ${workspace.as_of || workspace.dashboard.snapshot.timestamp}`,
    timestamp: new Date().toISOString(),
    tone: 'neutral',
  };
}

export function eventActivity(event: BackendEvent): ActivityItem | undefined {
  const timestamp =
    event.received_at || event.timestamp || event.created_at || new Date().toISOString();
  const payload = event.payload || {};
  if (event.type === 'data_refresh') {
    const symbol = String(payload.symbol || 'Market').toUpperCase();
    return {
      key: `data-refresh:${symbol}`,
      title: `${symbol} data received`,
      message:
        payload.snapshot_id == null ? 'New snapshot' : `Snapshot #${String(payload.snapshot_id)}`,
      timestamp,
      tone: 'neutral',
    };
  }
  if (event.type === 'MARKET_UPDATE')
    return {
      key: 'market-update',
      title: 'Market cycle received',
      message: 'Overview and downstream integrations refreshed.',
      timestamp,
      tone: 'neutral',
    };
  const alert = marketAlert(event);
  if (alert)
    return {
      key: alert.id,
      title: alert.title,
      message: alert.message,
      timestamp,
      tone: alert.severity === 'critical' ? 'negative' : 'warning',
    };
  if (event.message || payload.message)
    return {
      title: pretty(event.type || event.alert_type),
      message: String(event.message || payload.message),
      timestamp,
      tone: 'neutral',
    };
  return undefined;
}

/** Coalesce routine health/refresh updates, preserving distinct actionable alerts. */
export function recordActivities(
  previous: ActivityItem[],
  entries: ActivityItem[],
): ActivityItem[] {
  return entries
    .reduce(
      (items, entry) => [entry, ...items.filter((item) => !entry.key || item.key !== entry.key)],
      previous,
    )
    .slice(0, 60);
}

export function chooseInstrument(
  available: string[],
  preferred: string,
  configured: string[] = [],
): string {
  return available.includes(preferred)
    ? preferred
    : configured.find((symbol) => available.includes(symbol)) || available[0] || '';
}
