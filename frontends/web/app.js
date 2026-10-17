const $ = id => document.getElementById(id);
const gib = b => (b / 2 ** 30).toFixed(1) + 'G';
const SYSTEM_FIELDS = ['os', 'kernel', 'arch', 'hostname', 'l4t'];
const esc = v => String(v).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const kv = (k, v) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`;
// a grid table: cols is the CSS column template; a row is its cells' HTML, idle rows are dimmed
const tr = (cells, cls = '') => `<div class="tr${cls}">${cells.map(c => `<span>${c}</span>`).join('')}</div>`;
const table = (cols, head, rows, minWidth = 0) => `<div class="table"><div style="--cols:${cols}${minWidth ? `;min-width:${minWidth}px` : ''}">` +
  tr(head, ' th') + rows.join('') + '</div></div>';
const muted = text => `<span class="muted">${text}</span>`;

// Status levels: ok below warn, warn from warn, crit from crit. Instantaneous values, no persistence.
// The server fills them in from [thresholds] in platmon.ini, temperature with its board's default when
// not set there (platmon.py BOARD_TEMPERATURE); these generic defaults apply only to a page not served by platmon.
const LIMITS = {cpu: [80, 90], gpu: [80, 90], memory: [80, 90], filesystem: [80, 90], temperature: [80, 90]};
try { Object.assign(LIMITS, JSON.parse($('levels').textContent)); } catch (e) {}  // not served by platmon
LIMITS.temperature ||= [80, 90];
const level = (v, [warn, crit]) => v >= crit ? 'crit' : v >= warn ? 'warn' : 'ok';
const textLevel = lv => lv === 'ok' ? '' : ' t-' + lv;
const pctOf = (used, total) => total ? 100 * used / total : 0;
// a bar on a 0–100 scale; limits add the level color and, unless ticks is false, two threshold ticks
const bar = (pct, limits, ticks = true) => `<div class="bar"><div class="${limits ? level(pct, limits) : 'ok'}" style="width:${Math.max(0, Math.min(pct, 100)).toFixed(1)}%"></div>` +
  (limits && ticks ? limits.map(t => `<span style="left:${t}%"></span>`).join('') : '') + '</div>';
const meter = (label, text, pct, limits) =>
  `<div class="meter"><div class="meter-head"><span>${label}</span><span>${text}</span></div>${bar(pct, limits)}</div>`;
const mhz = hz => hz ? (hz / 1e6).toFixed(0) + ' MHz' : '';
const uptimeText = seconds => {
  const up = Math.floor(seconds);
  return `${Math.floor(up / 86400)}d ${String(Math.floor(up % 86400 / 3600)).padStart(2, '0')}:${String(Math.floor(up % 3600 / 60)).padStart(2, '0')}`;
};

const TABS = ['overview', 'resources', 'network', 'storage', 'thermal', 'power', 'system'];
let activeTab = 'overview', latestStats = null;
// The views whose cards come from /api/observations. A view holding such a card must be listed here, or
// the page never asks for them while that view is open and the card stays empty: the fetch is gated on
// the view so a view that shows none costs no request (see pollObs).
const OBS_TABS = ['network', 'storage', 'power'];
const wantsObs = () => OBS_TABS.includes(activeTab);
const visible = () => document.visibilityState === 'visible';
function activateTab(tab, save = true) {
  if (!TABS.includes(tab)) return;
  activeTab = tab;
  for (const name of TABS) {
    $('tab-' + name).setAttribute('aria-selected', String(name === tab));
    $('tab-' + name).tabIndex = name === tab ? 0 : -1;
    $('panel-' + name).hidden = name !== tab;
  }
  if (save && location.hash !== '#' + tab) history.replaceState(history.state, '', '#' + tab);
  $('tab-' + tab).scrollIntoView({block: 'nearest', inline: 'nearest'});
  // Switching views reuses data and polling deadlines, never creates another timer.
  if (!visible()) return;
  renderActive(); renderObservations(); renderHistory();
  pollObs(); pollHist();
}
// why CPU rows are missing: the service compares /proc/stat readings, so the first one has none (same as cli.py)
const CPU_REASONS = {warmup: 'warming up', counter_regressed: 'counter went backwards', no_ticks: 'no ticks counted',
  invalid_interval: 'invalid interval', gap: 'restarting after a gap'};
function cpuNote(s) {
  const groups = {};
  for (const u of (s.cpu_sampling && s.cpu_sampling.unavailable) || [])
    (groups[CPU_REASONS[u.reason] || u.reason] ||= []).push('CPU' + u.id);
  const whys = Object.keys(groups);
  if (!whys.length) return '';
  return 'CPU sampling: ' + (!s.cpu.length && whys.length === 1 ? whys[0]
    : whys.map(why => groups[why].join(' ') + ' ' + why).join('; '));
}

// network interfaces and disks with the rates the server computed (same wording as cli.py); a counter
// without a rate says why, it is never shown as 0. I/O time is the share of the window with I/O in flight.
const RATE_REASONS = {warmup: 'warming up', gap: 'restarting after a gap', counter_regressed: 'counter went backwards',
  invalid_interval: 'invalid interval', identity_unavailable: 'identity unknown', namespace_changed: 'namespace changed'};
const perS = v => {
  let i = 0;
  while (Math.abs(v) >= 1024 && i < 3) { v /= 1024; i++; }
  return (i ? v.toFixed(1) : v.toFixed(0)) + ' ' + ['B/s', 'KiB/s', 'MiB/s', 'GiB/s'][i];
};
const why = x => esc(RATE_REASONS[x.reason] || x.reason);
const listOf = (obj, key) => obj && Array.isArray(obj[key]) ? obj[key] : [];
const idle = r => r && !r.rx_bytes_per_s && !r.tx_bytes_per_s ? ' idle' : '';
const netCaption = s => {
  const timed = listOf(s.network, 'interfaces').find(i => Number.isFinite(i.window_ms));
  return "This process's network namespace" + (timed ? ` · window ${(timed.window_ms / 1000).toFixed(3)} s` : '') + ' · errors and drops are totals';
};
function netRows(s) {
  const items = listOf(s.network, 'interfaces');
  return items.length ? table('minmax(90px, 1fr) 110px 110px 150px 110px',
    ['Interface', '↓ Receive', '↑ Send', 'Packets/s rx · tx', 'Errors · drops'], items.map(i => {
      const r = i.rates, total = k => ((i.rx || {})[k] || 0) + ((i.tx || {})[k] || 0);
      const errs = `${total('errors')} · ${total('dropped')}`;
      return tr([esc(i.name), r ? perS(r.rx_bytes_per_s) : why(i), r ? perS(r.tx_bytes_per_s) : '',
        r ? muted(`${r.rx_packets_per_s.toFixed(1)} · ${r.tx_packets_per_s.toFixed(1)}`) : '',
        total('errors') || total('dropped') ? `<span class="t-warn">${errs}</span>` : muted(errs)], idle(r));
    }), 600) : '';
}
function diskRows(s) {
  const items = listOf(s.disk_io, 'disks');
  return items.length ? table('minmax(90px, 1fr) 110px 110px 140px 80px', ['Disk', 'Read', 'Write', 'Ops/s r · w', 'I/O time'],
    items.map(d => {
      const r = d.rates;
      return tr([esc(d.name), r ? perS(r.read_bytes_per_s) : why(d), r ? perS(r.write_bytes_per_s) : '',
        r ? muted(`${r.reads_per_s.toFixed(1)} · ${r.writes_per_s.toFixed(1)}`) : '', r ? (100 * r.io_time_ratio).toFixed(1) + '%' : '']);
    }), 560) : '';
}

// Low-frequency groups (/api/observations) change on their own cadence: asked for at most every 10 s, only
// while a view needs them, on the same 1 s timer as the age display; a 404 is not asked again.
// Their age is the server's data_age_ms plus browser time since that answer, like the sample age.
let obs = null, obsAt = 0, obsAsked = -Infinity, obsSupported = true, obsRequest = null;
async function pollObs() {
  const now = performance.now();
  if (obsRequest || !obsSupported || !visible() || !wantsObs() || now - obsAsked < 10000) return;
  obsAsked = now;
  const ctl = obsRequest = new AbortController(), mine = epoch;
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch('api/observations', {signal: ctl.signal, cache: 'no-store'});
    const body = r.ok ? await r.json() : null;
    if (mine !== epoch || ctl.signal.aborted) return;
    if (r.status === 404) { obsSupported = false; obs = null; }
    else if (r.ok) { obs = body; obsAt = performance.now(); }
    else obs = null;
  } catch (e) { if (mine === epoch) obs = null; } finally {
    clearTimeout(timer); obsRequest = null;
  }  // the main view goes on without it
  renderObservations();
}
function observationStale(g) {
  const o = g.observation;
  return o.stale || (Number.isFinite(g.stale_after_ms) && o.data_age_ms + performance.now() - obsAt > g.stale_after_ms);
}
function observationAge(group) {
  const g = obs && obs.groups && obs.groups[group];
  if (!g || !g.observation) return '';
  const age = Math.round((g.observation.data_age_ms + performance.now() - obsAt) / 1000);
  return `observed ${age} s ago${observationStale(g) ? ' (not current)' : ''}`;
}
// TCP connect time to the configured targets (not ICMP ping): the latest attempt, and recent failures
function probeRows() {
  const g = obs && obs.groups && obs.groups.probe, o = g && g.observation, d = g && g.data;
  const items = d && Array.isArray(d.targets) ? d.targets : [];
  if (!o || !items.length) return '';
  return table('minmax(0, 1fr) 80px 80px', ['Target', 'Latest', 'Failed'], items.map(t =>
    tr([esc(t.address.includes(':') ? `[${t.address}]:${t.port}` : `${t.address}:${t.port}`),
      t.connect_ms != null ? t.connect_ms.toFixed(1) + ' ms' : esc(t.reason),
      `<span class="${t.failures ? 't-warn' : 'muted'}">${t.failures}/${t.attempts}</span>`])));
}
// the signal while connected, never the last one after; a value without dBm is shown without a unit or bar
function wifiRows() {
  const g = obs && obs.groups && obs.groups.wifi, o = g && g.observation, d = g && g.data;
  const items = d && Array.isArray(d.interfaces) ? d.interfaces : [];
  if (!o || !items.length) return '';
  return '<div class="meters">' + items.map(i => {
    const state = i.connected !== true ? (i.connected === false ? 'not connected' : esc(i.connection_state || 'unknown'))
      : i.signal_dbm != null ? i.signal_dbm + ' dBm' : i.signal_raw != null ? `signal ${i.signal_raw} (unit not reported)` : 'signal n/a';
    return `<div class="meter"><div class="meter-head"><span class="mono">${esc(i.name)}</span><span>${state}</span></div>` +
      (i.connected === true && i.signal_dbm != null ? bar((i.signal_dbm + 90) / 60 * 100) : '') +
      (i.connected === true ? `<div class="detail">Connected${i.link_quality != null ? ' · link quality ' + i.link_quality : ''}</div>` : '') + '</div>';
  }).join('') + '</div>';
}
// batteries and external supplies, observed on their own slow cadence: one reading is one firmware call,
// so the age is shown. No threshold colouring: for a battery a low reading is the bad one, the opposite of
// every level() reading, and [thresholds] has no battery entry.
function batteryRows() {
  const g = obs && obs.groups && obs.groups.battery, o = g && g.observation, d = g && g.data;
  const cells = d && Array.isArray(d.batteries) ? d.batteries : [];
  const external = d && Array.isArray(d.external) ? d.external : [];
  if (!o || !(cells.length || external.length)) return '';
  const rows = cells.map(b => {
    const head = b.present === false ? 'not present'
      : b.percent != null ? b.percent + ' %' : esc(b.capacity_level || 'charge n/a');
    const open = `<div class="meter"><div class="meter-head"><span class="mono">${esc(b.name)}</span><span>${head}</span></div>`;
    if (b.present === false) return open + '</div>';
    const full = b.charge_full_uah || b.energy_full_uwh, design = b.charge_full_design_uah || b.energy_full_design_uwh;
    const detail = [b.voltage_uv != null ? (b.voltage_uv / 1e6).toFixed(2) + ' V' : '',
      b.current_ua != null ? (b.current_ua / 1e6).toFixed(2) + ' A' : '',
      b.power_uw != null ? (b.power_uw / 1e6).toFixed(2) + ' W' : '',
      b.cycle_count != null ? b.cycle_count + ' cycles' : '',
      full && design ? Math.round(full * 100 / design) + ' % of design' : ''].filter(Boolean).join(' · ');
    return open + (b.percent != null ? bar(b.percent) : '') +
      `<div class="detail">${esc(b.status || 'status n/a')}${detail ? ' · ' + detail : ''}</div></div>`;
  });
  if (external.length) {
    rows.push('<div class="meter"><div class="meter-head"><span>External power</span></div><div class="detail">' +
      external.map(e => `${esc(e.name)} ${e.online ? 'on' : e.online === false ? 'off' : 'n/a'}`).join(' · ') +
      '</div></div>');
  }
  return '<div class="meters">' + rows.join('') + '</div>';
}
const fsPct = f => f.total_bytes ? pctOf(f.used_bytes, f.total_bytes) : -1;
function storageRows() {
  const g = obs && obs.groups && obs.groups.storage, o = g && g.observation, d = g && g.data;
  if (!o || !d) return '';
  const fss = Array.isArray(d.filesystems) ? d.filesystems : [], parts = Array.isArray(d.partitions) ? d.partitions : [];
  if (!fss.length && !parts.length) return '';
  return [...fss].sort((a, b) => fsPct(b) - fsPct(a)).map(f => {
    const m = f.mount_points, pct = fsPct(f);
    return '<div class="fs"><div><div class="fs-name"><span>' + esc(m[0].path) + '</span>' +
      (m[0].read_only ? '<span class="badge">ro</span>' : '') + (m.length > 1 ? muted(`+${m.length - 1} more`) : '') + '</div>' +
      `<div class="detail">${esc(f.fstype)} · ${esc(f.device || f.major + ':' + f.minor)}</div></div>` +
      (f.total_bytes != null ? meter(`${gib(f.used_bytes)} / ${gib(f.total_bytes)} · ${pct.toFixed(0)}%`,
        `${gib(f.available_bytes)} free`, pct, LIMITS.filesystem) : muted('capacity unknown')) + '</div>';
  }).join('') + (parts.length ? '<h3>Partitions</h3>' + table('minmax(0, 1fr) 80px minmax(0, 1fr)', ['Partition', 'Size', 'Mounted at'],
    parts.map(p => tr([esc(p.name), gib(p.size_bytes), p.mount_points.length ? esc(p.mount_points[0] +
      (p.mount_points.length > 1 ? ` +${p.mount_points.length - 1} more` : '')) : muted('not mounted')]))) : '');
}

// One bounded history cache shared across detail views, fetched at most every 10 s.
// Overview does not fetch history. A null is a gap in the line, never 0.
let hist = null, histAt = 0, histAsked = -Infinity, histSupported = true, histRequest = null;
const HIST_PREFIXES = {resources: ['cpu/', 'gpu/', 'npu/', 'memory/'], network: ['network/', 'observation/wifi/', 'observation/probe/'],
  storage: ['disk_io/', 'observation/storage/'], thermal: ['temperature/'],
  power: ['power/', 'observation/battery/']};
// One button per kind of series a view can draw, in this order, labelled with how many it covers. A
// viewer turns a kind off to take its graphs out of the list; single series are not picked one at a time.
const HIST_GROUPS = [['cpu/', 'CPU cores'], ['gpu/', 'GPU'], ['npu/', 'NPU'], ['memory/', 'RAM'], ['network/', 'Interfaces'],
  ['observation/wifi/', 'Wi-Fi'], ['observation/probe/', 'TCP connect'], ['disk_io/', 'Disk I/O'],
  ['observation/storage/', 'Filesystems'], ['temperature/', 'Temperature'], ['power/', 'Power rails'],
  ['observation/battery/', 'Battery']];
const histOff = new Set();  // kinds a viewer turned off; every kind of a view starts on
const wantsHistory = () => !!HIST_PREFIXES[activeTab];
const historyMatches = id => (HIST_PREFIXES[activeTab] || []).some(prefix => id.startsWith(prefix));
const HIST_LABEL = [[/^cpu\/(\d+)\/usage$/, m => 'CPU' + m[1]],
  [/^gpu\/usage$/, () => 'GPU'],
  [/^npu\/usage$/, () => 'NPU'],
  [/^observation\/storage\/([^/]+)\/[^/]+\/(used_bytes|available_bytes)$/, m => `FS ${m[1]} ${m[2] === 'used_bytes' ? 'used' : 'free'}`],
  [/^observation\/wifi\/[^/]+\/\d+\/([^/]+)\/signal_dbm$/, m => `${m[1]} signal`],
  [/^observation\/probe\/([^/]+)\/connect_ms$/, m => `TCP ${m[1]}`],
  [/^observation\/battery\/([^/]+)\/percent$/, m => `${m[1]} charge`],
  [/^observation\/battery\/([^/]+)\/charge_now_uah$/, m => `${m[1]} charge now`],
  [/^observation\/battery\/([^/]+)\/energy_now_uwh$/, m => `${m[1]} energy now`],
  [/^observation\/battery\/([^/]+)\/power_uw$/, m => `${m[1]} draw`],
  [/^memory\/used_bytes$/, () => 'RAM used'],
  [/^temperature\/name:(.+)$/, m => 'temp ' + m[1]],
  [/^temperature\/(.+)$/, m => 'temp ' + sensorName('temperature', m[1])],
  [/^power\/name:(.+)$/, m => 'rail ' + m[1]],
  [/^power\/(.+)$/, m => 'rail ' + sensorName('power', m[1])],
  [/^network\/[^/]+\/\d+\/([^/]+)\/(rx|tx)_bytes_per_s$/, m => `${m[1]} ${m[2]}`],
  [/^disk_io\/[^/]+\/([^/]+)\/(read|write)_bytes_per_s$/, m => `${m[1]} ${m[2]}`]];
// History keys a sensor by its source id (docs/history.md); its name is the sensor_meta pointer with that id
function sensorName(group, sid) {
  const meta = latestStats && latestStats.sensor_meta && typeof latestStats.sensor_meta === 'object' ? latestStats.sensor_meta : {};
  const ptr = Object.keys(meta).find(p => p.startsWith('/' + group + '/') && meta[p] && meta[p].id === sid);
  return ptr ? ptr.slice(group.length + 2).replace(/~1/g, '/').replace(/~0/g, '~') : sid;
}
const histValue = (id, v) => id.startsWith('temperature') ? v.toFixed(1) + ' °C'
  : id.startsWith('power/') ? v.toFixed(2) + ' W'
  : id.endsWith('/percent') ? v.toFixed(0) + '%'
  : id.endsWith('_uah') ? (v / 1e3).toFixed(0) + ' mAh' : id.endsWith('_uwh') ? (v / 1e3).toFixed(0) + ' mWh'
  : id.endsWith('_uw') ? (v / 1e6).toFixed(2) + ' W'
  : id.startsWith('cpu/') || id.startsWith('gpu/') || id.startsWith('npu/') ? v.toFixed(1) + '%'
  : id.includes('/wifi/') ? v.toFixed(1) + ' dBm' : id.includes('/probe/') ? v.toFixed(1) + ' ms'
  : id.startsWith('memory') || id.includes('/storage/') ? gib(v) : perS(v);
// x of a point on the 0–200 chart: spaced by its age, so irregular intervals stay irregular
const histX = (points, p) => {
  const oldest = points[0][1], newest = points[points.length - 1][1];
  return oldest > newest ? 200 * (oldest - p[1]) / (oldest - newest) : 0;
};
function spark(points) {
  const vs = points.map(p => p[2]).filter(v => v != null), lo = Math.min(...vs), hi = Math.max(...vs);
  const svg = paths => `<svg class="spark" viewBox="0 0 200 28" preserveAspectRatio="none">${paths}</svg>`;
  if (!vs.length) return svg('');  // keeps the chart's height
  const y = v => hi > lo ? 26 - 24 * (v - lo) / (hi - lo) : 14;
  let line = '', area = '', run = [];
  const close = () => { if (run.length > 1) area += `M${run[0][0]},28L${run.map(p => p.join(',')).join('L')}L${run[run.length - 1][0]},28Z`; run = []; };
  points.forEach(p => {
    if (p[2] == null) { close(); return; }
    const xy = [histX(points, p).toFixed(1), y(p[2]).toFixed(1)];
    line += `${run.length ? 'L' : 'M'}${xy.join(',')}`;
    run.push(xy);
  });
  close();
  return svg((area ? `<path class="area" d="${area}"/>` : '') + `<path class="line" d="${line}"/>`);
}
function chartOf(id, points, rule) {
  const latest = points[points.length - 1], last = latest[2], vs = points.map(p => p[2]).filter(v => v != null);
  const lo = vs.length && histValue(id, Math.min(...vs)), hi = vs.length && histValue(id, Math.max(...vs));
  return `<div class="card chart"><div class="chart-head"><span>${esc(rule[1](id.match(rule[0])))}</span><span>${last == null ? 'no value' : histValue(id, last)}</span></div>` +
    `<div class="plot" data-series="${esc(id)}">${spark(points)}</div>` +
    `<div class="chart-foot"><span>${!vs.length ? '' : lo === hi ? lo : lo + ' – ' + hi}</span>` +
    `<span data-history-age="${Number(latest[1])}">${historyAge(latest[1])}</span></div></div>`;
}
// One section per kind of series, in the button row's order: a single RAM graph never sits in a wall of cores.
function histRows() {
  const series = hist && hist.series && typeof hist.series === 'object' ? hist.series : {};
  const sections = [];
  for (const [prefix, label] of HIST_GROUPS) {
    if (histOff.has(prefix)) continue;
    const rows = [];
    for (const [id, points] of Object.entries(series)) {
      const rule = HIST_LABEL.find(([re]) => re.test(id));
      if (!rule || !historyMatches(id) || histGroup(id) !== prefix
        || !Array.isArray(points) || !points.length) continue;
      rows.push(chartOf(id, points, rule));
    }
    if (rows.length) sections.push(`<div class="hist-group"><h3>${esc(label)}</h3><div>${rows.join('')}</div></div>`);
  }
  // Every kind off leaves the panel with its buttons, so the viewer can turn one back on.
  return sections.join('') || (histMatching().length ? '<div class="muted">Every group is off · turn one on above</div>' : '');
}
// The series this view can draw, labelled ones only: the list and the buttons agree on it.
const histMatching = () => Object.keys((hist && hist.series && typeof hist.series === 'object' ? hist.series : {}))
  .filter(id => historyMatches(id) && HIST_LABEL.some(([re]) => re.test(id)));
const histGroup = id => (HIST_GROUPS.find(([prefix]) => id.startsWith(prefix)) || [])[0];
function histButtons() {
  const ids = histMatching();
  const groups = HIST_GROUPS.map(([prefix, label]) => [prefix, label, ids.filter(id => id.startsWith(prefix)).length])
    .filter(([, , count]) => count);
  // A view with one kind has nothing to filter against, so it gets no buttons.
  $('history-groups').innerHTML = groups.length < 2 ? '' : groups.map(([prefix, label, count]) =>
    `<button data-group="${prefix}" aria-pressed="${!histOff.has(prefix)}">${esc(label)} ${count}</button>`).join('');
}
const histDraw = () => { $('hist').innerHTML = histRows(); histButtons(); };
const historyAge = age => `latest point ${Math.round((age + performance.now() - histAt) / 1000)} s ago`;
function renderHistory() {
  if (!visible()) return;
  $('history-panel').hidden = !wantsHistory() || !hist;
  if ($('history-panel').hidden) return;
  $('history-cap').textContent = `Last ${Math.round(hist.retention_s / 60)} min · each chart has its own scale · gaps = no sample`;
  histDraw();
  $('history-panel').hidden = !$('hist').innerHTML;
}
// hover or touch: the nearest point's value and age above the chart
function readout(event) {
  document.querySelectorAll('#hist .cursor, #hist .tip').forEach(e => e.remove());
  const plot = event.target.closest && event.target.closest('.plot');
  const id = plot && plot.dataset.series, points = id && hist && hist.series[id];
  if (!points) return;
  const box = plot.getBoundingClientRect(), at = 200 * (event.clientX - box.left) / box.width;
  let best = null;
  for (const p of points) if (p[2] != null && (!best || Math.abs(histX(points, p) - at) < Math.abs(histX(points, best) - at))) best = p;
  if (!best) return;
  const x = histX(points, best) / 2, shift = x < 15 ? 0 : x > 85 ? -100 : -50;
  const when = best === points[points.length - 1] ? 'now' : Math.round((best[1] + performance.now() - histAt) / 1000) + ' s ago';
  plot.insertAdjacentHTML('beforeend', `<span class="cursor" style="left:${x}%"></span>` +
    `<span class="tip" style="left:${x}%;transform:translateX(${shift}%)">${esc(histValue(id, best[2]))} · ${when}</span>`);
}
async function pollHist() {
  const now = performance.now();
  if (histRequest || !histSupported || !visible() || !wantsHistory() || now - histAsked < 10000) return;
  histAsked = now;
  const ctl = histRequest = new AbortController(), mine = epoch;
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch('api/history?points=120', {signal: ctl.signal, cache: 'no-store'});
    const body = r.ok ? await r.json() : null;
    if (mine !== epoch || ctl.signal.aborted) return;
    if (r.status === 404) { histSupported = false; hist = null; }
    else if (r.ok) { hist = body; histAt = performance.now(); }
    else hist = null;
  } catch (e) { if (mine === epoch) hist = null; } finally {
    clearTimeout(timer); histRequest = null;
  }
  renderHistory();
}

// pressure stall information: the kernel's 10 s averages of the share of time tasks were stalled (not usage)
function psiRows(s) {
  const res = s.pressure && s.pressure.resources && typeof s.pressure.resources === 'object' ? s.pressure.resources : {};
  const rows = Object.entries(res).map(([name, r]) => tr([esc(name), r.some.avg10.toFixed(1) + '%', muted(r.full ? r.full.avg10.toFixed(1) + '%' : '—')]));
  return rows.length ? table('minmax(0, 1fr) 80px 80px', ['Resource', 'Some', 'Full'], rows) : '';
}

// optional groups that could not read everything, as in cli.py; nothing for absent sensors or older servers
function collectionNote(s) {
  const c = s.collectors && typeof s.collectors === 'object' ? s.collectors : {};
  return Object.entries(c).filter(([name, v]) => name !== 'core' && v && (v.state === 'partial' || v.state === 'error'))
    .map(([name, v]) => name + ' ' + v.state).join(', ');
}

function hideEmptyPanels() {
  document.querySelectorAll('[data-card]').forEach(sec => sec.hidden = !sec.lastElementChild.innerHTML);
  $('network-empty').hidden = ['net', 'wifi', 'probe'].some(id => $(id).innerHTML);
  $('thermal-empty').hidden = !!$('temp').innerHTML;
  $('power-empty').hidden = ['pwr', 'batt'].some(id => $(id).innerHTML);
}

function overviewRows(s) {
  const cards = [], alerts = [];
  // value null: unavailable, no bar; limits give the level, the threshold ticks and a Needs attention entry
  const metric = (label, sub, value, unit, limits, detail, tab, name = label) => {
    const lv = value == null ? 'ok' : level(value, limits);
    if (lv !== 'ok') alerts.push({lv, tab, name, value: value.toFixed(1) + (unit === '°C' ? ' °C' : unit)});
    cards.push(`<article class="card metric"><div class="metric-head"><span>${esc(label)}</span><span>${esc(sub)}</span></div>` +
      (value == null ? '<div class="value">Unavailable</div>'
        : `<div class="value${textLevel(lv)}">${value.toFixed(1)}<span class="unit">${unit}</span></div>${bar(value, limits)}`) +
      (detail ? `<div class="detail">${esc(detail)}</div>` : '') + '</article>');
  };
  const cpus = s.cpu.filter(c => Number.isFinite(c.usage)), m = s.memory, d = s.disk, note = cpuNote(s);
  metric('CPU', s.cpu.length + (s.cpu.length === 1 ? ' core' : ' cores'),
    cpus.length ? cpus.reduce((sum, c) => sum + c.usage, 0) / cpus.length : null, '%', LIMITS.cpu,
    `Mean of ${cpus.length} measured cores` + (note ? ' · ' + note : ''), 'resources');
  metric('Memory', gib(m.total), pctOf(m.used, m.total), '%', LIMITS.memory, `${gib(m.used)} used · ${gib(m.total - m.used)} free`, 'resources');
  if (s.gpu) metric('GPU', mhz(s.gpu.freq), s.gpu.usage, '%', LIMITS.gpu, '', 'resources');
  metric('Root filesystem', gib(d.total), pctOf(d.used, d.total), '%', LIMITS.filesystem, `${gib(d.used)} used · ${gib(d.total - d.used)} free`, 'storage', '/');
  const temps = Object.entries(s.temperature).filter(([, value]) => Number.isFinite(value)).sort((a, b) => b[1] - a[1]);
  if (temps.length) {
    metric('Temperature', temps[0][0], temps[0][1], '°C', LIMITS.temperature, `Hottest of ${temps.length} ${temps.length === 1 ? 'sensor' : 'sensors'}`, 'thermal', temps[0][0]);
    for (const [name, value] of temps.slice(1)) {  // every hot sensor, not only the hottest
      const lv = level(value, LIMITS.temperature);
      if (lv !== 'ok') alerts.push({lv, tab: 'thermal', name, value: value.toFixed(1) + ' °C'});
    }
  }
  // other filesystems, from the storage observation when the Storage view fetched it; the root card covers /
  const storage = obs && obs.groups && obs.groups.storage;
  const fsData = storage && storage.observation && !observationStale(storage) ? storage.data : null;
  for (const f of listOf(fsData, 'filesystems')) {
    const lv = f.total_bytes && !f.mount_points.some(p => p.path === '/') ? level(fsPct(f), LIMITS.filesystem) : 'ok';
    if (lv !== 'ok') alerts.push({lv, tab: 'storage', name: f.mount_points[0].path, value: fsPct(f).toFixed(0) + '%'});
  }
  alerts.sort((a, b) => (b.lv === 'crit') - (a.lv === 'crit'));
  let html = alerts.length ? '<div class="card attention"><strong>Needs attention</strong>' + alerts.map(a =>
    `<a class="pill ${a.lv}" href="#${a.tab}">${esc(a.name)} <span class="mono">${esc(a.value)}</span></a>`).join('') + '</div>' : '';
  html += `<div class="metrics">${cards.join('')}</div>`;
  // Show individual readings: names and traffic volume do not identify the host's uplink.
  const traffic = i => i.rates ? i.rates.rx_bytes_per_s + i.rates.tx_bytes_per_s : -1;  // busiest first
  const interfaces = listOf(s.network, 'interfaces').filter(i => i.name !== 'lo').sort((a, b) => traffic(b) - traffic(a));
  if (interfaces.length) {
    const shown = interfaces.slice(0, 4);
    html += '<section class="card"><div class="card-head"><h2>Network</h2><a class="cap" href="#network">All interfaces →</a></div>' +
      table('minmax(0, 1fr) 110px 110px', ['Interface', '↓ Receive', '↑ Send'], shown.map(i => tr([esc(i.name),
        i.rates ? perS(i.rates.rx_bytes_per_s) : why(i), i.rates ? muted(perS(i.rates.tx_bytes_per_s)) : ''], idle(i.rates)))) +
      (shown.length < interfaces.length ? `<p class="note">Showing ${shown.length} of ${interfaces.length}</p>` : '') + '</section>';
  }
  return html;
}
function renderResources(s) {
  const note = cpuNote(s), cpus = s.cpu.filter(c => Number.isFinite(c.usage));
  const unavailable = (s.cpu_sampling && s.cpu_sampling.unavailable || []).length;
  $('cpu-cap').textContent = cpus.length ? `mean ${(cpus.reduce((sum, c) => sum + c.usage, 0) / cpus.length).toFixed(1)}% · ` +
    `${cpus.length} of ${s.cpu.length + unavailable} measured` : '';
  $('cpu').innerHTML = (s.cpu.length ? '<div class="cores">' + s.cpu.map(c => `<div class="core"><span>CPU${c.id}</span>` +
    `<strong class="${textLevel(level(c.usage, LIMITS.cpu))}">${c.usage.toFixed(1)}%</strong>` +
    bar(c.usage, LIMITS.cpu, false) + `<span>${mhz(c.freq) || '&nbsp;'}</span></div>`).join('') + '</div>' : '') +
    (note ? `<div class="muted">${esc(note)}</div>` : '');
  const m = s.memory, g = s.gpu, n = s.npu;
  // the NPU has no levels in [thresholds], so its bar carries none either
  const load = (a, label) => meter(label, (a.usage == null ? 'no load reading' : a.usage.toFixed(1) + '%') +
    (a.freq ? ' · ' + mhz(a.freq) : ''), a.usage == null ? 0 : a.usage, label === 'GPU' ? LIMITS.gpu : null);
  $('res').innerHTML = '<div class="meters">' +
    (g ? load(g, 'GPU') : '') + (n ? load(n, 'NPU') : '') +
    meter('RAM', `${gib(m.used)} / ${gib(m.total)}`, pctOf(m.used, m.total), LIMITS.memory) +
    (m.swap_total ? meter('Swap', `${gib(m.swap_used)} / ${gib(m.swap_total)}`, pctOf(m.swap_used, m.swap_total), LIMITS.memory) : '') + '</div>';
  $('psi').innerHTML = psiRows(s);
}
function renderThermal(s) {
  const temps = Object.entries(s.temperature).sort((a, b) => b[1] - a[1]);
  $('temp').innerHTML = temps.length ? '<div class="temps">' + temps.map(([k, v]) =>
    `<div class="temp"><span>${esc(k)}</span>${bar(v, LIMITS.temperature)}<span>${v.toFixed(1)} °C</span></div>`).join('') + '</div>' : '';
}
// what the board draws and what it has left, read together: consumption is the draw over a window
// against the charge that window cost
function renderPower(s) {
  const rows = Object.entries(s.power).map(([k, v]) => kv(esc(k), v.toFixed(2) + ' W')).join('') +
    s.fans.map(f => kv('Fan ' + esc(f.name), ([f.rpm !== null && f.rpm + ' rpm', f.percent !== null && f.percent + '%'].filter(Boolean).join(' · ') || 'n/a'))).join('');
  $('pwr-cap').innerHTML = s.power_mode ? `<span class="chip">MODE ${esc(s.power_mode)}</span>` : '';
  $('pwr').innerHTML = rows || (s.power_mode ? '<p class="muted">No power rails or fans reported.</p>' : '');
}
function renderSystem(s) {
  const sys = s.system || {};
  const fields = {platform: s.platform, uptime: uptimeText(s.uptime)};
  for (const key of SYSTEM_FIELDS) fields[key] = sys[key];
  for (const [key, value] of Object.entries(fields)) {
    const text = value == null ? '' : String(value);
    if ($('system-' + key).textContent !== text) $('system-' + key).textContent = text;
    $('system-' + key + '-row').hidden = !text;
  }
  // Preserve additional board-specific fields without changing the API or rebuilding stable rows.
  const extra = Object.entries(sys).filter(([key, value]) => !SYSTEM_FIELDS.includes(key) && value)
    .map(([key, value]) => `<div class="sys"><span>${esc(key)}</span><span>${esc(value)}</span></div>`).join('');
  if ($('system-extra').innerHTML !== extra) $('system-extra').innerHTML = extra;
}
function renderActive() {
  if (!latestStats || !visible()) return;
  const s = latestStats;
  if (activeTab === 'overview') {  // rebuilt every second: keyboard focus stays on the same link
    const links = () => [...document.querySelectorAll('#overview a')], at = links().indexOf(document.activeElement);
    $('overview').innerHTML = overviewRows(s);
    if (at >= 0 && links()[at]) links()[at].focus();
  }
  else if (activeTab === 'resources') renderResources(s);
  else if (activeTab === 'network') { $('net').innerHTML = netRows(s); $('net-cap').textContent = netCaption(s); }
  else if (activeTab === 'storage') $('dio').innerHTML = diskRows(s);
  else if (activeTab === 'thermal') renderThermal(s);
  else if (activeTab === 'power') renderPower(s);
  else if (activeTab === 'system') renderSystem(s);
  hideEmptyPanels();
}
function renderObservations() {
  if (!visible()) return;
  if (activeTab === 'storage') $('sto').innerHTML = storageRows();
  if (activeTab === 'network') { $('wifi').innerHTML = wifiRows(); $('probe').innerHTML = probeRows(); }
  if (activeTab === 'power') $('batt').innerHTML = batteryRows();
  hideEmptyPanels();
  updateDetailAges();
}
function render(s) {
  // Keep the last good snapshot for instant view switches even if the next request fails.
  $('model').textContent = s.model;
  const sys = s.system || {};
  $('subtitle').textContent = [sys.hostname, s.platform, Number.isFinite(s.uptime) ? 'up ' + uptimeText(s.uptime) : '']
    .filter(Boolean).join(' · ');
  $('coll').textContent = collectionNote(s);
  latestStats = s;
  renderActive();
}
function updateDetailAges() {
  document.querySelectorAll('#panel-' + activeTab + ' [data-observation-age]').forEach(el => {
    el.textContent = observationAge(el.dataset.observationAge);
  });
  if (!$('history-panel').hidden) document.querySelectorAll('[data-history-age]').forEach(el => {
    el.textContent = historyAge(Number(el.dataset.historyAge));
  });
}

const TIMEOUT_MS = 5000;  // a request that hangs must not stop the updates
// What is on screen and how old it is. The age comes from the server (data_age_ms) plus browser time since
// that answer arrived (performance.now, so the device and browser clocks never get compared); it ignores
// transfer time. A repeated 200 for the same sample keeps counting from the server's age, never from 0.
// Older servers send no sample metadata: then only the time since their last answer is known.
let shown = null;       // {kind: 'sample' | 'legacy' | 'invalid', seq, ageMs, t0}
let failing = false;    // the last request failed: what is shown is the last good data, not current
let checking = false;   // tab came back: browser time may have stopped while hidden, wait for a new answer
let inflight = null, refresh = null;  // inflight: the running request's AbortController
let epoch = 0;          // counts tab returns: an answer to a request started before the latest one proves nothing

function sampleOf(s, t0) {
  if (!('schema_version' in s) && !('sample' in s)) return {kind: 'legacy', t0};
  const m = s.sample;
  const ok = s.schema_version === 1 && m && typeof m === 'object' && typeof m.instance_id === 'string'
    && Number.isInteger(m.sequence) && m.sequence > 0 && Number.isFinite(m.data_age_ms) && m.data_age_ms >= 0;
  return ok ? {kind: 'sample', seq: m.sequence, ageMs: m.data_age_ms, t0} : {kind: 'invalid', t0};
}

// The status pill says Live, Partial or Offline; its tooltip carries the sample number and data age.
// Offline shows a banner with the error and the last good data's age, and dims the views.
function showAge() {
  const coll = $('coll').textContent;
  const [status, state] = failing ? ['Offline', 'offline'] : checking ? ['Checking', 'idle'] : !shown ? ['Connecting', 'idle']
    : coll ? ['Partial', 'partial'] : ['Live', 'live'];
  if ($('status').textContent !== status) $('status').textContent = status;
  $('status').setAttribute('data-state', state);
  $('main').setAttribute('data-offline', String(failing));
  $('err-banner').hidden = !failing;
  $('coll-banner').hidden = failing || !coll;
  if (!shown) { $('status').title = $('err-age').textContent = ''; return; }
  const since = performance.now() - shown.t0;
  const secs = ms => Math.round(ms / 1000) + ' s';
  const text = shown.kind === 'sample' ? `sample #${shown.seq} · data age ${secs(shown.ageMs + since)}`
    : `response received ${secs(since)} ago · ` + (shown.kind === 'legacy'
      ? 'this server reports no sample age' : 'sample age unavailable (invalid metadata)');
  const line = failing ? 'last good: ' + text + ' (not current)' : checking ? 'checking… last seen: ' + text : text;
  $('status').title = line[0].toUpperCase() + line.slice(1);
  $('err-age').textContent = failing ? (shown.kind === 'sample' ? ` Showing the last good sample, ${secs(shown.ageMs + since)} old.`
    : ` Showing the last good answer, received ${secs(since)} ago.`) : '';
}

