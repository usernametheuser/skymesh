import { api } from '../api.js';
import { h, mount, badge, ago, toast, kv, formDialog } from '../ui.js';
import { on, locationShare } from '../state.js';
export async function render(page) {
  const shareCard = h('section', { class: 'card stack' });
  const theme = h('button', { class: 'btn', onclick: async () => (await import('../app.js')).wallpaperDialog() }, 'Change wallpaper…');

  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'skymesh'), ':~$ settings'), h('h1', {}, 'Settings'))),
    h('div', { class: 'grid two' }, shareCard,
      h('section', { class: 'card stack' }, h('h2', {}, 'Desktop'), h('div', { class: 'row' }, theme),
        h('p', { class: 'dim' }, 'Tips: right-click the desktop for wallpaper and Show desktop. Ctrl+K opens the launcher. The thin strip at the top-right corner shows the desktop. Double-click a window title to maximise it.')),
      h('section', { class: 'card stack' }, h('h2', {}, 'Account'), h('button', { class: 'btn', onclick: changePassword }, 'Change password'))));

  let floors = [], rooms = [];
  async function loadMap() {
    const bs = await api.get('buildings').catch(() => []);
    floors = bs.flatMap((b) => b.floors.map((f) => ({ id: f.id, label: `${b.name} — ${f.name}` })));
  }

  async function renderShare() {
    const active = locationShare.active();
    const devs = await api.get('devices').catch(() => []);
    const phone = devs.find((d) => d.id === locationShare.deviceId());
    const last = locationShare.lastSent;
    const floorSel = h('select', { onchange: () => locationShare.setFloor(floorSel.value || null) },
      h('option', { value: '' }, '— I don’t know / outdoors —'), floors.map((f) => h('option', { value: f.id, selected: f.id === locationShare.floorId() }, f.label)));
    mount(shareCard, h('h2', {}, 'Location sharing'),
      h('div', { class: 'row' }, active ? badge('SHARING', 'b-ok') : badge('NOT SHARING', 'b-muted'),
        phone ? h('span', { class: 'dim' }, `as ${phone.name}`) : null),
      h('p', { class: 'dim' }, 'Off by default. When on, responders and administrators can see this device’s position; other users cannot. ',
        'You can stop at any time — nobody else can turn it on for you. Updates are sent only while this page is open: ',
        'browsers pause location in the background, so your marker will turn STALE if you switch apps.'),
      window.isSecureContext ? null : h('div', { class: 'notice warn' }, 'This page is not on HTTPS, so the browser will refuse to provide GPS. You can still choose your room manually.'),
      h('label', { class: 'field' }, 'Which floor are you on? (GPS cannot tell floors apart)', floorSel),
      h('div', { class: 'row' },
        active ? h('button', { class: 'btn danger', onclick: async () => { await locationShare.stop(); toast('Location sharing stopped', 'ok'); renderShare(); } }, 'Stop sharing')
          : h('button', { class: 'btn primary', onclick: async () => {
            try { await locationShare.start(); toast('Location sharing on', 'ok'); } catch (e) { toast(e.message, 'bad', 8000); }
            renderShare();
          } }, 'Share my live location'),
        h('button', { class: 'btn', onclick: pickRoom }, 'I’m in a specific room…')),
      last ? kv([
        ['Last sent', ago(last.at)], ['Result', last.kind === 'floor' ? 'Placed on the floor plan' : last.kind === 'room' ? 'Room-level position' : 'GPS only (not on floor plan)'],
        ['Accuracy', last.accuracy_m ? `±${last.accuracy_m} m` : '—'], ['Notes', (last.flags || []).join(', ') || '—']]) : null,
      locationShare.lastError ? h('div', { class: 'notice bad' }, locationShare.lastError) : null);
  }

  async function pickRoom() {
    if (!floors.length) return toast('No campus map configured', 'bad');
    const fv = await formDialog('Choose floor', [{ name: 'floor', label: 'Floor', type: 'select', value: locationShare.floorId() || floors[0].id, options: floors.map((f) => ({ value: f.id, label: f.label })) }], 'Next');
    if (!fv) return;
    const fl = await api.get(`floors/${fv.floor}`);
    rooms = fl.features.filter((f) => f.geometry_type === 'polygon' && ['room', 'corridor', 'toilet', 'other'].includes(f.kind));
    if (!rooms.length) return toast('No rooms are mapped on that floor yet', 'bad');
    const rv = await formDialog('Where are you?', [{ name: 'room', label: 'Room / area', type: 'select', value: rooms[0].id, options: rooms.map((r) => ({ value: r.id, label: r.name })) }], 'Share room',
      'This shares a room-level position only. It stays on the map as your last known location until you update or stop sharing.');
    if (!rv) return;
    try { await locationShare.shareRoom(rv.room); toast('Room shared', 'ok'); } catch (e) { toast(e.message, 'bad'); }
    renderShare();
  }

  async function changePassword() {
    const v = await formDialog('Change password', [{ name: 'current_password', label: 'Current password', type: 'password', required: true },
      { name: 'new_password', label: 'New password (min 10)', type: 'password', required: true }], 'Change', 'Other sessions will be signed out.');
    if (v) api.post('auth/password', v).then(() => toast('Password changed', 'ok')).catch((e) => toast(e.message, 'bad'));
  }

  await loadMap();
  await renderShare();
  const off = on('sharing', renderShare);
  return off;
}
