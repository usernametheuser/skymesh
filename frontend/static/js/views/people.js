import { api } from '../api.js';
import { h, mount, badge, ago, table, emptyState } from '../ui.js';
import { can, on } from '../state.js';
import { debounce } from './overview.js';

export async function render(page) {
  const body = h('div', { class: 'stack' });
  const q = h('input', { type: 'search', placeholder: 'Filter people…', 'aria-label': 'Filter people' });
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'skymesh'), ':~$ who'), h('h1', {}, 'People')),
    h('span', { class: 'spacer' }), q), body);

  async function load() {
    const [users, locs] = await Promise.all([api.get('users'), api.get('locations/current')]);
    const f = q.value.trim().toLowerCase();
    const byOwner = {};
    for (const l of locs) (byOwner[l.owner_user_id] ||= []).push(l);
    const rows = users.filter((u) => !f || `${u.display_name} ${u.username}`.toLowerCase().includes(f));
    mount(body,
      can('location.view_all') ? null : h('p', { class: 'notice' }, 'Locations are visible only to responders and administrators, and only for people who chose to share. You see your own devices.'),
      rows.length ? table([
        { label: 'Name', render: (u) => h('b', {}, u.display_name) },
        { label: 'User', render: (u) => h('span', { class: 'mono' }, `@${u.username}`) },
        { label: 'Role', render: (u) => badge(u.role.toUpperCase(), u.role === 'admin' ? 'b-violet' : u.role === 'responder' ? 'b-info' : 'b-muted') },
        { label: 'Presence', render: (u) => badge(u.online ? 'ONLINE' : 'OFFLINE', u.online ? 'b-ok' : 'b-muted') },
        { label: 'Location', render: (u) => {
          const ls = byOwner[u.id] || [];
          if (!ls.length) return h('span', { class: 'dim' }, can('location.view_all') ? 'no tracked device' : '—');
          return h('div', { class: 'stack' }, ls.map((l) => h('div', { class: 'row' }, badge(l.state), h('span', { class: 'dim' },
            `${l.device_type}: ${l.room_name || (l.x !== undefined && l.x !== null ? 'unmapped area' : 'not on map')}${l.observed_at ? ` · ${ago(l.observed_at)}` : ''}`))));
        } },
        { label: '', render: (u) => h('a', { class: 'btn small', href: '#/chat', onclick: async (e) => {
          e.preventDefault();
          const c = await api.post('chat/direct', { user_id: u.id }).catch(() => null);
          location.hash = c ? `#/chat?c=${c.id}` : '#/chat';
        } }, 'Message') },
      ], rows) : emptyState('No matching people.'));
  }
  q.addEventListener('input', debounce(load, 200));
  await load();
  const offs = [on('ws:presence', debounce(load, 500)), on('ws:location', debounce(load, 1500))];
  return () => offs.forEach((f) => f());
}
