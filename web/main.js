let refreshTimer = null;
let countdownTimer = null;
let currentSettings = { refresh_interval: 60, theme: 'dark', symbols: [], backend_update_delay: 180 };
let timeLeft = 0;
let cachedData = null;
let compassHistory = { Traders: [], Whale: [] }; // Trail history per compass


// --- Init ---
async function init() {
    currentSettings = await eel.get_settings()();
    document.getElementById('settingInterval').value = currentSettings.refresh_interval;
    document.getElementById('settingTheme').value = currentSettings.theme || 'dark';
    document.getElementById('settingSymbols').value = (currentSettings.symbols || []).join(',');
    document.getElementById('settingBackendDelay').value = currentSettings.backend_update_delay || 180;

    // Trigger data refresh on load
    const lastUpdateEl = document.getElementById('lastUpdate');
    if (lastUpdateEl) lastUpdateEl.innerText = "Refreshing...";

    console.log("Triggering backend data refresh...");
    await eel.trigger_data_refresh()();
    console.log("Backend data refresh complete.");

    const symbols = await eel.get_symbols()();

    const selector = document.getElementById('symbolSelector');
    selector.innerHTML = '';
    symbols.forEach(sym => {
        const opt = document.createElement('option');
        opt.value = sym;
        opt.innerText = sym;
        selector.appendChild(opt);
    });

    if (symbols.length > 0) {
        loadSymbol();
        startTimers();
    }
}

function switchView(viewName) {
    document.querySelectorAll('.view-section').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('active'));

    document.getElementById(`view-${viewName}`).style.display = 'block';

    document.getElementById(`view-${viewName}`).style.display = 'block';

    const btns = document.querySelectorAll('.nav-btn');
    // Indices: 0=Overview, 1=Dashboard, 2=Analysis, 3=Settings
    if (viewName === 'overview') {
        btns[0].classList.add('active');
        // Just call loadOverview every time for now or verify compass
        loadOverview();
    }
    if (viewName === 'dashboard') btns[1].classList.add('active');
    if (viewName === 'analysis') btns[2].classList.add('active');
    if (viewName === 'settings') btns[3].classList.add('active');

    if (viewName === 'dashboard' && cachedData) {
        Plotly.Plots.resize('profileChart');
        Plotly.Plots.resize('historyChart');
    }
    if (viewName === 'overview') {
        Plotly.Plots.resize('tiltChart');
    }
}

async function loadSymbol() {
    const symbol = document.getElementById('symbolSelector').value;
    const data = await eel.get_dashboard_data(symbol)();

    if (data.error) { console.error(data.error); return; }

    cachedData = data;
    renderDashboard(data);
    renderAnalysisTable(data);
    timeLeft = currentSettings.refresh_interval;
}

function renderDashboard(data) {
    // Pre-process data for KPIs to find High/Low Vol Points
    let strikes = {};
    let maxNetPos = { val: 0, strike: 0 };
    let maxNetNeg = { val: 0, strike: 0 };

    data.profile.forEach(row => {
        const s = row.strike_price;
        if (!strikes[s]) strikes[s] = 0;
        strikes[s] += row.gex_value; // Combine Call (+) and Put (-)
    });

    for (const [s, netGex] of Object.entries(strikes)) {
        const strike = parseFloat(s);
        if (netGex > maxNetPos.val) maxNetPos = { val: netGex, strike: strike };
        if (netGex < maxNetNeg.val) maxNetNeg = { val: netGex, strike: strike };
    }

    updateKPIs(data.snapshot, maxNetPos.strike, maxNetNeg.strike);
    renderProfileChart(data.profile, data.snapshot.spot_price);
    renderHistoryChart(data.history);
}

function updateKPIs(snap, lowVolStrike, highVolStrike) {
    document.getElementById('kpiSpot').innerText = `$${snap.spot_price.toFixed(2)}`;

    // Update Regime Gauge
    const netGexM = snap.total_net_gex / 1000000;
    const regimeMarker = document.getElementById('regimeIndicator');
    const regimeText = document.getElementById('regimeText');

    // Normalize for gauge (assume +/- $1B range for visual sake, clamp it)
    let pct = 50 + (netGexM / 1000) * 50;
    if (pct > 95) pct = 95;
    if (pct < 5) pct = 5;

    regimeMarker.style.left = `${pct}%`;

    if (netGexM > 0) {
        regimeText.innerText = `COMPRESSION ($${netGexM.toFixed(0)}M)`;
        regimeText.style.color = "var(--stability-color)";
    } else {
        regimeText.innerText = `EXPANSION ($${netGexM.toFixed(0)}M)`;
        regimeText.style.color = "var(--volatility-color)";
    }

    // High/Low Vol Points
    document.getElementById('kpiLowVol').innerText = lowVolStrike > 0 ? lowVolStrike.toFixed(0) : 'N/A';
    document.getElementById('kpiHighVol').innerText = highVolStrike > 0 ? highVolStrike.toFixed(0) : 'N/A';

    const dateObj = new Date(snap.timestamp);
    document.getElementById('lastUpdate').innerText = dateObj.toLocaleTimeString();
}

