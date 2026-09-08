let refreshTimer = null;
let countdownTimer = null;
let statusTimer = null;
let currentSettings = {
    refresh_interval: 10,
    theme: 'dark',
    symbols: [],
    api_rate_limit_per_second: 10,
    api_rate_limit_utilization: 0.6,
    min_poll_interval_seconds: 15,
    max_poll_interval_seconds: 120,
    raw_retention_days: 30
};
let timeLeft = 0;
let cachedData = null;
let cachedSymbol = null;
let cachedOverview = null;
let cachedTradeSetups = null;
let cachedOneOffData = null;
let cachedTraceData = null;
let cachedWorkspace = null;
let cockpitModel = null;
let gammaSweepOverlayEnabled = false;
let traceMode = 'net_gex';
let traceOverlaysEnabled = false;
let traceTimelineBuckets = [];
let traceTimelineWindowSize = 1;
let traceDatesSymbol = null;
let compassHistory = { Traders: [], Whale: [] }; // Trail history per compass
const symbolRequestCoordinator = createRequestCoordinator();
let appliedSymbolGeneration = 0;

const NUMERIC_FONT = '"Cascadia Mono", Consolas, "Roboto Mono", "JetBrains Mono", monospace';
let CHART_TEXT = '#96a3af';
let CHART_GRID = 'rgba(122,148,170,0.14)';
const chartInstances = {};
const chartZoomState = {};
const chartInteractionHandlers = {};
const profileAutoscaleState = {};
let activeView = 'cockpit';
let initialized = false;
let initInFlight = null;
let statusInFlight = false;
let refreshInFlight = false;
let traceGeneration = 0;
let edgeGeneration = 0;
let lastRenderedSnapshot = null;
let lastAnalysisSnapshot = null;

// Bound bridge calls and release completed Eel callbacks.
const apiCall = createBridgeClient(() => window.eel);

function hasNumber(value) {
    return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
}

function setWorkspaceState(kind, message = '') {
    const state = document.getElementById('workspaceState');
    if (!state) return;
    state.hidden = !message;
    state.dataset.kind = kind;
    setText('workspaceStateMessage', message);
    document.body.classList.toggle('symbol-unavailable', Boolean(message) && (!cachedData || cachedSymbol !== document.getElementById('symbolSelector').value));
    const button = document.getElementById('retryWorkspace');
    button.hidden = kind === 'loading';
}

function applyTheme(theme) {
    document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
    const style = getComputedStyle(document.documentElement);
    CHART_TEXT = style.getPropertyValue('--muted').trim();
    CHART_GRID = style.getPropertyValue('--border-soft').trim();
    for (const id of Object.keys(chartInstances)) {
        chartInstances[id]?.dispose();
        delete chartInstances[id];
    }
    if (cachedData) renderActiveView();
}

function toggleSidebar() {
    const collapsed = document.querySelector('.terminal-shell').classList.toggle('sidebar-collapsed');
    const button = document.querySelector('.collapse-btn');
    button.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    button.setAttribute('aria-expanded', String(!collapsed));
    resizeCharts();
}

function renderActiveView() {
    if (activeView === 'cockpit') return loadCockpit();
    if (activeView === 'analysis' && cachedData && cachedSymbol === document.getElementById('symbolSelector').value) {
        const key = `${cachedSymbol}:${cachedData.snapshot.id}`;
        if (key !== lastAnalysisSnapshot) { renderAnalysisTable(cachedData); lastAnalysisSnapshot = key; }
    }
    if (activeView === 'regime') return loadOverview();
    if (activeView === 'trace') return loadTrace();
    if (activeView === 'edge-lab') return loadEdgeLab();
}

