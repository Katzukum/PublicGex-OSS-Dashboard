const test = require('node:test');
const assert = require('node:assert/strict');

const createRequestCoordinator = require('./web/request_coordinator.js');

test('a hung request times out and a successful retry supersedes the late response', async () => {
    const coordinator = createRequestCoordinator({ timeoutMs: 15 });
    const stuck = deferred();
    const failed = await coordinator.request('SPX', () => stuck.promise);
    assert.match(failed.error.message, /timed out/);
    const retried = await coordinator.request('SPX', async () => 'fresh');
    stuck.resolve('obsolete');
    assert.equal(retried.value, 'fresh');
    assert.equal(coordinator.isCurrent(failed, 'SPX'), false);
    assert.equal(coordinator.isCurrent(retried, 'SPX'), true);
});


function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}


test('same-symbol requests coalesce onto one promise', async () => {
    const coordinator = createRequestCoordinator();
    const pending = deferred();
    let loads = 0;
    const loader = () => {
        loads += 1;
        return pending.promise;
    };

    const first = coordinator.request('SPX', loader);
    const second = coordinator.request('SPX', loader);

    assert.strictEqual(first, second);
    pending.resolve({ symbol: 'SPX' });
    const result = await first;
    assert.equal(loads, 1);
    assert.equal(coordinator.isCurrent(result, 'SPX'), true);
});


test('new symbol supersedes an out-of-order older response', async () => {
    const coordinator = createRequestCoordinator();
    const oldRequest = deferred();
    const newRequest = deferred();

    const oldResultPromise = coordinator.request('SPX', () => oldRequest.promise);
    const newResultPromise = coordinator.request('NDX', () => newRequest.promise);

    newRequest.resolve({ symbol: 'NDX' });
    const newResult = await newResultPromise;
    assert.equal(coordinator.isCurrent(newResult, 'NDX'), true);

    oldRequest.resolve({ symbol: 'SPX' });
    const oldResult = await oldResultPromise;
    assert.equal(coordinator.isCurrent(oldResult, 'NDX'), false);
    assert.equal(coordinator.isCurrent(oldResult, 'SPX'), false);
});


test('selected-symbol mismatch and invalidation discard a response', async () => {
    const coordinator = createRequestCoordinator();
    const first = await coordinator.request('IWM', async () => ({ symbol: 'IWM' }));
    assert.equal(coordinator.isCurrent(first, 'SPY'), false);
    assert.equal(coordinator.isCurrent(first, 'IWM'), true);

    coordinator.invalidate();
    assert.equal(coordinator.isCurrent(first, 'IWM'), false);
});