function renderProfileChart(profileData, spotPrice) {
    let strikeMap = {};
    profileData.forEach(row => {
        if (!strikeMap[row.strike_price]) strikeMap[row.strike_price] = { call: 0, put: 0, net: 0 };
        if (row.option_type === 'CALL') strikeMap[row.strike_price].call += row.gex_value;
        else strikeMap[row.strike_price].put += row.gex_value; // Puts are negative
        strikeMap[row.strike_price].net += row.gex_value;
    });

    const strikes = Object.keys(strikeMap).map(parseFloat).sort((a, b) => a - b);
    const netGexArr = strikes.map(s => strikeMap[s].net);
    const callGexArr = strikes.map(s => strikeMap[s].call);
    const putGexArr = strikes.map(s => strikeMap[s].put); // Negative values

    // Trace 1: Net Gamma Curve
    const traceLine = {
        x: strikes,
        y: netGexArr,
        type: 'scatter',
        mode: 'lines',
        name: 'Net Gamma',
        line: { color: '#b392f0', width: 3, shape: 'spline' },
        yaxis: 'y1'
    };

    // Trace 2: Calls (Orange)
    const traceCalls = {
        x: strikes,
        y: callGexArr,
        type: 'bar',
        name: 'Call Gamma',
        marker: { color: '#ff9100' },
        yaxis: 'y2',
        opacity: 0.6
    };

    // Trace 3: Puts (Blue)
    const tracePuts = {
        x: strikes,
        y: putGexArr,
        type: 'bar',
        name: 'Put Gamma',
        marker: { color: '#0091ff' },
        yaxis: 'y2',
        opacity: 0.6
    };

    const layout = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#8b949e', family: 'Inter' },
        margin: { t: 30, l: 60, r: 60, b: 40 },
        hovermode: 'x unified',
        xaxis: {
            title: 'Strike',
            gridcolor: '#30363d',
        },
        yaxis: {
            title: 'Net Gamma Exposure',
            titlefont: { color: '#b392f0' },
            tickfont: { color: '#b392f0' },
            gridcolor: '#30363d',
        },
        yaxis2: {
            title: 'Total Call/Put GEX',
            overlaying: 'y',
            side: 'right',
            showgrid: false
        },
        barmode: 'relative',
        showlegend: false,
        shapes: [{
            type: 'line',
            x0: spotPrice, x1: spotPrice,
            y0: 0, y1: 1, xref: 'x', yref: 'paper',
            line: { color: 'white', width: 1, dash: 'solid' }
        }]
    };

    Plotly.newPlot('profileChart', [traceCalls, tracePuts, traceLine], layout, { displayModeBar: false, responsive: true });
}

function renderHistoryChart(history) {
    if (!history || history.length === 0) return;

    const traceGex = {
        x: history.map(d => d.timestamp),
        y: history.map(d => d.total_net_gex),
        type: 'scatter',
        mode: 'lines',
        fill: 'tozeroy',
        name: 'Net GEX',
        line: { color: '#b392f0', width: 2 }
    };

    // Add Zero Line
    const layout = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#8b949e', family: 'Inter' },
        margin: { t: 10, l: 40, r: 20, b: 40 },
        xaxis: { gridcolor: '#30363d' },
        yaxis: { gridcolor: '#30363d' },
        showlegend: false,
        shapes: [{
            type: 'line',
            x0: history[0].timestamp,
            x1: history[history.length - 1].timestamp,
            y0: 0, y1: 0,
            xref: 'x', yref: 'y',
            line: { color: '#444', width: 1, dash: 'dot' }
        }]
    };
    Plotly.newPlot('historyChart', [traceGex], layout, { displayModeBar: false, responsive: true });
}