function formatStatusAge(seconds) {
    if (seconds === null || seconds === undefined) return 'n/a';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${(seconds / 3600).toFixed(1)}h`;
}

async function updateBackendStatus() {
    const statusEl = document.getElementById('backendStatus');
    if (!statusEl || statusInFlight || document.hidden) return;
    statusInFlight = true;

    try {
        const status = await apiCall('get_backend_status');
        recordActivity(ActivityFeed.statusActivity(status));
        if (!status.ok) {
            statusEl.className = 'status-offline';
            statusEl.innerHTML = '<i></i> No Backend';
            setClassText('marketState', 'OFFLINE', 'status-pill status-stale');
            return;
        }

        const age = status.snapshot_age_seconds;
        const runStatus = String(status.run_status || '').toLowerCase();
        let statusClass = 'status-healthy';
        let label = `Fresh ${formatStatusAge(age)}`;

        if (runStatus === 'running') {
            statusClass = 'status-running';
            label = `Running ${formatStatusAge(status.run_age_seconds)}`;
        } else if (age === null || age === undefined) {
            statusClass = 'status-unknown';
            label = 'No Snapshot';
        } else if (age > 180) {
            statusClass = 'status-stale';
            label = `Stale ${formatStatusAge(age)}`;
        }

        statusEl.className = statusClass;
        statusEl.innerHTML = `<i></i> ${label}`;
        const selectedTime = new Date(cachedData?.snapshot?.timestamp || '').getTime();
        const selectedAge = Number.isFinite(selectedTime) ? (Date.now() - selectedTime) / 1000 : null;
        const fresh = hasNumber(selectedAge) && selectedAge <= 180;
        setClassText('marketState', fresh ? 'FRESH' : 'STALE', `status-pill ${fresh ? 'status-healthy' : 'status-stale'}`);
    } catch (e) {
        statusEl.className = 'status-error';
        statusEl.innerHTML = '<i></i> Status Error';
        setClassText('marketState', 'OFFLINE', 'status-pill status-stale');
        recordActivity(ActivityFeed.statusActivity({ ok: false, error: e?.message || 'Status request failed.' }));
        console.error('Backend status failed', e);
    } finally {
        statusInFlight = false;
    }
}

function startStatusTimer() {
    if (statusTimer) clearInterval(statusTimer);
    updateBackendStatus();
    statusTimer = setInterval(updateBackendStatus, 5000);
}

function chartText(size = 12, color = CHART_TEXT) {
    return { color, fontFamily: NUMERIC_FONT, fontSize: size };
}

function getChart(id) {
    const el = document.getElementById(id);
    if (!el || !window.echarts || !el.clientWidth || !el.clientHeight) return null;
    if (!chartInstances[id] || chartInstances[id].isDisposed()) {
        chartInstances[id] = echarts.init(el, null, { renderer: 'canvas' });
    }
    return chartInstances[id];
}

function resizeCharts(ids = Object.keys(chartInstances)) {
    requestAnimationFrame(() => {
        ids.forEach(id => {
            const el = document.getElementById(id);
            if (el?.clientWidth && el?.clientHeight) chartInstances[id]?.resize();
        });
        const edgeEl = document.getElementById('edgeLabExpectancyChart');
        if (edgeEl?.clientWidth) window.echarts?.getInstanceByDom(edgeEl)?.resize();
    });
}

function setChartOption(id, option, preserveZoom = true) {
    const chart = getChart(id);
    if (!chart) return;
    if (preserveZoom) persistChartZoom(id);
    const storedZoom = preserveZoom && option?.dataZoom ? chartZoomState[id] : null;
    if (storedZoom) {
        const zoom = storedZoom.startValue != null && storedZoom.endValue != null
            ? {startValue: storedZoom.startValue, endValue: storedZoom.endValue, rangeMode:['value','value']}
            : {start: storedZoom.start, end: storedZoom.end};
        option = {...option, dataZoom: option.dataZoom.map((item,index) => index ? item : {...item, ...zoom})};
    }
    chart.dispatchAction({ type: 'hideTip' });
    chart.setOption(option, true);
}

function zoomDataOptions(startValue = null, endValue = null) {
    const insideZoom = {
        type: 'inside',
        xAxisIndex: 0,
        filterMode: 'none',
        zoomOnMouseWheel: false,
        moveOnMouseWheel: false,
        moveOnMouseMove: true,
        preventDefaultMouseMove: true,
        throttle: 80
    };

    if (Number.isFinite(startValue) && Number.isFinite(endValue) && startValue < endValue) {
        insideZoom.startValue = startValue;
        insideZoom.endValue = endValue;
    }

    return [insideZoom];
}

function captureChartZoomState(chart) {
    const dataZoom = chart?.getOption?.()?.dataZoom?.[0];
    if (!dataZoom) return null;

    const state = {};
    if (Number.isFinite(dataZoom.start) && Number.isFinite(dataZoom.end)) {
        state.start = dataZoom.start;
        state.end = dataZoom.end;
    }
    if (dataZoom.startValue !== undefined && dataZoom.startValue !== null) {
        state.startValue = dataZoom.startValue;
    }
    if (dataZoom.endValue !== undefined && dataZoom.endValue !== null) {
        state.endValue = dataZoom.endValue;
    }

    return Object.keys(state).length ? state : null;
}

function persistChartZoom(id) {
    const chart = chartInstances[id];
    if (!chart || chart.isDisposed()) return;
    const state = captureChartZoomState(chart);
    if (state) chartZoomState[id] = state;
}

function applyChartZoomState(chart, state) {
    if (!chart || chart.isDisposed() || !state) return;

    const action = { type: 'dataZoom', dataZoomIndex: 0 };
    if (Number.isFinite(state.start) && Number.isFinite(state.end)) {
        action.start = state.start;
        action.end = state.end;
    } else if (state.startValue !== undefined && state.endValue !== undefined) {
        action.startValue = state.startValue;
        action.endValue = state.endValue;
    } else {
        return;
    }

    chart.dispatchAction(action);
}

function resetChartZoom(id) {
    delete chartZoomState[id];
    const chart = chartInstances[id];
    if (!chart || chart.isDisposed()) return;
    chart.dispatchAction({
        type: 'dataZoom',
        dataZoomIndex: 0,
        start: 0,
        end: 100
    });
    delete chartZoomState[id];
}

function resetChartsZoom(ids = Object.keys(chartZoomState)) {
    ids.forEach(resetChartZoom);
}

function nearestAxisIndex(axisValues, rawValue) {
    if (typeof rawValue === 'number') {
        if (typeof axisValues[0] === 'number') {
            return axisValues.reduce((bestIndex, value, index) => {
                return Math.abs(value - rawValue) < Math.abs(axisValues[bestIndex] - rawValue) ? index : bestIndex;
            }, 0);
        }
        return Math.max(0, Math.min(Math.round(rawValue), axisValues.length - 1));
    }

    const exactIndex = axisValues.indexOf(rawValue);
    return exactIndex >= 0 ? exactIndex : 0;
}

function currentZoomIndices(chart, axisValues) {
    const maxIndex = axisValues.length - 1;
    const state = captureChartZoomState(chart) || {};

    if (state.startValue !== undefined && state.endValue !== undefined) {
        const startIndex = nearestAxisIndex(axisValues, state.startValue);
        const endIndex = nearestAxisIndex(axisValues, state.endValue);
        return {
            startIndex: Math.max(0, Math.min(startIndex, endIndex)),
            endIndex: Math.min(maxIndex, Math.max(startIndex, endIndex))
        };
    }

    const startPct = Number.isFinite(state.start) ? state.start : 0;
    const endPct = Number.isFinite(state.end) ? state.end : 100;
    return {
        startIndex: Math.max(0, Math.round(maxIndex * startPct / 100)),
        endIndex: Math.min(maxIndex, Math.round(maxIndex * endPct / 100))
    };
}

function zoomChartToIndexRange(chart, axisValues, startIndex, endIndex) {
    const maxIndex = axisValues.length - 1;
    const boundedStart = Math.max(0, Math.min(startIndex, maxIndex));
    const boundedEnd = Math.max(boundedStart, Math.min(endIndex, maxIndex));

    chart.dispatchAction({
        type: 'dataZoom',
        dataZoomIndex: 0,
        startValue: axisValues[boundedStart],
        endValue: axisValues[boundedEnd]
    });
}

function zoomChartAroundIndex(chart, axisValues, dataIndex, zoomSize) {
    const halfWindow = zoomSize / 2;
    const startIndex = Math.max(Math.floor(dataIndex - halfWindow), 0);
    const endIndex = Math.min(Math.ceil(dataIndex + halfWindow), axisValues.length - 1);

    zoomChartToIndexRange(chart, axisValues, startIndex, endIndex);
}

function wheelZoomChart(chart, axisValues, event, zoomSize = 6) {
    if (!chart || !axisValues?.length) return;
    const point = [event.offsetX, event.offsetY];
    if (!chart.containPixel({ gridIndex: 0 }, point)) return;

    event.event?.preventDefault?.();
    const wheelDelta = Number(event.wheelDelta ?? -(event.event?.deltaY || 0));
    if (!Number.isFinite(wheelDelta) || wheelDelta === 0) return;

    const [rawX] = chart.convertFromPixel({ gridIndex: 0 }, point);
    const centerIndex = nearestAxisIndex(axisValues, rawX);
    const { startIndex, endIndex } = currentZoomIndices(chart, axisValues);
    const currentSpan = Math.max(2, endIndex - startIndex + 1);
    const minSpan = Math.max(2, Math.min(zoomSize, axisValues.length));
    const factor = wheelDelta > 0 ? 0.88 : 1.12;
    const nextSpan = Math.max(minSpan, Math.min(axisValues.length, Math.round(currentSpan * factor)));
    const leftShare = currentSpan > 1 ? (centerIndex - startIndex) / (currentSpan - 1) : 0.5;
    let nextStart = Math.round(centerIndex - (nextSpan - 1) * Math.max(0, Math.min(1, leftShare)));
    let nextEnd = nextStart + nextSpan - 1;

    if (nextStart < 0) {
        nextEnd -= nextStart;
        nextStart = 0;
    }
    if (nextEnd > axisValues.length - 1) {
        nextStart -= nextEnd - (axisValues.length - 1);
        nextEnd = axisValues.length - 1;
    }

    zoomChartToIndexRange(chart, axisValues, nextStart, nextEnd);
}

function attachClickZoom(id, axisValues, zoomSize = 6) {
    const chart = getChart(id);
    if (!chart || !axisValues?.length) return;
    const previous = chartInteractionHandlers[id];

    if (previous?.dataZoom) chart.off('dataZoom', previous.dataZoom);
    if (previous?.dblclick) chart.getZr().off('dblclick', previous.dblclick);
    if (previous?.mousewheel) chart.getZr().off('mousewheel', previous.mousewheel);

    const dataZoom = event => {
        chartZoomState[id] = captureChartZoomState(chart) || chartZoomState[id];
        if (id === 'traceHeatmapChart') {
            syncTraceTimelineFromChart(chart, axisValues);
        }
        const autoscale = profileAutoscaleState[id];
        if (autoscale?.rows?.length) {
            const { startValue, endValue } = resolveZoomRange(event, autoscale.strikes);
            applyProfileAxisBounds(chart, autoscale.rows, startValue, endValue);
        }
    };
    const dblclick = event => {
        const point = [event.offsetX, event.offsetY];
        if (!chart.containPixel({ gridIndex: 0 }, point)) return;
        const [rawX] = chart.convertFromPixel({ gridIndex: 0 }, point);
        zoomChartAroundIndex(chart, axisValues, nearestAxisIndex(axisValues, rawX), zoomSize);
    };
    const mousewheel = event => wheelZoomChart(chart, axisValues, event, zoomSize);

    chart.on('dataZoom', dataZoom);
    chart.getZr().on('dblclick', dblclick);
    chart.getZr().on('mousewheel', mousewheel);
    chartInteractionHandlers[id] = { dataZoom, dblclick, mousewheel };
}

function formatCompactNumber(value) {
    const abs = Math.abs(value);
    if (abs >= 1000000000) return `${(value / 1000000000).toFixed(1)}B`;
    if (abs >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
    if (abs >= 1000) return `${(value / 1000).toFixed(1)}k`;
    return Number(value).toFixed(0);
}

function formatAxisPrice(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '';
    return n >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : n.toFixed(2);
}

function formatMillionsAxis(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '';
    const abs = Math.abs(n);
    if (abs >= 100) return n.toFixed(0);
    if (abs >= 10) return n.toFixed(1);
    return n.toFixed(2);
}

function formatMillionsValue(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    return `${formatMillionsAxis(n)}M`;
}

function formatShareCount(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    const abs = Math.abs(n);
    if (abs >= 1000000) return `${(n / 1000000).toFixed(2)}M sh`;
    if (abs >= 1000) return `${(n / 1000).toFixed(1)}k sh`;
    return `${n.toFixed(0)} sh`;
}

function sweepRowsFromPayload(gammaSweep, divisor = 1) {
    if (!gammaSweep || gammaSweep.status !== 'ok' || !Array.isArray(gammaSweep.points)) return [];
    return gammaSweep.points
        .map(point => ({
            spot: Number(point.spot),
            netGex: Number(point.net_gex) / divisor,
            hedgeShares: Number(point.hedge_shares)
        }))
        .filter(point => Number.isFinite(point.spot) && Number.isFinite(point.netGex) && Number.isFinite(point.hedgeShares));
}

function nearestSweepPoint(rows, spot) {
    const target = Number(spot);
    if (!rows.length || !Number.isFinite(target)) return null;
    return rows.reduce((best, row) => (
        !best || Math.abs(row.spot - target) < Math.abs(best.spot - target) ? row : best
    ), null);
}

function sweepZeroLabel(gammaSweep, fallback) {
    const below = hasNumber(gammaSweep?.zero_crossings?.below) ? Number(gammaSweep.zero_crossings.below) : NaN;
    const above = hasNumber(gammaSweep?.zero_crossings?.above) ? Number(gammaSweep.zero_crossings.above) : NaN;
    const hasBelow = Number.isFinite(below);
    const hasAbove = Number.isFinite(above);
    if (hasBelow && hasAbove) return `B ${formatTargetPrice(below)} / A ${formatTargetPrice(above)}`;
    if (hasBelow) return `B ${formatTargetPrice(below)}`;
    if (hasAbove) return `A ${formatTargetPrice(above)}`;
    return fallback ? formatTargetPrice(fallback) : '--';
}

function sweepZeroMarkerLevels(gammaSweep, fallback, spot) {
    const levels = [];
    const below = hasNumber(gammaSweep?.zero_crossings?.below) ? Number(gammaSweep.zero_crossings.below) : NaN;
    const above = hasNumber(gammaSweep?.zero_crossings?.above) ? Number(gammaSweep.zero_crossings.above) : NaN;
    if (Number.isFinite(below)) {
        levels.push({ label: 'Zero Gamma below', value: below, color: '#ff454f', dash: 'dash' });
    }
    if (Number.isFinite(above) && (!Number.isFinite(below) || Math.abs(above - below) > 0.0001)) {
        levels.push({ label: 'Zero Gamma above', value: above, color: '#ff454f', dash: 'dash' });
    }
    if (levels.length) return levels;
    return hasNumber(fallback)
        ? [{ label: 'Flip', value: fallback, color: '#ff454f', dash: 'dash' }]
        : [];
}

function niceStep(value) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const exponent = Math.floor(Math.log10(value));
    const fraction = value / Math.pow(10, exponent);
    const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
    return niceFraction * Math.pow(10, exponent);
}

function chartBounds(values, padding = 0.14) {
    const finiteValues = (values || []).filter(Number.isFinite);
    if (!finiteValues.length) return { min: -1, max: 1 };

    let min = Math.min(0, ...finiteValues);
    let max = Math.max(0, ...finiteValues);
    let range = max - min;
    if (range === 0) range = Math.max(Math.abs(max), 1);

    const rawMin = min - (range * padding);
    const rawMax = max + (range * padding);
    const step = niceStep((rawMax - rawMin) / 5);

    min = Math.floor(rawMin / step) * step;
    max = Math.ceil(rawMax / step) * step;
    if (min === max) {
        min -= step;
        max += step;
    }

    return { min, max };
}

function rowsInStrikeRange(rows, startValue, endValue) {
    if (!Number.isFinite(startValue) || !Number.isFinite(endValue)) return rows;
    const start = Math.min(startValue, endValue);
    const end = Math.max(startValue, endValue);
    const visibleRows = rows.filter(row => row.strike >= start && row.strike <= end);
    return visibleRows.length ? visibleRows : rows;
}

function profileAxisBounds(rows) {
    return {
        netBounds: chartBounds(rows.map(row => row.net), 0.18),
        sideBounds: chartBounds(rows.flatMap(row => [row.call, row.put]), 0.16),
    };
}

function strikeAxisBounds(strikes) {
    const finiteStrikes = strikes.filter(Number.isFinite);
    if (!finiteStrikes.length) return {};

    const minStrike = Math.min(...finiteStrikes);
    const maxStrike = Math.max(...finiteStrikes);
    const span = maxStrike - minStrike;
    const padding = span > 0 ? span * 0.025 : Math.max(Math.abs(minStrike) * 0.01, 1);

    return {
        min: minStrike - padding,
        max: maxStrike + padding
    };
}

function resolveZoomRange(payload, strikes) {
    const dataMin = Math.min(...strikes);
    const dataMax = Math.max(...strikes);
    const zoom = payload?.batch?.[0] || payload || {};
    const startValue = Number(zoom.startValue);
    const endValue = Number(zoom.endValue);

    if (Number.isFinite(startValue) && Number.isFinite(endValue)) {
        return { startValue, endValue };
    }

    const startPct = Number(zoom.start);
    const endPct = Number(zoom.end);
    if (Number.isFinite(startPct) && Number.isFinite(endPct)) {
        const span = dataMax - dataMin;
        return {
            startValue: dataMin + (span * startPct / 100),
            endValue: dataMin + (span * endPct / 100),
        };
    }

    return { startValue: dataMin, endValue: dataMax };
}

function applyProfileAxisBounds(chart, rows, startValue, endValue) {
    const visibleRows = rowsInStrikeRange(rows, startValue, endValue);
    const { netBounds, sideBounds } = profileAxisBounds(visibleRows);

    chart.setOption({
        yAxis: [
            { min: netBounds.min, max: netBounds.max },
            { min: sideBounds.min, max: sideBounds.max }
        ]
    }, false);
}

function attachProfileAxisAutoscale(id, rows) {
    const chart = getChart(id);
    if (!chart || !rows.length) return;
    profileAutoscaleState[id] = {
        rows,
        strikes: rows.map(row => row.strike)
    };
}

function focusedStrikeWindow(strikes, markers, spotPrice) {
    const finiteStrikes = strikes.filter(Number.isFinite);
    if (!finiteStrikes.length || !Number.isFinite(spotPrice) || spotPrice <= 0) {
        return { startValue: null, endValue: null };
    }

    const dataMin = Math.min(...finiteStrikes);
    const dataMax = Math.max(...finiteStrikes);
    const relevant = [
        spotPrice,
        ...(markers || []).map(level => level.value).filter(Number.isFinite)
    ];
    const relevantMin = Math.min(...relevant);
    const relevantMax = Math.max(...relevant);
    const minHalfWindow = spotPrice * 0.015;
    const markerHalfWindow = Math.max((relevantMax - relevantMin) * 1.4, minHalfWindow);
    const center = (Math.min(relevantMin, spotPrice) + Math.max(relevantMax, spotPrice)) / 2;
    const startValue = Math.max(dataMin, center - markerHalfWindow);
    const endValue = Math.min(dataMax, center + markerHalfWindow);

    if (endValue - startValue < (dataMax - dataMin) * 0.15) {
        const expandedHalf = Math.max((dataMax - dataMin) * 0.075, minHalfWindow);
        return {
            startValue: Math.max(dataMin, center - expandedHalf),
            endValue: Math.min(dataMax, center + expandedHalf)
        };
    }

    return { startValue, endValue };
}

function buildMarkerLines(markerLevels, strikes) {
    const finiteStrikes = strikes.filter(Number.isFinite);
    const xSpan = finiteStrikes.length ? Math.max(...finiteStrikes) - Math.min(...finiteStrikes) : 0;
    const crowdDistance = xSpan * 0.025;

    return markerLevels
        .slice()
        .sort((a, b) => a.value - b.value)
        .map((level, index, sortedLevels) => {
            const crowded = crowdDistance > 0 && sortedLevels.some(other => {
                return other !== level && Math.abs(other.value - level.value) <= crowdDistance;
            });
            const showLabel = !crowded || level.label === 'Spot';

            return {
                xAxis: level.value,
                name: level.label,
                lineStyle: {
                    color: level.color,
                    width: level.label === 'Spot' ? 2 : 1.5,
                    type: level.dash === 'dot' ? 'dotted' : level.dash === 'dash' ? 'dashed' : 'solid'
                },
                label: {
                    show: showLabel,
                    formatter: showLabel && !crowded ? `${level.label}\n${formatTargetPrice(level.value)}` : level.label,
                    position: index % 2 === 0 ? 'insideEndTop' : 'insideStartTop',
                    color: level.color,
                    backgroundColor: 'rgba(2,5,8,0.82)',
                    borderColor: level.color,
                    borderWidth: 1,
                    borderRadius: 3,
                    padding: [3, 5],
                    fontFamily: NUMERIC_FONT,
                    fontSize: 10,
                    fontWeight: 700
                }
            };
        });
}

function profileTooltipFormatter(rows, valueFormatter = formatCompactNumber) {
    return params => {
        const items = Array.isArray(params) ? params : [params];
        const rawAxisValue = Number(items[0]?.axisValue ?? (Array.isArray(items[0]?.value) ? items[0].value[0] : NaN));
        const nearest = rows.reduce((best, row) => {
            if (!best) return row;
            return Math.abs(row.strike - rawAxisValue) < Math.abs(best.strike - rawAxisValue) ? row : best;
        }, null);

        if (!nearest) return '';

        return [
            `<strong>${formatAxisPrice(nearest.strike)}</strong>`,
            `<span style="color:#ff8b1a">&bull;</span> Call GEX: ${valueFormatter(nearest.call)}`,
            `<span style="color:#2388e8">&bull;</span> Put GEX: ${valueFormatter(nearest.put)}`,
            `<span style="color:#b177ff">&bull;</span> Net GEX by Strike: ${valueFormatter(nearest.net)}`,
        ].join('<br/>');
    };
}

function baseChartOptions({ valueFormatter = formatCompactNumber } = {}) {
    return {
        backgroundColor: 'transparent',
        textStyle: chartText(),
        animationDuration: 280,
        tooltip: {
            trigger: 'axis',
            backgroundColor: '#020508',
            borderColor: '#223140',
            borderWidth: 1,
            textStyle: chartText(13, '#f2f5f8'),
            formatter: params => {
                const items = Array.isArray(params) ? params : [params];
                const axisLabel = items[0]?.axisValueLabel || items[0]?.axisValue || '';
                const lines = [`<strong>${axisLabel}</strong>`];
                items.forEach(item => {
                    const raw = Array.isArray(item.value) ? item.value[1] : item.value;
                    lines.push(`${item.marker}${item.seriesName}: ${valueFormatter(Number(raw), item)}`);
                });
                return lines.join('<br/>');
            },
            axisPointer: {
                type: 'cross',
                label: {
                    backgroundColor: '#101720',
                    color: '#f2f5f8',
                    fontFamily: NUMERIC_FONT,
                    fontSize: 12
                }
            }
        }
    };
}


// --- Init ---
async function init() {
    if (initInFlight) return initInFlight;
    initInFlight = (async () => {
        setWorkspaceState('loading', 'Connecting to your local market data…');
        try {
            currentSettings = { ...currentSettings, ...await apiCall('get_settings') };
            applyTheme(currentSettings.theme);
            for (const [id, value] of Object.entries({
                settingInterval: currentSettings.refresh_interval,
                settingTheme: currentSettings.theme || 'dark',
                settingSymbols: (currentSettings.symbols || []).join(','),
                settingRateLimit: currentSettings.api_rate_limit_per_second || 10,
                settingRateUtilization: currentSettings.api_rate_limit_utilization || 0.6,
                settingMinPoll: currentSettings.min_poll_interval_seconds || 15,
                settingMaxPoll: currentSettings.max_poll_interval_seconds || 120,
                settingRetentionDays: currentSettings.raw_retention_days || 30,
            })) document.getElementById(id).value = value;
            await discoverSymbols();
            initialized = true;
            await loadSymbol();
        } catch (error) {
            setWorkspaceState('error', error.message || String(error));
        } finally {
            startTimers();
            startStatusTimer();
            initInFlight = null;
        }
    })();
    return initInFlight;
}

async function discoverSymbols() {
    const symbols = await apiCall('get_symbols');
    const selector = document.getElementById('symbolSelector');
    let preferred;
    try { preferred = window.localStorage?.getItem('opengamma-symbol'); } catch (_) {}
    const previous = selector.value;
    const available = [...new Set([...(symbols || []), ...(currentSettings.symbols || [])])];
    selector.replaceChildren(...available.map(symbol => new Option(symbol, symbol)));
    selector.value = available.includes(previous) ? previous : available.includes(preferred) ? preferred : available.includes(currentSettings.symbols?.[0]) ? currentSettings.symbols[0] : available[0] || '';
    if (!available.length) selector.add(new Option('No symbols configured', ''));
    return available;
}

function switchView(viewName) {
    const target = document.getElementById(`view-${viewName}`);
    if (!target) return;
    activeView = viewName;
    document.body.dataset.activeView = viewName;
    document.querySelectorAll('.view-section').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.nav-btn').forEach(el => {
        const active = el.dataset.view === viewName;
        el.classList.toggle('active', active);
        if (active) el.setAttribute('aria-current', 'page');
        else el.removeAttribute('aria-current');
    });
    target.style.display = ['cockpit', 'edge-lab', 'trace'].includes(viewName) ? 'grid' : viewName === 'one-off' ? 'flex' : 'block';
    const labels = {cockpit:'Market cockpit', 'edge-lab':'Historical edge', regime:'Market regime', trace:'Session replay', analysis:'Strike matrix', 'one-off':'One-off analysis', settings:'Settings'};
    setText('viewTitle', labels[viewName]);
    Promise.resolve(viewName === 'one-off' ? loadOneOffProfiles() : renderActiveView()).catch(error => {
        showToast('View unavailable', error.message || String(error), 'error');
    });
    if (viewName === 'one-off' && cachedOneOffData) renderOneOffProfile(cachedOneOffData);
    resizeCharts();
}

async function loadSymbol() {
    const symbol = document.getElementById('symbolSelector').value;
    if (!symbol) {
        setWorkspaceState('empty', 'No symbols configured. Add symbols in Settings to begin.');
        return false;
    }
    try { window.localStorage?.setItem('opengamma-symbol', symbol); } catch (_) {}
    if (cachedSymbol !== symbol) {
        traceGeneration++;
        edgeGeneration++;
        setText('topSpot', '--');
        setText('lastUpdate', '--');
        setWorkspaceState('loading', `Loading ${symbol} market data…`);
    }

    const result = await symbolRequestCoordinator.request(
        symbol,
        () => apiCall('get_decision_workspace', symbol)
    );
    const selectedSymbol = document.getElementById('symbolSelector').value;
    if (!symbolRequestCoordinator.isCurrent(result, selectedSymbol)) return false;
    if (result.generation === appliedSymbolGeneration) return true;
    if (result.error) {
        console.error(result.error);
        setWorkspaceState('error', `Could not load ${symbol}. ${result.error.message || result.error}`);
        return false;
    }

    const workspace = result.value;

    if (!workspace || workspace.error || !workspace.dashboard?.snapshot) {
        setWorkspaceState('empty', workspace?.error || `No snapshot for ${symbol}. The app will retry automatically as data arrives.`);
        return false;
    }
    const data = workspace.dashboard;

    const symbolChanged = cachedSymbol && cachedSymbol !== symbol;
    if (symbolChanged) {
        for (const id of Object.keys(chartInstances).filter(id => !id.startsWith('oneOff'))) {
            chartInstances[id]?.dispose();
            delete chartInstances[id];
            delete chartZoomState[id];
        }
        cachedTraceData = null;
        lastAnalysisSnapshot = null;
    }
    appliedSymbolGeneration = result.generation;
    cachedData = data;
    cachedSymbol = symbol;
    cachedWorkspace = workspace;
    setWorkspaceState('ready');
    recordActivity(ActivityFeed.workspaceActivity(symbol, workspace));
    renderSharedSnapshot(data);
    renderMarketContext(workspace.market_context);
    const snapshotKey = `${symbol}:${workspace.snapshot_id}`;
    const snapshotChanged = snapshotKey !== lastRenderedSnapshot;
    lastRenderedSnapshot = snapshotKey;
    if (document.getElementById('view-cockpit').style.display !== 'none') {
        await loadCockpit(result, snapshotChanged);
    }
    if (document.getElementById('view-edge-lab').style.display !== 'none') {
        await loadEdgeLab(result);
    }
    if (
        document.getElementById('view-regime').style.display === 'block'
    ) {
        await loadOverview(result);
    }
    if (document.getElementById('view-trace').style.display !== 'none') {
        if (snapshotChanged || !cachedTraceData) await loadTrace(result);
    }
    if (activeView === 'analysis') renderActiveView();
    if (symbolRequestCoordinator.isCurrent(result, document.getElementById('symbolSelector').value)) {
        timeLeft = currentSettings.refresh_interval;
        return true;
    }
    return false;
}

function toggleGammaSweepOverlay(enabled) {
    gammaSweepOverlayEnabled = Boolean(enabled);
    const cockpitToggle = document.getElementById('cockpitGammaSweepToggle');
    const oneOffToggle = document.getElementById('oneOffGammaSweepToggle');
    if (cockpitToggle) cockpitToggle.checked = gammaSweepOverlayEnabled;
    if (oneOffToggle) oneOffToggle.checked = gammaSweepOverlayEnabled;
    if (cachedData && cockpitModel) {
        renderCockpitProfileChart(cachedData.profile, cachedData.snapshot.spot_price, cockpitModel, cachedData.gamma_sweep);
        renderSweepChart('cockpitSweepChart', cachedData.gamma_sweep, cachedData.snapshot);
    }
    if (cachedOneOffData) {
        renderSweepChart('oneOffSweepChart', cachedOneOffData.gamma_sweep, cachedOneOffData.snapshot);
    }
}

function renderSharedSnapshot(data) {
    const snap = data?.snapshot || {};
    const topSpot = document.getElementById('topSpot');
    const cockpitSymbol = document.getElementById('cockpitSymbol');
    if (topSpot) topSpot.innerText = hasNumber(snap.spot_price)
        ? Number(snap.spot_price).toFixed(2)
        : '--';
    if (cockpitSymbol) cockpitSymbol.innerText = snap.symbol || cachedSymbol || '--';
    const dateObj = new Date(snap.timestamp);
    const lastUpdate = document.getElementById('lastUpdate');
    if (lastUpdate) lastUpdate.innerText = Number.isNaN(dateObj.getTime()) ? '--:--' : dateObj.toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
    const fresh = !Number.isNaN(dateObj.getTime()) && Date.now() - dateObj.getTime() <= 180000;
    setClassText('marketState', fresh ? 'FRESH' : 'STALE', `status-pill ${fresh ? 'status-healthy' : 'status-stale'}`);

}

function renderProfileChartTo(chartId, profileData, spotPrice) {
    let strikeMap = {};
    profileData.forEach(row => {
        if (!strikeMap[row.strike_price]) strikeMap[row.strike_price] = { call: 0, put: 0, net: 0 };
        if (optionSide(row) === 'call') strikeMap[row.strike_price].call += row.gex_value;
        else strikeMap[row.strike_price].put += row.gex_value; // Puts are negative
        strikeMap[row.strike_price].net += row.gex_value;
    });

    const strikes = Object.keys(strikeMap).map(parseFloat).sort((a, b) => a - b);
    const netGexArr = strikes.map(s => strikeMap[s].net);
    const callGexArr = strikes.map(s => strikeMap[s].call);
    const putGexArr = strikes.map(s => strikeMap[s].put); // Negative values
    const profileRows = strikes.map((strike, i) => ({
        strike,
        call: callGexArr[i],
        put: putGexArr[i],
        net: netGexArr[i],
    }));
    const { netBounds, sideBounds } = profileAxisBounds(profileRows);
    const xBounds = strikeAxisBounds(strikes);

    const option = {
        ...baseChartOptions(),
        grid: { top: 30, left: 62, right: 62, bottom: 42, containLabel: true },
        dataZoom: zoomDataOptions(),
        xAxis: {
            type: 'value',
            name: 'Strike',
            nameLocation: 'middle',
            nameGap: 30,
            nameTextStyle: chartText(12),
            axisLabel: { ...chartText(12), formatter: formatAxisPrice },
            axisLine: { onZero: false, lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: 'rgba(122,148,170,0.12)' } },
            ...xBounds
        },
        yAxis: [
            {
                type: 'value',
                name: 'Net GEX by Strike',
                min: netBounds.min,
                max: netBounds.max,
                nameTextStyle: chartText(12, '#b177ff'),
                axisLabel: { ...chartText(12, '#b177ff'), formatter: formatCompactNumber },
                axisLine: { lineStyle: { color: '#223140' } },
                splitLine: { lineStyle: { color: 'rgba(122,148,170,0.12)' } }
            },
            {
                type: 'value',
                name: 'Total Call/Put GEX',
                min: sideBounds.min,
                max: sideBounds.max,
                nameTextStyle: chartText(12),
                axisLabel: { ...chartText(12), formatter: formatCompactNumber },
                axisLine: { lineStyle: { color: '#223140' } },
                splitLine: { show: false }
            }
        ],
        series: [
            {
                name: 'Call GEX',
                type: 'bar',
                yAxisIndex: 1,
                stack: 'gex',
                data: strikes.map((s, i) => [s, callGexArr[i]]),
                itemStyle: { color: '#ff8b1a', opacity: 0.76 },
                barWidth: 10
            },
            {
                name: 'Put GEX',
                type: 'bar',
                yAxisIndex: 1,
                stack: 'gex',
                data: strikes.map((s, i) => [s, putGexArr[i]]),
                itemStyle: { color: '#2388e8', opacity: 0.76 },
                barWidth: 10
            },
            {
                name: 'Net GEX by Strike',
                type: 'line',
                smooth: true,
                symbol: 'none',
                data: strikes.map((s, i) => [s, netGexArr[i]]),
                lineStyle: { color: '#b177ff', width: 3 },
                markLine: {
                    symbol: 'none',
                    silent: true,
                    data: [{
                        xAxis: spotPrice,
                        lineStyle: { color: '#ffffff', width: 1 },
                        label: {
                            formatter: 'Latest spot',
                            color: '#ffffff',
                            fontFamily: NUMERIC_FONT,
                            fontSize: 12
                        }
                    }]
                }
            }
        ]
    };

    setChartOption(chartId, option);
    attachProfileAxisAutoscale(chartId, profileRows);
    attachClickZoom(chartId, strikes);
}

function renderProfileChart(profileData, spotPrice) {
    renderProfileChartTo('profileChart', profileData, spotPrice);
}

function renderSweepChart(chartId, gammaSweep, snapshot = {}) {
    const el = document.getElementById(chartId);
    if (!el) return;

    const rows = gammaSweepOverlayEnabled ? sweepRowsFromPayload(gammaSweep, 1000000) : [];
    if (!rows.length) {
        el.style.display = 'none';
        const chart = chartInstances[chartId];
        if (chart && !chart.isDisposed()) chart.clear();
        return;
    }

    el.style.display = 'block';
    const spot = Number(snapshot?.spot_price ?? gammaSweep?.current?.spot);
    const current = gammaSweep?.current
        ? {
            spot: Number(gammaSweep.current.spot),
            netGex: Number(gammaSweep.current.net_gex) / 1000000,
            hedgeShares: Number(gammaSweep.current.hedge_shares)
        }
        : nearestSweepPoint(rows, spot);
    const zeroCrossings = (gammaSweep?.zero_crossings?.all || [])
        .map(Number)
        .filter(Number.isFinite)
        .map(value => ({
            xAxis: value,
            lineStyle: { color: '#ff454f', width: 1, type: 'dashed' },
            label: {
                formatter: `Zero Gamma ${formatTargetPrice(value)}`,
                color: '#ff8b8f',
                fontFamily: NUMERIC_FONT,
                fontSize: 11
            }
        }));
    const spotLine = Number.isFinite(spot) ? [{
        xAxis: spot,
        lineStyle: { color: '#ffffff', width: 1 },
        label: {
            formatter: 'Latest spot',
            color: '#ffffff',
            fontFamily: NUMERIC_FONT,
            fontSize: 11
        }
    }] : [];

    const option = {
        ...baseChartOptions({ valueFormatter: formatMillionsValue }),
        tooltip: {
            ...baseChartOptions({ valueFormatter: formatMillionsValue }).tooltip,
            formatter: params => {
                const items = Array.isArray(params) ? params : [params];
                const axisLabel = items[0]?.axisValueLabel || items[0]?.axisValue || '';
                const lines = [`<strong>${axisLabel}</strong>`];
                items.forEach(item => {
                    const raw = Array.isArray(item.value) ? item.value[1] : item.value;
                    const formatter = item.seriesName === 'Cumulative Hedge Demand'
                        ? formatShareCount
                        : formatMillionsValue;
                    lines.push(`${item.marker}${item.seriesName}: ${formatter(Number(raw))}`);
                });
                return lines.join('<br/>');
            }
        },
        grid: { top: 38, left: 62, right: 86, bottom: 42, containLabel: true },
        dataZoom: zoomDataOptions(),
        legend: {
            top: 4,
            right: 12,
            textStyle: chartText(11),
            itemWidth: 16,
            itemHeight: 8
        },
        xAxis: {
            type: 'value',
            name: 'Hypothetical Spot',
            nameLocation: 'middle',
            nameGap: 30,
            nameTextStyle: chartText(12),
            axisLabel: { ...chartText(12), formatter: formatAxisPrice },
            axisLine: { onZero: false, lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: CHART_GRID } },
            ...strikeAxisBounds(rows.map(row => row.spot))
        },
        yAxis: [
            {
                type: 'value',
                name: 'Modeled Net GEX (M)',
                ...chartBounds(rows.map(row => row.netGex), 0.18),
                nameTextStyle: chartText(12, '#00d37f'),
                axisLabel: { ...chartText(12, '#00d37f'), formatter: formatMillionsAxis },
                axisLine: { lineStyle: { color: '#223140' } },
                splitLine: { lineStyle: { color: CHART_GRID } }
            },
            {
                type: 'value',
                name: 'Cumulative Hedge Demand',
                ...chartBounds(rows.map(row => row.hedgeShares), 0.18),
                nameTextStyle: chartText(12, '#f5a524'),
                axisLabel: { ...chartText(12, '#f5a524'), formatter: formatShareCount },
                axisLine: { lineStyle: { color: '#223140' } },
                splitLine: { show: false }
            }
        ],
        series: [
            {
                name: 'Modeled Net GEX Sweep',
                type: 'line',
                smooth: true,
                symbol: 'none',
                data: rows.map(row => [row.spot, row.netGex]),
                lineStyle: { color: '#00d37f', width: 2, type: 'dashed' },
                markLine: { symbol: 'none', silent: true, data: spotLine.concat(zeroCrossings) },
                markPoint: current && Number.isFinite(current.netGex) ? {
                    symbolSize: 44,
                    data: [{
                        coord: [current.spot, current.netGex],
                        value: current.netGex,
                        itemStyle: { color: '#00d37f' },
                        label: { formatter: formatMillionsValue(current.netGex), color: '#06120d', fontSize: 10 }
                    }]
                } : undefined
            },
            {
                name: 'Cumulative Hedge Demand',
                type: 'line',
                yAxisIndex: 1,
                smooth: true,
                symbol: 'none',
                data: rows.map(row => [row.spot, row.hedgeShares]),
                lineStyle: { color: '#f5a524', width: 2, type: 'dotted' },
                markPoint: current && Number.isFinite(current.hedgeShares) ? {
                    symbolSize: 44,
                    data: [{
                        coord: [current.spot, current.hedgeShares],
                        value: current.hedgeShares,
                        itemStyle: { color: '#f5a524' },
                        label: { formatter: formatShareCount(current.hedgeShares), color: '#1a1001', fontSize: 10 }
                    }]
                } : undefined
            }
        ]
    };

    setChartOption(chartId, option);
    attachClickZoom(chartId, rows.map(row => row.spot));
}

function renderHistoryChart(history) {
    if (!history || history.length === 0) return;

    const option = {
        ...baseChartOptions(),
        grid: { top: 12, left: 42, right: 22, bottom: 38, containLabel: true },
        dataZoom: zoomDataOptions(),
        xAxis: {
            type: 'category',
            data: history.map(d => d.timestamp),
            axisLabel: { ...chartText(12), hideOverlap: true, formatter: value => String(value).slice(11, 16) },
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: 'rgba(122,148,170,0.12)' } }
        },
        yAxis: {
            type: 'value',
            axisLabel: { ...chartText(12), formatter: formatCompactNumber },
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: 'rgba(122,148,170,0.12)' } }
        },
        series: [{
            name: 'Net GEX',
            type: 'line',
            smooth: true,
            symbol: 'none',
            data: history.map(d => d.total_net_gex),
            lineStyle: { color: '#b177ff', width: 2 },
            areaStyle: { color: 'rgba(177,119,255,0.16)' },
            markLine: {
                symbol: 'none',
                silent: true,
                data: [{
                    yAxis: 0,
                    lineStyle: { color: '#667584', width: 1, type: 'dotted' },
                    label: { show: false }
                }]
            }
        }]
    };

    setChartOption('traceTrendChart', option);
    attachClickZoom('traceTrendChart', history.map(d => d.timestamp), 16);
}

function traceMetricLabel(metric = traceMode) {
    if (metric === 'call_gex') return 'Call GEX';
    if (metric === 'put_gex') return 'Put GEX';
    if (metric === 'modeled_delta_pressure') return 'Modeled Delta Pressure';
    if (metric === 'modeled_charm_pressure') return 'Modeled Charm Pressure';
    return 'Net GEX';
}

function traceMetricColor(metric = traceMode) {
    if (metric === 'call_gex') return '#ff8b1a';
    if (metric === 'put_gex') return '#2388e8';
    if (metric === 'modeled_delta_pressure') return '#00d37f';
    if (metric === 'modeled_charm_pressure') return '#f5a524';
    return '#18c7b7';
}

function setTraceMode(mode) {
    const nextMode = ['net_gex', 'call_gex', 'put_gex', 'modeled_delta_pressure', 'modeled_charm_pressure'].includes(mode) ? mode : 'net_gex';
    const changed = nextMode !== traceMode;
    traceMode = nextMode;
    if (changed) resetChartsZoom(['traceHeatmapChart', 'traceProfileChart']);
    document.querySelectorAll('[data-trace-mode]').forEach(button => {
        button.classList.toggle('active', button.dataset.traceMode === traceMode);
    });
    if (cachedTraceData) renderTraceView(cachedTraceData);
}

function resetTraceCharts() {
    resetChartsZoom(['traceHeatmapChart', 'traceProfileChart']);
    if (cachedTraceData) renderTraceView(cachedTraceData);
}

function updateTraceTimelineControl(range) {
    const slider = document.getElementById('traceTimelineSlider');
    const fill = document.getElementById('traceTrackFill');
    const scrubber = document.getElementById('traceScrubber');
    if (slider) slider.value = String(range.index);
    if (fill) fill.style.width = `${range.positionPct}%`;
    if (scrubber) scrubber.style.left = `${range.positionPct}%`;
    setText('traceTimelineValue', range.label || '--:--');
}

function configureTraceTimeline(buckets) {
    traceTimelineBuckets = Array.isArray(buckets) ? buckets : [];
    const slider = document.getElementById('traceTimelineSlider');
    const latestIndex = Math.max(0, traceTimelineBuckets.length - 1);
    const range = traceTimelineRange(traceTimelineBuckets, latestIndex);
    traceTimelineWindowSize = Math.max(1, range.endIndex - range.startIndex + 1);
    if (slider) {
        slider.min = '0';
        slider.max = String(latestIndex);
        slider.disabled = traceTimelineBuckets.length <= 1;
    }
    updateTraceTimelineControl(range);
}

function scrubTraceTimeline(rawIndex) {
    if (!traceTimelineBuckets.length) return;
    const range = traceTimelineRange(
        traceTimelineBuckets,
        rawIndex,
        traceTimelineWindowSize
    );
    updateTraceTimelineControl(range);
    const chart = chartInstances.traceHeatmapChart;
    if (!chart || chart.isDisposed()) return;
    zoomChartToIndexRange(
        chart,
        traceTimelineBuckets,
        range.startIndex,
        range.endIndex
    );
}

function syncTraceTimelineFromChart(chart, buckets = traceTimelineBuckets) {
    if (!chart || !buckets?.length) return;
    const { endIndex } = currentZoomIndices(chart, buckets);
    const range = traceTimelineRange(buckets, endIndex, traceTimelineWindowSize);
    updateTraceTimelineControl(range);
}

function isSymbolContextCurrent(symbol, requestContext = null) {
    const selectedSymbol = document.getElementById('symbolSelector')?.value;
    return selectedSymbol === symbol && (
        !requestContext || symbolRequestCoordinator.isCurrent(requestContext, selectedSymbol)
    );
}

async function loadTrace(requestContext = null) {
    const generation = ++traceGeneration;
    const symbol = document.getElementById('symbolSelector')?.value || cachedSymbol || 'SPX';
    const statusEl = document.getElementById('traceStatus');
    const traceView = document.getElementById('view-trace');
    traceView.setAttribute('aria-busy', 'true');
    if (statusEl) statusEl.innerText = 'Loading session…';

    try {
        const dateSelect = document.getElementById('traceSessionDate');
        if (traceDatesSymbol !== symbol) {
            const dates = await apiCall('get_trace_dates', symbol, 30);
            if (generation !== traceGeneration || !isSymbolContextCurrent(symbol, requestContext)) return;
            if (dateSelect) {
                dateSelect.innerHTML = dates.map((value, index) => `<option value="${value}"${index === 0 ? ' selected' : ''}>${value}</option>`).join('') || '<option value="">No sessions</option>';
            }
            traceDatesSymbol = symbol;
        }
        const sessionDate = dateSelect?.value || null;
        const data = await apiCall('get_trace_data', symbol, 390, sessionDate);
        if (generation !== traceGeneration || !isSymbolContextCurrent(symbol, requestContext)) return;
        if (data.error) {
            if (statusEl) statusEl.innerText = data.error;
            showToast('TRACE unavailable', data.error, 'info');
            return;
        }
        if (cachedTraceData?.symbol !== data.symbol || cachedTraceData?.session_date !== data.session_date) resetChartsZoom(['traceHeatmapChart', 'traceProfileChart', 'traceTrendChart']);
        cachedTraceData = data;
        renderTraceView(data);
    } catch (error) {
        if (generation !== traceGeneration || !isSymbolContextCurrent(symbol, requestContext)) return;
        console.error('TRACE load failed', error);
        if (statusEl) statusEl.innerText = 'Load failed';
        showToast('TRACE unavailable', 'Could not load local heatmap data.', 'info');
    } finally { if (generation === traceGeneration) traceView.setAttribute('aria-busy', 'false'); }
}

function renderTraceView(data) {
    if (!data) return;
    const profileRows = Array.isArray(data.latest_profile) ? data.latest_profile : [];
    const heatmapRows = Array.isArray(data.heatmap) ? data.heatmap : [];
    const buckets = [...new Set(heatmapRows.map(row => row.timestamp))].sort();
    const gross = profileRows.reduce((sum, row) => sum + Math.abs(Number(row.net_gex || 0)), 0);
    const net = Math.abs(Number(data.total_net_gex || 0));
    const stability = gross > 0 ? Math.round(Math.min(99, Math.max(1, (net / gross) * 100))) : 0;
    const date = data.timestamp ? new Date(data.timestamp) : null;
    const latestTime = date ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '--:--';
    const startTime = buckets[0] ? String(buckets[0]).slice(11, 16) : '--:--';
    const endTime = buckets[buckets.length - 1] ? String(buckets[buckets.length - 1]).slice(11, 16) : latestTime;

    setText('traceSymbol', data.symbol || '--');
    setText('traceSpot', data.spot_price ? formatTargetPrice(data.spot_price) : '--');
    setText('traceNetGex', formatMoneyM(data.total_net_gex || 0, 0));
    setText('traceStability', stability ? `${stability}%` : '--');
    setText('traceCells', formatCompactNumber(data.range?.cells || heatmapRows.length || 0));
    setText('traceUpdated', latestTime);
    setText('traceStatus', `${traceMetricLabel()} | ${buckets.length || 0} time buckets`);
    setText('traceTrendSession', `Selected session: ${String(data.timestamp || '').slice(0, 10)}`);
    setText('traceStartTime', startTime);
    setText('traceEndTime', endTime);
    configureTraceTimeline(buckets);
    setText('traceScaleLabel', traceMode.startsWith('modeled_') ? `${traceMetricLabel()} (modeled contract exposure)` : `${traceMetricLabel()} ($ Notional)`);
    setText('traceCoverageWarning', traceMode.startsWith('modeled_') ? (data.modeled_pressure_coverage?.warning || `Modeled pressure coverage ${Math.round(Number(data.modeled_pressure_coverage?.ratio || 0) * 100)}%`) : '');

    renderTraceHeatmap(data);
    renderTraceProfile(data);
    renderHistoryChart(data.history || []);
}

function buildTraceCandles(spotTicks, buckets) {
    const grouped = {};
    (spotTicks || []).forEach(tick => {
        const ts = String(tick.timestamp || '');
        const bucket = ts.length >= 16 ? `${ts.slice(0, 16)}:00` : tick.timestamp;
        const spot = Number(tick.spot_price);
        if (!bucket || !Number.isFinite(spot)) return;
        if (!grouped[bucket]) grouped[bucket] = [];
        grouped[bucket].push(spot);
    });

    let previousClose = null;
    return buckets.map(bucket => {
        const values = grouped[bucket] || [];
        if (!values.length) {
            if (!Number.isFinite(previousClose)) return null;
            return [previousClose, previousClose, previousClose, previousClose];
        }
        const open = Number.isFinite(previousClose) ? previousClose : values[0];
        const close = values[values.length - 1];
        const low = Math.min(open, close, ...values);
        const high = Math.max(open, close, ...values);
        previousClose = close;
        return [open, close, low, high];
    });
}

function traceStrikeBounds(data) {
    const strikes = [...new Set((data.heatmap || data.latest_profile || []).map(row => Number(row.strike)))].filter(Number.isFinite).sort((a,b) => a-b);
    if (!strikes.length) return {};
    const steps = strikes.slice(1).map((strike,index) => strike-strikes[index]).filter(step => step > 0).sort((a,b)=>a-b);
    const step = steps[Math.floor(steps.length / 2)] || 1;
    const spot = Number(data.spot_price);
    return spot > 0 ? {min: Math.max(strikes[0] - step / 2, Math.floor(spot * .985 / step) * step), max: Math.min(strikes.at(-1) + step / 2, Math.ceil(spot * 1.015 / step) * step), step} : {min:strikes[0]-step/2,max:strikes.at(-1)+step/2,step};
}

function toggleTraceOverlays(enabled) {
    traceOverlaysEnabled = enabled;
    if (cachedTraceData) renderTraceHeatmap(cachedTraceData);
}

function renderTraceHeatmap(data) {
    const heatmap = Array.isArray(data.heatmap) ? data.heatmap : [];
    const spotPath = Array.isArray(data.spot_path) ? data.spot_path : [];
    const spotTicks = Array.isArray(data.spot_ticks) ? data.spot_ticks : [];
    const chart = getChart('traceHeatmapChart');
    if (!chart) return;

    if (!heatmap.length) {
        chart.clear();
        return;
    }

    const buckets = [...new Set(heatmap.map(row => row.timestamp))].sort();
    const hadStoredZoom = Boolean(chartZoomState.traceHeatmapChart);
    const bucketIndex = new Map(buckets.map((bucket, index) => [bucket, index]));
    const valuesM = heatmap.map(row => Number(row[traceMode] || 0) / 1000000).filter(Number.isFinite);
    const maxAbs = Math.max(...valuesM.map(value => Math.abs(value)), 1);
    const minValue = traceMode === 'call_gex' ? 0 : traceMode === 'put_gex' ? -maxAbs : -maxAbs;
    const maxValue = traceMode === 'put_gex' ? 0 : maxAbs;
    setText('traceScaleMax', formatCompactNumber(maxValue * 1000000));
    setText('traceScaleMin', formatCompactNumber(minValue * 1000000));
    const heatmapData = heatmap
        .map(row => [bucketIndex.get(row.timestamp), Number(row.strike), Number(row[traceMode] || 0) / 1000000])
        .filter(row => Number.isFinite(row[0]) && Number.isFinite(row[1]) && Number.isFinite(row[2]));
    const priceData = spotPath
        .map(row => [bucketIndex.get(row.timestamp), Number(row.spot_price)])
        .filter(row => Number.isFinite(row[0]) && Number.isFinite(row[1]));
    const candleData = buildTraceCandles(spotTicks, buckets);
    const strikes = heatmap.map(row => Number(row.strike)).filter(Number.isFinite);
    const yBounds = traceStrikeBounds(data);
    const labelStep = Math.max(1, Math.ceil(buckets.length / 8));
    const currentSpot = Number(data.spot_price);
    const flip = Number(data.flip_strike);

    const neutralColor = document.documentElement.dataset.theme === 'light' ? '#edf1f6' : '#111820';
    const colorRange = traceMode === 'call_gex'
        ? [neutralColor, '#8f5a17', '#ff8b1a']
        : traceMode === 'put_gex'
            ? ['#2388e8', '#262d6e', neutralColor]
            : ['#2388e8', neutralColor, '#ff8b1a'];

    document.querySelector('.trace-gradient').style.background = `linear-gradient(180deg, ${[...colorRange].reverse().join(', ')})`;
    const markerLines = [];
    if (Number.isFinite(currentSpot) && currentSpot > 0) {
        markerLines.push({
            yAxis: currentSpot,
            lineStyle: { color: CHART_TEXT, width: 1 },
            label: {
                formatter: 'Latest spot',
                position: 'insideEndTop',
                color: CHART_TEXT,
                fontFamily: NUMERIC_FONT,
                fontSize: 11
            }
        });
    }
    (traceOverlaysEnabled ? (data.overlays || []).slice(-20) : []).forEach(overlay => {
        const bucket = String(overlay.timestamp || '').slice(0, 16) + ':00';
        const index = bucketIndex.get(bucket);
        if (Number.isFinite(index)) markerLines.push({
            xAxis: index,
            lineStyle: { color: overlay.overlay_type === 'alert' ? '#ff454f' : '#f5a524', width: 1, type: 'dashed' },
            label: { show: overlay.overlay_type === 'alert', formatter: overlay.label || overlay.overlay_type, color: CHART_TEXT, fontSize: 10 }
        });
    });
    if (Number.isFinite(flip) && flip > 0) {
        markerLines.push({
            yAxis: flip,
            lineStyle: { color: '#ff454f', width: 1, type: 'dashed' },
            label: { formatter: `Flip ${formatTargetPrice(flip)}`, color: '#ff8b8f', fontSize: 11 }
        });
    }

    const option = {
        ...baseChartOptions({ valueFormatter: formatMillionsValue }),
        backgroundColor: 'transparent',
        grid: { top: 28, left: 72, right: 18, bottom: 60, containLabel: false },
        dataZoom: zoomDataOptions(Math.max(0, buckets.length - traceTimelineWindowSize), buckets.length - 1),
        tooltip: {
            trigger: 'item',
            backgroundColor: '#071018',
            borderColor: '#223140',
            textStyle: chartText(12, '#f2f5f8'),
            formatter: params => {
                if (params.seriesName === 'Spot Path') {
                    return `<strong>${buckets[params.value[0]]?.slice(11, 16) || ''}</strong><br/>Spot: ${formatTargetPrice(params.value[1])}`;
                }
                if (params.seriesName === 'Price Candles') {
                    return [
                        `<strong>${buckets[params.dataIndex]?.slice(11, 16) || ''}</strong>`,
                        `Open: ${formatTargetPrice(params.value[0])}`,
                        `Close: ${formatTargetPrice(params.value[1])}`,
                        `Low: ${formatTargetPrice(params.value[2])}`,
                        `High: ${formatTargetPrice(params.value[3])}`
                    ].join('<br/>');
                }
                const time = buckets[params.value[0]]?.slice(11, 16) || '';
                return [
                    `<strong>${time}</strong>`,
                    `Strike: ${formatAxisPrice(params.value[1])}`,
                    `${traceMetricLabel()}: ${formatMillionsValue(params.value[2])}`
                ].join('<br/>');
            }
        },
        visualMap: {
            show: false,
            seriesIndex: 0,
            dimension: 2,
            min: minValue,
            max: maxValue,
            calculable: true,
            orient: 'vertical',
            right: 12,
            top: 54,
            bottom: 54,
            textStyle: chartText(11),
            inRange: { color: colorRange }
        },
        xAxis: {
            type: 'category',
            data: buckets,
            axisLabel: {
                ...chartText(11),
                hideOverlap: true, formatter: value => String(value).slice(11, 16)
            },
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { show: false }
        },
        yAxis: {
            type: 'value',
            name: 'Strike / Price ($)',
            nameTextStyle: chartText(12),
            axisLabel: { ...chartText(11), formatter: formatAxisPrice },
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: 'rgba(122,148,170,0.11)' } },
            ...yBounds
        },
        series: [
            {
                name: traceMetricLabel(),
                type: 'custom',
                data: heatmapData,
                encode: {x:0, y:1, tooltip:2},
                progressive: 0,
                renderItem: (params, api) => {
                    const point = api.coord([api.value(0), api.value(1)]);
                    const size = api.size([1, yBounds.step || 5]);
                    const shape = echarts.graphic.clipRectByRect({x:point[0]-size[0]/2, y:point[1]-Math.abs(size[1])/2, width:Math.max(1,size[0]), height:Math.max(1,Math.abs(size[1]))}, params.coordSys);
                    return shape && {type:'rect',shape,style:{fill:api.visual('color'),opacity:.9}};
                },
                emphasis: { itemStyle: { borderColor: '#ffffff', borderWidth: 1 } }
            },
            {
                name: 'Price Candles',
                type: 'candlestick',
                data: candleData,
                itemStyle: {
                    color: '#f2f5f8',
                    color0: '#101720',
                    borderColor: '#d8e3ee',
                    borderColor0: '#667584'
                },
                barWidth: '36%',
                z: 6
            },
            {
                name: 'Spot Path',
                type: 'line',
                symbol: 'none',
                data: priceData,
                lineStyle: { color: '#2f9bff', width: 2 },
                z: 7,
                markLine: markerLines.length ? { symbol: 'none', silent: true, data: markerLines } : undefined
            }
        ]
    };

    setChartOption('traceHeatmapChart', option, hadStoredZoom);
    attachClickZoom('traceHeatmapChart', buckets, 18);
    syncTraceTimelineFromChart(chart, buckets);
}

function renderTraceProfile(data) {
    let rows = Array.isArray(data.latest_profile) ? data.latest_profile : [];
    if (traceMode.startsWith('modeled_')) {
        const heatmap = Array.isArray(data.heatmap) ? data.heatmap : [];
        const latestBucket = heatmap.map(row => row.timestamp).sort().at(-1);
        rows = heatmap.filter(row => row.timestamp === latestBucket);
    }
    const chart = getChart('traceProfileChart');
    if (!chart) return;

    if (!rows.length) {
        chart.clear();
        return;
    }

    const metric = traceMode;
    const values = rows.map(row => Number(row[metric] || 0) / 1000000);
    const bounds = chartBounds(values, 0.18);
    const strikes = rows.map(row => Number(row.strike)).filter(Number.isFinite);
    const yBounds = traceStrikeBounds(data);
    const maxAbs = Math.max(...values.map(value => Math.abs(value)), 1);
    const option = {
        ...baseChartOptions({ valueFormatter: formatMillionsValue }),
        backgroundColor: 'transparent',
        grid: { top: 28, left: 18, right: 18, bottom: 60, containLabel: false },
        tooltip: {
            trigger: 'item',
            backgroundColor: '#071018',
            borderColor: '#223140',
            textStyle: chartText(12, '#f2f5f8'),
            formatter: params => {
                return `<strong>${formatAxisPrice(params.value[1])}</strong><br/>${traceMetricLabel()}: ${formatMillionsValue(params.value[0])}`;
            }
        },
        xAxis: {
            type: 'value',
            min: Math.min(bounds.min, -maxAbs),
            max: Math.max(bounds.max, maxAbs),
            splitNumber: 2, axisLabel: { ...chartText(10), hideOverlap:true, formatter: value => formatCompactNumber(value * 1000000) },
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: 'rgba(122,148,170,0.11)' } }
        },
        yAxis: {
            type: 'value',
            axisLabel: { show: false },
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { show: false },
            ...yBounds
        },
        series: [{
            name: traceMetricLabel(),
            type: 'custom',
            data: rows.map(row => [Number(row[metric] || 0) / 1000000, Number(row.strike)]),
            encode: { x: 0, y: 1 },
            renderItem: (params, api) => {
                const value = Number(api.value(0));
                const strike = Number(api.value(1));
                const zero = api.coord([0, strike]);
                const end = api.coord([value, strike]);
                const height = Math.max(3, Math.min(12, api.size([0, 5])[1] * 0.72));
                const x = Math.min(zero[0], end[0]);
                const shape = echarts.graphic.clipRectByRect({
                    x, y: end[1] - height / 2,
                    width: Math.max(1, Math.abs(end[0] - zero[0])), height
                }, params.coordSys);
                return shape && { type: 'rect', shape, style: api.style() };
            },
            itemStyle: {
                color: params => {
                    const value = Array.isArray(params.value) ? Number(params.value[0]) : Number(params.value);
                    if (metric === 'call_gex') return '#ff8b1a';
                    if (metric === 'put_gex') return '#2388e8';
                    return value >= 0 ? '#b177ff' : '#ff454f';
                }
            },
            markLine: {
                symbol: 'none',
                silent: true,
                data: [{
                    xAxis: 0,
                    lineStyle: { color: '#667584', width: 1, type: 'dotted' },
                    label: { show: false }
                }]
            }
        }]
    };

    setChartOption('traceProfileChart', option);
}

// --- Analysis Table (Matches previous redesign) ---
function jumpToSpot() {
    const cells = [...document.querySelectorAll('#analysisTableBody .strike-cell')];
    const spot = Number(cachedData?.snapshot?.spot_price);
    if (!cells.length || !Number.isFinite(spot)) return;
    const nearest = cells.reduce((best, cell) => Math.abs(Number(cell.textContent) - spot) < Math.abs(Number(best.textContent) - spot) ? cell : best);
    nearest.scrollIntoView({block: 'center', behavior: 'auto'});
}

function renderAnalysisTable(data) {
    const tbody = document.getElementById('analysisTableBody');
    tbody.innerHTML = '';
    const spotPrice = data.snapshot.spot_price;

    let strikes = {};
    let maxGexAbs = 0;
    let maxNetGexAbs = 0;

    data.profile.forEach(row => {
        const s = row.strike_price;
        if (!strikes[s]) strikes[s] = { strike: s, callGex: 0, putGex: 0, callOI: 0, putOI: 0 };
        if (optionSide(row) === 'call') {
            strikes[s].callGex += row.gex_value;
            strikes[s].callOI += row.open_interest || 0;
        } else {
            strikes[s].putGex += row.gex_value;
            strikes[s].putOI += row.open_interest || 0;
        }
    });

    let sortedStrikes = Object.values(strikes).sort((a, b) => a.strike - b.strike);

    sortedStrikes.forEach(row => {
        row.netGex = row.callGex + row.putGex;
        if (Math.abs(row.callGex) > maxGexAbs) maxGexAbs = Math.abs(row.callGex);
        if (Math.abs(row.putGex) > maxGexAbs) maxGexAbs = Math.abs(row.putGex);
        if (Math.abs(row.netGex) > maxNetGexAbs) maxNetGexAbs = Math.abs(row.netGex);
    });

    sortedStrikes.forEach(row => {
        if (row.callOI + row.putOI < 10 && Math.abs(row.netGex) < 100000) return;

        const tr = document.createElement('tr');
        let strikeClass = 'otm';

        if (Math.abs(row.strike - spotPrice) / spotPrice < 0.001) strikeClass = 'atm';
        else if (row.strike < spotPrice) strikeClass = 'itm';

        const isMagnet = Math.abs(row.netGex) === maxNetGexAbs;

        let badgeHtml = '';
        if (isMagnet) {
            badgeHtml = `<span class="badge badge-magnet">MAGNET</span>`;
        } else if (row.netGex > 0) {
            badgeHtml = `<span class="badge badge-stability">STABILITY</span>`;
        } else {
            badgeHtml = `<span class="badge badge-volatility">VOLATILITY</span>`;
        }

        const callWidth = (Math.abs(row.callGex) / maxGexAbs) * 100;
        const putWidth = (Math.abs(row.putGex) / maxGexAbs) * 100;
        const netValM = (row.netGex / 1000000).toFixed(2);

        tr.innerHTML = `
            <td class="strike-cell ${strikeClass}">${row.strike.toFixed(0)}</td>
            <td>
                ${badgeHtml}
                <div style="font-size:11px; color:var(--muted); margin-top:3px;">
                    ${row.netGex > 0 ? 'Dealer Long Gamma' : 'Dealer Short Gamma'}
                </div>
            </td>
            <td style="font-family: var(--font-mono); font-size:14px; font-variant-numeric: tabular-nums slashed-zero;">
                <span class="${row.netGex > 0 ? 'val-positive' : 'val-negative'}">$${netValM}M</span>
            </td>
            <td>
                <div class="bar-container">
                    <span style="font-size:12px; color:#9aa6b2; font-variant-numeric: tabular-nums slashed-zero;">${(row.callGex / 1000000).toFixed(2)}</span>
                    <div class="bg-bar bar-call" style="width: ${callWidth}px; max-width:80px;"></div>
                </div>
            </td>
            <td>
                <div class="bar-container">
                    <div class="bg-bar bar-put" style="width: ${putWidth}px; max-width:80px;"></div>
                    <span style="font-size:12px; color:#9aa6b2; font-variant-numeric: tabular-nums slashed-zero;">${(Math.abs(row.putGex) / 1000000).toFixed(2)}</span>
                </div>
            </td>
            <td style="color:#888;">${(row.callOI + row.putOI).toLocaleString()}</td>
        `;
        tbody.appendChild(tr);
    });

    // Keep the reader’s position stable during background refreshes.
}

function startTimers() {
    if (refreshTimer) clearInterval(refreshTimer);
    if (countdownTimer) clearInterval(countdownTimer);
    timeLeft = currentSettings.refresh_interval;

    countdownTimer = setInterval(() => {
        if (document.hidden) return;
        timeLeft = Math.max(0, timeLeft - 1);
        const timerEl = document.getElementById('timerCountdown');
        if (timerEl) timerEl.innerText = `${timeLeft}s`;
        if (timeLeft <= 0) timeLeft = currentSettings.refresh_interval;
    }, 1000);

    refreshTimer = setInterval(async () => {
        if (document.hidden || refreshInFlight) return;
        if (!initialized) { init(); return; }
        if (['settings', 'one-off'].includes(activeView)) return;
        refreshInFlight = true;
        try {
            if (!cachedData) await discoverSymbols();
            await loadSymbol();
        } catch (error) {
            setWorkspaceState('error', error.message || String(error));
        } finally { refreshInFlight = false; }
    }, Math.max(5, Number(currentSettings.refresh_interval) || 10) * 1000);
}

async function manualRefresh() {
    const button = document.getElementById('refreshButton');
    if (button?.disabled) return;
    if (window.eel?._websocket?.readyState === 3) { window.location.reload(); return; }
    if (button) button.disabled = true;
    try {
        if (!initialized) return await init();
        await discoverSymbols();
        if (await loadSymbol()) {
            await renderActiveView();
            showToast('Data refreshed', 'Latest saved snapshot loaded.', 'info');
        }
        await updateBackendStatus();
    } catch (error) { setWorkspaceState('error', error.message || String(error)); }
    finally { if (button) button.disabled = false; }
}

function oneOffSetText(id, value) {
    const el = document.getElementById(id);
    if (el) el.innerText = value;
}

function setOneOffInputs(symbol, expirationDate) {
    const symbolInput = document.getElementById('oneOffSymbol');
    const dateInput = document.getElementById('oneOffDate');
    if (symbolInput && symbol) symbolInput.value = String(symbol).toUpperCase();
    if (dateInput && expirationDate) dateInput.value = String(expirationDate);
}

function renderOneOffProfile(data, dbPath = '') {
    if (!data || data.error || !data.snapshot) {
        showToast('One-Off Profile', data?.error || 'No one-off profile data.', 'error');
        return;
    }

    cachedOneOffData = data;
    const snap = data.snapshot;
    const rows = buildStrikeProfile(data.profile || []);
    oneOffSetText('oneOffProfileLabel', snap.symbol || '--');
    oneOffSetText('oneOffSpot', snap.spot_price ? `$${Number(snap.spot_price).toFixed(2)}` : '--');
    oneOffSetText('oneOffExpiration', snap.expiration_date || '--');
    oneOffSetText('oneOffContracts', (data.profile || []).length.toLocaleString());
    oneOffSetText('oneOffNetGex', formatMoneyM(snap.total_net_gex || 0, 1));
    oneOffSetText('oneOffSnapshotTime', snap.timestamp ? new Date(snap.timestamp).toLocaleString() : '--');
    oneOffSetText('oneOffStatus', rows.length ? `${rows.length} strikes` : 'No strikes');
    if (dbPath) oneOffSetText('oneOffDbPath', dbPath.split(/[\\/]/).pop());

    renderProfileChartTo('oneOffProfileChart', data.profile || [], snap.spot_price || 0);
    renderSweepChart('oneOffSweepChart', data.gamma_sweep, snap);

    const tbody = document.getElementById('oneOffTableBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    rows.forEach(row => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td class="strike-cell">${formatAxisPrice(row.strike)}</td>
            <td class="${row.netGex >= 0 ? 'val-positive' : 'val-negative'}">${formatMoneyM(row.netGex, 2)}</td>
            <td>${formatMoneyM(row.callGex, 2)}</td>
            <td>${formatMoneyM(row.putGex, 2)}</td>
            <td>${row.oi.toLocaleString()}</td>
        `;
        tbody.appendChild(tr);
    });
}

