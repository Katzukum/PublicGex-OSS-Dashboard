import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from './api';
import { Badge, Empty, Field, Metric, Notice, Panel, RequestState, Toggle } from './components';
import { COLORS } from './charts';
import { CategoryChart, DistributionChart } from './BasicCharts';
import { useAction, useRemote } from './hooks';
import { localDateTime, money, number, percent, pretty, validateJournalNumbers } from './models';
import { journalContext, journalPrefill, savedTime } from './managementParity';
import './managementParity.css';
import type { EdgeFilters, JournalEntry, JournalInput, Theme, Workspace } from './types';

interface JournalForm {
  session_date: string;
  entry_time: string;
  exit_time: string;
  contracts: string;
  entry_price: string;
  exit_price: string;
  fees: string;
  notes: string;
  exit_reason: string;
  adhered_to_plan: boolean;
}
function blankJournal(): JournalForm {
  const now = localDateTime();
  return {
    session_date: now.slice(0, 10),
    entry_time: now,
    exit_time: '',
    contracts: '1',
    entry_price: '',
    exit_price: '',
    fees: '0',
    notes: '',
    exit_reason: '',
    adhered_to_plan: false,
  };
}
function Journal({
  symbol,
  workspace,
  revision,
  focus,
  active,
}: {
  symbol: string;
  workspace: Workspace | undefined;
  revision: number;
  focus: number;
  active: boolean;
}) {
  const [form, setForm] = useState<JournalForm>(blankJournal),
    [editing, setEditing] = useState<JournalEntry>(),
    [deleteId, setDeleteId] = useState<number>(),
    [week, setWeek] = useState('');
  const entries = useRemote('get_journal_entries', [symbol || null], {
    enabled: active && !!symbol,
    revision,
  });
  const review = useRemote('get_weekly_journal_review', [week || null], {
    enabled: active,
    revision,
  });
  const action = useAction();
  const handledFocus = useRef(0);
  const draftSymbol = useRef(symbol);
  const drafts = useRef(new Map<string, { form: JournalForm; editing?: JournalEntry }>());
  useEffect(() => {
    if (draftSymbol.current === symbol) return;
    drafts.current.set(draftSymbol.current, { form, editing });
    draftSymbol.current = symbol;
    const previous = drafts.current.get(symbol);
    setEditing(previous?.editing);
    setForm(previous?.form ?? blankJournal());
    setDeleteId(undefined);
  }, [symbol]);
  useEffect(() => {
    if (active && focus > handledFocus.current && workspace?.symbol === symbol) {
      handledFocus.current = focus;
      setEditing(undefined);
      setForm((previous) => ({
        ...previous,
        ...journalPrefill(workspace, symbol, localDateTime()),
      }));
      document
        .getElementById('journal-form')
        ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [active, focus, workspace, symbol]);
  const update = <K extends keyof JournalForm>(key: K, value: JournalForm[K]) =>
    setForm((previous) => ({ ...previous, [key]: value }));
  const save = async (event: FormEvent) => {
    event.preventDefault();
    await action.run(
      async () => {
        const contracts = Number(form.contracts),
          entry = Number(form.entry_price),
          exit = form.exit_price === '' ? null : Number(form.exit_price),
          fees = Number(form.fees);
        const validation = validateJournalNumbers(contracts, entry, exit, fees);
        if (validation) throw new Error(validation);
        if (!form.session_date || !form.entry_time || form.entry_price === '')
          throw new Error('Session date, entry time, and entry price are required.');
        if (form.exit_time && form.exit_time < form.entry_time)
          throw new Error('Exit time cannot precede entry time.');
        const payload: JournalInput = {
          symbol,
          session_date: form.session_date,
          entry_time: form.entry_time,
          exit_time: form.exit_time || null,
          exit_reason: form.exit_reason.trim() || null,
          contracts,
          entry_price: entry,
          exit_price: exit,
          fees,
          adhered_to_plan: form.adhered_to_plan,
          notes: form.notes,
        };
        if (!editing) Object.assign(payload, journalContext(workspace, symbol, form.session_date));
        const saved = editing
          ? await api('update_journal_entry', [editing.id, payload])
          : await api('create_journal_entry', [payload]);
        setEditing(undefined);
        setForm((previous) =>
          editing
            ? blankJournal()
            : {
                ...previous,
                entry_price: '',
                exit_price: '',
                exit_time: '',
                exit_reason: '',
                notes: '',
              },
        );
        entries.reload();
        review.reload();
        return saved;
      },
      editing ? 'Journal entry updated.' : 'Journal entry saved locally.',
    );
  };
  const edit = (entry: JournalEntry) => {
    setEditing(entry);
    setForm({
      session_date: entry.session_date,
      entry_time: entry.entry_time.slice(0, 16),
      exit_time: entry.exit_time?.slice(0, 16) || '',
      contracts: String(entry.contracts),
      entry_price: String(entry.entry_price),
      exit_price: entry.exit_price == null ? '' : String(entry.exit_price),
      fees: String(entry.fees),
      notes: entry.notes || '',
      exit_reason: entry.exit_reason || '',
      adhered_to_plan: entry.adhered_to_plan,
    });
    document.getElementById('journal-form')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const context = editing ?? journalContext(workspace, symbol, form.session_date);
  return (
    <>
      <Panel
        title="Manual execution journal"
        eyebrow="Your actual fills · stored locally"
        action={<Badge>{symbol}</Badge>}
      >
        <form id="journal-form" onSubmit={(event) => void save(event)} className="journal-form">
          {editing && (
            <Notice tone="info">
              Editing entry #{editing.id}
              <button
                type="button"
                className="text-button"
                onClick={() => {
                  setEditing(undefined);
                  setForm(blankJournal());
                }}
              >
                Cancel edit
              </button>
            </Notice>
          )}
          <div className="journal-context">
            <span className="muted">Saved context:</span>
            <Badge>
              {context.scenario_type ? pretty(context.scenario_type) : 'No matching scenario'}
            </Badge>
            {context.regime && <Badge>{pretty(context.regime)}</Badge>}
            {context.liquidity_grade && <Badge>Liquidity {context.liquidity_grade}</Badge>}
          </div>
          <div className="form-grid four">
            <Field label="Session date">
              <input
                type="date"
                value={form.session_date}
                required
                onChange={(e) => update('session_date', e.target.value)}
              />
            </Field>
            <Field label="Entry time">
              <input
                type="datetime-local"
                value={form.entry_time}
                required
                onChange={(e) => update('entry_time', e.target.value)}
              />
            </Field>
            <Field label="Contracts">
              <input
                type="number"
                min="1"
                step="1"
                value={form.contracts}
                required
                onChange={(e) => update('contracts', e.target.value)}
              />
            </Field>
            <Field label="Entry price">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.entry_price}
                required
                placeholder="0.00"
                onChange={(e) => update('entry_price', e.target.value)}
              />
            </Field>
            <Field label="Exit price" hint="Leave blank for an open entry">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.exit_price}
                onChange={(e) => update('exit_price', e.target.value)}
              />
            </Field>
            <Field label="Exit time" hint="Optional">
              <input
                type="datetime-local"
                value={form.exit_time}
                onChange={(e) => update('exit_time', e.target.value)}
              />
            </Field>
            <Field label="Exit reason" hint="Optional">
              <input
                value={form.exit_reason}
                onChange={(e) => update('exit_reason', e.target.value)}
                placeholder="Target, invalidation, discretionary…"
              />
            </Field>
            <Field label="Fees">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.fees}
                required
                onChange={(e) => update('fees', e.target.value)}
              />
            </Field>
            <div className="field">
              <span>Plan adherence</span>
              <Toggle
                checked={form.adhered_to_plan}
                onChange={(v) => update('adhered_to_plan', v)}
                label="Followed my plan"
              />
            </div>
          </div>
          <Field label="Journal notes">
            <textarea
              rows={2}
              value={form.notes}
              placeholder="What was the setup? What did you learn?"
              onChange={(e) => update('notes', e.target.value)}
            />
          </Field>
          <div className="split">
            <p className="source-note">
              Actual-fill P&amp;L is separate from modeled opportunity outcomes.
            </p>
            <button type="submit" className="primary" disabled={action.busy || !symbol}>
              {action.busy ? 'Saving…' : editing ? 'Update journal entry' : 'Save journal entry'}
            </button>
          </div>
          {action.error && <Notice tone="error">{action.error}</Notice>}
          {action.message && <Notice tone="success">{action.message}</Notice>}
        </form>
        <RequestState {...entries} hasData={!!entries.data} />
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Entry</th>
                <th>Scenario</th>
                <th>Contracts</th>
                <th>Entry / exit</th>
                <th>P&amp;L</th>
                <th>Adherence</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {entries.data?.map((entry) => (
                <tr key={entry.id}>
                  <td>
                    {entry.entry_time.replace('T', ' ').slice(0, 16)}
                    {entry.notes && <small className="cell-note">{entry.notes}</small>}
                  </td>
                  <td>
                    {pretty(entry.scenario_type)}
                    {entry.regime && <small className="cell-note">{pretty(entry.regime)}</small>}
                    {entry.liquidity_grade && (
                      <small className="cell-note">Liquidity {entry.liquidity_grade}</small>
                    )}
                  </td>
                  <td>{entry.contracts}</td>
                  <td>
                    {number(entry.entry_price)} / {number(entry.exit_price)}
                    {entry.exit_reason && <small className="cell-note">{entry.exit_reason}</small>}
                  </td>
                  <td className={(entry.pnl ?? 0) >= 0 ? 'positive-text' : 'negative-text'}>
                    {entry.pnl == null ? 'Open' : money(entry.pnl, 2)}
                  </td>
                  <td>{entry.adhered_to_plan ? 'Followed' : 'Deviated'}</td>
                  <td>
                    <div className="row-actions">
                      <button aria-label={`Edit entry ${entry.id}`} onClick={() => edit(entry)}>
                        Edit
                      </button>
                      <button
                        aria-label={`Delete entry ${entry.id}`}
                        onClick={() => setDeleteId(entry.id)}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!entries.loading && !entries.data?.length && (
          <p className="panel-padding muted">No execution entries for this instrument.</p>
        )}
        {deleteId !== undefined && (
          <Notice tone="warning">
            <span>Delete journal entry #{deleteId}? This removes the local record.</span>
            <div className="row-actions">
              <button onClick={() => setDeleteId(undefined)}>Keep entry</button>
              <button
                className="danger"
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await api('delete_journal_entry', [deleteId]);
                    setDeleteId(undefined);
                    entries.reload();
                    review.reload();
                  }, 'Journal entry deleted.')
                }
              >
                Confirm delete
              </button>
            </div>
          </Notice>
        )}
      </Panel>
      <Panel
        title="Weekly execution review"
        eyebrow="Actual results across all instruments, grouped by context"
        action={
          <Field label="Review week">
            <input type="date" value={week} onChange={(event) => setWeek(event.target.value)} />
          </Field>
        }
      >
        <RequestState {...review} hasData={!!review.data} />
        {review.data && (
          <p className="panel-padding muted">
            {review.data.week_start} — {review.data.week_end}
          </p>
        )}
        <div className="review-grid">
          {review.data?.groups.map((group, index) => (
            <div className="review-card" key={`${group.dimension}:${group.value}:${index}`}>
              <span>
                {pretty(group.dimension)} · {pretty(group.value)}
              </span>
              <strong className={group.pnl >= 0 ? 'positive-text' : 'negative-text'}>
                {money(group.pnl, 2)}
              </strong>
              <small>
                {group.trades} user {group.trades === 1 ? 'trade' : 'trades'}
              </small>
            </div>
          ))}
        </div>
        {!review.data?.groups.length && !review.loading && (
          <Empty title="No trades in this week">
            Record actual fills to compare results by scenario, regime, and adherence.
          </Empty>
        )}
      </Panel>
    </>
  );
}

