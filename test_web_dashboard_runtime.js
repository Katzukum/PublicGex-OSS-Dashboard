const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function runtime() {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) {
            const classes = new Set();
            elements.set(id, { id, value: '', textContent: '', innerText: '', hidden: false,
                style: { display: 'none' }, dataset: {},
                classList: { toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); }, contains: name => classes.has(name) },
                setAttribute() {}, removeAttribute() {},
            });
        }
        return elements.get(id);
    };
    const document = {getElementById: element, querySelectorAll: () => [], addEventListener() {}, body: element('body')};
    const eel = {expose() {}};
    const window = {eel, innerWidth: 1920, addEventListener() {}};
    const context = vm.createContext({window, document, eel, console: {error() {}, log() {}},
        createBridgeClient: require('./web/bridge_client'), createRequestCoordinator: require('./web/request_coordinator'),
        setTimeout, clearTimeout, setInterval, clearInterval, requestAnimationFrame: fn => fn(),
        ActivityFeed: {workspaceActivity: () => null},
    });
    const source = fs.readFileSync('web/main.js', 'utf8').replace('\ninit();', '\n');
    vm.runInContext(source, context);
    vm.runInContext('renderSharedSnapshot = () => {}; renderMarketContext = () => {}; recordActivity = () => {};', context);
    return {context, element, eel, run: code => vm.runInContext(code, context)};
}

test('failed symbol changes hide previous instrument data until the selected asset loads', async () => {
    const r = runtime();
    r.run("cachedSymbol='SPX'; cachedData={snapshot:{symbol:'SPX'}};");
    r.element('symbolSelector').value = 'NDX';
    r.eel.get_decision_workspace = () => () => Promise.resolve({error:'No NDX snapshot'});
    assert.equal(await r.context.loadSymbol(), false);
    assert.equal(r.element('body').classList.contains('symbol-unavailable'), true);
    assert.equal(r.element('workspaceStateMessage').innerText, 'No NDX snapshot');
    r.eel.get_decision_workspace = () => () => Promise.resolve({symbol:'NDX', snapshot_id:2, dashboard:{snapshot:{symbol:'NDX',id:2}}});
    assert.equal(await r.context.loadSymbol(), true);
    assert.equal(r.element('body').classList.contains('symbol-unavailable'), false);
    assert.equal(r.run('cachedSymbol'), 'NDX');
});

test('older symbol responses cannot replace the selected asset', async () => {
    const r = runtime();
    let resolveOld;
    const old = new Promise(resolve => { resolveOld = resolve; });
    r.eel.get_decision_workspace = symbol => () => symbol === 'SPX' ? old : Promise.resolve({symbol:'NDX',snapshot_id:2,dashboard:{snapshot:{symbol:'NDX',id:2}}});
    r.element('symbolSelector').value = 'SPX';
    const first = r.context.loadSymbol();
    await Promise.resolve();
    r.element('symbolSelector').value = 'NDX';
    await r.context.loadSymbol();
    resolveOld({symbol:'SPX',snapshot_id:1,dashboard:{snapshot:{symbol:'SPX',id:1}}});
    await first;
    assert.equal(r.run('cachedSymbol'), 'NDX');
});

test('unavailable decision targets are not rendered as zero prices', () => {
    const r = runtime();
    const model = r.context.cockpitModelFromWorkspace({symbol:'SPX',decision:{target:null,invalidation:null}});
    assert.equal(model.plan.target, '--');
    assert.equal(model.plan.invalidation, '--');
    assert.equal(r.context.hasNumber(null), false);
    assert.equal(r.context.hasNumber(0), true);
    assert.equal(r.context.sweepZeroMarkerLevels({zero_crossings:{below:null,above:null}}, null, 7700).length, 0);
    const levels = r.context.sweepZeroMarkerLevels({zero_crossings:{below:null,above:7720}}, null, 7700);
    assert.equal(levels.length, 1);
    assert.equal(levels[0].value, 7720);
});

test('startup failure keeps retry timers alive and surfaces an error', async () => {
    const r = runtime();
    r.eel.get_settings = () => () => Promise.reject(new Error('Database unavailable'));
    r.run('let timerStarts=0; startTimers=()=>timerStarts++; startStatusTimer=()=>{};');
    await r.context.init();
    assert.equal(r.run('timerStarts'), 1);
    assert.equal(r.element('workspaceStateMessage').innerText, 'Database unavailable');
    assert.equal(r.run('initInFlight'), null);
});

test('empty startup starts polling so arriving data can be discovered', async () => {
    const r = runtime();
    r.eel.get_settings = () => () => Promise.resolve({symbols:[]});
    r.run('let timerStarts=0; startTimers=()=>timerStarts++; startStatusTimer=()=>{}; applyTheme=()=>{}; discoverSymbols=async()=>[];');
    await r.context.init();
    assert.equal(r.run('timerStarts'), 1);
    assert.equal(r.run('initialized'), true);
    assert.match(r.element('workspaceStateMessage').innerText, /No symbols configured/);
});
