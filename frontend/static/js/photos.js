// Badge photo gallery (SOS scene pictures and requested photos).
import { api } from './api.js';
import { h, mount, openDialog, confirmDialog, time, toast } from './ui.js';
import { state, can, on } from './state.js';

const REASON = { sos: 'with SOS', request: 'requested', test: 'test' };

export function photoGrid(photos) {
  if (!photos.length) return h('p', { class: 'dim' }, 'No photos yet.');
  return h('div', { class: 'photo-grid' }, photos.map((p) => h('figure', {},
    h('img', { src: p.url, alt: `Photo ${REASON[p.reason] || ''} at ${time(p.created_at)}`, loading: 'lazy',
      onclick: () => openDialog(`Photo ${REASON[p.reason] || ''}`, h('div', { class: 'stack' },
        h('img', { src: p.url, alt: 'Badge photo', class: 'evidence' }),
        h('span', { class: 'dim mono' }, `${time(p.created_at)} · ${p.width}×${p.height} · ${(p.bytes / 1024).toFixed(0)} KB`)),
      [{ label: 'Close', value: true, kind: 'primary' }]) }),
    h('figcaption', {}, `${REASON[p.reason] || p.reason} · ${time(p.created_at)}`))));
}

// Section for one badge: its photos, live-updating, plus "Request photo" for responders.
export function badgePhotos(device) {
  const box = h('div', { class: 'stack' });
  const grid = h('div', {});
  const load = async () => mount(grid, photoGrid(await api.get(`devices/${device.id}/photos`).catch(() => [])));
  const ask = () => api.post(`devices/${device.id}/photo-request`).then((r) => toast(r.note, 'ok', 8000));
  const mine = device.owner_user_id && device.owner_user_id === state.me?.id;
  const req = can('incident.manage') || mine ? h('button', { class: 'btn small', onclick: async () => {
    try { await ask(); } catch (e) {
      if (!/turned off/i.test(e.message)) return toast(e.message, 'bad', 8000);
      if (!can('settings.manage')) return toast('Photo requests are turned off. Ask an administrator to turn them on.', 'bad', 8000);
      // Off by default for privacy: an administrator turns it on here, once, knowingly.
      const ok = await confirmDialog('Turn on photo requests?',
        'Responders and administrators will be able to ask any badge for a photo of what is in front of it. Every request and every view is logged, and photos are deleted after 7 days. You can turn this off again in Administration → Settings.',
        'Turn on and request photo');
      if (!ok) return;
      try {
        await api.patch('settings', { values: { 'badge.remote_photo_enabled': true } });
        await ask();
      } catch (e2) { toast(e2.message, 'bad', 8000); }
    }
  } }, 'Request photo') : null;
  const off = on('ws:device_photo', (e) => { if (e.detail.device_id === device.id) load(); });
  box.append(h('div', { class: 'row' }, h('b', {}, 'Photos'), h('span', { class: 'spacer' }), req), grid,
    h('p', { class: 'dim' }, 'The badge sends a photo of the scene right after an SOS, and you can request one at any time: responders from any badge, everyone else from their own. Every view and request is logged; photos are deleted after 7 days.'));
  load();
  box._off = off;
  return box;
}
