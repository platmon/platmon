// View selection, bounded polling and hidden-page lifecycle with deterministic fixtures.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
let now = 0, nextTimer = 0, focused = '', failure = false;
const elements = {}, intervals = [], timers = new Map(), listeners = [], requests = [];
const navigation = {};
const el = id => elements[id] ||= {innerHTML: '', textContent: '', value: '', hidden: false, attrs: {}, handlers: {},
  setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(k, fn) { this.handlers[k] = fn; }, focus() { focused = id; }, scrollIntoView() {}};
const stats = {model: 'Fixture', platform: 'Linux', uptime: 12, system: {}, power_mode: null,
  cpu: [{id: 0, usage: 20, freq: null}, {id: 1, usage: 40, freq: null}], gpu: null,
  memory: {used: 2e9, total: 8e9, swap_total: 0}, disk: {used: 5e9, total: 10e9},
  temperature: {'<b>hot</b>': 60, cool: 40}, power: {}, fans: [],
  network: {interfaces: [{name: 'lo', rx: {}, tx: {}, rates: {rx_bytes_per_s: 999999, tx_bytes_per_s: 0, rx_packets_per_s: 0, tx_packets_per_s: 0}},
    {name: 'eth0', rx: {}, tx: {}, rates: {rx_bytes_per_s: 1024, tx_bytes_per_s: 2048, rx_packets_per_s: 1, tx_packets_per_s: 2}}]}};
const history = {retention_s: 600, series: {'cpu/0/usage': [[1, 2000, 20]], 'gpu/usage': [[1, 1500, 30]],
  'memory/used_bytes': [[1, 1000, 2e9]],
  'network/ns/1/eth0/rx_bytes_per_s': [[1, 3000, 1024]], 'temperature/name:cool': [[1, 1000, 40]]}};
const context = vm.createContext({AbortController, console, performance: {now: () => now},
  location: {hash: ''}, history: {replaceState(_, __, hash) { context.location.hash = hash; }},
  window: {addEventListener(type, fn) { navigation[type] = fn; }},
  document: {visibilityState: 'visible', getElementById: el, querySelectorAll: () => [],
    addEventListener: (_, fn) => listeners.push(fn)},
  setInterval: fn => intervals.push(fn), setTimeout: (fn, ms) => { timers.set(++nextTimer, {fn, ms}); return nextTimer; },
  clearTimeout: id => timers.delete(id), fetch: async url => {
    requests.push(url);
    if (failure && url === 'api/stats') return {ok: false, status: 503};
    return {ok: true, status: 200, json: async () => url === 'api/stats' ? stats : url.startsWith('api/history') ? history : observations};
  }});
const observations = {schema_version: 2, groups: {battery: {state: 'ok', stale_after_ms: 180000,
  observation: {id: 5, data_age_ms: 1000, stale: false},
  data: {provider: 'sysfs_power_supply', external: [],
         batteries: [{name: 'BAT1', present: true, percent: 72, status: 'Discharging', capacity_level: null,
                      technology: null, health: null, charge_now_uah: null, charge_full_uah: null,
                      charge_full_design_uah: null, energy_now_uwh: null, energy_full_uwh: null,
                      energy_full_design_uwh: null, current_ua: null, power_uw: null, voltage_uv: null,
                      cycle_count: null}]}}}};
