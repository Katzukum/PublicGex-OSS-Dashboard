const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = __dirname;
const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
const main = fs.readFileSync(path.join(root, 'web', 'main.js'), 'utf8');

test('final navigation targets have matching views in the approved order', () => {
    const expected = ['cockpit', 'edge-lab', 'regime', 'trace', 'analysis', 'one-off', 'settings'];
    const navTargets = [...html.matchAll(/data-view="([^"]+)"/g)].map(match => match[1]);
    assert.deepEqual(navTargets, expected);
    expected.forEach(target => assert.match(html, new RegExp(`id="view-${target}"`)));
});

test('retired Gamma and Setups view contracts are absent', () => {
    ['view-dashboard', 'view-setups', 'gammaSweepChart', 'historyChart'].forEach(id => {
        assert.equal(html.includes(id), false, `${id} remains in HTML`);
        assert.equal(main.includes(id), false, `${id} remains in main.js`);
    });
    assert.equal(/data-view="(?:dashboard|setups)"/.test(html), false);
});

test('unique Net Gamma Trend content now lives in TRACE', () => {
    const traceStart = html.indexOf('id="view-trace"');
    const traceEnd = html.indexOf('id="view-analysis"');
    const traceMarkup = html.slice(traceStart, traceEnd);
    assert.match(traceMarkup, /Net Gamma Trend/);
    assert.match(traceMarkup, /id="traceTrendChart"/);
});
