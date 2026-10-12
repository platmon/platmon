// Optional endpoint concurrency and tab-return tests with real AbortController and fake time.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const elements = {}, requests = [], timers = new Map(), intervals = [], listeners = [];
let now = 0, next = 0;
const context = vm.createContext({
  AbortController, console, performance: {now: () => now},
  location: {hash: ''}, history: {replaceState() {}}, window: {addEventListener() {}},
  document: {visibilityState: 'visible', getElementById: id => elements[id] ||= {innerHTML: '', textContent: '', value: '',
    setAttribute() {}, addEventListener() {}, focus() {}, scrollIntoView() {}},
    querySelectorAll: () => [], addEventListener: (_, fn) => listeners.push(fn)},
  setTimeout: (fn, ms) => { timers.set(++next, {fn, ms}); return next; },
  clearTimeout: id => timers.delete(id), setInterval: fn => intervals.push(fn),
  fetch: (url, options) => new Promise((resolve, reject) => {
    const request = {url, options, resolve, reject}; requests.push(request);
    options?.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
  }),
});
const flush = async () => { for (let n = 0; n < 6; n++) await new Promise(setImmediate); };
const count = path => requests.filter(r => r.url.startsWith(path)).length;
(async () => {
  vm.runInContext(code, context);
  vm.runInContext("activateTab('network')", context);
  await flush();
  now = 11000; intervals.forEach(fn => fn()); await flush();
  assert.equal(count('api/observations'), 1, 'a hanging observation request must not overlap another');
  assert.equal(count('api/history'), 1, 'a hanging history request must not overlap another');
  for (const [id, t] of [...timers]) if (t.ms === 5000) { timers.delete(id); t.fn(); }
  await flush();
  assert(requests.every(r => r.options.signal.aborted), 'all endpoints have a deadline');
  now = 22000; intervals.forEach(fn => fn()); await flush();
  assert.equal(count('api/observations'), 2);
  assert.equal(count('api/history'), 2);
  context.document.visibilityState = 'hidden'; listeners.forEach(fn => fn());
  now += 600000;
  context.document.visibilityState = 'visible'; listeners.forEach(fn => fn()); await flush();
  intervals.forEach(fn => fn()); await flush();
  assert.equal(count('api/observations'), 3, 'tab return refreshes observations');
  assert.equal(count('api/history'), 3, 'tab return refreshes history');
  // Delay body completion past a tab return. Abort may already have raced with a
  // response, so epoch rejection must protect both endpoints independently.
  const pending = requests.filter(r => r.url.startsWith('api/observations') || r.url.startsWith('api/history')).slice(-2);
  const bodies = [];
  pending.forEach(r => r.resolve({ok: true, status: 200, json: () => new Promise(resolve => bodies.push(resolve))}));
  await flush();
  listeners.forEach(fn => fn());
  intervals.forEach(fn => fn()); await flush();
  assert.equal(count('api/observations'), 3, 'body cleanup still owns the observation request');
  assert.equal(count('api/history'), 3, 'body cleanup still owns the history request');
  bodies.forEach(resolve => resolve({obsolete: true})); await flush();
  assert.equal(vm.runInContext('obs', context), null, 'old observation body is ignored');
  assert.equal(vm.runInContext('hist', context), null, 'old history body is ignored');
  intervals.forEach(fn => fn()); await flush();
  assert.equal(count('api/observations'), 4);
  assert.equal(count('api/history'), 4);
  const fresh = requests.filter(r => r.url.startsWith('api/observations') || r.url.startsWith('api/history')).slice(-2);
  fresh.forEach(r => r.resolve({ok: true, status: 200, json: async () => ({groups: {}, series: {}, fresh: true})}));
  await flush();
  assert.equal(vm.runInContext('obs.fresh && hist.fresh', context), true);
  now += 11000; intervals.forEach(fn => fn()); await flush();
  const failed = requests.filter(r => r.url.startsWith('api/observations') || r.url.startsWith('api/history')).slice(-2);
  failed.forEach(r => r.resolve({ok: false, status: 503})); await flush();
  assert.equal(vm.runInContext('obs', context), null);
  assert.equal(vm.runInContext('hist', context), null);
  assert.equal(elements.sto.innerHTML + elements.wifi.innerHTML + elements.probe.innerHTML + elements.hist.innerHTML, '');
  now += 11000; intervals.forEach(fn => fn()); await flush();
  const absent = requests.filter(r => r.url.startsWith('api/observations') || r.url.startsWith('api/history')).slice(-2);
  absent.forEach(r => r.resolve({ok: false, status: 404})); await flush();
  const totals = [count('api/observations'), count('api/history')];
  now += 11000; listeners.forEach(fn => fn()); intervals.forEach(fn => fn()); await flush();
  assert.deepEqual([count('api/observations'), count('api/history')], totals, '404 stops polling after tab return too');
  assert.equal(vm.runInContext('obsRequest', context), null);
  assert.equal(vm.runInContext('histRequest', context), null);
  console.log('optional endpoint concurrency, deadline and tab return: passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
