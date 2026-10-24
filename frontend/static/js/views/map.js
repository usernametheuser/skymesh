// Campus Map: live markers with freshness/quality, incidents, camera observations and the admin editor.
import { api } from '../api.js';
import { h, s, mount, clear, badge, ago, ageSec, toast, kv, formDialog, confirmDialog, openDialog, onlineBadge } from '../ui.js';
import { state, can, on } from '../state.js';
import { MapView, polygonPoints, centroid, hit, drawPlan } from '../mapview.js';

const FEATURE_KINDS = ['room', 'corridor', 'stair', 'lift', 'exit', 'entrance', 'restricted', 'assembly_point', 'void', 'toilet', 'other'];
const ANCHOR_KINDS = ['ble_anchor', 'wifi_ap', 'camera_node', 'gateway', 'reference', 'server'];
const FILTERS = [
  ['rooms', 'Areas'], ['phones', 'Phones'], ['badges', 'Badges'], ['infra', 'Infrastructure'], ['incidents', 'Incidents'],
  ['obs', 'Camera observations'], ['historic', 'Historical positions'], ['drafts', 'Unverified drafts'],
];

export async function render(page) {
  const view = new MapView();
  const st = {
    buildings: [], floorId: null, floor: null, items: [], incidents: [], obs: [], filters: new Set(FILTERS.map((f) => f[0])),
    selected: null, search: '', edit: false, tool: 'select', draft: [], cfg: state.config || { live_s: 30, recent_s: 120, stale_s: 900 },
    look: (() => { try { return localStorage.getItem('skymesh.maplook') || 'digital'; } catch { return 'digital'; } })(),
  };
  // Digital = a clean drawn plan from the mapped rooms; Photo = the original evacuation-plan photo.
  const lookBtn = h('div', { class: 'seg', role: 'group', 'aria-label': 'Map style' },
    ...['digital', 'blend', 'photo'].map((k) => h('button', { class: ['seg-b', st.look === k && 'on'], 'data-look': k, onclick: () => setLook(k) },
      { digital: 'Digital', blend: 'Digital + photo', photo: 'Photo' }[k])));
  function setLook(k) {
    st.look = k;
    try { localStorage.setItem('skymesh.maplook', k); } catch { /* ignore */ }
    lookBtn.querySelectorAll('.seg-b').forEach((b) => b.classList.toggle('on', b.dataset.look === k));
    drawAll();
  }
  const floorSel = h('select', { 'aria-label': 'Floor' });
  const search = h('input', { type: 'search', placeholder: 'Search room, person, device…', 'aria-label': 'Search map' });
  const chips = h('div', { class: 'chips' });
  const editBtn = can('map.edit') ? h('button', { class: 'btn small', onclick: () => { st.edit = !st.edit; st.tool = 'select'; st.draft = []; refreshAll(); } }, 'Edit map') : null;
  const side = h('aside', { class: 'map-side' });
  const legend = h('div', { class: 'map-legend' },
    h('span', {}, h('i', { class: 'lg-phone' }), 'phone'), h('span', {}, h('i', { class: 'lg-badge' }), 'badge'),
    h('span', {}, h('i', { class: 'lg-hist' }), 'not live'), h('span', {}, h('i', { class: 'lg-sos' }), 'SOS'),
    h('span', {}, h('i', { class: 'lg-exit' }), 'exit'), h('span', {}, h('i', { class: 'lg-obs' }), 'camera obs.'));
  for (const [cls, color] of [['lg-phone', 'var(--info)'], ['lg-badge', 'var(--accent-2)'], ['lg-hist', 'var(--muted)'],
    ['lg-sos', 'var(--sos)'], ['lg-exit', 'var(--ok)'], ['lg-obs', 'var(--warn)']]) legend.querySelector(`.${cls}`).style.background = color;
  view.stage.append(legend);

  mount(page, h('div', { class: 'map-layout' },
    h('div', { class: 'map-main' }, h('div', { class: 'map-toolbar' }, floorSel, lookBtn, search, chips, h('span', { class: 'spacer' }), editBtn), view.stage),
    side));

  function renderChips() {
    mount(chips, FILTERS.filter(([k]) => (k !== 'obs' || can('camera.view')) && (k !== 'drafts' || can('map.edit'))).map(([k, label]) =>
      h('button', { class: ['chip', st.filters.has(k) && 'on'], 'aria-pressed': String(st.filters.has(k)), onclick: () => {
        st.filters.has(k) ? st.filters.delete(k) : st.filters.add(k); renderChips(); drawAll();
      } }, label)));
  }

  // ------------------------------------------------------------ data

  async function loadBuildings() {
    st.buildings = await api.get('buildings');
    const floors = st.buildings.flatMap((b) => b.floors.map((f) => ({ ...f, building: b.name })));
    mount(floorSel, floors.map((f) => h('option', { value: f.id }, `${f.building} — ${f.name}`)));
    if (!floors.length) return false;
    const saved = localStorage.getItem('skymesh.floor');
    st.floorId = floors.some((f) => f.id === saved) ? saved : floors[0].id;
    floorSel.value = st.floorId;
    return true;
  }

  async function loadFloor() {
    st.floor = await api.get(`floors/${st.floorId}`);
    if (st.floor.image) await view.setImage(st.floor.image.url, st.floor.image.width, st.floor.image.height);
  }

  async function loadLive() {
    const [items, incidents, obs] = await Promise.all([
      api.get(`locations/current?floor_id=${encodeURIComponent(st.floorId)}`),
      api.get('incidents?active=true'),
      can('camera.view') ? api.get(`observations/current?floor_id=${encodeURIComponent(st.floorId)}`) : [],
    ]);
    items.forEach((i) => { i._fetched = Date.now(); });
    st.items = items; st.incidents = incidents; st.obs = obs;
  }

  async function refreshAll() {
    try {
      await loadFloor();
      await loadLive();
    } catch (e) { toast(e.message, 'bad'); }
    drawAll(); renderSide();
  }

  // Server state is computed at fetch time; age it locally so nothing stays LIVE after its window.
  function liveState(i) {
    if (!['LIVE', 'LOW_CONFIDENCE', 'RECENT', 'STALE'].includes(i.state) || !i.observed_at) return { state: i.state, age: i.age_s };
    const age = (Date.now() - Date.parse(i.observed_at)) / 1000;
    let stt = i.state;
    if (age > st.cfg.stale_s) stt = 'LAST_KNOWN';
    else if (age > st.cfg.recent_s) stt = 'STALE';
    else if (age > st.cfg.live_s) stt = 'RECENT';
    return { state: stt, age };
  }
  const isLive = (i) => ['LIVE', 'LOW_CONFIDENCE'].includes(liveState(i).state);

  // ------------------------------------------------------------ drawing

  const mpu = () => st.floor?.image?.transform?.metres_per_unit || null;
  const editing = () => st.edit && st.tool !== 'select';
  const matches = (text) => !st.search || String(text || '').toLowerCase().includes(st.search);

  function drawAll() {
    if (!st.floor?.image) return;
    drawPlan(view, st.floor.features, st.look);
    drawMarkers();
  }
  // Everything whose size depends on the zoom (the plan itself scales with the world).
  function drawMarkers() {
    if (!st.floor?.image) return;
    drawFeatures(); drawAnchors(); drawDevices(); drawIncidents(); drawObs(); drawDraft();
  }

  function drawFeatures() {
    const g = view.clearLayer('features');
    const lbl = view.clearLayer('rooms');
    for (const f of st.floor.features) {
      const draft = !f.verified;
      // In the digital look every mapped area is part of the drawing, so it stays clickable even as a draft.
      if (draft && !(st.edit || st.filters.has('drafts') || (st.look !== 'photo' && f.geometry_type === 'polygon'))) continue;
      const sel = st.selected?.type === 'feature' && st.selected.id === f.id;
      if (f.geometry_type === 'polygon') {
        if (!st.filters.has('rooms') && !sel) continue;
        const found = st.search && matches(f.name);
        const poly = s('polygon', { points: polygonPoints(view, f.geometry), class: ['f-poly', `k-${f.kind}`, draft && 'draft', sel && 'sel', found && 'room-hit'].filter(Boolean).join(' ') });
        hit(poly, () => (st.tool === 'select' ? select('feature', f.id) : false), view);
        g.append(poly);
        if ((f.kind === 'room' && st.look === 'photo') || sel || found) {  // digital look draws its own labels
          const [cx, cy] = centroid(f.geometry);
          lbl.append(s('text', { x: view.X(cx), y: view.Y(cy), 'text-anchor': 'middle', class: 'f-label', 'font-size': view.px(12) }, f.name.replace(/\s*\(.*\)$/, '')));
        }
      } else {
        if (!st.filters.has('infra') && !st.filters.has('rooms') && !sel) continue;
        if (st.look !== 'photo' && !st.edit && !sel && f.kind === 'exit') continue;  // drawn as EXIT tags by the digital plan
        const [[x, y]] = f.geometry;
        const c = s('circle', { cx: view.X(x), cy: view.Y(y), r: view.px(7), class: ['m-exit', draft && 'draft'].filter(Boolean).join(' ') },
          s('title', {}, `${f.name}${draft ? ' (unverified draft)' : ''}`));
        hit(c, () => (st.tool === 'select' ? select('feature', f.id) : false), view);
        g.append(c);
      }
    }
  }

  function drawAnchors() {
    const g = view.clearLayer('anchors');
    if (!st.filters.has('infra')) return;
    for (const a of st.floor.anchors) {
      const x = view.X(a.x), y = view.Y(a.y), r = view.px(7);
      let el;
      if (a.kind === 'reference') el = s('circle', { cx: x, cy: y, r: view.px(9), class: 'm-ref' });
      else if (a.kind === 'camera_node') el = s('rect', { x: x - r, y: y - r, width: 2 * r, height: 2 * r, rx: view.px(2), class: 'm-cam' });
      else el = s('polygon', { points: `${x},${y - r * 1.2} ${x + r},${y} ${x},${y + r * 1.2} ${x - r},${y}`, class: 'm-anchor' });
      el.append(s('title', {}, `${a.name} (${a.kind.replace('_', ' ')})`));
      hit(el, () => (st.tool === 'select' ? select('anchor', a.id) : false), view);
      g.append(el);
    }
  }

  function drawDevices() {
    const g = view.clearLayer('devices');
    const acc = view.clearLayer('accuracy');
    for (const i of st.items) {
      if (i.floor_id !== st.floorId || i.x === null || i.x === undefined) continue;
      if (i.device_type === 'phone' && !st.filters.has('phones')) continue;
      if (i.device_type === 'badge' && !st.filters.has('badges')) continue;
      const live = isLive(i);
      if (!live && !st.filters.has('historic')) continue;
      if (!matches(`${i.device_name} ${i.owner_name} ${i.device_id} ${i.room_name}`)) continue;
      const sel = st.selected?.type === 'device' && st.selected.id === i.device_id;
      if (i.kind === 'room') {
        // Room-level only: highlight the room instead of drawing a falsely precise dot.
        const room = st.floor.features.find((f) => f.id === i.room_id);
        if (room) g.append(s('polygon', { points: polygonPoints(view, room.geometry), class: 'f-poly room-hit' }));
      } else if (i.accuracy_m && mpu() && live) {
        acc.append(s('circle', { cx: view.X(i.x), cy: view.Y(i.y), r: (i.accuracy_m / mpu()) * view.w, class: 'm-acc' }));
      }
      const c = s('circle', { cx: view.X(i.x), cy: view.Y(i.y), r: view.px(sel ? 10 : 7),
        class: ['m-dev', i.device_type, !live && 'historic'].filter(Boolean).join(' ') },
        s('title', {}, `${i.owner_name || i.device_name} — ${liveState(i).state}${i.kind === 'room' ? ' (room-level)' : ''}`));
      hit(c, () => (editing() ? false : select('device', i.device_id)), view);
      g.append(c);
    }
  }

  function drawIncidents() {
    const g = view.clearLayer('incidents');
    if (!st.filters.has('incidents')) return;
    for (const inc of st.incidents) {
      if (inc.floor_id !== st.floorId || inc.x === null) continue;
      const x = view.X(inc.x), y = view.Y(inc.y);
      if (!inc.is_test) g.append(s('circle', { cx: x, cy: y, r: view.px(16), class: 'm-sos-ring' }));
      const c = s('circle', { cx: x, cy: y, r: view.px(9), class: 'm-sos' }, s('title', {}, `${inc.is_test ? 'TEST ' : ''}SOS ${inc.state} — ${inc.location_note}`));
      hit(c, () => (editing() ? false : select('incident', inc.id)), view);
      g.append(c);
    }
  }

  function drawObs() {
    const g = view.clearLayer('obs');
    if (!st.filters.has('obs')) return;
    for (const o of st.obs) {
      const x = view.X(o.x), y = view.Y(o.y), r = view.px(6);
      const el = s('rect', { x: x - r, y: y - r, width: 2 * r, height: 2 * r, transform: `rotate(45 ${x} ${y})`, class: 'm-obs' },
        s('title', {}, `${o.track_label} — camera observation, ${Math.round(o.confidence * 100)}%${o.association ? ` · confirmed: ${o.association.device_name}` : ''}`));
      hit(el, () => (editing() ? false : select('obs', o.id)), view);
      g.append(el);
    }
  }

  function drawDraft() {
    const g = view.clearLayer('draw');
    if (!st.draft.length) return;
    g.append(s('polyline', { points: polygonPoints(view, st.draft), class: 'm-draw' }));
    st.draft.forEach(([x, y]) => g.append(s('circle', { cx: view.X(x), cy: view.Y(y), r: view.px(4), class: 'm-draw-pt' })));
  }

  view.onZoom = () => drawMarkers();

  // ------------------------------------------------------------ selection & side panel

  function select(type, id) { st.selected = { type, id }; drawMarkers(); renderSide(); }

  function itemStateBadges(i) {
    const ls = liveState(i);
    return h('span', { class: 'row' }, badge(ls.state), i.sos_incident_id ? badge('SOS', 'b-sos') : null,
      i.kind === 'room' ? badge('ROOM-LEVEL', 'b-violet') : null, (i.flags || []).includes('estimate') ? badge('ESTIMATE', 'b-warn') : null);
  }

  function renderSide() {
    const parts = [];
    if (!st.floor) { mount(side, h('div', { class: 'empty' }, 'No floor selected.')); return; }
    if (st.edit) parts.push(editorPanel());
    const sel = st.selected;
    if (sel) parts.push(selectionPanel(sel));
    // Tracked items
    const tracked = st.items.filter((i) => matches(`${i.device_name} ${i.owner_name} ${i.device_id} ${i.room_name}`));
    parts.push(h('section', { class: 'card' }, h('h2', {}, `Tracked devices (${tracked.length})`),
      tracked.length ? h('div', { class: 'stack' }, tracked.map((i) => h('div', {
        class: ['list-item', sel?.type === 'device' && sel.id === i.device_id && 'sel'], onclick: () => {
          select('device', i.device_id);
          if (i.x !== null && i.x !== undefined && i.floor_id === st.floorId) view.centerOn(i.x, i.y);
        } },
      h('div', { class: 'row' }, h('b', {}, i.owner_name || i.device_name), h('span', { class: 'dim mono' }, i.device_type)),
      itemStateBadges(i),
      h('div', { class: 'dim' }, i.floor_id && i.floor_id !== st.floorId ? 'On another floor'
        : i.x === null || i.x === undefined ? (i.lat ? 'GPS only — not on floor plan' : 'No position on this map')
          : `${i.room_name || 'Unmapped area'} · ${i.observed_at ? ago(i.observed_at) : ''}`))))
        : h('div', { class: 'dim' }, can('location.view_all') ? 'No phones or badges are reporting.'
          : 'Only your own devices are shown. Turn on sharing in Settings.')));
    const incHere = st.incidents.filter((i) => i.floor_id === st.floorId || !i.floor_id);
    if (incHere.length) parts.push(h('section', { class: 'card' }, h('h2', {}, `Active incidents (${incHere.length})`),
      h('div', { class: 'stack' }, incHere.map((i) => h('div', { class: 'list-item', onclick: () => { select('incident', i.id); if (i.x !== null) view.centerOn(i.x, i.y); } },
        h('div', { class: 'row' }, badge(i.state), i.is_test ? badge('TEST', 'b-muted') : null, h('b', {}, i.reporter_name || i.device_name || 'Unknown')),
        h('div', { class: 'dim' }, i.location_note))))));
    if (st.search) {
      const rooms = st.floor.features.filter((f) => f.geometry_type === 'polygon' && matches(f.name));
      if (rooms.length) parts.push(h('section', { class: 'card' }, h('h2', {}, 'Matching areas'), h('div', { class: 'stack' },
        rooms.map((f) => h('div', { class: 'list-item', onclick: () => { select('feature', f.id); view.centerOn(...centroid(f.geometry)); } },
          f.name, f.verified ? null : badge('DRAFT', 'b-warn'))))));
    }
    const georef = st.floor.georef;
    parts.push(h('section', { class: 'card' }, h('h2', {}, 'This floor'), kv([
      ['Image', st.floor.image ? `v${st.floor.image.version} · ${st.floor.image.width}×${st.floor.image.height}` : 'none'],
      ['Scale', mpu() ? `${mpu()} m across the plan width` : 'not calibrated (accuracy circles hidden)'],
      ['GPS georeference', georef.ready ? `ready (${georef.count} pts, rms ${georef.rms_residual})` : `not ready (${georef.count}/3 points)`],
      ['Areas', `${st.floor.features.filter((f) => f.verified).length} verified · ${st.floor.features.filter((f) => !f.verified).length} unverified drafts`],
    ])));
    mount(side, parts);
  }

  function selectionPanel(sel) {
    const close = h('button', { class: 'btn small', onclick: () => { st.selected = null; drawMarkers(); renderSide(); } }, 'Close');
    if (sel.type === 'device') {
      const i = st.items.find((x) => x.device_id === sel.id);
      if (!i) return h('div');
      const ls = liveState(i);
      return h('section', { class: 'card' }, h('div', { class: 'row' }, h('h2', {}, i.owner_name || i.device_name), h('span', { class: 'spacer' }), close),
        itemStateBadges(i), kv([
          ['Device', `${i.device_name} (${i.device_type})`], ['Device ID', h('span', { class: 'mono' }, i.device_id)],
          ['Status', onlineBadge(i.online)], ['Area', i.room_name || (i.x !== null && i.x !== undefined ? 'Unmapped area' : '—')],
          ['Method', i.source || '—'], ['Accuracy', i.accuracy_m ? `±${i.accuracy_m} m` : (i.kind === 'room' ? 'room-level' : 'unknown')],
          ['Confidence', i.confidence !== null && i.confidence !== undefined ? `${Math.round(i.confidence * 100)}%` : '—'],
          ['Observed', i.observed_at ? `${ago(i.observed_at)} (${ageSec(ls.age)})` : '—'],
          ['Battery', i.battery_pct !== null ? `${i.battery_pct}%` : (i.battery_mv ? `${i.battery_mv} mV` : 'not reported')],
          i.lat ? ['GPS', `${i.lat.toFixed(6)}, ${i.lon.toFixed(6)}`] : null,
          (i.flags || []).length ? ['Flags', h('span', { class: 'mono dim' }, i.flags.join(', '))] : null,
        ]), i.sos_incident_id ? h('a', { href: '#/emergency', class: 'btn danger' }, 'Open SOS incident') : null);
    }
    if (sel.type === 'incident') {
      const i = st.incidents.find((x) => x.id === sel.id);
      if (!i) return h('div');
      return h('section', { class: 'card' }, h('div', { class: 'row' }, h('h2', {}, `${i.is_test ? 'Test ' : ''}Incident`), h('span', { class: 'spacer' }), close),
        h('div', { class: 'row' }, badge(i.state), badge(i.severity.toUpperCase(), 'b-bad')), kv([
          ['From', i.reporter_name || i.device_name || '—'], ['Source', i.source], ['Raised', ago(i.created_at)],
          ['Location', i.location_note], ['Area', i.room_name || '—'], ['Method', i.location_source || '—'],
          ['Assigned', i.assigned_name || 'nobody'],
        ]), h('a', { href: '#/emergency', class: 'btn' }, 'Manage in Emergency Center'));
    }
    if (sel.type === 'obs') {
      const o = st.obs.find((x) => x.id === sel.id);
      if (!o) return h('div');
      return h('section', { class: 'card' }, h('div', { class: 'row' }, h('h2', {}, o.track_label), h('span', { class: 'spacer' }), close),
        badge('CAMERA OBSERVATION', 'b-warn'), kv([
          ['Camera', o.camera_device_id], ['Confidence', `${Math.round(o.confidence * 100)}%`], ['Observed', ago(o.observed_at)],
          ['Identity', o.association ? `Operator-confirmed: ${o.association.device_name}` : 'Unknown — not linked to any person or device'],
        ]));
    }
    if (sel.type === 'feature') {
      const f = st.floor.features.find((x) => x.id === sel.id);
      if (!f) return h('div');
      return h('section', { class: 'card' }, h('div', { class: 'row' }, h('h2', {}, f.name), h('span', { class: 'spacer' }), close),
        h('div', { class: 'row' }, badge(f.kind.toUpperCase(), 'b-accent'), f.verified ? badge('VERIFIED', 'b-ok') : badge('UNVERIFIED DRAFT', 'b-warn'),
          f.needs_recheck ? badge('RE-CHECK: NEW IMAGE', 'b-warn') : null),
        f.properties?.origin ? h('p', { class: 'dim' }, f.properties.origin) : null,
        can('map.edit') ? h('div', { class: 'row' },
          !f.verified || f.needs_recheck ? h('button', { class: 'btn small primary', onclick: () => act(api.post(`features/${f.id}/verify`), 'Verified') }, 'Confirm as correct') : null,
          h('button', { class: 'btn small', onclick: () => editFeature(f) }, 'Rename / type'),
          f.geometry_type === 'polygon' ? h('button', { class: 'btn small', onclick: () => { st.edit = true; st.tool = 'redraw'; st.redrawId = f.id; st.draft = []; renderSide(); toast('Click the corners of the area, then press Finish.'); } }, 'Redraw') : null,
          h('button', { class: 'btn small danger', onclick: async () => { if (await confirmDialog('Delete area', `Delete “${f.name}”?`, 'Delete', 'danger')) { st.selected = null; act(api.del(`features/${f.id}`), 'Deleted'); } } }, 'Delete'))
          : null);
    }
    if (sel.type === 'anchor') {
      const a = st.floor.anchors.find((x) => x.id === sel.id);
      if (!a) return h('div');
      return h('section', { class: 'card' }, h('div', { class: 'row' }, h('h2', {}, a.name), h('span', { class: 'spacer' }), close),
        kv([['Kind', a.kind], ['Identifier', a.identifier || (can('map.edit') ? '—' : 'hidden')], ['RSSI @1 m', a.rssi_at_1m ?? 'default (-59)'],
          ['Device', a.device_id || '—'], ['Position', `${a.x.toFixed(4)}, ${a.y.toFixed(4)}`]]),
        can('map.edit') ? h('div', { class: 'row' },
          h('button', { class: 'btn small', onclick: () => { st.tool = 'move-anchor'; st.moveId = a.id; st.edit = true; renderSide(); toast('Click the new position on the map.'); } }, 'Move'),
          h('button', { class: 'btn small danger', onclick: async () => { if (await confirmDialog('Remove', `Remove ${a.name}?`, 'Remove', 'danger')) { st.selected = null; act(api.del(`anchors/${a.id}`), 'Removed'); } } }, 'Remove'))
          : null);
    }
    return h('div');
  }

  async function act(promise, msg) {
    try { await promise; if (msg) toast(msg, 'ok'); } catch (e) { toast(e.message, 'bad'); }
    await refreshAll();
  }

  async function editFeature(f) {
    const v = await formDialog('Edit area', [
      { name: 'name', label: 'Name', value: f.name, required: true },
      { name: 'kind', label: 'Type', type: 'select', value: f.kind, options: FEATURE_KINDS.map((k) => ({ value: k, label: k })) }]);
    if (v) act(api.patch(`features/${f.id}`, v), 'Saved');
  }

  // ------------------------------------------------------------ editor

  const TOOLS = [
    ['select', 'Select'], ['area', 'Draw area'], ['point', 'Add exit / entrance'], ['anchor', 'Place infrastructure'],
    ['georef', 'GPS control point'], ['scale', 'Set scale'],
  ];

  function editorPanel() {
    const drafts = st.floor.features.filter((f) => !f.verified || f.needs_recheck);
    return h('section', { class: 'card' }, h('h2', {}, 'Map editor'),
      h('div', { class: 'chips' }, TOOLS.map(([k, label]) => h('button', { class: ['chip', st.tool === k && 'on'], onclick: () => { st.tool = k; st.draft = []; drawMarkers(); renderSide(); } }, label))),
      h('p', { class: 'dim' }, {
        select: 'Click an area, marker or device to inspect it.',
        area: 'Click each corner of the room/corridor, then Finish. Coordinates are saved normalized to the image.',
        redraw: 'Click each corner of the new outline, then Finish.',
        point: 'Click where the exit/entrance/assembly point is.',
        anchor: 'Click where the BLE anchor, access point, camera node or reference point is installed.',
        georef: 'Click a point whose GPS coordinates you measured on site (outdoor corners work best). 3+ points needed.',
        scale: 'Click two points whose real distance you know (e.g. a classroom labelled 16.5 m wide).',
        'move-anchor': 'Click the new position.',
      }[st.tool] || ''),
      ['area', 'redraw', 'scale'].includes(st.tool) && st.draft.length ? h('div', { class: 'row' },
        h('span', { class: 'mono dim' }, `${st.draft.length} point(s)`),
        st.tool !== 'scale' ? h('button', { class: 'btn small primary', onclick: finishArea }, 'Finish') : null,
        h('button', { class: 'btn small', onclick: () => { st.draft.pop(); drawMarkers(); renderSide(); } }, 'Undo'),
        h('button', { class: 'btn small', onclick: () => { st.draft = []; drawMarkers(); renderSide(); } }, 'Cancel')) : null,
      h('div', { class: 'row' },
        h('button', { class: 'btn small', onclick: rectifyDialog }, 'New image / rectify…'),
        h('button', { class: 'btn small', onclick: () => act(api.post(`floors/${st.floorId}/assist`).then((r) => toast(`${r.proposed} new exit-sign drafts proposed`)), null) }, 'Detect exit signs'),
        h('button', { class: 'btn small', onclick: newFloorDialog }, 'Add floor…')),
      drafts.length ? h('details', {}, h('summary', {}, `${drafts.length} area(s) need verification`),
        h('div', { class: 'stack' }, drafts.map((f) => h('div', { class: 'list-item', onclick: () => { select('feature', f.id); view.centerOn(...centroid(f.geometry)); } },
          f.name, ' ', f.needs_recheck ? badge('RE-CHECK', 'b-warn') : badge('DRAFT', 'b-warn'))))) : h('p', { class: 'dim' }, 'All areas verified.'));
  }

  view.onClick = async ([x, y]) => {
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    if (!st.edit || st.tool === 'select') { st.selected = null; drawMarkers(); renderSide(); return; }
    const pt = [Math.round(x * 1e5) / 1e5, Math.round(y * 1e5) / 1e5];
    if (st.tool === 'area' || st.tool === 'redraw') { st.draft.push(pt); drawMarkers(); renderSide(); return; }
    if (st.tool === 'scale') {
      st.draft.push(pt); drawAll();
      if (st.draft.length === 2) {
        const v = await formDialog('Set scale', [{ name: 'd', label: 'Real distance between the two points (metres)', type: 'number', step: '0.1', required: true }]);
        if (v?.d > 0) await act(api.post(`floors/${st.floorId}/scale`, { a: st.draft[0], b: st.draft[1], distance_m: v.d }), 'Scale saved');
        st.draft = []; drawAll();
      }
      renderSide();
      return;
    }
    if (st.tool === 'point') {
      const v = await formDialog('Add marker', [
        { name: 'kind', label: 'Type', type: 'select', value: 'exit', options: ['exit', 'entrance', 'assembly_point', 'stair', 'lift', 'other'].map((k) => ({ value: k, label: k })) },
        { name: 'name', label: 'Name', required: true, placeholder: 'e.g. North-west stair exit' }]);
      if (v) act(api.post(`floors/${st.floorId}/features`, { ...v, geometry: [pt] }), 'Marker added');
      return;
    }
    if (st.tool === 'anchor') {
      const cams = can('device.view_all') ? (await api.get('devices')).filter((d) => ['camera_node', 'anchor', 'gateway'].includes(d.type)) : [];
      const v = await formDialog('Place infrastructure', [
        { name: 'kind', label: 'Type', type: 'select', value: 'ble_anchor', options: ANCHOR_KINDS.map((k) => ({ value: k, label: k })) },
        { name: 'name', label: 'Name', required: true },
        { name: 'identifier', label: 'Identifier (BLE MAC or AP BSSID)', placeholder: 'aa:bb:cc:dd:ee:ff', help: 'Badges match BLE anchors by this value; topology matches APs by BSSID.' },
        { name: 'rssi_at_1m', label: 'Measured RSSI at 1 m (BLE, optional)', type: 'number' },
        { name: 'device_id', label: 'Linked SkyMesh device', type: 'select', value: '', options: [{ value: '', label: '— none —' }, ...cams.map((d) => ({ value: d.id, label: `${d.name} (${d.type})` }))] }]);
      if (v) act(api.post(`floors/${st.floorId}/anchors`, { kind: v.kind, name: v.name, x: pt[0], y: pt[1], identifier: v.identifier || null,
        rssi_at_1m: v.rssi_at_1m, device_id: v.device_id || null }), 'Placed');
      return;
    }
    if (st.tool === 'move-anchor') {
      st.tool = 'select';
      act(api.patch(`anchors/${st.moveId}`, { x: pt[0], y: pt[1] }), 'Moved');
      return;
    }
    if (st.tool === 'georef') {
      const v = await formDialog('GPS control point', [
        { name: 'lat', label: 'Latitude (WGS84)', type: 'number', step: 'any', required: true },
        { name: 'lon', label: 'Longitude (WGS84)', type: 'number', step: 'any', required: true },
        { name: 'note', label: 'Note', placeholder: 'e.g. NE outer corner, measured with phone GPS ±4 m' }],
      'Add', 'Use positions measured on site. Indoor GPS is often off by tens of metres — prefer outdoor corners.');
      if (v) act(api.post(`floors/${st.floorId}/georef-points`, { lat: v.lat, lon: v.lon, x: pt[0], y: pt[1], note: v.note || null })
        .then((r) => toast(r.ready ? `Georeference ready (rms ${r.rms_residual})` : 'Point saved — need at least 3 non-collinear points')), null);
    }
  };

  async function finishArea() {
    if (st.draft.length < 3) return toast('An area needs at least 3 corners', 'bad');
    if (st.tool === 'redraw') {
      await act(api.patch(`features/${st.redrawId}`, { geometry: st.draft }), 'Outline updated');
      st.draft = []; st.tool = 'select';
      return;
    }
    const v = await formDialog('New area', [
      { name: 'name', label: 'Name', required: true, placeholder: 'e.g. Classroom 05' },
      { name: 'kind', label: 'Type', type: 'select', value: 'room', options: FEATURE_KINDS.map((k) => ({ value: k, label: k })) }]);
    if (!v) return;
    await act(api.post(`floors/${st.floorId}/features`, { ...v, geometry: st.draft }), 'Area saved');
    st.draft = [];
  }

  async function newFloorDialog() {
    const v = await formDialog('Add floor', [
      { name: 'building_id', label: 'Building', type: 'select', value: st.buildings[0]?.id, options: [...st.buildings.map((b) => ({ value: b.id, label: b.name })), { value: '__new', label: '+ New building' }] },
      { name: 'building_name', label: 'New building name (if adding one)' },
      { name: 'name', label: 'Floor name', required: true }, { name: 'level', label: 'Level number', type: 'number', required: true }]);
    if (!v) return;
    try {
      let bid = v.building_id;
      if (bid === '__new') bid = (await api.post('buildings', { name: v.building_name || 'New building' })).id;
      const f = await api.post('floors', { building_id: bid, name: v.name, level: v.level });
      toast('Floor created — now add its floor-plan image with “New image / rectify”.', 'ok');
      await loadBuildings(); st.floorId = f.id; floorSel.value = f.id; localStorage.setItem('skymesh.floor', f.id);
      await refreshAll();
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function rectifyDialog() {
    let sources = await api.get('map-sources');
    const upload = h('input', { type: 'file', accept: 'image/jpeg,image/png' });
    const srcSel = h('select', {}, sources.map((n) => h('option', { value: n }, n)));
    const rot = h('select', {}, [0, 90, 180, 270].map((r) => h('option', { value: r }, `rotate ${r}°`)));
    const imgEl = h('img', { alt: 'Source photo' });
    const overlay = s('svg', { class: 'rectify-svg' });
    const box = h('div', { class: 'rectify-box' }, imgEl, overlay);
    box.style.position = 'relative'; box.style.maxHeight = '50vh'; box.style.overflow = 'auto';
    imgEl.style.maxWidth = '100%'; imgEl.style.display = 'block';
    overlay.style.position = 'absolute'; overlay.style.inset = '0'; overlay.style.width = '100%'; overlay.style.height = '100%';
    let corners = [];
    const hint = h('p', { class: 'dim' });
    const redraw = () => {
      clear(overlay);
      const sx = imgEl.clientWidth / imgEl.naturalWidth || 1;
      overlay.setAttribute('viewBox', `0 0 ${imgEl.clientWidth} ${imgEl.clientHeight}`);
      if (corners.length) overlay.append(s('polygon', { points: corners.map(([x, y]) => `${x * sx},${y * sx}`).join(' '), class: 'm-draw' }));
      corners.forEach(([x, y], i) => overlay.append(s('circle', { cx: x * sx, cy: y * sx, r: 5, class: 'm-draw-pt' }), s('text', { x: x * sx + 7, y: y * sx - 7, class: 'f-label' }, ['TL', 'TR', 'BR', 'BL'][i])));
      hint.textContent = corners.length < 4 ? `Click corner ${corners.length + 1} of 4: ${['top-left', 'top-right', 'bottom-right', 'bottom-left'][corners.length]} of the plan. (Skip to use the whole photo.)` : 'Four corners set. The area will be warped to an upright rectangle.';
    };
    const load = () => { corners = []; imgEl.onload = redraw; imgEl.src = `/api/v1/map-sources/${encodeURIComponent(srcSel.value)}`; };
    overlay.addEventListener('click', (e) => {
      if (corners.length >= 4) return;
      const r = imgEl.getBoundingClientRect();
      const k = imgEl.naturalWidth / imgEl.clientWidth;
      corners.push([Math.round((e.clientX - r.left) * k), Math.round((e.clientY - r.top) * k)]);
      redraw();
    });
    srcSel.addEventListener('change', load);
    upload.addEventListener('change', async () => {
      const f = upload.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      try {
        const r = await api.post('map-sources', fd);
        sources = await api.get('map-sources');
        mount(srcSel, sources.map((n) => h('option', { value: n }, n)));
        srcSel.value = r.name; load();
      } catch (e) { toast(e.message, 'bad'); }
    });
    if (sources.length) load();
    const body = h('div', { class: 'stack' }, h('div', { class: 'row' }, srcSel, rot, h('button', { class: 'btn small', onclick: () => { corners = []; redraw(); } }, 'Reset corners')),
      h('label', { class: 'field' }, 'Or upload a new photo of the plan', upload), hint, box,
      h('p', { class: 'dim' }, 'Creates a new image version. Existing areas keep their normalized positions and are flagged for re-checking.'));
    const ok = await openDialog('Floor-plan image', body, [{ label: 'Cancel', value: false }, { label: 'Create version', value: true, kind: 'primary' }]);
    if (!ok) return;
    if (corners.length && corners.length !== 4) return toast('Set all four corners or none', 'bad');
    act(api.post(`floors/${st.floorId}/images`, { source_file: srcSel.value, corners: corners.length ? corners : null, rotate: Number(rot.value) }), 'New image version active');
  }

  // ------------------------------------------------------------ wiring

  floorSel.addEventListener('change', () => { st.floorId = floorSel.value; localStorage.setItem('skymesh.floor', st.floorId); st.selected = null; refreshAll(); });
  let searchT;
  search.addEventListener('input', () => { clearTimeout(searchT); searchT = setTimeout(() => { st.search = search.value.trim().toLowerCase(); drawMarkers(); renderSide(); }, 150); });

  renderChips();
  if (!(await loadBuildings())) {
    mount(view.stage, h('div', { class: 'empty' }, can('map.edit') ? 'No floors yet. Run `python -m app.cli init` or add a floor.' : 'No campus map has been configured yet.'));
    if (can('map.edit')) side.append(h('button', { class: 'btn primary', onclick: newFloorDialog }, 'Add floor'));
    return () => view.destroy();
  }
  await refreshAll();

  // Many badges reporting at once: coalesce their updates into one redraw per frame, and rebuild the
  // side panel at most twice a second (it is the expensive part, and not while the editor is open).
  let rafId = 0, sideT = 0;
  function liveRedraw() {
    if (!rafId) rafId = requestAnimationFrame(() => { rafId = 0; drawDevices(); });
    if (!sideT && !st.edit) sideT = setTimeout(() => { sideT = 0; renderSide(); }, 500);
  }
  const offs = [
    on('ws:location', (e) => {
      const i = { ...e.detail, _fetched: Date.now() };
      const idx = st.items.findIndex((x) => x.device_id === i.device_id);
      if (idx >= 0) st.items[idx] = i; else st.items.push(i);
      liveRedraw();
    }),
    on('ws:incident', async () => { st.incidents = await api.get('incidents?active=true').catch(() => st.incidents); drawIncidents(); renderSide(); }),
    on('ws:map_changed', (e) => { if (e.detail.floor_id === st.floorId && e.detail.what !== 'training') refreshAll(); }),
    on('ws:camera_observations', async () => { if (can('camera.view')) { st.obs = await api.get(`observations/current?floor_id=${st.floorId}`).catch(() => st.obs); drawObs(); } }),
    on('reconnected', () => loadLive().then(() => { drawMarkers(); renderSide(); })),
  ];
  // Re-evaluate freshness locally every 5 s and resync every 30 s.
  const tick = setInterval(() => { drawDevices(); if (!st.edit) renderSide(); }, 5000);
  const resync = setInterval(() => loadLive().then(() => { drawMarkers(); }).catch(() => {}), 30000);
  return () => { offs.forEach((f) => f()); clearInterval(tick); clearInterval(resync); cancelAnimationFrame(rafId); clearTimeout(sideT); view.destroy(); };
}
