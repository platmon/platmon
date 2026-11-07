// The web page's Needs attention entries for a snapshot: prints [name, value, level] in display order.
// Usage: node web_attention_harness.js app.js case.json   (case: {stats, observations, levels})
const fs = require('node:fs'), vm = require('node:vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const elements = {};
const context = vm.createContext({AbortController, console, performance: {now: () => 0},
  location: {hash: ''}, history: {replaceState() {}}, window: {addEventListener() {}},
  document: {visibilityState: 'hidden', getElementById: id => elements[id] ||= {innerHTML: '', textContent: '', value: '',
    setAttribute() {}, addEventListener() {}, focus() {}, scrollIntoView() {}},
    querySelectorAll: () => [], addEventListener() {}},
  setTimeout: () => 0, clearTimeout() {}, setInterval() {}, fetch: () => new Promise(() => {})});
elements.levels = {textContent: JSON.stringify(input.levels)};  // as the server fills it in from [thresholds]
vm.runInContext(code, context);
context.input = input;
const html = vm.runInContext('obs = input.observations; obsAt = 0; attentionHtml(input.stats)', context);
const pills = [...html.matchAll(/<a class="pill (warn|crit)" href="#\w+">(.*?) <span class="mono">(.*?)<\/span><\/a>/g)];
console.log(JSON.stringify(pills.map(([, lv, name, value]) => [name, value, lv])));
