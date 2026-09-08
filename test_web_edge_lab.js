const assert = require('node:assert/strict');
const test = require('node:test');
const { viewModel } = require('./web/edge_lab.js');

test('Edge Lab exposes honest empty and insufficient states', () => {
    const empty = viewModel({ status: 'INSUFFICIENT', stats: {}, opportunities: [] });
    assert.equal(empty.headline, 'Insufficient evidence');
    assert.match(empty.warning, /No independent/);
    const insufficient = viewModel({ status: 'INSUFFICIENT', stats: { independent_opportunities: 12, unique_days: 5 } });
    assert.equal(insufficient.count, 12);
    assert.equal(insufficient.days, 5);
});

test('Edge Lab distinguishes emerging and calibrated evidence', () => {
    assert.equal(viewModel({ status: 'EMERGING', stats: {} }).headline, 'Emerging evidence');
    assert.equal(viewModel({ status: 'CALIBRATED', stats: {} }).headline, 'Calibrated historical edge');
});
