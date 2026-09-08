(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.ActivityFeed = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function statusActivity(status = {}) {
        const bridge = status.event_bridge || {};
        const bridgeState = bridge.status === 'listening'
            ? (bridge.last_event_type ? `event bridge ${bridge.last_event_type}` : 'event bridge listening')
            : 'event bridge stopped';

        if (!status.ok) {
            return {
                key: 'backend-health',
                title: 'Backend unavailable',
                body: status.error || 'Collector status could not be read.',
                typeClass: 'type-error'
            };
        }

        const running = String(status.run_status || '').toLowerCase() === 'running';
        const snapshotAge = Number.isFinite(Number(status.snapshot_age_seconds))
            ? `snapshot ${Math.round(Number(status.snapshot_age_seconds))}s old`
            : 'no snapshot age';
        return {
            key: 'backend-health',
            title: running ? 'Collector running' : 'Backend connected',
            body: `${snapshotAge} • ${bridgeState}`,
            typeClass: running ? 'type-running' : 'type-good'
        };
    }

    function workspaceActivity(symbol, workspace = {}) {
        const snapshot = workspace.dashboard?.snapshot || {};
        const snapshotId = workspace.snapshot_id ?? snapshot.id;
        const timestamp = workspace.as_of || snapshot.timestamp;
        const details = [];
        if (snapshotId !== null && snapshotId !== undefined) details.push(`snapshot #${snapshotId}`);
        if (timestamp) details.push(String(timestamp));
        return {
            key: `workspace:${String(symbol || workspace.symbol || 'unknown').toUpperCase()}`,
            title: `${String(symbol || workspace.symbol || 'Market').toUpperCase()} cockpit refreshed`,
            body: details.join(' • ') || 'Latest local snapshot rendered.',
            typeClass: 'type-system'
        };
    }

    function eventActivity(event = {}) {
        const payload = event.payload || {};
        if (event.type === 'data_refresh') {
            const symbol = String(payload.symbol || 'Market').toUpperCase();
            const details = payload.snapshot_id !== undefined ? `snapshot #${payload.snapshot_id}` : 'new snapshot';
            return {
                key: `event:data-refresh:${symbol}`,
                title: `${symbol} data received`,
                body: details,
                typeClass: 'type-system'
            };
        }
        if (event.type === 'MARKET_UPDATE') {
            return {
                key: 'event:market-update',
                title: 'Market cycle received',
                body: 'Overview and downstream integrations refreshed.',
                typeClass: 'type-system'
            };
        }
        if (event.type === 'magnet_change') {
            const oldMagnet = Number(payload.old_magnet);
            const newMagnet = Number(payload.new_magnet);
            return {
                title: 'Magnet Shift',
                body: `${payload.symbol || 'Market'}: ${Number.isFinite(oldMagnet) ? oldMagnet.toFixed(0) : '--'} -> ${Number.isFinite(newMagnet) ? newMagnet.toFixed(0) : '--'}`,
                typeClass: 'type-magnet'
            };
        }
        if (event.type === 'decision_alert') {
            return {
                title: payload.alert_type || 'Decision Alert',
                body: payload.message || '',
                typeClass: payload.severity === 'critical' ? 'type-magnet' : 'type-info'
            };
        }
        return null;
    }

    return { eventActivity, statusActivity, workspaceActivity };
});
