// --- Eel Mock ---
const eel = {
    get_settings: () => () => Promise.resolve(mockData.settings),
    get_symbols: () => () => Promise.resolve(mockData.symbols),
    trigger_data_refresh: () => () => Promise.resolve(),
    get_dashboard_data: (symbol) => () => Promise.resolve(mockData.dashboardData[symbol] || mockData.dashboardData['SPY']),
    get_market_overview: () => () => Promise.resolve(mockData.marketOverview),
    save_settings: (settings) => () => {
        console.log("Settings saved:", settings);
        mockData.settings = settings;
        return Promise.resolve();
    },
    expose: (fn) => { console.log("Exposed function:", fn.name); }
};

let refreshTimer = null;
let currentSettings = { refresh_interval: 60, theme: 'dark', symbols: [], backend_update_delay: 180 };
let cachedData = null;
let chartRange = 'near'; // 'near' | 'wide' | 'full'

// --- Helper: Get CSS Var ---
function getCssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function getHslColor(name, alpha = 1) {
    const val = getCssVar(name);
    if (!val) return '#fff';
    const [h, s, l] = val.split(' ');
    if (!s || !l) return val;
    return `hsla(${h}, ${s}, ${l}, ${alpha})`;
}

// --- Color Utils ---
function hexToHsl(hex) {
    let c = hex.substring(1).split('');
    if (c.length === 3) c = [c[0], c[0], c[1], c[1], c[2], c[2]];
    c = '0x' + c.join('');
    let r = (c >> 16) & 255, g = (c >> 8) & 255, b = c & 255;
    r /= 255; g /= 255; b /= 255;
    let max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h, s, l = (max + min) / 2;

    if (max == min) { h = s = 0; }
    else {
        let d = max - min;
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        switch (max) {
            case r: h = (g - b) / d + (g < b ? 6 : 0); break;
            case g: h = (b - r) / d + 2; break;
            case b: h = (r - g) / d + 4; break;
        }
        h /= 6;
    }
    return `${Math.round(h * 360)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%`;
}

function hslToHex(hslStr) {
    if (!hslStr) return "#000000";
    let [h, s, l] = hslStr.replace(/%/g, '').split(' ').map(Number);
    l /= 100; const a = s * Math.min(l, 1 - l) / 100;
    const f = n => {
        const k = (n + h / 30) % 12;
        const color = l - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
        return Math.round(255 * color).toString(16).padStart(2, '0');
    };
    return `#${f(0)}${f(8)}${f(4)}`;
}

function applyTheme(colors) {
    const root = document.documentElement;
    if (colors.primary) root.style.setProperty('--primary', colors.primary);
    if (colors.secondary) root.style.setProperty('--secondary', colors.secondary);
    if (colors.background) {
        root.style.setProperty('--background', colors.background);
        // Auto-adjust generic darker shades based on background (optional, but keeps consistency)
        // For now, we trust the background color picker or just let --card inherit distinct opacity if needed
    }
    if (colors.up) root.style.setProperty('--color-up', colors.up);
    if (colors.down) root.style.setProperty('--color-down', colors.down);
}

// --- Init ---
async function init() {
    console.log("Initializing Avant-Garde Playground...");
    currentSettings = await eel.get_settings()();

    // Config Inputs
    const intervalInput = document.getElementById('settingInterval');
    if (intervalInput) intervalInput.value = currentSettings.refresh_interval;

    const symbolsInput = document.getElementById('settingSymbols');
    if (symbolsInput) symbolsInput.value = (currentSettings.symbols || []).join(',');

    // Theme Config Inputs
    if (currentSettings.theme_colors) {
        applyTheme(currentSettings.theme_colors);
    }

    // Populate Color Pickers
    const setPicker = (id, varName) => {
        const el = document.getElementById(id);
        if (el) el.value = hslToHex(getCssVar(varName));
    };

    // Defer slightly to ensure CSS variables are computed
    setTimeout(() => {
        setPicker('colorBg', '--background');
        setPicker('colorPrimary', '--primary');
        setPicker('colorSecondary', '--secondary');
        setPicker('colorUp', '--color-up');
        setPicker('colorDown', '--color-down');
    }, 100);

    const lastUpdateEl = document.getElementById('lastUpdate');
    if (lastUpdateEl) lastUpdateEl.innerText = "Syncing...";

    await eel.trigger_data_refresh()();
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

    // Set default view
    switchView('overview');

    // Start random event simulation
    startMockEventGenerator();
}

