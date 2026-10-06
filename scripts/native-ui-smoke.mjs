import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium, expect } from '@playwright/test';

// Launch only this project's packaged executable. CDP is enabled only for this
// short-lived test process, never in launchers or production configuration.
const root = resolve(import.meta.dirname, '..');
const mode = process.argv.includes('--live') ? 'live' : 'demo';
const probe = createServer();
await new Promise((done) => probe.listen(0, '127.0.0.1', done));
const port = probe.address().port;
await new Promise((done) => probe.close(done));
const child = spawn(resolve(root, 'src-tauri/target/release/publicgex-dashboard.exe'), [], {
  cwd: root,
  windowsHide: true,
  stdio: ['ignore', 'pipe', 'pipe'],
  env: {
    ...process.env,
    PUBLICGEX_DEMO: mode === 'demo' ? '1' : '0',
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port}`,
  },
});
let processError;
child.on('error', (error) => {
  processError = error;
});
let browser;
async function renderedChart(shell) {
  await expect(shell).toHaveAttribute('data-chart-engine', 'lightweight-charts');
  const host = shell.locator('.lwc-chart-host[data-chart-ready="true"]');
  await expect(host).toBeVisible({ timeout: 60000 });
  await expect
    .poll(
      () =>
        host.evaluate((element) => {
          const api = element.__lightweightChart;
          if (!api || !element.__lightweightSeries?.some(({ series }) => series.data().length > 0))
            return false;
          const canvas = api.takeScreenshot();
          if (canvas.width < 200 || canvas.height < 120) return false;
          const pixels = canvas
            .getContext('2d')
            .getImageData(0, 0, canvas.width, canvas.height).data;
          let colored = 0;
          for (let i = 0; i < pixels.length; i += 16) {
            const r = pixels[i],
              g = pixels[i + 1],
              b = pixels[i + 2];
            if (Math.max(r, g, b) - Math.min(r, g, b) > 50 && Math.max(r, g, b) > 90) colored++;
          }
          return colored > 8;
        }),
      { timeout: 30000 },
    )
    .toBe(true);
  return host;
}
try {
  const endpoint = `http://127.0.0.1:${port}`;
  await expect
    .poll(
      async () => {
        if (processError) throw processError;
        if (child.exitCode !== null) throw new Error(`Native process exited ${child.exitCode}`);
        try {
          return (await fetch(`${endpoint}/json/version`)).ok;
        } catch {
          return false;
        }
      },
      { timeout: 90000 },
    )
    .toBe(true);
  browser = await chromium.connectOverCDP(endpoint);
  const context = browser.contexts()[0];
  await expect.poll(() => context.pages().length, { timeout: 30000 }).toBeGreaterThan(0);
  const page = context.pages()[0];
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await expect(page.getByRole('heading', { name: 'Cockpit', exact: true })).toBeVisible({
    timeout: 60000,
  });
  const runtime = await page.evaluate(() =>
    window.__TAURI_INTERNALS__.invoke('backend_request', { method: 'get_runtime_info', args: [] }),
  );
  expect(runtime.mode).toBe(mode);
  const settings = await page.evaluate(() =>
    window.__TAURI_INTERNALS__.invoke('backend_request', { method: 'get_settings', args: [] }),
  );
  if (mode === 'demo') {
    expect(runtime.collector.running).toBe(false);
    expect(runtime.ninjatrader.running).toBe(false);
  } else {
    expect(runtime.ninjatrader.running).toBe(settings.auto_start_ninjatrader);
    expect(runtime.collector.running).toBe(
      settings.auto_start_collector && runtime.credentials_configured,
    );
  }
  expect(runtime.data_dir).toContain('com.publicgex.dashboard');
  await expect(
    page.getByRole('complementary', { name: 'Notifications', exact: true }),
  ).toBeVisible();
  if (mode === 'demo') {
    await expect(page.getByTestId('demo-banner')).toBeVisible();
    await expect(page.getByLabel('Selected instrument').locator('option')).toHaveText([
      'SPY',
      'QQQ',
      'IWM',
      'SPX',
      'NDX',
    ]);
    await renderedChart(
      page.getByRole('img', {
        name: 'Unsigned OI concentration and recent activity by strike',
        exact: true,
      }),
    );
    await expect(page.getByText('Dealer direction unknown', { exact: true })).toBeVisible();
    await renderedChart(
      page.getByRole('img', {
        name: 'Gamma by strike: call, put, and net exposure with spot and scenario levels',
        exact: true,
      }),
    );
    await mkdir(resolve(root, 'docs/screenshots'), { recursive: true });
    await page.screenshot({ path: resolve(root, 'docs/screenshots/native-cockpit.png') });
    await expect(page.getByLabel('Show modeled sweep', { exact: true })).toBeChecked();
    const sweep = page.getByRole('img', {
      name: 'Modeled gamma sweep and cumulative hedge demand across underlying prices',
    });
    const sweepHost = await renderedChart(sweep);
    const axes = await sweepHost.evaluate((element) =>
      element.__lightweightSeries
        .filter(({ hidden }) => !hidden)
        .map(({ series }) => series.options().priceScaleId),
    );
    expect(axes).toContain('left');
    expect(axes).toContain('right');
    await page.getByRole('button', { name: 'Regime', exact: true }).click();
    const compasses = page.locator('[data-view-panel="regime"] .lwc-chart-shell');
    await expect.poll(() => compasses.count()).toBeGreaterThanOrEqual(2);
    for (const chart of await compasses.all()) await renderedChart(chart);
    await page.getByLabel('Selected instrument', { exact: true }).selectOption('SPX');
    await page.getByRole('button', { name: 'Edge Lab', exact: true }).click();
    const evidence = page.locator('[data-view-panel="edge"] .lwc-chart-shell');
    await expect.poll(() => evidence.count()).toBeGreaterThanOrEqual(2);
    for (const chart of await evidence.all()) await renderedChart(chart);
    await page.getByRole('button', { name: 'TRACE', exact: true }).click();
    const heatmap = page.getByRole('img', {
      name: 'Net gamma heatmap by strike and session time',
      exact: true,
    });
    await renderedChart(heatmap);
    await page.getByLabel('Decision overlays', { exact: true }).check();
    await renderedChart(heatmap);
    await page.screenshot({ path: resolve(root, 'docs/screenshots/native-trace.png') });
    await page.getByRole('button', { name: 'Cockpit', exact: true }).click();
    await expect(page.getByLabel('Show modeled sweep', { exact: true })).toBeChecked();
    await renderedChart(sweep);
    await page.getByRole('button', { name: 'One-Off', exact: true }).click();
    const oneoff = page.locator('[data-view-panel="oneoff"]');
    await oneoff.locator('.saved-profiles button').first().click();
    await expect(oneoff.getByLabel('Show sweep', { exact: true })).toBeChecked();
    await renderedChart(
      oneoff.getByRole('img', {
        name: 'Gamma by strike: call, put, and net exposure with spot and scenario levels',
        exact: true,
      }),
    );
    await renderedChart(
      oneoff.getByRole('img', {
        name: 'Modeled gamma sweep and cumulative hedge demand across underlying prices',
        exact: true,
      }),
    );
  } else {
    await expect(page.getByTestId('demo-banner')).toHaveCount(0);
  }
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByLabel('NinjaTrader port')).toHaveValue(String(settings.ninjatrader_port));
  if (mode === 'demo')
    await expect(page.getByRole('button', { name: 'Start collector', exact: true })).toBeDisabled();
  expect(errors).toEqual([]);
  console.log(
    `Native ${mode} UI passed: Rust IPC, isolated Python, production CSP, ${mode === 'demo' ? 'Lightweight Charts gamma/TRACE/Sweep/compass/evidence rendering and retained tab state, ' : ''}and settings. Data: ${runtime.data_dir}`,
  );
} finally {
  // Killing this test-owned process also exercises the Rust job object's
  // descendant cleanup; normal application close uses graceful service shutdown.
  child.kill();
  await browser?.close().catch(() => {});
}
