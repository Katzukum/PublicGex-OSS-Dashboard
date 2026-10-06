import { describe, expect, it, vi } from 'vitest';
import type { CanvasRenderingTarget2D, MediaCoordinatesRenderingScope } from 'fancy-canvas';
import type { Coordinate } from 'lightweight-charts';
import { chartTime } from './chartConstants';
import {
  heatmapColor,
  TraceHeatmapSeries,
  traceHeatmapData,
  traceOverlayData,
  traceReplayRange,
  traceViewportSelection,
} from './traceHeatmap';
import type { HeatmapRow, TraceMetric } from './types';

const at = '2026-09-30 13:30:00';
const rows: HeatmapRow[] = [
  {
    timestamp: at,
    strike: 100,
    net_gex: 2e6,
    call_gex: 3e6,
    put_gex: -1e6,
    modeled_delta_pressure: 5e6,
    modeled_charm_pressure: null,
  },
  {
    timestamp: at,
    strike: 100.5,
    net_gex: -1e6,
    call_gex: 0,
    put_gex: -1e6,
    modeled_delta_pressure: null,
    modeled_charm_pressure: -0.5e6,
  },
  {
    timestamp: '2026-09-30 14:30:00',
    strike: 100,
    net_gex: 0,
    call_gex: 0,
    put_gex: 0,
    modeled_delta_pressure: 0,
    modeled_charm_pressure: 0,
  },
];

describe('Lightweight Charts TRACE data', () => {
  it.each<[TraceMetric, number | null, number | null]>([
    ['net_gex', 2, -1],
    ['call_gex', 3, 0],
    ['put_gex', -1, -1],
    ['modeled_delta_pressure', 5, null],
    ['modeled_charm_pressure', null, -0.5],
  ])('preserves signed %s values and null cells in million units', (metric, first, second) => {
    const { columns } = traceHeatmapData(rows, metric);
    expect(columns).toHaveLength(2);
    expect(columns[0]!.time).toBe(Date.UTC(2026, 8, 30, 13, 30) / 1000);
    expect(columns[0]!.cells.map((cell) => cell.value)).toEqual([first, second]);
    expect(columns[0]!.cells.map((cell) => [cell.low, cell.high])).toEqual([
      [99.75, 100.25],
      [100.25, 100.75],
    ]);
    expect(columns[1]!.cells).toHaveLength(1); // Absent strikes remain absent, not invented zeros.
  });
  it('sorts and deduplicates native series times and rejects malformed coordinates', () => {
    const { columns } = traceHeatmapData(
      [...rows].reverse().concat([
        { timestamp: at, strike: 100, net_gex: 4e6 },
        { timestamp: 'invalid', strike: 100, net_gex: 1 },
        { timestamp: at, strike: NaN, net_gex: 1 },
      ]),
      'net_gex',
    );
    expect(columns.map((column) => column.time)).toEqual([
      chartTime(at),
      chartTime(rows[2]!.timestamp),
    ]);
    expect(columns[0]!.cells[0]!.value).toBe(4);
  });
  it('uses a symmetric color scale even for positive-only or negative-only surfaces', () => {
    expect(heatmapColor(1, 1, 'dark')).toBe('rgb(57, 199, 160)');
    expect(heatmapColor(-1, 1, 'dark')).toBe('rgb(187, 80, 108)');
    expect(heatmapColor(0, 1, 'dark')).toBe('rgb(23, 34, 49)');
    expect(heatmapColor(0, 1, 'light')).toBe('rgb(241, 244, 248)');
  });
  it('the actual custom renderer skips null cells but paints a measured zero', () => {
    const { columns, maxAbs } = traceHeatmapData(rows, 'modeled_charm_pressure');
    const renderer = new TraceHeatmapSeries(maxAbs, 'dark');
    renderer.update({
      bars: columns.map((originalData, time) => ({
        originalData,
        time,
        x: time * 10,
        barColor: '#fff',
      })),
      barSpacing: 10,
      visibleRange: { from: 0, to: 2 },
      conflationFactor: 1,
    });
    const fillRect = vi.fn();
    const target = {
      useMediaCoordinateSpace: (draw: (scope: MediaCoordinatesRenderingScope) => void) =>
        draw({
          context: {
            save: vi.fn(),
            restore: vi.fn(),
            beginPath: vi.fn(),
            rect: vi.fn(),
            clip: vi.fn(),
            fillRect,
          } as unknown as CanvasRenderingContext2D,
          mediaSize: { width: 100, height: 100 } as MediaCoordinatesRenderingScope['mediaSize'],
        }),
    } as unknown as CanvasRenderingTarget2D;
    renderer.draw(target, (value) => value as Coordinate);
    expect(fillRect).toHaveBeenCalledTimes(2);
    renderer.destroy();
    renderer.draw(target, (value) => value as Coordinate);
    expect(fillRect).toHaveBeenCalledTimes(2);
  });
  it('keeps sparse replay windows in elapsed minutes, including single observations', () => {
    const times = [0, 30, 120, 150].map((minutes) => chartTime(at) + minutes * 60);
    expect(traceReplayRange(times, 3, 60)).toEqual([times[3]! - 3600, times[3]]);
    expect(traceViewportSelection(times, { from: times[3]! - 3600, to: times[3]! })).toEqual({
      cursor: 3,
      width: 60,
    });
    expect(traceReplayRange([times[0]!], 0, 60)).toEqual([times[0]! - 30, times[0]! + 30]);
    expect(traceReplayRange([], 0, 60)).toBeUndefined();
  });
  it('uses safe overlay labels and session wall-clock coordinates', () => {
    expect(
      traceOverlayData([
        { timestamp: at, overlay_type: 'scenario', label: null },
        { timestamp: at, overlay_type: 'alert' },
        { timestamp: at, overlay_type: 'scenario', label: '  ' },
        { timestamp: at, overlay_type: 'scenario', label: 'UPSIDE_EXPANSION' },
      ]).map((event) => [event.time, event.label]),
    ).toEqual([
      [chartTime(at), 'scenario'],
      [chartTime(at), 'alert'],
      [chartTime(at), 'scenario'],
      [chartTime(at), 'UPSIDE EXPANSION'],
    ]);
  });
});
