// SkyMesh OS shell: boot, lock screen / first-start setup, desktop, menubar, dock, launcher,
// notification centre, phone home screen and the system-wide SOS panel.
import { api } from './api.js';
import { h, mount, clear, toast, openDialog, clock, ago } from './ui.js';
import { state, can, on, connectLive, disconnectLive, raiseSos, currentSos, clearSos, flushSos, locationShare } from './state.js';
import { icon } from './os/icons.js';
import { WindowManager } from './os/wm.js';
import { qrSvg } from './os/qr.js';
import { showSosAlert } from './os/sosalert.js';

export const APPS = [
  { id: 'overview', label: 'Overview', icon: 'overview', load: () => import('./views/overview.js'), size: [900, 620] },
  { id: 'map', label: 'Campus Map', icon: 'map', load: () => import('./views/map.js'), full: true, size: [1180, 740] },
  { id: 'trainer', label: 'Map Trainer', icon: 'target', load: () => import('./views/trainer.js'), full: true, size: [1180, 740] },
  { id: 'chat', label: 'Messages', icon: 'chat', load: () => import('./views/chat.js'), full: true, size: [860, 580] },
  { id: 'emergency', label: 'Emergency', icon: 'emergency', load: () => import('./views/emergency.js'), size: [760, 620] },
  { id: 'people', label: 'People', icon: 'people', load: () => import('./views/people.js'), size: [820, 560] },
  { id: 'devices', label: 'Devices', icon: 'devices', load: () => import('./views/devices.js'), size: [1000, 580] },
  { id: 'adddevice', label: 'Add Device', icon: 'usb', perm: 'device.enroll', load: () => import('./views/adddevice.js'), size: [720, 600] },
  { id: 'cameras', label: 'Cameras', icon: 'cameras', perm: 'camera.view', load: () => import('./views/cameras.js'), size: [960, 640] },
  { id: 'topology', label: 'Network', icon: 'topology', perm: 'topology.view', load: () => import('./views/topology.js'), size: [960, 700] },
  { id: 'events', label: 'Event Log', icon: 'events', perm: 'events.view', load: () => import('./views/events.js'), size: [860, 560] },
  { id: 'reports', label: 'Reports', icon: 'reports', perm: 'reports.view', load: () => import('./views/reports.js'), size: [820, 520] },
  { id: 'admin', label: 'Administration', icon: 'admin', perm: 'user.manage', load: () => import('./views/admin.js'), size: [900, 620] },
  { id: 'settings', label: 'Settings', icon: 'settings', load: () => import('./views/settings.js'), size: [820, 600] },
  { id: 'diagnostics', label: 'Diagnostics', icon: 'diagnostics', perm: 'diagnostics.view', load: () => import('./views/diagnostics.js'), size: [900, 600] },
  { id: 'help', label: 'Guide', icon: 'help', load: () => import('./views/help.js'), size: [820, 640] },
];
const DOCK = ['overview', 'map', 'trainer', 'chat', 'emergency', 'people', 'devices', 'cameras', 'topology'];

const root = document.getElementById('root');
let wm = null;
let session = null;
let activeIncidents = 0;
const notifications = [];
const visibleApps = () => APPS.filter((a) => !a.perm || can(a.perm));

export function openApp(id, params) { return wm?.open(id, params); }
export function go(id) { openApp(id); }

// ---------------------------------------------------------------- boot

async function boot() {
  const log = h('div', { class: 'boot-log', role: 'status' });
  const screen = h('div', { class: 'boot' }, h('div', { class: 'boot-inner' },
    h('div', { class: 'boot-mark' }, icon('logo'), h('div', {}, h('b', {}, 'SkyMesh'), h('div', {}, h('span', {}, 'campus safety network · KD Research')))),
    log, h('div', { class: 'boot-bar' }, h('i'))));
  const quick = sessionStorage.getItem('skymesh.booted') === '1';
  if (!quick) mount(root, screen);
  const line = (text) => { const l = h('div', {}, h('span', { class: 'dim' }, '[ .... ] '), text); log.append(l); return l; };
  const mark = (l, ok, extra) => { l.firstChild.textContent = ok ? '[  ok  ] ' : '[ fail ] '; l.firstChild.className = ok ? 'ok' : 'fail'; if (extra) l.append(h('span', { class: 'dim' }, ` ${extra}`)); };
  const t0 = performance.now();
  const l1 = line('Contacting SkyMesh backend');
  let health;
  try { health = await api.get('health'); mark(l1, true, `v${health.version}`); } catch (e) {
    mark(l1, false, e.message);
    if (quick) mount(root, screen);
    log.append(h('div', {}, ' '), h('button', { class: 'btn primary', onclick: () => { sessionStorage.removeItem('skymesh.booted'); boot(); } }, 'Retry'));
    return;
  }
  mark(line('Database'), health.database === 'ok', health.database);
  const l3 = line('Session');
  session = await api.get('auth/session');
  mark(l3, true, session.authenticated ? `signed in as ${session.user.username}` : session.needs_setup ? 'first start' : 'locked');
  mark(line('Secure transport'), true, session.secure ? 'HTTPS' : 'HTTP (location needs HTTPS on phones)');
  if (!quick) await new Promise((r) => setTimeout(r, Math.max(0, 650 - (performance.now() - t0))));
  sessionStorage.setItem('skymesh.booted', '1');
  if (session.authenticated) await enterDesktop(session);
  else renderLock();
}

