export type NullableNumber = number | null;
export type Theme = 'dark' | 'light';
export type View = 'cockpit' | 'edge' | 'regime' | 'trace' | 'matrix' | 'oneoff' | 'settings';

export interface Settings {
  refresh_interval: number;
  theme: Theme;
  symbols: string[];
  api_rate_limit_per_second: number;
  api_rate_limit_utilization: number;
  min_poll_interval_seconds: number;
  max_poll_interval_seconds: number;
  raw_retention_days: number;
  weights?: Record<string, number>;
  weights_whale?: Record<string, number>;
  weights_index_basket?: Record<string, number>;
  maximum_risk_dollars?: number;
  fees_per_contract?: number;
  feature_flags?: Record<string, boolean>;
  auto_start_collector?: boolean;
  auto_start_ninjatrader?: boolean;
  ninjatrader_port?: number;
}
export interface Snapshot {
  id: number;
  symbol: string;
  timestamp: string;
  expiration_date?: string | null;
  spot_price: number;
  total_net_gex: number;
  total_call_gex?: number;
  total_put_gex?: number;
  flip_strike?: NullableNumber;
  max_call_gex_strike?: NullableNumber;
  max_put_gex_strike?: NullableNumber;
  gex_slope?: NullableNumber;
  effective_gex?: NullableNumber;
}
export interface ProfileRow {
  strike_price: number;
  option_type: string;
  gex_value: number;
  open_interest?: number | null;
}
export interface HistoryRow {
  timestamp: string;
  total_net_gex: number;
  spot_price?: number;
}
export interface GammaSweep {
  status: string;
  reason?: string;
  warning?: string;
  model?: string;
  points?: { spot: number; net_gex: number; hedge_shares?: number }[];
  zero_crossings?: { all?: number[]; below: NullableNumber; above: NullableNumber };
  current?: { spot: number; net_gex: number; hedge_shares: number };
  range?: { min: number; max: number; step?: number };
  skipped_contracts?: Record<string, number>;
}
export interface Dashboard {
  snapshot: Snapshot;
  profile: ProfileRow[];
  history: HistoryRow[];
  gamma_sweep?: GammaSweep;
  positioning?: Positioning | null;
}
export interface PositioningStrike {
  strike: number;
  call_oi_gex: number;
  put_oi_gex: number;
  gross_oi_gex: number;
  net_oi_proxy: number;
  call_activity_5m: NullableNumber;
  put_activity_5m: NullableNumber;
  activity_5m: NullableNumber;
  call_activity_15m: NullableNumber;
  put_activity_15m: NullableNumber;
  activity_15m: NullableNumber;
  oi_persistence: NullableNumber;
}
export interface Positioning {
  model: 'oi_activity_v1';
  as_of: string;
  dealer_direction: 'unknown';
  status: 'ready' | 'partial' | 'unavailable';
  warnings: string[];
  windows_minutes: [5, 15];
  coverage: {
    observed_minutes: number;
    max_gap_seconds: NullableNumber;
    contracts: number;
    valid_activity_contracts: number;
  };
  strikes: PositioningStrike[];
  top_levels: number[];
}
export interface Quality {
  score?: number;
  label?: string;
  warnings?: string[];
  age_seconds?: NullableNumber;
}
export interface Compass {
  label?: string;
  strategy?: string;
  composition?: string;
  x_score?: number;
  y_score?: number;
  confidence?: number;
  data_quality?: Quality;
  warnings?: string[];
}
export interface Component {
  symbol: string;
  spot?: number;
  flip_strike?: NullableNumber;
  distance_pct?: number;
  net_gex?: number;
  effective_gex?: number;
  gex_imbalance?: number;
  acceleration?: number;
  data_quality?: Quality;
  confidence?: number;
  age_seconds?: NullableNumber;
  flip_quality?: string;
  warnings?: string[];
}
export interface Vote {
  score?: number;
  detail?: string;
}
export interface Decision {
  bias?: string;
  score?: number;
  context?: string;
  target?: NullableNumber;
  invalidation?: NullableNumber;
  flip?: NullableNumber;
  market_vote?: Vote;
  dealer_vote?: Vote;
  liquidity_vote?: Vote;
}
export interface Scenario {
  scenario_id?: string;
  scenario_type?: string;
  eligible?: boolean;
  bias?: string;
  reasons?: string[];
  warnings?: string[];
  trigger_zone?: { low: number; high: number } | null;
  target_zone?: { low: number; high: number } | null;
  invalidation_zone?: { low: number; high: number } | null;
}
export interface MarketContext {
  event_risk?: { state?: string };
  implied_move?: NullableNumber;
  range_consumed?: NullableNumber;
  realized_volatility_15m?: NullableNumber;
  cross_asset_state?: string;
  warnings?: string[];
}
export interface Candidate {
  status?: string;
  side?: string;
  reason?: string;
  rationale?: string;
  liquidity_grade?: string | null;
  estimated_debit?: NullableNumber;
  max_profit?: NullableNumber;
  estimated_debit_dollars?: NullableNumber;
  lower?: NullableNumber;
  center?: NullableNumber;
  upper?: NullableNumber;
  long_strike?: NullableNumber;
  short_strike?: NullableNumber;
  lower_breakeven?: NullableNumber;
  upper_breakeven?: NullableNumber;
  breakeven?: NullableNumber;
  target?: NullableNumber;
  max_loss_dollars?: NullableNumber;
  max_reward_dollars?: NullableNumber;
  contracts?: number;
  settlement_type?: string;
  time_to_close?: string;
  quote?: {
    bid?: NullableNumber;
    mid?: NullableNumber;
    ask?: NullableNumber;
    age_seconds?: NullableNumber;
  };
}
export interface EdgeHorizon {
  horizon_minutes: number;
  sample_size?: number;
  win_rate?: NullableNumber;
  median_move_points?: NullableNumber;
  median_favorable_points?: NullableNumber;
  median_adverse_points?: NullableNumber;
  sample_label?: string;
  independent_opportunities?: number;
  unique_days?: number;
  evidence_status?: string;
  after_cost_expectancy?: NullableNumber;
  holdout_expectancy?: NullableNumber;
  win_rate_interval?: [number, number] | null;
  target_first_rate?: NullableNumber;
  invalidation_first_rate?: NullableNumber;
}
export interface Workspace {
  schema_version: number;
  symbol: string;
  snapshot_id: number;
  as_of?: string;
  dashboard: Dashboard;
  decision?: Decision;
  active_scenario?: Scenario;
  data_quality?: Quality;
  market_context?: MarketContext;
  regime?: { traders?: Compass; index_basket?: Compass };
  components?: Component[];
  eligibility?: { eligible?: boolean; blockers?: string[]; event_state?: string };
  historical_edge?: { symbol?: string; horizons?: EdgeHorizon[]; primary?: EdgeHorizon } | null;
  execution_candidates?: { ideas?: Record<string, Candidate>; error?: string };
  alerts?: BackendEvent[];
}
export interface Overview {
  compass_traders?: Compass;
  compass?: Compass;
  index_basket?: Compass;
  compass_whale?: Compass;
  components: Component[];
  tilt?: { symbol: string; net_gex: number }[];
}
export type TraceMetric =
  'net_gex' | 'call_gex' | 'put_gex' | 'modeled_delta_pressure' | 'modeled_charm_pressure';