// --- Analysis Table (Matches previous redesign) ---
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
        if (row.option_type === 'CALL') {
            strikes[s].callGex = row.gex_value;
            strikes[s].callOI = row.open_interest;
        } else {
            strikes[s].putGex = row.gex_value;
            strikes[s].putOI = row.open_interest;
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
            badgeHtml = `<span class="badge badge-magnet">🔥 MAGNET</span>`;
        } else if (row.netGex > 0) {
            badgeHtml = `<span class="badge badge-stability">🛡️ STABILITY</span>`;
        } else {
            badgeHtml = `<span class="badge badge-volatility">⚡ VOLATILITY</span>`;
        }

        const callWidth = (Math.abs(row.callGex) / maxGexAbs) * 100;
        const putWidth = (Math.abs(row.putGex) / maxGexAbs) * 100;
        const netValM = (row.netGex / 1000000).toFixed(2);

        tr.innerHTML = `
            <td class="strike-cell ${strikeClass}">${row.strike.toFixed(0)}</td>
            <td>
                ${badgeHtml}
                <div style="font-size:10px; color:#666; margin-top:3px;">
                    ${row.netGex > 0 ? 'Dealer Long Gamma' : 'Dealer Short Gamma'}
                </div>
            </td>
            <td style="font-family: monospace; font-size:13px;">
                <span class="${row.netGex > 0 ? 'val-positive' : 'val-negative'}">$${netValM}M</span>
            </td>
            <td>
                <div class="bar-container">
                    <span style="font-size:11px; color:#888">${(row.callGex / 1000000).toFixed(2)}</span>
                    <div class="bg-bar bar-call" style="width: ${callWidth}px; max-width:80px;"></div>
                </div>
            </td>
            <td>
                <div class="bar-container" style="justify-content: flex-start;">
                    <div class="bg-bar bar-put" style="width: ${putWidth}px; max-width:80px;"></div>
                    <span style="font-size:11px; color:#888">${(Math.abs(row.putGex) / 1000000).toFixed(2)}</span>
                </div>
            </td>
            <td style="color:#888;">${(row.callOI + row.putOI).toLocaleString()}</td>
        `;
        tbody.appendChild(tr);
    });

    setTimeout(() => {
        const atmRow = document.querySelector('.strike-cell.atm');
        if (atmRow) atmRow.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 500);
}

function startTimers() {
    if (refreshTimer) clearInterval(refreshTimer);
    if (countdownTimer) clearInterval(countdownTimer);
    timeLeft = currentSettings.refresh_interval;

    countdownTimer = setInterval(() => {
        timeLeft--;
        document.getElementById('timerCountdown').innerText = `${timeLeft}s`;
        if (timeLeft <= 0) timeLeft = currentSettings.refresh_interval;
    }, 1000);

    refreshTimer = setInterval(async () => {
        console.log("Auto-refreshing data...");
        const lastUpdateEl = document.getElementById('lastUpdate');
        if (lastUpdateEl) lastUpdateEl.innerText = "Refreshing...";

        await eel.trigger_data_refresh()();
        loadSymbol();
    }, currentSettings.refresh_interval * 1000);
}

function saveSettings() {
    const newInterval = parseInt(document.getElementById('settingInterval').value);
    const newTheme = document.getElementById('settingTheme').value;
    const newSymbols = document.getElementById('settingSymbols').value.split(',').map(s => s.trim()).filter(Boolean);
    const newBackendDelay = parseInt(document.getElementById('settingBackendDelay').value);
    eel.save_settings({
        refresh_interval: newInterval,
        theme: newTheme,
        symbols: newSymbols,
        backend_update_delay: newBackendDelay
    })();
    currentSettings.refresh_interval = newInterval;
    currentSettings.theme = newTheme;
    currentSettings.symbols = newSymbols;
    currentSettings.backend_update_delay = newBackendDelay;
    alert("Settings Saved");
    startTimers();
}

// --- Overview / Signal Dashboard ---

async function loadOverview() {
    const data = await eel.get_market_overview()();
    if (data.error) { console.error(data.error); return; }
    renderSignalDashboard(data);
    document.getElementById('trafficLight').dataset.loaded = "true";
}

function renderSignalDashboard(data) {
    renderCompass(data.compass_traders, 'Traders');
    renderCompass(data.compass_whale, 'Whale');

    renderPillars(data.components);
    renderTiltChart(data.tilt);
}

