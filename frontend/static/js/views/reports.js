import { api } from '../api.js';
import { h, mount, kv } from '../ui.js';

export async function render(page) {
  const r = await api.get('reports/summary');
  const bars = (obj) => {
    const max = Math.max(1, ...Object.values(obj));
    return Object.keys(obj).length ? h('div', { class: 'stack' }, Object.entries(obj).map(([k, v]) => {
      const bar = h('div', {});
      bar.style.height = '10px'; bar.style.borderRadius = '4px'; bar.style.background = 'var(--accent)'; bar.style.width = `${(v / max) * 100}%`;
      return h('div', {}, h('div', { class: 'row' }, h('span', { class: 'mono' }, k), h('span', { class: 'dim' }, String(v))), bar);
    })) : h('p', { class: 'dim' }, 'No data yet.');
  };
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'skymesh'), ':~$ report --incidents'), h('h1', {}, 'Reports')),
    h('span', { class: 'spacer' }), h('a', { class: 'btn', href: '/api/v1/reports/incidents.csv' }, 'Export incidents CSV')),
  h('div', { class: 'grid two' },
    h('section', { class: 'card' }, h('h2', {}, 'Real incidents by state'), bars(r.incidents_by_state)),
    h('section', { class: 'card' }, h('h2', {}, 'Real incidents by source'), bars(r.incidents_by_source)),
    h('section', { class: 'card' }, h('h2', {}, 'Response'), kv([
      ['Mean time to acknowledge', r.mean_seconds_to_acknowledge != null ? `${r.mean_seconds_to_acknowledge} s` : 'no acknowledged incidents'],
      ['Acknowledged incidents', String(r.acknowledged_count)], ['Test incidents (excluded above)', String(r.tests)]]))));
}