// One request at a time; whatever happens, the next one is scheduled. On failure the last good values
// stay on screen with their age, and the message says what failed.
async function tick() {
  if (inflight || !visible()) return;
  clearTimeout(refresh);
  const ctl = inflight = new AbortController(), mine = epoch;
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  let stage = 'connect';
  try {
    const r = await fetch('api/stats', {signal: ctl.signal, cache: 'no-store'});
    if (r.status === 503) throw new Error('No current data: platmon is running but has no recent sample (see its log)');
    if (!r.ok) throw new Error(`platmon answered ${r.status} ${r.statusText}`);
    stage = 'read';
    const s = await r.json();
    const t0 = performance.now();
    if (mine !== epoch) return;  // started before the tab came back: drop it, finally asks again
    stage = 'render';
    render(s);
    shown = sampleOf(s, t0);
    failing = checking = false;
    $('err').textContent = '';
  } catch (e) {
    if (mine !== epoch) return;
    failing = true;  // shown keeps its own age: a failure does not make old data newer
    checking = false;
    $('err').textContent = e.name === 'AbortError' ? `No answer within ${TIMEOUT_MS / 1000} s`
      : stage === 'connect' && e instanceof TypeError ? 'Connection lost: ' + e.message
      : stage === 'read' ? 'Unexpected response (not platmon data)'
      : e.message;
  } finally {
    clearTimeout(timer);
    inflight = null;
    refresh = visible() ? setTimeout(tick, mine === epoch ? 1000 : 0) : null;
    if (visible()) showAge();
  }
}
setInterval(() => {
  if (!visible()) return;
  showAge(); pollObs(); pollHist();
  updateDetailAges();
}, 1000);  // ages keep moving while a request hangs
for (const [index, tab] of TABS.entries()) {
  $('tab-' + tab).addEventListener('click', () => activateTab(tab));
  $('tab-' + tab).addEventListener('blur', event => {
    if (TABS.some(name => $('tab-' + name) === event.relatedTarget)) return;
    // Re-entering the tab list returns to the selected view, even after arrow-only exploration.
    for (const name of TABS) $('tab-' + name).tabIndex = name === activeTab ? 0 : -1;
  });
  $('tab-' + tab).addEventListener('keydown', event => {
    const next = event.key === 'ArrowRight' ? (index + 1) % TABS.length : event.key === 'ArrowLeft' ? (index + TABS.length - 1) % TABS.length
      : event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : null;
    if (next === null) return;
    event.preventDefault();
    // Manual activation: moving focus never sends a request; Enter/Space click the native button.
    for (const name of TABS) $('tab-' + name).tabIndex = name === TABS[next] ? 0 : -1;
    $('tab-' + TABS[next]).focus();
  });
}
$('history-groups').addEventListener('click', event => {
  const button = event.target.closest && event.target.closest('[data-group]');
  if (!button) return;
  if (!histOff.delete(button.dataset.group)) histOff.add(button.dataset.group);  // delete reports a hit
  histDraw();
});
$('hist').addEventListener('pointermove', readout);
$('hist').addEventListener('pointerleave', readout);
document.addEventListener('visibilitychange', () => {
  epoch++;
  clearTimeout(refresh); refresh = null;
  if (obsRequest) obsRequest.abort();
  if (histRequest) histRequest.abort();
  if (!visible()) { if (inflight) inflight.abort(); return; }
  obs = hist = null;
  obsAsked = histAsked = -Infinity;
  for (const id of ['sto', 'wifi', 'probe', 'hist']) $(id).innerHTML = '';
  hideEmptyPanels();
  $('history-panel').hidden = true;
  checking = true;
  showAge();
  if (inflight) inflight.abort();  // its answer would not count; its finally starts the new request
  else tick();
});
const restoreTab = () => activateTab(TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview', false);
window.addEventListener('hashchange', restoreTab);
restoreTab();
tick();
