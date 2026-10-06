import { describe, expect, it } from 'vitest';
import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  SeriesAttachedParameter,
} from 'lightweight-charts';
import {
  HorizontalProfilePrimitive,
  VerticalLevelsPrimitive,
  VerticalProfilePrimitive,
  focusedProfileRange,
  sampleNumericCurve,
  strikeLattice,
  numericCoordinate,
  numericGrid,
  nearestProfileRow,
  profileScale,
} from './profilePrimitives';

describe('Lightweight Charts numeric profile geometry', () => {
  it('fills missing strikes with an exact lattice so unequal price gaps stay proportional', () => {
    const domain = strikeLattice([225, 226, 230]);
    expect(domain).toEqual([225, 226, 227, 228, 229, 230]);
    expect(strikeLattice([225, 225.5, 227])).toEqual([225, 225.5, 226, 226.5, 227]);
    const chart = {
      timeScale: () => ({
        timeToCoordinate: (value: number) =>
          domain!.includes(value) ? domain!.indexOf(value) * 20 : null,
      }),
    } as unknown as IChartApiBase<number>;
    const x225 = numericCoordinate(chart, 225, domain!)!;
    const x226 = numericCoordinate(chart, 226, domain!)!;
    const x230 = numericCoordinate(chart, 230, domain!)!;
    expect((x230 - x226) / (x226 - x225)).toBe(4);
    expect(numericCoordinate(chart, 225.5, domain!)).toBe(10);
    expect(strikeLattice([100, 100.000001, 10000])).toBeNull();
  });

  it('resamples irregular sweep points by numeric distance and keeps absent hedge values missing', () => {
    const points = [
      { x: 0, y: 0 },
      { x: 1, y: 8 },
      { x: 5, y: 0 },
    ];
    expect(
      sampleNumericCurve(
        points,
        [0, 0.5, 1, 2, 3, 4, 5],
        (point) => point.x,
        (point) => point.y,
      ),
    ).toEqual([
      { time: 0, value: 0 },
      { time: 0.5, value: 4 },
      { time: 1, value: 8 },
      { time: 2, value: 6 },
      { time: 3, value: 4 },
      { time: 4, value: 2 },
      { time: 5, value: 0 },
    ]);
    expect(
      sampleNumericCurve(
        [
          { x: 0, y: 2 },
          { x: 1, y: null },
          { x: 2, y: 4 },
        ],
        [0, 0.5, 1, 1.5, 2],
        (point) => point.x,
        (point) => point.y,
      ),
    ).toEqual([
      { time: 0, value: 2 },
      { time: 0.5 },
      { time: 1 },
      { time: 1.5 },
      { time: 2, value: 4 },
    ]);
  });

  it('restores the original marker-centered focus padding and minimum chain coverage', () => {
    const markers = [{ value: 480 }, { value: 520 }];
    expect(focusedProfileRange([400, 450, 500, 550, 600], markers, 500)).toEqual([444, 556]);
    expect(focusedProfileRange([0, 1000], [{ value: 501 }], 500)).toEqual([425.5, 575.5]);
    expect(focusedProfileRange([225.5], [], 225.5)).toEqual([225, 226]);
    expect(focusedProfileRange([], [], 500)).toBeUndefined();
  });
  it('keeps a signed, evenly spaced exposure domain, including zero', () => {
    const domain = numericGrid(-10, 10);
    expect(domain).toHaveLength(101);
    expect(domain[0]).toBe(-10);
    expect(domain[50]).toBe(0);
    expect(domain[100]).toBe(10);
    expect(numericGrid(4, 4, 3)).toEqual([3, 4, 5]);
    expect(numericGrid(2, -2, 3)).toEqual([-2, 0, 2]);
    expect(numericGrid(NaN, 1)).toEqual([]);
  });

  it('interpolates fractional market levels rather than moving them to a listed strike', () => {
    const chart = {
      timeScale: () => ({
        timeToCoordinate: (value: number) =>
          new Map([
            [225, 10],
            [226, 30],
            [230, 70],
          ]).get(value) ?? null,
      }),
    } as unknown as IChartApiBase<number>;
    expect(numericCoordinate(chart, 225.5, [225, 226, 230])).toBe(20);
    expect(numericCoordinate(chart, 228, [225, 226, 230])).toBe(50);
    expect(numericCoordinate(chart, 230, [225, 226, 230])).toBe(70);
    expect(numericCoordinate(chart, 220, [])).toBeNull();
  });

  it('scales net independently and excludes out-of-window concentrations', () => {
    const rows = [
      { strike: 225, call: 10, put: -8, net: 2 },
      { strike: 226, call: 20, put: -19, net: 1 },
      { strike: 240, call: 200, put: -120, net: 80 },
    ];
    const scale = profileScale(rows, [225, 226]);
    expect(scale.sideMax).toBeCloseTo(23);
    expect(scale.netMax).toBeCloseTo(2.3);
    expect(nearestProfileRow(rows, 225.6)?.strike).toBe(226);
    expect(nearestProfileRow([], 225)).toBeUndefined();
    expect(profileScale([]).sideMax).toBeGreaterThan(0);
  });
});

