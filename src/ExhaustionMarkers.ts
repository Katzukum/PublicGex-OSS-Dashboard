import type { SeriesMarker } from 'lightweight-charts';
import type { ExhaustionEvent } from './types';

const eventTime = (value: string) =>
  Date.parse(`${value.replace(' ', 'T').replace(/(?:Z|[+-]\d{2}:?\d{2})$/, '')}Z`) / 1000;

const COLORS: Record<`${ExhaustionEvent['side']}_${ExhaustionEvent['action']}`, string> = {
  CALL_BUY: '#39c7a0',
  CALL_SELL: '#e7bc76',
  PUT_BUY: '#89a8ff',
  PUT_SELL: '#f07987',
};

export function visibleExhaustionEvents(events: ExhaustionEvent[] | undefined, asOf?: number) {
  return (events ?? []).filter((event) => asOf === undefined || eventTime(event.timestamp) <= asOf);
}

export function exhaustionMarkers(
  events: ExhaustionEvent[] | undefined,
  asOf?: number,
): SeriesMarker<number>[] {
  return visibleExhaustionEvents(events, asOf)
    .map((event) => ({
      id: event.id,
      time: eventTime(event.timestamp),
      position: event.side === 'CALL' ? ('aboveBar' as const) : ('belowBar' as const),
      shape: event.hedge_direction > 0 ? ('arrowUp' as const) : ('arrowDown' as const),
      color: COLORS[`${event.side}_${event.action}`],
      text: `${event.side[0]}${event.action[0]} ${Math.round(event.score * 100)}`,
      size: 1.25,
    }))
    .sort((a, b) => a.time - b.time || String(a.id).localeCompare(String(b.id)));
}

export function exhaustionEvidence(event: ExhaustionEvent) {
  const envelope = event.envelope;
  const evidence = event.evidence.map((name) => name.replaceAll('_', ' '));
  return [
    event.label,
    `Flow percentile ${Math.round(event.flow_percentile * 100)}%`,
    `5-bar range position ${event.price_position === null ? 'unavailable' : `${Math.round(event.price_position * 100)}%`}`,
    `Classification ${event.classification_ratio === null ? 'unavailable' : `${Math.round(event.classification_ratio * 100)}%`}`,
    envelope?.nearest_kind && envelope.nearest_level != null
      ? `Nearest Envelope ${envelope.nearest_kind} ${envelope.nearest_level}`
      : 'Nearest Envelope unavailable',
    `Evidence ${evidence.join(', ') || 'none'}`,
  ];
}
