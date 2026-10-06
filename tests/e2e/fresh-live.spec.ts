import { test, expect } from '@playwright/test';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { resolve, join } from 'node:path';
import { startBackend } from '../../scripts/backend-process.mjs';

test('fresh live UI exposes original instruments and automatically starts its own bridge', async ({
  page,
}) => {
  const root = resolve('.');
  mkdirSync(join(root, '.test-data'), { recursive: true });
  const directory = mkdtempSync(join(root, '.test-data', 'live-ui-'));
  const probe = createServer();
  await new Promise<void>((done) => probe.listen(0, '127.0.0.1', done));
  const address = probe.address();
  if (!address || typeof address === 'string') throw new Error('No loopback port');
  const port = address.port;
  await new Promise<void>((done) => probe.close(() => done()));
  writeFileSync(join(directory, 'settings.json'), JSON.stringify({ ninjatrader_port: port }));
  const backend = await startBackend(root, { demo: false, dataDir: directory });
  try {
    // Render the normal frontend against a real fresh live service, without keys.
    await page.route('**/api/rpc', async (route) => {
      const request = route.request().postDataJSON();
      try {
        await route.fulfill({
          json: { result: await backend.request(request.method, request.args) },
        });
      } catch (error) {
        await route.fulfill({ status: 400, json: { error: String(error) } });
      }
    });
    await page.goto('/');
    await expect(page.getByLabel('Selected instrument')).toHaveValue('SPY');
    await expect(page.getByLabel('Selected instrument').locator('option')).toHaveText([
      'SPY',
      'QQQ',
      'IWM',
      'SPX',
      'NDX',
    ]);
    await expect(page.getByTestId('demo-banner')).toHaveCount(0);
    expect((await backend.request('get_backend_status', [])).latest_snapshot_at).toBeNull();
    await page.getByRole('button', { name: 'Settings', exact: true }).click();
    await expect(page.getByLabel('NinjaTrader port')).toHaveValue(String(port));
    await expect(page.getByRole('button', { name: 'Stop broadcaster', exact: true })).toBeEnabled();
    await expect(
      page.getByText(/Add PUBLIC_API_KEY and PUBLIC_ACCOUNT_ID to this workspace/),
    ).toBeVisible();
    await expect(page.getByLabel('Start collector when app opens')).toBeChecked();
    await expect(page.getByLabel('Start NinjaTrader when app opens')).toBeChecked();
  } finally {
    await page.close();
    await backend.stop();
  }
});
