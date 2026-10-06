import { test, expect, type Page } from '@playwright/test';
import { renderedChart, visibleChart } from './chart-helpers';

async function open(page: Page) {
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible({ timeout: 60000 });
  await renderedChart(visibleChart(page));
}
async function view(page: Page, name: string) {
  await page.getByRole('button', { name, exact: true }).click();
}

test('all seven views render real analytics with Lightweight Charts and no runtime errors', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    // The native app uses its Windows icon; Vite has no browser favicon.
    if (message.type() === 'error' && !message.location().url.endsWith('/favicon.ico'))
      errors.push(message.text());
  });
  await open(page);
  await expect(
    page.getByRole('heading', { name: 'OI-based positioning proxy', exact: true }),
  ).toBeVisible();
  await view(page, 'Edge Lab');
  await expect(
    page.getByRole('heading', { name: 'Historical evidence', exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole('heading', { name: 'Manual execution journal', exact: true }),
  ).toBeVisible();
  await view(page, 'Regime');
  await expect(page.getByRole('heading', { name: 'Traders', exact: true })).toBeVisible();
  await renderedChart(visibleChart(page));
  await view(page, 'TRACE');
  await expect(page.getByLabel('Replay cursor', { exact: true })).toBeVisible({ timeout: 30000 });
  await page.getByLabel('Exposure metric').selectOption('modeled_charm_pressure');
  await expect(
    page.getByRole('img', { name: 'Modeled charm heatmap by strike and session time' }),
  ).toBeVisible();
  await page.getByLabel('Replay cursor', { exact: true }).focus();
  await page.getByLabel('Replay cursor', { exact: true }).press('Home');
  await expect(page.getByLabel('Replay cursor', { exact: true })).toHaveValue('0');
  await page.getByRole('button', { name: 'Play replay', exact: true }).click();
  await expect
    .poll(() => page.getByLabel('Replay cursor', { exact: true }).inputValue())
    .not.toBe('0');
  await page.getByRole('button', { name: 'Pause replay', exact: true }).click();
  await page.getByLabel('Decision overlays').check();
  await page.getByRole('button', { name: 'Fit session', exact: true }).click();
  await view(page, 'Strike Matrix');
  await expect(page.locator('[data-view-panel]:visible tbody tr').first()).toBeVisible();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export CSV ↓' }).click();
  expect((await download).suggestedFilename()).toMatch(/PublicGex-.*-strikes\.csv/);
  await view(page, 'One-Off');
  await expect(page.locator('.saved-profiles button').first()).toBeVisible();
  await page.locator('.saved-profiles button').first().click();
  await expect(page.getByRole('heading', { name: /option profile$/ })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Build profile', exact: true })).toBeDisabled();
  await view(page, 'Settings');
  await expect(page.getByLabel('NinjaTrader port')).toHaveValue('5010');
  await expect(page.getByRole('button', { name: 'Start collector', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Start broadcaster', exact: true })).toBeDisabled();
  expect(errors).toEqual([]);
});

test('rapid instrument changes and transport recovery do not show the wrong instrument', async ({
  page,
}) => {
  await open(page);
  const selector = page.getByLabel('Selected instrument');
  await selector.selectOption('NDX');
  await selector.selectOption('SPY');
  await selector.selectOption('SPX');
  await expect(selector).toHaveValue('SPX');
  await expect(page.locator('.header-price')).toContainText('SPX');
  await page.route('**/api/rpc', (route) => route.abort('connectionrefused'));
  await view(page, 'Regime');
  await expect(page.getByRole('alert').first()).toBeVisible();
  await page.unroute('**/api/rpc');
  await page.getByRole('button', { name: 'Retry', exact: true }).first().click();
  await renderedChart(visibleChart(page));
});

test('manual journal supports create, edit, weekly review and delete', async ({ page }) => {
  await open(page);
  await view(page, 'Edge Lab');
  const note = `Browser regression ${Date.now()}`;
  await page.getByLabel('Entry price', { exact: true }).fill('1.25');
  await page.getByLabel('Exit price', { exact: true }).fill('1.75');
  await page.getByLabel('Contracts', { exact: true }).fill('2');
  await page.getByLabel('Fees', { exact: true }).fill('2');
  await page.getByLabel('Journal notes', { exact: true }).fill(note);
  await page.getByRole('button', { name: 'Save journal entry', exact: true }).click();
  const row = page.locator('tr').filter({ hasText: note });
  await expect(row).toContainText('$98.00');
  await row.getByRole('button', { name: /Edit entry/ }).click();
  await page.getByLabel('Exit price', { exact: true }).fill('2.25');
  await page.getByRole('button', { name: 'Update journal entry', exact: true }).click();
  await expect(row).toContainText('$198.00');
  await expect(
    page.getByRole('heading', { name: 'Weekly execution review', exact: true }),
  ).toBeVisible();
  await row.getByRole('button', { name: /Delete entry/ }).click();
  await page.getByRole('button', { name: 'Confirm delete', exact: true }).click();
  await expect(row).toHaveCount(0);
});

test('settings survive reload and compact desktop layout stays within viewport', async ({
  page,
}) => {
  await open(page);
  await view(page, 'Settings');
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.getByLabel('UI refresh interval (seconds)', { exact: true }).fill('18');
  await page.getByRole('button', { name: 'Save settings', exact: true }).click();
  await expect(page.getByText('Settings saved.', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('demo-banner')).toBeVisible();
  await view(page, 'Settings');
  await expect(page.getByLabel('Appearance', { exact: true })).toHaveValue('light');
  await expect(page.getByLabel('UI refresh interval (seconds)', { exact: true })).toHaveValue('18');
  await page.getByLabel('Appearance', { exact: true }).selectOption('dark');
  await page.getByLabel('UI refresh interval (seconds)', { exact: true }).fill('10');
  await page.getByRole('button', { name: 'Save settings', exact: true }).click();
  await expect(page.getByText('Settings saved.', { exact: true })).toBeVisible();
  await view(page, 'Cockpit');
  await page.setViewportSize({ width: 1024, height: 768 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
    .toBe(true);
  await page.setViewportSize({ width: 1500, height: 950 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await renderedChart(visibleChart(page));
  await page.screenshot({ path: 'docs/screenshots/cockpit.png', fullPage: true });
  await view(page, 'TRACE');
  await expect(page.getByLabel('Replay cursor', { exact: true })).toBeVisible();
  await renderedChart(visibleChart(page));
  await page.screenshot({ path: 'docs/screenshots/trace.png', fullPage: true });
});