function switchView(viewName) {
    // 1. Hide all View Sections
    document.querySelectorAll('.view-section').forEach(el => el.classList.add('hidden'));

    // 2. Show Target View
    const targetView = document.getElementById(`view-${viewName}`);
    if (targetView) targetView.classList.remove('hidden');

    // 3. Update Sidebar Navigation State
    const navItems = document.querySelectorAll('.nav-btn');
    navItems.forEach(btn => btn.classList.remove('active'));

    const activeBtn = document.getElementById(`nav-${viewName}`);
    if (activeBtn) activeBtn.classList.add('active');

    // 4. Trigger Specific Loaders & Resizers
    const main = document.querySelector('main');
    if (main) main.scrollTo({ top: 0, behavior: 'smooth' });

    // Handle Chart Resizing
    if (viewName === 'overview') {
        loadOverview();
        requestAnimationFrame(() => Plotly.Plots.resize('tiltChart'));
    } else if (viewName === 'dashboard') {
        if (cachedData) {
            renderProfileChart(cachedData.profile, cachedData.snapshot.spot_price);
            requestAnimationFrame(() => {
                Plotly.Plots.resize('profileChart');
                Plotly.Plots.resize('historyChart');
            });
        }
    } else if (viewName === 'analysis') {
        if (cachedData) renderAnalysisTable(cachedData);
    }
}

async function loadSymbol() {
    const symbol = document.getElementById('symbolSelector').value;
    const data = await eel.get_dashboard_data(symbol)();
    if (data.error) { console.error(data.error); return; }

    cachedData = data;
    renderDashboard(data);
    renderAnalysisTable(data);

    showToast("Data Loaded", `Successfully synced ${symbol}`);
}

function renderDashboard(data) {
    // Calculate Max Limits
    let maxNetPos = { val: 0, strike: 0 };
    let maxNetNeg = { val: 0, strike: 0 };

    data.profile.forEach(row => {
        if (row.gex_value > maxNetPos.val) maxNetPos = { val: row.gex_value, strike: row.strike_price };
        if (row.gex_value < maxNetNeg.val) maxNetNeg = { val: row.gex_value, strike: row.strike_price };
    });

    updateKPIs(data.snapshot, maxNetPos.strike, maxNetNeg.strike);
    renderProfileChart(data.profile, data.snapshot.spot_price);
    renderHistoryChart(data.history);
}

function updateKPIs(snap, lowVolStrike, highVolStrike) {
    document.getElementById('kpiSpot').innerText = `$${snap.spot_price.toFixed(2)}`;

    const netGexM = snap.total_net_gex / 1000000;
    const regimeBar = document.getElementById('regimeBar');
    const regimeText = document.getElementById('regimeText');

    let pct = 50 + (netGexM / 1000) * 50;
    pct = Math.min(Math.max(pct, 5), 95);

    if (regimeBar) regimeBar.style.width = `${pct}%`;

    if (netGexM > 0) {
        regimeText.innerText = `$${netGexM.toFixed(0)}M`;
        regimeText.className = "text-2xl font-mono font-bold text-emerald-400 glow-up";
        if (regimeBar) regimeBar.className = "h-full bg-emerald-500 shadow-[0_0_15px_currentColor]";
    } else {
        regimeText.innerText = `-$${Math.abs(netGexM).toFixed(0)}M`;
        regimeText.className = "text-2xl font-mono font-bold text-red-400 glow-down";
        if (regimeBar) regimeBar.className = "h-full bg-red-500 shadow-[0_0_15px_currentColor]";
    }

    document.getElementById('kpiLowVol').innerText = lowVolStrike > 0 ? lowVolStrike.toFixed(0) : '---';
    document.getElementById('kpiHighVol').innerText = highVolStrike > 0 ? highVolStrike.toFixed(0) : '---';

    const dateObj = new Date(snap.timestamp);
    const lastUpdateEl = document.getElementById('lastUpdate');
    if (lastUpdateEl) lastUpdateEl.innerText = dateObj.toLocaleTimeString();
}

