let refreshTimer = null;
let countdownTimer = null;
let currentSettings = { refresh_interval: 60, theme: 'dark', symbols: [], backend_update_delay: 180, raw_retention_days: 30 };
let timeLeft = 0;
let cachedData = null;
let cachedSymbol = null;
let cachedOverview = null;
let cockpitModel = null;
let compassHistory = { Traders: [], Whale: [] }; // Trail history per compass


// --- Init ---
async function init() {
    currentSettings = await eel.get_settings()();
    document.getElementById('settingInterval').value = currentSettings.refresh_interval;
    document.getElementById('settingTheme').value = currentSettings.theme || 'dark';
    document.getElementById('settingSymbols').value = (currentSettings.symbols || []).join(',');
    document.getElementById('settingBackendDelay').value = currentSettings.backend_update_delay || 180;
    const retentionInput = document.getElementById('settingRetentionDays');
    if (retentionInput) retentionInput.value = currentSettings.raw_retention_days || 30;

    // Trigger data refresh on load
    const lastUpdateEl = document.getElementById('lastUpdate');
    if (lastUpdateEl) lastUpdateEl.innerText = "Refreshing...";

    console.log("Triggering backend data refresh...");
    const refreshResult = await eel.trigger_data_refresh()();
    console.log("Backend data refresh complete.", refreshResult);
    if (refreshResult && refreshResult.ok === false) {
        showToast("Refresh Skipped", refreshResult.message || "Collector did not save new data.", "info");
    }

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
        await loadSymbol();
        startTimers();
        switchView('cockpit');
    } else {
        const opt = document.createElement('option');
        opt.value = '';
        opt.innerText = 'No 0DTE data';
        selector.appendChild(opt);
        if (lastUpdateEl) lastUpdateEl.innerText = "No data";
    }
}

function switchView(viewName) {
    document.querySelectorAll('.view-section').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('active'));

    const target = document.getElementById(`view-${viewName}`);
    if (target) target.style.display = 'block';

    document.querySelectorAll(`[data-view="${viewName}"]`).forEach(btn => btn.classList.add('active'));
    if (viewName === 'dashboard') document.querySelector('[data-view="dashboard"]')?.classList.add('active');

    if (viewName === 'cockpit') {
        document.querySelector('[data-view="cockpit"]')?.classList.add('active');
        loadCockpit();
    }
    if (viewName === 'market-signal') {
        document.querySelector('[data-view="market-signal"]')?.classList.add('active');
        loadOverview();
    }
    if (viewName === 'analysis') document.querySelector('[data-view="analysis"]')?.classList.add('active');
    if (viewName === 'settings') document.querySelector('[data-view="settings"]')?.classList.add('active');

    if (viewName === 'dashboard' && cachedData) {
        Plotly.Plots.resize('profileChart');
        Plotly.Plots.resize('historyChart');
    }
    if (viewName === 'market-signal') {
        Plotly.Plots.resize('tiltChart');
    }
    if (viewName === 'cockpit' && cachedData) {
        requestAnimationFrame(() => Plotly.Plots.resize('cockpitProfileChart'));
    }
}

