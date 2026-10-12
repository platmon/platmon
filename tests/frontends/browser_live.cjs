// Optional browser soak against an isolated already-running service. No extra dependency installation.
// PLATMON_PLAYWRIGHT=... node tests/frontends/browser_live.cjs CHROMIUM URL OUTPUT_DIR [SECONDS]
const {chromium} = require(process.env.PLATMON_PLAYWRIGHT);
const fs = require('node:fs'); const path = require('node:path'); const assert = require('node:assert/strict');
const duration = Number(process.argv[5] || 660), output = process.argv[4];
fs.mkdirSync(output, {recursive: true});
(async () => {
 const browser = await chromium.launch({executablePath: process.argv[2], headless: true});
 try {
  const page = await browser.newPage(); const errors = [], requests = {}, samples = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { const p = new URL(r.url()).pathname; requests[p] = (requests[p] || 0) + 1; });
  for (const width of [1280, 390]) {
   await page.setViewportSize({width, height: 900}); await page.goto(process.argv[3]);
   await page.waitForFunction(() => document.querySelector('#overview').textContent.includes('CPU'));
   await page.waitForTimeout(1500);
   assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
   assert(await page.locator('.identity img').evaluate(img => img.complete && img.naturalWidth > 0));
   await page.screenshot({path: path.join(output, `live-${width}.png`), fullPage: true});
  }
  await page.locator('#tab-network').click(); // exercise optional endpoints during the soak
  const cdp = await page.context().newCDPSession(page); await cdp.send('Performance.enable');
  const start = Date.now();
  while (Date.now() - start < duration * 1000) {
   const metrics = await cdp.send('Performance.getMetrics');
   samples.push({elapsed_s: (Date.now()-start)/1000,
    nodes: await page.locator('*').count(),
    metrics: Object.fromEntries(metrics.metrics.filter(m => ['TaskDuration','JSHeapUsedSize','Nodes'].includes(m.name)).map(m=>[m.name,m.value])),
    error: await page.locator('#err').innerText()});
   await page.waitForTimeout(10000);
  }
  const last = samples.filter(s => s.elapsed_s > duration / 2);
  assert(last.length && Math.max(...last.map(s=>s.nodes))-Math.min(...last.map(s=>s.nodes)) <= 20, 'bounded DOM in second half');
  assert(samples.every(s=>!s.error)); assert.deepEqual(errors, []);
  assert((requests['/api/stats'] || 0) <= duration + 20, 'one stats polling stream');
  for (const p of ['/api/observations','/api/history']) assert((requests[p] || 0) <= duration/9 + 5);
  await page.screenshot({path: path.join(output,'live-soak-final.png'), fullPage:true});
  fs.writeFileSync(path.join(output,'browser-live.json'),JSON.stringify({browser:await browser.version(),duration_s:(Date.now()-start)/1000,
   samples,requests,errors,metrics_scope:'CDP renderer tasks/JS heap, not whole-browser or server CPU'},null,2));
  console.log('live browser soak passed');
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