function updateChartRange(mode) {
    chartRange = mode;
    ['near', 'wide', 'full'].forEach(m => {
        const btn = document.getElementById(`btn-range-${m}`);
        if (btn) {
            if (m === mode) {
                btn.className = "px-3 py-1 text-[10px] rounded-md transition-all bg-white text-black font-bold shadow-[0_0_10px_white]";
            } else {
                btn.className = "px-3 py-1 text-[10px] rounded-md transition-all text-muted-foreground hover:text-white bg-black/20";
            }
        }
    });
    if (cachedData) renderProfileChart(cachedData.profile, cachedData.snapshot.spot_price);
}

// --- PLOTLY CONFIG HELPER ---
function getCommonLayout() {
    const gridColor = 'rgba(255,255,255,0.03)';
    return {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: getHslColor('--muted-foreground'), family: 'JetBrains Mono', size: 10 },
        margin: { t: 10, l: 40, r: 20, b: 30 },
        hovermode: 'x unified',
        dragmode: 'pan',
        xaxis: { gridcolor: gridColor, zeroline: false },
        yaxis: { gridcolor: gridColor, zeroline: false },
        showlegend: false
    };
}

function renderProfileChart(profileData, spotPrice) {
    let strikeMap = {};
    profileData.forEach(row => {
        if (!strikeMap[row.strike_price]) strikeMap[row.strike_price] = { call: 0, put: 0, net: 0 };
        if (row.option_type === 'CALL') strikeMap[row.strike_price].call += row.gex_value;
        else strikeMap[row.strike_price].put += row.gex_value;
        strikeMap[row.strike_price].net += row.gex_value;
    });

    let strikes = Object.keys(strikeMap).map(parseFloat).sort((a, b) => a - b);

    if (chartRange !== 'full') {
        const pct = chartRange === 'near' ? 0.03 : 0.15;
        const minStrike = spotPrice * (1 - pct);
        const maxStrike = spotPrice * (1 + pct);
        strikes = strikes.filter(s => s >= minStrike && s <= maxStrike);
    }

    const netGexArr = strikes.map(s => strikeMap[s].net);
    const callGexArr = strikes.map(s => strikeMap[s].call);
    const putGexArr = strikes.map(s => strikeMap[s].put);

    const colorNet = getHslColor('--primary');
    const colorCall = getHslColor('--color-up', 0.8);
    const colorPut = getHslColor('--color-down', 0.8);

    const traceCalls = {
        x: strikes, y: callGexArr, type: 'bar', name: 'Calls',
        marker: { color: colorCall, line: { width: 0 } },
        opacity: 0.6,
        hovertemplate: 'Calls: $%{y:.2s}<extra></extra>'
    };

    const tracePuts = {
        x: strikes, y: putGexArr, type: 'bar', name: 'Puts',
        marker: { color: colorPut, line: { width: 0 } },
        opacity: 0.6,
        hovertemplate: 'Puts: $%{y:.2s}<extra></extra>'
    };

    const traceLine = {
        x: strikes, y: netGexArr, type: 'scatter', mode: 'lines', name: 'Net GEX',
        line: { color: 'white', width: 2, shape: 'spline', smoothing: 1.3 },
        fill: 'tozeroy',
        fillcolor: 'rgba(255,255,255,0.02)',
        hovertemplate: '<b>Net: $%{y:.2s}</b><extra></extra>'
    };

    const layout = {
        ...getCommonLayout(),
        barmode: 'relative',
        yaxis2: { overlaying: 'y', showgrid: false, zeroline: false, showticklabels: false },
        shapes: [{
            type: 'line', x0: spotPrice, x1: spotPrice, y0: 0, y1: 1, xref: 'x', yref: 'paper',
            line: { color: '#ffffff', width: 1, dash: 'dot' }
        }]
    };

    Plotly.newPlot('profileChart', [traceCalls, tracePuts, traceLine], layout, { displayModeBar: false, responsive: true });
}

