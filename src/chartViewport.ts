export interface ChartRange {
  from: number;
  to: number;
}

/** Retain fractional bar edges and future padding as data is appended or shifts. */
export function retainChartRange(
  range: ChartRange,
  before: readonly number[],
  after: readonly number[],
): ChartRange {
  if (
    before.length < 2 ||
    after.length < 2 ||
    (before.length === after.length && before.every((value, index) => value === after[index]))
  )
    return range;
  const valueAt = (index: number) => {
    const lo = Math.max(0, Math.min(before.length - 2, Math.floor(index)));
    return before[lo]! + (index - lo) * (before[lo + 1]! - before[lo]!);
  };
  const indexAt = (value: number) => {
    let lo = 0,
      hi = after.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (after[mid]! < value) lo = mid + 1;
      else hi = mid;
    }
    const index = Math.max(0, Math.min(after.length - 2, lo - 1));
    return index + (value - after[index]!) / (after[index + 1]! - after[index]!);
  };
  return { from: indexAt(valueAt(range.from)), to: indexAt(valueAt(range.to)) };
}
