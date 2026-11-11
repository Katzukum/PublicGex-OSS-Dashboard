import { spawn } from 'node:child_process';
const port = process.env.PUBLICGEX_E2E_PORT || '1420';
if (!/^\d{2,5}$/.test(port) || Number(port) > 65535) throw new Error('Invalid demo port');
const child = spawn(
  process.execPath,
  ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', port, '--strictPort'],
  { stdio: 'inherit', env: { ...process.env, PUBLICGEX_DEMO: '1' } },
);
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.on('exit', (code) => process.exit(code ?? 0));
