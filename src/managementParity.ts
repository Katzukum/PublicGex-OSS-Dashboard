import type { JournalInput, Settings, Workspace } from './types';

export function formatWeights(weights: Record<string, number> | undefined): string {
  return Object.entries(weights ?? {})
    .map(([symbol, weight]) => `${symbol}=${weight}`)
    .join(', ');
}

export function parseWeights(value: string, label: string): Record<string, number> {
  const result: Record<string, number> = {};
  for (const item of value
    .split(/[,\n]/)
    .map((part) => part.trim())
    .filter(Boolean)) {
    const pieces = item.split('=');
    const symbol = pieces[0]?.trim().toUpperCase() ?? '';
    const raw = pieces[1]?.trim() ?? '';
    const weight = Number(raw);
    if (
      pieces.length !== 2 ||
      !/^[A-Z0-9][A-Z0-9.-]{0,11}$/.test(symbol) ||
      !raw ||
      !Number.isFinite(weight) ||
      weight < 0
    )
      throw new Error(`${label}: use SYMBOL=weight pairs with nonnegative numbers.`);
    if (Object.hasOwn(result, symbol))
      throw new Error(`${label}: ${symbol} appears more than once.`);
    result[symbol] = weight;
  }
  if (
    !Object.keys(result).length ||
    Object.values(result).reduce((sum, weight) => sum + weight, 0) <= 0
  )
    throw new Error(`${label}: include at least one positive weight.`);
  return result;
}

export function settingsWithWeights(settings: Settings, traders: string, basket: string): Settings {
  const weights = parseWeights(traders, 'Trader basket');
  const indexWeights = parseWeights(basket, 'Index basket');
  return {
    ...settings,
    weights,
    weights_index_basket: indexWeights,
    weights_whale: { ...indexWeights },
  };
}

export function journalContext(
  workspace: Workspace | undefined,
  symbol: string,
  session: string,
): Partial<JournalInput> {
  if (workspace?.symbol !== symbol || workspace.as_of?.slice(0, 10) !== session) return {};
  const executable = Object.values(workspace.execution_candidates?.ideas ?? {}).find(
    (idea) => idea.status === 'EXECUTABLE',
  );
  return {
    scenario_id: workspace.active_scenario?.scenario_id,
    scenario_type: workspace.active_scenario?.scenario_type,
    regime: workspace.regime?.traders?.label,
    liquidity_grade: executable?.liquidity_grade ?? null,
  };
}

export function journalPrefill(workspace: Workspace | undefined, symbol: string, now: string) {
  return {
    session_date:
      workspace?.symbol === symbol && workspace.as_of
        ? workspace.as_of.slice(0, 10)
        : now.slice(0, 10),
    entry_time: now,
  };
}

export function savedTime(value: string | undefined): string {
  if (!value) return 'Time unavailable';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
