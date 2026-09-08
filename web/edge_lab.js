(function (root, factory) {
    const api = factory();
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.EdgeLab = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
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
        elements.table.innerHTML = model.opportunities.length ? `<table class="analysis-table"><thead><tr><th>Date</th><th>Scenario</th><th>Outcome</th><th>After cost</th><th>MFE</th><th>MAE</th></tr></thead><tbody>${model.opportunities.map(row => `<tr><td>${row.session_date}</td><td>${row.scenario_type || '--'}</td><td>${row.outcome}</td><td>${row.after_cost_points == null ? '--' : row.after_cost_points.toFixed(2)}</td><td>${row.mfe ?? '--'}</td><td>${row.mae ?? '--'}</td></tr>`).join('')}</tbody></table>` : `<p class="muted">${model.warning}</p>`;
        if (echartsApi && elements.chart) {
            const chart = echartsApi.getInstanceByDom(elements.chart) || echartsApi.init(elements.chart);
            chart.setOption({backgroundColor:'transparent',textStyle:{color:'#96a3af'},xAxis:{type:'value'},yAxis:{type:'value',show:false},series:[{type:'bar',data:model.distribution.map((value,index)=>[value,index]),itemStyle:{color:'#18c7b7'}}]});
        }
        return model;
    }
    return { viewModel, render };
});