function renderHistoryChart(history) {
    if (!history || history.length === 0) return;

    const traceGex = {
        x: history.map(d => d.timestamp), y: history.map(d => d.total_net_gex),
        type: 'scatter', mode: 'lines', fill: 'tozeroy', name: 'Net GEX',
        line: { color: getHslColor('--secondary'), width: 2 },
        fillcolor: getHslColor('--secondary', 0.05)
    };

    const layout = getCommonLayout();
    Plotly.newPlot('historyChart', [traceGex], layout, { displayModeBar: false, responsive: true });
}

function renderAnalysisTable(data) {
    const tbody = document.getElementById('analysisTableBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    const spotPrice = data.snapshot.spot_price;
    let strikes = {};
    let maxGexAbs = 0;

    data.profile.forEach(row => {
        const s = row.strike_price;
        if (!strikes[s]) strikes[s] = { strike: s, callGex: 0, putGex: 0, callOI: 0, putOI: 0 };
        if (row.option_type === 'CALL') { strikes[s].callGex = row.gex_value; strikes[s].callOI = row.open_interest; }
        else { strikes[s].putGex = row.gex_value; strikes[s].putOI = row.open_interest; }
    });

    let sortedStrikes = Object.values(strikes).sort((a, b) => a.strike - b.strike);
    sortedStrikes.forEach(row => { row.netGex = row.callGex + row.putGex; maxGexAbs = Math.max(maxGexAbs, Math.abs(row.callGex), Math.abs(row.putGex)); });
    let maxNetGexAbs = Math.max(...sortedStrikes.map(r => Math.abs(r.netGex)));

    sortedStrikes.forEach(row => {
        if (row.callOI + row.putOI < 10 && Math.abs(row.netGex) < 100000) return;
        const isATM = Math.abs(row.strike - spotPrice) / spotPrice < 0.001;

        const tr = document.createElement('tr');
        tr.className = `hover:bg-white/5 transition-colors border-b border-white/5 last:border-0 ${isATM ? 'bg-primary/10' : ''}`;

        const isMagnet = Math.abs(row.netGex) === maxNetGexAbs;
        let badgeHtml = isMagnet
            ? `<span class="inline-flex items-center rounded-md border border-orange-500/50 bg-orange-500/10 px-2 py-1 text-[10px] font-semibold text-orange-400">🔥 MAGNET</span>`
            : (row.netGex > 0
                ? `<span class="inline-flex items-center rounded-md border border-emerald-500/50 bg-emerald-500/10 px-2 py-1 text-[10px] font-semibold text-emerald-400">SUPPORT</span>`
                : `<span class="inline-flex items-center rounded-md border border-red-500/50 bg-red-500/10 px-2 py-1 text-[10px] font-semibold text-red-500">RESIST</span>`);

        const callWidth = (Math.abs(row.callGex) / maxGexAbs) * 100;
        const putWidth = (Math.abs(row.putGex) / maxGexAbs) * 100;
        const netColor = row.netGex > 0 ? 'text-emerald-400' : 'text-red-400';

        tr.innerHTML = `
            <td class="px-6 py-4 font-mono font-medium ${isATM ? 'text-white' : 'text-muted-foreground'}">${row.strike.toFixed(0)} ${isATM ? '<span class="ml-2 text-[10px] text-black bg-white px-1 rounded font-bold">ATM</span>' : ''}</td>
            <td class="px-6 py-4">${badgeHtml}</td>
            <td class="px-6 py-4 font-mono ${netColor}">$${(row.netGex / 1000000).toFixed(2)}M</td>
            <td class="px-6 py-4"><div class="flex items-center gap-2"><span class="text-[10px] text-muted-foreground w-8 text-right">${(row.callGex / 1000000).toFixed(1)}</span><div class="h-1.5 rounded-full bg-emerald-500/50 shadow-[0_0_5px_rgba(52,211,153,0.3)]" style="width: ${callWidth}%"></div></div></td>
            <td class="px-6 py-4"><div class="flex items-center gap-2"><div class="h-1.5 rounded-full bg-red-500/50 ml-auto shadow-[0_0_5px_rgba(248,113,113,0.3)]" style="width: ${putWidth}%"></div><span class="text-[10px] text-muted-foreground w-8">${(Math.abs(row.putGex) / 1000000).toFixed(1)}</span></div></td>
            <td class="px-6 py-4 text-right font-mono text-muted-foreground">${(row.callOI + row.putOI).toLocaleString()}</td>
        `;
        tbody.appendChild(tr);
    });
}

function startTimers() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(async () => {
        await eel.trigger_data_refresh()();
        loadSymbol();
    }, currentSettings.refresh_interval * 1000);
}