const run = text => vm.runInContext(text, context);
const flush = async () => { for (let n = 0; n < 6; n++) await new Promise(setImmediate); };
const count = prefix => requests.filter(url => url.startsWith(prefix)).length;
const switchTo = async tab => { el('tab-' + tab).handlers.click(); await flush(); };
// the Recent history buttons are delegated on their row, as the chart readout is
const clickGroup = prefix => el('history-groups').handlers.click({target: {closest: () => ({dataset: {group: prefix}})}});
(async () => {
  el('levels').textContent = JSON.stringify({gpu: [70, 85]});  // as the server fills it in from [thresholds]
  run(code); await flush();
  assert.equal(count('api/stats'), 1);
  assert.equal(count('api/history'), 0, 'overview does not request graphs');
  assert.equal(count('api/observations'), 0);
  assert(el('overview').innerHTML.includes('30.0<span class="unit">%'));
  assert(el('overview').innerHTML.includes('Mean of 2 measured cores'));
  assert(el('overview').innerHTML.includes('60.0<span class="unit">°C') && el('overview').innerHTML.includes('&#60;b&#62;hot'));
  assert(!el('overview').innerHTML.includes('Needs attention'), 'nothing above its warning level');
  const attention = patch => run('overviewRows({...latestStats, ...' + JSON.stringify(patch) + '})');
  const full = attention({memory: {used: 7.6e9, total: 8e9, swap_total: 0}});
  assert(full.includes('Needs attention') && full.includes('<a class="pill crit" href="#resources">Memory'), 'memory 95% is critical');
  assert(attention({temperature: {cpu: 82}}).includes('pill warn'), 'not served by platmon: generic 80/90 °C');
  run('LIMITS.temperature = [85, 95]');  // what the server sends for a Jetson Orin (tests/runtime/test_platmon.py)
  const orin = attention({temperature: {cpu: 90, gpu: 86, soc: 40}});
  assert(orin.includes('href="#thermal">cpu') && orin.includes('href="#thermal">gpu') && !orin.includes('>soc'), 'every hot sensor');
  assert(orin.includes('pill warn') && !orin.includes('pill crit'), 'the served temperature level applies');
  assert(attention({gpu: {usage: 75, freq: null}}).includes('href="#resources">GPU'), 'configured GPU level: warns from 70 %');
  assert(!full.includes('href="#resources">CPU'), 'unset levels keep their defaults');
  run('LIMITS.temperature = [80, 90]');
  assert(el('overview').innerHTML.includes('eth0') && !el('overview').innerHTML.includes('999999'));
  const networkOverview = interfaces => run('overviewRows({...latestStats, network: ' + JSON.stringify({interfaces}) + '})');
  const rate = (name, rx) => ({name, rates: {rx_bytes_per_s: rx, tx_bytes_per_s: 0}});
  const mixed = networkOverview([rate('br-example', 0), rate('eth0', 1024), rate('lo', 999999), rate('wlan0', 2048)]);
  assert(mixed.includes('br-example') && mixed.includes('eth0') && mixed.includes('wlan0'), 'bridge does not hide physical interface readings');
  assert(mixed.includes('1.0 KiB/s') && mixed.includes('2.0 KiB/s') && mixed.includes('0 B/s'));
  assert(mixed.includes('<div class="tr idle"><span>br-example') && !mixed.includes('first non-loopback'), 'idle interfaces are dimmed');
  assert(mixed.indexOf('wlan0') < mixed.indexOf('eth0') && mixed.indexOf('eth0') < mixed.indexOf('br-example'), 'busiest interfaces first');
  assert(mixed.includes('href="#network">All interfaces'));
  const many = networkOverview(Array.from({length: 20}, (_, n) => rate('iface-' + n, n)));
  assert(many.includes('Showing 4 of 20') && !many.includes('iface-4<'), 'overview rows stay bounded');
  assert(many.includes('iface-19<') && !many.includes('iface-0<'), 'the four busiest are shown');
  const missing = networkOverview([{name: '<b>wifi</b>', rates: null, reason: 'warmup'}]);
  assert(missing.includes('warming up') && missing.includes('&#60;b&#62;wifi') && !missing.includes('<b>'));
  assert(!networkOverview([rate('lo', 1)]).includes('All interfaces'));
  assert(!networkOverview([]).includes('All interfaces'));
  assert.equal(el('cpu').innerHTML, '', 'inactive resource view is not rendered');
  const overview = el('overview').innerHTML;
  await switchTo('resources');
  assert.equal(el('tab-resources').attrs['aria-selected'], 'true');
  assert.equal(el('panel-overview').hidden, true);
  assert.equal(el('panel-resources').hidden, false);
  assert(el('cpu').innerHTML.includes('CPU0'));
  assert.equal(count('api/history'), 1);
  assert(!el('hist').innerHTML.includes('eth0'));
  assert(el('history-cap').textContent.startsWith('Last 10 min'));
  run("latestStats = {...latestStats, sensor_meta: {'/temperature/cpu~1a': {id: 'src1:abc'}, '/power/x': {id: 'src1:def'}}}");
  assert.equal(run("sensorName('temperature', 'src1:abc')"), 'cpu/a', 'a temperature series is labelled with its sensor name');
  assert.equal(run("sensorName('temperature', 'src1:def')"), 'src1:def', 'only temperature pointers count; unknown ids stay as they are');
  const label = id => run(`(([re, name]) => name(${JSON.stringify(id)}.match(re)))(HIST_LABEL.find(([re]) => re.test(${JSON.stringify(id)})))`);
  assert.equal(label('temperature/src1:abc'), 'temp cpu/a');
  assert.equal(label('temperature/name:board'), 'temp board');
  assert.equal(el('cpu-cap').textContent, 'mean 30.0% · 2 of 2 measured');
  assert.deepEqual(el('hist').innerHTML.match(/<h3>[^<]+<\/h3>/g), ['<h3>CPU cores</h3>', '<h3>GPU</h3>', '<h3>RAM</h3>'],
    'the GPU is a kind of its own, between the cores and RAM');
  clickGroup('cpu/');  // the CPU cores button off leaves the other kinds of this view
  assert(el('hist').innerHTML.includes('RAM used') && !el('hist').innerHTML.includes('CPU0'));
  const graph = el('hist').innerHTML;
  stats.cpu[0].usage = 80; await run('tick()');
  assert.equal(el('overview').innerHTML, overview, 'inactive overview is not rewritten');
  now = 4000; intervals.forEach(fn => fn()); await flush();
  assert.equal(el('hist').innerHTML, graph, 'one-second timer never rebuilds graphs');
  clickGroup('cpu/');  // and back on
  assert(el('hist').innerHTML.includes('CPU0') && el('hist').innerHTML.includes('RAM used'));
  await switchTo('network');
  assert.equal(count('api/observations'), 1);
  assert.equal(count('api/history'), 1, 'tab switch shares the history cache and deadline');
  assert(el('hist').innerHTML.includes('eth0') && !el('hist').innerHTML.includes('CPU0'));
  for (let n = 0; n < 20; n++) { await switchTo('storage'); await switchTo('network'); }
  assert.equal(count('api/observations'), 1);
  assert.equal(count('api/history'), 1);
  assert.equal(intervals.length, 1);
  assert.equal([...timers.values()].filter(t => t.ms === 1000).length, 1);
  const beforeKeys = requests.length;
  el('tab-network').handlers.keydown({key: 'ArrowRight', preventDefault() {}});
  assert.equal(focused, 'tab-storage');
  assert.equal(el('tab-network').attrs['aria-selected'], 'true', 'arrows move focus without fetching');
  assert.equal(requests.length, beforeKeys);
  failure = true; await run('tick()');
  await switchTo('overview');
  assert(el('overview').innerHTML.includes('60.0%'), 'last good snapshot is available in another view');
  assert(el('status').title.includes('not current') && el('status').textContent === 'Offline');
  const beforeHidden = requests.length;
  context.document.visibilityState = 'hidden'; listeners.forEach(fn => fn()); await flush();
  assert.equal(timers.size, 0, 'no pending request or refresh timer while hidden');
  now += 60000; intervals.forEach(fn => fn()); await run('tick()');
  assert.equal(requests.length, beforeHidden);
  failure = false;
  context.document.visibilityState = 'visible'; listeners.forEach(fn => fn()); await flush();
  assert.equal(count('api/stats'), 4, 'return requests one fresh stats snapshot');
  assert.equal(el('err').textContent, '');
  assert.equal(count('api/history'), 1, 'return to overview still does not request history');
  const optionalBefore = [count('api/history'), count('api/observations')];
  assert.equal(el('system-os').textContent, '', 'system fields are not rendered in other views');
  await switchTo('system');
  assert.equal(el('history-panel').hidden, true);
  assert.equal(el('system-platform').textContent, 'Linux');
  assert.equal(el('system-l4t-row').hidden, true, 'unsupported fields are hidden');
  now += 30000; intervals.forEach(fn => fn()); await flush();
  assert.deepEqual([count('api/history'), count('api/observations')], optionalBefore, 'System does not poll optional endpoints even when due');
  stats.system = {os: 'Ubuntu 22.04.5 LTS', kernel: '5.15.199-tegra', arch: 'aarch64', hostname: '<b>host</b>',
    l4t: 'R36.5.2', '<b>custom</b>': '<i>value</i>'};
  stats.uptime = 3 * 86400 + 7 * 3600 + 41 * 60;
  stats.power_mode = 'MAXN_SUPER';
  await run('tick()');
  assert.equal(el('system-os').textContent, 'Ubuntu 22.04.5 LTS');
  assert.equal(el('system-uptime').textContent, '3d 07:41');
  assert.equal(el('system-hostname').textContent, '<b>host</b>');
  assert.equal(el('system-l4t-row').hidden, false);
  assert(el('system-extra').innerHTML.includes('&#60;b&#62;custom') && !el('system-extra').innerHTML.includes('<i>'));
  let osWrites = 0, osText = el('system-os').textContent;
  Object.defineProperty(el('system-os'), 'textContent', {get: () => osText, set: value => { osWrites++; osText = value; }});
  stats.uptime += 60; await run('tick()');
  assert.equal(osWrites, 0, 'uptime updates do not rewrite unchanged system values');
  assert.equal(el('system-uptime').textContent, '3d 07:42');
  await switchTo('overview');
  stats.system.os = 'Updated OS'; delete stats.system.l4t; await run('tick()');
  assert.equal(osWrites, 0, 'inactive System is not updated');
  await switchTo('system');
  assert.equal(osWrites, 1);
  assert.equal(el('system-l4t-row').hidden, true);
  const obsBeforePower = count('api/observations');
  now += 30000;  // past the 10 s request interval, so a view that needs them may ask again
  await switchTo('power'); intervals.forEach(fn => fn()); await flush();
  assert(count('api/observations') > obsBeforePower, 'Power & battery asks for /api/observations');
  assert(el('batt').innerHTML.includes('72 %'), 'and fills its Battery card from the answer');
  assert(el('pwr-cap').innerHTML.includes('MODE MAXN_SUPER'), 'power mode is in the Power & battery view');
  assert(el('pwr').innerHTML.includes('No power rails or fans reported'), 'a power mode alone still shows the card');
  assert.equal(context.location.hash, '#power', 'selection is stored in the URL');
  await switchTo('thermal');
  assert(el('temp').innerHTML.indexOf('&#60;b&#62;hot') < el('temp').innerHTML.indexOf('cool'), 'hottest first');
  assert.equal(el('pwr').innerHTML.includes('No power rails'), true, 'the other view keeps what it drew');
  assert.equal(context.location.hash, '#thermal', 'selection is stored in the URL');
  const beforeNavigation = requests.length;
  context.location.hash = '#system'; navigation.hashchange();
  assert.equal(el('panel-system').hidden, false);
  context.location.hash = '#unknown'; navigation.hashchange();
  assert.equal(el('panel-overview').hidden, false, 'unknown links fall back to Overview');
  assert.equal(requests.length, beforeNavigation, 'System and Overview navigation do not fetch');
  // Recent history: a button per kind of series, pressed = drawn, and every series of a kind is drawn
  await switchTo('resources'); await flush();
  run(`hist = {retention_s: 600, series: Object.fromEntries([...Array.from({length: 20},
    (_, i) => ['cpu/' + i + '/usage', [[1, 1000, i]]]), ['memory/used_bytes', [[1, 1000, 2e9]]]])}`);
  run('renderHistory()');
  const drawn = () => (el('hist').innerHTML.match(/data-series=/g) || []).length;
  assert.equal(drawn(), 21, 'every series of every kind on, with no cap');
  assert.deepEqual(el('hist').innerHTML.match(/<h3>[^<]+<\/h3>/g), ['<h3>CPU cores</h3>', '<h3>RAM</h3>'],
    'one section per kind, cores and RAM apart, in the button row order');
  assert(el('history-groups').innerHTML.includes('CPU cores 20') && el('history-groups').innerHTML.includes('RAM 1'));
  assert.equal((el('history-groups').innerHTML.match(/aria-pressed="true"/g) || []).length, 2);
  clickGroup('cpu/');
  assert.equal(drawn(), 1);
  assert.deepEqual(el('hist').innerHTML.match(/<h3>[^<]+<\/h3>/g), ['<h3>RAM</h3>'], 'a kind that is off has no section');
  assert(el('history-groups').innerHTML.includes('data-group="cpu/" aria-pressed="false"'));
  clickGroup('memory/');
  assert.equal(drawn(), 0);
  assert(el('hist').innerHTML.includes('Every group is off'), 'the panel keeps its buttons with nothing drawn');
  assert.equal(el('history-panel').hidden, false);
  clickGroup('cpu/'); clickGroup('memory/');
  assert.equal(drawn(), 21);
  await switchTo('thermal'); await flush();
  assert.equal(el('history-groups').innerHTML, '', 'a view with one kind of series offers no buttons');
  console.log('views, summaries, keyboard, shared polling, cached errors and hidden-page suspension: passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