async function loadSymbol() {
    const symbol = document.getElementById('symbolSelector').value;
    if (!symbol) return;
    const data = await eel.get_dashboard_data(symbol)();

    if (data.error) {
        console.error(data.error);
        showToast("No Data", data.error, "info");
        return;
    }

    cachedData = data;
    cachedSymbol = symbol;
    renderDashboard(data);
    renderAnalysisTable(data);
    if (document.getElementById('view-cockpit').style.display !== 'none') {
        await loadCockpit();
    }
    if (
        document.getElementById('view-market-signal').style.display === 'block'
    ) {
        loadOverview();
    }
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
    const topSpot = document.getElementById('topSpot');
    const cockpitSymbol = document.getElementById('cockpitSymbol');
    if (topSpot) topSpot.innerText = snap.spot_price.toFixed(2);
    if (cockpitSymbol) cockpitSymbol.innerText = snap.symbol || cachedSymbol || '--';

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
        regimeText.style.color = "var(--green)";
    } else {
        regimeText.innerText = `EXPANSION ($${netGexM.toFixed(0)}M)`;
        regimeText.style.color = "var(--red)";
    }

    // High/Low Vol Points
    document.getElementById('kpiLowVol').innerText = lowVolStrike > 0 ? lowVolStrike.toFixed(0) : 'N/A';
    document.getElementById('kpiHighVol').innerText = highVolStrike > 0 ? highVolStrike.toFixed(0) : 'N/A';

    // Acceleration (GEX Slope)
    const accelEl = document.getElementById('kpiAcceleration');
    if (accelEl && snap.gex_slope !== undefined) {
        const slopeM = snap.gex_slope / 1000000;
        accelEl.innerText = `$${slopeM.toFixed(1)}M`;
        accelEl.style.color = snap.gex_slope >= 0 ? 'var(--green)' : 'var(--red)';
    }

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
        const timerEl = document.getElementById('timerCountdown');
        if (timerEl) timerEl.innerText = `${timeLeft}s`;
        if (timeLeft <= 0) timeLeft = currentSettings.refresh_interval;
    }, 1000);

    refreshTimer = setInterval(async () => {
        console.log("Auto-refreshing data...");
        const lastUpdateEl = document.getElementById('lastUpdate');
        if (lastUpdateEl) lastUpdateEl.innerText = "Refreshing...";

        const result = await eel.trigger_data_refresh()();
        if (result && result.ok === false) {
            showToast("Refresh Skipped", result.message || "Collector did not save new data.", "info");
        }
        await loadSymbol();
    }, currentSettings.refresh_interval * 1000);
}

async function manualRefresh() {
    const lastUpdateEl = document.getElementById('lastUpdate');
    if (lastUpdateEl) lastUpdateEl.innerText = "Refreshing...";

    const result = await eel.trigger_data_refresh()();
    if (result && result.ok === false) {
        showToast("Refresh Skipped", result.message || "Collector did not save new data.", "info");
    } else {
        showToast("Refresh Complete", "Latest market data requested.", "info");
    }

    await loadSymbol();
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
}

function saveSettings() {
    const newInterval = parseInt(document.getElementById('settingInterval').value);
    const newTheme = document.getElementById('settingTheme').value;
    const newSymbols = document.getElementById('settingSymbols').value.split(',').map(s => s.trim()).filter(Boolean);
    const newBackendDelay = parseInt(document.getElementById('settingBackendDelay').value);
    const retentionEl = document.getElementById('settingRetentionDays');
    const newRetentionDays = retentionEl ? parseInt(retentionEl.value) : 30;
    eel.save_settings({
        refresh_interval: newInterval,
        theme: newTheme,
        symbols: newSymbols,
        backend_update_delay: newBackendDelay,
        raw_retention_days: newRetentionDays
    })();
    currentSettings.refresh_interval = newInterval;
    currentSettings.theme = newTheme;
    currentSettings.symbols = newSymbols;
    currentSettings.backend_update_delay = newBackendDelay;
    currentSettings.raw_retention_days = newRetentionDays;
    showToast("Settings Saved", "Configuration updated.", "info");
    startTimers();
}

// --- Overview / Signal Dashboard ---

async function loadOverview() {
    const data = await eel.get_market_overview()();
    if (data.error) { console.error(data.error); return; }
    cachedOverview = data;
    const selectedSymbol = document.getElementById('symbolSelector').value;
    let symbolData = cachedData;
    if (selectedSymbol && (!symbolData || cachedSymbol !== selectedSymbol)) {
        symbolData = await eel.get_dashboard_data(selectedSymbol)();
        if (!symbolData.error) {
            cachedData = symbolData;
            cachedSymbol = selectedSymbol;
        }
    }
    renderSignalDashboard(data);
    renderActionOverview(data, symbolData);
    const trafficLight = document.getElementById('trafficLight');
    if (trafficLight) trafficLight.dataset.loaded = "true";
}

