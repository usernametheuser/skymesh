// Map Trainer: walk around with a badge (or a phone), tap where you stand, press "I'm here".
// Badge: its Wi-Fi scans at that spot are stored as a fingerprint; later every badge is located by
// matching what it hears against the trained spots. Phone: browsers cannot scan Wi-Fi, so "I'm here"
// shows you on the map and (for admins) records a GPS point that lines GPS up with the floor plan.
import { api } from '../api.js';
import { h, s, mount, badge, ago, toast, confirmDialog, onlineBadge } from '../ui.js';
import { can, on, locationShare } from '../state.js';
import { MapView, drawPlan, hit, polygonPoints } from '../mapview.js';

const SAMPLES = 4;
const SOURCE = { wifi_fingerprint: 'Wi-Fi fingerprint', map_trainer: 'your “I’m here”', ble_weighted_centroid: 'beacons (estimate)',
  ble_nearest_anchor: 'nearest beacon', manual_point: 'marked by hand', manual_room: 'room chosen by hand', browser_geolocation: 'phone GPS' };
const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { /* private mode */ } return null; };

export async function render(page) {
  const view = new MapView();
  const st = {
    floorId: null, floor: null, summary: null, badges: [], live: null, here: null, sel: null, session: null,
    mode: can('map.train') ? (store('skymesh.trainer.mode') || 'badge') : 'phone',
    deviceId: store('skymesh.trainer.device'), look: store('skymesh.maplook') || 'digital', gps: null,
  };

  // ------------------------------------------------------------ layout
  const floorSel = h('select', { 'aria-label': 'Floor', onchange: () => { st.floorId = floorSel.value; store('skymesh.floor', st.floorId); st.here = null; loadFloor(); } });
  const modeSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'What are you carrying' });
  const side = h('aside', { class: 'map-side' });
  const goBtn = h('button', { class: 'btn primary train-go', onclick: () => go() }, 'I’m here');
  const status = h('div', { class: 'train-status' });
  const bar = h('div', { class: 'map-ctl train-bar' }, status, goBtn);
  view.stage.append(bar);
  mount(page, h('div', { class: 'map-layout' },
    h('div', { class: 'map-main' }, h('div', { class: 'map-toolbar' }, floorSel, modeSeg, h('span', { class: 'spacer' }),
      h('span', { class: 'dim train-hint' }, 'Stand still · tap your spot · press I’m here')), view.stage),
    side));

  function renderMode() {
    const modes = can('map.train') ? [['badge', 'With a badge'], ['phone', 'With this phone']] : [['phone', 'With this phone']];
    mount(modeSeg, modes.map(([k, label]) => h('button', { class: ['seg-b', st.mode === k && 'on'], onclick: async () => {
      if (st.session) return toast('Finish or cancel the current spot first', 'bad');
      st.mode = k; store('skymesh.trainer.mode', k); renderMode();
      await loadLive();
      renderSide(); renderBar(); drawOverlay();
    } }, label)));
  }

  // ------------------------------------------------------------ data
  async function loadFloors() {
    const buildings = await api.get('buildings');
    const floors = buildings.flatMap((b) => b.floors.map((f) => ({ ...f, building: b.name })));
    mount(floorSel, floors.map((f) => h('option', { value: f.id }, `${f.building} — ${f.name}`)));
    if (!floors.length) return false;
    const saved = store('skymesh.floor');
    st.floorId = floors.some((f) => f.id === saved) ? saved : floors[0].id;
    floorSel.value = st.floorId;
    return true;
  }

  async function loadFloor() {
    st.floor = await api.get(`floors/${st.floorId}`);
    if (st.floor.image) await view.setImage(st.floor.image.url, st.floor.image.width, st.floor.image.height);
    drawPlan(view, st.floor.features, st.look);
    await loadSummary();
  }

  async function loadSummary() {
    st.summary = can('map.train') ? await api.get(`floors/${st.floorId}/training`).catch(() => null) : null;
    drawOverlay(); renderSide();
  }

  async function loadBadges() {
    if (!can('map.train')) return;
    st.badges = (await api.get('devices')).filter((d) => d.type === 'badge' && !d.revoked_at);
    if (!st.badges.some((b) => b.id === st.deviceId)) st.deviceId = (st.badges.find((b) => b.online) || st.badges[0])?.id || null;
    await loadLive();
  }

  async function loadLive() {
    const items = await api.get('locations/current').catch(() => []);
    const id = st.mode === 'badge' ? st.deviceId : locationShare.deviceId();
    st.live = items.find((i) => i.device_id === id) || null;
  }

  const badgeDev = () => st.badges.find((b) => b.id === st.deviceId);
  const mpu = () => st.floor?.image?.transform?.metres_per_unit || null;
  const roomAt = ([x, y]) => {
    let best = null, area = Infinity;
    for (const f of st.floor?.features || []) {
      if (f.geometry_type !== 'polygon' || !inPoly(x, y, f.geometry)) continue;
      const a = Math.abs(f.geometry.reduce((t, p, i, g) => t + p[0] * g[(i + 1) % g.length][1] - g[(i + 1) % g.length][0] * p[1], 0));
      if (a < area) { best = f; area = a; }
    }
    return best;
  };

  // ------------------------------------------------------------ drawing (zoom-dependent overlay only)
  function drawOverlay() {
    if (!st.floor?.image) return;
    const spotsG = view.clearLayer('anchors');
    const rooms = view.clearLayer('rooms');
    const pin = view.clearLayer('draw');
    const est = view.clearLayer('devices');
    if (st.mode === 'badge' && st.summary) {
      // Rooms nobody has trained yet: a dashed outline, so you can see where to walk next.
      const trained = new Set(st.summary.spots.map((p) => p.room_id).filter(Boolean));
      for (const f of st.floor.features) {
        if (f.geometry_type === 'polygon' && f.kind === 'room' && !trained.has(f.id)) {
          rooms.append(s('polygon', { points: polygonPoints(view, f.geometry), class: 'm-untrained' }));
        }
      }
      for (const p of st.summary.spots) {
        const c = s('circle', { cx: view.X(p.x), cy: view.Y(p.y), r: view.px(st.sel === p.id ? 8 : 6), class: ['m-spot', st.sel === p.id && 'sel'].filter(Boolean).join(' ') },
          s('title', {}, `${p.room_name || 'Trained spot'} · ${p.samples} scans · ${p.signals} signals`));
        hit(c, () => { st.sel = p.id; drawOverlay(); renderSide(); }, view);
        spotsG.append(c);
      }
    }
    if (st.mode === 'phone' && st.floor.georef?.points?.length) {
      for (const g of st.floor.georef.points) spotsG.append(s('circle', { cx: view.X(g.x), cy: view.Y(g.y), r: view.px(6), class: 'm-gps' }, s('title', {}, `GPS point ${g.note || ''}`)));
    }
    const live = st.live;
    if (live && live.floor_id === st.floorId && live.x != null && live.source !== 'map_trainer' && live.source !== 'manual_point') {
      const x = view.X(live.x), y = view.Y(live.y);
      if (st.here) est.append(s('line', { x1: view.X(st.here[0]), y1: view.Y(st.here[1]), x2: x, y2: y, class: 'm-link' }));
      est.append(s('circle', { cx: x, cy: y, r: view.px(8), class: 'm-est' }, s('title', {}, `Where SkyMesh thinks it is (${SOURCE[live.source] || live.source})`)));
    }
    if (st.here) {
      const x = view.X(st.here[0]), y = view.Y(st.here[1]), r = view.px(11);
      pin.append(s('circle', { cx: x, cy: y, r, class: ['m-here', st.session && 'busy'].filter(Boolean).join(' ') }),
        s('line', { x1: x - r * 1.6, y1: y, x2: x + r * 1.6, y2: y, class: 'm-here-x' }),
        s('line', { x1: x, y1: y - r * 1.6, x2: x, y2: y + r * 1.6, class: 'm-here-x' }));
    }
  }
  view.onZoom = () => drawOverlay();
  view.onClick = ([x, y]) => {
    if (x < 0 || x > 1 || y < 0 || y > 1 || st.session) return;
    st.here = [Math.round(x * 1e5) / 1e5, Math.round(y * 1e5) / 1e5];
    st.sel = null;
    drawOverlay(); renderBar(); renderSide();
  };

  // ------------------------------------------------------------ the "I'm here" bar over the map
  function renderBar() {
    const room = st.here ? roomAt(st.here) : null;
    goBtn.disabled = !st.here || !!st.session || (st.mode === 'badge' && !st.deviceId);
    if (st.session) {
      const s1 = st.session;
      const pct = Math.round(100 * (s1.samples || 0) / s1.target);
      mount(status, h('b', {}, s1.waitingLong ? 'Still waiting for the badge…' : 'Listening — hold still'),
        meter(Math.max(pct, 4)),
        h('span', { class: 'dim' }, `${s1.samples || 0} of ${s1.target} scans${s1.heard != null ? ` · last scan heard ${s1.heard} signals` : ''}`),
        h('button', { class: 'btn small', onclick: stopSession }, 'Cancel'));
      goBtn.textContent = '…';
      return;
    }
    goBtn.textContent = st.mode === 'phone' ? 'I’m here' : 'I’m here — record';
    mount(status, st.here
      ? h('span', {}, h('b', {}, room ? room.name.replace(/\s*\(.*\)$/, '') : 'Unmapped spot'), h('span', { class: 'dim' }, ' · marked'))
      : h('span', { class: 'dim' }, st.mode === 'badge' ? 'Tap the map where you (and the badge) are standing' : 'Tap the map where you are standing'));
  }

  // ------------------------------------------------------------ actions
  async function go() {
    if (!st.here) return;
    if (st.mode === 'phone') return phoneHere();
    const d = badgeDev();
    if (!d) return toast('Choose a badge first', 'bad');
    try {
      const r = await api.post('training/sessions', { device_id: d.id, floor_id: st.floorId, x: st.here[0], y: st.here[1], samples: SAMPLES });
      st.session = { ...r, samples: 0, startedAt: Date.now() };
      if (!r.badge_online) toast(`${d.name} looks offline: it must be on Wi-Fi to record this spot.`, 'bad', 8000);
      else if (!r.badge_scans_wifi) toast(`${d.name} has old firmware without Wi-Fi scans. Install 0.5.0 with Add device (USB).`, 'bad', 10000);
      pollSession();
    } catch (e) { toast(e.message, 'bad'); }
    drawOverlay(); renderBar();
  }

  let pollT = 0;
  function pollSession() {
    clearTimeout(pollT);
    if (!st.session) return;
    pollT = setTimeout(async () => {
      if (!st.session) return;
      try { sessionUpdate(await api.get(`training/sessions/${st.session.id}`)); } catch { /* next tick */ }
      pollSession();
    }, 3000);
  }

  function sessionUpdate(u) {
    if (!st.session || u.id !== st.session.id) return;
    Object.assign(st.session, { samples: u.samples, heard: u.heard ?? st.session.heard });
    st.session.waitingLong = !u.samples && Date.now() - st.session.startedAt > 25000;
    if (u.done || u.ended_at) {
      const ok = u.samples > 0;
      toast(ok ? `Spot saved (${u.samples} scans). Walk 3–5 m and do the next one.` : 'Nothing was recorded: the badge sent no scans.', ok ? 'ok' : 'bad', 6000);
      st.session = null; st.here = null; clearTimeout(pollT);
      loadSummary();
    } else if (u.reason === 'nothing_heard') {
      toast('The badge heard no Wi-Fi at all on that scan.', 'bad');
    }
    renderBar(); drawOverlay();
  }

  async function stopSession() {
    if (!st.session) return;
    const id = st.session.id;
    st.session = null; clearTimeout(pollT);
    await api.post(`training/sessions/${id}/stop`).catch(() => {});
    renderBar(); loadSummary();
  }

  async function phoneHere() {
    const [x, y] = st.here;
    goBtn.disabled = true;
    try {
      await locationShare.sharePoint(st.floorId, x, y);
      toast('You are now shown here on the map.', 'ok');
    } catch (e) { toast(e.message, 'bad', 8000); }
    if (can('map.edit') && st.gpsCalibrate !== false && window.isSecureContext && 'geolocation' in navigator) {
      try {
        const fix = await collectGps(12000, (best, n) => mount(status, h('b', {}, 'Reading GPS…'), h('span', { class: 'dim' }, ` ${n} fixes, best ±${Math.round(best)} m`)));
        if (fix.accuracy > 20) {
          toast(`GPS here is ±${Math.round(fix.accuracy)} m: too rough to calibrate (indoors?). Try next to a window or outside. Not saved.`, 'bad', 10000);
        } else {
          const r = await api.post(`floors/${st.floorId}/georef-points`, { lat: fix.lat, lon: fix.lon, x, y, note: `Map trainer: phone GPS ±${Math.round(fix.accuracy)} m (${fix.n} fixes)` });
          toast(r.ready ? `GPS calibration point saved. Phones now appear on this floor (rms ${r.rms_residual}).` : 'GPS point saved. You need at least 3, spread around the building.', 'ok', 8000);
          st.floor = await api.get(`floors/${st.floorId}`);
        }
      } catch (e) { toast(e.message, 'bad', 8000); }
    }
    st.here = null;
    await loadLive();
    renderBar(); drawOverlay(); renderSide();
  }

  // ------------------------------------------------------------ side panel
  function renderSide() {
    if (!st.floor) return;
    const parts = st.mode === 'badge' ? badgeCards() : phoneCards();
    mount(side, parts);
  }

  function liveCard() {
    const l = st.live;
    let body;
    if (!l) body = h('p', { class: 'dim' }, 'No position reported yet.');
    else if (l.x == null || l.floor_id !== st.floorId) body = h('p', { class: 'dim' }, l.floor_id && l.floor_id !== st.floorId ? 'On another floor.' : `No position (${(l.state || '').replace(/_/g, ' ').toLowerCase()}).`);
    else {
      const err = st.here && mpu() ? Math.hypot(l.x - st.here[0], (l.y - st.here[1]) * (st.floor.image.height / st.floor.image.width)) * mpu() : null;
      body = h('div', { class: 'stack' },
        h('div', { class: 'row' }, badge(l.state), l.room_name ? h('b', {}, l.room_name.replace(/\s*\(.*\)$/, '')) : h('span', { class: 'dim' }, 'unmapped area')),
        h('div', { class: 'dim' }, `by ${SOURCE[l.source] || l.source}${l.confidence != null ? ` · ${Math.round(l.confidence * 100)}% sure` : ''}${l.accuracy_m ? ` · ±${l.accuracy_m} m` : ''} · ${l.observed_at ? ago(l.observed_at) : ''}`),
        st.here ? h('div', {}, err != null ? h('span', {}, 'Distance to your marked spot: ', h('b', {}, `${err.toFixed(1)} m`))
          : h('span', { class: 'dim' }, 'Set the map scale (Campus Map → Edit map → Set scale) to see the error in metres.')) : null);
    }
    return h('section', { class: 'card' }, h('h2', {}, 'Live check'),
      h('p', { class: 'dim' }, 'Where SkyMesh places you right now (orange dot). Mark where you really are to compare.'), body);
  }

  function badgeCards() {
    const d = badgeDev();
    const sel = h('select', { 'aria-label': 'Badge', onchange: async () => { st.deviceId = sel.value; store('skymesh.trainer.device', sel.value); await loadLive(); renderSide(); drawOverlay(); renderBar(); } },
      st.badges.map((b) => h('option', { value: b.id, selected: b.id === st.deviceId }, `${b.name}${b.online ? '' : ' (offline)'}`)));
    const sum = st.summary;
    const spot = sum?.spots.find((p) => p.id === st.sel);
    return [
      h('section', { class: 'card' }, h('h2', {}, 'Badge you are carrying'),
        st.badges.length ? sel : h('p', { class: 'notice warn' }, 'No badges yet. Add one with Add device (USB).'),
        d ? h('div', { class: 'row' }, onlineBadge(d.online), h('span', { class: 'mono dim' }, d.firmware_version || 'no firmware reported')) : null,
        d && !d.capabilities?.wifi_scan ? h('div', { class: 'notice warn' }, 'This badge does not send Wi-Fi scans yet. Plug it in and run ',
          h('a', { href: '#/adddevice' }, 'Add device'), ' to install firmware 0.5.0.') : null),
      spot ? h('section', { class: 'card' }, h('h2', {}, spot.room_name || 'Trained spot'),
        h('div', { class: 'dim' }, `${spot.samples} scans · ${spot.signals} signals · ${ago(spot.created_at)}`),
        h('div', { class: 'row' }, h('button', { class: 'btn small danger', onclick: async () => {
          await api.del(`training/sessions/${spot.id}`).catch((e) => toast(e.message, 'bad'));
          st.sel = null; loadSummary();
        } }, 'Delete spot'), h('button', { class: 'btn small', onclick: () => { st.sel = null; drawOverlay(); renderSide(); } }, 'Close'))) : null,
      liveCard(),
      h('section', { class: 'card' }, h('h2', {}, 'Training progress'),
        sum ? h('div', { class: 'stack' },
          h('div', { class: 'train-stats' },
            stat(sum.spots.length, 'spots'), stat(`${sum.rooms_trained}/${sum.rooms_total}`, 'rooms'), stat(sum.signals, 'Wi-Fi signals')),
          meter(sum.rooms_total ? Math.round(100 * sum.rooms_trained / sum.rooms_total) : 0),
          h('p', { class: 'dim' }, sum.spots.length < 3 ? 'Train at least one spot in every room. Rooms with a dashed outline are still untrained.'
            : 'Good. Add spots in big rooms and corridors every 3–5 m. Then walk around and use Live check.'),
          sum.spots.length && can('map.edit') ? h('button', { class: 'btn small danger', onclick: async () => {
            if (await confirmDialog('Clear training', `Delete all ${sum.spots.length} trained spots on this floor?`, 'Delete all', 'danger')) {
              await api.del(`floors/${st.floorId}/training`).catch((e) => toast(e.message, 'bad'));
              loadSummary();
            }
          } }, 'Clear all training') : null)
          : h('p', { class: 'dim' }, 'Loading…')),
      h('section', { class: 'card' }, h('h2', {}, 'How to train'), h('ol', { class: 'train-steps' },
        h('li', {}, 'Carry the badge (switched on, on Wi-Fi) to a spot.'),
        h('li', {}, 'Tap that spot on the map.'),
        h('li', {}, 'Press “I’m here” and stand still about 15 seconds.'),
        h('li', {}, 'Move 3–5 m and repeat. Cover every room and corridor.')),
        h('p', { class: 'dim' }, 'Only the Wi-Fi routers’ hardware addresses and signal strengths are stored, never network names. Phone hotspots are ignored.')),
    ];
  }

  function phoneCards() {
    const secure = window.isSecureContext && 'geolocation' in navigator;
    const g = st.floor.georef;
    const gpsToggle = h('input', { type: 'checkbox', checked: st.gpsCalibrate !== false, onchange: () => { st.gpsCalibrate = gpsToggle.checked; } });
    return [
      h('section', { class: 'card' }, h('h2', {}, 'With this phone'),
        h('p', {}, '“I’m here” shows you at that spot on the map, so responders can find you.'),
        h('p', { class: 'dim' }, 'Browsers cannot scan Wi-Fi, so phones are not fingerprinted. Badges are; switch to “With a badge” to train those.'),
        can('map.edit') ? h('label', { class: 'row' }, gpsToggle, secure ? 'Also save a GPS calibration point here (best outdoors / at a window)'
          : 'GPS calibration needs the secure https:// address (scan the Phone QR code)') : null),
      liveCard(),
      h('section', { class: 'card' }, h('h2', {}, 'GPS calibration'),
        h('div', { class: 'row' }, g.ready ? badge('READY', 'b-ok') : badge(`${g.count}/3 POINTS`, 'b-warn'),
          h('span', { class: 'dim' }, g.ready ? `rms ${g.rms_residual}` : 'needs 3 points spread around the building')),
        h('p', { class: 'dim' }, 'With 3+ points, phones sharing their location appear on this floor automatically. Indoor GPS is often 10–40 m off, so these points are taken only when GPS is better than ±20 m.'),
        can('map.edit') && g.points?.length ? h('div', { class: 'stack' }, g.points.map((p) => h('div', { class: 'list-item row' },
          h('span', { class: 'mono dim' }, `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`), h('span', { class: 'spacer' }),
          h('button', { class: 'btn small danger', onclick: async () => {
            await api.del(`georef-points/${p.id}`).catch((e) => toast(e.message, 'bad'));
            st.floor = await api.get(`floors/${st.floorId}`); renderSide(); drawOverlay();
          } }, 'Delete')))) : null),
    ];
  }

  const stat = (v, label) => h('div', {}, h('b', {}, String(v)), h('span', { class: 'dim' }, label));

  // ------------------------------------------------------------ start
  renderMode();
  if (!(await loadFloors())) {
    mount(view.stage, h('div', { class: 'empty' }, 'No floor plan yet. Create one in Campus Map first.'));
    return () => view.destroy();
  }
  await loadBadges().catch(() => {});
  await loadFloor().catch((e) => toast(e.message, 'bad'));
  renderBar();

  const offs = [
    on('ws:training', (e) => sessionUpdate(e.detail)),
    on('ws:location', (e) => {
      const id = st.mode === 'badge' ? st.deviceId : locationShare.deviceId();
      if (e.detail.device_id !== id) return;
      st.live = e.detail;
      drawOverlay(); if (!st.sel) renderSide();
    }),
    on('ws:device', (e) => {
      const b = st.badges.find((x) => x.id === e.detail.id);
      if (b && b.online !== e.detail.online) { Object.assign(b, e.detail); renderSide(); }
    }),
    on('ws:map_changed', (e) => { if (e.detail.floor_id === st.floorId && e.detail.what === 'training') loadSummary(); }),
    on('reconnected', () => { loadBadges().then(() => { renderSide(); drawOverlay(); }).catch(() => {}); }),
  ];
  return () => { offs.forEach((f) => f()); clearTimeout(pollT); view.destroy(); };
}

