import { api } from '../api.js';
import { h, mount, badge, ago, time, toast, formDialog, emptyState } from '../ui.js';
import { can, on } from '../state.js';
import { debounce } from './overview.js';
import { photoGrid } from '../photos.js';

const NEXT = { NEW: ['ACKNOWLEDGED', 'FALSE_ALARM'], ACKNOWLEDGED: ['RESPONDING', 'RESOLVED', 'FALSE_ALARM'], RESPONDING: ['RESOLVED', 'FALSE_ALARM'] };

export async function render(page) {
  let showClosed = false;
  const listEl = h('div', { class: 'stack' });
  const toggle = h('button', { class: 'btn small', onclick: () => { showClosed = !showClosed; toggle.textContent = showClosed ? 'Show active only' : 'Show closed too'; load(); } }, 'Show closed too');
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'skymesh'), ':~$ incidents --watch'), h('h1', {}, 'Emergency Center')),
    h('span', { class: 'spacer' }), toggle,
    can('incident.test') ? h('button', { class: 'btn', onclick: testAlert }, 'Send test alert') : null),
  can('incident.view_all') ? null : h('p', { class: 'notice' }, 'You see only alerts you raised. Use the red SOS button to raise one.'),
  listEl);

  let users = [];
  async function load() {
    try {
      const [list, us] = await Promise.all([api.get(`incidents?active=${!showClosed}&limit=200`), can('incident.manage') ? api.get('users') : []]);
      users = us.filter((u) => u.role !== 'user');
      mount(listEl, list.length ? list.map(card) : emptyState(showClosed ? 'No incidents recorded.' : 'No active incidents.'));
    } catch (e) { mount(listEl, h('div', { class: 'notice bad' }, e.message)); }
  }

  function card(i) {
    const active = NEXT[i.state];
    const tl = h('div', { class: 'timeline hidden' });
    const tlBtn = h('button', { class: 'btn small', onclick: async () => {
      if (tl.classList.toggle('hidden')) return;
      const d = await api.get(`incidents/${i.id}`);
      mount(tl, d.timeline.map((e) => h('div', {}, h('span', { class: 'mono dim' }, new Date(e.at).toLocaleTimeString()), ' ',
        e.to_state ? badge(e.to_state) : h('b', {}, e.kind), ' ', e.actor_name || (e.actor_device_id ? `device ${e.actor_device_id}` : ''), e.note ? ` — ${e.note}` : '')));
    } }, 'Timeline');
    return h('div', { class: ['inc', active && !i.is_test && 'active', i.is_test && 'test'] },
      h('div', { class: 'row' }, badge(i.state), i.is_test ? badge('TEST', 'b-muted') : badge(i.severity.toUpperCase(), 'b-bad'),
        i.source === 'sensor' ? badge('AUTOMATIC – VERIFY', 'b-warn') : null,
        i.repeat_count ? badge(`PRESSED ${i.repeat_count + 1}×`, 'b-bad') : null,
        h('b', {}, i.reporter_name || i.device_name || 'Unknown reporter'), h('span', { class: 'dim' }, `${i.source} · ${ago(i.created_at)}`)),
      i.message ? h('p', {}, i.source === 'sensor' ? i.message : `“${i.message}”`) : null,
      i.source === 'sensor' && can('incident.view_all') ? evidence(i) : null,
      i.source === 'badge' ? incidentPhotos(i) : null,
      h('div', { class: 'kv' }, h('dt', {}, 'location'), h('dd', {}, i.location_note, i.room_name ? ` — ${i.room_name}` : '',
        i.x !== null && i.floor_id ? h('a', { href: '#/map', class: 'btn small' }, 'Show on map') : null),
      h('dt', {}, 'method'), h('dd', {}, i.location_source ? `${i.location_source}${i.accuracy_m ? ` ±${i.accuracy_m} m` : ''}` : '—'),
      h('dt', {}, 'delivered'), h('dd', {}, `${time(i.created_at)} (received by backend)`),
      h('dt', {}, 'acknowledged'), h('dd', {}, i.acknowledged_at ? time(i.acknowledged_at) : h('span', { class: 'b-warn' }, 'not yet')),
      h('dt', {}, 'assigned'), h('dd', {}, i.assigned_name || 'nobody'),
      h('dt', {}, 'id'), h('dd', { class: 'mono dim' }, i.id)),
      h('div', { class: 'row' }, tlBtn,
        can('incident.manage') && active ? active.map((s) => h('button', { class: ['btn', 'small', s === 'ACKNOWLEDGED' && 'primary', s === 'FALSE_ALARM' && 'danger'],
          onclick: () => transition(i, s) }, s === 'ACKNOWLEDGED' ? 'Acknowledge' : s === 'RESPONDING' ? 'Responding' : s === 'RESOLVED' ? 'Resolve' : 'False alarm')) : null,
        can('incident.manage') && active ? h('button', { class: 'btn small', onclick: () => assign(i) }, 'Assign') : null,
        active ? h('button', { class: 'btn small', onclick: () => note(i) }, 'Add note') : null),
      tl);
  }

  function incidentPhotos(i) {
    const box = h('div', {});
    api.get(`incidents/${i.id}/photos`).then((ps) => mount(box, ps.length ? photoGrid(ps)
      : h('p', { class: 'dim' }, 'No photo from the badge yet (it follows the alert within a few seconds if the badge has a camera).'))).catch(() => {});
    return box;
  }

  function evidence(i) {
    const box = h('div', {});
    const btn = h('button', { class: 'btn small', onclick: async () => {
      btn.disabled = true;
      try {
        const r = await fetch(`/api/v1/incidents/${i.id}/evidence`, { credentials: 'same-origin' });
        if (!r.ok) throw new Error('No snapshot was saved');
        mount(box, h('img', { class: 'evidence', alt: 'Camera snapshot at the moment of the alert', src: URL.createObjectURL(await r.blob()) }));
      } catch (e) { mount(box, h('span', { class: 'dim' }, e.message)); }
    } }, 'Show snapshot');
    mount(box, btn);
    return box;
  }

  async function transition(i, to) {
    const v = to === 'ACKNOWLEDGED' ? { note: '' } : await formDialog(`Mark ${to.replace('_', ' ').toLowerCase()}`, [{ name: 'note', label: 'Note', type: 'textarea' }], 'Confirm');
    if (!v) return;
    try { await api.post(`incidents/${i.id}/transition`, { to_state: to, note: v.note || null }); } catch (e) { toast(e.message, 'bad'); }
    load();
  }
  async function assign(i) {
    if (!users.length) return toast('No responders available', 'bad');
    const v = await formDialog('Assign responder', [{ name: 'user_id', label: 'Responder', type: 'select', value: users[0].id,
      options: users.map((u) => ({ value: u.id, label: `${u.display_name}${u.online ? ' · online' : ''}` })) }], 'Assign');
    if (v) api.post(`incidents/${i.id}/assign`, v).then(load).catch((e) => toast(e.message, 'bad'));
  }
  async function note(i) {
    const v = await formDialog('Add note', [{ name: 'note', label: 'Note', type: 'textarea', required: true }], 'Add');
    if (v) api.post(`incidents/${i.id}/notes`, v).then(load).catch((e) => toast(e.message, 'bad'));
  }
  async function testAlert() {
    const v = await formDialog('Test alert', [{ name: 'message', label: 'Message', value: 'Drill — test alert' }], 'Send test',
      'Test alerts are clearly labelled, excluded from SOS counts and never shown as real emergencies.');
    if (v) api.post('incidents/test', v).then(() => { toast('Test alert created', 'ok'); load(); }).catch((e) => toast(e.message, 'bad'));
  }

  await load();
  const offs = [on('ws:incident', debounce(load, 300)), on('ws:device_photo', debounce(load, 300)), on('reconnected', load)];
  return () => offs.forEach((f) => f());
}