function renderSignalDashboard(data) {
    renderCompass(data.compass_traders, 'Traders');
    renderCompass(data.compass_whale, 'Whale');

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
        if (row.option_type === 'CALL') map[strike].callGex += row.gex_value;
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
    const whale = overviewData.compass_whale || {};
    const traderConfidence = traders.confidence || 0;
    const whaleConfidence = whale.confidence || 0;
    const totalConfidence = traderConfidence + whaleConfidence || 1;
    const score = clamp(
        ((traders.y_score || 0) * traderConfidence + (whale.y_score || 0) * whaleConfidence) / totalConfidence,
        -1,
        1
    );
    const detail = `Traders ${voteLabel(traders.y_score || 0)} / Whale ${voteLabel(whale.y_score || 0)} | confidence ${formatPct((traderConfidence + whaleConfidence) / 2)}`;
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
    const maxAbs = rows.reduce((max, row) => Math.max(max, Math.abs(row.netGex)), 0);
    const threshold = maxAbs * 0.20;
    const candidates = rows
        .filter(row => direction === 'above' ? row.strike > spot : row.strike < spot)
        .filter(row => Math.abs(row.netGex) >= threshold && Math.sign(row.netGex) === sign)
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

function buildCockpitModel(symbolData, overviewData) {
    if (!symbolData || symbolData.error || !overviewData || overviewData.error) return null;

    const component = selectedComponent(overviewData, symbolData);
    const marketVote = buildMarketVote(overviewData);
    const dealerVote = buildDealerVote(symbolData, component);
    const liquidityVote = buildLiquidityVote(symbolData);
    const rawScore = clamp((marketVote.score * 0.45) + (dealerVote.score * 0.35) + (liquidityVote.score * 0.20), -1, 1);
    const agreement = [marketVote.score, dealerVote.score, liquidityVote.score]
        .filter(score => Math.sign(score) === Math.sign(rawScore) && Math.abs(score) > 0.20).length;
    const confidencePenalty = component ? component.confidence || 0.5 : 0.45;
    const finalScore = rawScore * clamp(0.65 + (agreement * 0.12), 0.65, 1) * confidencePenalty;
    const plan = buildTradePlan(symbolData, overviewData, component, finalScore, marketVote);
    const rows = buildStrikeProfile(symbolData.profile);
    const fallbackFlip = estimateFlipFromProfileRows(rows);
    const flip = component?.flip_strike || fallbackFlip.strike;
    const direction = finalScore > 0.22 ? 1 : finalScore < -0.22 ? -1 : 0;
    const title = direction > 0 ? 'CALL BIAS' : direction < 0 ? 'PUT BIAS' : 'WAIT';
    const selectedSymbol = symbolData.snapshot.symbol || document.getElementById('symbolSelector').value;
    const conflictText = agreement < 2 ? 'Inputs are mixed; require price confirmation.' : 'Inputs are aligned enough for directional context.';
    const context = `${conflictText} ${plan.description}`;
    const whale = overviewData.compass_whale || {};

    return {
        symbol: selectedSymbol,
        component,
        marketVote,
        dealerVote,
        liquidityVote,
        whale,
        score: finalScore,
        confidence: component?.confidence ?? Math.abs(finalScore),
        title,
        context,
        plan,
        flip,
        target: parseLevel(plan.target),
        invalidation: parseLevel(plan.invalidation)
    };
}

async function loadCockpit() {
    if (!cachedData || cachedData.error) return;
    cachedOverview = await eel.get_market_overview()();
    if (cachedOverview.error) {
        console.error(cachedOverview.error);
        return;
    }
    cockpitModel = buildCockpitModel(cachedData, cachedOverview);
    renderCockpit(cockpitModel, cachedData, cachedOverview);
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

function renderCockpit(model, symbolData, overviewData) {
    if (!model) return;

    const scoreClass = model.score > 0.22 ? 'green' : model.score < -0.22 ? 'red' : 'amber';
    setText('cockpitSymbol', model.symbol);
    setText('cockpitContext', model.context);
    setClassText('cockpitBiasLabel', model.title, scoreClass);
    setClassText('cockpitBiasScore', `${Math.round(model.score * 100)}%`, scoreClass);
    setText('cockpitConfidence', `${Math.round((model.confidence || 0) * 100)}%`);
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
    setText('tileWhaleDetail', model.whale.strategy || 'No whale composite.');

    renderCockpitProfileChart(symbolData.profile, symbolData.snapshot.spot_price, model);
    renderMetricStrip(symbolData, model);
    renderCockpitPillars(overviewData.components || []);
}

function renderMetricStrip(symbolData, model) {
    const callTotal = symbolData.profile
        .filter(row => row.option_type === 'CALL')
        .reduce((sum, row) => sum + row.gex_value, 0);
    const putTotal = symbolData.profile
        .filter(row => row.option_type !== 'CALL')
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
    setText('stripZeroGamma', model.flip ? formatTargetPrice(model.flip) : '--');
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
        const quality = comp.confidence >= 0.8 ? 'A' : comp.confidence >= 0.65 ? 'B+' : comp.confidence >= 0.45 ? 'B' : 'C';
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
                <div><span>Quality</span><strong class="${comp.confidence >= 0.65 ? 'green-text' : 'amber-text'}">${quality}</strong></div>
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

function renderCockpitProfileChart(profileData, spotPrice, model) {
    const strikeMap = {};
    profileData.forEach(row => {
        if (!strikeMap[row.strike_price]) strikeMap[row.strike_price] = { call: 0, put: 0, net: 0 };
        if (row.option_type === 'CALL') strikeMap[row.strike_price].call += row.gex_value;
        else strikeMap[row.strike_price].put += row.gex_value;
        strikeMap[row.strike_price].net += row.gex_value;
    });

    const strikes = Object.keys(strikeMap).map(parseFloat).sort((a, b) => a - b);
    if (!strikes.length) return;
    const netGexArr = strikes.map(s => strikeMap[s].net / 1000000);
    const callGexArr = strikes.map(s => strikeMap[s].call / 1000000);
    const putGexArr = strikes.map(s => strikeMap[s].put / 1000000);
    const minY = Math.min(...netGexArr, ...callGexArr, ...putGexArr);
    const maxY = Math.max(...netGexArr, ...callGexArr, ...putGexArr);

    const markerLevels = [
        { label: 'Target', value: model.target, color: '#ff454f', dash: 'dot' },
        { label: 'Spot', value: spotPrice, color: '#ffffff', dash: 'solid' },
        { label: 'Flip', value: model.flip, color: '#ff454f', dash: 'dash' },
        { label: 'Invalidation', value: model.invalidation, color: '#f5a524', dash: 'dash' }
    ].filter(level => Number.isFinite(level.value));

    const shapes = markerLevels.map(level => ({
        type: 'line',
        x0: level.value,
        x1: level.value,
        y0: 0,
        y1: 1,
        xref: 'x',
        yref: 'paper',
        line: { color: level.color, width: level.label === 'Spot' ? 2 : 1.5, dash: level.dash }
    }));

    const annotations = markerLevels.map(level => ({
        x: level.value,
        y: maxY,
        xref: 'x',
        yref: 'y',
        text: `${level.label}<br>${formatTargetPrice(level.value)}`,
        showarrow: false,
        yshift: 12,
        font: { color: level.color, size: 11, family: 'JetBrains Mono' }
    }));

    const traceCalls = {
        x: strikes,
        y: callGexArr,
        type: 'bar',
        name: 'Call GEX',
        marker: { color: '#ff8b1a' },
        opacity: 0.86
    };
    const tracePuts = {
        x: strikes,
        y: putGexArr,
        type: 'bar',
        name: 'Put GEX',
        marker: { color: '#2388e8' },
        opacity: 0.86
    };
    const traceLine = {
        x: strikes,
        y: netGexArr,
        type: 'scatter',
        mode: 'lines',
        name: 'Net GEX',
        line: { color: '#b177ff', width: 3, shape: 'spline' }
    };

    const layout = {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: '#96a3af', family: 'JetBrains Mono', size: 11 },
        margin: { t: 42, l: 62, r: 62, b: 48 },
        hovermode: 'x unified',
        barmode: 'relative',
        showlegend: false,
        xaxis: {
            title: 'Price',
            gridcolor: 'rgba(122,148,170,0.14)',
            zerolinecolor: 'rgba(255,255,255,0.25)',
            range: [Math.min(...strikes), Math.max(...strikes)]
        },
        yaxis: {
            title: 'GEX (Millions USD)',
            gridcolor: 'rgba(122,148,170,0.14)',
            zerolinecolor: 'rgba(255,255,255,0.35)',
            range: [minY * 1.18, maxY * 1.22]
        },
        shapes,
        annotations
    };

    Plotly.newPlot('cockpitProfileChart', [traceCalls, tracePuts, traceLine], layout, { displayModeBar: false, responsive: true });
}

function nearestAnySignificant(rows, spot, direction) {
    const maxAbs = rows.reduce((max, row) => Math.max(max, Math.abs(row.netGex)), 0);
    const threshold = maxAbs * 0.18;
    const candidates = rows
        .filter(row => direction === 1 ? row.strike > spot : row.strike < spot)
        .filter(row => Math.abs(row.netGex) >= threshold)
        .sort((a, b) => Math.abs(a.strike - spot) - Math.abs(b.strike - spot));
    return candidates[0] || null;
}

function fallbackTarget(spot, direction, expansion) {
    const movePct = expansion ? 0.012 : 0.006;
    return spot * (1 + (direction * movePct));
}

function choosePriceObjective(rows, spot, direction, expansion, flip) {
    if (!rows.length) return fallbackTarget(spot, direction, expansion);

    if (!expansion && flip > 0 && ((direction === 1 && flip > spot) || (direction === -1 && flip < spot))) {
        return flip;
    }

    const preferredSign = expansion ? -1 : 1;
    const preferred = nearestSignificant(rows, spot, direction === 1 ? 'above' : 'below', preferredSign);
    const anyLevel = preferred || nearestAnySignificant(rows, spot, direction);
    return anyLevel ? anyLevel.strike : fallbackTarget(spot, direction, expansion);
}

function chooseInvalidation(rows, spot, direction, flip) {
    const oppositeDirection = direction === 1 ? 'below' : 'above';
    const wall = nearestSignificant(rows, spot, oppositeDirection, 1) || nearestAnySignificant(rows, spot, -direction);
    if (wall) return wall.strike;

    if (flip > 0 && ((direction === 1 && flip < spot) || (direction === -1 && flip > spot))) {
        return flip;
    }

    return spot * (1 - (direction * 0.006));
}

function buildTradePlan(symbolData, overviewData, component, finalScore, marketVote) {
    if (!symbolData || symbolData.error || Math.abs(finalScore) < 0.22) {
        return {
            setupType: 'No Trade',
            target: '---',
            invalidation: '---',
            description: 'Bias is too mixed for a price objective.'
        };
    }

    const rows = buildStrikeProfile(symbolData.profile);
    const spot = symbolData.snapshot.spot_price;
    const localRows = rows.filter(row => Math.abs(row.strike - spot) / spot <= 0.02);
    const localNet = localRows.reduce((sum, row) => sum + row.netGex, 0);
    const fallbackFlip = estimateFlipFromProfileRows(rows);
    const flip = component && component.flip_strike ? component.flip_strike : fallbackFlip.strike;
    const marketVol = ((overviewData.compass_traders?.x_score || 0) + (overviewData.compass_whale?.x_score || 0)) / 2;
    const direction = finalScore > 0 ? 1 : -1;
    const isExpansion = localNet < 0 || marketVol < -0.15 || (Math.sign(marketVote.score) === direction && Math.abs(marketVote.score) > 0.65);
    const side = direction === 1 ? 'Call' : 'Put';
    const setupType = isExpansion
        ? `Expansion ${direction === 1 ? 'Up' : 'Down'} ${side}`
        : `Mean-Reversion ${side}`;
    const target = choosePriceObjective(rows, spot, direction, isExpansion, flip);
    const invalidation = chooseInvalidation(rows, spot, direction, flip);
    const description = isExpansion
        ? `Target follows open liquidity in the ${direction === 1 ? 'upside' : 'downside'} direction.`
        : `Target is a reversion move toward flip or the next stabilizing gamma level.`;

    return {
        setupType,
        target: formatTargetPrice(target),
        invalidation: formatTargetPrice(invalidation),
        description
    };
}

function renderActionOverview(overviewData, symbolData) {
    const titleEl = document.getElementById('actionBiasTitle');
    const contextEl = document.getElementById('actionBiasContext');
    const scoreEl = document.getElementById('actionBiasScore');
    if (!titleEl || !contextEl || !scoreEl) return;

    const selectedSymbol = symbolData && symbolData.snapshot ? symbolData.snapshot.symbol : document.getElementById('symbolSelector').value;
    const component = findComponent(overviewData, selectedSymbol);
    const marketVote = buildMarketVote(overviewData);
    const dealerVote = buildDealerVote(symbolData, component);
    const liquidityVote = buildLiquidityVote(symbolData);
    const rawScore = clamp((marketVote.score * 0.45) + (dealerVote.score * 0.35) + (liquidityVote.score * 0.20), -1, 1);
    const agreement = [marketVote.score, dealerVote.score, liquidityVote.score]
        .filter(score => Math.sign(score) === Math.sign(rawScore) && Math.abs(score) > 0.20).length;
    const confidencePenalty = component ? component.confidence || 0.5 : 0.45;
    const finalScore = rawScore * clamp(0.65 + (agreement * 0.12), 0.65, 1) * confidencePenalty;
    const absScore = Math.abs(finalScore);

    let title = 'WAIT / NO TRADE';
    if (absScore >= 0.55) title = finalScore > 0 ? 'STRONG CALL BIAS' : 'STRONG PUT BIAS';
    else if (absScore >= 0.22) title = finalScore > 0 ? 'CALL BIAS' : 'PUT BIAS';

    const conflictText = agreement < 2 ? 'Inputs are mixed; require price confirmation.' : 'Inputs are aligned enough for directional context.';
    const symbolText = selectedSymbol ? `${selectedSymbol}: ` : '';
    const actionClass = finalScore > 0.22 ? 'action-call' : finalScore < -0.22 ? 'action-put' : 'action-wait';
    const plan = buildTradePlan(symbolData, overviewData, component, finalScore, marketVote);
    contextEl.innerText = `${symbolText}${conflictText} ${plan.description}`;
    titleEl.innerText = title;
    titleEl.className = actionClass;
    scoreEl.innerText = `${Math.round(finalScore * 100)}%`;
    scoreEl.className = `action-score ${actionClass}`;

    setVote('voteMarket', 'voteMarketDetail', marketVote);
    setVote('voteDealer', 'voteDealerDetail', dealerVote);
    setVote('voteLiquidity', 'voteLiquidityDetail', liquidityVote);

    const setupEl = document.getElementById('actionSetupType');
    const targetEl = document.getElementById('actionTargetPrice');
    const invalidationEl = document.getElementById('actionInvalidation');
    if (setupEl) setupEl.innerText = plan.setupType;
    if (targetEl) targetEl.innerText = plan.target;
    if (invalidationEl) invalidationEl.innerText = plan.invalidation;
}

function renderCompass(compassData, type) {
    // type: 'Traders' or 'Whale'
    if (!compassData || !compassData.label) return;

    // 0. Update Tooltip
    // Show composition + default explanation
    const container = document.getElementById(`compass${type}`);
    if (container) {
        const baseTooltip = "X-Axis = normalized net-vs-gross gamma imbalance. Y-Axis = spot vs estimated flip. Confidence falls when data is stale, thin, or approximate.";
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
    descEl.innerText = `${compassData.strategy} Confidence: ${formatPct(compassData.confidence)} ${compassData.confidence_label || ''}${warnings}`;

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
        const confidence = formatPct(comp.confidence);
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
                <span>Quality ${confidence}</span>
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
            title: 'Effective GEX ($ per 1% move)'
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
            `${p.symbol} Magnet Moved: ${p.old_magnet.toFixed(0)} -> ${p.new_magnet.toFixed(0)}`,
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
        body = `${event.payload.symbol}: ${event.payload.old_magnet.toFixed(0)} -> ${event.payload.new_magnet.toFixed(0)}`;
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
        <div class="toast-close" onclick="this.parentElement.remove()">x</div>
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