window.addEventListener('skymesh:unauthenticated', () => {
  if (!state.me) return;
  signOutLocal();
  renderLock('Your session ended. Please sign in again.');
});

// ---------------------------------------------------------------- lock screen / setup

function lockClock() {
  const t = h('div', { class: 't' }), d = h('div', { class: 'd' });
  const tick = () => {
    const n = new Date();
    t.textContent = n.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    d.textContent = n.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' });
  };
  tick();
  const timer = setInterval(() => { if (!t.isConnected) clearInterval(timer); else tick(); }, 1000);
  return h('div', { class: 'lock-clock' }, t, d);
}

function renderLock(notice) {
  const setup = session?.needs_setup;
  let mode = setup ? 'setup' : 'login';
  const err = h('div', { class: 'notice bad hidden', role: 'alert' });
  const f = {
    code: h('input', { type: 'text', autocomplete: 'off', placeholder: 'XXXX-XXXX', maxlength: 12 }),
    display: h('input', { type: 'text', autocomplete: 'name' }),
    user: h('input', { type: 'text', autocomplete: 'username' }),
    pass: h('input', { type: 'password', autocomplete: 'current-password' }),
    pass2: h('input', { type: 'password', autocomplete: 'new-password' }),
  };
  const field = (label, input, hint) => h('label', { class: 'field' }, label, input, hint ? h('span', { class: 'dim' }, hint) : null);
  const submit = h('button', { class: 'btn primary', type: 'submit' });
  const panel = h('div', { class: 'lock-panel' });
  const draw = () => {
    submit.textContent = { setup: 'Create administrator', login: 'Sign in', register: 'Create account' }[mode];
    f.pass.autocomplete = mode === 'login' ? 'current-password' : 'new-password';
    mount(panel,
      h('h2', {}, { setup: 'Welcome to SkyMesh', login: 'Sign in', register: 'Create your account' }[mode]),
      h('p', { class: 'dim' }, { setup: 'First start. Create the administrator account. The setup code is shown in the SkyMesh server window.',
        login: 'Campus safety network', register: 'Accounts start as ordinary users. An administrator can grant responder access.' }[mode]),
      notice ? h('div', { class: 'notice warn' }, notice) : null,
      h('form', { class: 'stack', onsubmit: go_ },
        mode === 'setup' ? field('Setup code', f.code) : null,
        mode !== 'login' ? field('Your name', f.display) : null,
        field('Username', f.user, mode !== 'login' ? '3–32 letters, digits, . _ -' : null),
        field('Password', f.pass, mode !== 'login' ? 'At least 10 characters' : null),
        mode !== 'login' ? field('Repeat password', f.pass2) : null,
        err,
        h('div', { class: 'row' }, submit,
          mode === 'login' && session?.allow_registration ? h('button', { class: 'btn', type: 'button', onclick: () => { mode = 'register'; draw(); } }, 'Create account') : null,
          mode === 'register' ? h('button', { class: 'btn', type: 'button', onclick: () => { mode = 'login'; draw(); } }, 'Back to sign in') : null)));
    (mode === 'setup' ? f.code : f.user).focus();
  };
  async function go_(e) {
    e.preventDefault();
    err.classList.add('hidden');
    if (mode !== 'login' && f.pass.value !== f.pass2.value) { err.textContent = 'The passwords do not match.'; err.classList.remove('hidden'); return; }
    submit.disabled = true;
    try {
      const base = { username: f.user.value.trim(), password: f.pass.value };
      let r;
      if (mode === 'setup') r = await api.post('setup', { ...base, display_name: f.display.value.trim() || base.username, setup_code: f.code.value });
      else if (mode === 'register') r = await api.post('auth/register', { ...base, display_name: f.display.value.trim() || base.username });
      else r = await api.post('auth/login', base, { allow401: true });
      notice = null;
      await enterDesktop({ authenticated: true, user: r.user, permissions: r.permissions, firstRun: mode === 'setup' });
    } catch (e2) {
      err.textContent = e2.message; err.classList.remove('hidden');
    } finally { submit.disabled = false; }
  }
  draw();
  mount(root, h('div', { class: 'wallpaper grid' }), h('div', { class: 'lock' }, lockClock(),
    h('div', {}, panel, h('div', { class: 'lock-foot' }, 'SkyMesh OS · local network only · nothing leaves this campus'))));
}

