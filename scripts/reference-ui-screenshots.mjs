import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { chromium, expect } from '@playwright/test';
import { startBackend } from './backend-process.mjs';

// Optional comparison tool. Reads reference UI source only; never starts the
// original application or opens its data. All RPCs use a new synthetic workspace.
const root = resolve(import.meta.dirname, '..');
if (!process.argv[2]) throw new Error('Pass the original source directory explicitly.');
const reference = resolve(process.argv[2], 'web');
const backend = await startBackend(root, {
  demo: true,
  dataDir: resolve(root, '.test-data', `reference-ui-${Date.now()}`),
});
const eel = `window.eel = new Proxy({expose(){}, _websocket:{readyState:1}, _call_return_callbacks:{}}, {get(target, name){if(name in target)return target[name];return (...args)=>()=>fetch('/reference-rpc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({method:name,args})}).then(response=>response.json()).then(result=>{if(result.error)throw new Error(result.error);return result.result;});}});`;
const server = createServer(async (request, response) => {
  try {
    if (request.url === '/reference-rpc' && request.method === 'POST') {
      const body = [];
      for await (const chunk of request) body.push(chunk);
      const { method, args } = JSON.parse(Buffer.concat(body).toString());
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ result: await backend.request(method, args) }));
      return;
    }
    if (request.url === '/eel.js') {
      response.setHeader('Content-Type', 'text/javascript');
      response.end(eel);
      return;
    }
    if (request.url === '/favicon.ico') {
      response.writeHead(204).end();
      return;
    }
    const path = resolve(
      reference,
      `.${decodeURIComponent(request.url === '/' ? '/index.html' : request.url.split('?')[0])}`,
    );
    if (!path.startsWith(reference + sep)) throw new Error('Invalid reference resource');
    response.setHeader(
      'Content-Type',
      { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }[extname(path)] ||
        'application/octet-stream',
    );
    response.end(await readFile(path));
  } catch (error) {
    response.statusCode = 500;
    response.end(JSON.stringify({ error: error.message }));
  }
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
let browser;
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1700, height: 1050 } });
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await expect(page.locator('#symbolSelector option')).toHaveCount(5, { timeout: 60000 });
  await page.locator('#symbolSelector').selectOption('SPX');
  await expect(page.locator('#cockpitSymbol')).toHaveText('SPX', { timeout: 60000 });
  await mkdir(resolve(root, 'docs/screenshots/reference'), { recursive: true });
  for (const [view, name] of [
    ['cockpit', 'Cockpit'],
    ['edge-lab', 'Edge Lab'],
    ['regime', 'Regime'],
    ['trace', 'TRACE'],
    ['analysis', 'Strike Matrix'],
    ['one-off', 'One-Off'],
    ['settings', 'Settings'],
  ]) {
    await page.locator(`.main-nav [data-view="${view}"]`).click();
    await expect(page.locator(`#view-${view}`)).toBeVisible();
    if (view === 'trace')
      await expect(page.locator('#traceSessionDate')).toHaveValue(/\d{4}-\d{2}-\d{2}/);
    await page.screenshot({
      path: resolve(root, `docs/screenshots/reference/${view}.png`),
      fullPage: true,
    });
    console.log(`Read-only original UI captured: ${name}`);
  }
} finally {
  await browser?.close();
  await new Promise((done) => server.close(done));
  await backend.stop();
}
