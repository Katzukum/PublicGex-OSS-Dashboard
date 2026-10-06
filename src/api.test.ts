import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, validateResult } from './api';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
const snapshot = {
  id: 1,
  symbol: 'SPX',
  timestamp: '2026-09-30 10:00:00',
  spot_price: 6000,
  total_net_gex: 1e6,
};
const workspace = {
  schema_version: 2,
  symbol: 'SPX',
  snapshot_id: 1,
  dashboard: {
    snapshot,
    profile: [{ strike_price: 6000, option_type: 'CALL', gex_value: 1e6 }],
    history: [],
  },
  decision: { target: null, invalidation: null },
};
describe('typed analytics boundary', () => {
  it('accepts nullable unavailable scenario targets', () => {
    expect(validateResult('get_decision_workspace', workspace).decision?.target).toBeNull();
  });
  it('rejects corrupt option profile numbers before rendering', () => {
    expect(() =>
      validateResult('get_decision_workspace', {
        ...workspace,
        dashboard: {
          ...workspace.dashboard,
          profile: [{ strike_price: '6000', option_type: 'CALL', gex_value: 1 }],
        },
      }),
    ).toThrow(/option profile/);
  });
  it('rejects string targets instead of silently coercing prices', () => {
    expect(() =>
      validateResult('get_decision_workspace', { ...workspace, decision: { target: '6000' } }),
    ).toThrow(/decision target/);
  });
  it('surfaces service domain errors', () => {
    expect(() =>
      validateResult('get_decision_workspace', { error: 'No data found for SPX.' }),
    ).toThrow('No data found for SPX.');
  });
  it('distinguishes an empty collector status from failed operations', () => {
    expect(validateResult('get_backend_status', { ok: false }).ok).toBe(false);
    expect(() =>
      validateResult('start_collector', { ok: false, message: 'Credentials are required' }),
    ).toThrow('Credentials are required');
  });
  it('sends a structured browser RPC and validates its response', async () => {
    vi.stubGlobal('window', {});
    const fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ result: ['SPX'] }) });
    vi.stubGlobal('fetch', fetch);
    expect(await api('get_symbols', [])).toEqual(['SPX']);
    const request = fetch.mock.calls[0]![1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({ method: 'get_symbols', args: [] });
  });
  it('retains useful backend validation text from HTTP error envelopes', async () => {
    vi.stubGlobal('window', {});
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({ error: 'Add PUBLIC_API_KEY before collecting.' }),
      }),
    );
    await expect(api('start_collector', [])).rejects.toThrow(
      'Add PUBLIC_API_KEY before collecting.',
    );
  });
  it('bounds a hung request and allows retry', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('window', {});
    const fetch = vi
      .fn()
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ result: ['SPX'] }) });
    vi.stubGlobal('fetch', fetch);
    const pending = api('get_symbols', []);
    const assertion = expect(pending).rejects.toBeInstanceOf(ApiError);
    await vi.advanceTimersByTimeAsync(20000);
    await assertion;
    expect(await api('get_symbols', [])).toEqual(['SPX']);
  });
});
