// Optional real Chromium check. Uses an already installed Playwright, never installs a dependency.
// PLATMON_PLAYWRIGHT=/path/to/playwright node tests/frontends/browser_smoke.cjs /path/to/chromium OUTPUT_DIR
const {chromium} = require(process.env.PLATMON_PLAYWRIGHT);
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const output = process.argv[3]; fs.mkdirSync(output, {recursive: true});
const bad = '<img src=x onerror="globalThis.injected=1">';
const snap = {platform: 'Linux', model: 'Fixture board', uptime: 3 * 86400 + 7 * 3600 + 41 * 60, power_mode: 'MAXN_SUPER',
  system: {os: 'Ubuntu 22.04.5 LTS', kernel: '5.15.199-tegra', arch: 'aarch64', hostname: bad, l4t: 'R36.5.2'},
  cpu: [{id: 0, usage: 1, freq: null}], gpu: null, memory: {total: 8e9, used: 2e9, swap_total: 0},
  disk: {total: 10e9, used: 5e9}, temperature: {[bad]: 40}, power: {[bad]: 2}, fans: [{name: bad, rpm: 0, percent: null}],
  network: {interfaces: [{name: bad, rx: {errors: 0, dropped: 0}, tx: {errors: 0, dropped: 0}, window_ms: 1234,
    rates: {rx_bytes_per_s: 1024, tx_bytes_per_s: 0, rx_packets_per_s: 1, tx_packets_per_s: 0}}]},
  disk_io: {disks: [{name: 'sda', in_flight: 0, rates: null, reason: 'warmup'}]},
  pressure: {resources: {cpu: {some: {avg10: 1.5}, full: null}}}};
snap.network.interfaces.unshift({name: 'br-example', rx: {}, tx: {}, rates: {
  rx_bytes_per_s: 0, tx_bytes_per_s: 0, rx_packets_per_s: 0, tx_packets_per_s: 0}});
const obs = {groups: {wifi: {state: 'ok', stale_after_ms: 15000,
  observation: {id: 1, data_age_ms: 14000, stale: false}, data: {interfaces: [
    {name: bad, connected: true, signal_dbm: -64, link_quality: 45}]}},
  storage: {state: 'ok', observation: {id: 1, data_age_ms: 1000, stale: false}, data: {filesystems: [
    {mount_points: [{path: '/long/' + 'mount'.repeat(40), read_only: true}], fstype: 'ext4', device: 'sda1',
      total_bytes: 4e9, used_bytes: 1e9, available_bytes: 3e9}], partitions: []}}}};
