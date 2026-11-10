import { Badge, Empty, Field, Metric, Notice, Panel } from './components';
import { millions, number, pretty } from './models';
import type { EnvelopeHistoryPoint, EnvelopePair, EnvelopeSource, OptionEnvelopes } from './types';

const availableTime = (value: string) =>
  Date.parse(`${value.replace(' ', 'T').replace(/(?:Z|[+-]\d{2}:?\d{2})$/, '')}Z`) / 1000;

export function envelopePairAt(
  latest: EnvelopePair | undefined,
  history: EnvelopeHistoryPoint[] | undefined,
  asOf?: number,
): EnvelopePair | undefined {
  if (asOf === undefined) return latest;
  return (
    [...(history ?? [])]
      // TRACE chart coordinates use the stored local wall clock. The backend
      // separately retains canonical UTC availability for causal validation.
      .filter((point) => availableTime(point.as_of) <= asOf)
      .sort((a, b) => availableTime(a.as_of) - availableTime(b.as_of))
      .at(-1)
  );
}

export function envelopeForSource(
  envelopes: EnvelopePair | undefined,
  source: EnvelopeSource,
): OptionEnvelopes | undefined {
  return source === 'volume_gamma' ? envelopes?.flow : envelopes?.structural;
}

export function EnvelopePanel({
  envelopes,
  source,
  onSourceChange,
  compact = false,
  title = 'Option Envelopes',
}: {
  envelopes?: EnvelopePair;
  source: EnvelopeSource;
  onSourceChange?: (source: EnvelopeSource) => void;
  compact?: boolean;
  title?: string;
}) {
  const selected = envelopeForSource(envelopes, source);
  const body = !selected ? (
    <Empty title="Option Envelopes unavailable">
      This payload predates the envelope research contract.
    </Empty>
  ) : (
    <>
      <div className="research-metric-grid">
        <Metric label="Zero Gamma" value={number(selected.zero_gamma)} />
        <Metric
          label="Major Gamma"
          value={number(selected.major_gamma)}
          detail={`Strength ${millions(selected.major_strength)}`}
        />
        <Metric
          label="Minor Gamma"
          value={number(selected.minor_gamma)}
          detail={`Strength ${millions(selected.minor_strength)}`}
        />
        <Metric
          label="Coverage"
          value={`${selected.coverage.usable_contracts} / ${selected.coverage.contracts}`}
          detail="usable contracts"
        />
      </div>
      <p className="source-note">
        {source === 'volume_gamma'
          ? 'Flow proxy · same-session cumulative option volume weighted gamma.'
          : 'Structural proxy · reported open interest weighted gamma.'}{' '}
        Collected strikes {number(selected.coverage.min_strike)}–
        {number(selected.coverage.max_strike)}.
      </p>
      {!!selected.warnings.length && (
        <Notice tone="warning">{selected.warnings.join(' · ')}</Notice>
      )}
    </>
  );
  if (compact) return <div className="envelope-panel-body">{body}</div>;
  return (
    <Panel
      title={title}
      eyebrow="Observational research"
      action={
        <div className="envelope-actions">
          {selected && (
            <Badge tone={selected.status === 'ready' ? 'positive' : 'warning'}>
              {pretty(selected.status)}
            </Badge>
          )}
          <Field label="Envelope source">
            <select
              value={source}
              onChange={(event) => onSourceChange?.(event.target.value as EnvelopeSource)}
              disabled={!onSourceChange}
            >
              <option value="volume_gamma">Flow · volume proxy</option>
              <option value="oi_gamma">Structural · OI proxy</option>
            </select>
          </Field>
        </div>
      }
    >
      {body}
    </Panel>
  );
}