function saveSettings() {
    const newInterval = parseInt(document.getElementById('settingInterval').value);
    const newSymbols = document.getElementById('settingSymbols').value.split(',').map(s => s.trim()).filter(Boolean);

    // Capture Theme Colors
    const themeColors = {
        background: hexToHsl(document.getElementById('colorBg').value),
        primary: hexToHsl(document.getElementById('colorPrimary').value),
        secondary: hexToHsl(document.getElementById('colorSecondary').value),
        up: hexToHsl(document.getElementById('colorUp').value),
        down: hexToHsl(document.getElementById('colorDown').value),
    };

    // Apply immediately
    applyTheme(themeColors);

    // Save
    eel.save_settings({
        refresh_interval: newInterval,
        theme: 'custom',
        symbols: newSymbols,
        theme_colors: themeColors
    })();

    currentSettings.refresh_interval = newInterval;
    currentSettings.theme_colors = themeColors;

    showToast("Configuration Saved", "Settings and Theme updated successfully.");
    startTimers();

    // Force chart re-render with new colors
    setTimeout(() => {
        if (cachedData) renderDashboard(cachedData);
    }, 200);
}

// --- Overview ---
async function loadOverview() {
    const data = await eel.get_market_overview()();
    if (data.error) { console.error(data.error); return; }
    renderSignalDashboard(data);
}

function renderSignalDashboard(data) {
    if (data.compass) renderCompass(data.compass);
    if (data.components) renderPillars(data.components);
    if (data.tilt) renderTiltChart(data.tilt);
}

function renderCompass(compassData) {
    const titleEl = document.getElementById('signalTitle');
    titleEl.innerText = compassData.label;

    // Update Puck Position
    const xPct = 50 + (compassData.x_score * 35);
    const yPct = 50 - (compassData.y_score * 35);
    const puck = document.getElementById('compassPuck');
    puck.style.left = `${xPct}%`;
    puck.style.top = `${yPct}%`;
}

function renderPillars(components) {
    const container = document.getElementById('pillarsContainer');
    container.innerHTML = '';
    components.forEach(comp => {
        let pct = comp.distance_pct;
        const isPos = pct >= 0;
        let barWidth = Math.min((Math.abs(pct) / 3.0) * 50, 50);

        const el = document.createElement('div');
        el.className = "flex flex-col gap-2 p-4 rounded-xl border border-white/5 bg-black/40 hover:bg-white/5 transition-colors";
        el.innerHTML = `
            <div class="flex justify-between items-center text-sm mb-1"><span class="font-bold text-white">${comp.symbol}</span><span class="font-mono ${isPos ? 'text-emerald-400' : 'text-red-400'}">${pct.toFixed(2)}%</span></div>
            <div class="relative h-2 bg-white/5 rounded-full overflow-hidden w-full"><div class="absolute left-1/2 top-0 bottom-0 w-px bg-white/20 z-10"></div><div class="absolute h-full ${isPos ? 'bg-emerald-500 shadow-[0_0_10px_#10b981]' : 'bg-red-500 shadow-[0_0_10px_#ef4444]'} transition-all duration-500" style="${isPos ? 'left: 50%;' : 'right: 50%;'} width: ${barWidth}%"></div></div>
            <div class="text-[10px] text-muted-foreground flex justify-between mt-1"><span>$${parseInt(comp.spot)}</span><span>Flip: ${parseInt(comp.flip_strike)}</span></div>
        `;
        container.appendChild(el);
    });
}