async function loadOneOffProfiles() {
    const list = document.getElementById('oneOffRecentList');
    if (!list) return;

    try {
        const profiles = await apiCall('get_one_off_profiles');
        oneOffSetText('oneOffRecentCount', String(profiles.length || 0));
        list.innerHTML = '';
        if (!profiles.length) {
            list.innerHTML = '<div class="one-off-empty">No saved pulls</div>';
            return;
        }

        profiles.forEach(profile => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'one-off-recent-item';
            btn.onclick = () => openOneOffProfile(profile);
            btn.innerHTML = `
                <span>${profile.symbol || '--'} ${profile.expiration_date || '--'}</span>
                <strong>${formatMoneyM(profile.total_net_gex || 0, 1)}</strong>
                <em>${profile.contract_count || 0} contracts</em>
            `;
            list.appendChild(btn);
        });
    } catch (e) {
        console.error('Failed to load one-off profiles', e);
        list.textContent = 'Could not load saved profiles. Use Refresh to retry.';
    }
}

async function openOneOffProfile(profile) {
    const snapshotId = typeof profile === 'object' ? profile.snapshot_id : profile;
    if (!snapshotId) return;
    if (typeof profile === 'object') {
        setOneOffInputs(profile.symbol, profile.expiration_date);
    }
    try {
    const data = await apiCall('get_one_off_profile', snapshotId);
    if (data?.snapshot) {
        setOneOffInputs(data.snapshot.symbol, data.snapshot.expiration_date);
    }
    renderOneOffProfile(data);
    } catch (error) { showToast('Profile unavailable', error.message || String(error), 'error'); }
}

