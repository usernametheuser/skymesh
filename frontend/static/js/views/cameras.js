import { api } from '../api.js';
import { h, mount, badge, ago, onlineBadge, toast, formDialog, openDialog, table, emptyState, kv } from '../ui.js';
import { can, on } from '../state.js';

export async function render(page) {
  const camsEl = h('div', { class: 'stack' });
  const sessEl = h('div', {});
  const fallEl = h('div', {});
  const liveStops = [];
  const liveActive = () => !!camsEl.querySelector('.cam-view.live');
  mount(page, h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'prompt' }, h('b', {}, 'skymesh'), ':~$ cams status'), h('h1', {}, 'Camera Nodes'))),
    fallEl,
    camsEl, h('h2', {}, 'Analysis sessions'), sessEl,
    h('p', { class: 'dim' }, 'Analysis runs on this computer, not on the ESP32-CAM. Detections are anonymous: no face recognition, and a person is linked to a badge only when an operator confirms it.'));

  let settings = null;
  function renderFall() {
    if (!can('settings.manage') || !settings) { mount(fallEl); return; }
    const on_ = !!settings['camera.fall_detection_enabled'];
    const still = h('input', { type: 'number', min: 3, max: 120, value: settings['camera.fall_still_seconds'], 'aria-label': 'Seconds still before alerting' });
    const idle = h('input', { type: 'number', min: 0, max: 3600, value: settings['camera.no_movement_seconds'], 'aria-label': 'Seconds without movement before alerting' });
    still.style.width = '70px'; idle.style.width = '80px';
    mount(fallEl, h('div', { class: ['card', 'stack'] },
      h('div', { class: 'row' }, h('b', {}, 'Automatic SOS: collapse or no movement'), on_ ? badge('ON', 'b-ok') : badge('OFF', 'b-muted'), h('span', { class: 'spacer' }),
        h('button', { class: ['btn', 'small', !on_ && 'primary'], onclick: () => saveSettings({ 'camera.fall_detection_enabled': !on_ }) }, on_ ? 'Turn off' : 'Turn on')),
      h('div', { class: 'row dim' }, 'Collapse: alert after the person has been still on the floor for', still, 'seconds'),
      h('div', { class: 'row dim' }, 'No movement: alert when a person in view has not moved at all for', idle, 'seconds (0 = off)',
        h('button', { class: 'btn small', onclick: () => saveSettings({
          'camera.fall_still_seconds': Math.min(120, Math.max(3, Number(still.value) || 8)),
          'camera.no_movement_seconds': Math.min(3600, Math.max(0, Number(idle.value) || 0)) }) }, 'Save')),
      h('p', { class: 'dim' }, 'Watches the live video from every online camera. The alert is an SOS marked AUTOMATIC – VERIFY with a snapshot, so a responder can check: it cannot tell fainting from lying down or sitting very still on purpose, and it needs a fixed camera that can see the floor. Post a notice that the area is monitored.')));
  }
  async function saveSettings(values) {
    try { settings = await api.patch('settings', { values }); toast('Saved', 'ok'); } catch (e) { toast(e.message, 'bad'); }
    renderFall();
  }

  async function load(force = false) {
    const [cams, sessions] = await Promise.all([api.get('cameras'), api.get('cameras/sessions')]);
    if (can('settings.manage')) { settings = await api.get('settings').catch(() => settings); renderFall(); }
    if (force || !liveActive()) {  // never tear down a video someone is watching
      mount(camsEl, cams.length ? cams.map(camCard) : emptyState('No camera nodes yet. Use Add device to set one up.'));
    }
    mount(sessEl, sessions.length ? table([
      { label: 'Started', render: (s) => ago(s.started_at) }, { label: 'Camera', key: 'camera_device_id' },
      { label: 'Source', render: (s) => `${s.source}${s.source_name ? ` · ${s.source_name}` : ''}` },
      { label: 'Frames', key: 'frames_processed' }, { label: 'Detections', key: 'detections' }, { label: 'Tracks', key: 'tracks' },
      { label: 'Projected', render: (s) => s.projected ? String(s.projected) : badge('NOT ON MAP', 'b-muted') },
      { label: 'Detector', render: (s) => h('span', { class: 'mono dim' }, s.detector) },
      { label: 'Note', render: (s) => s.finished_at ? (s.note || '') : badge('RUNNING', 'b-info') },
    ], sessions, showTracks) : emptyState('No analysis yet.'));
  }

  // One camera: picture on the left (live video or snapshot), controls on the right.
  function camCard(c) {
    const pic = { flash: 0, brightness: 0, contrast: 0, saturation: 0, quality: 35, framesize: 'qvga', ...(c.picture || {}) };
    const empty = h('div', { class: 'cam-empty' }, c.online ? 'Press Live to watch, or Snapshot for one picture.' : 'Camera is offline.');
    const hud = h('span', { class: 'cam-hud hidden' });
    const view = h('div', { class: 'cam-view' }, empty, hud);
    let liveTimer = null, liveImg = null;

    const showImg = (src, alt) => {
      view.querySelectorAll('img').forEach((x) => { x.src = ''; x.remove(); });
      const im = h('img', { alt, src });
      view.prepend(im);
      empty.classList.add('hidden');
      return im;
    };
    const liveBtn = h('button', { class: 'btn primary', onclick: () => (liveTimer ? stopLive() : startLive()) }, 'Live');
    function startLive() {
      liveImg = showImg(`/api/v1/cameras/${c.id}/live?t=${Date.now()}`, `Live video from ${c.name}`);
      liveImg.onerror = () => { hud.textContent = 'video stopped: camera unreachable'; hud.classList.add('bad'); };
      hud.textContent = 'connecting…'; hud.classList.remove('hidden', 'bad');
      liveBtn.textContent = 'Stop';
      view.classList.add('live');
      liveTimer = setInterval(async () => {
        const s = await api.get(`cameras/${c.id}/live-status`).catch(() => null);
        if (!s) return;
        hud.classList.toggle('bad', !!s.error);
        hud.textContent = s.error ? s.error : `● LIVE  ${s.fps} fps · ${(s.frame_bytes / 1024).toFixed(1)} KB`;
      }, 2000);
      liveStops.push(stopLive);
    }
    function stopLive() {
      clearInterval(liveTimer); liveTimer = null;
      if (liveImg) { liveImg.src = ''; liveImg.remove(); liveImg = null; }  // closes the stream
      hud.classList.add('hidden');
      empty.classList.remove('hidden');
      view.classList.remove('live');
      liveBtn.textContent = 'Live';
    }
    async function snapshot() {
      if (liveTimer) stopLive();
      hud.textContent = 'fetching…'; hud.classList.remove('hidden', 'bad');
      try {
        const r = await fetch(`/api/v1/cameras/${c.id}/snapshot`, { credentials: 'same-origin' });
        if (!r.ok) throw new Error((await r.json()).error.message);
        showImg(URL.createObjectURL(await r.blob()), `Snapshot from ${c.name}`);
        hud.textContent = `snapshot ${new Date().toLocaleTimeString()}`;
      } catch (e) { hud.textContent = e.message; hud.classList.add('bad'); }
    }

    // Picture controls: each change is sent when the slider is released, and remembered by the camera.
    const slider = (label, key, min, max, step, fmt) => {
      const out = h('span', { class: 'mono dim' }, fmt(pic[key]));
      const input = h('input', { type: 'range', min, max, step, value: pic[key], 'aria-label': label });
      input.addEventListener('input', () => { out.textContent = fmt(Number(input.value)); });
      input.addEventListener('change', () => { pic[key] = Number(input.value); cmd(c, key, String(input.value)); });
      return h('label', { class: 'ctl' }, h('span', { class: 'ctl-l' }, label, out), input);
    };
    const signed = (v) => (v > 0 ? `+${v}` : String(v));
    const size = h('select', { 'aria-label': 'Resolution', onchange: () => cmd(c, 'framesize', size.value) },
      [['qqvga', '160×120 · fastest'], ['qvga', '320×240 · 12-15 fps'], ['vga', '640×480 · sharper, slower'],
        ...(c.software_jpeg ? [] : [['svga', '800×600 · sharpest']])].map(([v, l]) => h('option', { value: v, selected: v === pic.framesize }, l)));
    const controls = can('camera.configure') ? h('div', { class: 'cam-ctl' },
      h('div', { class: 'ctl-group' }, h('div', { class: 'ctl-h' }, 'Light'),
        slider('Flash LED', 'flash', 0, 80, 5, (v) => (v ? `${v}%` : 'off')),
        h('p', { class: 'dim ctl-note' }, 'The white LED next to the lens. Use a low level as fill light in the dark; it gets hot at high levels.')),
      h('div', { class: 'ctl-group' }, h('div', { class: 'ctl-h' }, 'Picture'),
        slider('Brightness', 'brightness', -2, 2, 1, signed),
        slider('Contrast', 'contrast', -2, 2, 1, signed),
        slider('Saturation', 'saturation', -2, 2, 1, signed),
        slider('Quality', 'quality', 10, 100, 5, (v) => `${v}`),
        h('label', { class: 'ctl' }, h('span', { class: 'ctl-l' }, 'Resolution'), size)),
      h('div', { class: 'row' },
        h('button', { class: 'btn small', title: 'Camera mounted upside down?', onclick: () => cmd(c, 'rotate180') }, 'Rotate 180°'),
        h('button', { class: 'btn small', onclick: () => { c.hmirror = !c.hmirror; cmd(c, 'hmirror', c.hmirror ? '1' : '0'); } }, 'Mirror'),
        h('button', { class: 'btn small', onclick: async () => { await cmd(c, 'reset'); load(true); } }, 'Reset'))) : null;

    const fall = c.live_analysis?.fall_detection ? `watching · ${c.live_analysis.fall_state || 'idle'}` : (settings?.['camera.fall_detection_enabled'] ? 'starting…' : 'off');
    return h('section', { class: 'card cam-card' },
      h('div', { class: 'cam-head' }, h('b', {}, c.name), onlineBadge(c.online),
        c.sensor ? badge(c.sensor, 'b-accent') : null,
        c.camera_ok === false ? badge('SENSOR FAULT', 'b-bad') : null,
        h('span', { class: 'spacer' }), h('span', { class: 'mono dim' }, c.firmware_version || '')),
      h('div', { class: 'cam-body' },
        h('div', { class: 'stack' }, view,
          h('div', { class: 'row' }, liveBtn, h('button', { class: 'btn', onclick: snapshot }, 'Snapshot'),
            h('span', { class: 'spacer' }),
            can('camera.configure') ? h('button', { class: 'btn small', onclick: () => analyzeNow(c) }, 'Find people') : null,
            can('camera.configure') ? h('button', { class: 'btn small', onclick: () => calibrate(c) }, 'Calibrate') : null,
            can('camera.configure') ? h('button', { class: 'btn small', onclick: () => uploadVideo(c) }, 'Video file…') : null),
          h('div', { class: 'cam-facts' },
            fact('Last seen', c.last_seen_at ? ago(c.last_seen_at) : 'never'),
            fact('On the map', c.placement ? 'placed' : 'not placed'),
            fact('Auto SOS', fall),
            fact('Image', c.software_jpeg ? 'encoded on chip' : (c.sensor ? 'sensor JPEG' : '—')))),
        controls));
  }
  const fact = (k, v) => h('div', { class: 'fact' }, h('span', {}, k), h('b', {}, v));

  async function cmd(c, command, val = '') {
    try {
      await api.post(`cameras/${c.id}/control`, { cmd: command, val });
      if (['rotate180', 'hmirror', 'reset'].includes(command)) toast('Done: the picture changes on the next frame', 'ok');
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function analyzeNow(c) {
    try { const r = await api.post(`cameras/${c.id}/analyze-snapshot`); toast(`${r.observations.length} person observation(s)`, 'ok'); load(); }
    catch (e) { toast(e.message, 'bad'); }
  }

  async function calibrate(c) {
    const buildings = await api.get('buildings');
    const floors = buildings.flatMap((b) => b.floors.map((f) => ({ value: f.id, label: `${b.name} — ${f.name}` })));
    const v = await formDialog(`Calibrate ${c.name}`, [
      { name: 'floor_id', label: 'Floor seen by the camera', type: 'select', value: floors[0]?.value, options: floors },
      { name: 'frame', label: 'Camera frame size (w×h px)', value: '800x600' },
      { name: 'image_points', label: 'Image points (pixels) — 4+ floor-level points, “u,v” per line', type: 'textarea', required: true, placeholder: '120,560\n690,550\n610,300\n200,310' },
      { name: 'map_points', label: 'Matching map points (normalized 0..1) — same order', type: 'textarea', required: true, placeholder: '0.62,0.27\n0.66,0.27\n0.66,0.20\n0.62,0.20' }],
    'Save', 'Pick points on the FLOOR (tile corners, door thresholds) that you can also locate on the plan. Only floor-plane positions project correctly; the camera must not move afterwards.');
    if (!v) return;
    const parse = (t) => t.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => l.split(/[ ,;]+/).map(Number));
    const [fw, fh] = v.frame.toLowerCase().split('x').map(Number);
    try {
      const r = await api.put(`cameras/${c.id}/calibration`, { floor_id: v.floor_id, frame_w: fw, frame_h: fh, image_points: parse(v.image_points), map_points: parse(v.map_points) });
      toast(`Calibration saved (max reprojection error ${r.max_reprojection_error})`, 'ok'); load();
    } catch (e) { toast(e.message, 'bad'); }
  }

  async function uploadVideo(c) {
    const v = await formDialog('Analyse authorized footage', [
      { name: 'file', label: 'Video (mp4/mov/avi/mkv)', type: 'file', accept: 'video/*', required: true },
      { name: 'fps', label: 'Frames per second to sample', type: 'number', value: 2, step: '0.5' }], 'Upload',
    'Only analyse footage you are authorized to process. Results are stored as anonymous observations with the configured retention period.');
    if (!v?.file) return;
    const fd = new FormData(); fd.append('file', v.file); fd.append('sample_fps', String(v.fps || 2));
    try { await api.post(`cameras/${c.id}/videos`, fd); toast('Uploaded — analysis running in the background', 'ok'); setTimeout(load, 1500); }
    catch (e) { toast(e.message, 'bad'); }
  }

  async function showTracks(sess) {
    const tracks = await api.get(`cameras/sessions/${sess.id}/tracks`);
    const devices = can('association.confirm') ? (await api.get('devices')).filter((d) => ['badge', 'phone'].includes(d.type) && !d.revoked_at) : [];
    await openDialog(`Session ${sess.id}`, tracks.length ? table([
      { label: 'Track', render: (t) => h('b', {}, t.track_label) }, { label: 'Frames', key: 'frames' },
      { label: 'Max conf.', render: (t) => `${Math.round(t.max_confidence * 100)}%` },
      { label: 'On map', render: (t) => t.projected_frames ? `${t.projected_frames} frames` : 'no' },
      { label: 'Identity', render: (t) => t.association ? h('div', { class: 'row' }, badge('CONFIRMED', 'b-ok'), t.association.device_name,
        can('association.confirm') ? h('button', { class: 'btn small', onclick: () => api.del(`associations/${t.association.id}`).then(() => toast('Association removed')) }, 'Remove') : null)
        : can('association.confirm') && devices.length ? h('button', { class: 'btn small', onclick: () => associate(sess, t, devices) }, 'Confirm identity…') : h('span', { class: 'dim' }, 'unknown') },
    ], tracks) : emptyState('No people detected in this session.'), [{ label: 'Close', value: true }]);
  }

  async function associate(sess, t, devices) {
    const v = await formDialog(`Who is ${t.track_label}?`, [
      { name: 'device_id', label: 'Badge / phone carried by this person', type: 'select', value: devices[0].id, options: devices.map((d) => ({ value: d.id, label: `${d.name}${d.owner_name ? ` (${d.owner_name})` : ''}` })) },
      { name: 'note', label: 'How did you verify this?', required: true, placeholder: 'e.g. confirmed in person at the camera location' }],
    'Confirm', 'Only confirm when you have verified it yourself. Appearance alone is not evidence. This action is audited.');
    if (v) api.post('associations', { session_id: sess.id, track_label: t.track_label, ...v }).then(() => toast('Association recorded', 'ok')).catch((e) => toast(e.message, 'bad'));
  }

  await load();
  // Background refreshes: a moment without the backend is shown by the status bar, not as an error here.
  const quiet = () => load().catch(() => {});
  const offs = [on('ws:analysis_session', quiet)];
  const timer = setInterval(quiet, 15000);
  return () => { offs.forEach((f) => f()); clearInterval(timer); liveStops.forEach((f) => f()); };
}