const points = Array.from({length: 120}, (_, i) => [i + 1, (121 - i) * 1000, i % 7 === 0 ? null : i]);
const history = {retention_s: 600, series: {
  'memory/used_bytes': points,
  'cpu/0/usage': points,
  'observation/wifi/ns:1/2/wlan0/signal_dbm': [[1, 12000, -65], [2, 11000, null], [3, 2000, -64]],
  'observation/probe/127.0.0.1:80/connect_ms': [[1, 12000, 1], [2, 11000, 2], [3, 2000, 3]],
  'observation/storage/8:1/token/used_bytes': [[1, 30000, 1e9]],
  'network/ns:1/3/<b>/rx_bytes_per_s': [[1, 3000, 5], [2, 2000, null]],
  ...Object.fromEntries(Array.from({length: 58}, (_, i) => [`cpu/${i+1}/usage`, points]))
}};
(async () => {
  const browser = await chromium.launch({executablePath: process.argv[2], headless: true});
  try {
    const page = await browser.newPage(); const errors = []; const counts = {};
    page.on('pageerror', e => errors.push(e.message));
    let mode = 'ok';
    await page.route('http://platmon.test/**', async route => {
      const url = new URL(route.request().url()); counts[url.pathname] = (counts[url.pathname] || 0) + 1;
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync('frontends/web/index.html', 'utf8')});
      if (url.pathname === '/app.js' || url.pathname === '/styles.css') return route.fulfill({contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css', body: fs.readFileSync(path.join('frontends/web', url.pathname))});
      if (url.pathname.startsWith('/assets/')) return route.fulfill({contentType: 'image/svg+xml', body: fs.readFileSync(path.join('frontends/web', url.pathname))});
      if (url.pathname === '/api/stats') {
        if (mode === 'timeout') return; // held until the browser's AbortController cancels
        return route.fulfill({status: mode === '503' ? 503 : 200, json: snap});
      }
      if (url.pathname === '/api/observations') return route.fulfill({json: obs});
      if (url.pathname === '/api/history') return route.fulfill({json: history});
      return route.fulfill({status: 404, json: {}});
    });
    for (const width of [1280, 390]) {
      await page.setViewportSize({width, height: 900}); await page.goto('http://platmon.test/');
      await page.waitForFunction(() => document.querySelector('#overview').textContent.includes('Root filesystem'));
      assert(await page.locator('#panel-overview').isVisible());
      assert((await page.locator('#overview').innerText()).includes('br-example'));
      assert((await page.locator('#overview').innerText()).includes(bad));
      assert.equal(await page.locator('#overview img').count(), 0);
      assert.equal(await page.locator('.tabs').evaluate(e => getComputedStyle(e).position), width < 640 ? 'fixed' : 'static');
      assert(!(await page.locator('header').innerText()).includes('Ubuntu 22.04.5 LTS'));
      assert(!(await page.locator('header').innerText()).includes('MAXN_SUPER'));
      assert.equal(await page.locator('#history-panel').isVisible(), false);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'overview fits long interface names');
      await page.screenshot({path: path.join(output, `overview-${width}.png`), fullPage: true});
      await page.locator('#overview a[href="#network"]').focus();
      await page.evaluate(() => tick());
      assert(await page.locator('#overview a[href="#network"]').evaluate(a => a === document.activeElement), 'refresh preserves detail-link focus');
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('#wifi').textContent.includes('-64 dBm'));
      await page.waitForTimeout(2200);
      assert((await page.locator('[data-observation-age="wifi"]').innerText()).includes('(not current)'));
      assert((await page.locator('#net-cap').innerText()).includes('window 1.234 s'));
      assert.equal(await page.locator('section img').count(), 0);
      assert(await page.locator('.identity img').evaluate(img => img.complete && img.naturalWidth > 0));
      assert.equal(await page.evaluate(() => !!globalThis.injected), false);
      assert.equal(await page.locator('#hist svg').count(), 3);
      const histText = await page.locator('#hist').innerText();
      assert(histText.includes('-64.0 dBm') && histText.includes('3.0 ms') && !histText.includes('CPU0'));
      assert(histText.includes('<b> rx') && histText.includes('no value'));
      assert(histText.includes('latest point 4 s ago') || histText.includes('latest point 5 s ago'), 'history age advances between responses');
      assert.equal(await page.locator('#hist b').count(), 0);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no horizontal overflow');
      await page.screenshot({path: path.join(output, `network-${width}.png`), fullPage: true});
      const requestsBefore = counts['/api/history'];
      await page.locator('#tab-resources').click();
      assert.equal(await page.locator('#hist svg').count(), 60);  // 59 cores and RAM, every series of both kinds
      assert((await page.locator('#hist').innerText()).includes('CPU58'));
      const graph = await page.locator('#hist svg path.line').first().getAttribute('d');
      assert(graph.split('M').length > 2, 'missing buckets break paths');
      assert.equal(await page.locator('#history-groups button').count(), 2);
      assert.deepEqual(await page.locator('#hist .hist-group h3').allInnerTexts(), ['CPU cores', 'RAM']);
      await page.locator('#history-groups button[data-group="cpu/"]').click();
      assert.equal(await page.locator('#hist svg').count(), 1);
      assert((await page.locator('#hist').innerText()).includes('RAM used'));
      await page.locator('#history-groups button[data-group="cpu/"]').click();
      assert.equal(await page.locator('#hist svg').count(), 60);
      assert.equal(counts['/api/history'], requestsBefore, 'cached tab and group buttons do not fetch');
      await page.evaluate(() => { globalThis.savedGraph = document.querySelector('#hist svg'); });
      await page.waitForTimeout(1100);
      assert(await page.evaluate(() => globalThis.savedGraph === document.querySelector('#hist svg')), 'age timer preserves SVG nodes');
      await page.screenshot({path: path.join(output, `resources-${width}.png`), fullPage: true});
      await page.locator('#tab-resources').focus();
      await page.keyboard.press('ArrowRight');
      assert.equal(await page.locator('#tab-network').evaluate(el => el === document.activeElement), true);
      assert.equal(await page.locator('#tab-resources').getAttribute('aria-selected'), 'true');
      await page.keyboard.press('Tab');
      await page.keyboard.press('Shift+Tab');
      assert.equal(await page.locator('#tab-resources').evaluate(el => el === document.activeElement), true);
      await page.keyboard.press('ArrowRight');
      await page.keyboard.press('Enter');
      assert(await page.locator('#panel-network').isVisible());
      for (const tab of ['storage', 'thermal', 'power', 'system']) {
        await page.locator('#tab-' + tab).click();
        assert(await page.locator('#panel-' + tab).isVisible());
        if (tab === 'storage') assert((await page.locator('#dio').innerText()).includes('warming up'));
        if (tab === 'thermal') assert((await page.locator('#temp').innerText()).includes('40.0 °C'));
        if (tab === 'power') assert((await page.locator('#pwr-cap').innerText()).includes('MAXN_SUPER'));
        if (tab === 'system') {
          assert.equal(await page.locator('#system-os').innerText(), 'Ubuntu 22.04.5 LTS');
          assert.equal(await page.locator('#system-uptime').innerText(), '3d 07:41');
          assert.equal(await page.locator('#system-hostname').innerText(), bad);
          assert.equal(await page.locator('#system-hostname img').count(), 0);
          assert.equal(await page.locator('#history-panel').isVisible(), false);
          const before = [counts['/api/observations'], counts['/api/history']];
          await page.waitForTimeout(1100);
          assert.deepEqual([counts['/api/observations'], counts['/api/history']], before);
          assert.equal(new URL(page.url()).hash, '#system');
          await page.reload();
          await page.waitForFunction(() => document.querySelector('#system-os').textContent === 'Ubuntu 22.04.5 LTS');
          assert(await page.locator('#panel-system').isVisible());
          assert.deepEqual([counts['/api/observations'], counts['/api/history']], before, 'restoring System does not request optional endpoints');
          const bounds = await page.locator('#tab-system').boundingBox();
          assert(bounds.x >= 0 && bounds.x + bounds.width <= width, 'restored tab is visible without horizontal scrolling');
        }
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), tab + ' fits narrow width');
        await page.screenshot({path: path.join(output, `${tab}-${width}.png`), fullPage: true});
      }
    }
    await page.locator('#tab-resources').click();
    await page.waitForFunction(() => document.querySelectorAll('#hist svg').length === 60);
    const before = await page.locator('*').count();
    for (let n = 0; n < 120; n++) await page.evaluate(() => tick());
    assert.equal(await page.locator('*').count(), before, 'repeated rendering does not append DOM nodes');
    await page.emulateMedia({colorScheme: 'dark'});
    await page.locator('#tab-overview').click();
    await page.screenshot({path: path.join(output, 'overview-dark.png'), fullPage: true});
    mode = '503'; await page.evaluate(() => tick());
    assert((await page.locator('#err').innerText()).includes('No current data'));
    mode = 'timeout'; await page.evaluate(() => tick());
    assert((await page.locator('#err').innerText()).includes('No answer within 5 s'));
    mode = 'ok'; await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForFunction(() => !document.querySelector('#err').textContent);
    await page.goto('http://platmon.test/#unknown');
    await page.waitForFunction(() => document.querySelector('#overview').textContent.includes('Root filesystem'));
    assert(await page.locator('#panel-overview').isVisible());
    await page.evaluate(() => { location.hash = 'system'; });
    await page.waitForFunction(() => !document.querySelector('#panel-system').hidden);
    await page.goBack();
    await page.waitForFunction(() => !document.querySelector('#panel-overview').hidden);
    await page.goto('http://platmon.test/#network');
    await page.waitForFunction(() => document.querySelector('#wifi').textContent.includes('-64 dBm'));
    assert(await page.locator('#panel-network').isVisible());
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({browser: await browser.version(),
      fixtures: true, widths: [1280, 390], history_series: 64, displayed_series: 60, points_per_series: 120,
      repeat_renders: 120, dom_nodes: before, errors, requests: counts,
      visibility: 'synthetic visibilitychange in real Chromium', timeout: 'real 5 second deadline'}, null, 2));
    console.log('Chromium desktop/narrow, XSS, stale, repeat DOM, 503, timeout, tab-return event: passed');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