// ---------------------------------------------------------------- desktop

let menubar, desktopEl, dockEl, widgetEl, homeEl, ncEl, wallEl;

async function enterDesktop(s) {
  state.me = s.user;
  state.perms = new Set(s.permissions);
  state.config = await api.get('config').catch(() => null);
  wallEl = h('div', { class: 'wallpaper' });
  menubar = h('header', { class: 'menubar' });
  desktopEl = h('div', { class: 'desktop' });
  dockEl = h('nav', { class: 'dock', 'aria-label': 'Dock' });
  widgetEl = h('aside', { class: 'widget widget-desk', 'aria-label': 'System status' });
  homeEl = h('div', { class: 'home hidden' });
  mount(root, wallEl, desktopEl, homeEl, menubar, dockEl);
  desktopEl.append(widgetEl);
  desktopEl.addEventListener('contextmenu', desktopMenu);
  wallEl.addEventListener('contextmenu', desktopMenu);
  wm = new WindowManager(desktopEl, visibleApps(), () => { renderMenubar(); renderDock(); });
  applyWallpaper();
  applyLayout();
  renderMenubar(); renderDock(); refreshStatus();
  connectLive();
  flushSos();
  if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});
  if (location.hash.length > 2) handleHash();
  else if (s.firstRun) openApp('help');
  else if (!wm.mobile) openApp('overview');
}

function signOutLocal() {
  wm?.closeAll();
  wm = null;
  state.me = null;
  disconnectLive();
  locationShare.stopWatch();
}

async function signOut() {
  await locationShare.stop().catch(() => {});
  await api.post('auth/logout').catch(() => {});
  signOutLocal();
  session = await api.get('auth/session').catch(() => session);
  renderLock();
}

// ---------------------------------------------------------------- wallpaper

export const WALLPAPERS = [
  { id: 'plan', label: 'Campus plan' }, { id: 'grid', label: 'Dot grid' }, { id: 'lines', label: 'Night lines' },
  { id: 'aurora', label: 'Ember' }, { id: 'topo', label: 'Contours' }, { id: 'dusk', label: 'Dusk' },
  { id: 'plain', label: 'Plain black' }, { id: 'custom', label: 'Your picture' },
];
const lsGet = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
const lsSet = (k, v) => { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); return true; } catch { return false; } };

export async function applyWallpaper() {
  if (!wallEl) return;
  const pref = lsGet('skymesh.wallpaper', 'plan');
  clear(wallEl);
  wallEl.style.backgroundImage = '';
  wallEl.className = `wallpaper wp-${pref}`;
  if (pref === 'custom') {
    const data = lsGet('skymesh.wallpaper.custom', null);
    if (data) wallEl.style.backgroundImage = `url("${data}")`;
    else wallEl.className = 'wallpaper wp-grid';
  }
  if (pref === 'plan') {
    const b = await api.get('buildings').catch(() => []);
    const img = b.flatMap((x) => x.floors).find((f) => f.image)?.image;
    if (img) wallEl.append(h('img', { src: img.url, alt: '' }));
  }
}
window.addEventListener('skymesh:wallpaper', applyWallpaper);

// Downscale an uploaded picture so it fits in browser storage (kept on this computer only).
function shrinkImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, 1920 / Math.max(img.width, img.height));
      const c = document.createElement('canvas');
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      resolve(c.toDataURL('image/jpeg', 0.82));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('That file is not an image')); };
    img.src = url;
  });
}