function renderTiltChart(tiltData) {
    const symbols = tiltData.map(d => d.symbol);
    const vals = tiltData.map(d => d.net_gex);
    const colors = vals.map(v => v >= 0 ? getHslColor('--color-up') : getHslColor('--color-down'));

    const trace = {
        x: symbols, y: vals, type: 'bar',
        marker: { color: colors, line: { width: 0 } },
        text: vals.map(v => `$${(v / 1000000).toFixed(1)}M`),
        textposition: 'auto', hoverinfo: 'none'
    };

    const layout = getCommonLayout();
    layout.margin = { t: 5, l: 30, r: 10, b: 20 };
    layout.yaxis.showticklabels = false;

    requestAnimationFrame(() => {
        Plotly.newPlot('tiltChart', [trace], layout, { displayModeBar: false, responsive: true });
    });
}

// --- Event Handling ---
eel.expose(handle_backend_event);
function handle_backend_event(event) {
    addNotificationToPanel(event);
    if (event.type === 'magnet_change') {
        showToast("Magnet Shift", `${event.payload.symbol} moved ${event.payload.old_magnet.toFixed(0)} -> ${event.payload.new_magnet.toFixed(0)}`);
    }
}

function addNotificationToPanel(event) {
    const feed = document.getElementById('activityFeed');
    if (!feed) return;
    if (feed.innerText.includes('Listening')) feed.innerHTML = '';

    const div = document.createElement('div');
    const ts = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    let title = "System Event", desc = "Unknown event type", icon = "🔷";

    if (event.type === 'magnet_change') { title = "Magnet Changed"; desc = `${event.payload.symbol} shifted to ${event.payload.new_magnet.toFixed(0)}`; icon = "🔥"; }
    else if (event.type === 'data_refresh') { title = "Data Refresh"; desc = `Market data updated for ${event.payload.symbol}`; icon = `✨`; }

    div.className = "flex flex-col gap-1 p-4 rounded-xl border-l-2 border-primary bg-white/5 animate-pulse-soft mb-3 hover:bg-white/10 transition-colors";
    div.innerHTML = `
        <div class="flex justify-between items-start"><span class="font-semibold text-xs flex items-center gap-2 text-white"><span>${icon}</span>${title}</span><span class="text-[10px] text-muted-foreground">${ts}</span></div>
        <p class="text-xs text-muted-foreground ml-6">${desc}</p>
    `;
    feed.insertBefore(div, feed.firstChild);
    if (feed.children.length > 20) feed.lastChild.remove();
}

function showToast(title, message) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = "pointer-events-auto relative flex w-full items-center justify-between space-x-4 overflow-hidden rounded-xl border border-white/10 p-4 pr-6 shadow-2xl transition-all bg-black/90 text-foreground backdrop-blur-xl animate-bounce-in";
    toast.innerHTML = `
        <div class="grid gap-1">
            <div class="text-sm font-semibold text-white">${title}</div>
            <div class="text-xs opacity-90 text-muted-foreground">${message}</div>
        </div>
        <button onclick="this.parentElement.remove()" class="absolute right-2 top-2 rounded-md p-1 text-white/50 hover:text-white">
            <svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
    `;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function startMockEventGenerator() {
    setInterval(() => {
        const types = ['data_refresh', 'magnet_change'];
        const type = types[Math.floor(Math.random() * types.length)];
        const symbol = mockData.symbols[Math.floor(Math.random() * mockData.symbols.length)];
        let event = { type: type, payload: { symbol: symbol } };
        if (type === 'magnet_change') { event.payload.old_magnet = 470 + Math.random() * 10; event.payload.new_magnet = 470 + Math.random() * 10; }
        handle_backend_event(event);
    }, 15000);
}

window.addEventListener('DOMContentLoaded', init);
window.addEventListener('resize', () => {
    // Debounced global resize if needed
    Plotly.Plots.resize('tiltChart');
    Plotly.Plots.resize('profileChart');
    Plotly.Plots.resize('historyChart');
});
