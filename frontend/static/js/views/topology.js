import { api } from '../api.js';
import { h, s, mount, badge, table, formDialog, toast } from '../ui.js';
import { can } from '../state.js';

const KIND_COLOR = { server: 'var(--accent)', wifi_ap: 'var(--violet)', badge: 'var(--accent-2)', camera_node: 'var(--info)', phone: 'var(--accent)', anchor: 'var(--warn)', gateway: 'var(--violet)', laptop: 'var(--muted)' };
const LINK_TEXT = {
  observed: 'Observed now (device reported this AP BSSID)', last_observed: 'Last observed (device offline)',
  observed_path_unknown: 'Reached backend; path not reported', last_observed_path_unknown: 'Last reached backend; path unknown',
  configured: 'Configured by an administrator', configured_assumed: 'AP assumed wired to the LAN (configured)', unknown: 'Unknown (browser phone: no telemetry)',
};

export async function render(page) {
  const body = h('div', { class: 'stack topo' });
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'skymesh'), ':~$ ip link show'), h('h1', {}, 'Network Topology')),
    h('span', { class: 'spacer' }), can('settings.manage') ? h('button', { class: 'btn', onclick: addLink }, 'Add configured link') : null), body);

  async function load() {
    const t = await api.get('topology');
    const W = 1000, H = 560, cx = W / 2, cy = H / 2;
    const pos = { backend: [cx, cy] };
    const aps = t.nodes.filter((n) => n.kind === 'wifi_ap');
    const others = t.nodes.filter((n) => n.id !== 'backend' && n.kind !== 'wifi_ap');
    aps.forEach((n, i) => { const a = (i / Math.max(aps.length, 1)) * Math.PI * 2; pos[n.id] = [cx + Math.cos(a) * 150, cy + Math.sin(a) * 120]; });
    others.forEach((n, i) => { const a = (i / Math.max(others.length, 1)) * Math.PI * 2 + 0.3; pos[n.id] = [cx + Math.cos(a) * 400, cy + Math.sin(a) * 230]; });
    const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Network topology diagram' });
    for (const l of t.links) {
      const a = pos[l.from], b = pos[l.to];
      if (!a || !b) continue;
      svg.append(s('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1], class: `l-${l.type}` }, s('title', {}, `${LINK_TEXT[l.type] || l.type}${l.rssi != null ? ` · ${l.rssi} dBm` : ''}`)));
    }
    for (const n of t.nodes) {
      const [x, y] = pos[n.id];
      const c = s('circle', { cx: x, cy: y, r: n.id === 'backend' ? 22 : 13 });
      c.style.fill = n.status === 'offline' ? 'var(--panel-2)' : (KIND_COLOR[n.kind] || 'var(--muted)');
      if (n.status === 'offline') c.style.stroke = 'var(--bad)';
      svg.append(s('g', { class: 'node' }, c, s('text', { x, y: y + (n.id === 'backend' ? 36 : 26), 'text-anchor': 'middle' }, n.label.slice(0, 26)),
        s('title', {}, `${n.label} — ${n.kind} — ${n.status}`)));
    }
    mount(body, h('p', { class: 'notice' }, 'Only connections backed by telemetry or administrator configuration are drawn, each labelled by evidence. ',
      'This deployment uses Wi-Fi infrastructure mode; no multi-hop mesh routing is active, so none is shown.'),
    svg,
    h('div', { class: 'card' }, h('h2', {}, 'Link legend'), h('div', { class: 'stack' }, Object.entries(LINK_TEXT).map(([k, v]) => {
      const sw = s('svg', { width: 40, height: 10 }, s('line', { x1: 0, y1: 5, x2: 40, y2: 5, class: `l-${k}` }));
      return h('div', { class: 'row' }, sw, v);
    }))),
    table([{ label: 'From', key: 'from' }, { label: 'To', key: 'to' }, { label: 'Evidence', render: (l) => badge(l.type.toUpperCase(), l.type === 'observed' ? 'b-ok' : 'b-muted') },
      { label: 'Medium', key: 'medium' }, { label: 'RSSI', render: (l) => (l.rssi != null ? `${l.rssi} dBm` : '—') },
      { label: '', render: (l) => l.id && can('settings.manage') ? h('button', { class: 'btn small danger', onclick: () => api.del(`network-links/${l.id}`).then(load) }, 'Remove') : '' }], t.links));
  }

  async function addLink() {
    const t = await api.get('topology');
    const opts = t.nodes.map((n) => ({ value: n.id, label: `${n.label} (${n.kind})` }));
    const v = await formDialog('Add configured link', [
      { name: 'from_node', label: 'From', type: 'select', value: opts[0]?.value, options: opts }, { name: 'to_node', label: 'To', type: 'select', value: 'backend', options: opts },
      { name: 'medium', label: 'Medium', type: 'select', value: 'ethernet', options: ['wifi', 'ethernet', 'ble', 'esp_mesh', 'usb', 'other'].map((m) => ({ value: m, label: m })) },
      { name: 'note', label: 'Note' }], 'Add', 'Configured links document how you installed the network; they are drawn separately from observed links.');
    if (v) api.post('network-links', v).then(load).catch((e) => toast(e.message, 'bad'));
  }

  await load();
  const timer = setInterval(() => load().catch(() => {}), 20000);
  return () => clearInterval(timer);
}
