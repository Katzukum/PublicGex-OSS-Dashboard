(function (root, factory) {
    const api = factory();
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.EdgeLab = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    function histogram(values) {
        const clean = values.filter(value => value != null && Number.isFinite(Number(value))).map(Number);
        if (!clean.length) return [];
        const low = Math.min(...clean), high = Math.max(...clean);
        const count = Math.min(12, Math.max(1, Math.ceil(Math.sqrt(clean.length))));
        const width = high === low ? 1 : (high - low) / count;
        const bins = Array.from({length: count}, (_, index) => ({label: (low + (index + .5) * width).toFixed(2), count: 0}));
        clean.forEach(value => bins[Math.min(count - 1, Math.floor((value - low) / width))].count++);
        return bins;
    }
    function viewModel(payload) {
        const stats = payload?.stats || {};
        const count = Number(stats.independent_opportunities || 0);
        const days = Number(stats.unique_days || 0);
        const status = payload?.status || 'INSUFFICIENT';
        return {
            status,
            count,
            days,
            headline: status === 'CALIBRATED' ? 'Calibrated historical edge' : status === 'EMERGING' ? 'Emerging evidence' : 'Insufficient evidence',
            expectancy: stats.after_cost_expectancy,
            holdout: stats.holdout_expectancy,
            warning: payload?.warning || (count ? '' : 'No independent opportunities match these filters.'),
            opportunities: payload?.opportunities || [],
            distribution: payload?.expectancy_distribution || [],
        };
    }

    function render(payload, elements, echartsApi) {
        const model = viewModel(payload);
        elements.status.textContent = `${model.headline} · n=${model.count} · ${model.days} unique days`;
        elements.summary.innerHTML = `
            <article class="edge-stat"><span>Status</span><strong>${model.status}</strong><em>not a live prediction</em></article>
            <article class="edge-stat"><span>After-cost expectancy</span><strong>${model.expectancy == null ? '--' : model.expectancy.toFixed(2) + ' pts'}</strong><em>n=${model.count} / ${model.days} days</em></article>
            <article class="edge-stat"><span>Holdout</span><strong>${model.holdout == null ? '--' : model.holdout.toFixed(2) + ' pts'}</strong><em>walk-forward tail</em></article>`;
        elements.table.innerHTML = model.opportunities.length ? `<table class="analysis-table"><thead><tr><th>Date</th><th>Scenario</th><th>Outcome</th><th>After cost</th><th>MFE</th><th>MAE</th></tr></thead><tbody>${model.opportunities.map(row => `<tr><td>${row.session_date}</td><td>${row.scenario_type || '--'}</td><td>${row.outcome}</td><td>${row.after_cost_points == null ? '--' : row.after_cost_points.toFixed(2)}</td><td>${row.mfe == null ? '--' : Number(row.mfe).toFixed(2)}</td><td>${row.mae == null ? '--' : Number(row.mae).toFixed(2)}</td></tr>`).join('')}</tbody></table>` : `<p class="muted">${model.warning}</p>`;
        if (echartsApi && elements.chart) {
            const chart = echartsApi.getInstanceByDom(elements.chart) || echartsApi.init(elements.chart);
            const bins = histogram(model.distribution);
            const muted = typeof getComputedStyle === 'function' ? getComputedStyle(elements.chart).getPropertyValue('--muted').trim() : '#96a3af';
            chart.setOption({
                animation: false, backgroundColor: 'transparent', textStyle: {color: muted},
                grid: {left: 60, right: 30, top: 36, bottom: 50},
                tooltip: {trigger: 'axis'},
                xAxis: {show: bins.length > 0, type: 'category', data: bins.map(bin => bin.label), name: 'After-cost points', nameLocation:'middle', nameGap:30, axisLabel:{color:muted}},
                yAxis: {show: bins.length > 0, type: 'value', minInterval:1, name:'Opportunities', axisLabel:{color:muted}},
                graphic: bins.length ? [] : [{type:'text',left:'center',top:'middle',style:{text:'No completed opportunities yet\nThe distribution appears as outcomes are recorded.',fill:muted,fontSize:13,lineHeight:24,textAlign:'center'}}],
                series: [{type:'bar',data:bins.map(bin => bin.count),itemStyle:{color:'#18a99d',borderRadius:[4,4,0,0]},barMaxWidth:50}],
            }, true);
            elements.chart.setAttribute('aria-label', bins.length ? 'Distribution of after-cost outcomes' : 'No completed opportunities to chart');
        }
        return model;
    }
    return { viewModel, render, histogram };
});
