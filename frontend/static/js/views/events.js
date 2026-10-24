import { api } from '../api.js';
import { h, mount } from '../ui.js';
import { on } from '../state.js';
import { debounce } from './overview.js';

export async function render(page) {
  const term = h('div', { class: 'term' });
  term.style.maxHeight = 'none';
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'skymesh'), ':~$ journalctl -f'), h('h1', {}, 'Event History'))),
    h('p', { class: 'dim' }, 'Device, incident, map and camera events. Sign-ins and chat content are not shown here.'), term);
  async function load() {
    const evs = await api.get('events?limit=300');
    mount(term, evs.length ? evs.map((e) => h('div', {}, h('span', { class: 'ts' }, new Date(e.at).toLocaleString()), '  ',
      h('span', { class: 'act' }, e.action.padEnd(28)), ' ', e.actor_name || (e.device_name ? `device:${e.device_name}` : 'system'),
      e.target ? ` → ${e.target}` : '', Object.keys(e.detail || {}).length ? `  ${JSON.stringify(e.detail)}` : '')) : 'no events');
  }
  await load();
  const off = on('ws:incident', debounce(load, 500));
  return off;
}
