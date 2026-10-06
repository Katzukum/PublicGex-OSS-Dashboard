import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { mkdirSync, mkdtempSync, readFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { startBackend } from '../scripts/backend-process.mjs';
import { validateResult } from '../src/api';
import type { ApiMap } from '../src/types';

describe('fresh live workspace matches the original configured defaults', () => {
  let backend: Awaited<ReturnType<typeof startBackend>>;
  const root = resolve('.');
  beforeAll(async () => {
    mkdirSync(join(root, '.test-data'), { recursive: true });
    backend = await startBackend(root, {
      demo: false,
      dataDir: mkdtempSync(join(root, '.test-data', 'live-parity-')),
    });
  }, 90000);
  afterAll(async () => {
    await backend?.stop();
  });
  it('exposes all five configured instruments before the first snapshot exists', async () => {
    const reference = JSON.parse(
      readFileSync(join(root, 'tests/fixtures/original-settings.json'), 'utf8'),
    );
    const actual = await backend.request('get_settings', []);
    expect(actual).toMatchObject(reference);
    expect(await backend.request('get_symbols', [])).toEqual(reference.symbols);
    const status = await backend.request('get_backend_status', []);
    expect(status.latest_snapshot_at).toBeNull();
    const runtime = await backend.request('get_runtime_info', []);
    expect(runtime.mode).toBe('live');
    expect(runtime.credentials_configured).toBe(false);
    expect(runtime.collector.running).toBe(false);
  });
  it('retains a deliberate instrument edit across a service restart', async () => {
    const runtime = await backend.request('get_runtime_info', []);
    await backend.request('save_settings', [{ symbols: ['SPY'] }]);
    await backend.stop();
    backend = await startBackend(root, { demo: false, dataDir: runtime.data_dir });
    expect((await backend.request('get_settings', [])).symbols).toEqual(['SPY']);
  });
});

describe('real isolated Python service and TypeScript contracts', () => {
  let backend: Awaited<ReturnType<typeof startBackend>>;
  const root = resolve('.');
  beforeAll(async () => {
    mkdirSync(join(root, '.test-data'), { recursive: true });
    const dataDir = mkdtempSync(join(root, '.test-data', 'contracts-'));
    backend = await startBackend(root, { demo: true, dataDir });
  }, 90000);
  afterAll(async () => {
    await backend?.stop();
  });
  async function call<K extends keyof ApiMap>(method: K, args: ApiMap[K]['args']) {
    return validateResult(method, await backend.request(method, args));
  }
  it('serves the complete dashboard and replay using actual analytical contracts', async () => {
    const runtime = await call('get_runtime_info', []);
    expect(runtime.demo).toBe(true);
    expect(runtime.data_dir).toContain('PublicGexDashboard');
    const symbols = await call('get_symbols', []);
    expect(symbols).toContain('SPX');
    await call('get_settings', []);
    await call('get_backend_status', []);
    const workspace = await call('get_decision_workspace', ['SPX']);
    expect(workspace.schema_version).toBe(2);
    expect(workspace.dashboard.profile.length).toBeGreaterThan(0);
    expect(workspace.dashboard.gamma_sweep?.points?.length).toBeGreaterThan(0);
    const overview = await call('get_market_overview', []);
    expect(overview.components.length).toBeGreaterThan(1);
    const dates = await call('get_trace_dates', ['SPX', 30]);
    expect(dates.length).toBeGreaterThan(0);
    const trace = await call('get_trace_data', ['SPX', 390, dates[0]]);
    expect(trace.heatmap.length).toBeGreaterThan(0);
    await call('get_edge_lab', [{ symbol: 'SPX', horizon: 30, include_legacy: false }]);
    await call('get_events', []);
    const profiles = await call('get_one_off_profiles', []);
    expect(profiles.length).toBeGreaterThan(0);
    await call('get_one_off_profile', [profiles[0].snapshot_id]);
  }, 60000);
  it('persists settings and journal changes only in its isolated workspace', async () => {
    const settings = await call('get_settings', []);
    await call('save_settings', [{ ...settings, theme: 'light', refresh_interval: 17 }]);
    expect((await call('get_settings', [])).refresh_interval).toBe(17);
    const date = new Date().toISOString().slice(0, 10);
    const entry = await call('create_journal_entry', [
      {
        symbol: 'SPX',
        session_date: date,
        entry_time: `${date}T10:00:00`,
        exit_time: `${date}T10:30:00`,
        contracts: 2,
        entry_price: 1,
        exit_price: 1.5,
        fees: 2,
        adhered_to_plan: true,
        notes: 'Automated isolated test',
      },
    ]);
    expect(entry.pnl).toBe(98);
    const edited = await call('update_journal_entry', [entry.id, { exit_price: 2 }]);
    expect(edited.pnl).toBe(198);
    expect((await call('get_journal_entries', ['SPX'])).some((row) => row.id === entry.id)).toBe(
      true,
    );
    await call('get_weekly_journal_review', [date]);
    expect(await call('delete_journal_entry', [entry.id])).toBe(true);
    expect((await call('get_journal_entries', ['SPX'])).some((row) => row.id === entry.id)).toBe(
      false,
    );
  });
  it('rejects external effects from demo mode and unknown methods', async () => {
    await expect(backend.request('start_collector', [])).rejects.toThrow(/demo|synthetic/i);
    await expect(backend.request('start_ninjatrader', [15010])).rejects.toThrow(/demo|synthetic/i);
    await expect(backend.request('execute', [])).rejects.toThrow(/unknown/i);
  });
});
