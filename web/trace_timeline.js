(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory;
    } else {
        root.traceTimelineRange = factory;
    }
}(typeof globalThis !== 'undefined' ? globalThis : this, function traceTimelineRange(
    buckets,
    requestedIndex,
    requestedWindowSize = null
) {
    const values = Array.isArray(buckets) ? buckets : [];
    if (!values.length) {
        return {
            index: 0,
            startIndex: 0,
            endIndex: 0,
            startValue: null,
            endValue: null,
            label: '--:--',
            positionPct: 0
        };
    }

    const maxIndex = values.length - 1;
    const numericIndex = Number(requestedIndex);
    const index = Math.max(0, Math.min(
        Number.isFinite(numericIndex) ? Math.round(numericIndex) : maxIndex,
        maxIndex
    ));
    const defaultWindowSize = Math.min(60, Math.max(18, Math.ceil(values.length * 0.25)));
    const numericWindowSize = requestedWindowSize == null ? NaN : Number(requestedWindowSize);
    const windowSize = Math.max(1, Math.min(
        Number.isFinite(numericWindowSize) ? Math.round(numericWindowSize) : defaultWindowSize,
        values.length
    ));
    let startIndex = Math.max(0, index - windowSize + 1);
    let endIndex = Math.min(maxIndex, startIndex + windowSize - 1);
    if (endIndex - startIndex + 1 < windowSize) {
        startIndex = Math.max(0, endIndex - windowSize + 1);
    }
    const value = String(values[index] || '');

    return {
        index,
        startIndex,
        endIndex,
        startValue: values[startIndex],
        endValue: values[endIndex],
        label: value.length >= 16 ? value.slice(11, 16) : value,
        positionPct: maxIndex > 0 ? (index / maxIndex) * 100 : 0
    };
}));
