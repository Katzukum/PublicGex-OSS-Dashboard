import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 60000,
  use: {
    baseURL: 'http://127.0.0.1:1420',
    viewport: { width: 1500, height: 950 },
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    channel: 'msedge',
  },
  webServer: {
    command: 'npm run dev:demo',
    url: 'http://127.0.0.1:1420',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
