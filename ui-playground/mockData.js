const mockData = {
    settings: {
        refresh_interval: 60,
        theme: 'dark',
        symbols: ['SPY', 'QQQ', 'IWM', 'BTC'],
        backend_update_delay: 180
    },
    symbols: ['SPY', 'QQQ', 'IWM', 'BTC'],
    dashboardData: {
        'SPY': {
            snapshot: {
                spot_price: 472.50,
                total_net_gex: -120000000,
                timestamp: new Date().toISOString()
            },
            profile: [
                { strike_price: 460, option_type: 'CALL', gex_value: 1000000, open_interest: 5000 },
                { strike_price: 460, option_type: 'PUT', gex_value: -5000000, open_interest: 12000 },
                { strike_price: 470, option_type: 'CALL', gex_value: 15000000, open_interest: 25000 },
                { strike_price: 470, option_type: 'PUT', gex_value: -8000000, open_interest: 18000 },
                { strike_price: 472, option_type: 'CALL', gex_value: 2000000, open_interest: 8000 },
                { strike_price: 472, option_type: 'PUT', gex_value: -15000000, open_interest: 20000 },
                { strike_price: 475, option_type: 'CALL', gex_value: 12000000, open_interest: 15000 },
                { strike_price: 475, option_type: 'PUT', gex_value: -2000000, open_interest: 5000 },
                { strike_price: 480, option_type: 'CALL', gex_value: 8000000, open_interest: 10000 },
                { strike_price: 480, option_type: 'PUT', gex_value: -1000000, open_interest: 2000 }
            ],
            history: [
                { timestamp: '2025-12-18T20:00:00Z', total_net_gex: -100000000 },
                { timestamp: '2025-12-18T21:00:00Z', total_net_gex: -110000000 },
                { timestamp: '2025-12-18T22:00:00Z', total_net_gex: -120000000 }
            ]
        }
    },
    marketOverview: {
        compass: {
            x_score: -0.4,
            y_score: -0.6,
            label: "CRASH / FLUSH",
            strategy: "Buy Puts / Sell Rips. Do not fade."
        },
        components: [
            { symbol: 'SPY', distance_pct: -0.5, flip_strike: 475, spot: 472 },
            { symbol: 'QQQ', distance_pct: 1.2, flip_strike: 400, spot: 405 },
            { symbol: 'IWM', distance_pct: -2.1, flip_strike: 200, spot: 195 }
        ],
        tilt: [
            { symbol: 'SPY', net_gex: -250000000 },
            { symbol: 'QQQ', net_gex: -180000000 },
            { symbol: 'IWM', net_gex: 50000000 }
        ]
    }
};

// Fill in other symbols with basic clones for testing
mockData.symbols.forEach(sym => {
    if (!mockData.dashboardData[sym]) {
        mockData.dashboardData[sym] = JSON.parse(JSON.stringify(mockData.dashboardData['SPY']));
    }
});