function canvasHarness() {
  const rectangles: number[][] = [],
    moves: number[][] = [],
    lines: number[][] = [],
    labels: string[] = [];
  const context = {
    save() {},
    restore() {},
    beginPath() {},
    rect() {},
    clip() {},
    setLineDash() {},
    stroke() {},
    fill() {},
    arc() {},
    fillRect: (...values: number[]) => rectangles.push(values),
    moveTo: (...values: number[]) => moves.push(values),
    lineTo: (...values: number[]) => lines.push(values),
    measureText: (value: string) => ({ width: value.length * 6 }),
    fillText: (text: string) => labels.push(text),
  } as unknown as CanvasRenderingContext2D;
  const domain = numericGrid(-10, 10);
  const chart = {
    timeScale: () => ({
      timeToCoordinate: (value: number) => (domain.includes(value) ? 100 + value * 10 : null),
    }),
  } as unknown as IChartApiBase<number>;
  const attachment = {
    chart,
    series: { priceToCoordinate: (price: number) => 100 - (price - 225) * 20 },
    requestUpdate() {},
  } as unknown as SeriesAttachedParameter<number>;
  const target = {
    useMediaCoordinateSpace: (
      draw: (scope: {
        context: CanvasRenderingContext2D;
        mediaSize: { width: number; height: number };
      }) => void,
    ) => draw({ context, mediaSize: { width: 200, height: 200 } }),
  } as Parameters<IPrimitivePaneRenderer['draw']>[0];
  return { rectangles, moves, lines, labels, attachment, target, domain };
}

describe('native profile primitive drawing', () => {
  it('keeps exact scalar bar positions when an irregular strike lattice exceeds the safe allocation limit', () => {
    const test = canvasHarness();
    const primitive = new VerticalProfilePrimitive(
      [
        { strike: 0, call: 1, put: -1, net: 0 },
        { strike: 1, call: 2, put: -1, net: 1 },
        { strike: 5, call: 3, put: -1, net: 2 },
      ],
      'call',
      test.domain,
    );
    primitive.attached(test.attachment);
    primitive.paneViews()[0]!.renderer()!.draw(test.target);
    const centers = test.rectangles.map((rect) => rect[0]! + rect[2]! / 2);
    expect(centers).toEqual([100, 110, 150]);
    expect((centers[2]! - centers[1]!) / (centers[1]! - centers[0]!)).toBe(4);
  });
  it('renders positive call bars right of zero, negative put bars left, and net on its own scale', () => {
    const test = canvasHarness();
    const primitive = new HorizontalProfilePrimitive([{ strike: 225, call: 8, put: -4, net: 1 }], {
      domain: test.domain,
      sideMax: 10,
      netMax: 2,
      theme: 'dark',
    });
    primitive.attached(test.attachment);
    primitive.paneViews()[0]!.renderer()!.draw(test.target);
    expect(test.rectangles[0]?.[0]).toBe(100);
    expect(test.rectangles[0]?.[2]).toBe(80);
    expect(test.rectangles[1]?.[0]).toBe(60);
    expect(test.rectangles[1]?.[2]).toBe(40);
    expect(test.moves).toContainEqual([150, 100]); // net 1 / max 2 uses half of the positive axis
    expect(test.labels).toContain('Net GEX · $M (top scale)');
  });

  it('uses fractional spot/zero levels in the real guide renderer', () => {
    const test = canvasHarness();
    const levels = [
      { value: 0.5, label: 'Spot', color: '#fff' },
      { value: -2.5, label: 'Zero gamma', color: '#aaa' },
    ];
    const primitive = new VerticalLevelsPrimitive(levels, test.domain, 'dark');
    primitive.attached(test.attachment);
    primitive.paneViews()[0]!.renderer()!.draw(test.target);
    expect(test.moves).toContainEqual([105, 0]);
    expect(test.moves).toContainEqual([75, 0]);
    expect(test.lines).toContainEqual([105, 200]);
    expect(test.labels.some((label) => label.includes('Zero gamma'))).toBe(true);
    primitive.detached();
    const count = test.lines.length;
    primitive.paneViews()[0]!.renderer()!.draw(test.target);
    expect(test.lines).toHaveLength(count);
  });
  it('never paints a spot-label background over the neighboring exposure bar', () => {
    const test = canvasHarness();
    const primitive = new HorizontalProfilePrimitive([{ strike: 225, call: 8, put: 0 }], {
      domain: test.domain,
      sideMax: 10,
      theme: 'dark',
      levels: [{ value: 224.9, label: 'Latest spot', color: '#fff' }],
    });
    primitive.attached(test.attachment);
    primitive.paneViews()[0]!.renderer()!.draw(test.target);
    expect(test.rectangles).toHaveLength(1);
    expect(test.rectangles[0]?.[2]).toBe(80);
    expect(test.labels).toEqual([]);
    expect(test.lines.some(([x, y]) => x === 200 && Math.abs(y! - 102) < 1e-6)).toBe(true);
  });
});
