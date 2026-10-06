import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { chromium } from '@playwright/test';

// The production WebView policy must work without enabling eval for charts.
const root = resolve(import.meta.dirname, '..');
const config = JSON.parse(await readFile(resolve(root, 'src-tauri/tauri.conf.json'), 'utf8'));
const libraryRoot = dirname(
  createRequire(import.meta.url).resolve('lightweight-charts/package.json'),
);
const lightweight = await readFile(
  resolve(libraryRoot, 'dist/lightweight-charts.standalone.production.js'),
);
const chartScript = `
  try {
    const L = LightweightCharts;
    const options = { width:640, height:280, layout:{background:{type:'solid',color:'#101826'},textColor:'#dbe6f7'}, leftPriceScale:{visible:true}, rightPriceScale:{visible:true} };
    const profile = L.createOptionsChart(document.getElementById('profile'), options);
    const bars = profile.addSeries(L.HistogramSeries,{color:'#39c7a0'});
    bars.setData([{time:100,value:-2,color:'#f07987'},{time:105,value:3},{time:110,value:1}]);
    const line = profile.addSeries(L.LineSeries,{color:'#89a8ff'});
    line.setData([{time:100,value:1},{time:105,value:2},{time:110,value:3}]);
    line.createPriceLine({price:0,color:'#e7bc76',lineWidth:1,title:'Zero'});
    const sweep = L.createOptionsChart(document.getElementById('sweep'), options);
    const gamma = sweep.addSeries(L.AreaSeries,{priceScaleId:'left',lineColor:'#39c7a0',topColor:'#39c7a080',bottomColor:'#39c7a000'});
    gamma.setData([{time:100,value:-3},{time:105,value:1},{time:110,value:4}]);
    const hedge = sweep.addSeries(L.LineSeries,{priceScaleId:'right',color:'#e7bc76'});
    hedge.setData([{time:100,value:100000},{time:105,value:500000},{time:110,value:300000}]);
    const candles = L.createChart(document.getElementById('candles'), options);
    const ohlc = candles.addSeries(L.CandlestickSeries,{upColor:'#39c7a0',downColor:'#f07987',wickUpColor:'#39c7a0',wickDownColor:'#f07987'});
    ohlc.setData([{time:1727703000,open:100,high:106,low:98,close:104},{time:1727703300,open:104,high:105,low:99,close:101}]);
    class Cells {
      renderer(){ return this; }
      update(data){ this.data=data; }
      defaultOptions(){ return {...L.customSeriesDefaultOptions, color:'#39c7a0'}; }
      isWhitespace(row){ return row.low===undefined; }
      priceValueBuilder(row){ return [row.low,row.high,row.high]; }
      draw(target, priceToCoordinate){
        target.useMediaCoordinateSpace(({context})=>{
          for(const bar of this.data?.bars??[]){
            const low=priceToCoordinate(bar.originalData.low),high=priceToCoordinate(bar.originalData.high);
            if(low===null||high===null) continue;
            context.fillStyle=bar.originalData.color;
            context.fillRect(bar.x-this.data.barSpacing/2,Math.min(low,high),this.data.barSpacing,Math.max(2,Math.abs(low-high)));
          }
        });
      }
    }
    const heatmap = L.createChart(document.getElementById('heatmap'), options);
    const cells=heatmap.addCustomSeries(new Cells());
    cells.setData([{time:1727703000,low:100,high:105,color:'#39c7a0'},{time:1727703300,low:103,high:108,color:'#f07987'}]);
    window.chartCases=[{name:'profile',api:profile,series:[bars,line]},{name:'sweep',api:sweep,series:[gamma,hedge]},{name:'candles',api:candles,series:[ohlc]},{name:'heatmap',api:heatmap,series:[cells]}];
    window.chartCases.forEach(({api})=>api.timeScale().fitContent());
    requestAnimationFrame(()=>requestAnimationFrame(()=>{document.body.dataset.ready='true';}));
  } catch(error) { document.body.dataset.error=String(error); }
`;
const server = createServer((request, response) => {
  response.setHeader('Content-Security-Policy', config.app.security.csp);
  if (request.url === '/lightweight-charts.js') {
    response.setHeader('Content-Type', 'text/javascript');
    response.end(lightweight);
  } else if (request.url === '/chart.js') {
    response.setHeader('Content-Type', 'text/javascript');
    response.end(chartScript);
  } else {
    response.setHeader('Content-Type', 'text/html');
    response.end(
      '<!doctype html><div id="profile"></div><div id="sweep"></div><div id="candles"></div><div id="heatmap"></div><script src="/lightweight-charts.js"></script><script src="/chart.js"></script>',
    );
  }
});
await new Promise((resolveReady) => server.listen(0, '127.0.0.1', resolveReady));
let browser;
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.waitForFunction(
    () => document.body.dataset.ready || document.body.dataset.error,
    undefined,
    {
      timeout: 30000,
    },
  );
  const state = await page.evaluate(() => ({
    ready: document.body.dataset.ready,
    error: document.body.dataset.error,
    charts: window.chartCases?.map(({ name, api, series }) => {
      const canvas = api.takeScreenshot();
      const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      let colored = 0;
      for (let i = 0; i < pixels.length; i += 16) {
        if (
          Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) -
            Math.min(pixels[i], pixels[i + 1], pixels[i + 2]) >
          50
        )
          colored++;
      }
      return {
        name,
        width: canvas.width,
        height: canvas.height,
        colored,
        series: series.map((item) => ({ type: item.seriesType(), size: item.data().length })),
      };
    }),
  }));
  if (
    state.ready !== 'true' ||
    state.error ||
    errors.length ||
    state.charts?.length !== 4 ||
    state.charts.some(
      (chart) =>
        chart.colored < 8 || chart.width < 600 || chart.series.some((series) => series.size < 2),
    )
  )
    throw new Error(JSON.stringify({ ...state, errors }));
  console.log(
    'Native CSP passed: bundled Lightweight Charts histogram/line, dual-axis area, candles, and custom heatmap render real pixels with script-src self and no unsafe-eval.',
  );
} finally {
  await browser?.close();
  await new Promise((resolveClosed) => server.close(resolveClosed));
}
