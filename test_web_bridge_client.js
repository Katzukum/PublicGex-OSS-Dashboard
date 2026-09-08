const test = require('node:test');
const assert = require('node:assert/strict');
const createBridgeClient = require('./web/bridge_client');

function mockBridge() {
    let callId = 0;
    const bridge = { _call_return_callbacks: {} };
    bridge.read = () => () => new Promise((resolve, reject) => {
        bridge._call_return_callbacks[++callId] = { resolve, reject };
    });
    return bridge;
}

test('completed bridge requests release only their own Eel callback', async () => {
    const bridge = mockBridge();
    const call = createBridgeClient(() => bridge);
    const a = call('read');
    const b = call('read');
    bridge._call_return_callbacks[1].resolve('first');
    assert.equal(await a, 'first');
    assert.deepEqual(Object.keys(bridge._call_return_callbacks), ['2']);
    bridge._call_return_callbacks[2].resolve('second');
    assert.equal(await b, 'second');
    assert.deepEqual(bridge._call_return_callbacks, {});
});

test('timeouts release callbacks and allow new bridge requests', async () => {
    const bridge = mockBridge();
    const call = createBridgeClient(() => bridge, { timeoutMs: 15 });
    await assert.rejects(call('read'), /did not respond/);
    assert.deepEqual(bridge._call_return_callbacks, {});
    const retry = call('read');
    bridge._call_return_callbacks[2].resolve('recovered');
    assert.equal(await retry, 'recovered');
});

test('missing, disconnected, and rejected backends fail with actionable errors', async () => {
    await assert.rejects(createBridgeClient(() => undefined)('read'), /python app.py/);
    await assert.rejects(createBridgeClient(() => ({ read() { throw new Error('closed'); } }))('read'), /closed/);
    const bridge = mockBridge();
    const result = createBridgeClient(() => bridge)('read');
    bridge._call_return_callbacks[1].reject('database busy');
    await assert.rejects(result, /database busy/);
    assert.deepEqual(bridge._call_return_callbacks, {});
});
