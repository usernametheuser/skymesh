// Guide: live setup checklist plus plain-language how-tos.
import { api } from '../api.js';
import { h, mount, toast } from '../ui.js';
import { state, can, locationShare } from '../state.js';
import { icon } from '../os/icons.js';
import { qrSvg } from '../os/qr.js';

const STATES = [
  ['LIVE', 'A fresh position (under 30 s old).'],
  ['LOW CONFIDENCE', 'Fresh, but accuracy is worse than 25 m.'],
  ['RECENT', '30 s – 2 min old.'],
  ['STALE', '2 – 15 min old. Not the current position.'],
  ['LAST KNOWN', 'Older than 15 min: history only.'],
  ['OFFLINE', 'The badge stopped reporting.'],
  ['SHARING DISABLED', 'The owner turned sharing off.'],
  ['PERMISSION DENIED', 'The phone refused location access.'],
  ['POSITION UNAVAILABLE', 'Nothing to show yet (e.g. no anchors heard).'],
];

export async function render(page) {
  const cfg = await api.get('config').catch(() => state.config || {});
  const ip = cfg.lan_ips?.[0] || location.hostname;
  const phoneUrl = cfg.https ? `https://${ip}:8443` : `http://${ip}:8000`;
  const deviceUrl = `http://${ip}:8000`;
  const tabsEl = h('div', { class: 'tabs', role: 'tablist' });
  const body = h('div', {});
  const tabs = {
    ...(can('settings.manage') ? { checklist: ['Setup checklist', checklistTab] } : { start: ['Get started', userStartTab] }),
    phones: ['Connect phones', phonesTab],
    devices: ['Badges & cameras', devicesTab],
    reference: ['Reference', referenceTab],
  };
  let cur = Object.keys(tabs)[0];
  const renderTabs = () => mount(tabsEl, Object.entries(tabs).map(([k, [label]]) => h('button', { class: cur === k ? 'active' : '', role: 'tab',
    onclick: () => { cur = k; renderTabs(); tabs[k][1](); } }, label)));
  mount(page, tabsEl, body);

  async function checklistTab() {
    let c;
    try { c = await api.get('setup/checklist'); } catch (e) { mount(body, h('div', { class: 'notice bad' }, e.message)); return; }
    const pct = Math.round((c.done / c.total) * 100);
    const bar = h('i'); bar.style.width = `${pct}%`;
    mount(body, h('div', { class: 'card stack' },
      h('div', { class: 'row' }, h('b', {}, `${c.done} of ${c.total} done`), h('span', { class: 'spacer' }),
        h('button', { class: 'btn small', onclick: checklistTab }, 'Refresh')),
      h('div', { class: 'widget-bar' }, bar),
      h('div', {}, c.items.map((it) => h('div', { class: ['check', it.done && 'done'] },
        h('span', { class: 'box' }, it.done ? icon('check') : null),
        h('div', { class: 'txt' }, h('b', {}, it.title), it.optional ? h('span', { class: 'dim' }, '  · optional') : null,
          h('div', { class: 'dim' }, it.detail)),
        it.done ? null : h('a', { class: 'btn small', href: `#/${it.app}` }, 'Open'))))));
    const wb = body.querySelector('.widget-bar');
    Object.assign(wb.style, { height: '3px', background: 'var(--border)', borderRadius: '2px', overflow: 'hidden' });
    Object.assign(bar.style, { display: 'block', height: '100%', background: 'var(--accent)' });
  }

  function userStartTab() {
    mount(body, h('div', { class: 'card' }, h('ol', { class: 'steps' },
      h('li', {}, h('b', {}, 'Share your location (optional). '), 'Settings → Location sharing → ', h('i', {}, 'Share my live location'),
        '. You can stop at any time. Only responders see it.'),
      h('li', {}, h('b', {}, 'Know the SOS button. '), 'The red SOS button is always in the top bar. Hold it for 1.5 s. It says "Delivered" only when the system really received it.'),
      h('li', {}, h('b', {}, 'Message people. '), 'Open Messages, then + Direct or + Group.'),
      h('li', {}, h('b', {}, 'On your phone, '), 'open ', h('code', {}, phoneUrl), ' and sign in with the same account.')),
    h('div', { class: 'row' }, h('a', { class: 'btn primary', href: '#/settings' }, 'Open Settings'),
      locationShare.active() ? h('span', { class: 'badge b-ok' }, 'SHARING') : null)));
  }

  function phonesTab() {
    mount(body, h('div', { class: 'stack' },
      h('div', { class: 'card qr-box' }, qrSvg(cfg.phone_url || phoneUrl, 170), h('div', { class: 'stack' },
        h('b', {}, 'Scan with the phone camera'), h('span', { class: 'mono dim' }, cfg.phone_url || phoneUrl),
        h('span', { class: 'dim' }, 'The phone must be on the same Wi-Fi as this computer.'))),
      h('div', { class: 'card' }, h('ol', { class: 'steps' },
        h('li', {}, 'Connect the phone to the ', h('b', {}, 'same Wi-Fi'), ' as this computer.'),
        h('li', {}, 'Open ', h('code', {}, phoneUrl), ' in Chrome (Android) or Safari (iPhone).'),
        cfg.https ? h('li', {}, 'You will see a certificate warning the first time. This is expected: SkyMesh uses its own certificate on your network. ',
          'Tap ', h('b', {}, 'Advanced → Proceed'), '. To remove the warning for good, install ', h('a', { href: '/skymesh-cert.crt' }, 'the SkyMesh certificate'),
          ' on the phone (Settings → Security → Install certificate).') : h('li', {}, 'HTTPS is off, so phones cannot share GPS. Restart SkyMesh with ', h('code', {}, 'Start SkyMesh.bat'), ', which enables it.'),
        h('li', {}, 'Sign in. Open ', h('b', {}, 'Settings → Share my live location'), ' and allow location when the phone asks.'),
        h('li', {}, 'Keep SkyMesh open on screen while sharing: phones pause web pages in the background.')),
      h('div', { class: 'notice' }, 'Indoors, GPS is often off by 10–50 m. Use ', h('b', {}, 'I’m in a specific room…'),
        ' for room-level sharing, or install BLE anchors for badges.'))));
  }

  function devicesTab() {
    mount(body, h('div', { class: 'stack' },
      h('div', { class: 'card' }, h('h2', {}, 'The easy way: install over USB'), h('ol', { class: 'steps' },
        h('li', {}, 'On this computer, open SkyMesh in ', h('b', {}, 'Chrome or Edge'), '.'),
        h('li', {}, 'Open ', h('b', {}, 'Add Device'), ', choose Badge or Camera, and type your Wi-Fi name and password.'),
        h('li', {}, 'Plug the board in by USB (ESP32-CAM: on its ESP32-CAM-MB adapter) and press ', h('b', {}, 'Connect & install'), '.'),
        h('li', {}, 'Wait about a minute. SkyMesh installs the firmware, sends the settings, and tells you when the device is online.')),
        can('device.enroll') ? h('a', { class: 'btn primary', href: '#/adddevice' }, 'Open Add Device') : null,
        h('p', { class: 'dim' }, 'No Arduino IDE, no files to edit. To move a device to a new Wi-Fi, run Add Device again. If the SkyMesh computer gets a new IP address, devices find it again by themselves.')),
      h('details', {}, h('summary', {}, 'Advanced: flash with the Arduino IDE instead'), h('div', { class: 'stack' },
      h('div', { class: 'card' }, h('h2', {}, 'Wearable badge (ESP32-S3-CAM)'), h('ol', { class: 'steps' },
        h('li', {}, 'Devices → ', h('b', {}, 'Enroll device'), ' → type Badge, choose who wears it. Copy the three ', h('code', {}, '#define'), ' lines it shows.'),
        h('li', {}, 'In the project folder open ', h('code', {}, 'firmware/SkyMesh_Badge'), ', copy ', h('code', {}, 'secrets.example.h'), ' to ', h('code', {}, 'secrets.h'),
          ', paste the lines, and fill in Wi-Fi name, password and ', h('code', {}, `BACKEND_URL "${deviceUrl}"`), '.'),
        h('li', {}, 'Arduino IDE: board ', h('b', {}, 'ESP32S3 Dev Module'), ', Partition ', h('b', {}, 'Huge APP'),
          ', USB CDC On Boot ', h('b', {}, 'Enabled'), ', USB Mode ', h('b', {}, 'Hardware CDC and JTAG'), '. Upload ', h('code', {}, 'SkyMesh_Badge.ino'), '.'),
        h('li', {}, 'Serial monitor 115200 → type ', h('code', {}, 'status'), '. Look for ', h('code', {}, 'last_hb_http=200'), '. The badge turns ONLINE in Devices.'),
        h('li', {}, 'Test: hold the BOOT button 1.5 s → an SOS appears in Emergency. Acknowledge it → the badge prints CONFIRMED.'))),
      h('div', { class: 'card' }, h('h2', {}, 'Camera node (ESP32-CAM AI-Thinker)'), h('ol', { class: 'steps' },
        h('li', {}, 'Devices → Enroll device → type Camera node. Copy the lines (including the camera key).'),
        h('li', {}, 'Fill ', h('code', {}, 'firmware/SkyMesh_Server/secrets.h'), ' the same way. Board: ', h('b', {}, 'AI Thinker ESP32-CAM'), '.'),
        h('li', {}, 'Flash with the ESP32-CAM-MB adapter, then power it from a 5 V 2 A supply.'),
        h('li', {}, 'Cameras → ', h('b', {}, 'Snapshot'), ' to check the picture. Place it on the map: Campus Map → Edit map → Place infrastructure.'))))),
      h('p', { class: 'dim' }, 'Wiring, power and safety notes: docs/wiring.md in the project folder.')));
  }

  function referenceTab() {
    mount(body, h('div', { class: 'grid two' },
      h('section', { class: 'card' }, h('h2', {}, 'Location states'), h('dl', { class: 'kv' }, STATES.map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]))),
      h('section', { class: 'card stack' }, h('h2', {}, 'Reading the map'),
        h('p', {}, 'Solid dots are live. Faded, dashed dots are old positions. A highlighted room means only the room is known.'),
        h('p', {}, 'Orange diamonds are anonymous camera observations: never assumed to be a particular person.'),
        h('p', {}, 'Dashed brown outlines are unverified drafts: confirm them in Edit map.')),
      h('section', { class: 'card stack' }, h('h2', {}, 'Privacy'),
        h('p', {}, 'Sharing is off by default and only its owner can switch it. Every time a responder views locations it is logged.'),
        h('p', {}, 'No face recognition. Camera analysis is off until an administrator enables it.')),
      h('section', { class: 'card stack' }, h('h2', {}, 'Keyboard'),
        h('p', {}, h('code', {}, 'Ctrl+K'), ' launcher · ', h('code', {}, 'Esc'), ' close menus · double-click a title bar to maximise.'),
        h('p', {}, 'Map: arrows pan, + / − zoom, 0 fits.'))));
  }

  renderTabs();
  await tabs[cur][1]();
  if (!cfg.lan_ips?.length && location.hostname === 'localhost') toast('No network address found: connect this computer to Wi-Fi so phones and badges can reach it.', 'bad');
}