async function buildOneOffProfile() {
    const symbolInput = document.getElementById('oneOffSymbol');
    const dateInput = document.getElementById('oneOffDate');
    const btn = document.getElementById('oneOffBuildBtn');
    const symbol = symbolInput?.value?.trim().toUpperCase();
    const expirationDate = dateInput?.value?.trim();

    if (!symbol || !expirationDate) {
        showToast('One-Off Profile', 'Enter a symbol and date.', 'error');
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.innerText = 'Building...';
    }
    oneOffSetText('oneOffStatus', 'Pulling data');

    try {
        const result = await apiCall('build_one_off_profile', symbol, expirationDate);
        if (!result || !result.ok) {
            oneOffSetText('oneOffStatus', 'Failed');
            showToast('One-Off Failed', result?.message || 'Profile build failed.', 'error');
            return;
        }

        renderOneOffProfile(result.data, result.db_path || '');
        setOneOffInputs(result.symbol, result.expiration_date);
        await loadOneOffProfiles();
        showToast('One-Off Saved', result.message || `${symbol} profile saved.`, 'info');
    } catch (e) {
        console.error('One-off profile failed', e);
        oneOffSetText('oneOffStatus', 'Error');
        showToast('One-Off Error', String(e), 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerText = 'Build Profile';
        }
    }
}