export default function EdgeLab({
  symbol,
  workspace,
  theme,
  revision,
  focus,
  active = true,
}: {
  symbol: string;
  workspace: Workspace | undefined;
  theme: Theme;
  revision: number;
  focus: number;
  active?: boolean;
}) {
  const [horizon, setHorizon] = useState(30),
    [legacy, setLegacy] = useState(false),
    [scenario, setScenario] = useState(''),
    [regime, setRegime] = useState(''),
    [liquidity, setLiquidity] = useState(''),
    [timeBucket, setTimeBucket] = useState(''),
    [eventTag, setEventTag] = useState('');
  const filters: EdgeFilters = {
    symbol,
    horizon,
    include_legacy: legacy,
    ...(scenario ? { scenario } : {}),
    ...(regime ? { regime } : {}),
    ...(liquidity ? { liquidity_grade: liquidity } : {}),
    ...(timeBucket ? { time_bucket: timeBucket } : {}),
    ...(eventTag.trim() ? { event_tag: eventTag.trim() } : {}),
  };
  const remote = useRemote('get_edge_lab', [filters], { enabled: active && !!symbol, revision });
  const data = remote.data;
  return (
    <div className="view-stack">
      <Panel
        title="Historical evidence"
        eyebrow="Edge Lab"
        action={
          <Badge tone={data?.status === 'CALIBRATED' ? 'positive' : 'warning'}>
            {pretty(data?.status || 'INSUFFICIENT')}
          </Badge>
        }
      >
        <div className="toolbar">
          <Field label="Evidence horizon">
            <select value={horizon} onChange={(event) => setHorizon(Number(event.target.value))}>
              <option value="15">15 minutes</option>
              <option value="30">30 minutes</option>
              <option value="60">60 minutes</option>
            </select>
          </Field>
          <Field label="Scenario filter">
            <select value={scenario} onChange={(event) => setScenario(event.target.value)}>
              <option value="">All scenarios</option>
              {['PIN_MEAN_REVERSION', 'UPSIDE_EXPANSION', 'DOWNSIDE_EXPANSION', 'NO_TRADE'].map(
                (item) => (
                  <option key={item} value={item}>
                    {pretty(item)}
                  </option>
                ),
              )}
            </select>
          </Field>
          <Field label="Regime filter">
            <input
              value={regime}
              placeholder="All regimes"
              onChange={(event) => setRegime(event.target.value)}
            />
          </Field>
          <Field label="Liquidity filter">
            <select value={liquidity} onChange={(event) => setLiquidity(event.target.value)}>
              <option value="">All grades</option>
              {['A', 'B', 'C', 'REJECTED'].map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </Field>
          <Field label="Time of day">
            <select value={timeBucket} onChange={(e) => setTimeBucket(e.target.value)}>
              <option value="">Full session</option>
              <option value="open">Open</option>
              <option value="midday">Midday</option>
              <option value="close">Close</option>
            </select>
          </Field>
          <Field label="Event tag filter" hint="Exact calendar event tag">
            <input
              value={eventTag}
              placeholder="All events"
              onChange={(event) => setEventTag(event.target.value)}
            />
          </Field>
          <Toggle checked={legacy} onChange={setLegacy} label="Legacy diagnostics" />
          <button
            type="button"
            onClick={() => {
              setHorizon(30);
              setLegacy(false);
              setScenario('');
              setRegime('');
              setLiquidity('');
              setTimeBucket('');
              setEventTag('');
            }}
          >
            Reset filters
          </button>
        </div>
        <RequestState {...remote} hasData={!!data} />
        {data?.warning && <Notice tone="warning">{data.warning}</Notice>}
        <div className="metrics-strip embedded">
          <Metric
            label="Independent opportunities"
            value={number(data?.stats.independent_opportunities, 0)}
          />
          <Metric label="Unique sessions" value={number(data?.stats.unique_days, 0)} />
          <Metric
            label="After-cost expectancy"
            value={number(data?.stats.after_cost_expectancy)}
            detail="points per opportunity"
          />
          <Metric
            label="Holdout expectancy"
            value={number(data?.stats.holdout_expectancy)}
            detail="walk-forward tail"
          />
        </div>
        <div className="evidence-detail-grid">
          <Metric
            label="Win rate"
            value={percent(data?.stats.win_rate)}
            detail={
              data?.stats.win_rate_interval
                ? `Interval ${percent(data.stats.win_rate_interval[0])} – ${percent(data.stats.win_rate_interval[1])}`
                : 'Insufficient evidence for an interval'
            }
          />
          <Metric label="Target first" value={percent(data?.stats.target_first_rate)} />
          <Metric label="Invalidation first" value={percent(data?.stats.invalidation_first_rate)} />
          <Metric
            label="Median move"
            value={number(data?.stats.median_move_points)}
            detail="underlying points"
          />
          <Metric
            label="Median favorable / adverse"
            value={`${number(data?.stats.median_favorable_points)} / ${number(data?.stats.median_adverse_points)}`}
            detail="underlying points"
          />
        </div>
        {data?.stats.summary && <p className="source-note">{data.stats.summary}</p>}
        <p className="source-note">
          These are historical independent opportunities, not live predictions or account execution
          results.
        </p>
      </Panel>
      <div className="two-col">
        <Panel title="Outcome distribution" eyebrow="After transaction-cost estimate">
          {data?.expectancy_distribution.length ? (
            <DistributionChart theme={theme} values={data.expectancy_distribution} />
          ) : (
            <Empty title="Evidence is still building">
              No completed opportunities match these filters.
            </Empty>
          )}
        </Panel>
        <Panel title="Outcome breakdown" eyebrow="Resolved observations">
          {data?.outcome_breakdown && Object.values(data.outcome_breakdown).some((v) => v > 0) ? (
            <CategoryChart
              theme={theme}
              label="Historical outcome categories"
              height={300}
              rows={Object.entries(data.outcome_breakdown).map(([name, value], index) => ({
                name: pretty(name),
                value,
                color: [
                  COLORS.positive,
                  COLORS.negative,
                  COLORS.accent,
                  COLORS.negative,
                  COLORS.amber,
                ][index],
              }))}
              yTitle="Opportunities"
            />
          ) : (
            <Empty title="No resolved outcomes">
              Historical outcomes appear as qualifying observations mature.
            </Empty>
          )}
        </Panel>
      </div>
      <Panel title="Independent opportunities" eyebrow={`${horizon}-minute outcomes`}>
        <div className="table-scroll compact-table">
          <table>
            <thead>
              <tr>
                <th>Session</th>
                <th>Scenario</th>
                <th>Regime / liquidity</th>
                <th>Outcome</th>
                <th>After cost</th>
                <th>MFE</th>
                <th>MAE</th>
              </tr>
            </thead>
            <tbody>
              {data?.opportunities.map((row, index) => (
                <tr key={`${row.signal_id}:${index}`}>
                  <td>
                    {row.session_date}
                    {row.emitted_at && (
                      <small className="cell-note">{savedTime(row.emitted_at)}</small>
                    )}
                  </td>
                  <td>{pretty(row.scenario_type)}</td>
                  <td>
                    {pretty(row.regime)}
                    {row.liquidity_grade && (
                      <small className="cell-note">Liquidity {row.liquidity_grade}</small>
                    )}
                  </td>
                  <td>
                    <Badge>{pretty(row.outcome)}</Badge>
                  </td>
                  <td
                    className={
                      (row.after_cost_points ?? 0) >= 0 ? 'positive-text' : 'negative-text'
                    }
                  >
                    {number(row.after_cost_points)}
                  </td>
                  <td>{number(row.mfe)}</td>
                  <td>{number(row.mae)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!data?.opportunities.length && (
          <p className="panel-padding muted">No matching opportunities.</p>
        )}
      </Panel>
      <Journal
        symbol={symbol}
        workspace={workspace}
        revision={revision}
        focus={focus}
        active={active}
      />
    </div>
  );
}
