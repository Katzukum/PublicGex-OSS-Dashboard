(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory;
    else root.createBridgeClient = factory;
}(typeof globalThis !== 'undefined' ? globalThis : this, function createBridgeClient(getBridge, { timeoutMs = 20000 } = {}) {
    return function call(name, ...args) {
        const duration = name === 'build_one_off_profile' ? Math.max(timeoutMs, 180000) : timeoutMs;
        return new Promise((resolve, reject) => {
            let callbacks;
            let callbackIds = [];
            let settled = false;
            const finish = (handler, value) => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                // Eel 0.18 keeps callbacks after returning; release our own entries.
                callbackIds.forEach(id => { delete callbacks[id]; });
                handler(value);
            };
            const timer = setTimeout(() => finish(reject, new Error('The local backend did not respond. Retry or restart OpenGamma.')), duration);
            try {
                const bridge = getBridge();
                if (!bridge || typeof bridge[name] !== 'function') throw new Error('Local backend unavailable. Start the app with python app.py.');
                callbacks = bridge._call_return_callbacks;
                const previous = new Set(Object.keys(callbacks || {}));
                const pending = bridge[name](...args)();
                callbackIds = Object.keys(callbacks || {}).filter(id => !previous.has(id));
                Promise.resolve(pending).then(value => finish(resolve, value), error => finish(reject, error instanceof Error ? error : new Error(String(error))));
            } catch (error) { finish(reject, error); }
        });
    };
}));
