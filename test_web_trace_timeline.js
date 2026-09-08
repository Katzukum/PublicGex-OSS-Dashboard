const test = require('node:test');
const assert = require('node:assert/strict');

const traceTimelineRange = require('./web/trace_timeline.js');

test('omitting window size shows a useful multi-minute range, not one candle', () => {
    const values = Array.from({length: 287}, (_, index) => String(index));
    const range = traceTimelineRange(values, 286);
    assert.equal(range.endIndex - range.startIndex + 1, 60);
});


const buckets = Array.from(
    { length: 100 },
    (_, index) => `2026-08-14T${String(9 + Math.floor(index / 60)).padStart(2, '0')}:${String(index % 60).padStart(2, '0')}:00`
);


test('TRACE timeline opens on a bounded latest-time viewport', () => {
    const range = traceTimelineRange(buckets, 99, 20);

    assert.equal(range.startIndex, 80);
    assert.equal(range.endIndex, 99);
    assert.equal(range.label, '10:39');
    assert.equal(range.positionPct, 100);
});


test('TRACE timeline scrolls backward without changing viewport width', () => {
    const range = traceTimelineRange(buckets, 40, 20);

    assert.equal(range.startIndex, 21);
    assert.equal(range.endIndex, 40);
    assert.equal(range.startValue, buckets[21]);
    assert.equal(range.endValue, buckets[40]);
});


test('TRACE timeline clamps the first snapshot and empty data', () => {
    const first = traceTimelineRange(buckets, -10, 20);
    const empty = traceTimelineRange([], 0);

    assert.equal(first.index, 0);
    assert.equal(first.startIndex, 0);
    assert.equal(first.endIndex, 19);
    assert.equal(empty.label, '--:--');
    assert.equal(empty.startValue, null);
});
