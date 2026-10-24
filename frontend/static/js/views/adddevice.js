// Add Device: plug a board in by USB → SkyMesh installs the firmware and configures it. No files, no Arduino IDE.
import { api } from '../api.js';
import { h, mount, toast, confirmDialog } from '../ui.js';
import { on } from '../state.js';
import { icon } from '../os/icons.js';

const TYPES = {
  badge: { label: 'Wearable badge', board: 'ESP32-S3-CAM', chip: 'ESP32-S3', icon: 'user',
    plug: 'Connect the badge to this computer with a USB-C data cable. Either USB port on the board works.' },
  camera_node: { label: 'Camera node', board: 'AI-Thinker ESP32-CAM', chip: 'ESP32', icon: 'cameras',
    plug: 'Seat the ESP32-CAM on its ESP32-CAM-MB adapter (camera facing away from the USB socket), then connect the adapter with a micro-USB data cable.' },
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ss = (k, v) => { try { if (v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch { /* ignore */ } return null; };

export async function render(page, ctx = {}) {
  const st = { step: 'choose', type: ctx.params?.type || 'badge', name: '', owner: '', ssid: ss('skymesh.wifi.ssid') || '', pass: ss('skymesh.wifi.pass') || '',
    url: '', users: [], manifest: null, enrolled: null, port: null, log: [], progress: 0, error: null, skipFlash: false };
  const body = h('div', { class: 'wizard' });
  mount(page, body);
  const cleanups = [];

  const [cfg, users, manifest, devices] = await Promise.all([api.get('config').catch(() => ({})), api.get('users').catch(() => []),
    api.get('firmware').catch((e) => ({ error: e.message })), api.get('devices').catch(() => [])]);
  st.devices = devices;
  st.url = st.autoUrl = cfg.device_url || `http://${location.hostname}:8000`;
  st.wifi = cfg.wifi || null;
  st.publicNets = cfg.public_networks || [];
  // Same network as this computer: the one that works. A name remembered from an earlier install loses
  // when this computer has since moved to another Wi-Fi (its saved password would be wrong too).
  if (st.wifi?.ssid && st.ssid !== st.wifi.ssid) { st.ssid = st.wifi.ssid; st.pass = ''; }
  st.country = ss('skymesh.wifi.country') || 'IN';
  st.users = users;
  st.manifest = manifest;

  const supported = 'serial' in navigator && window.isSecureContext;
  const log = (line) => { st.log.push(line); if (st.log.length > 200) st.log.shift(); const t = body.querySelector('.term'); if (t) { t.textContent = st.log.join('\n'); t.scrollTop = t.scrollHeight; } };

  function stepper() {
    const steps = [['choose', 'Choose'], ['plug', 'Plug in'], ['install', 'Install'], ['online', 'Online']];
    const idx = steps.findIndex(([k]) => k === st.step);
    return h('div', { class: 'wiz-steps' }, steps.map(([k, label], i) => h('span', { class: ['wiz-step', i < idx && 'done', i === idx && 'cur'] },
      h('b', {}, i < idx ? '✓' : String(i + 1)), label)));
  }

  function draw() {
    const T = TYPES[st.type];
    if (!supported) {
      mount(body, stepper(), h('div', { class: 'notice warn' }, 'USB installing needs Google Chrome or Microsoft Edge on the SkyMesh computer itself. ',
        'Open ', h('code', {}, 'http://localhost:8000'), ' in Chrome or Edge on that computer and try again.'));
      return;
    }
    if (st.manifest?.error) {
      mount(body, stepper(), h('div', { class: 'notice bad' }, `Firmware files are missing (${st.manifest.error}). Run backend/scripts/build_firmware.py once.`));
      return;
    }
    if (st.step === 'choose') {
      const name = h('input', { type: 'text', value: st.name || defaultName(), maxlength: 60 });
      const owner = h('select', {}, h('option', { value: '' }, '— nobody yet —'), st.users.map((u) => h('option', { value: u.id, selected: u.id === st.owner }, u.display_name)));
      const ssid = h('input', { type: 'text', value: st.ssid, placeholder: 'Wi-Fi name', autocomplete: 'off' });
      const pass = h('input', { type: 'password', value: st.pass, placeholder: 'Wi-Fi password', autocomplete: 'off' });
      const url = h('input', { type: 'text', value: st.url });
      const skip = h('input', { type: 'checkbox', checked: st.skipFlash });
      const country = h('select', {}, [['IN', 'India (channels 1-13)'], ['GB', 'UK / Europe (1-13)'], ['AE', 'Middle East (1-13)'],
        ['AU', 'Australia (1-13)'], ['JP', 'Japan (1-14)'], ['US', 'USA / Canada (1-11)']].map(([v, l]) => h('option', { value: v, selected: v === st.country }, l)));
      const warnings = [];
      if (st.wifi?.is_5ghz) warnings.push(`This computer is on “${st.wifi.ssid}” at 5 GHz. Badges and cameras only use 2.4 GHz: make sure that network (or your phone hotspot) also broadcasts on 2.4 GHz.`);
      if (st.publicNets.length) warnings.push(`Windows treats “${st.publicNets.join(', ')}” as a Public network, so its firewall will block the device. Fix: Settings → Network & internet → Wi-Fi → ${st.publicNets[0]} → Network profile type → Private.`);
      mount(body, stepper(),
        h('h3', { class: 'wiz-h' }, 'What are you adding?'),
        h('div', { class: 'wiz-cards' }, Object.entries(TYPES).map(([k, t]) => h('button', {
          class: ['wiz-card', st.type === k && 'sel'], onclick: () => { st.type = k; st.name = ''; draw(); } },
        icon(t.icon), h('b', {}, t.label), h('span', { class: 'dim' }, t.board)))),
        h('div', { class: 'grid two' },
          h('label', { class: 'field' }, 'Name', name),
          st.type === 'badge' ? h('label', { class: 'field' }, 'Who wears it', owner) : h('span'),
          h('label', { class: 'field' }, 'Wi-Fi name (2.4 GHz)', ssid,
            st.wifi?.ssid ? h('span', { class: 'dim' }, `This computer is on “${st.wifi.ssid}”${st.wifi.channel ? `, channel ${st.wifi.channel}` : ''}. Use the same network.`) : null),
          h('label', { class: 'field' }, 'Wi-Fi password', pass)),
        warnings.map((w) => h('div', { class: 'notice warn' }, w)),
        h('details', {}, h('summary', {}, 'Advanced'), h('div', { class: 'stack' },
          h('label', { class: 'field' }, 'Server address the device will use', url),
          h('label', { class: 'field' }, 'Wi-Fi country (which channels the device may use)', country),
          h('label', { class: 'row' }, skip, 'The board already has the latest SkyMesh firmware: only configure it'))),
        h('div', { class: 'row wiz-actions' }, h('span', { class: 'spacer' }), h('button', { class: 'btn primary', onclick: async () => {
          if (!name.value.trim() || !ssid.value.trim()) return toast('Enter a name and the Wi-Fi name', 'bad');
          if (st.wifi?.ssid && ssid.value.trim() !== st.wifi.ssid && !(await confirmDialog('Different Wi-Fi',
            `This computer is on “${st.wifi.ssid}”, but you entered “${ssid.value.trim()}”. The device can only reach SkyMesh on the same network as this computer, so it will not come online. Continue anyway?`,
            'Continue anyway', 'danger'))) return;
          Object.assign(st, { name: name.value.trim(), owner: owner.value, ssid: ssid.value.trim(), pass: pass.value, url: url.value.trim(),
            skipFlash: skip.checked, country: country.value, step: 'plug' });
          ss('skymesh.wifi.country', st.country);
          ss('skymesh.wifi.ssid', st.ssid); ss('skymesh.wifi.pass', st.pass);  // remembered for this browser session only
          draw();
        } }, 'Next')));
    } else if (st.step === 'plug') {
      mount(body, stepper(),
        h('h3', { class: 'wiz-h' }, `Plug in the ${T.label.toLowerCase()}`),
        h('div', { class: 'wiz-plug' }, icon('usb'), h('p', {}, T.plug)),
        h('p', { class: 'dim' }, 'Then press Connect and pick the new port in the list (usually “USB Serial”, “CH340”, “CP210x” or “USB JTAG/serial debug unit”).'),
        h('div', { class: 'row wiz-actions' }, h('button', { class: 'btn', onclick: () => { st.step = 'choose'; draw(); } }, 'Back'), h('span', { class: 'spacer' }),
          h('button', { class: 'btn primary', onclick: connectAndInstall }, 'Connect & install')));
    } else if (st.step === 'install') {
      const bar = h('i'); bar.style.width = `${st.progress}%`;
      mount(body, stepper(),
        h('h3', { class: 'wiz-h' }, st.error ? 'Something went wrong' : 'Installing… keep it plugged in'),
        h('div', { class: 'wiz-bar' }, bar), h('div', { class: 'dim mono' }, st.phase || ''),
        st.pressReset && !st.error ? h('div', { class: 'notice warn' }, h('b', {}, 'Press the RST button once'),
          st.type === 'camera_node' ? ' on the ESP32-CAM board itself (the small button next to the camera ribbon), not on the adapter.'
            : ' (labelled RST or RESET) on the badge.', ' Waiting up to a minute…') : null,
        st.error ? h('div', { class: 'notice bad' }, st.error) : null,
        st.error ? hints() : null,
        h('pre', { class: 'term' }, st.log.join('\n')),
        st.error ? h('div', { class: 'row wiz-actions' }, h('button', { class: 'btn', onclick: () => { Object.assign(st, { step: 'choose', error: null, enrolled: null }); draw(); } }, 'Start over'),
          h('span', { class: 'spacer' }), h('button', { class: 'btn primary', onclick: connectAndInstall }, 'Try again')) : null);
    } else if (st.step === 'online') {
      const ok = st.online;
      mount(body, stepper(),
        h('div', { class: 'wiz-done' }, ok ? h('span', { class: 'big-ok' }, icon('check')) : null,
          h('h3', { class: 'wiz-h' }, ok ? `${st.name} is online` : `Waiting for ${st.name} to connect…`),
          h('p', { class: 'dim' }, ok ? (st.type === 'badge'
            ? 'Test it: hold the badge’s BOOT button for 1.5 seconds. An SOS appears in Emergency; acknowledge it and the badge confirms.'
            : 'Open Cameras → Snapshot to see the picture, then place it on the map (Campus Map → Edit map → Place infrastructure).')
            : `It restarts and joins “${st.ssid}”. This usually takes 10–30 seconds. You can unplug it now and power it normally.`)),
        !ok && st.wifiProblem ? h('div', { class: 'notice bad' }, h('b', {}, `The device cannot join “${st.ssid}”: `), st.wifiProblem,
          '. Fix it, then go back and run Add device again with “only configure it” ticked (under Advanced).') : null,
        !ok && st.waitedLong ? hints(true) : null,
        h('pre', { class: 'term' }, st.log.join('\n')),
        h('div', { class: 'row wiz-actions' }, h('a', { class: 'btn', href: '#/devices' }, 'Open Devices'), h('span', { class: 'spacer' }),
          h('button', { class: 'btn primary', onclick: async () => {
            st.devices = await api.get('devices').catch(() => st.devices);
            Object.assign(st, { step: 'choose', name: '', enrolled: null, log: [], online: false, waitedLong: false });
            draw();
          } }, 'Add another device')));
    }
  }

  function hints(afterInstall) {
    return h('ul', { class: 'wiz-hints' },
      afterInstall ? [
        h('li', {}, 'Wi-Fi must be 2.4 GHz, and the name and password exact (they are case-sensitive).'),
        h('li', {}, 'Windows Firewall must allow SkyMesh on private networks (port 8000).'),
        h('li', {}, `The device connects to ${st.url}. That address must be reachable on this Wi-Fi.`),
      ] : [
        h('li', {}, 'Use a data cable: many USB cables only charge.'),
        st.type === 'camera_node' ? h('li', {}, 'ESP32-CAM: hold the IO0 button on the adapter, tap RST on the camera board, release IO0, then press Try again.') : null,
        st.type === 'badge' ? h('li', {}, 'Badge: hold BOOT, tap RESET (RST), release BOOT, then press Try again.') : null,
        h('li', {}, 'Close the Arduino IDE serial monitor or any other program using the port.'),
        h('li', {}, 'No port listed? Install the CH340 / CP210x USB driver for your board.'),
      ]);
  }

  function defaultName() {
    const base = st.type === 'badge' ? 'Badge' : 'Camera';
    const taken = new Set(st.devices.map((d) => d.name));
    let n = st.devices.filter((d) => d.type === st.type).length + 1;
    while (taken.has(`${base} ${n}`)) n++;
    return `${base} ${n}`;
  }

  // ------------------------------------------------------------ install pipeline

  async function connectAndInstall() {
    st.error = null; st.progress = 0; st.log = [];
    let port;
    try {
      port = await navigator.serial.requestPort();
    } catch { return; } // user closed the chooser
    st.port = port;
    st.step = 'install';
    draw();
    try {
      // This computer may have moved to another Wi-Fi since the wizard opened: hand the device the address it
      // has now (unless someone typed their own under Advanced).
      const fresh = await api.get('config').catch(() => null);
      if (fresh?.device_url && st.url === st.autoUrl && fresh.device_url !== st.url) {
        log(`This computer's address changed: the device will use ${fresh.device_url} (was ${st.url})`);
        st.url = st.autoUrl = fresh.device_url;
      }
      if (fresh?.wifi?.ssid && fresh.wifi.ssid !== st.ssid) {
        log(`Note: this computer is now on “${fresh.wifi.ssid}”, but the device is being set up for “${st.ssid}”.`);
      }
      const image = st.manifest.images.find((i) => i.role === st.type);
      if (!st.skipFlash) await flash(port, image);
      setPhase('Registering the device with SkyMesh…', 92);
      const creds = await enrollOnce();
      setPhase('Sending Wi-Fi and server settings to the device…', 95);
      await provision(port, creds);
      setPhase('Done. Waiting for it to come online…', 100);
      st.step = 'online';
      draw();
      waitOnline();
    } catch (e) {
      st.error = friendly(e);
      log(`ERROR: ${e.message || e}`);
      draw();
    }
  }

  function setPhase(text, pct) { st.phase = text; if (pct !== undefined) st.progress = pct; log(text); draw(); }

  async function flash(port, image) {
    setPhase('Loading the installer…', 2);
    const { ESPLoader, Transport } = await import('../../vendor/esptool-bundle.js');
    setPhase(`Downloading ${image.version} (${(image.size / 1048576).toFixed(1)} MB)…`, 4);
    const res = await fetch(`/api/v1/firmware/${image.file}`, { credentials: 'same-origin' });
    if (!res.ok) throw new Error('Could not download the firmware image from the server');
    const data = new Uint8Array(await res.arrayBuffer());
    const transport = new Transport(port, false);
    const terminal = { clean() {}, writeLine: (l) => log(l), write: (t) => log(t) };
    const loader = new ESPLoader({ transport, baudrate: 460800, romBaudrate: 115200, terminal, debugLogging: false });
    try {
      setPhase('Connecting to the chip…', 6);
      const desc = await loader.main();
      const chip = loader.chip.CHIP_NAME;
      log(`Found ${desc}`);
      if (chip !== image.chip) {
        throw new Error(`This board is an ${chip}, but a ${TYPES[st.type].label} needs an ${image.chip}. Go back and choose ${chip === 'ESP32-S3' ? 'Wearable badge' : 'Camera node'}.`);
      }
      setPhase('Writing firmware (about 30 seconds)…', 8);
      await loader.writeFlash({ fileArray: [{ data, address: image.address }], flashSize: 'keep', flashMode: 'keep', flashFreq: 'keep',
        eraseAll: false, compress: true,
        reportProgress: (_i, written, total) => { st.progress = 8 + Math.round((written / total) * 80); const b = body.querySelector('.wiz-bar i'); if (b) b.style.width = `${st.progress}%`; } });
      setPhase('Restarting the device…', 90);
      await loader.after('hard_reset');
    } finally {
      await transport.disconnect().catch(() => {});
    }
  }

  async function enrollOnce() {
    if (!st.enrolled) {
      const r = await api.post('devices', { type: st.type, name: st.name, owner_user_id: st.owner || null, hardware_model: TYPES[st.type].board });
      st.enrolled = { id: r.device.id, token: r.device_token, camkey: r.camera_key || '' };
      log(`Registered as ${r.device.id}`);
    }
    return st.enrolled;
  }

  async function openSerial(port) {
    // Native-USB boards re-appear after a restart: reopen the same port, or find it again.
    for (let attempt = 0; attempt < 12; attempt++) {
      try {
        await port.open({ baudRate: 115200, bufferSize: 4096 });
        await port.setSignals({ dataTerminalReady: false, requestToSend: false }).catch(() => {}); // release EN/IO0 on auto-reset adapters
        return port;
      } catch {
        const info = port.getInfo();
        const again = (await navigator.serial.getPorts()).find((p) => p !== port && p.getInfo().usbVendorId === info.usbVendorId
          && p.getInfo().usbProductId === info.usbProductId);
        if (again) port = again;
        await sleep(700);
      }
    }
    throw new Error('Could not reopen the USB port after the restart. Unplug the device, plug it back in, and press Try again (tick “only configure it” under Advanced).');
  }

  async function provision(port0, creds) {
    await sleep(1200);
    const port = await openSerial(port0);
    const reader = port.readable.getReader();
    const writer = port.writable.getWriter();
    const enc = new TextEncoder(), dec = new TextDecoder();
    let buf = '';
    const lines = [];
    let closed = false;
    const pump = (async () => {
      try {
        while (!closed) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, i).replace(/\r$/, '');
            buf = buf.slice(i + 1);
            if (line.trim()) { lines.push(line); log(`device> ${line}`); }
          }
        }
      } catch { /* port closed */ }
    })();
    const waitFor = async (prefix, ms) => {
      const t0 = Date.now();
      while (Date.now() - t0 < ms) {
        const hit = lines.find((l) => l.startsWith(prefix));
        if (hit) { lines.splice(lines.indexOf(hit), 1); return hit; }
        await sleep(100);
      }
      return null;
    };
    const send = (text) => writer.write(enc.encode(`${text}\n`));
    try {
      let info = null;
      for (let i = 0; i < 40 && !info; i++) {
        if (i === 3) {
          // Some adapters (e.g. many ESP32-CAM-MB boards) cannot restart the chip by themselves after flashing.
          st.pressReset = true;
          setPhase('Press the RST button on the board once now…');
        }
        await send('SKYMESH-INFO');
        info = await waitFor('SKYMESH-INFO', 1500) || await waitFor('SKYMESH-READY', 10);
      }
      st.pressReset = false;
      if (!info) throw new Error('The device did not answer. If you ticked “only configure it”, the board probably needs the firmware installed: untick it and try again.');
      const role = JSON.parse(info.slice(info.indexOf('{'))).role;
      if (role !== st.type) throw new Error(`This device runs ${role} firmware, not ${st.type}. Untick “only configure it” so the right firmware is installed.`);
      const cfg = { ssid: st.ssid, pass: st.pass, url: st.url, id: creds.id, token: creds.token, camkey: creds.camkey, country: st.country };
      lines.length = 0;
      await send(`SKYMESH-PROVISION ${JSON.stringify(cfg)}`);
      const res = await Promise.race([waitFor('SKYMESH-OK', 6000), waitFor('SKYMESH-ERR', 6000)]);
      if (!res) throw new Error('The device did not confirm the settings. Press Try again.');
      if (res.startsWith('SKYMESH-ERR')) throw new Error(`The device rejected the settings: ${res.slice(12)}`);
      log('Settings saved on the device. It restarts now.');
      // Keep listening until the device joins the Wi-Fi or says why it cannot (firmware 0.5.1+).
      setPhase(`Joining “${st.ssid}”…`);
      const joined = await Promise.race([waitFor('[wifi] connected', 25000), waitFor('[wifi] cannot join', 25000)]);
      st.wifiProblem = joined?.startsWith('[wifi] cannot join') ? joined.slice(joined.indexOf(': ') + 2).replace(/\s*\(reason \d+\)$/, '') : null;
      if (joined && !st.wifiProblem) log('Joined the Wi-Fi.');
    } finally {
      closed = true;
      await reader.cancel().catch(() => {});
      reader.releaseLock();
      writer.releaseLock();
      await pump;
      await port.close().catch(() => {});
    }
  }

  function waitOnline() {
    const id = st.enrolled?.id;
    const done = () => { if (!st.online) { st.online = true; draw(); toast(`${st.name} is online`, 'ok'); } };
    cleanups.push(on('ws:device_online', (e) => { if (e.detail.id === id) done(); }));
    cleanups.push(on('ws:device', (e) => { if (e.detail.id === id && e.detail.online) done(); }));
    const t0 = Date.now();
    const timer = setInterval(async () => {
      if (st.online || st.step !== 'online') return clearInterval(timer);
      const d = (await api.get('devices').catch(() => [])).find((x) => x.id === id);
      if (d?.online) done();
      else if (Date.now() - t0 > 60000 && !st.waitedLong) { st.waitedLong = true; draw(); }
    }, 3000);
    cleanups.push(() => clearInterval(timer));
  }

  function friendly(e) {
    const m = String(e?.message || e);
    if (/Failed to connect|Timeout|No serial data|Invalid head/i.test(m)) return 'Could not talk to the chip’s bootloader. Follow the tips below, then press Try again.';
    if (/already open|Failed to open|NetworkError|access denied/i.test(m)) return 'The USB port is busy. Close the Arduino serial monitor (or any app using it) and try again.';
    if (/The device has been lost|disconnected/i.test(m)) return 'The device was unplugged during the install. Plug it back in and press Try again.';
    return m;
  }

  draw();
  return () => cleanups.forEach((f) => f());
}