export interface HeatmapRow {
  timestamp: string;
  strike: number;
  net_gex: number;
  call_gex?: number;
  put_gex?: number;
  modeled_delta_pressure?: NullableNumber;
  modeled_charm_pressure?: NullableNumber;
  open_interest?: number;
}
export interface TraceData {
  symbol: string;
  timestamp: string;
  session_date: string;
  spot_price: number;
  flip_strike?: NullableNumber;
  total_net_gex?: number;
  heatmap: HeatmapRow[];
  spot_path?: { timestamp: string; spot_price: number }[];
  spot_ticks?: { timestamp: string; spot_price: number }[];
  history?: HistoryRow[];
  latest_profile?: Omit<HeatmapRow, 'timestamp'>[];
  positioning?: Positioning | null;
  overlays?: {
    timestamp: string;
    overlay_type: string;
    label?: string | null;
    target?: NullableNumber;
    invalidation?: NullableNumber;
  }[];
  modeled_pressure_coverage?: {
    ratio: number;
    matched_contracts?: number;
    total_contracts?: number;
    warning?: string | null;
  };
}
export interface EdgeFilters {
  symbol: string;
  horizon: number;
  include_legacy: boolean;
  scenario?: string;
  regime?: string;
  liquidity_grade?: string;
  time_bucket?: string;
  event_tag?: string;
}
export interface EdgeData {
  symbol: string;
  status: string;
  horizon_minutes: number;
  stats: {
    sample_size?: number;
    evidence_status?: string;
    wins?: number;
    median_move_points?: NullableNumber;
    median_favorable_points?: NullableNumber;
    median_adverse_points?: NullableNumber;
    win_rate_interval?: [number, number] | null;
    target_first_rate?: NullableNumber;
    invalidation_first_rate?: NullableNumber;
    sample_label?: string;
    summary?: string;
    independent_opportunities?: number;
    unique_days?: number;
    after_cost_expectancy?: NullableNumber;
    holdout_expectancy?: NullableNumber;
    win_rate?: NullableNumber;
  };
  warning?: string | null;
  expectancy_distribution: number[];
  outcome_breakdown?: Record<string, number>;
  opportunities: {
    signal_id: number;
    session_date: string;
    emitted_at?: string;
    scenario_id?: string | null;
    scenario_type?: string;
    regime?: string | null;
    liquidity_grade?: string | null;
    outcome: string;
    after_cost_points?: NullableNumber;
    mfe?: NullableNumber;
    mae?: NullableNumber;
  }[];
  calibration?: { forecast: number; outcome: number }[];
}
export interface JournalInput {
  symbol: string;
  session_date: string;
  entry_time: string;
  exit_time?: string | null;
  exit_reason?: string | null;
  contracts: number;
  entry_price: number;
  exit_price: number | null;
  fees: number;
  adhered_to_plan: boolean;
  notes: string;
  scenario_id?: string;
  scenario_type?: string;
  regime?: string;
  liquidity_grade?: string | null;
}
export interface JournalEntry extends JournalInput {
  id: number;
  pnl: NullableNumber;
  exit_reason?: string | null;
}
export interface WeeklyReview {
  week_start: string;
  week_end: string;
  groups: { dimension: string; value: string; pnl: number; trades: number }[];
  entries?: JournalEntry[];
}
export interface SavedProfile {
  snapshot_id: number;
  symbol: string;
  expiration_date: string;
  total_net_gex: number;
  contract_count: number;
  timestamp?: string;
}
export interface RuntimeInfo {
  mode: string;
  demo?: boolean;
  data_dir: string;
  credentials_path?: string;
  env_path?: string;
  settings_path?: string;
  database_path?: string;
  collector?: { running: boolean; pid: number | null };
  ninjatrader?: { running: boolean; port: number | null };
  event_bridge?: { running: boolean; port: number | null };
  collector_running?: boolean;
  ninjatrader_running?: boolean;
  ninjatrader_port?: number;
  credentials_configured?: boolean;
  startup_results?: Record<
    string,
    { status: 'started' | 'disabled' | 'blocked' | 'error' | 'stopped'; message: string }
  >;
  version?: string;
  source?: string;
}
export interface BackendStatus {
  ok: boolean;
  run_status?: string | null;
  run_message?: string | null;
  run_age_seconds?: NullableNumber;
  snapshot_age_seconds?: NullableNumber;
  latest_snapshot_at?: string | null;
  latest_symbol?: string | null;
  collector_running?: boolean;
  event_bridge?: { status?: string; event_count?: number; last_event_type?: string };
}
export interface BackendEvent {
  id?: number | string;
  type?: string;
  timestamp?: string;
  created_at?: string;
  received_at?: string;
  message?: string;
  alert_type?: string;
  payload?: { symbol?: string; message?: string; alert_type?: string; [key: string]: unknown };
  data?: Record<string, unknown>;
}
export interface ActionResult {
  ok: boolean;
  message?: string;
}
export interface ApiMap {
  get_symbols: { args: []; result: string[] };
  get_settings: { args: []; result: Settings };
  save_settings: { args: [Settings]; result: ActionResult & { settings?: Settings } };
  get_backend_status: { args: []; result: BackendStatus };
  get_runtime_info: { args: []; result: RuntimeInfo };
  get_events: { args: []; result: BackendEvent[] };
  get_decision_workspace: { args: [string]; result: Workspace };
  get_market_overview: { args: []; result: Overview };
  get_trace_dates: { args: [string, number]; result: string[] };
  get_trace_data: { args: [string, number, string | null]; result: TraceData };
  get_edge_lab: { args: [EdgeFilters]; result: EdgeData };
  get_journal_entries: { args: [string | null]; result: JournalEntry[] };
  get_weekly_journal_review: { args: [string | null]; result: WeeklyReview };
  create_journal_entry: { args: [JournalInput]; result: JournalEntry };
  update_journal_entry: { args: [number, Partial<JournalInput>]; result: JournalEntry };
  delete_journal_entry: { args: [number]; result: boolean };
  get_one_off_profiles: { args: []; result: SavedProfile[] };
  get_one_off_profile: { args: [number]; result: Dashboard };
  build_one_off_profile: {
    args: [string, string];
    result: ActionResult & { data?: Dashboard; db_path?: string };
  };
  start_collector: { args: []; result: ActionResult };
  stop_collector: { args: []; result: ActionResult };
  start_ninjatrader: { args: [number]; result: ActionResult };
  stop_ninjatrader: { args: []; result: ActionResult };
}
