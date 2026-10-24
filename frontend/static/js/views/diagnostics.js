import { api } from '../api.js';
import { h, mount, kv } from '../ui.js';
import { state } from '../state.js';

export async function render(page) {
  const d = await api.get('diagnostics');
  const t0 = performance.now();
  await api.get('health');
  const rtt = Math.round(performance.now() - t0);
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'root@skymesh'), ':~# skymesh doctor'), h('h1', {}, 'Diagnostics'))),
    h('div', { class: 'grid two' },
      h('section', { class: 'card' }, h('h2', {}, 'Backend'), kv([['Version', d.version], ['Python', d.python], ['Platform', d.platform],
        ['API round trip', `${rtt} ms`], ['Live connection', state.conn], ['WebSocket clients', String(d.realtime.clients)],
        ['Secure cookies (HTTPS)', String(d.secure_cookies)], ['Rate limits', String(d.rate_limits)], ['Person detector', d.detector]])),
      h('section', { class: 'card' }, h('h2', {}, 'Database'), kv([['Path', h('span', { class: 'mono' }, d.database.path)],
        ['Size', `${(d.database.size_bytes / 1024).toFixed(0)} KiB`], ['Migrations', d.database.migrations.join(', ')],
        ...Object.entries(d.database.rows).map(([k, v]) => [k, String(v)])])),
      h('section', { class: 'card' }, h('h2', {}, 'Location thresholds'), kv(Object.entries(d.thresholds).map(([k, v]) => [k, String(v)]))),
      h('section', { class: 'card' }, h('h2', {}, 'Live camera analysis'), Object.keys(d.live_analysis).length
        ? h('div', { class: 'term' }, JSON.stringify(d.live_analysis, null, 2)) : h('p', { class: 'dim' }, 'Not running (disabled in settings, or no camera online).')),
      h('section', { class: 'card' }, h('h2', {}, 'This browser'), kv([['Secure context', String(window.isSecureContext)],
        ['Geolocation API', String('geolocation' in navigator)], ['Service worker', String('serviceWorker' in navigator)],
        ['Online (browser)', String(navigator.onLine)], ['User agent', navigator.userAgent]]))));
}
