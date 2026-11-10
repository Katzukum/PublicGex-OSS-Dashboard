import { expect, test } from '@playwright/test';
import { renderedChart } from './chart-helpers';

test('observational option research renders and TRACE data remains availability ordered', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible({ timeout: 60000 });

  const cockpit = page.locator('[data-view-panel="cockpit"]');
  await expect(
    cockpit.getByRole('heading', { name: 'Option Envelopes', exact: true }),
  ).toBeVisible();
  await cockpit.getByLabel('Envelope source', { exact: true }).selectOption('oi_gamma');
  await expect(cockpit.getByText(/Structural proxy/)).toBeVisible();
  await expect(
    cockpit.getByRole('heading', { name: 'Estimated Hedge Pressure', exact: true }),
  ).toBeVisible();
  await renderedChart(cockpit.getByRole('img', { name: /^Estimated Hedge Pressure:/ }));
  await expect(
    cockpit.getByText(/dealer\/customer identity is not observed/i).first(),
  ).toBeVisible();
  await expect(
    cockpit.getByRole('heading', { name: 'Option Exhaustion', exact: true }),
  ).toBeVisible();

  await page.getByRole('button', { name: 'TRACE', exact: true }).click();
  const trace = page.locator('[data-view-panel="trace"]');
  await expect(
    trace.getByRole('heading', { name: 'TRACE Option Envelopes', exact: true }),
  ).toBeVisible();
  await expect(
    trace.getByRole('heading', { name: 'TRACE Estimated Hedge Pressure', exact: true }),
  ).toBeVisible();
  await renderedChart(trace.getByRole('img', { name: /^TRACE Estimated Hedge Pressure:/ }));

  const response = await page.request.post('/api/rpc', {
    data: { method: 'get_trace_data', args: ['SPX', 390, null] },
  });
  expect(response.ok()).toBe(true);
  const payload = await response.json();
  const result = payload.result;
  expect(result.envelope_history.length).toBeGreaterThan(10);
  expect(result.estimated_pressure.model).toBe('estimated_hedge_pressure_v1');
  expect(result.exhaustion.model).toBe('estimated_option_exhaustion_v1');
  const envelopeTimes = result.envelope_history.map((item: { available_at: string }) =>
    Date.parse(item.available_at),
  );
  expect(envelopeTimes).toEqual([...envelopeTimes].sort((left, right) => left - right));
  for (const event of result.exhaustion_events) {
    expect(Date.parse(event.available_at)).toBeLessThanOrEqual(
      Date.parse(result.estimated_pressure.available_at),
    );
  }
});
