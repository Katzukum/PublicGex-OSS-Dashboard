import { expect, test, type Page } from '@playwright/test';

async function navigate(page: Page, name: string) {
  await page.getByRole('button', { name, exact: true }).click();
}

test('one-off selection, evidence filters and journal drafts survive view switches', async ({
  page,
}) => {
  const workspaceResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/rpc') &&
      response.request().postDataJSON()?.method === 'get_decision_workspace',
  );
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible({ timeout: 60000 });
  const workspace = (await (await workspaceResponse).json()).result;
  await navigate(page, 'One-Off');
  const oneoff = page.locator('[data-view-panel="oneoff"]');
  await expect(oneoff.getByLabel('One-off symbol', { exact: true })).toHaveValue('NVDA');
  await oneoff.locator('.saved-profiles button').first().click();
  await expect(oneoff.getByRole('heading', { name: /option profile$/ })).toBeVisible();
  const profileTitle = await oneoff.getByRole('heading', { name: /option profile$/ }).textContent();
  await oneoff.getByLabel('Show sweep', { exact: true }).check();
  await navigate(page, 'Cockpit');
  await page.getByRole('button', { name: /Journal this scenario/ }).click();
  const edge = page.locator('[data-view-panel="edge"]');
  await expect(edge.getByLabel('Session date', { exact: true })).toHaveValue(
    workspace.as_of.slice(0, 10),
  );
  await expect(edge.getByLabel('Followed my plan', { exact: true })).not.toBeChecked();
  await expect(edge.locator('.journal-context')).toContainText('Saved context:');
  await edge
    .getByLabel('Journal notes', { exact: true })
    .fill('Keep this draft when changing views');
  await edge.getByLabel('Evidence horizon', { exact: true }).selectOption('60');
  const taggedRequest = page.waitForRequest(
    (request) =>
      request.url().endsWith('/api/rpc') &&
      request.postDataJSON()?.method === 'get_edge_lab' &&
      request.postDataJSON()?.args[0].event_tag === 'FOMC',
  );
  await edge.getByLabel('Event tag filter', { exact: true }).fill('FOMC');
  await taggedRequest;
  await navigate(page, 'One-Off');
  await expect(oneoff.getByRole('heading', { name: /option profile$/ })).toHaveText(profileTitle!);
  await expect(oneoff.getByLabel('Show sweep', { exact: true })).toBeChecked();
  await navigate(page, 'Edge Lab');
  await expect(edge.getByLabel('Evidence horizon', { exact: true })).toHaveValue('60');
  await expect(edge.getByLabel('Event tag filter', { exact: true })).toHaveValue('FOMC');
  await expect(edge.getByLabel('Journal notes', { exact: true })).toHaveValue(
    'Keep this draft when changing views',
  );
  const instrument = page.getByLabel('Selected instrument', { exact: true });
  const previousInstrument = await instrument.inputValue();
  await instrument.selectOption(previousInstrument === 'NDX' ? 'SPX' : 'NDX');
  await expect(edge.getByLabel('Journal notes', { exact: true })).toHaveValue('');
  await instrument.selectOption(previousInstrument);
  await expect(edge.getByLabel('Journal notes', { exact: true })).toHaveValue(
    'Keep this draft when changing views',
  );
  await edge.getByRole('button', { name: 'Reset filters', exact: true }).click();
  await expect(edge.getByLabel('Evidence horizon', { exact: true })).toHaveValue('30');
  await expect(edge.getByLabel('Event tag filter', { exact: true })).toHaveValue('');
});

test('original collection ranges and advanced risk and basket settings save and reload', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('demo-banner')).toBeVisible({ timeout: 60000 });
  await navigate(page, 'Settings');
  const panel = page.locator('[data-view-panel="settings"]');
  await panel.getByText('Execution & regime settings', { exact: true }).click();
  const changed = [
    ['API requests per second', '0.1'],
    ['Minimum poll interval (seconds)', '1'],
    ['Maximum poll interval (seconds)', '2'],
    ['Maximum risk per idea ($)', '750.25'],
    ['Fees per contract ($)', '0.65'],
    ['Trader basket weights', 'SPY=0.6, QQQ=0.2, IWM=0.2'],
    ['Index basket weights', 'SPX=0.4, NDX=0.4, IWM=0.2'],
  ] as const;
  const original = await Promise.all(
    changed.map(
      async ([label]) =>
        [label, await panel.getByLabel(label, { exact: true }).inputValue()] as const,
    ),
  );
  try {
    await expect(panel.getByLabel('API requests per second', { exact: true })).toHaveAttribute(
      'min',
      '0.1',
    );
    await expect(panel.getByLabel('API requests per second', { exact: true })).toHaveAttribute(
      'step',
      '0.1',
    );
    for (const [label, value] of changed)
      await panel.getByLabel(label, { exact: true }).fill(value);
    const save = page.waitForRequest(
      (request) =>
        request.url().endsWith('/api/rpc') && request.postDataJSON()?.method === 'save_settings',
    );
    await panel.getByRole('button', { name: 'Save settings', exact: true }).click();
    const payload = (await save).postDataJSON().args[0];
    expect(payload.weights_index_basket).toEqual(payload.weights_whale);
    expect(payload.maximum_risk_dollars).toBe(750.25);
    await expect(panel.getByText('Settings saved.', { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByTestId('demo-banner')).toBeVisible();
    await navigate(page, 'Settings');
    await panel.getByText('Execution & regime settings', { exact: true }).click();
    for (const [label, value] of changed)
      await expect(panel.getByLabel(label, { exact: true })).toHaveValue(value);
  } finally {
    for (const [label, value] of original)
      await panel.getByLabel(label, { exact: true }).fill(value);
    await panel.getByRole('button', { name: 'Save settings', exact: true }).click();
    await expect(panel.getByText('Settings saved.', { exact: true })).toBeVisible();
  }
});
