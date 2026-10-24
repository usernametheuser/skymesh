import { api } from '../api.js';
import { h, mount, badge, ago, table, onlineBadge, formDialog, openDialog, confirmDialog, toast, emptyState, kv } from '../ui.js';
import { can, on } from '../state.js';
import { debounce } from './overview.js';
import { badgePhotos } from '../photos.js';

export async function render(page) {
  const body = h('div', {});
  let typeFilter = '';
  const typeSel = h('select', { 'aria-label': 'Device type', onchange: () => { typeFilter = typeSel.value; load(); } },
    ['', 'badge', 'camera_node', 'phone', 'anchor', 'gateway'].map((t) => h('option', { value: t }, t || 'All types')));
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'skymesh'), ':~$ devices ls'), h('h1', {}, 'Devices')),
    h('span', { class: 'spacer' }), typeSel,
    can('device.enroll') ? h('button', { class: 'btn', onclick: enrollDialog, title: 'Create credentials to paste into secrets.h yourself' }, 'Manual enroll') : null,
    can('device.enroll') ? h('a', { class: 'btn primary', href: '#/adddevice' }, '+ Add device (USB)') : null), body);

  let users = [];
  async function load() {
    const devs = (await api.get('devices')).filter((d) => !typeFilter || d.type === typeFilter);
    mount(body, devs.length ? table([
      { label: 'Name', render: (d) => h('div', {}, h('b', {}, d.name), h('div', { class: 'mono dim' }, d.id)) },
      { label: 'Type', render: (d) => h('span', { class: 'mono' }, d.type) },
      { label: 'Owner', render: (d) => d.owner_name || '—' },
      { label: 'Status', render: (d) => d.revoked_at ? badge('REVOKED', 'b-bad') : onlineBadge(d.online) },
      { label: 'Last heartbeat', render: (d) => d.last_seen_at ? ago(d.last_seen_at) : (d.type === 'phone' ? 'browser (no heartbeat)' : 'never') },
      { label: 'Firmware', render: (d) => d.firmware_version || '—' },
      { label: 'Signal', render: (d) => d.rssi !== null ? `${d.rssi} dBm` : '—' },
      { label: 'Battery', render: (d) => d.battery_pct !== null ? `${d.battery_pct}%` : d.battery_mv ? `${d.battery_mv} mV` : h('span', { class: 'dim' }, 'not measured') },
      { label: 'Camera', render: (d) => d.camera_ok === null ? '—' : badge(d.camera_ok ? 'OK' : 'FAULT', d.camera_ok ? 'b-ok' : 'b-bad') },
      { label: 'Sharing', render: (d) => d.type === 'phone' ? badge(d.sharing_enabled ? 'ON' : (d.permission_state === 'denied' ? 'DENIED' : 'OFF'), d.sharing_enabled ? 'b-ok' : 'b-muted') : '—' },
    ], devs, details) : emptyState(can('device.view_all') ? 'No devices enrolled yet.' : 'You have no devices. Register this phone under Settings → Location sharing.'));
  }

  async function details(d) {
    const actions = [];
    if (can('device.enroll') && !d.revoked_at) {
      actions.push(h('button', { class: 'btn small', onclick: async () => {
        const v = await formDialog('Assign wearer / rename', [{ name: 'name', label: 'Name', value: d.name },
          { name: 'owner', label: 'Owner', type: 'select', value: d.owner_user_id || '', options: [{ value: '', label: '— none —' }, ...users.map((u) => ({ value: u.id, label: u.display_name }))] }]);
        if (v) api.patch(`devices/${d.id}`, { name: v.name, owner_user_id: v.owner || null, clear_owner: !v.owner }).then(load).catch((e) => toast(e.message, 'bad'));
      } }, 'Edit'));
      if (d.type !== 'phone') actions.push(h('button', { class: 'btn small', onclick: async () => {
        if (!(await confirmDialog('Rotate token', 'The device will stop working until its firmware is updated with the new token.', 'Rotate', 'danger'))) return;
        const r = await api.post(`devices/${d.id}/rotate-token`);
        showSecret('New device token', { SKYMESH_DEVICE_TOKEN: r.device_token });
      } }, 'Rotate token'));
      actions.push(h('button', { class: 'btn small danger', onclick: async () => {
        if (await confirmDialog('Revoke device', `Revoke ${d.name}? It will be rejected by the backend immediately.`, 'Revoke', 'danger')) {
          api.post(`devices/${d.id}/revoke`).then(() => { toast('Revoked', 'ok'); load(); }).catch((e) => toast(e.message, 'bad'));
        }
      } }, 'Revoke'));
    }
    const hb = can('device.view_all') && d.type !== 'phone' ? await api.get(`devices/${d.id}/heartbeats?limit=10`).catch(() => []) : [];
    const photos = d.type === 'badge' ? badgePhotos(d) : null;
    if (d.type === 'badge' && d.capabilities?.camera_error) photos.prepend(h('div', { class: 'notice warn' }, `Badge camera: ${d.capabilities.camera_error}`));
    await openDialog(d.name, h('div', { class: 'stack' }, photos, kv([
      ['ID', h('span', { class: 'mono' }, d.id)], ['Type', d.type], ['Hardware', d.hardware_model || '—'], ['Firmware', d.firmware_version || '—'],
      ['Enrolled', d.enrolled_at], ['Last IP', d.last_ip || '—'], ['BSSID', d.bssid || '—'], ['Free heap', d.free_heap ?? '—'], ['Uptime', d.uptime_s != null ? `${d.uptime_s} s` : '—'],
      ['Capabilities', h('span', { class: 'mono dim' }, JSON.stringify(d.capabilities))]]),
    hb.length ? h('div', { class: 'term' }, hb.map((x) => h('div', {}, h('span', { class: 'ts' }, x.received_at), ` rssi=${x.payload.rssi} heap=${x.payload.free_heap} seq=${x.payload.sequence}`))) : null,
    h('div', { class: 'row' }, actions)), [{ label: 'Close', value: true }]);
    photos?._off?.();
  }

  async function enrollDialog() {
    const v = await formDialog('Enroll device', [
      { name: 'type', label: 'Type', type: 'select', value: 'badge', options: [
        { value: 'badge', label: 'Wearable badge (ESP32-S3-CAM)' }, { value: 'camera_node', label: 'Camera / server node (ESP32-CAM)' },
        { value: 'anchor', label: 'Positioning anchor' }, { value: 'gateway', label: 'Gateway' }] },
      { name: 'name', label: 'Name', required: true, placeholder: 'e.g. Badge 01 or Corridor camera N' },
      { name: 'hardware_model', label: 'Hardware model', placeholder: 'e.g. AI-Thinker ESP32-CAM' },
      { name: 'owner', label: 'Wearer (badges)', type: 'select', value: '', options: [{ value: '', label: '— none —' }, ...users.map((u) => ({ value: u.id, label: u.display_name }))] }],
    'Enroll', 'A device token is generated and shown once. Put it in the firmware secrets.h file.');
    if (!v) return;
    try {
      const r = await api.post('devices', { type: v.type, name: v.name, hardware_model: v.hardware_model || null, owner_user_id: v.owner || null });
      const secrets = { SKYMESH_DEVICE_ID: r.device.id, SKYMESH_DEVICE_TOKEN: r.device_token };
      if (r.camera_key) secrets.SKYMESH_CAMERA_KEY = r.camera_key;
      await showSecret('Device credentials (shown once)', secrets);
      load();
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function showSecret(title, obj) {
    const text = Object.entries(obj).map(([k, v]) => `#define ${k} "${v}"`).join('\n');
    await openDialog(title, h('div', { class: 'stack' }, h('p', { class: 'notice warn' }, 'Copy now. The backend stores only a hash and cannot show this again.'),
      h('pre', { class: 'term' }, text),
      h('button', { class: 'btn small', onclick: () => navigator.clipboard?.writeText(text).then(() => toast('Copied', 'ok')) }, 'Copy')), [{ label: 'Done', value: true, kind: 'primary' }]);
  }

  if (can('device.enroll')) users = await api.get('users').catch(() => []);
  await load();
  const quiet = () => load().catch(() => {});  // offline moments are shown by the status bar
  const offs = [on('ws:device', debounce(quiet, 1000)), on('reconnected', quiet)];
  const t = setInterval(quiet, 15000);
  return () => { offs.forEach((f) => f()); clearInterval(t); };
}
