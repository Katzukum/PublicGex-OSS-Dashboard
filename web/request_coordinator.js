(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory;
    } else {
        root.createRequestCoordinator = factory;
    }
}(typeof globalThis !== 'undefined' ? globalThis : this, function createRequestCoordinator({ timeoutMs = 20000 } = {}) {
    let generation = 0;
    let activeKey = null;
    let inFlight = null;

    function isCurrent(result, selectedKey = result?.key) {
        return Boolean(
            result
            && result.generation === generation
            && result.key === activeKey
            && result.key === selectedKey
        );
    }

    function request(key, loader) {
        const normalizedKey = String(key || '');
        if (inFlight && inFlight.key === normalizedKey && activeKey === normalizedKey) {
            return inFlight.promise;
        }

        generation += 1;
        activeKey = normalizedKey;
        const requestGeneration = generation;
        let timer;
        const timeout = new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error('Request timed out. Please retry.')), timeoutMs);
        });
        const promise = Promise.race([Promise.resolve().then(loader), timeout])
            .then(
                value => ({ key: normalizedKey, generation: requestGeneration, value, error: null }),
                error => ({ key: normalizedKey, generation: requestGeneration, value: null, error })
            )
            .finally(() => {
                clearTimeout(timer);
                if (inFlight && inFlight.generation === requestGeneration) {
                    inFlight = null;
                }
            });

        inFlight = { key: normalizedKey, generation: requestGeneration, promise };
        return promise;
    }

    function invalidate() {
        generation += 1;
        activeKey = null;
        inFlight = null;
    }

    return { request, isCurrent, invalidate };
}));