function renderCompass(compassData, type) {
    // type: 'Traders' or 'Whale'
    if (!compassData || !compassData.label) return;

    // 0. Update Tooltip
    // Show composition + default explanation
    const container = document.getElementById(`compass${type}`);
    if (container) {
        const baseTooltip = "X-Axis = Volatility (Net Gamma). Y-Axis = Trend (Spot vs Flip).";
        container.setAttribute('data-tooltip', `${compassData.composition}. ${baseTooltip}`);
    }

    // 1. Update Text
    const titleEl = document.getElementById(`title${type}`);
    const descEl = document.getElementById(`desc${type}`);

    titleEl.innerText = compassData.label;
    descEl.innerText = compassData.strategy;

    // 2. Position the Puck
    // scores are -1 to 1. 0 is center (50%).
    // x (vol) -> left/right. -1 = 15%, 1 = 85% (Scale 35 to stay in circle)
    // y (trend) -> bottom/top. -1 = 85%, 1 = 15% (inverted)

    const xPct = 50 + (compassData.x_score * 35);
    const yPct = 50 - (compassData.y_score * 35);

    const puck = document.getElementById(`puck${type}`);
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
    if (compassData.label.includes('GRIND')) titleEl.style.color = 'var(--stability-color)';
    else if (compassData.label.includes('CRASH')) titleEl.style.color = 'var(--volatility-color)';
    else if (compassData.label.includes('MELT')) titleEl.style.color = '#ffc800';
    else titleEl.style.color = 'white';
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

        // Visual Bar Logic
        // We use a simple CSS grid or absolute positioning relative to center
        // Center is 50%.
        // Scale: Let's say max range is +/- 3%.
        const rangeMax = 3.0;
        let barWidth = (Math.abs(pct) / rangeMax) * 50;
        if (barWidth > 50) barWidth = 50;

        let barStyle = '';
        if (isPos) {
            barStyle = `left: 50%; width: ${barWidth}%; background-color: var(--stability-color);`;
        } else {
            barStyle = `right: 50%; width: ${barWidth}%; background-color: var(--volatility-color);`;
        }

        card.innerHTML = `
            <div class="pillar-header">
                <span class="pillar-symbol">${comp.symbol}</span>
                <span class="pillar-val ${colorClass}">${rawDist}%</span>
            </div>
            <div class="pillar-flip-info">
                Flip: ${parseInt(comp.flip_strike)} | Spot: ${parseInt(comp.spot)}
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

    const trace = {
        x: symbols,
        y: vals,
        type: 'bar',
        marker: { color: colors },
        text: vals.map(v => `$${(v / 1000000).toFixed(1)}M`), // 1 decimal for smaller effective numbers
        textposition: 'auto'
    };

    const layout = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#8b949e', family: 'Inter' },
        margin: { t: 20, l: 40, r: 20, b: 30 },
        xaxis: { gridcolor: '#30363d' },
        yaxis: {
            gridcolor: '#30363d',
            title: 'Effective GEX ($)'
        }
    };

    Plotly.newPlot('tiltChart', [trace], layout, { displayModeBar: false, responsive: true });
}



init();

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

    // 1. Add to Panel
    addNotificationToPanel(event);

    // 2. Handle Specifics
    if (event.type === 'data_refresh') {
        // Trigger UI update if it matches current symbol or just notify
        const currentSymbol = document.getElementById('symbolSelector').value;
        if (event.payload.symbol === currentSymbol) {
            console.log("Refreshing data for current symbol...");
            loadSymbol(); // Reloads data from DB
            showToast("Data Updated", `New data available for ${event.payload.symbol}`, "info");
        }
    }
    else if (event.type === 'magnet_change') {
        const p = event.payload;
        // Check if irrelevant to current view? Maybe show anyway if it's important.
        showToast(
            "MAGNET CHANGE",
            `${p.symbol} Magnet Moved: ${p.old_magnet.toFixed(0)} ➔ ${p.new_magnet.toFixed(0)}`,
            "magnet"
        );
    }
}

function addNotificationToPanel(event) {
    const feed = document.getElementById('activityFeed');
    if (!feed) return;

    // Remove "Listening..." placeholder if exists
    if (feed.children.length === 1 && feed.children[0].innerText.includes('Listening')) {
        feed.innerHTML = '';
    }

    const div = document.createElement('div');
    const ts = new Date().toLocaleTimeString();

    let title = "Event";
    let body = "";
    let typeClass = "type-info";

    if (event.type === 'magnet_change') {
        title = "Magnet Shift";
        body = `${event.payload.symbol}: ${event.payload.old_magnet.toFixed(0)} ➔ ${event.payload.new_magnet.toFixed(0)}`;
        typeClass = "type-magnet";
    } else if (event.type === 'data_refresh') {
        title = "Data Update";
        body = `Refresh for ${event.payload.symbol}`;
    }

    div.className = `activity-item ${typeClass}`;
    div.innerHTML = `
        <span class="activity-time">${ts}</span>
        <span class="activity-title">${title}</span>
        <span class="activity-msg">${body}</span>
    `;

    // Prepend
    feed.insertBefore(div, feed.firstChild);

    // Limit history
    if (feed.children.length > 50) {
        feed.removeChild(feed.lastChild);
    }
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

    toast.innerHTML = `
        <div class="toast-body">
            <div class="toast-header">${title}</div>
            <div>${message}</div>
        </div>
        <div class="toast-close" onclick="this.parentElement.remove()">×</div>
    `;

    document.getElementById('toast-container').appendChild(toast);

    // Auto remove
    setTimeout(() => {
        toast.style.animation = 'fadeOut 0.3s ease-out forwards';
        setTimeout(() => toast.remove(), 300);
    }, 5000);
}

// Initialize Toasts on Load
window.addEventListener('load', initToastContainer);