function toggleFullscreen() {
    if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen?.();
    } else {
        document.exitFullscreen?.();
    }
}

function toggleActivityFeed() {
    const panel = document.querySelector('.right-panel');
    if (panel) panel.classList.toggle('collapsed');
    document.querySelector('.terminal-shell')?.classList.toggle('feed-collapsed');
    const open = !panel?.classList.contains('collapsed');
    document.getElementById('activityToggle')?.setAttribute('aria-expanded', String(open));
    resizeCharts();
}

async function saveSettings() {
    const saveButton = document.getElementById('saveSettingsButton');
    if (saveButton?.disabled) return;
    if (saveButton) saveButton.disabled = true;
    try {
    const newInterval = parseInt(document.getElementById('settingInterval').value, 10);
    const newTheme = document.getElementById('settingTheme').value;
    const newSymbols = document.getElementById('settingSymbols').value.split(',').map(s => s.trim()).filter(Boolean);
    const newRateLimit = parseFloat(document.getElementById('settingRateLimit').value);
    const newRateUtilization = parseFloat(document.getElementById('settingRateUtilization').value);
    const newMinPoll = parseInt(document.getElementById('settingMinPoll').value, 10);
    const newMaxPoll = parseInt(document.getElementById('settingMaxPoll').value, 10);
    const retentionEl = document.getElementById('settingRetentionDays');
    const newRetentionDays = retentionEl ? parseInt(retentionEl.value, 10) : 30;

    if (
        !Number.isFinite(newInterval) ||
        !Number.isFinite(newRateLimit) ||
        !Number.isFinite(newRateUtilization) ||
        !Number.isFinite(newMinPoll) ||
        !Number.isFinite(newMaxPoll) ||
        !Number.isFinite(newRetentionDays) ||
        newSymbols.length === 0
    ) {
        showToast("Settings Not Saved", "Enter valid numbers and at least one symbol.", "error");
        return;
    }

    const result = await apiCall('save_settings', {
        refresh_interval: newInterval,
        theme: newTheme,
        symbols: newSymbols,
        api_rate_limit_per_second: newRateLimit,
        api_rate_limit_utilization: newRateUtilization,
        min_poll_interval_seconds: newMinPoll,
        max_poll_interval_seconds: newMaxPoll,
        raw_retention_days: newRetentionDays
    });

    if (!result || !result.ok) {
        showToast("Settings Not Saved", result?.message || "Configuration was rejected.", "error");
        return;
    }

    currentSettings = result.settings || {
        ...currentSettings,
        refresh_interval: newInterval,
        theme: newTheme,
        symbols: newSymbols,
        api_rate_limit_per_second: newRateLimit,
        api_rate_limit_utilization: newRateUtilization,
        min_poll_interval_seconds: newMinPoll,
        max_poll_interval_seconds: newMaxPoll,
        raw_retention_days: newRetentionDays
    };
    document.getElementById('settingInterval').value = currentSettings.refresh_interval;
    document.getElementById('settingRateLimit').value = currentSettings.api_rate_limit_per_second;
    document.getElementById('settingRateUtilization').value = currentSettings.api_rate_limit_utilization;
    document.getElementById('settingMinPoll').value = currentSettings.min_poll_interval_seconds;
    document.getElementById('settingMaxPoll').value = currentSettings.max_poll_interval_seconds;
    if (retentionEl) retentionEl.value = currentSettings.raw_retention_days;
    document.getElementById('settingSymbols').value = (currentSettings.symbols || []).join(',');
    showToast("Settings Saved", result.message || "Configuration updated.", "info");
    applyTheme(currentSettings.theme);
    await discoverSymbols();
    startTimers();
    } catch (error) {
        showToast('Settings not saved', error.message || String(error), 'error');
    } finally { if (saveButton) saveButton.disabled = false; }
}

// --- Overview / Signal Dashboard ---

async function loadOverview(requestContext = null) {
    const selectedSymbol = document.getElementById('symbolSelector').value;
    const data = await apiCall('get_market_overview');
    if (!isSymbolContextCurrent(selectedSymbol, requestContext)) return;
    if (data.error) { console.error(data.error); return; }
    let symbolData = cachedData;
    if (selectedSymbol && (!symbolData || cachedSymbol !== selectedSymbol)) {
        symbolData = await apiCall('get_dashboard_data', selectedSymbol);
        if (!isSymbolContextCurrent(selectedSymbol, requestContext)) return;
    }
    cachedOverview = data;
    if (symbolData && !symbolData.error) {
        cachedData = symbolData;
        cachedSymbol = selectedSymbol;
    }
    renderSignalDashboard(data);
    renderActionOverview(data, symbolData);
    const trafficLight = document.getElementById('trafficLight');
    if (trafficLight) trafficLight.dataset.loaded = "true";
}

function renderSignalDashboard(data) {
    renderCompass(data.compass_traders, 'Traders');
    renderCompass(data.index_basket || data.compass_whale, 'Whale');

    renderPillars(data.components);
    renderTiltChart(data.tilt);
}

function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

function formatPct(value, decimals = 0) {
    const n = Number(value || 0);
    return `${(n * 100).toFixed(decimals)}%`;
}