export async function wallpaperDialog() {
  closeMenus();
  const grid = h('div', { class: 'wp-grid' });
  const file = h('input', { type: 'file', accept: 'image/*', class: 'hidden' });
  const draw = () => {
    const cur = lsGet('skymesh.wallpaper', 'plan');
    mount(grid, WALLPAPERS.map((w) => {
      const thumb = h('span', { class: `wp-thumb wallpaper wp-${w.id}` });
      if (w.id === 'custom') {
        const data = lsGet('skymesh.wallpaper.custom', null);
        if (data) thumb.style.backgroundImage = `url("${data}")`;
        else thumb.append(icon('plus'));
      }
      return h('button', { class: ['wp-item', cur === w.id && 'sel'], onclick: () => {
        if (w.id === 'custom' && !lsGet('skymesh.wallpaper.custom', null)) return file.click();
        lsSet('skymesh.wallpaper', w.id); applyWallpaper(); draw();
      } }, thumb, h('span', {}, w.label));
    }));
  };
  file.addEventListener('change', async () => {
    const f = file.files[0];
    if (!f) return;
    try {
      const data = await shrinkImage(f);
      if (!lsSet('skymesh.wallpaper.custom', data)) throw new Error('The picture is too large to keep in this browser');
      lsSet('skymesh.wallpaper', 'custom'); applyWallpaper(); draw();
    } catch (e) { toast(e.message, 'bad'); }
  });
  draw();
  await openDialog('Wallpaper', h('div', { class: 'stack' }, grid, file,
    h('div', { class: 'row' }, h('button', { class: 'btn small', onclick: () => file.click() }, icon('plus'), 'Upload a picture…'),
      h('span', { class: 'dim' }, 'Pictures stay on this computer only.'))), [{ label: 'Done', value: true, kind: 'primary' }]);
}

// ---------------------------------------------------------------- show desktop

let hiddenByShowDesktop = [];
export function showDesktop() {
  if (!wm) return;
  if (wm.mobile) { wm.closeAll(); return; }
  const visible = [...wm.wins.values()].filter((w) => !w.el.classList.contains('min'));
  if (visible.length) {
    hiddenByShowDesktop = visible.map((w) => w.id);
    visible.forEach((w) => wm.minimize(w.id));
  } else {
    hiddenByShowDesktop.forEach((id) => wm.wins.get(id) && wm.toggle(id));  // second click brings them back
    hiddenByShowDesktop = [];
  }
}

function desktopMenu(e) {
  if (e.target !== desktopEl && e.target !== wallEl) return;
  e.preventDefault();
  const anchor = h('span');
  anchor.style.position = 'fixed'; anchor.style.left = `${e.clientX}px`; anchor.style.top = `${e.clientY - 4}px`;
  document.body.append(anchor);
  menuAt(anchor, [
    { label: 'Change wallpaper…', icon: 'image', run: wallpaperDialog },
    { label: 'Show desktop', icon: 'desktop', run: showDesktop },
    { label: 'All apps…', icon: 'launcher', run: openLauncher },
    ...(can('device.enroll') ? [{ label: 'Add a badge or camera…', icon: 'usb', run: () => openApp('adddevice') }] : []),
    { label: 'Open on a phone…', icon: 'phone', run: phoneDialog },
    '-', { label: 'Close all windows', icon: 'close', run: () => wm.closeAll() }]);
  setTimeout(() => anchor.remove(), 0);
}

const mq = window.matchMedia('(max-width: 720px)');
function applyLayout() {
  const mobile = mq.matches;
  document.body.classList.toggle('mobile', mobile);
  if (!wm) return;
  wm.mobile = mobile;
  homeEl.classList.toggle('hidden', !mobile);
  if (mobile) renderHome();
}
mq.addEventListener('change', applyLayout);

// ---------------------------------------------------------------- menubar

function menuAt(anchor, items) {
  closeMenus();
  const m = h('div', { class: 'menu', role: 'menu' }, items.map((it) => it === '-' ? h('div', { class: 'sep' })
    : it.header ? h('div', { class: 'hdr' }, it.header)
      : h('button', { role: 'menuitem', onclick: () => { closeMenus(); it.run(); } }, it.icon ? icon(it.icon) : null, it.label)));
  document.body.append(m);
  const r = anchor.getBoundingClientRect();
  m.style.top = `${r.bottom + 4}px`;
  m.style.left = `${Math.min(r.left, window.innerWidth - m.offsetWidth - 8)}px`;
  anchor.classList.add('open');
  setTimeout(() => document.addEventListener('pointerdown', outside), 0);
  function outside(e) { if (!m.contains(e.target)) closeMenus(); }
  m._off = () => { document.removeEventListener('pointerdown', outside); anchor.classList.remove('open'); };
}
function closeMenus() { document.querySelectorAll('.menu').forEach((m) => { m._off?.(); m.remove(); }); }

