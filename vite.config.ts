import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { startBackend } from './scripts/backend-process.mjs';

const root = fileURLToPath(new URL('.', import.meta.url));
function analyticsBridge(): Plugin {
  const native = Boolean(process.env.TAURI_ENV_PLATFORM || process.env.PUBLICGEX_NATIVE);
  let backend: Awaited<ReturnType<typeof startBackend>> | undefined;
  let starting: ReturnType<typeof startBackend> | undefined;
  let closing = false;
  const stop = async () => {
    if (closing) return;
    closing = true;
    if (starting) {
      try {
        backend = await starting;
      } catch {}
    }
    await backend?.stop();
  };
  async function middleware(req: IncomingMessage, res: ServerResponse, next: () => void) {
    const route = req.url?.split('?')[0];
    if (!route?.startsWith('/api/')) {
      next();
      return;
    }
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'no-store');
    const reply = (status: number, value: unknown) => {
      res.statusCode = status;
      res.end(JSON.stringify(value));
    };
    if (native) {
      reply(503, { error: 'Use the native app window for this development server.' });
      return;
    }
    if (!req.headers.host || !/^(127\.0\.0\.1|localhost):\d+$/.test(req.headers.host)) {
      reply(403, { error: 'Invalid local host.' });
      return;
    }
    if (req.headers.origin && req.headers.origin !== `http://${req.headers.host}`) {
      reply(403, { error: 'Cross-origin requests are disabled.' });
      return;
    }
    if (
      (route !== '/api/rpc' || req.method !== 'POST') &&
      (route !== '/api/health' || req.method !== 'GET')
    ) {
      reply(404, { error: 'Unknown API route.' });
      return;
    }
    try {
      if (!starting)
        starting = startBackend(root, {
          demo: process.env.PUBLICGEX_DEMO === '1',
          dataDir: process.env.PUBLICGEX_DEV_DATA_DIR,
        }).catch((error) => {
          starting = undefined;
          throw error;
        });
      backend = await starting;
      if (route === '/api/health') {
        reply(200, { result: await backend.request('get_runtime_info') });
        return;
      }
      if (!req.headers['content-type']?.startsWith('application/json')) {
        reply(415, { error: 'JSON content type required.' });
        return;
      }
      const chunks: Buffer[] = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 1048576) {
          reply(413, { error: 'Request too large.' });
          return;
        }
        chunks.push(chunk);
      }
      let body: { method?: unknown; args?: unknown };
      try {
        body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      } catch {
        reply(400, { error: 'Invalid JSON.' });
        return;
      }
      if (typeof body.method !== 'string' || !Array.isArray(body.args)) {
        reply(400, { error: 'Expected method and args.' });
        return;
      }
      reply(200, { result: await backend.request(body.method, body.args) });
    } catch (error) {
      reply(503, {
        error: error instanceof Error ? error.message : 'Analytics service unavailable.',
      });
    }
  }
  return {
    name: 'isolated-analytics-bridge',
    configureServer(server) {
      server.middlewares.use(middleware);
      server.httpServer?.once('close', () => {
        void stop();
      });
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
      server.httpServer.once('close', () => {
        void stop();
      });
    },
    async closeBundle() {
      await stop();
    },
  };
}
export default defineConfig({
  plugins: [react(), analyticsBridge()],
  server: {
    host: '127.0.0.1',
    port: 1420,
    strictPort: true,
    watch: {
      ignored: [
        '**/backend/**',
        '**/data/**',
        '**/.demo-data/**',
        '**/.test-data/**',
        '**/.venv/**',
        '**/.tools/**',
        '**/src-tauri/**',
      ],
    },
  },
  clearScreen: false,
  build: { target: 'es2022', chunkSizeWarningLimit: 600 },
});
