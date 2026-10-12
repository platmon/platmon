const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let now = 0;
const elements = {}, intervals = [], age = {dataset: {historyAge: '2000'}, textContent: ''};
const context = vm.createContext({AbortController, performance: {now: () => now}, console,
  location: {hash: ''}, history: {replaceState() {}}, window: {addEventListener() {}},
  document: {visibilityState: 'visible', getElementById: id => elements[id] ||= {innerHTML:'',textContent:'',value:'',
    setAttribute() {}, addEventListener() {}, focus() {}, scrollIntoView() {}},
    querySelectorAll: selector => selector === '[data-history-age]' ? [age] : [], addEventListener() {}},
  setInterval: fn => intervals.push(fn), setTimeout() {}, clearTimeout() {},
  fetch: async url => url.startsWith('api/history') ? {ok:true, status:200, json:async() => ({retention_s:600,
    series:{'memory/used_bytes':[[1,2000,1024]]}})} : {ok:false,status:404}});
(async () => {
  vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
  vm.runInContext("activateTab('resources')", context);
  await new Promise(setImmediate);
  assert(elements.hist.innerHTML.includes('latest point 2 s ago'));
  const originalGraph = elements.hist.innerHTML;
  now = 4000; intervals.forEach(fn => fn()); await new Promise(setImmediate);
  assert.equal(age.textContent, 'latest point 6 s ago');
  assert.equal(elements.hist.innerHTML, originalGraph, 'age updates do not recreate SVG');
  context.document.visibilityState = 'hidden';
  now = 5000; intervals.forEach(fn => fn()); await new Promise(setImmediate);
  assert.equal(age.textContent, 'latest point 6 s ago', 'hidden tabs skip rendering');
  context.document.visibilityState = 'visible';
  now = 6000; intervals.forEach(fn => fn()); await new Promise(setImmediate);
  assert.equal(age.textContent, 'latest point 8 s ago');
  assert.equal(elements.hist.innerHTML, originalGraph);
  console.log('cached history age advances without another response: passed');
})().catch(e => {console.error(e); process.exitCode = 1;});
