// Shared client state: session, live connection, offline outboxes, SOS delivery and location sharing.
import { api, ApiError } from './api.js';
import { uid, toast } from './ui.js';

const bus = new EventTarget();
export const on = (type, fn) => { bus.addEventListener(type, fn); return () => bus.removeEventListener(type, fn); };
export const emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));

export const state = {
  me: null, perms: new Set(), config: null,
  conn: 'connecting', // connecting | online | offline
};
export const can = (p) => state.perms.has(p);

function store(key, fallback) {
  try { return JSON.parse(localStorage.getItem(`skymesh.${key}`)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(`skymesh.${key}`, JSON.stringify(value)); } catch { /* storage unavailable: keep in memory */ }
}
// Outboxes are per user so a shared computer never replays someone else's queue.
const userKey = (k) => `${state.me?.id || 'anon'}.${k}`;

// ---------------------------------------------------------------- live connection

let ws, backoff = 1000, pingTimer, retryTimer;
export async function connectLive() {
  if (ws && ws.readyState <= 1) return;
  clearTimeout(retryTimer);
  setConn('connecting');
  if (backoff > 1000) {
    // After a failure, probe with a cheap request first instead of opening sockets that will fail.
    try { await fetch('/api/v1/health', { cache: 'no-store' }); } catch {
      setConn('offline');
      retryTimer = setTimeout(connectLive, backoff);
      backoff = Math.min(backoff * 2, 30000);
      return;
    }
    if (!state.me) return;
  }
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/v1/ws`);
  ws.onopen = () => {
    backoff = 1000; setConn('online');
    clearInterval(pingTimer);
    pingTimer = setInterval(() => ws.readyState === 1 && ws.send('ping'), 25000);
    flushChatOutbox(); flushSos();
    emit('reconnected');
  };
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.type && ev.type !== 'pong') emit(`ws:${ev.type}`, ev.data);
  };
  ws.onclose = (e) => {
    clearInterval(pingTimer);
    setConn('offline');
    if (e.code === 4401 || !state.me) return; // not signed in
    backoff = Math.min(backoff * 2, 30000);
    retryTimer = setTimeout(connectLive, backoff);
  };
}
export function disconnectLive() { clearTimeout(retryTimer); if (ws) { ws.onclose = null; ws.close(); ws = null; } }
function setConn(c) { state.conn = c; emit('conn', c); }
window.addEventListener('online', () => connectLive());

// ---------------------------------------------------------------- chat outbox (queued -> sent)

export function chatOutbox() { return store(userKey('outbox'), []); }
export function queueChat(convId, body) {
  const item = { conv: convId, client_msg_id: uid(), body, queued_at: new Date().toISOString(), state: 'queued' };
  save(userKey('outbox'), [...chatOutbox(), item]);
  emit('outbox');
  flushChatOutbox();
  return item;
}
let flushing = false;
export async function flushChatOutbox() {
  if (flushing) return;
  flushing = true;
  try {
    for (const item of chatOutbox()) {
      try {
        const r = await api.post(`chat/conversations/${item.conv}/messages`, { client_msg_id: item.client_msg_id, body: item.body });
        save(userKey('outbox'), chatOutbox().filter((x) => x.client_msg_id !== item.client_msg_id));
        emit('chat:sent', r.message);
      } catch (e) {
        if (e.offline || e.status >= 500 || e.status === 429) { markOutbox(item, 'queued', e.message); break; }
        markOutbox(item, 'failed', e.message); // permanent (403/404/422): keep visible, do not retry
      }
    }
  } finally { flushing = false; emit('outbox'); }
}
function markOutbox(item, st, err) {
  save(userKey('outbox'), chatOutbox().map((x) => x.client_msg_id === item.client_msg_id ? { ...x, state: st, error: err } : x));
}
export function dropOutbox(id) { save(userKey('outbox'), chatOutbox().filter((x) => x.client_msg_id !== id)); emit('outbox'); }

// ---------------------------------------------------------------- SOS (never "green" until the backend confirms)

// sos = { key, created_at, phase: 'sending'|'not_delivered'|'delivered', incident, error }
export function currentSos() { return store(userKey('sos'), null); }
function setSos(v) { save(userKey('sos'), v); emit('sos', v); }
export async function raiseSos(message) {
  const existing = currentSos();
  if (existing && existing.phase !== 'closed' && !['RESOLVED', 'FALSE_ALARM'].includes(existing.incident?.state)) return flushSos();
  setSos({ key: `sos-${uid()}`, created_at: new Date().toISOString(), phase: 'sending', message, device_id: locationShare.deviceId() });
  return flushSos();
}
let sosTimer;
export async function flushSos() {
  const s = currentSos();
  if (!s || s.phase === 'delivered' || s.phase === 'closed') return;
  clearTimeout(sosTimer);
  try {
    const r = await api.post('incidents/sos', { idempotency_key: s.key, message: s.message || null, device_id: s.device_id || null,
      timestamp: s.created_at });
    setSos({ ...s, phase: 'delivered', incident: r.incident, error: null });
  } catch (e) {
    setSos({ ...s, phase: 'not_delivered', error: e.message, attempts: (s.attempts || 0) + 1 });
    if (e.offline || e.status >= 500 || e.status === 429) sosTimer = setTimeout(flushSos, 5000);
  }
}
export function clearSos() { setSos(null); }
on('ws:incident', (e) => {
  const s = currentSos();
  if (s?.incident && e.detail.id === s.incident.id) setSos({ ...s, incident: e.detail });
});

// ---------------------------------------------------------------- phone location sharing (foreground only)

export const locationShare = {
  watchId: null, lastSent: null, lastError: null, seq: store('loc.seq', 0),
  deviceId: () => store(userKey('phone_id'), null),
  floorId: () => store(userKey('share_floor'), null),
  setFloor(id) { save(userKey('share_floor'), id); },
  active() { return this.watchId !== null; },

  async ensurePhone() {
    let id = this.deviceId();
    if (id) {
      const mine = await api.get('devices');
      if (mine.some((d) => d.id === id && !d.revoked_at)) return id;
    }
    const name = `${navigator.userAgentData?.platform || navigator.platform || 'Browser'} · ${new Date().toLocaleDateString()}`;
    const d = await api.post('phones', { name: name.slice(0, 60) });
    save(userKey('phone_id'), d.id);
    return d.id;
  },

  async start() {
    if (!('geolocation' in navigator)) {
      const id = await this.ensurePhone();
      await api.post(`phones/${id}/sharing`, { enabled: false, permission_state: 'unsupported' });
      throw new Error('This browser has no geolocation support. You can still pick your room manually.');
    }
    if (!window.isSecureContext) throw new Error('Browsers only allow location on HTTPS (or localhost). Open SkyMesh over https://');
    const id = await this.ensurePhone();
    await api.post(`phones/${id}/sharing`, { enabled: true, permission_state: 'granted' });
    this.watchId = navigator.geolocation.watchPosition((p) => this.send(id, p), (err) => this.fail(id, err),
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
    emit('sharing', true);
  },

  async send(id, p) {
    // After "I'm here" the person's own word beats indoor GPS for a few minutes, unless GPS is really good.
    if (this.pinnedUntil > Date.now() && p.coords.accuracy > 10) return;
    this.seq += 1; save('loc.seq', this.seq);
    const body = { kind: 'geo', lat: p.coords.latitude, lon: p.coords.longitude, accuracy_m: Math.round(p.coords.accuracy * 10) / 10,
      timestamp: new Date(p.timestamp).toISOString(), sequence: this.seq, source: 'browser_geolocation', floor_id: this.floorId() || null };
    try {
      this.lastSent = { ...(await api.post(`phones/${id}/location`, body)), accuracy_m: body.accuracy_m, at: new Date().toISOString() };
      this.lastError = null;
    } catch (e) { this.lastError = e.message; }
    emit('sharing', true);
  },

  async fail(id, err) {
    this.lastError = err.message || 'Location unavailable';
    if (err.code === 1) { // PERMISSION_DENIED
      await api.post(`phones/${id}/location`, { kind: 'denied', reason: 'PERMISSION_DENIED' }).catch(() => {});
      this.stopWatch();
      toast('Location permission was denied. Sharing is off. You can still pick your room manually.', 'bad', 8000);
    } else {
      await api.post(`phones/${id}/location`, { kind: 'unavailable', reason: err.code === 3 ? 'TIMEOUT' : 'POSITION_UNAVAILABLE' }).catch(() => {});
    }
    emit('sharing', this.active());
  },

  async shareRoom(roomId) {
    const id = await this.ensurePhone();
    await api.post(`phones/${id}/sharing`, { enabled: true });
    this.lastSent = { ...(await api.post(`phones/${id}/location`, { kind: 'room', room_id: roomId, source: 'manual_room' })), at: new Date().toISOString() };
    emit('sharing', this.active());
  },

  // "I'm here": the person marks their own position on the map (shown to responders as set by hand).
  async sharePoint(floorId, x, y) {
    const id = await this.ensurePhone();
    await api.post(`phones/${id}/sharing`, { enabled: true });
    this.setFloor(floorId);
    this.pinnedUntil = Date.now() + 5 * 60 * 1000;
    this.lastSent = { ...(await api.post(`phones/${id}/location`, { kind: 'floor', floor_id: floorId, x, y, source: 'manual_point' })), at: new Date().toISOString() };
    emit('sharing', this.active());
    return this.lastSent;
  },

  stopWatch() { if (this.watchId !== null) navigator.geolocation.clearWatch(this.watchId); this.watchId = null; },

  async stop() {
    this.stopWatch();
    const id = this.deviceId();
    if (id) await api.post(`phones/${id}/sharing`, { enabled: false }).catch((e) => { if (!(e instanceof ApiError) || e.status !== 404) throw e; });
    emit('sharing', false);
  },
};
