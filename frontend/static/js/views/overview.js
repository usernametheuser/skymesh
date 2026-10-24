import { api } from '../api.js';
import { h, mount, badge, ago, onlineBadge, emptyState } from '../ui.js';
import { state, can, on } from '../state.js';

export async function render(page) {
  const body = h('div', { class: 'stack' });
  mount(page, h('div', { class: 'page-head' }, h('div', {},
    h('div', { class: 'prompt' }, h('b', {}, `${state.me.username}@skymesh`), ':~$ status --overview'), h('h1', {}, 'Overview'))), body);

  async function load() {
    let o;
    try { o = await api.get('overview'); } catch (e) { mount(body, h('div', { class: 'notice bad' }, e.message)); return; }
    const scope = can('device.view_all') ? 'campus' : 'your devices';
    mount(body,
      h('div', { class: 'grid kpi' },
        kpi('Active incidents', o.incidents.active, o.incidents.active_tests ? `${o.incidents.active_tests} test incident(s) open` : 'excludes tests', o.incidents.active ? 'var(--sos)' : null),
        kpi('Registered devices', o.devices.registered, scope),
        kpi('Online', o.devices.online, `${o.devices.offline} offline · ${o.devices.no_heartbeat_expected} browser phones`),
        kpi('Live locations', o.locations.live, `${o.locations.visible} visible to you`),
        kpi('Backend', o.backend.status.toUpperCase(), `v${o.backend.version} · ${o.backend.ws_clients} live client(s)`)),
      h('div', { class: 'grid two' },
        h('section', { class: 'card' }, h('h2', {}, 'Latest location updates'),
          o.locations.latest.length ? h('div', { class: 'stack' }, o.locations.latest.map((l) => h('div', { class: 'row' },
            badge(l.state), h('b', {}, l.owner_name || l.device_name), h('span', { class: 'dim' }, `${l.room_name || (l.x !== undefined && l.x !== null ? 'unmapped area' : 'not on map')} · ${l.source} · ${ago(l.observed_at)}`))))
            : emptyState('No location updates yet. Phones share only after their owner opts in (Settings → Location sharing).')),
        h('section', { class: 'card' }, h('h2', {}, 'Camera nodes'),
          o.cameras.length ? h('div', { class: 'stack' }, o.cameras.map((c) => h('div', { class: 'row' }, onlineBadge(c.online), h('b', {}, c.name),
            c.camera_ok === null ? h('span', { class: 'dim' }, 'camera status not reported') : badge(c.camera_ok ? 'CAMERA OK' : 'CAMERA FAULT', c.camera_ok ? 'b-ok' : 'b-bad'))))
            : emptyState('No camera nodes enrolled.')),
        h('section', { class: 'card' }, h('h2', {}, 'Battery warnings'),
          o.battery_warnings.length ? h('div', { class: 'stack' }, o.battery_warnings.map((b) => h('div', { class: 'row' }, badge(`${b.battery_pct}%`, 'b-warn'), b.name)))
            : h('p', { class: 'dim' }, 'None. Only devices with battery-measurement hardware report a level.')),
        o.recent_events ? h('section', { class: 'card' }, h('h2', {}, 'Recent events'),
          h('div', { class: 'term' }, o.recent_events.length ? o.recent_events.map((e) => h('div', {}, h('span', { class: 'ts' }, new Date(e.at).toLocaleTimeString()), ' ',
            h('span', { class: 'act' }, e.action), ' ', e.actor_name || e.device_name || 'system', e.target ? ` → ${e.target}` : '')) : 'no events yet'))
          : null));
  }
  function kpi(label, v, sub, color) {
    const val = h('div', { class: 'v' }, String(v));
    if (color) val.style.color = color;
    return h('div', { class: 'card kpi' }, h('div', { class: 'l' }, label), val, h('div', { class: 's' }, sub));
  }
  await load();
  const offs = ['ws:incident', 'ws:device', 'ws:location', 'reconnected'].map((t) => on(t, debounce(load, 800)));
  const timer = setInterval(load, 20000);
  return () => { offs.forEach((f) => f()); clearInterval(timer); };
}

export function debounce(fn, ms) { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }
