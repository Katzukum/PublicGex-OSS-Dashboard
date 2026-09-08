const assert = require('node:assert/strict');
const test = require('node:test');

const { eventActivity, statusActivity, workspaceActivity } = require('./web/activity_feed.js');

test('routine refresh events are visible system activity, not actionable alerts', () => {
    assert.deepEqual(eventActivity({
        type: 'data_refresh',
        payload: { symbol: 'spy', snapshot_id: 42 }
    }), {
        key: 'event:data-refresh:SPY',
        title: 'SPY data received',
        body: 'snapshot #42',
        typeClass: 'type-system'
    });
    assert.equal(eventActivity({ type: 'unknown' }), null);
});

test('backend health exposes collector and event-bridge state', () => {
    const activity = statusActivity({
        ok: true,
        run_status: 'running',
        snapshot_age_seconds: 7.4,
        event_bridge: { status: 'listening', last_event_type: 'data_refresh' }
    });
    assert.equal(activity.key, 'backend-health');
    assert.equal(activity.title, 'Collector running');
    assert.match(activity.body, /snapshot 7s old/);
    assert.match(activity.body, /event bridge data_refresh/);
});

test('workspace refresh activity is coalesced per symbol', () => {
    const activity = workspaceActivity('SPY', {
        snapshot_id: 99,
        as_of: '2026-08-14 20:08:49',
        dashboard: { snapshot: {} }
    });
    assert.equal(activity.key, 'workspace:SPY');
    assert.equal(activity.title, 'SPY cockpit refreshed');
    assert.match(activity.body, /snapshot #99/);
});
