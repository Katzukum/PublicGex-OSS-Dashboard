import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { resolve, join, relative, isAbsolute } from 'node:path';
import { createInterface } from 'node:readline';

export async function startBackend(root, { demo = false, dataDir } = {}) {
  const directory = resolve(dataDir || join(root, demo ? '.demo-data' : 'data'));
  const rel = relative(root, directory);
  if (!rel || rel.startsWith('..') || isAbsolute(rel))
    throw new Error('Development data must be in a dedicated folder inside this project.');
  mkdirSync(directory, { recursive: true });
  const venv = join(
    root,
    '.venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
  );
  const python = existsSync(venv) ? venv : process.platform === 'win32' ? 'python' : 'python3';
  const args = [
    '-B',
    '-u',
    join(root, 'backend/service.py'),
    '--data-dir',
    directory,
    '--port',
    '0',
  ];
  if (demo) args.push('--demo');
  const child = spawn(python, args, {
    cwd: directory,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1' },
  });
  child.stdin.on('error', () => {}); // A failed child may close its lifetime pipe before cleanup.
  let stderr = '';
  child.stderr.on('data', (chunk) => {
    stderr = (stderr + chunk.toString()).slice(-4000);
  });
  const lines = createInterface({ input: child.stdout });
  let stopped = false;
  const ready = await new Promise((resolveReady, reject) => {
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error('Backend startup timed out. Run npm run setup:python.'));
    }, 60000);
    const fail = (error) => {
      clearTimeout(timer);
      reject(error);
    };
    child.once('error', (error) =>
      fail(new Error(`Cannot start Python backend: ${error.message}`)),
    );
    child.once('exit', (code) =>
      fail(new Error(`Backend exited during startup (${code}). ${stderr}`)),
    );
    lines.on('line', (line) => {
      let data;
      try {
        data = JSON.parse(line);
      } catch {
        return;
      }
      if (data.type !== 'ready') return;
      if (
        !Number.isInteger(data.port) ||
        data.port < 1 ||
        data.port > 65535 ||
        typeof data.token !== 'string' ||
        data.token.length < 20
      ) {
        child.kill();
        fail(new Error('Invalid backend startup handshake.'));
        return;
      }
      clearTimeout(timer);
      resolveReady(data);
    });
  });
  const request = async (method, args = []) => {
    if (stopped || child.exitCode !== null)
      throw new Error('The local analytics service has stopped. Restart the app.');
    const response = await fetch(`http://127.0.0.1:${ready.port}/rpc`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${ready.token}` },
      body: JSON.stringify({ method, args }),
      signal: AbortSignal.timeout(method === 'build_one_off_profile' ? 190000 : 45000),
    });
    const payload = await response.json();
    if (!response.ok || payload.error)
      throw new Error(payload.error || `Analytics service returned ${response.status}`);
    return payload.result;
  };
  const stop = async () => {
    if (stopped) return;
    try {
      await Promise.race([
        request('shutdown'),
        new Promise((resolveStop) => setTimeout(resolveStop, 1800)),
      ]);
    } catch {}
    stopped = true;
    child.stdin.end();
    if (child.exitCode === null) {
      await Promise.race([
        new Promise((resolveStop) => child.once('exit', resolveStop)),
        new Promise((resolveStop) => setTimeout(resolveStop, 1800)),
      ]);
      if (child.exitCode === null) child.kill();
    }
    lines.close();
  };
  return { request, stop, pid: child.pid, dataDir: directory };
}