function formatAge(seconds) {
    if (seconds === null || seconds === undefined) return 'age n/a';
    if (seconds < 60) return `${Math.round(seconds)}s old`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m old`;
    return `${(seconds / 3600).toFixed(1)}h old`;
}

function optionSide(row) {
    const type = String(row.option_type || '').toUpperCase();
    if (type.includes('CALL')) return 'call';
    if (type.includes('PUT')) return 'put';
    return Number(row.gex_value || 0) < 0 ? 'put' : 'call';
}

function voteLabel(score) {
    if (score > 0.20) return 'CALL';
    if (score < -0.20) return 'PUT';
    return 'WAIT';
}

function voteClass(score) {
    if (score > 0.20) return 'vote-call';
    if (score < -0.20) return 'vote-put';
    return 'vote-wait';
}

function setVote(id, detailId, vote) {
    const el = document.getElementById(id);
    const detailEl = document.getElementById(detailId);
    if (!el || !detailEl) return;
    el.className = voteClass(vote.score);
    el.innerText = vote.label || voteLabel(vote.score);
    detailEl.innerText = vote.detail;
}

function buildStrikeProfile(profileData) {
    const map = {};
    (profileData || []).forEach(row => {
        const strike = Number(row.strike_price);
        if (!map[strike]) map[strike] = { strike, callGex: 0, putGex: 0, netGex: 0, oi: 0 };
        if (optionSide(row) === 'call') map[strike].callGex += row.gex_value;
        else map[strike].putGex += row.gex_value;
        map[strike].netGex += row.gex_value;
        map[strike].oi += row.open_interest || 0;
    });
    return Object.values(map).sort((a, b) => a.strike - b.strike);
}

function estimateFlipFromProfileRows(rows) {
    if (!rows.length) return { strike: 0, quality: 'missing' };

    let running = 0;
    let previous = 0;
    let previousStrike = rows[0].strike;

    for (let i = 0; i < rows.length; i++) {
        running += rows[i].netGex;
        if (i === 0) {
            previous = running;
            previousStrike = rows[i].strike;
            continue;
        }
        if ((previous < 0 && running >= 0) || (previous > 0 && running <= 0)) {
            const span = running - previous;
            const ratio = span === 0 ? 0 : Math.abs(previous) / Math.abs(span);
            return { strike: previousStrike + ((rows[i].strike - previousStrike) * ratio), quality: 'crossing' };
        }
        previous = running;
        previousStrike = rows[i].strike;
    }

    const total = rows.reduce((sum, row) => sum + row.netGex, 0);
    const oppositeRows = rows.filter(row => row.netGex !== 0 && Math.sign(row.netGex) !== Math.sign(total));
    if (oppositeRows.length) {
        const strongest = oppositeRows.reduce((best, row) => Math.abs(row.netGex) > Math.abs(best.netGex) ? row : best, oppositeRows[0]);
        return { strike: strongest.strike, quality: 'proxy' };
    }

    return { strike: rows[Math.floor(rows.length / 2)].strike, quality: 'edge' };
}

function findComponent(overviewData, symbol) {
    return (overviewData.components || []).find(c => c.symbol === symbol) || null;
}

function buildMarketVote(overviewData) {
    const traders = overviewData.compass_traders || {};
    const whale = overviewData.index_basket || overviewData.compass_whale || {};
    const traderQuality = traders.data_quality?.score ?? traders.confidence ?? 0;
    const basketQuality = whale.data_quality?.score ?? whale.confidence ?? 0;
    const totalQuality = traderQuality + basketQuality || 1;
    const score = clamp(
        ((traders.y_score || 0) * traderQuality + (whale.y_score || 0) * basketQuality) / totalQuality,
        -1,
        1
    );
    const detail = `Traders ${voteLabel(traders.y_score || 0)} / Index Basket ${voteLabel(whale.y_score || 0)} | data quality ${formatPct((traderQuality + basketQuality) / 2)}`;
    return { score, detail };
}

function buildDealerVote(symbolData, component) {
    if (!symbolData || symbolData.error) {
        return { score: 0, detail: 'No selected-symbol dealer data' };
    }

    const rows = buildStrikeProfile(symbolData.profile);
    const spot = symbolData.snapshot.spot_price;
    const localRows = rows.filter(row => Math.abs(row.strike - spot) / spot <= 0.02);
    const localNet = localRows.reduce((sum, row) => sum + row.netGex, 0);
    const fallbackFlip = estimateFlipFromProfileRows(rows);
    const flip = component && component.flip_strike ? component.flip_strike : fallbackFlip.strike;
    const flipQuality = component ? component.flip_quality : fallbackFlip.quality;
    const direction = flip > 0 ? clamp((spot - flip) / (flip * 0.006), -1, 1) : 0;
    const gammaMultiplier = localNet < 0 ? 1.0 : 0.45;
    const score = clamp(direction * gammaMultiplier, -1, 1);
    const dealerState = localNet < 0 ? 'short gamma momentum' : 'long gamma compression';
    const detail = `${dealerState}; spot ${flip > 0 ? (spot >= flip ? 'above' : 'below') : 'near'} flip (${flipQuality || 'unknown'})`;
    return { score, detail };
}

function nearestSignificant(rows, spot, direction, sign) {
    const sideValue = row => sign > 0 ? Number(row.callGex || 0) : Number(row.putGex || 0);
    const maxAbs = rows.reduce((max, row) => Math.max(max, Math.abs(sideValue(row))), 0);
    const threshold = maxAbs * 0.20;
    const candidates = rows
        .filter(row => direction === 'above' ? row.strike > spot : row.strike < spot)
        .filter(row => Math.abs(sideValue(row)) >= threshold && Math.sign(sideValue(row)) === sign)
        .sort((a, b) => Math.abs(a.strike - spot) - Math.abs(b.strike - spot));
    return candidates[0] || null;
}

function roomScore(level, spot) {
    if (!level) return 0.35;
    const distPct = Math.abs(level.strike - spot) / spot;
    if (distPct < 0.0035) return -0.45;
    if (distPct < 0.0075) return -0.15;
    if (distPct > 0.015) return 0.35;
    return 0.10;
}

function buildLiquidityVote(symbolData) {
    if (!symbolData || symbolData.error) {
        return { score: 0, detail: 'No liquidity profile' };
    }

    const rows = buildStrikeProfile(symbolData.profile);
    const spot = symbolData.snapshot.spot_price;
    const upsideWall = nearestSignificant(rows, spot, 'above', 1);
    const downsideWall = nearestSignificant(rows, spot, 'below', 1);
    const upsideAccel = nearestSignificant(rows, spot, 'above', -1);
    const downsideAccel = nearestSignificant(rows, spot, 'below', -1);
    const callRoom = roomScore(upsideWall, spot) + (upsideAccel ? 0.15 : 0);
    const putRoom = roomScore(downsideWall, spot) + (downsideAccel ? 0.15 : 0);
    const score = clamp(callRoom - putRoom, -1, 1);
    const aboveText = upsideWall ? `upside wall ${upsideWall.strike.toFixed(0)}` : 'upside open';
    const belowText = downsideWall ? `downside wall ${downsideWall.strike.toFixed(0)}` : 'downside open';
    return { score, detail: `${aboveText}; ${belowText}` };
}

function formatTargetPrice(value) {
    if (!Number.isFinite(value) || value <= 0) return '---';
    return value >= 1000 ? value.toFixed(0) : value.toFixed(2);
}

function formatNumber(value, decimals = 0) {
    const n = Number(value || 0);
    return n.toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
    });
}

function formatMoneyM(value, decimals = 0) {
    const n = Number(value || 0);
    const sign = n < 0 ? '-' : '';
    return `${sign}$${formatNumber(Math.abs(n) / 1000000, decimals)}M`;
}

function parseLevel(value) {
    const n = Number.parseFloat(value);
    return Number.isFinite(n) ? n : null;
}

function classifyVote(score) {
    if (score > 0.20) return { label: 'CALL', className: 'green-text', badge: 'Supportive' };
    if (score < -0.20) return { label: 'PUT', className: 'red-text', badge: 'Active' };
    return { label: 'WAIT', className: 'amber-text', badge: 'Neutral' };
}

function selectedComponent(overviewData, symbolData) {
    const selectedSymbol = symbolData?.snapshot?.symbol || document.getElementById('symbolSelector')?.value;
    return findComponent(overviewData, selectedSymbol) || null;
}

async function loadCockpit(requestContext = null, renderCharts = true) {
    const symbol = document.getElementById('symbolSelector')?.value;
    const symbolData = cachedData;
    if (!symbol || !symbolData || symbolData.error || symbolData.snapshot?.symbol !== symbol) return;
    const workspace = cachedWorkspace;
    if (!workspace || workspace.symbol !== symbol || !isSymbolContextCurrent(symbol, requestContext)) return;
    const overview = {
        components: workspace.components || [],
        edge_stats: { [symbol]: workspace.historical_edge },
        index_basket: workspace.regime?.index_basket,
    };
    cockpitModel = cockpitModelFromWorkspace(workspace);
    renderCockpit(cockpitModel, symbolData, overview, renderCharts);
    renderMarketContext(workspace.market_context);
    renderExecutionCandidates(workspace.execution_candidates, cockpitModel, symbolData);
}

function cockpitModelFromWorkspace(workspace) {
    const decision = workspace?.decision || {};
    const scenario = workspace?.active_scenario || {};
    const target = decision.target;
    const invalidation = decision.invalidation;
    const bias = decision.bias || 'WAIT';
    return {
        symbol: workspace.symbol,
        marketVote: decision.market_vote || { score: 0, detail: 'Unavailable' },
        dealerVote: decision.dealer_vote || { score: 0, detail: 'Unavailable' },
        liquidityVote: decision.liquidity_vote || { score: 0, detail: 'Candidate-specific' },
        whale: decision.index_basket || {},
        score: Number(decision.score || 0),
        dataQuality: decision.data_quality || workspace.data_quality || { score: 0, warnings: [] },
        title: bias === 'CALL' ? 'CALL BIAS' : bias === 'PUT' ? 'PUT BIAS' : 'WAIT',
        context: decision.context || (scenario.reasons || []).join('; '),
        plan: {
            target: hasNumber(target) ? formatTargetPrice(target) : '--',
            invalidation: hasNumber(invalidation) ? formatTargetPrice(invalidation) : '--',
            description: scenario.scenario_type || 'NO_TRADE',
        },
        flip: decision.flip,
        target,
        invalidation,
        scenarioId: scenario.scenario_id,
        scenarioType: scenario.scenario_type,
    };
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.innerText = value;
}

function setClassText(id, value, className) {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerText = value;
    el.className = className || '';
}

function renderMarketContext(context) {
    const eventState = context?.event_risk?.state || 'UNAVAILABLE';
    setClassText('contextEventRisk', eventState, eventState === 'BLOCKED' ? 'red-text' : eventState === 'CAUTION' ? 'amber-text' : eventState === 'NORMAL' ? 'green-text' : 'muted');
    setText('contextImpliedMove', hasNumber(context?.implied_move) ? formatTargetPrice(context.implied_move) : 'Unavailable');
    setText('contextRangeConsumed', hasNumber(context?.range_consumed) ? `${Math.round(Number(context.range_consumed) * 100)}%` : 'Unavailable');
    setText('contextRealizedVol', hasNumber(context?.realized_volatility_15m) ? formatPct(context.realized_volatility_15m, 2) : 'Unavailable');
    setText('contextCrossAsset', context?.cross_asset_state || 'Unavailable');
    setText('contextWarnings', (context?.warnings || []).join(' | ') || 'All configured context sources available');
}

function formatEdgeWinRate(value) {
    if (value === null || value === undefined || value === '') return '--';
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    return `${Math.round(n * 100)}%`;
}

function formatEdgeMove(value) {
    if (value === null || value === undefined || value === '') return '--';
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    const sign = n > 0 ? '+' : '';
    return `${sign}${n.toFixed(Math.abs(n) >= 10 ? 0 : 1)} pts`;
}

function renderEdgeStats(edgeStats) {
    const grid = document.getElementById('edgeStatsGrid');
    const source = document.getElementById('edgeStatsSource');
    if (!grid) return;

    const horizons = edgeStats?.horizons || [];
    if (!horizons.length) {
        if (source) source.innerText = 'No empirical bucket for this symbol';
        grid.innerHTML = [15, 30, 60].map(horizon => `
            <article class="edge-stat">
                <span>${horizon}m</span>
                <strong class="amber-text">--</strong>
                <em>n=0 | med --</em>
            </article>
        `).join('');
        return;
    }

    const primary = edgeStats.primary || horizons.find(item => item.horizon_minutes === 30) || horizons[0];
    if (source) {
        const sampleLabel = primary.sample_label || 'historical';
        source.innerText = `${edgeStats.symbol || ''} ${sampleLabel} outcomes`;
    }

    grid.innerHTML = horizons.map(item => {
        const sample = Number(item.sample_size || 0);
        const winRate = formatEdgeWinRate(item.win_rate);
        const moveText = formatEdgeMove(item.median_move_points);
        const favorable = formatEdgeMove(item.median_favorable_points);
        const adverse = formatEdgeMove(item.median_adverse_points);
        const numericWinRate = Number(item.win_rate);
        const cls = !Number.isFinite(numericWinRate) ? 'amber-text' : numericWinRate >= 0.55 ? 'green-text' : numericWinRate <= 0.45 ? 'red-text' : 'amber-text';
        const labelClass = sample > 0 ? cls : 'amber-text';
        const detail = sample > 0
            ? `n=${sample} | med ${moveText} | MFE ${favorable} | MAE ${adverse}`
            : 'n=0 | med --';

        return `
            <article class="edge-stat">
                <span>${item.horizon_minutes}m</span>
                <strong class="${labelClass}">${winRate}</strong>
                <em>${detail}</em>
            </article>
        `;
    }).join('');
}

function renderCockpit(model, symbolData, overviewData, renderCharts = true) {
    if (!model) return;

    const scoreClass = model.score > 0.22 ? 'green' : model.score < -0.22 ? 'red' : 'amber';
    setText('cockpitSymbol', model.symbol);
    setText('cockpitContext', model.context);
    setClassText('cockpitBiasLabel', model.title, scoreClass);
    setClassText('cockpitBiasScore', `${Math.round(model.score * 100)}%`, scoreClass);
    setText('cockpitConfidence', `${Math.round((model.dataQuality?.score || 0) * 100)}%`);
    setText('cockpitTarget', model.plan.target);
    setText('cockpitInvalidation', model.plan.invalidation);
    setText('cockpitFlip', model.flip ? formatTargetPrice(model.flip) : '--');

    const market = classifyVote(model.marketVote.score);
    setClassText('tileMarketValue', market.label, market.className);
    setText('tileMarketBadge', market.badge);
    setText('tileMarketDetail', model.marketVote.detail);

    const dealer = classifyVote(model.dealerVote.score);
    setClassText('tileDealerValue', model.dealerVote.score < -0.2 ? 'Short Gamma' : model.dealerVote.score > 0.2 ? 'Long Gamma' : 'Mixed Gamma', dealer.className);
    setText('tileDealerBadge', Math.abs(model.dealerVote.score) > 0.55 ? 'High Risk' : dealer.badge);
    setText('tileDealerDetail', model.dealerVote.detail);

    const liquidity = classifyVote(model.liquidityVote.score);
    setClassText('tileLiquidityValue', model.liquidityVote.score < -0.2 ? 'Downside Open' : model.liquidityVote.score > 0.2 ? 'Upside Open' : 'Balanced', liquidity.className);
    setText('tileLiquidityBadge', Math.abs(model.liquidityVote.score) > 0.45 ? 'Fragile' : liquidity.badge);
    setText('tileLiquidityDetail', model.liquidityVote.detail);

    const whaleScore = model.whale.y_score || 0;
    const whaleVote = classifyVote(whaleScore);
    setClassText('tileWhaleValue', model.whale.label || whaleVote.label, whaleVote.className);
    setText('tileWhaleBadge', model.whale.confidence_label || whaleVote.badge);
    setText('tileWhaleDetail', model.whale.strategy || 'No index-basket composite.');

    if (renderCharts) {
        renderCockpitProfileChart(symbolData.profile, symbolData.snapshot.spot_price, model, symbolData.gamma_sweep);
        if (gammaSweepOverlayEnabled) renderSweepChart('cockpitSweepChart', symbolData.gamma_sweep, symbolData.snapshot);
    }
    renderMetricStrip(symbolData, model);
    renderEdgeStats(overviewData.edge_stats?.[model.symbol]);
    renderCockpitPillars(overviewData.components || []);
}

async function loadExecutionCandidates(requestContext = null) {
    const symbol = document.getElementById('symbolSelector')?.value;
    if (!symbol) return;

    if (!isSymbolContextCurrent(symbol, requestContext)) return;
    const data = cachedWorkspace?.execution_candidates;
    cachedTradeSetups = data;
    renderExecutionCandidates(data, cockpitModel, cachedData);
}

async function loadEdgeLab(requestContext = null) {
    const generation = ++edgeGeneration;
    const symbol = document.getElementById('symbolSelector')?.value;
    if (!symbol || !isSymbolContextCurrent(symbol, requestContext)) return;
    const filters = {
        symbol,
        horizon: Number(document.getElementById('edgeLabHorizon')?.value || 30),
        include_legacy: Boolean(document.getElementById('edgeLabLegacy')?.checked),
    };
    setText('edgeLabStatus', 'Loading historical evidence…');
    try {
    const payload = await apiCall('get_edge_lab', filters);
    if (generation !== edgeGeneration || !isSymbolContextCurrent(symbol, requestContext)) return;
    if (payload?.error) throw new Error(payload.error);
    EdgeLab.render(payload, {
        status: document.getElementById('edgeLabStatus'),
        summary: document.getElementById('edgeLabSummary'),
        table: document.getElementById('edgeLabTable'),
        chart: document.getElementById('edgeLabExpectancyChart'),
    }, window.echarts);
    await loadJournal(symbol, generation);
    } catch (error) {
        if (generation !== edgeGeneration) return;
        setText('edgeLabStatus', `Unable to load evidence. ${error.message || error}`);
    }
}

function prefillJournalFromCockpit() {
    const workspace = cachedWorkspace;
    if (!workspace) return;
    switchView('edge-lab');
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString();
    const session = workspace.as_of ? String(workspace.as_of).slice(0, 10) : local.slice(0, 10);
    const dateInput = document.getElementById('journalSessionDate');
    const timeInput = document.getElementById('journalEntryTime');
    if (dateInput) dateInput.value = session;
    if (timeInput) timeInput.value = local.slice(0, 16);
}

async function saveJournalEntry() {
    const button = document.getElementById('saveJournalButton');
    if (button?.disabled) return;
    if (button) button.disabled = true;
    const workspace = cachedWorkspace || {};
    const payload = {
        symbol: document.getElementById('symbolSelector')?.value,
        session_date: document.getElementById('journalSessionDate')?.value,
        scenario_id: workspace.active_scenario?.scenario_id,
        scenario_type: workspace.active_scenario?.scenario_type,
        regime: workspace.regime?.traders?.label,
        liquidity_grade: Object.values(workspace.execution_candidates?.ideas || {}).find(item => item.status === 'EXECUTABLE')?.liquidity_grade,
        contracts: Number(document.getElementById('journalContracts')?.value),
        entry_price: Number(document.getElementById('journalEntryPrice')?.value),
        exit_price: document.getElementById('journalExitPrice')?.value || null,
        entry_time: document.getElementById('journalEntryTime')?.value,
        fees: Number(document.getElementById('journalFees')?.value || 0),
        adhered_to_plan: Boolean(document.getElementById('journalAdhered')?.checked),
        notes: document.getElementById('journalNotes')?.value || '',
    };
    try {
        if (!payload.session_date || !payload.entry_time || !hasNumber(document.getElementById('journalEntryPrice')?.value)) throw new Error('Enter a session date, entry time, and entry price.');
        await apiCall('create_journal_entry', payload);
        await loadJournal(payload.symbol);
        showToast('Journal saved', 'Execution result saved locally.', 'info');
        document.getElementById('journalEntryPrice').value = '';
        document.getElementById('journalExitPrice').value = '';
        document.getElementById('journalNotes').value = '';
    } catch (error) {
        showToast('Journal validation', String(error), 'info');
    } finally { if (button) button.disabled = false; }
}

async function loadJournal(symbol, generation = edgeGeneration) {
    const [entries, review] = await Promise.all([
        apiCall('get_journal_entries', symbol),
        apiCall('get_weekly_journal_review', null),
    ]);
    if (generation !== edgeGeneration || !isSymbolContextCurrent(symbol)) return;
    const results = document.getElementById('journalExecutionResults');
    const weekly = document.getElementById('journalWeeklyReview');
    if (results) results.innerHTML = entries.length ? `<table class="analysis-table"><thead><tr><th>Entry</th><th>Scenario</th><th>Contracts</th><th>P&amp;L</th><th>Adherence</th></tr></thead><tbody>${entries.map(row => `<tr><td>${String(row.entry_time).replace('T', ' ')}</td><td>${row.scenario_type || '--'}</td><td>${row.contracts}</td><td>${row.pnl == null ? 'Open' : formatTradeDollars(row.pnl)}</td><td>${row.adhered_to_plan ? 'Followed' : 'Deviated'}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">No user execution entries for this symbol.</p>';
    if (weekly) weekly.innerHTML = review.groups.length ? review.groups.map(group => `<article class="edge-stat"><span>${group.dimension}: ${group.value}</span><strong>${formatTradeDollars(group.pnl)}</strong><em>${group.trades} user trades</em></article>`).join('') : '<article class="edge-stat"><span>Weekly review</span><strong>--</strong><em>No user trades this week</em></article>';
}

function formatTradePoints(value, decimals = 2) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    return `${n.toFixed(decimals)} pts`;
}

function formatTradeDollars(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    const sign = n < 0 ? '-' : '';
    return `${sign}$${formatNumber(Math.abs(n), 0)}`;
}

function setupSideClass(side) {
    return side === 'CALL' ? 'green-text' : side === 'PUT' ? 'red-text' : 'amber-text';
}

function renderSetupStat(label, value, className = '') {
    return `
        <div>
            <span>${label}</span>
            <strong class="${className}">${value}</strong>
        </div>
    `;
}

function unavailableSetupCard(title, idea) {
    const status = idea?.status || 'UNAVAILABLE';
    const modeled = status === 'MODELED_ONLY';
    return `
        <div class="setup-card-head">
            <span>${title}</span>
            <em>${status}</em>
        </div>
        <strong class="amber-text">${modeled ? `Theoretical debit ${formatTradePoints(idea?.estimated_debit)}` : 'Not executable'}</strong>
        <p>${idea?.reason || 'No eligible quoted structure in the current snapshot.'}</p>
    `;
}

function renderExecutionQuote(idea) {
    const quote = idea?.quote;
    if (!quote) return '';
    const age = hasNumber(quote.age_seconds) ? `${Math.round(Number(quote.age_seconds))}s old` : 'age unavailable';
    const sizing = idea.status === 'EXECUTABLE' ? renderSetupStat('Contracts', String(idea.contracts ?? 0), 'green-text') : '';
    return `
        <div class="setup-stat-grid">
            ${renderSetupStat('Bid / Mid / Ask', `${formatTradePoints(quote.bid)} / ${formatTradePoints(quote.mid)} / ${formatTradePoints(quote.ask)}`)}
            ${renderSetupStat('Quote age', age)}
            ${renderSetupStat('Liquidity', idea.liquidity_grade || '--', idea.liquidity_grade === 'REJECTED' ? 'red-text' : 'green-text')}
            ${renderSetupStat('Max loss', formatTradeDollars(idea.max_loss_dollars), 'amber-text')}
            ${renderSetupStat('Max reward', formatTradeDollars(idea.max_reward_dollars), 'green-text')}
            ${renderSetupStat('Settlement', idea.settlement_type || '--')}
            ${renderSetupStat('Close', idea.time_to_close || '--')}
            ${sizing}
        </div>
    `;
}

function renderButterflyCard(idea) {
    const card = document.getElementById('butterflyCandidateCard');
    if (!card) return;
    if (!idea || idea.status === 'MODELED_ONLY' || idea.status === 'REJECTED') {
        card.innerHTML = unavailableSetupCard('Butterfly', idea);
        return;
    }

    const sideClass = setupSideClass(idea.side);
    card.innerHTML = `
        <div class="setup-card-head">
            <span>Butterfly</span>
            <em>${idea.status} / ${idea.liquidity_grade}</em>
        </div>
        <strong class="${sideClass}">${idea.side} ${formatTargetPrice(idea.lower)} / ${formatTargetPrice(idea.center)} / ${formatTargetPrice(idea.upper)}</strong>
        <div class="setup-stat-grid">
            ${renderSetupStat('Debit', formatTradePoints(idea.estimated_debit), sideClass)}
            ${renderSetupStat('Max Reward', formatTradePoints(idea.max_profit), 'green-text')}
            ${renderSetupStat('Risk', formatTradeDollars(idea.estimated_debit_dollars), 'amber-text')}
            ${renderSetupStat('Tent', `${formatTargetPrice(idea.lower_breakeven)} - ${formatTargetPrice(idea.upper_breakeven)}`)}
        </div>
        ${renderExecutionQuote(idea)}
        <p>${idea.rationale}</p>
    `;
}

function renderDebitSpreadCard(idea) {
    const card = document.getElementById('debitSpreadCandidateCard');
    if (!card) return;
    if (!idea || idea.status === 'MODELED_ONLY' || idea.status === 'REJECTED') {
        card.innerHTML = unavailableSetupCard('Debit Spread', idea);
        return;
    }

    const sideClass = setupSideClass(idea.side);
    card.innerHTML = `
        <div class="setup-card-head">
            <span>Debit Spread</span>
            <em>${idea.status} / ${idea.liquidity_grade}</em>
        </div>
        <strong class="${sideClass}">${idea.side} ${formatTargetPrice(idea.long_strike)} / ${formatTargetPrice(idea.short_strike)}</strong>
        <div class="setup-stat-grid">
            ${renderSetupStat('Debit', formatTradePoints(idea.estimated_debit), sideClass)}
            ${renderSetupStat('Max Reward', formatTradePoints(idea.max_profit), 'green-text')}
            ${renderSetupStat('Breakeven', formatTargetPrice(idea.breakeven))}
            ${renderSetupStat('Pit Target', formatTargetPrice(idea.target), 'amber-text')}
        </div>
        ${renderExecutionQuote(idea)}
        <p>${idea.rationale}</p>
    `;
}

function setupMarkerLevels(setups, model) {
    const ideas = setups?.ideas || {};
    const fly = ideas.butterfly || {};
    const spread = ideas.debit_spread || {};
    const levels = [
        { label: 'Spot', value: setups?.spot, color: '#ffffff', dash: 'solid' },
        { label: 'Cockpit Target', value: model?.target, color: '#ff454f', dash: 'dot' },
        { label: 'Cockpit Invalid', value: model?.invalidation, color: '#f5a524', dash: 'dash' },
    ];

    if (fly.status === 'ready') {
        levels.push(
            { label: 'Fly Body', value: fly.center, color: '#00d37f', dash: 'solid' },
            { label: 'Fly Wing', value: fly.lower, color: '#00d37f', dash: 'dash' },
            { label: 'Fly Wing', value: fly.upper, color: '#00d37f', dash: 'dash' },
        );
    }
    if (spread.status === 'ready') {
        levels.push(
            { label: 'Spread Target', value: spread.target, color: '#2f9bff', dash: 'solid' },
            { label: 'Pit Wall', value: spread.pit_left_wall_strike, color: '#b177ff', dash: 'dash' },
            { label: 'Pit Wall', value: spread.pit_right_wall_strike, color: '#b177ff', dash: 'dash' },
        );
    }

    return levels.filter(level => hasNumber(level.value)).map(level => ({
        ...level,
        value: Number(level.value)
    }));
}

function renderSetupProfileChart(setups, model) {
    const profile = setups?.profile || [];
    const strikes = profile.map(row => Number(row.strike)).filter(Number.isFinite);
    if (!strikes.length) return;

    const rows = profile.map(row => ({
        strike: Number(row.strike),
        net: Number(row.net_gex || 0) / 1000000,
        call: Number(row.call_gex || 0) / 1000000,
        put: Number(row.put_gex || 0) / 1000000,
    })).sort((a, b) => a.strike - b.strike);
    const markerLevels = setupMarkerLevels(setups, model);
    const markerLines = buildMarkerLines(markerLevels, rows.map(row => row.strike));
    const focusWindow = focusedStrikeWindow(rows.map(row => row.strike), markerLevels, setups.spot);
    const visibleRows = rowsInStrikeRange(rows, focusWindow.startValue, focusWindow.endValue);
    const bounds = chartBounds(visibleRows.map(row => row.net), 0.20);

    const option = {
        ...baseChartOptions({ valueFormatter: formatMillionsValue }),
        tooltip: {
            ...baseChartOptions({ valueFormatter: formatMillionsValue }).tooltip,
            formatter: profileTooltipFormatter(rows, formatMillionsValue)
        },
        grid: { top: 42, left: 58, right: 58, bottom: 46, containLabel: true },
        dataZoom: zoomDataOptions(focusWindow.startValue, focusWindow.endValue),
        xAxis: {
            type: 'value',
            name: 'Strike',
            nameLocation: 'middle',
            nameGap: 30,
            nameTextStyle: chartText(12),
            axisLabel: { ...chartText(12), formatter: formatAxisPrice },
            axisLine: { onZero: false, lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: CHART_GRID } }
        },
        yAxis: {
            type: 'value',
            name: 'Net GEX by Strike (M)',
            min: bounds.min,
            max: bounds.max,
            nameTextStyle: chartText(12, '#b177ff'),
            axisLabel: { ...chartText(12, '#b177ff'), formatter: formatMillionsAxis },
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: CHART_GRID } }
        },
        series: [{
            name: 'Net GEX by Strike',
            type: 'line',
            smooth: true,
            symbol: 'circle',
            symbolSize: 5,
            data: rows.map(row => [row.strike, row.net]),
            lineStyle: { color: '#b177ff', width: 3 },
            itemStyle: { color: '#b177ff' },
            areaStyle: { color: 'rgba(177,119,255,0.08)' },
            markLine: {
                symbol: 'none',
                silent: true,
                data: markerLines
            }
        }]
    };

    setChartOption('setupProfileChart', option);
    attachClickZoom('setupProfileChart', rows.map(row => row.strike));
}

function renderExecutionCandidates(setups, model, symbolData) {
    if (!setups || setups.error) {
        showToast('Setups unavailable', setups?.error || 'No setup data', 'info');
        return;
    }

    const executable = Object.values(setups.ideas || {}).filter(idea => idea.status === 'EXECUTABLE').length;
    setText('executionQuoteStatus', executable ? `${executable} quote-backed candidate${executable === 1 ? '' : 's'}; sizing uses conservative max loss` : 'No executable quote; modeled/rejected candidates remain visible');

    renderButterflyCard(setups.ideas?.butterfly);
    renderDebitSpreadCard(setups.ideas?.debit_spread);
}

function renderMetricStrip(symbolData, model) {
    const callTotal = symbolData.profile
        .filter(row => optionSide(row) === 'call')
        .reduce((sum, row) => sum + row.gex_value, 0);
    const putTotal = symbolData.profile
        .filter(row => optionSide(row) === 'put')
        .reduce((sum, row) => sum + row.gex_value, 0);
    const history = symbolData.history || [];
    const firstHist = history[0]?.total_net_gex || 0;
    const lastHist = history[history.length - 1]?.total_net_gex || symbolData.snapshot.total_net_gex || 0;
    const localNet = buildStrikeProfile(symbolData.profile)
        .filter(row => Math.abs(row.strike - symbolData.snapshot.spot_price) / symbolData.snapshot.spot_price <= 0.02)
        .reduce((sum, row) => sum + row.netGex, 0);

    setClassText('stripCallGex', formatMoneyM(callTotal, 0), 'amber-text');
    setClassText('stripPutGex', formatMoneyM(putTotal, 0), 'red-text');
    setClassText('stripNetGex', formatMoneyM(symbolData.snapshot.total_net_gex, 0), symbolData.snapshot.total_net_gex >= 0 ? 'green-text' : 'red-text');
    setClassText('stripGammaExposure', localNet >= 0 ? 'Long' : 'Short', localNet >= 0 ? 'green-text' : 'red-text');
    setClassText('stripGexChange', formatMoneyM(lastHist - firstHist, 0), lastHist - firstHist >= 0 ? 'green-text' : 'red-text');
    setText('stripZeroGamma', sweepZeroLabel(symbolData.gamma_sweep, model.flip));
    setClassText('stripGammaSlope', `${formatMoneyM(symbolData.snapshot.gex_slope || 0, 2)} / pt`, symbolData.snapshot.gex_slope >= 0 ? 'green-text' : 'red-text');
}

function renderCockpitPillars(components) {
    const container = document.getElementById('cockpitPillars');
    if (!container) return;
    container.innerHTML = '';

    components.forEach(comp => {
        const card = document.createElement('article');
        card.className = 'asset-card';
        const pct = Number(comp.distance_pct || 0);
        const isPos = pct >= 0;
        const score = comp.data_quality?.score ?? comp.confidence ?? 0;
        const quality = score >= 0.8 ? 'A' : score >= 0.65 ? 'B+' : score >= 0.45 ? 'B' : 'C';
        const accelLabel = Math.abs(comp.acceleration || 0) > 10000000 ? 'High' : Math.abs(comp.acceleration || 0) > 3000000 ? 'Rising' : 'Neutral';
        const width = Math.min(Math.abs(pct) / 3 * 50, 50);
        const barStyle = isPos
            ? `left:50%;width:${width}%;background:var(--green);`
            : `right:50%;width:${width}%;background:var(--red);`;

        card.innerHTML = `
            <div class="asset-card-header">
                <h3>${comp.symbol}</h3>
                <span class="watch-star">*</span>
            </div>
            <div class="asset-metrics">
                <div><span>Flip Dist</span><strong class="${isPos ? 'green-text' : 'red-text'}">${pct.toFixed(1)}%</strong></div>
                <div><span>Quality</span><strong class="${score >= 0.65 ? 'green-text' : 'amber-text'}">${quality}</strong></div>
                <div><span>Accel</span><strong class="${accelLabel === 'High' ? 'red-text' : accelLabel === 'Rising' ? 'amber-text' : ''}">${accelLabel}</strong></div>
            </div>
            <div class="pressure-label">Pressure</div>
            <div class="pressure-track">
                <div class="pressure-center"></div>
                <div class="pressure-bar" style="${barStyle}"></div>
            </div>
            <div class="pressure-scale"><span>-3s</span><span>0</span><span>+3s</span></div>
        `;
        container.appendChild(card);
    });
}

function renderCockpitProfileChart(profileData, spotPrice, model, gammaSweep = null) {
    const strikeMap = {};
    profileData.forEach(row => {
        if (!strikeMap[row.strike_price]) strikeMap[row.strike_price] = { call: 0, put: 0, net: 0 };
        if (optionSide(row) === 'call') strikeMap[row.strike_price].call += row.gex_value;
        else strikeMap[row.strike_price].put += row.gex_value;
        strikeMap[row.strike_price].net += row.gex_value;
    });

    const strikes = Object.keys(strikeMap).map(parseFloat).sort((a, b) => a - b);
    if (!strikes.length) return;
    const netGexArr = strikes.map(s => strikeMap[s].net / 1000000);
    const callGexArr = strikes.map(s => strikeMap[s].call / 1000000);
    const putGexArr = strikes.map(s => strikeMap[s].put / 1000000);

    const markerLevels = [
        { label: 'Target', value: model.target, color: '#ff454f', dash: 'dot' },
        { label: 'Spot', value: spotPrice, color: '#ffffff', dash: 'solid' },
        ...sweepZeroMarkerLevels(gammaSweep, model.flip, spotPrice),
        { label: 'Invalidation', value: model.invalidation, color: '#f5a524', dash: 'dash' }
    ].filter(level => Number.isFinite(level.value));
    const markerLines = buildMarkerLines(markerLevels, strikes);
    const focusWindow = focusedStrikeWindow(strikes, markerLevels, spotPrice);
    const profileRows = strikes.map((strike, i) => ({
        strike,
        call: callGexArr[i],
        put: putGexArr[i],
        net: netGexArr[i]
    }));
    const initialRows = rowsInStrikeRange(profileRows, focusWindow.startValue, focusWindow.endValue);
    const { netBounds, sideBounds } = profileAxisBounds(initialRows);

    const option = {
        ...baseChartOptions({ valueFormatter: formatMillionsValue }),
        tooltip: {
            ...baseChartOptions({ valueFormatter: formatMillionsValue }).tooltip,
            formatter: profileTooltipFormatter(profileRows, formatMillionsValue)
        },
        grid: { top: 42, left: 58, right: 62, bottom: 46, containLabel: true },
        dataZoom: zoomDataOptions(focusWindow.startValue, focusWindow.endValue),
        legend: { show: false },
        xAxis: {
            type: 'value',
            name: 'Strike',
            nameLocation: 'middle',
            nameGap: 30,
            nameTextStyle: chartText(12),
            axisLabel: { ...chartText(12), formatter: formatAxisPrice },
            axisLine: { onZero: false, lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: CHART_GRID } }
        },
        yAxis: [
            {
                type: 'value',
                name: 'Net GEX by Strike (M)',
                min: netBounds.min,
                max: netBounds.max,
                nameTextStyle: chartText(12, '#b177ff'),
                axisLabel: { ...chartText(12, '#b177ff'), formatter: formatMillionsAxis },
                axisLine: { lineStyle: { color: '#223140' } },
                splitLine: { lineStyle: { color: CHART_GRID } }
            },
            {
                type: 'value',
                name: 'Call / Put GEX (M)',
                min: sideBounds.min,
                max: sideBounds.max,
                nameTextStyle: chartText(12),
                axisLabel: { ...chartText(12), formatter: formatMillionsAxis },
                axisLine: { lineStyle: { color: '#223140' } },
                splitLine: { show: false }
            }
        ],
        series: [
            {
                name: 'Call GEX',
                type: 'bar',
                yAxisIndex: 1,
                stack: 'gex',
                data: strikes.map((s, i) => [s, callGexArr[i]]),
                itemStyle: { color: '#ff8b1a', opacity: 0.86 },
                barMinHeight: 2,
                barWidth: 12
            },
            {
                name: 'Put GEX',
                type: 'bar',
                yAxisIndex: 1,
                stack: 'gex',
                data: strikes.map((s, i) => [s, putGexArr[i]]),
                itemStyle: { color: '#2388e8', opacity: 0.86 },
                barMinHeight: 2,
                barWidth: 12
            },
            {
                name: 'Net GEX by Strike',
                type: 'line',
                yAxisIndex: 0,
                smooth: true,
                symbol: 'none',
                data: strikes.map((s, i) => [s, netGexArr[i]]),
                lineStyle: { color: '#b177ff', width: 3 },
                markLine: {
                    symbol: 'none',
                    silent: true,
                    data: markerLines
                }
            }
        ]
    };

    setChartOption('cockpitProfileChart', option);
    attachProfileAxisAutoscale('cockpitProfileChart', profileRows);
    attachClickZoom('cockpitProfileChart', strikes);
}

function renderActionOverview(overviewData, symbolData) {
    const titleEl = document.getElementById('actionBiasTitle');
    const contextEl = document.getElementById('actionBiasContext');
    const scoreEl = document.getElementById('actionBiasScore');
    if (!titleEl || !contextEl || !scoreEl) return;

    const model = cachedWorkspace ? cockpitModelFromWorkspace(cachedWorkspace) : null;
    if (!model) return;
    const actionClass = model.score > 0.22 ? 'action-call' : model.score < -0.22 ? 'action-put' : 'action-wait';
    contextEl.innerText = `${model.symbol}: ${model.context}`;
    titleEl.innerText = model.title;
    titleEl.className = actionClass;
    scoreEl.innerText = `${Math.round(model.score * 100)}%`;
    scoreEl.className = `action-score ${actionClass}`;

    setVote('voteMarket', 'voteMarketDetail', model.marketVote);
    setVote('voteDealer', 'voteDealerDetail', model.dealerVote);
    setVote('voteLiquidity', 'voteLiquidityDetail', model.liquidityVote);

    const setupEl = document.getElementById('actionSetupType');
    const targetEl = document.getElementById('actionTargetPrice');
    const invalidationEl = document.getElementById('actionInvalidation');
    if (setupEl) setupEl.innerText = model.scenarioType;
    if (targetEl) targetEl.innerText = model.plan.target;
    if (invalidationEl) invalidationEl.innerText = model.plan.invalidation;
}

function renderCompass(compassData, type) {
    // type: 'Traders' or 'Whale'
    if (!compassData || !compassData.label) return;

    // 0. Update Tooltip
    // Show composition + default explanation
    const container = document.getElementById(`compass${type}`);
    if (container) {
        const baseTooltip = "X-Axis = normalized net-vs-gross gamma imbalance. Y-Axis = spot vs estimated flip. Data Quality falls when data is stale, thin, or approximate.";
        container.setAttribute('data-tooltip', `${compassData.composition}. ${baseTooltip}`);
    }

    // 1. Update Text
    const titleEl = document.getElementById(`title${type}`);
    const descEl = document.getElementById(`desc${type}`);
    if (!titleEl || !descEl) return;

    titleEl.innerText = compassData.label;
    const warnings = compassData.warnings && compassData.warnings.length
        ? ` | ${compassData.warnings.join(', ')}`
        : '';
    const quality = compassData.data_quality || { score: compassData.confidence, label: compassData.confidence_label };
    descEl.innerText = `${compassData.strategy} Data quality: ${formatPct(quality.score)} ${quality.label || ''}${warnings}`;

    // 2. Position the Puck
    // scores are -1 to 1. 0 is center (50%).
    // x (vol) -> left/right. -1 = 15%, 1 = 85% (Scale 35 to stay in circle)
    // y (trend) -> bottom/top. -1 = 85%, 1 = 15% (inverted)

    const xPct = 50 + (compassData.x_score * 35);
    const yPct = 50 - (compassData.y_score * 35);

    const puck = document.getElementById(`puck${type}`);
    if (!puck) return;
    puck.style.left = `${xPct}%`;
    puck.style.top = `${yPct}%`;

    // 3. Trail Logic (Ghost Pucks)
    const newPos = { x: xPct, y: yPct };
    const history = compassHistory[type];

    // Init history if empty
    if (history.length === 0) {
        history.push(newPos);
    } else {
        // Only add if position changed significantly (> 0.5%)
        const last = history[history.length - 1];
        const dist = Math.sqrt(Math.pow(newPos.x - last.x, 2) + Math.pow(newPos.y - last.y, 2));
        if (dist > 0.5) {
            history.push(newPos);
        }
    }

    // Keep max 6 items (Current + 5 Trails)
    if (history.length > 6) history.shift();

    // Render Trail Elements
    // Remove existing trails first (scoped to this compass container)
    // We need to query selector only inside this compass container
    const compassContainer = document.getElementById(`compass${type}`);
    if (!compassContainer) return;
    compassContainer.querySelectorAll('.compass-trail-puck').forEach(el => el.remove());

    // Iterate backwards from 1 step ago
    for (let i = 1; i <= 5; i++) {
        const idx = history.length - 1 - i;
        if (idx >= 0) {
            const pos = history[idx];
            const el = document.createElement('div');
            el.className = `compass-trail-puck trail-${i}`;
            el.style.left = `${pos.x}%`;
            el.style.top = `${pos.y}%`;
            compassContainer.appendChild(el);
        }
    }
    // Colorize Title
    titleEl.className = ''; // reset
    if (compassData.label.includes('GRIND')) titleEl.style.color = 'var(--green)';
    else if (compassData.label.includes('CRASH')) titleEl.style.color = 'var(--red)';
    else if (compassData.label.includes('MELT')) titleEl.style.color = '#ffc800';
    else titleEl.style.color = 'var(--text)';
}

function renderPillars(components) {
    const container = document.getElementById('pillarsContainer');
    container.innerHTML = '';

    components.forEach(comp => {
        // Distance Percentage (-5% to +5% range for visual)
        let pct = comp.distance_pct;
        // Clamp for visual bar
        // layout:  [Red ... 0 ... Green]
        // If pct is +1%, we want bar to go from center to right.

        const card = document.createElement('div');
        card.className = 'pillar-card';

        const isPos = pct >= 0;
        const colorClass = isPos ? 'val-positive' : 'val-negative';
        const rawDist = pct.toFixed(2);
        const quality = formatPct(comp.data_quality?.score ?? comp.confidence);
        const imbalance = formatPct(comp.gex_imbalance, 0);
        const warnings = comp.warnings && comp.warnings.length ? comp.warnings.join(', ') : 'clean';
        const flipQuality = (comp.flip_quality || 'unknown').replace('_', ' ');

        // Visual Bar Logic
        // We use a simple CSS grid or absolute positioning relative to center
        // Center is 50%.
        // Scale: Let's say max range is +/- 3%.
        const rangeMax = 3.0;
        let barWidth = (Math.abs(pct) / rangeMax) * 50;
        if (barWidth > 50) barWidth = 50;

        let barStyle = '';
        if (isPos) {
            barStyle = `left: 50%; width: ${barWidth}%; background-color: var(--green);`;
        } else {
            barStyle = `right: 50%; width: ${barWidth}%; background-color: var(--red);`;
        }

        card.innerHTML = `
            <div class="pillar-header">
                <span class="pillar-symbol">${comp.symbol}</span>
                <span class="pillar-val ${colorClass}">${rawDist}%</span>
            </div>
            <div class="pillar-flip-info">
                Flip: ${parseInt(comp.flip_strike)} | Spot: ${parseInt(comp.spot)}
            </div>
            <div class="pillar-quality">
                <span>Quality ${quality}</span>
                <span>${formatAge(comp.age_seconds)}</span>
                <span>Flip ${flipQuality}</span>
            </div>
            <div class="pillar-quality">
                <span>GEX imbalance ${imbalance}</span>
                <span>Warnings: ${warnings}</span>
            </div>
            <div class="pillar-accel">
                Accel <span style="color: ${comp.acceleration >= 0 ? 'var(--green)' : 'var(--red)'}">
                    $${(comp.acceleration / 1000000).toFixed(1)}M/pt
                </span>
            </div>
            <div class="pillar-track">
                <div class="pillar-center-line"></div>
                <div class="pillar-bar" style="${barStyle}"></div>
            </div>
        `;
        container.appendChild(card);
    });
}

function renderTiltChart(tiltData) {
    const symbols = tiltData.map(d => d.symbol);
    const vals = tiltData.map(d => d.net_gex); // This is now Effective GEX from backend
    const colors = vals.map(v => v >= 0 ? '#00d26a' : '#f85149');

    const option = {
        ...baseChartOptions(),
        grid: { top: 36, left: 42, right: 20, bottom: 34, containLabel: true },
        dataZoom: zoomDataOptions(),
        xAxis: {
            type: 'category',
            data: symbols,
            axisLabel: chartText(12),
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { show: false }
        },
        yAxis: {
            type: 'value',
            name: '$ per 1% move',
            nameTextStyle: { ...chartText(12), align: 'left' },
            axisLabel: { ...chartText(12), formatter: formatCompactNumber },
            axisLine: { lineStyle: { color: '#223140' } },
            splitLine: { lineStyle: { color: 'rgba(122,148,170,0.12)' } }
        },
        series: [{
            name: 'Effective GEX',
            type: 'bar',
            data: vals.map((v, i) => ({
                value: v,
                itemStyle: { color: colors[i] },
                label: {
                    show: true,
                    position: v >= 0 ? 'top' : 'bottom',
                    formatter: `$${(v / 1000000).toFixed(1)}M`,
                    color: CHART_TEXT,
                    fontFamily: NUMERIC_FONT,
                    fontSize: 12,
                    fontWeight: 700
                }
            })),
            barWidth: 24
        }]
    };

    setChartOption('tiltChart', option);
    attachClickZoom('tiltChart', symbols, 4);
}



if (window.innerWidth <= 1180) toggleActivityFeed();
init();
window.addEventListener('resize', () => resizeCharts());

// --- Tooltip Logic ---
document.addEventListener('mouseover', function (e) {
    const target = e.target.closest('[data-tooltip]');
    if (target) {
        const tooltip = document.getElementById('tooltip');
        tooltip.innerText = target.getAttribute('data-tooltip');
        tooltip.style.display = 'block';
    }
});

document.addEventListener('mousemove', function (e) {
    const tooltip = document.getElementById('tooltip');
    if (tooltip.style.display === 'block') {
        const x = e.clientX + 15;
        const y = e.clientY + 15;

        // Boundary check (simple)
        if (x + 250 > window.innerWidth) {
            tooltip.style.left = (e.clientX - 260) + 'px';
        } else {
            tooltip.style.left = x + 'px';
        }

        if (y + 50 > window.innerHeight) {
            tooltip.style.top = (e.clientY - 40) + 'px';
        } else {
            tooltip.style.top = y + 'px';
        }
    }
});

document.addEventListener('mouseout', function (e) {
    const target = e.target.closest('[data-tooltip]');
    if (target) {
        const tooltip = document.getElementById('tooltip');
        tooltip.style.display = 'none';
    }
});

// --- Backend Event Handling ---

eel.expose(handle_backend_event);
function handle_backend_event(event) {
    console.log("Backend Event Received:", event);

    addNotificationToPanel(event);

    // 2. Handle Specifics
    if (event.type === 'data_refresh') {
        updateBackendStatus();
        // Trigger UI update if it matches current symbol or just notify
        const currentSymbol = document.getElementById('symbolSelector').value;
        if (event.payload.symbol === currentSymbol) {
            console.log("Refreshing data for current symbol...");
            loadSymbol(); // Reloads data from DB
        }
    }
    else if (event.type === 'MARKET_UPDATE') {
        updateBackendStatus();
        loadSymbol();
    }
    else if (event.type === 'magnet_change') {
        const p = event.payload;
        // Check if irrelevant to current view? Maybe show anyway if it's important.
        showToast(
            "MAGNET CHANGE",
            `${p.symbol} Magnet Moved: ${p.old_magnet.toFixed(0)} -> ${p.new_magnet.toFixed(0)}`,
            "magnet"
        );
    } else if (event.type === 'decision_alert') {
        showToast(event.payload.alert_type || 'Decision Alert', event.payload.message, event.payload.severity === 'critical' ? 'magnet' : 'info');
    }
}

function addNotificationToPanel(event) {
    recordActivity(ActivityFeed.eventActivity(event));
}

function recordActivity(activity) {
    const feed = document.getElementById('activityFeed');
    if (!feed || !activity) return;

    if (feed.children.length === 1 && feed.children[0].innerText.includes('Listening')) {
        feed.innerHTML = '';
    }

    let div = null;
    if (activity.key) {
        div = [...feed.children].find(item => item.dataset.feedKey === activity.key) || null;
    }
    if (!div) {
        div = document.createElement('div');
        if (activity.key) div.dataset.feedKey = activity.key;
    }

    div.className = `activity-item ${activity.typeClass || 'type-info'}`;
    div.replaceChildren();
    for (const [className, value] of [
        ['activity-time', new Date().toLocaleTimeString()],
        ['activity-title', activity.title || 'Event'],
        ['activity-msg', activity.body || '']
    ]) {
        const span = document.createElement('span');
        span.className = className;
        span.textContent = value;
        div.appendChild(span);
    }
    feed.insertBefore(div, feed.firstChild);

    while (feed.children.length > 50) feed.removeChild(feed.lastChild);
}

// --- Toast Notifications ---

function initToastContainer() {
    if (!document.getElementById('toast-container')) {
        const c = document.createElement('div');
        c.id = 'toast-container';
        document.body.appendChild(c);
    }
}

function showToast(title, message, type = "info") {
    const container = document.getElementById('toast-container');
    if (!container) initToastContainer();

    const toast = document.createElement('div');
    toast.className = `toast-notification toast-${type}`;

    const body = document.createElement('div');
    body.className = 'toast-body';
    const heading = document.createElement('div');
    heading.className = 'toast-header';
    heading.textContent = title;
    const detail = document.createElement('div');
    detail.textContent = message;
    body.append(heading, detail);
    const close = document.createElement('button');
    close.className = 'toast-close';
    close.textContent = '×';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.onclick = () => toast.remove();
    toast.append(body, close);

    document.getElementById('toast-container').appendChild(toast);
    while (document.getElementById('toast-container').children.length > 3) document.getElementById('toast-container').firstChild.remove();

    // Auto remove
    setTimeout(() => {
        toast.style.animation = 'fadeOut 0.3s ease-out forwards';
        setTimeout(() => toast.remove(), 300);
    }, 5000);
}

// Initialize Toasts on Load
window.addEventListener('load', initToastContainer);