function renderMenubar() {
  if (!menubar || !state.me) return;
  const focused = wm?.focused ? wm.app(wm.focused) : null;
  const connCls = { online: 'ok', connecting: 'warn', offline: 'bad' }[state.conn] || 'warn';
  const connTxt = { online: 'Live', connecting: 'Connecting', offline: 'Offline' }[state.conn] || '';
  const unread = notifications.filter((n) => !n.read).length;
  const logo = h('button', { class: 'mb-item mb-logo', 'aria-label': 'SkyMesh menu' }, icon('logo'));
  logo.onclick = () => menuAt(logo, [
    { header: `SkyMesh OS · ${state.me.display_name}` },
    { label: 'About SkyMesh', icon: 'logo', run: about },
    { label: 'All apps…', icon: 'launcher', run: openLauncher },
    { label: 'Open on a phone…', icon: 'phone', run: phoneDialog },
    ...(can('device.enroll') ? [{ label: 'Add a badge or camera…', icon: 'usb', run: () => openApp('adddevice') }] : []),
    { label: 'Setup guide', icon: 'help', run: () => openApp('help') },
    { label: 'Settings', icon: 'settings', run: () => openApp('settings') },
    { label: 'Change wallpaper…', icon: 'image', run: wallpaperDialog },
    { label: 'Show desktop', icon: 'desktop', run: showDesktop },
    '-', { label: 'Close all windows', icon: 'close', run: () => wm.closeAll() },
    { label: 'Sign out', icon: 'power', run: signOut }]);
  const user = h('button', { class: 'mb-item mb-hide-m' }, icon('user'), state.me.display_name);
  user.onclick = () => menuAt(user, [{ header: `@${state.me.username} · ${state.me.role}` },
    { label: 'Settings', icon: 'settings', run: () => openApp('settings') }, { label: 'Sign out', icon: 'power', run: signOut }]);
  mount(menubar, logo,
    h('span', { class: 'mb-item mb-app' }, focused ? focused.label : 'Desktop'),
    h('span', { class: 'mb-spacer' }),
    activeIncidents ? h('button', { class: 'mb-item mb-alert', onclick: () => openApp('emergency') }, icon('emergency'), `${activeIncidents} active SOS`) : null,
    locationShare.active() ? h('button', { class: 'mb-item mb-share', onclick: () => openApp('settings'), title: 'Your location is being shared' }, 'Sharing location') : null,
    h('span', { class: 'mb-item', title: `Connection to the SkyMesh backend: ${connTxt}` }, h('span', { class: ['mb-dot', connCls] }), h('span', { class: 'mb-hide-m' }, connTxt)),
    h('button', { class: 'mb-item', 'aria-label': `Notifications (${unread} unread)`, onclick: toggleNC }, icon('bell'), unread ? h('span', { class: 'count' }, String(unread)) : null),
    h('button', { class: 'mb-item mb-hide-m', 'aria-label': 'Search apps', onclick: openLauncher }, icon('search')),
    user,
    h('span', { class: 'mb-item mb-clock', id: 'mb-clock' }),
    h('button', { class: 'mb-sos', onclick: openSosPanel, 'aria-label': 'Send an SOS emergency alert' }, 'SOS'),
    h('button', { class: 'mb-desktop', onclick: showDesktop, title: 'Show desktop (click again to bring the windows back)', 'aria-label': 'Show desktop' }));
  tickClock();
}
function tickClock() {
  const el = document.getElementById('mb-clock');
  if (!el) return;
  const n = new Date();
  el.textContent = `${n.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })}  ${n.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}
setInterval(tickClock, 10000);

export async function phoneDialog() {
  const cfg = await api.get('config').catch(() => ({}));
  const url = cfg.phone_url || location.origin;
  await openDialog('Open SkyMesh on a phone', h('div', { class: 'stack' },
    h('div', { class: 'qr-box' }, qrSvg(url, 190), h('ol', { class: 'steps' },
      h('li', {}, 'Connect the phone to the same Wi-Fi as this computer.'),
      h('li', {}, 'Point the phone camera at the code and open the link.'),
      cfg.https ? h('li', {}, 'First time only: tap ', h('b', {}, 'Advanced → Proceed'), ' on the certificate warning.') : null,
      h('li', {}, 'Sign in, then Settings → Share my live location.'))),
    h('div', { class: 'mono dim' }, url),
    cfg.https ? h('details', {}, h('summary', {}, 'Remove the certificate warning for good'), h('div', { class: 'qr-box' }, qrSvg(`${url}/skymesh-cert.crt`, 130),
      h('p', { class: 'dim' }, 'Scan this on the phone to download the SkyMesh certificate, then install it: Android Settings → Security → Encryption & credentials → Install a certificate → CA certificate. iPhone: Settings → Profile Downloaded → Install, then General → About → Certificate Trust Settings → enable SkyMesh.'))) : null),
  [{ label: 'Close', value: true, kind: 'primary' }]);
}

async function about() {
  const hl = await api.get('health').catch(() => ({}));
  openDialog('About SkyMesh OS', h('div', { class: 'stack' },
    h('div', { class: 'row' }, icon('logo'), h('b', {}, 'SkyMesh OS'), h('span', { class: 'dim mono' }, `v${hl.version || '?'}`)),
    h('p', { class: 'dim' }, 'Local-first campus communication and safety network by KD Research. Runs entirely on your campus network.'),
    h('dl', { class: 'kv' }, h('dt', {}, 'backend'), h('dd', {}, hl.status || 'unreachable'), h('dt', {}, 'database'), h('dd', {}, hl.database || '—'),
      h('dt', {}, 'connection'), h('dd', {}, location.protocol === 'https:' ? 'HTTPS (encrypted)' : 'HTTP'))), [{ label: 'Close', value: true, kind: 'primary' }]);
}

// ---------------------------------------------------------------- dock, home, widget

function renderDock() {
  if (!dockEl || !wm) return;
  const apps = visibleApps();
  const pinned = DOCK.map((id) => apps.find((a) => a.id === id)).filter(Boolean);
  const extra = apps.filter((a) => !DOCK.includes(a.id) && wm.isOpen(a.id));
  const btn = (a) => h('button', { class: ['dock-btn', wm.isOpen(a.id) && 'running', wm.focused === a.id && 'focused'],
    'aria-label': a.label, onclick: () => wm.toggle(a.id) }, icon(a.icon), h('span', { class: 'tip' }, a.label),
  a.id === 'emergency' && activeIncidents ? h('span', { class: 'count' }, String(activeIncidents)) : null);
  mount(dockEl, pinned.map(btn), extra.length ? h('span', { class: 'dock-sep' }) : null, extra.map(btn),
    h('span', { class: 'dock-sep' }),
    h('button', { class: 'dock-btn', 'aria-label': 'All apps', onclick: openLauncher }, icon('launcher'), h('span', { class: 'tip' }, 'All apps  ·  Ctrl+K')),
    h('button', { class: 'dock-btn', 'aria-label': 'Show desktop', onclick: showDesktop }, icon('desktop'), h('span', { class: 'tip' }, 'Show desktop')));
}

function renderHome() {
  if (!homeEl) return;
  const homeWidget = h('div', { class: 'widget' });
  mount(homeEl, homeWidget, h('div', { class: 'home-grid' }, visibleApps().map((a) => h('button', {
    class: 'home-app', onclick: () => { wm.closeAll(); openApp(a.id); } },
  h('span', { class: 'tile' }, icon(a.icon)), a.label,
  a.id === 'emergency' && activeIncidents ? h('span', { class: 'count' }, String(activeIncidents)) : null))));
  fillWidget(homeWidget, lastStatus);
}

let lastStatus = null;
async function refreshStatus() {
  if (!state.me) return;
  try {
    const [ov, checklist] = await Promise.all([api.get('overview'),
      can('settings.manage') ? api.get('setup/checklist').catch(() => null) : null]);
    lastStatus = { ov, checklist };
    activeIncidents = ov.incidents.active;
  } catch { /* connection indicator shows the problem */ }
  fillWidget(widgetEl, lastStatus);
  if (wm?.mobile) renderHome();
  renderMenubar(); renderDock();
}
function fillWidget(el, st) {
  if (!el) return;
  if (!st) { mount(el, h('h3', {}, 'System'), h('div', { class: 'dim' }, 'Waiting for the backend…')); return; }
  const { ov, checklist } = st;
  const stat = (label, value, app, alert) => h('div', { class: ['stat', alert && 'alert'], onclick: () => openApp(app) }, h('span', {}, label), h('b', {}, String(value)));
  mount(el, h('h3', {}, 'System status'),
    stat('Active SOS', ov.incidents.active, 'emergency', ov.incidents.active > 0),
    stat('Devices online', `${ov.devices.online}/${ov.devices.registered}`, 'devices'),
    stat('Live locations', ov.locations.live, 'map'),
    stat('Cameras', ov.cameras.length ? `${ov.cameras.filter((c) => c.online).length}/${ov.cameras.length}` : '—', can('camera.view') ? 'cameras' : 'overview'),
    h('div', { class: 'row', style: null }, h('button', { class: 'btn small', onclick: phoneDialog }, icon('phone'), 'Phone'),
      can('device.enroll') ? h('button', { class: 'btn small', onclick: () => openApp('adddevice') }, icon('usb'), 'Add device') : null),
    checklist && checklist.done < checklist.total ? h('a', { class: 'setup', href: '#/help', onclick: (e) => { e.preventDefault(); openApp('help'); } },
      h('div', { class: 'row' }, h('span', {}, 'Finish setting up'), h('span', { class: 'spacer' }), h('span', { class: 'mono dim' }, `${checklist.done}/${checklist.total}`)),
      h('div', { class: 'bar' }, (() => { const i = h('i'); i.style.width = `${(checklist.done / checklist.total) * 100}%`; return i; })())) : null);
}
let statusT;
const statusSoon = () => { clearTimeout(statusT); statusT = setTimeout(refreshStatus, 600); };
['ws:incident', 'ws:device', 'ws:device_online', 'ws:location', 'reconnected'].forEach((t) => on(t, statusSoon));
setInterval(() => state.me && state.conn !== 'offline' && refreshStatus(), 30000);  // no polling while the server is down

// ---------------------------------------------------------------- launcher

function openLauncher() {
  if (document.querySelector('.launcher')) return;
  closeMenus();
  const input = h('input', { type: 'search', placeholder: 'Search apps and actions…', 'aria-label': 'Search' });
  const grid = h('div', { class: 'launcher-grid' });
  let sel = 0, list = [];
  const actions = [{ label: 'Send SOS', icon: 'emergency', run: openSosPanel }, { label: 'Share my location', icon: 'location', run: () => openApp('settings') },
    { label: 'Open on a phone', icon: 'phone', run: phoneDialog },
    { label: 'Sign out', icon: 'power', run: signOut }];
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    list = [...visibleApps().map((a) => ({ label: a.label, icon: a.icon, run: () => openApp(a.id) })), ...actions].filter((x) => !q || x.label.toLowerCase().includes(q));
    sel = Math.min(sel, Math.max(0, list.length - 1));
    mount(grid, list.length ? list.map((x, i) => h('button', { class: i === sel ? 'sel' : '', onclick: () => { close(); x.run(); } }, icon(x.icon), x.label))
      : h('div', { class: 'dim' }, 'Nothing matches.'));
  };
  const overlay = h('div', { class: 'launcher', onclick: (e) => { if (e.target === overlay) close(); } },
    h('div', { class: 'launcher-box', role: 'dialog', 'aria-label': 'Launcher' },
      h('div', { class: 'launcher-search' }, icon('search'), input), grid,
      h('div', { class: 'launcher-hint' }, 'Enter opens · ← → choose · Esc closes · Ctrl+K anywhere')));
  function close() { overlay.remove(); }
  input.addEventListener('input', () => { sel = 0; draw(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { sel = Math.min(sel + 1, list.length - 1); draw(); e.preventDefault(); }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); draw(); e.preventDefault(); }
    if (e.key === 'Enter' && list[sel]) { close(); list[sel].run(); }
  });
  draw();
  document.body.append(overlay);
  input.focus();
}
document.addEventListener('keydown', (e) => {
  if (!state.me) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openLauncher(); }
  if (e.key === 'Escape') { closeMenus(); document.querySelector('.launcher')?.remove(); }
});

// ---------------------------------------------------------------- notification centre

window.addEventListener('skymesh:notify', (e) => {
  notifications.unshift({ ...e.detail, at: new Date().toISOString(), read: !!ncEl });
  notifications.splice(50);
  if (ncEl) renderNC();
  renderMenubar();
});
function toggleNC() {
  if (ncEl) { ncEl.remove(); ncEl = null; return; }
  notifications.forEach((n) => { n.read = true; });
  ncEl = h('aside', { class: 'ncenter', 'aria-label': 'Notifications' });
  document.body.append(ncEl);
  renderNC();
  renderMenubar();
}
function renderNC() {
  mount(ncEl, h('header', {}, h('b', {}, 'Notifications'),
    h('button', { class: 'btn small', onclick: () => { notifications.length = 0; renderNC(); renderMenubar(); } }, 'Clear'),
    h('button', { class: 'win-btn', 'aria-label': 'Close', onclick: toggleNC }, icon('close'))),
  h('div', { class: 'list' }, notifications.length ? notifications.map((n) => h('div', { class: ['n', n.kind === 'sos' && 'sos'] }, n.text, h('small', {}, ago(n.at))))
    : h('div', { class: 'dim' }, 'No notifications.')));
}

// ---------------------------------------------------------------- live indicators

on('conn', () => renderMenubar());
on('sharing', () => renderMenubar());
// A new SOS, or a repeat press of an open one: the alarm on the desktop (shown for sos.alert_display_s).
on('ws:sos_alert', (e) => { if (can('incident.view_all')) showSosAlert(e.detail, openApp); });
on('ws:device_online', (e) => can('device.view_all') && toast(`${e.detail.name} came online`, 'ok'));

// Links inside apps use #/app or #/app?key=value: open (or focus) that app.
function handleHash() {
  const m = location.hash.match(/^#\/([a-z]+)(?:\?(.*))?$/);
  if (!m || !wm) return;
  const params = Object.fromEntries(new URLSearchParams(m[2] || ''));
  history.replaceState(null, '', location.pathname);
  if (wm.mobile) wm.closeAll();
  openApp(m[1], params);
}
window.addEventListener('hashchange', handleHash);

// ---------------------------------------------------------------- SOS panel

function sosStatusView() {
  const s = currentSos();
  if (!s) return h('div', { class: 'sos-state' }, 'No alert sent from this device.');
  if (s.phase === 'sending') return h('div', { class: 'sos-state pending' }, 'Sending… not yet delivered.');
  if (s.phase === 'not_delivered') return h('div', { class: 'sos-state failed' },
    'NOT DELIVERED. The backend has not received your alert.', h('div', { class: 'dim' }, s.error || ''),
    h('div', {}, 'Retrying automatically. If you are in danger, also call for help by phone or go to the nearest exit.'));
  const inc = s.incident;
  const acked = ['ACKNOWLEDGED', 'RESPONDING', 'RESOLVED'].includes(inc.state);
  return h('div', { class: ['sos-state', acked ? 'acked' : 'delivered'] },
    acked ? `Acknowledged by a responder (${inc.state.toLowerCase()})` : 'Delivered. Waiting for a responder to acknowledge.',
    h('div', { class: 'dim' }, `${clock(inc.created_at)} · ${inc.location_note}`));
}

async function openSosPanel() {
  closeMenus();
  const statusBox = h('div', {}, sosStatusView());
  const off = on('sos', () => mount(statusBox, sosStatusView()));
  const fill = h('span', { class: 'fill' });
  let timer, start;
  const holdBtn = h('button', { class: 'hold-btn', type: 'button' }, fill, h('span', {}, 'HOLD FOR SOS'));
  const msg = h('input', { type: 'text', placeholder: 'Optional: what is happening?', maxlength: 500 });
  const begin = (e) => {
    e.preventDefault();
    start = performance.now();
    const tick = () => {
      const p = Math.min(1, (performance.now() - start) / 1500);
      fill.style.width = `${p * 100}%`;
      if (p >= 1) { raiseSos(msg.value.trim()); fill.style.width = '0'; return; }
      timer = requestAnimationFrame(tick);
    };
    timer = requestAnimationFrame(tick);
  };
  const end = () => { cancelAnimationFrame(timer); fill.style.width = '0'; };
  holdBtn.addEventListener('pointerdown', begin);
  ['pointerup', 'pointerleave', 'pointercancel'].forEach((t) => holdBtn.addEventListener(t, end));
  holdBtn.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && !e.repeat) begin(e); });
  holdBtn.addEventListener('keyup', end);
  const shareNote = locationShare.active() ? 'Your live location will be attached.'
    : 'Location sharing is off: responders will see your last known location, or "location unavailable".';
  const body = h('div', { class: 'stack' }, h('p', { class: 'dim' }, 'Hold the button for 1.5 seconds to alert campus responders.'),
    msg, holdBtn, h('p', { class: 'dim' }, shareNote), statusBox);
  const res = await openDialog('Emergency SOS', body, [{ label: 'Start a new alert', value: 'clear' }, { label: 'Close', value: 'close', kind: 'primary' }]);
  off();
  if (res === 'clear') {
    const s = currentSos();
    if (s && s.phase !== 'delivered') toast('The previous alert was not delivered yet; it keeps retrying.', 'bad');
    else clearSos();
  }
}

boot();