// Width via the CSSOM: the page's CSP blocks style="" attributes.
function meter(pct) {
  const i = h('i');
  i.style.width = `${pct}%`;
  return h('div', { class: 'meter' }, i);
}

function inPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-12) + xi) inside = !inside;
  }
  return inside;
}

// Average GPS over a few seconds, keeping only the fixes close to the best accuracy seen.
function collectGps(ms, onTick) {
  return new Promise((resolve, reject) => {
    const pts = [];
    const finish = () => {
      clearTimeout(t); navigator.geolocation.clearWatch(id);
      if (!pts.length) return reject(new Error('No GPS fix. Is location allowed for this site?'));
      const best = Math.min(...pts.map((p) => p.accuracy));
      const use = pts.filter((p) => p.accuracy <= best * 1.5);
      const w = use.map((p) => 1 / (p.accuracy * p.accuracy));
      const tw = w.reduce((a, b) => a + b, 0);
      resolve({ lat: use.reduce((a, p, i) => a + p.latitude * w[i], 0) / tw, lon: use.reduce((a, p, i) => a + p.longitude * w[i], 0) / tw,
        accuracy: best, n: use.length });
    };
    const id = navigator.geolocation.watchPosition((p) => {
      pts.push(p.coords);
      onTick(Math.min(...pts.map((q) => q.accuracy)), pts.length);
    }, (e) => { if (e.code === 1) { clearTimeout(t); navigator.geolocation.clearWatch(id); reject(new Error('Location permission was denied.')); } },
    { enableHighAccuracy: true, maximumAge: 0, timeout: ms });
    const t = setTimeout(finish, ms);
  });
}
